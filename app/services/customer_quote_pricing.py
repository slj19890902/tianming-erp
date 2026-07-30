from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from app.services.box_type_rules import box_type_code
from app.services.material_pricing import get_effective_material_price


SQUARE_MM_PER_SQUARE_METRE = Decimal("1000000")
TWO_PLACES = Decimal("0.01")
DEFAULT_MATERIAL_TO_CUSTOMER_FACTOR = Decimal("1.30")


class CustomerQuotePricingError(ValueError):
    pass


def canonical_quote_box_type(box_type: str | None) -> str:
    """Use one stable key for the A1/0201 aliases used by customer preferences."""
    if box_type_code(box_type) == "a1_0201":
        return "A1"
    return str(box_type or "").strip().upper()


def a1_area_m2(*, length_mm: Decimal, width_mm: Decimal, height_mm: Decimal) -> Decimal:
    """Return the A1 customer-quotation area, using the confirmed +80/+40 rule."""
    if min(length_mm, width_mm, height_mm) <= 0:
        raise CustomerQuotePricingError("长、宽、高必须大于0")
    return (
        (length_mm + width_mm + Decimal("80"))
        * (width_mm + height_mm + Decimal("40"))
        * Decimal("2")
        / SQUARE_MM_PER_SQUARE_METRE
    )


def resolve_customer_square_price(
    db,
    *,
    material,
    flute_type: str,
    saved_square_price: Decimal | None,
) -> dict:
    """Prefer an active saved customer price, otherwise suggest effective material x 1.30."""
    effective = get_effective_material_price(
        db, material=material, flute_type=flute_type
    )
    material_square_price = effective.get("effective_price")
    if saved_square_price is not None:
        return {
            "customer_square_price": Decimal(saved_square_price),
            "price_source": "customer_preference",
            "material_effective_square_price": (
                None
                if material_square_price is None
                else Decimal(str(material_square_price))
            ),
        }
    if material_square_price is None:
        raise CustomerQuotePricingError("所选材质缺少有效平方价，无法生成默认建议")
    return {
        "customer_square_price": (
            Decimal(str(material_square_price)) * DEFAULT_MATERIAL_TO_CUSTOMER_FACTOR
        ).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP),
        "price_source": "material_effective_price_x_1_30",
        "material_effective_square_price": Decimal(str(material_square_price)),
    }


def estimate_a1_unit_price(
    *,
    length_mm: Decimal,
    width_mm: Decimal,
    height_mm: Decimal,
    customer_square_price: Decimal,
    manual_unit_price: Decimal | None = None,
) -> dict:
    """Return an estimate; an explicit caller/manual price is always final."""
    if customer_square_price <= 0:
        raise CustomerQuotePricingError("客户平方报价必须大于0")
    area = a1_area_m2(
        length_mm=length_mm, width_mm=width_mm, height_mm=height_mm
    )
    estimated = (area * customer_square_price).quantize(
        TWO_PLACES, rounding=ROUND_HALF_UP
    )
    final = (
        Decimal(manual_unit_price).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
        if manual_unit_price is not None
        else estimated
    )
    return {
        "area_m2": area,
        "estimated_unit_price": estimated,
        "final_unit_price": final,
        "final_price_source": "manual_unit_price" if manual_unit_price is not None else "estimated",
    }
