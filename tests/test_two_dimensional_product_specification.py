from decimal import Decimal
from pathlib import Path

from app.services.product_specification import (
    dimension_specification,
    resolved_product_specification,
)


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def test_structured_dimensions_support_two_or_three_dimensions() -> None:
    assert dimension_specification(Decimal("778"), Decimal("1137"), None) == "778×1137mm"
    assert dimension_specification(Decimal("520"), Decimal("350"), Decimal("300")) == "520×350×300mm"
    assert dimension_specification(Decimal("430.50"), Decimal("68.00"), None) == "430.5×68mm"
    assert dimension_specification(None, Decimal("68"), None) is None
    assert dimension_specification(Decimal("430"), None, None) is None


def test_only_missing_placeholders_fall_back_to_structured_dimensions() -> None:
    for placeholder in (None, "", "-", "—", "未登记", "规格未登记"):
        assert resolved_product_specification(
            placeholder,
            length_mm=778,
            width_mm=1137,
        ) == "778×1137mm"
    assert resolved_product_specification(
        "客户冻结规格 26×45",
        length_mm=778,
        width_mm=1137,
    ) == "客户冻结规格 26×45"


def test_meaningful_order_snapshot_precedes_current_product_dimensions() -> None:
    assert resolved_product_specification(
        "-",
        fallback_snapshots=("订单冻结规格",),
        length_mm=778,
        width_mm=1137,
    ) == "订单冻结规格"


def test_common_box_frontend_does_not_require_height_for_specification() -> None:
    start = INDEX.index("          spec(row) {")
    end = INDEX.index("          statusTone(value) {", start)
    body = INDEX[start:end]
    assert "[row.length_mm,row.width_mm].every" in body
    assert "[row.length_mm,row.width_mm,row.height_mm].every" not in body
    assert 'dimensions.map(fmt).join("×")' in body
    assert "Number(row.height_mm) > 0" in body
