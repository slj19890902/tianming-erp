from __future__ import annotations
from app.core.sheet_dimensions import SheetDimension


PRESSURE_CREASE_TYPE = "压线"


def crease_width_error(
    *,
    label: str,
    crease_type: str | None,
    report_width_mm: SheetDimension | None,
    left_mm: int | None,
    middle_mm: int | None,
    right_mm: int | None,
) -> str | None:
    """Validate one-piece report width while keeping legacy reads compatible."""
    if (crease_type or "").strip() != PRESSURE_CREASE_TYPE:
        return None

    segments = (left_mm, middle_mm, right_mm)
    if any(value is None for value in segments):
        return f"{label}必须完整填写三段尺寸"
    if left_mm < 0 or middle_mm <= 0 or right_mm < 0:
        return f"{label}尺寸必须是有效毫米数"
    if report_width_mm is None or report_width_mm <= 0:
        return f"{label}对应报料宽必须大于 0"

    total = left_mm + middle_mm + right_mm
    if total != report_width_mm:
        return (
            f"{label}三段合计 {total}mm 必须等于报料宽 "
            f"{report_width_mm}mm"
        )
    return None


def product_crease_width_error(product: object) -> str | None:
    """Return the first main/base crease error for a Product-like object."""
    errors = (
        crease_width_error(
            label="压线",
            crease_type=getattr(product, "crease_type", None),
            report_width_mm=getattr(product, "report_width_mm", None),
            left_mm=getattr(product, "crease_left_mm", None),
            middle_mm=getattr(product, "crease_middle_mm", None),
            right_mm=getattr(product, "crease_right_mm", None),
        ),
        crease_width_error(
            label="底压线",
            crease_type=getattr(product, "base_crease_type", None),
            report_width_mm=getattr(product, "base_report_width_mm", None),
            left_mm=getattr(product, "base_crease_left_mm", None),
            middle_mm=getattr(product, "base_crease_middle_mm", None),
            right_mm=getattr(product, "base_crease_right_mm", None),
        ),
    )
    return next((error for error in errors if error), None)
