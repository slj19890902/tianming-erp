from __future__ import annotations

from typing import Any
import json
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models.delivery import DeliveryItem
from app.models.order import OrderItem
from app.models.product import Product
from app.services.product_specification import resolved_product_specification


def _text(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def sales_contract(*, unit, price, tax_mode, tax_rate, source):
    if not _text(unit):
        raise ValueError('缺少销售单位，请补齐常用箱销售单位')
    if price is None or not Decimal(str(price)).is_finite() or Decimal(str(price)) < 0:
        raise ValueError('缺少有效销售单价')
    if tax_mode not in ('tax_inclusive','tax_exclusive'):
        raise ValueError('缺少销售含税/未税依据，请完善订单销售口径')
    if tax_mode == 'tax_exclusive' and tax_rate is None:
        raise ValueError('未税销售单价缺少税率')
    if tax_rate is not None and (not Decimal(str(tax_rate)).is_finite() or not 0 <= Decimal(str(tax_rate)) <= 1):
        raise ValueError('销售税率无效')
    return json.dumps(dict(schema_version=1,currency='CNY',unit=_text(unit),unit_price=str(price),
        tax_mode=tax_mode,tax_rate=str(tax_rate) if tax_rate is not None else None,source=source),ensure_ascii=False,sort_keys=True)


def read_sales_contract(item):
    if not item.sales_contract_json:return None
    value=json.loads(item.sales_contract_json)
    if not isinstance(value,dict) or value.get('currency')!='CNY' or value.get('schema_version')!=1:
        raise ValueError('销售快照版本或币种异常')
    sales_contract(unit=value.get('unit'),price=value.get('unit_price'),tax_mode=value.get('tax_mode'),
                   tax_rate=value.get('tax_rate'),source=value.get('source'))
    return value


def statement_sales_terms(item, price, mode, rate):
    """New statements must use the same immutable sale as the dashboard."""
    try:
        value=read_sales_contract(item)
        if value:
            return Decimal(value['unit_price']),value['tax_mode'],Decimal(value['tax_rate'] or '0')
        return price,mode,rate
    except (ValueError,TypeError,ArithmeticError) as error:
        from fastapi import HTTPException
        raise HTTPException(409,f'存货编码 {item.product_code_snapshot}：销售冻结口径异常，请核对原送货记录') from error


def build_order_delivery_snapshot(
    db: Session,
    order_item: OrderItem,
) -> dict[str, str | None]:
    """Freeze customer-facing product facts when a delivery line is created."""

    product = db.get(Product, order_item.product_id) if order_item.product_id else None
    unit = _text(getattr(order_item,'sales_unit_snapshot',None)) or _text(product.unit if product else None)
    mode, rate = order_item.price_tax_mode_snapshot, order_item.tax_rate_snapshot
    source = {'kind':'order_at_delivery_creation','order_item_id':order_item.id,
              'unit_basis':'order_snapshot' if getattr(order_item,'sales_unit_snapshot',None) else 'product_at_delivery_creation'}
    if mode is None:
        # This function is only used to create a new/pending delivery contract,
        # never to backfill dispatched history. Record today's terms explicitly.
        from app.models.order import Order
        from app.services.customer_price_tax import resolve_customer_price_tax_terms
        order = db.get(Order, order_item.order_id)
        if order is not None:
            terms = resolve_customer_price_tax_terms(db, order.customer_id)
            mode, rate = terms.price_tax_mode, terms.tax_rate
            source.update(tax_basis='customer_terms_at_delivery_creation',
                          profile_id=terms.profile_id, profile_version=terms.profile_version)
    try:
        contract = sales_contract(unit=unit,price=order_item.unit_price,
            tax_mode=mode,tax_rate=rate,source=source)
    except ValueError as error:
        from fastapi import HTTPException
        raise HTTPException(422, f'存货编码 {order_item.snapshot_product_code}：{error}') from error
    return {
        'unit_snapshot':unit,
        'sales_contract_json':contract,
        "product_code_snapshot": (
            _text(order_item.snapshot_product_code)
            or _text(product.product_code if product else None)
        ),
        "product_name_snapshot": (
            _text(order_item.snapshot_product_name)
            or _text(product.product_name if product else None)
        ),
        "specification_snapshot": resolved_product_specification(
            order_item.snapshot_spec,
            product,
        ),
    }


def ensure_order_delivery_snapshot(
    db: Session,
    delivery_item: DeliveryItem,
    order_item: OrderItem,
) -> None:
    """Fill missing snapshots without rewriting facts already frozen on the delivery."""

    # Never reinterpret an already frozen sale when today's order/master changes.
    if delivery_item.sales_contract_json:
        return
    values = build_order_delivery_snapshot(db, order_item)
    for field, value in values.items():
        if not _text(getattr(delivery_item, field, None)) and value is not None:
            setattr(delivery_item, field, value)


def ensure_unordered_sales_contract(db, delivery, item):
    """Freeze an old pending draft on its first real dispatch, never history."""
    if item.sales_contract_json:
        return
    from app.services.customer_price_tax import resolve_customer_price_tax_terms
    from app.services.warehouse_inventory import WarehouseInventoryError
    terms=resolve_customer_price_tax_terms(db,delivery.customer_id)
    try:
        item.sales_contract_json=sales_contract(unit=item.unit_snapshot,price=item.unit_price_snapshot,
            tax_mode=terms.price_tax_mode,tax_rate=terms.tax_rate,
            source={'kind':'pending_unordered_first_dispatch','profile_id':terms.profile_id,'profile_version':terms.profile_version})
    except ValueError as error:
        raise WarehouseInventoryError(f'存货编码 {item.product_code_snapshot}：{error}',422) from error
