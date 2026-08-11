from __future__ import annotations

import pytest

from app.api.products import ProductPayload
from app.api.requisition import (
    PendingSupplierOrderDraftItem,
    RequisitionLinePayload,
)
from app.services.box_type_rules import BoxTypeRuleError, normalize_box_configuration
from app.services.requisition_quantities import (
    CuttingModeError,
    cutting_factor,
    cutting_mode_input_value,
    normalize_cutting_mode,
    purchase_sheet_quantity,
)
from app.services.production_workflow import production_output_quantity


@pytest.mark.parametrize(
    ("raw", "normalized", "factor"),
    [
        ("一开一", "一开一", 1),
        ("1", "一开一", 1),
        (2, "一开二", 2),
        ("一开六", "一开六", 6),
        ("12", "一开12", 12),
        (12, "一开12", 12),
        ("一开12", "一开12", 12),
    ],
)
def test_positive_integer_cutting_mode_normalizes_without_a_fixed_upper_list(
    raw: object,
    normalized: str,
    factor: int,
) -> None:
    assert normalize_cutting_mode(raw, strict=True) == normalized
    assert cutting_factor(raw) == factor
    assert cutting_mode_input_value(normalized) == factor


@pytest.mark.parametrize("raw", ["", 0, "0", -1, "-1", "1.5", "一开0", "一开01", "十二"])
def test_positive_integer_cutting_mode_rejects_invalid_values(raw: object) -> None:
    with pytest.raises(CuttingModeError, match="大于0的整数"):
        normalize_cutting_mode(raw, strict=True)


def test_one_to_twelve_purchase_and_double_splice_output_stay_independent() -> None:
    assert purchase_sheet_quantity(25, 0, "一开12") == 3
    assert production_output_quantity(3, 12, 1) == 36
    assert production_output_quantity(3, 12, 2) == 18


def test_product_and_requisition_payloads_accept_numeric_one_to_twelve() -> None:
    product = ProductPayload(
        customer_id=1,
        product_code="P1-41-12",
        customer_material_code="P1-41-12",
        product_name="一开十二测试内衬",
        box_category="normal",
        box_style="衬板",
        default_cutting_mode=12,
    )
    assert product.default_cutting_mode == "一开12"

    requisition = RequisitionLinePayload(
        order_item_id=1,
        requisition_qty=3,
        cardboard_len="600",
        cardboard_width="400",
        special_process="12",
    )
    assert requisition.special_process == "一开12"

    draft = PendingSupplierOrderDraftItem(
        source_type="order_item",
        order_item_id=1,
        cutting_mode="一开12",
        requisition_qty=3,
        report_length_mm="600",
        report_width_mm="400",
    )
    assert draft.cutting_mode == "一开12"


def test_box_configuration_accepts_one_to_twelve_and_rejects_zero() -> None:
    configuration = normalize_box_configuration(
        box_style="衬板",
        splice_mode="single",
        pieces_per_box=1,
        flap_mm=None,
        default_cutting_mode="12",
    )
    assert configuration["default_cutting_mode"] == "一开12"

    with pytest.raises(BoxTypeRuleError, match="大于0的整数"):
        normalize_box_configuration(
            box_style="衬板",
            splice_mode="single",
            pieces_per_box=1,
            flap_mm=None,
            default_cutting_mode="0",
        )
