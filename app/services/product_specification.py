from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re
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
_DIMENSION_SPECIFICATION = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*[xX×*]\s*(\d+(?:\.\d+)?)"
    r"(?:\s*[xX×*]\s*(\d+(?:\.\d+)?))?\s*(?:mm)?\s*$",
    re.IGNORECASE,
)
_EMBEDDED_DIMENSION_SPECIFICATION = re.compile(
    r"(?<!\d)(\d+(?:\.\d+)?)\s*[xX×*]\s*(\d+(?:\.\d+)?)"
    r"(?:\s*[xX×*]\s*(\d+(?:\.\d+)?))?\s*(?:mm)?(?!\d)",
    re.IGNORECASE,
)


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


def product_stock_specification(product: Product | None) -> str | None:
    """Keep die identity without pretending a multi-up tool is one piece's size.

    A registered die and explicit mold count identify an existing die-cut SKU.
    This descriptive specification never supplies dimensions or changes units,
    quantities, supplier cutting factors, or material-cost evidence.
    """
    dimensions = product_dimension_specification(product)
    if dimensions or product is None:
        return dimensions
    from app.services.sheet_cutting_settings import component_settings
    from app.services.sheet_cutting_contract import SheetCuttingContractError
    try:
        setting = component_settings(getattr(product, "sheet_cutting_settings", None))
    except SheetCuttingContractError:
        return None
    if setting is None or not setting.is_die_cut:
        return None
    mold = getattr(product, "mold_tool", None)
    code = str(getattr(product, "product_code", "") or "").strip()
    mold_code = str(getattr(mold, "mold_code", "") or "").strip()
    if (not code or not mold_code or mold is None
            or not getattr(mold, "is_active", False)
            or getattr(mold, "deleted_at", None) is not None
            or getattr(mold, "archive_status", "active") != "active"
            or getattr(product, "mold_tool_id", None) != mold.id):
        return None
    return f"模切成品 {code}；模具 {mold_code}；{setting.mold_count}模"


def strip_unregistered_size_note(value: str) -> str:
    return re.sub(r"[（(]\s*单片尺寸未登记\s*[）)]|单片尺寸未登记", "", value)


def normalized_specification_text(value: Any) -> str | None:
    """Normalize a pure 2D/3D size while preserving non-dimensional text."""

    text = strip_unregistered_size_note(str(value or "")).strip()
    if text in _MISSING_SPECIFICATIONS:
        return None
    match = _DIMENSION_SPECIFICATION.fullmatch(text)
    if match is None:
        return text
    dimensions = [Decimal(part) for part in match.groups() if part is not None]
    return "×".join(_compact_decimal(part) for part in dimensions) + "mm"


def embedded_dimension_specification(value: Any) -> str | None:
    """Read an explicitly written 2D/3D size from descriptive source text."""

    match = _EMBEDDED_DIMENSION_SPECIFICATION.search(str(value or ""))
    if match is None:
        return None
    dimensions = [Decimal(part) for part in match.groups() if part is not None]
    return "×".join(_compact_decimal(part) for part in dimensions) + "mm"


def resolved_product_specification(
    snapshot: Any,
    product: Product | None = None,
    *,
    fallback_snapshots: tuple[Any, ...] = (),
    customer_name_snapshots: tuple[Any, ...] = (),
    length_mm: Any = None,
    width_mm: Any = None,
    height_mm: Any = None,
) -> str | None:
    """Keep a meaningful frozen value; repair only missing placeholders."""

    for candidate in (snapshot, *fallback_snapshots):
        normalized = normalized_specification_text(candidate)
        if normalized is not None:
            return normalized
    # Customer-facing frozen names may carry the finished size when the dedicated
    # spec field is blank. Never use supplier external-packaging dimensions here.
    for name in customer_name_snapshots:
        embedded = embedded_dimension_specification(name)
        if embedded is not None:
            return embedded
    if product is not None:
        return product_stock_specification(product)
    return dimension_specification(length_mm, width_mm, height_mm)


def delivery_specification(product):
    """Only the common-box product specification belongs on a delivery note.

    Tool locations, mold counts, report sizes and names are not finished sizes.
    Keep the separate warehouse/production specification resolver unchanged.
    """
    if product is None:
        return ''
    if getattr(product, 'supply_mode', None) == 'external_purchase':
        text = str(getattr(product, 'external_packaging_specification_summary', None) or '').strip()
        if text in _MISSING_SPECIFICATIONS or any(note in text for note in ('待完善', '待补', '未登记')):
            return ''
        return text
    return product_dimension_specification(product) or ''
