"""Read-only product inputs for editable drawing candidates.

These nominal A1 dimensions are starting values, not manufacturing allowances
or procurement rules. Callers must not overwrite saved drawing adjustments.
"""
from __future__ import annotations

from decimal import Decimal

from app.services.box_type_rules import get_box_type_rule
from app.services.drawing_geometry import DrawingGeometryError, number, plain


class DrawingProductRuleError(DrawingGeometryError):
    pass


def validate_single_piece_slotted(product: object) -> None:
    """Check product compatibility before offering or publishing slotted_v1.

    No fallback to a one-piece box is safe when splice or piece count is absent.
    The existing four-panel template does not represent either half of a
    two-piece carton.
    """
    rule = get_box_type_rule(getattr(product, "box_style", None))
    if rule is None or rule.code != "a1_0201":
        raise DrawingProductRuleError("开槽箱模板仅适用于已识别的A1/0201箱型")
    if getattr(product, "splice_mode", None) != "single":
        raise DrawingProductRuleError("当前开槽箱模板仅支持明确为单片的产品，请核对拼接方式")
    pieces = getattr(product, "pieces_per_box", None)
    if isinstance(pieces, bool) or pieces != 1:
        raise DrawingProductRuleError("当前开槽箱模板要求每箱片数明确为1，不能将双片或未知片数按单片出图")


def _dimension(product: object, field: str, label: str) -> Decimal | None:
    value = getattr(product, field, None)
    return None if value is None else number(value, label)


def slotted_parameter_candidates(product: object) -> dict[str, str | None]:
    """Return slotted_v1 parameter keys, keeping missing evidence explicit.

    Reads product dimensions and its recorded joint width only. In particular,
    report/crease/machine values are neither read nor mutated. Slot width must
    be supplied separately; neither paper thickness nor an integer reporting
    rule establishes it. Width/2 preserves decimals such as 395/2 = 197.5.
    """
    validate_single_piece_slotted(product)
    length = _dimension(product, "length_mm", "产品长")
    width = _dimension(product, "width_mm", "产品宽")
    height = _dimension(product, "height_mm", "产品高")
    joint = _dimension(product, "flap_mm", "接头")

    def value(dimension: Decimal | None) -> str | None:
        return None if dimension is None else plain(dimension)

    cover = None if width is None else width / 2
    return {
        "panel_1_mm": value(length),
        "panel_2_mm": value(width),
        "panel_3_mm": value(length),
        "panel_4_mm": value(width),
        "body_height_mm": value(height),
        "top_flap_mm": value(cover),
        "bottom_flap_mm": value(cover),
        "glue_flap_mm": value(joint),
        "slot_width_mm": None,
    }
