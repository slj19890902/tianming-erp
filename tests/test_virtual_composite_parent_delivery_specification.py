from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path

from app.api.products import (
    ProductPayload,
    _VIRTUAL_COMPOSITE_PARENT_SYNC_BLOCKED_FIELDS,
)
from app.services.product_specification import resolved_product_specification


def test_virtual_composite_parent_keeps_box_type_and_finished_dimensions_for_delivery() -> None:
    payload = ProductPayload(
        customer_id=1,
        product_code="KIT-001",
        customer_material_code="KIT-001",
        product_name="组合父件",
        box_category="normal",
        box_style="A1",
        length_mm=750,
        width_mm=450,
        height_mm=180,
        is_virtual_composite_parent=True,
        combination_mode="parent_priced_set",
        composite_fulfillment_mode="parent_delivery",
    )

    assert payload.is_virtual_composite_parent is True
    assert payload.box_style == "A1"
    assert (payload.length_mm, payload.width_mm, payload.height_mm) == (750, 450, 180)
    assert payload.material_id is None
    assert payload.report_length_mm is None
    assert payload.report_width_mm is None
    assert resolved_product_specification(
        None,
        SimpleNamespace(
            length_mm=payload.length_mm,
            width_mm=payload.width_mm,
            height_mm=payload.height_mm,
        ),
    ) == "750×450×180mm"


def test_order_sync_cannot_overwrite_virtual_parent_delivery_specification() -> None:
    assert {"box_style", "length_mm", "width_mm", "height_mm"}.issubset(
        _VIRTUAL_COMPOSITE_PARENT_SYNC_BLOCKED_FIELDS
    )


def test_common_box_explains_the_parent_dimensions_are_delivery_only() -> None:
    source = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    # The compact editor keeps this distinction in a tooltip, not an extra
    # explanatory row. Verify both halves of the business meaning.
    assert 'title="父件只表示整套数量和价格，不报料、不生产；尺寸用于送货规格"' in source
