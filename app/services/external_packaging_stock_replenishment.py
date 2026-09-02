from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
import hashlib
import json
from types import SimpleNamespace
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.core.time_contract import beijing_today, utc_now_naive
from app.models.external_packaging_price import ExternalPackagingPriceVersion
from app.models.external_packaging_purchase import (
    ExternalPackagingPurchaseBatch,
    ExternalPackagingPurchaseItem,
    ExternalPackagingPurchaseOrder,
)
from app.models.product import Product
from app.models.stock_replenishment import (
    InventoryStockPolicy,
    StockReplenishmentOrder,
    StockReplenishmentOrderItem,
)
from app.models.supplier import (
    ExternalPackagingProduct,
    SupplierSupplyCategory,
)
from app.models.user import User
from app.services.corner_guard_pricing import (
    CATEGORY_CODE as CORNER_GUARD_CATEGORY_CODE,
    current_corner_guard_meter_price,
)
from app.services.external_packaging_purchase import (
    MONEY,
    PACKAGING_MISSING_PRICE_MESSAGE,
    SIX_PLACES,
    ExternalPurchaseContractError,
    _amounts,
    _next_purchase_number,
    _purchase_quantity_error,
    _quantity_error,
    _resolved_purchase_pricing,
    serialize_external_purchase_batch,
)


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    rendered = format(Decimal(value), "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _positive_decimal(value: Any, *, label: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ExternalPurchaseContractError(f"{label}格式无效") from error
    if not result.is_finite() or result <= 0:
        raise ExternalPurchaseContractError(f"{label}必须大于 0")
    if result.quantize(SIX_PLACES) != result:
        raise ExternalPurchaseContractError(f"{label}最多保留 6 位小数")
    return result


def _default_candidate_snapshot(product: Product) -> dict[str, Any]:
    try:
        rows = json.loads(product.external_packaging_candidate_snapshot_json or "[]")
    except (TypeError, json.JSONDecodeError) as error:
        raise ExternalPurchaseContractError(
            f"{product.product_code}的供应商候选格式无效，请重新保存常用箱"
        ) from error
    if not isinstance(rows, list) or not rows or any(
        not isinstance(row, dict) for row in rows
    ):
        raise ExternalPurchaseContractError(
            f"{product.product_code}没有可用供应商候选，请先维护常用箱"
        )
    defaults = [row for row in rows if bool(row.get("is_default"))]
    if len(rows) == 1 and not defaults:
        defaults = rows
    if len(defaults) != 1:
        raise ExternalPurchaseContractError(
            f"{product.product_code}必须且只能指定一个默认外购供应商"
        )
    return defaults[0]


def _current_price(
    db: Session,
    *,
    external_product: ExternalPackagingProduct,
    category_code: str,
) -> ExternalPackagingPriceVersion | None:
    as_of = beijing_today()
    if category_code == CORNER_GUARD_CATEGORY_CODE:
        return current_corner_guard_meter_price(
            db,
            external_product_id=external_product.id,
            product_version=external_product.version,
            as_of=as_of,
        )
    return db.scalar(
        select(ExternalPackagingPriceVersion)
        .where(
            ExternalPackagingPriceVersion.external_product_id
            == external_product.id,
            ExternalPackagingPriceVersion.product_version
            == external_product.version,
            ExternalPackagingPriceVersion.quote_unit
            == external_product.purchase_unit,
            ExternalPackagingPriceVersion.effective_from <= as_of,
            or_(
                ExternalPackagingPriceVersion.effective_to.is_(None),
                ExternalPackagingPriceVersion.effective_to >= as_of,
            ),
        )
        .order_by(
            ExternalPackagingPriceVersion.effective_from.desc(),
            ExternalPackagingPriceVersion.version_number.desc(),
        )
        .limit(1)
    )


def prepare_external_stock_purchase(
    db: Session,
    *,
    product: Product,
    finished_quantity: int,
    purchase_quantity_override: Any | None = None,
) -> dict[str, Any]:
    if product.supply_mode != "external_purchase":
        raise ExternalPurchaseContractError("当前常用箱不是纯外购产品")
    if finished_quantity <= 0:
        raise ExternalPurchaseContractError("建议备库数量必须大于 0")
    category_code = str(product.external_packaging_category_code or "").strip()
    specification_summary = str(
        product.external_packaging_specification_summary or ""
    ).strip()
    specification_json = str(
        product.external_packaging_specification_json or "{}"
    ).strip()
    purchase_unit = str(product.external_packaging_purchase_unit or "").strip()
    if not category_code or not specification_summary or not purchase_unit:
        raise ExternalPurchaseContractError(
            f"{product.product_code}缺少包材类别、客户采购规格或采购单位"
        )
    try:
        specification = json.loads(specification_json)
    except (TypeError, json.JSONDecodeError) as error:
        raise ExternalPurchaseContractError(
            f"{product.product_code}的客户采购规格格式无效"
        ) from error
    if not isinstance(specification, dict) or not specification:
        raise ExternalPurchaseContractError(
            f"{product.product_code}缺少客户采购规格"
        )
    order_basis = _positive_decimal(
        product.external_packaging_default_order_quantity_basis,
        label="客户产品数量基数",
    )
    purchase_basis = _positive_decimal(
        product.external_packaging_default_purchase_quantity_basis,
        label="供应商采购数量基数",
    )
    recommended_purchase_quantity = (
        Decimal(finished_quantity) * purchase_basis / order_basis
    )
    if (
        recommended_purchase_quantity.quantize(SIX_PLACES)
        != recommended_purchase_quantity
    ):
        raise ExternalPurchaseContractError(
            "当前备库数量按常用箱比例换算后超过 6 位小数，请调整目标库存"
        )
    purchase_quantity = (
        recommended_purchase_quantity.quantize(SIX_PLACES)
        if purchase_quantity_override is None
        else _positive_decimal(
            purchase_quantity_override,
            label="供应商采购数量",
        ).quantize(SIX_PLACES)
    )
    converted_finished_quantity = int(
        (purchase_quantity * order_basis / purchase_basis).to_integral_value(
            rounding=ROUND_FLOOR
        )
    )
    if converted_finished_quantity <= 0:
        raise ExternalPurchaseContractError(
            "供应商采购数量按常用箱比例不足 1 个成品，请增加采购数量"
        )

    snapshot = _default_candidate_snapshot(product)
    try:
        external_product_id = int(snapshot["external_product_id"])
        frozen_version = int(snapshot["external_product_version"])
        frozen_supplier_id = int(snapshot["supplier_id"])
    except (KeyError, TypeError, ValueError) as error:
        raise ExternalPurchaseContractError(
            f"{product.product_code}的默认供应商候选缺少稳定版本，请重新保存常用箱"
        ) from error
    external_product = db.scalar(
        select(ExternalPackagingProduct)
        .options(joinedload(ExternalPackagingProduct.supplier))
        .where(ExternalPackagingProduct.id == external_product_id)
    )
    if external_product is None or external_product.supplier is None:
        raise ExternalPurchaseContractError("常用箱默认外购产品或供应商不存在")
    supplier = external_product.supplier
    if (
        not external_product.is_active
        or not supplier.is_active
        or external_product.version != frozen_version
        or external_product.supplier_id != frozen_supplier_id
    ):
        raise ExternalPurchaseContractError(
            f"{external_product.supplier_product_code}资料已变化或停用，请先重新保存常用箱"
        )
    if (
        external_product.category_code != category_code
        or external_product.purchase_unit != purchase_unit
    ):
        raise ExternalPurchaseContractError(
            f"{external_product.supplier_product_code}的包材类别或采购单位与常用箱不一致"
        )
    if external_product.customer_scope_id not in (None, product.customer_id):
        raise ExternalPurchaseContractError("默认外购产品属于其他客户，不能用于本次备库")
    if not db.scalar(
        select(SupplierSupplyCategory.id).where(
            SupplierSupplyCategory.supplier_id == supplier.id,
            SupplierSupplyCategory.category_code == category_code,
            SupplierSupplyCategory.is_active.is_(True),
        )
    ):
        raise ExternalPurchaseContractError("默认供应商未启用当前包材供货类别")
    price = _current_price(
        db,
        external_product=external_product,
        category_code=category_code,
    )
    if price is None:
        raise ExternalPurchaseContractError(PACKAGING_MISSING_PRICE_MESSAGE)

    quantity_error = _purchase_quantity_error(
        purchase_quantity, unit=purchase_unit
    )
    if quantity_error:
        raise ExternalPurchaseContractError(quantity_error)
    component = SimpleNamespace(
        category_code=category_code,
        specification_json=specification_json,
        purpose=product.product_name or product.product_code,
    )
    pricing = _resolved_purchase_pricing(
        component, price, purchase_quantity=purchase_quantity
    )
    pricing_quantity = Decimal(pricing["pricing_quantity"])
    quantity_error = _quantity_error(
        price, pricing_quantity, unit=price.quote_unit
    )
    if quantity_error:
        raise ExternalPurchaseContractError(quantity_error)
    unit_price = Decimal(pricing["purchase_unit_price"])
    line_amount, tax_amount, total_amount = _amounts(
        price,
        quantity=purchase_quantity,
        unit_price=unit_price,
        tax_amount_per_purchase_unit=pricing["tax_amount_per_purchase_unit"],
    )
    return {
        "product": product,
        "external_product": external_product,
        "supplier": supplier,
        "price": price,
        "pricing": pricing,
        "category_code": category_code,
        "specification_summary": specification_summary,
        "specification_json": specification_json,
        "purchase_unit": purchase_unit,
        "finished_quantity": converted_finished_quantity,
        "suggested_finished_quantity": int(finished_quantity),
        "order_basis": order_basis,
        "purchase_basis": purchase_basis,
        "purchase_quantity": purchase_quantity,
        "unit_price": unit_price,
        "line_amount": line_amount,
        "tax_amount": tax_amount,
        "total_amount": total_amount,
    }


def external_stock_draft(
    db: Session,
    *,
    policy: InventoryStockPolicy,
    finished_quantity: int,
) -> dict[str, Any]:
    product = policy.product
    if product is None:
        raise ExternalPurchaseContractError("库存预警关联的外购常用箱不存在")
    prepared = prepare_external_stock_purchase(
        db, product=product, finished_quantity=finished_quantity
    )
    supplier = prepared["supplier"]
    return {
        "procurement_mode": "external_purchase",
        "supplier_name": supplier.display_name or supplier.standard_name,
        "draft_ready": True,
        "missing_fields": [],
        "item": {
            "stock_policy_id": policy.id,
            "procurement_mode": "external_purchase",
            "target_inventory_type": "finished",
            "product_id": product.id,
            "reference_product_id": product.id,
            "customer_id": product.customer_id,
            "product_code": product.product_code,
            "product_name": product.product_name,
            "material_id": None,
            "material_code": None,
            "layer_count": None,
            "flute_type": None,
            "report_length_mm": None,
            "report_width_mm": None,
            "sheet_type": "raw_board",
            "component_type": "whole",
            "pieces_per_box": 1,
            "stock_yield_per_sheet": 1,
            "quantity": int(finished_quantity),
            "suggested_finished_quantity": int(finished_quantity),
            "external_category_code": prepared["category_code"],
            "external_specification_summary": prepared[
                "specification_summary"
            ],
            "external_purchase_quantity": _decimal_text(
                prepared["purchase_quantity"]
            ),
            "external_purchase_unit": prepared["purchase_unit"],
            "external_order_quantity_basis": _decimal_text(
                prepared["order_basis"]
            ),
            "external_purchase_quantity_basis": _decimal_text(
                prepared["purchase_basis"]
            ),
            "external_supplier_product_code": prepared[
                "external_product"
            ].supplier_product_code,
            "external_supplier_product_name": prepared[
                "external_product"
            ].product_name,
            "draft_ready": True,
            "missing_fields": [],
        },
    }


def create_external_stock_replenishment_purchase(
    db: Session,
    *,
    policy: InventoryStockPolicy,
    finished_quantity: int,
    purchase_quantity: Any | None = None,
    order_number: str,
    idempotency_key: str,
    remark: str | None,
    user: User,
) -> StockReplenishmentOrder:
    existing_order = replay_external_stock_replenishment_purchase(
        db,
        policy=policy,
        finished_quantity=finished_quantity,
        purchase_quantity=purchase_quantity,
        idempotency_key=idempotency_key,
    )
    if existing_order is not None:
        return existing_order

    product = policy.product or db.get(Product, policy.product_id)
    if product is None or product.deleted_at is not None or not product.is_active:
        raise ExternalPurchaseContractError("库存预警关联的外购常用箱不可用")
    prepared = prepare_external_stock_purchase(
        db,
        product=product,
        finished_quantity=finished_quantity,
        purchase_quantity_override=purchase_quantity,
    )
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "stock_policy_id": int(policy.id),
                "product_id": int(product.id),
                "finished_quantity": int(prepared["finished_quantity"]),
                "purchase_quantity": _decimal_text(prepared["purchase_quantity"]),
                "order_basis": _decimal_text(prepared["order_basis"]),
                "purchase_basis": _decimal_text(prepared["purchase_basis"]),
                "external_product_id": int(prepared["external_product"].id),
                "price_version_id": int(prepared["price"].id),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    order = StockReplenishmentOrder(
        order_number=order_number,
        supplier_name=(
            prepared["supplier"].display_name
            or prepared["supplier"].standard_name
        ),
        customer_id=product.customer_id,
        source_type="stock_warning",
        status="confirmed",
        remark=remark,
        created_by=user.id,
        confirmed_by=user.id,
        confirmed_at=utc_now_naive(),
    )
    item = StockReplenishmentOrderItem(
        stock_policy_id=policy.id,
        target_inventory_type="finished",
        procurement_route_snapshot="external_packaging",
        product_id=product.id,
        reference_product_id=product.id,
        customer_id=product.customer_id,
        product_code_snapshot=product.product_code,
        product_name_snapshot=product.product_name,
        sheet_type="raw_board",
        component_type="whole",
        pieces_per_box=1,
        stock_yield_per_sheet=1,
        quantity=int(prepared["finished_quantity"]),
        stocked_quantity=0,
        remark=remark,
    )
    order.items = [item]
    db.add(order)
    db.flush()

    batch = ExternalPackagingPurchaseBatch(
        sales_order_id=None,
        stock_replenishment_order_id=order.id,
        idempotency_key=idempotency_key,
        request_fingerprint=fingerprint,
        confirmed_by=user.id,
    )
    db.add(batch)
    db.flush()
    supplier = prepared["supplier"]
    price = prepared["price"]
    purchase_order = ExternalPackagingPurchaseOrder(
        batch_id=batch.id,
        purchase_number=_next_purchase_number(db, beijing_today()),
        supplier_id=supplier.id,
        supplier_name_snapshot=supplier.display_name or supplier.standard_name,
        supplier_business_code_snapshot=supplier.business_code,
        currency=price.currency,
        status="confirmed",
        goods_amount=Decimal(prepared["line_amount"]).quantize(MONEY),
        tax_amount=Decimal(prepared["tax_amount"]).quantize(MONEY),
        total_amount=Decimal(prepared["total_amount"]).quantize(MONEY),
        confirmed_by=user.id,
    )
    db.add(purchase_order)
    db.flush()
    external_product = prepared["external_product"]
    pricing = prepared["pricing"]
    db.add(
        ExternalPackagingPurchaseItem(
            purchase_order_id=purchase_order.id,
            sales_order_id=None,
            sales_order_item_id=None,
            order_component_id=None,
            order_candidate_id=None,
            stock_replenishment_item_id=item.id,
            customer_product_id_snapshot=product.id,
            order_quantity_basis_snapshot=prepared["order_basis"],
            purchase_quantity_basis_snapshot=prepared["purchase_basis"],
            purpose_snapshot=f"库存预警备库｜{product.product_code}",
            category_code_snapshot=prepared["category_code"],
            specification_summary_snapshot=prepared["specification_summary"],
            specification_json_snapshot=prepared["specification_json"],
            external_product_id_snapshot=external_product.id,
            external_product_version_snapshot=external_product.version,
            supplier_product_code_snapshot=external_product.supplier_product_code,
            product_name_snapshot=external_product.product_name,
            price_version_id=price.id,
            price_version_number_snapshot=price.version_number,
            purchase_quantity=prepared["purchase_quantity"],
            purchase_unit=prepared["purchase_unit"],
            unit_price=prepared["unit_price"],
            currency=price.currency,
            tax_mode=price.tax_mode,
            tax_rate=price.tax_rate,
            tax_amount_per_unit=pricing["tax_amount_per_purchase_unit"],
            line_amount=prepared["line_amount"],
            tax_amount=prepared["tax_amount"],
            total_amount=prepared["total_amount"],
            tier_basis_json=json.dumps(
                pricing["tier_prices"], ensure_ascii=False, separators=(",", ":")
            ),
            moq_quantity_snapshot=price.moq_quantity,
            packaging_multiple_snapshot=price.packaging_multiple,
            shipping_fee_mode=price.shipping_fee_mode,
            shipping_fee_snapshot=price.shipping_fee,
            sample_fee_snapshot=price.sample_fee,
            plate_fee_snapshot=price.plate_fee,
            die_fee_snapshot=price.die_fee,
            price_evidence_reference_snapshot=price.evidence_reference,
        )
    )
    db.flush()
    return order


def replay_external_stock_replenishment_purchase(
    db: Session,
    *,
    policy: InventoryStockPolicy,
    finished_quantity: int,
    purchase_quantity: Any | None,
    idempotency_key: str,
) -> StockReplenishmentOrder | None:
    """Replay from frozen purchase facts without consulting mutable master data."""

    existing_batch = db.scalar(
        select(ExternalPackagingPurchaseBatch).where(
            ExternalPackagingPurchaseBatch.idempotency_key == idempotency_key
        )
    )
    if existing_batch is None:
        return None
    if existing_batch.stock_replenishment_order_id is None:
        raise ExternalPurchaseContractError(
            "同一提交标识已用于其他外购业务，请关闭后重新操作"
        )
    existing_order = db.get(
        StockReplenishmentOrder,
        existing_batch.stock_replenishment_order_id,
    )
    if existing_order is None:
        raise ExternalPurchaseContractError("外购备库来源记录不完整")
    stock_items = list(
        db.scalars(
            select(StockReplenishmentOrderItem)
            .where(
                StockReplenishmentOrderItem.replenishment_order_id
                == existing_order.id
            )
            .order_by(StockReplenishmentOrderItem.id)
            .limit(2)
        ).all()
    )
    if len(stock_items) != 1:
        raise ExternalPurchaseContractError("外购备库来源明细不完整")
    stock_item = stock_items[0]
    purchase_items = list(
        db.scalars(
            select(ExternalPackagingPurchaseItem)
            .where(
                ExternalPackagingPurchaseItem.stock_replenishment_item_id
                == stock_item.id
            )
            .order_by(ExternalPackagingPurchaseItem.id)
            .limit(2)
        ).all()
    )
    if len(purchase_items) != 1:
        raise ExternalPurchaseContractError("外购采购冻结明细不完整")
    purchase_item = purchase_items[0]
    content_changed = (
        stock_item.stock_policy_id != policy.id
        or stock_item.product_id != policy.product_id
        or purchase_item.customer_product_id_snapshot != policy.product_id
    )
    if purchase_quantity is None:
        content_changed = content_changed or (
            int(stock_item.quantity) != int(finished_quantity)
        )
    else:
        requested_purchase_quantity = _positive_decimal(
            purchase_quantity,
            label="供应商采购数量",
        ).quantize(SIX_PLACES)
        frozen_purchase_quantity = Decimal(
            purchase_item.purchase_quantity
        ).quantize(SIX_PLACES)
        content_changed = content_changed or (
            requested_purchase_quantity != frozen_purchase_quantity
        )
    if content_changed:
        raise ExternalPurchaseContractError(
            "同一提交标识对应的外购备库内容已变化，请刷新后重新操作"
        )
    return existing_order


def external_purchase_batch_for_replenishment(
    db: Session, replenishment_order_id: int
) -> ExternalPackagingPurchaseBatch | None:
    return db.scalar(
        select(ExternalPackagingPurchaseBatch)
        .options(
            selectinload(
                ExternalPackagingPurchaseBatch.purchase_orders
            ).selectinload(ExternalPackagingPurchaseOrder.items)
        )
        .where(
            ExternalPackagingPurchaseBatch.stock_replenishment_order_id
            == replenishment_order_id
        )
        .order_by(ExternalPackagingPurchaseBatch.id.desc())
        .limit(1)
    )


def external_stock_purchase_payload(
    db: Session, order: StockReplenishmentOrder
) -> dict[str, Any] | None:
    batch = external_purchase_batch_for_replenishment(db, int(order.id))
    if batch is None:
        return None
    serialized = serialize_external_purchase_batch(batch)
    return {
        "procurement_mode": "external_purchase",
        "external_purchase_batch_id": int(batch.id),
        "external_purchase_orders": serialized["purchase_orders"],
    }
