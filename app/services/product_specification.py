from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from app.models.product import Product


_MISSING_SPECIFICATIONS = {
    "",
    "-",
    "—",
    "－",
    "未登记",
    "规格未登记",
}


def _positive_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number > 0 else None


def _compact_decimal(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def dimension_specification(
    length_mm: Any,
    width_mm: Any,
    height_mm: Any = None,
) -> str | None:
    """Format structured product dimensions for customer-facing documents."""

    length = _positive_decimal(length_mm)
    width = _positive_decimal(width_mm)
    if length is None or width is None:
        return None
    dimensions = [length, width]
    height = _positive_decimal(height_mm)
    if height is not None:
        dimensions.append(height)
    return "×".join(_compact_decimal(value) for value in dimensions) + "mm"


def product_dimension_specification(product: Product | None) -> str | None:
    if product is None:
        return None
    return dimension_specification(
        product.length_mm,
        product.width_mm,
        product.height_mm,
    )


def resolved_product_specification(
    snapshot: Any,
    product: Product | None = None,
    *,
    fallback_snapshots: tuple[Any, ...] = (),
    length_mm: Any = None,
    width_mm: Any = None,
    height_mm: Any = None,
) -> str | None:
    """Keep a meaningful frozen value; repair only missing placeholders."""

    for candidate in (snapshot, *fallback_snapshots):
        text = str(candidate or "").strip()
        if text not in _MISSING_SPECIFICATIONS:
            return text
    if product is not None:
        return product_dimension_specification(product)
    return dimension_specification(length_mm, width_mm, height_mm)
