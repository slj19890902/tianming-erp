from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_HALF_UP
import json
from typing import Any, Mapping

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from app.models.external_packaging_price import ExternalPackagingPriceVersion
from app.models.order import Order, OrderItem
from app.models.order_external_packaging import SalesOrderItemExternalComponent
from app.models.supplier import ExternalPackagingProduct, Supplier


CATEGORY_CODE = "paper_corner_guard"
ROOT_UNITS = frozenset({"根", "支"})
METER_UNIT = "米"
SIX_PLACES = Decimal("0.000001")
UNIT_COST_PLACES = Decimal("0.000001")
MONEY_PLACES = Decimal("0.01")


class CornerGuardPricingError(ValueError):
    pass


def _positive_decimal(raw: object, label: str) -> Decimal:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise CornerGuardPricingError(f"{label}必须是有效毫米数") from error
    if not value.is_finite() or value <= 0:
        raise CornerGuardPricingError(f"{label}必须大于0")
    if value > Decimal("999999"):
        raise CornerGuardPricingError(f"{label}超出允许范围")
    return value.quantize(SIX_PLACES).normalize()


def _json_number(value: Decimal) -> int | float:
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def _display_decimal(value: Decimal) -> str:
    if value == value.to_integral_value():
        return str(int(value))
    return format(value.normalize(), "f")


def normalize_customer_corner_guard_specification(
    raw: Mapping[str, object] | None,
) -> tuple[dict[str, object], str]:
    source = dict(raw or {})
    shape = str(source.get("shape") or "L").strip().upper()
    if shape not in {"L", "U", "其他"}:
        raise CornerGuardPricingError("纸护角形状请选择L、U或其他")
    length = _positive_decimal(source.get("length_mm"), "纸护角单根长度")
    side_a = _positive_decimal(source.get("side_a_mm"), "纸护角边宽A")
    side_b = _positive_decimal(source.get("side_b_mm"), "纸护角边宽B")
    thickness = _positive_decimal(source.get("thickness_mm"), "纸护角厚度")
    specification = {
        "shape": shape,
        "length_mm": _json_number(length),
        "side_a_mm": _json_number(side_a),
        "side_b_mm": _json_number(side_b),
        "thickness_mm": _json_number(thickness),
    }
    summary = (
        f"{_display_decimal(length)}×"
        f"{_display_decimal(side_a)}×"
        f"{_display_decimal(side_b)}×"
        f"{_display_decimal(thickness)}mm"
    )
    return specification, summary


def corner_guard_sections_match(
    customer_specification: Mapping[str, object],
    supplier_specification: Mapping[str, object],
) -> bool:
    try:
        customer, _ = normalize_customer_corner_guard_specification(
            customer_specification
        )
        supplier = dict(supplier_specification or {})
        supplier_shape = str(supplier.get("shape") or "").strip().upper()
        if supplier_shape != str(customer["shape"]):
            return False
        for field in ("side_a_mm", "side_b_mm", "thickness_mm"):
            if _positive_decimal(supplier.get(field), field) != _positive_decimal(
                customer.get(field), field
            ):
                return False
        return True
    except CornerGuardPricingError:
        return False


def current_corner_guard_meter_price(
    db: Session,
    *,
    external_product_id: int,
    product_version: int,
    as_of: date,
) -> ExternalPackagingPriceVersion | None:
    return db.scalar(
        select(ExternalPackagingPriceVersion)
        .where(
            ExternalPackagingPriceVersion.external_product_id
            == external_product_id,
            ExternalPackagingPriceVersion.product_version == product_version,
            ExternalPackagingPriceVersion.quote_unit == METER_UNIT,
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


def _tier_prices(price: ExternalPackagingPriceVersion) -> list[tuple[Decimal, Decimal]]:
    try:
        raw = json.loads(price.tier_prices_json or "[]")
    except (TypeError, json.JSONDecodeError):
        raw = []
    result: list[tuple[Decimal, Decimal]] = []
    for row in raw if isinstance(raw, list) else []:
        try:
            minimum = Decimal(str(row.get("min_quantity")))
            unit_price = Decimal(str(row.get("unit_price")))
        except (InvalidOperation, TypeError, ValueError):
            continue
        if minimum > 0 and unit_price > 0:
            result.append((minimum, unit_price))
    return sorted(result)


def calculate_corner_guard_cost(
    *,
    customer_specification: Mapping[str, object],
    root_quantity: Decimal,
    price: ExternalPackagingPriceVersion,
) -> dict[str, Any]:
    specification, summary = normalize_customer_corner_guard_specification(
        customer_specification
    )
    if price.quote_unit != METER_UNIT:
        raise CornerGuardPricingError("纸护角成本只接受供应商正式每米报价")
    quantity = Decimal(root_quantity)
    if not quantity.is_finite() or quantity <= 0:
        raise CornerGuardPricingError("纸护角根数必须大于0")
    length_m = (Decimal(str(specification["length_mm"])) / Decimal("1000"))
    meter_quantity = (quantity * length_m).quantize(
        SIX_PLACES, rounding=ROUND_HALF_UP
    )
    effective_meter_price = Decimal(price.unit_price)
    for minimum, tier_price in _tier_prices(price):
        if meter_quantity >= minimum:
            effective_meter_price = tier_price
        else:
            break
    unit_cost = (length_m * effective_meter_price).quantize(
        UNIT_COST_PLACES, rounding=ROUND_HALF_UP
    )
    total_cost = (quantity * unit_cost).quantize(
        MONEY_PLACES, rounding=ROUND_HALF_UP
    )
    return {
        "specification": specification,
        "specification_summary": summary,
        "root_quantity": quantity.quantize(SIX_PLACES),
        "length_m_per_root": length_m.quantize(SIX_PLACES),
        "pricing_quantity_m": meter_quantity,
        "effective_meter_price": effective_meter_price.quantize(
            UNIT_COST_PLACES, rounding=ROUND_HALF_UP
        ),
        "unit_cost_per_root": unit_cost,
        "total_cost": total_cost,
    }


def resolve_corner_guard_cost(
    db: Session,
    *,
    external_product_id: int,
    external_product_version: int,
    customer_id: int,
    customer_specification: Mapping[str, object],
    root_quantity: Decimal,
    as_of: date,
) -> tuple[ExternalPackagingProduct, ExternalPackagingPriceVersion, dict[str, Any]]:
    product = db.scalar(
        select(ExternalPackagingProduct)
        .options(
            selectinload(ExternalPackagingProduct.supplier).selectinload(
                Supplier.supply_categories
            )
        )
        .where(ExternalPackagingProduct.id == external_product_id)
    )
    if product is None:
        raise CornerGuardPricingError("冻结的纸护角供应商产品已不存在")
    if product.category_code != CATEGORY_CODE:
        raise CornerGuardPricingError("冻结候选不是纸护角产品")
    if product.version != external_product_version:
        raise CornerGuardPricingError("纸护角供应商产品版本已变化，请人工核对")
    if product.customer_scope_id not in (None, customer_id):
        raise CornerGuardPricingError("纸护角候选属于其他客户")
    if not product.is_active or product.supplier is None or not product.supplier.is_active:
        raise CornerGuardPricingError("纸护角供应商或产品已停用")
    if not any(
        row.is_active and row.category_code == CATEGORY_CODE
        for row in product.supplier.supply_categories
    ):
        raise CornerGuardPricingError("供应商未启用纸护角供货类别")
    try:
        supplier_specification = json.loads(product.specification_json or "{}")
    except (TypeError, json.JSONDecodeError) as error:
        raise CornerGuardPricingError("纸护角供应商截面规格无效") from error
    if not corner_guard_sections_match(
        customer_specification, supplier_specification
    ):
        raise CornerGuardPricingError("客户护角截面与供应商候选不兼容")
    price = current_corner_guard_meter_price(
        db,
        external_product_id=product.id,
        product_version=external_product_version,
        as_of=as_of,
    )
    if price is None:
        raise CornerGuardPricingError("当前没有有效的纸护角每米正式报价")
    return product, price, calculate_corner_guard_cost(
        customer_specification=customer_specification,
        root_quantity=root_quantity,
        price=price,
    )


def estimate_order_item_external_packaging_cost(
    db: Session,
    item: OrderItem,
    *,
    as_of: date,
) -> dict[str, Any]:
    components = list(
        db.scalars(
            select(SalesOrderItemExternalComponent)
            .options(selectinload(SalesOrderItemExternalComponent.candidates))
            .where(
                SalesOrderItemExternalComponent.sales_order_item_id == item.id
            )
            .order_by(SalesOrderItemExternalComponent.display_order)
        ).all()
    )
    if not components:
        return {"components": [], "missing_items": [], "known_total": Decimal("0")}
    customer_id = db.scalar(
        select(Order.customer_id).where(Order.id == item.order_id)
    )
    if customer_id is None:
        return {
            "components": [],
            "missing_items": ["订单客户快照缺失"],
            "known_total": Decimal("0"),
        }
    result: list[dict[str, Any]] = []
    missing: list[str] = []
    known_total = Decimal("0")
    for component in components:
        label = f"外购组件：{component.purpose}"
        if component.category_code != CATEGORY_CODE:
            missing.append(f"{label}成本换算规则待完善")
            continue
        default_candidate = next(
            (candidate for candidate in component.candidates if candidate.is_default),
            None,
        )
        if default_candidate is None:
            missing.append(f"{label}缺少冻结默认供应商候选")
            continue
        if default_candidate.purchase_unit_snapshot not in ROOT_UNITS:
            missing.append(f"{label}不是按根/支采购，需人工完善换算")
            continue
        try:
            specification = json.loads(component.specification_json or "{}")
        except (TypeError, json.JSONDecodeError):
            missing.append(f"{label}客户定长规格无效")
            continue
        required_roots = (
            Decimal(int(item.quantity or 0))
            * Decimal(component.quantity_per_finished_unit)
            * (Decimal("1") + Decimal(component.waste_rate))
        ).to_integral_value(rounding=ROUND_CEILING)
        try:
            product, price, cost = resolve_corner_guard_cost(
                db,
                external_product_id=(
                    default_candidate.external_product_id_snapshot
                ),
                external_product_version=(
                    default_candidate.external_product_version_snapshot
                ),
                customer_id=int(customer_id),
                customer_specification=specification,
                root_quantity=required_roots,
                as_of=as_of,
            )
        except CornerGuardPricingError as error:
            missing.append(f"{label}{error}")
            continue
        total = Decimal(cost["total_cost"])
        known_total += total
        result.append(
            {
                "source_type": "external_corner_guard",
                "label": label,
                "category_code": CATEGORY_CODE,
                "specification_summary": cost["specification_summary"],
                "required_piece_quantity": int(required_roots),
                "purchase_unit": "根",
                "length_m_per_root": str(cost["length_m_per_root"]),
                "pricing_quantity_m": str(cost["pricing_quantity_m"]),
                "supplier_id": product.supplier_id,
                "supplier_name": (
                    product.supplier.display_name
                    or product.supplier.standard_name
                ),
                "external_product_id": product.id,
                "external_product_version": product.version,
                "price_version_id": price.id,
                "price_version_number": price.version_number,
                "quote_unit": price.quote_unit,
                "effective_meter_price": str(cost["effective_meter_price"]),
                "estimated_unit_cost_per_root": str(
                    cost["unit_cost_per_root"]
                ),
                "estimated_material_cost": str(total),
                "cost_scope_label": "下单时供应商正式每米报价换算，预计而非实际成本",
            }
        )
    return {
        "components": result,
        "missing_items": list(dict.fromkeys(missing)),
        "known_total": known_total.quantize(
            MONEY_PLACES, rounding=ROUND_HALF_UP
        ),
    }
