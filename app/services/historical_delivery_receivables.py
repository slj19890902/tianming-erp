"""Explicit historical receivables; never repeat a delivery or stock movement.

The caller owns the transaction. Use only with a reviewed source manifest and
explicit owner authorization to retain prior records and recognize the new
period independently. Ordinary surcharge clients cannot create this source.
"""
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import re

from fastapi import HTTPException
from sqlalchemy import select

from app.core.time_contract import utc_now_naive
from app.models.customer_charge import CustomerCharge
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.delivery import Delivery, DeliveryItem
from app.services.audit_log import append_audit_event
from app.services.customer_price_tax import resolve_customer_price_tax_terms


def historical_snapshot(charge):
    raw = getattr(charge, "historical_delivery_snapshot_json", None)
    if not raw:
        return None
    value = json.loads(raw)
    if value.get("schema_version") != 1 or value.get("source_kind") != "historical_delivery":
        raise ValueError("历史送货应收来源损坏，不能当作普通附加收费读取")
    return value


def historical_fields(raw):
    """Presentation shared by statement screens and both exports."""
    if not raw:
        return {}
    doc = historical_snapshot(type("Source", (), {"historical_delivery_snapshot_json": raw})())
    return {
        "product_code": doc["product_code"], "product_name": doc["product_name"],
        "specification": doc["specification"], "delivery_date": date.fromisoformat(doc["delivery_date"]),
        "delivery_number": doc["customer_receipt_number"],
        "actual_received_quantity": int(doc["quantity"]),
        "source_kind": "historical_delivery", "source_label": "历史送货应收补录",
    }


def import_historical_receivable(db, *, actor, source, authorization_reason):
    from app.api.finance import require_customer_access
    if not actor or not actor.is_active or actor.role not in {"admin", "boss"}:
        raise HTTPException(403, "历史应收补录仅限管理员或负责人")
    if not authorization_reason or not authorization_reason.strip():
        raise HTTPException(422, "必须记录本次保留旧账并独立确认新期应收的明确授权")
    required = {"customer_id", "product_id", "order_item_id", "source_sha256", "source_row",
                "delivery_date", "customer_receipt_number", "product_code", "product_name",
                "specification", "quantity", "unit", "gross_unit_price", "gross_amount",
                "reconciliation_month", "original_delivery_item_ids"}
    if set(source) != required:
        raise HTTPException(422, "历史送货来源字段不完整或包含未知字段")
    if not re.fullmatch(r"[0-9a-f]{64}", source["source_sha256"]) or type(source["source_row"]) is not int or source["source_row"] < 1:
        raise HTTPException(422, "来源文件摘要或行号无效")
    require_customer_access(source["customer_id"], actor, db)
    product = db.get(Product, source["product_id"])
    item = db.get(OrderItem, source["order_item_id"])
    order = db.get(Order, item.order_id) if item else None
    if not product or not order or item.product_id != product.id or product.customer_id != source["customer_id"] or order.customer_id != source["customer_id"]:
        raise HTTPException(409, "历史来源的产品、订单与客户不一致")
    if source["product_code"] not in {product.product_code, product.customer_material_code}:
        raise HTTPException(409, "来源存货编码与明确匹配的常用箱不一致")
    if not isinstance(source["specification"], str) or len(source["specification"]) > 1000 or not source["product_name"] or len(source["product_name"]) > 200:
        raise HTTPException(422, "历史产品名称或规格无效")
    from app.core.time_contract import beijing_today
    day = date.fromisoformat(source["delivery_date"])
    if day > beijing_today() or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", source["reconciliation_month"]):
        raise HTTPException(422, "历史送货日期或对账月份无效")
    quantity = Decimal(str(source["quantity"]))
    price = Decimal(str(source["gross_unit_price"]))
    amount = Decimal(str(source["gross_amount"]))
    if not all(x.is_finite() for x in (quantity, price, amount)) or quantity <= 0 or quantity != int(quantity) or price < 0 or amount <= 0:
        raise HTTPException(422, "历史数量或金额无效")
    if (quantity * price).quantize(Decimal(".01"), rounding=ROUND_HALF_UP) != amount:
        raise HTTPException(422, "历史数量、含税单价与来源金额不一致")
    if price != price.quantize(Decimal(".0001")) or amount != amount.quantize(Decimal(".01")):
        raise HTTPException(422, "来源单价或金额精度超出保存契约")
    ids = source["original_delivery_item_ids"]
    if not ids or len(ids) != len(set(ids)):
        raise HTTPException(422, "必须关联已有送货来源，不能伪造新送货")
    originals = list(db.scalars(select(DeliveryItem).where(DeliveryItem.id.in_(ids))))
    if len(originals) != len(ids):
        raise HTTPException(409, "原送货来源已变化")
    for original in originals:
        delivery = db.get(Delivery, original.delivery_id)
        if original.order_item_id != item.id or not delivery or delivery.customer_id != source["customer_id"] or delivery.status != "dispatched" or not original.is_current:
            raise HTTPException(409, "原送货来源不匹配或不再有效")
    if quantity > sum(original.delivered_quantity for original in originals):
        raise HTTPException(409, "关联旧送货数量不足，不得用历史应收代替真实补录送货")
    terms = resolve_customer_price_tax_terms(db, product.customer_id)
    if terms.price_tax_mode != "tax_inclusive":
        raise HTTPException(409, "当前导入契约仅支持经确认的含税来源")
    value = dict(source, schema_version=1, source_kind="historical_delivery",
                 customer_po=order.customer_po,
                 customer_drawing_number=product.customer_drawing_number or "",
                 authorization_reason=authorization_reason.strip(),
                 price_tax_mode="tax_inclusive", tax_rate=str(terms.tax_rate),
                 prior_financial_records_preserved=True, inventory_movement_created=False)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    key = f"{source['customer_id']}:{source['source_sha256']}:{source['source_row']}"
    prior = db.scalar(select(CustomerCharge).where(CustomerCharge.historical_source_key == key))
    if prior:
        if prior.historical_delivery_snapshot_json != encoded or prior.status == "cancelled":
            raise HTTPException(409, "同一原表行已有不同历史补录，不得重复或覆盖")
        return prior
    related = list(db.scalars(select(CustomerCharge).where(
        CustomerCharge.order_item_id == item.id,
        CustomerCharge.reconciliation_month == source["reconciliation_month"],
        CustomerCharge.historical_source_key.is_not(None),
        CustomerCharge.status != "cancelled")))
    prior_quantity = Decimal(0)
    for old in related:
        old_source = historical_snapshot(old)
        if old_source["source_sha256"] != source["source_sha256"]:
            raise HTTPException(409, "本订单明细本期已有其他文件的历史应收来源，须核对而不能重复导入")
        prior_quantity += old.quantity
    if prior_quantity + quantity > sum(original.delivered_quantity for original in originals):
        raise HTTPException(409, "同一来源累计补录数量超过关联旧送货数量")
    from app.api.invoice_tasks import _confirmed_rule_by_product
    rules = _confirmed_rule_by_product(db, product.customer_id, effective_on=beijing_today())
    rule = rules.get(product.id) or rules.get(None)
    charge = CustomerCharge(customer_id=product.customer_id, order_id=order.id, order_item_id=item.id,
        charge_type="other", display_name=source["product_name"], quantity=quantity, unit=source["unit"],
        unit_price=price, amount=amount, price_tax_mode="tax_inclusive", tax_rate=terms.tax_rate,
        tax_project_name=source["product_code"], tax_classification_code=rule.tax_classification_code if rule else None,
        reconciliation_month=source["reconciliation_month"], status="confirmed", version=1,
        note="历史送货应收补录；原表行" + str(source["source_row"]) + "；" + authorization_reason.strip(),
        historical_source_key=key, historical_delivery_snapshot_json=encoded,
        created_by=actor.id, updated_by=actor.id, confirmed_by=actor.id, confirmed_at=utc_now_naive())
    db.add(charge)
    db.flush()
    append_audit_event(db, actor=actor, event_category="business", result="success", source="script",
        module_code="finance", action_code="finance.historical_delivery.receivable_imported",
        resource="CustomerCharge", customer_id=product.customer_id,
        batch_id=source["source_sha256"], description="按明确授权补录独立期间历史送货应收",
        details={"charge_id": charge.id, "source_key": key, "source_sha256": hashlib.sha256(encoded.encode()).hexdigest(),
                 "quantity": str(quantity), "gross_amount": str(amount),
                 "original_delivery_item_ids": ids, "authorization_reason": authorization_reason.strip(),
                 "prior_records_preserved": True, "inventory_movement_created": False})
    return charge
