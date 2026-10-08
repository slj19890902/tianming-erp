from decimal import Decimal

import pytest

from app.services.sheet_cutting_contract import (
    SheetCuttingContract,
    SheetCuttingContractError,
)


def contract(**overrides):
    values = dict(theoretical_length_mm="340", theoretical_width_mm="200")
    values.update(overrides)
    return SheetCuttingContract(**values)


def test_new_sheet_defaults_to_one_by_one_and_one_piece():
    sheet = contract()
    assert (sheet.length_parts, sheet.width_parts, sheet.mold_count) == (1, 1, 1)
    assert sheet.cutting_mode == "一开一"
    assert sheet.supplier_size_mm == (Decimal("340"), Decimal("200"))
    assert sheet.purchase_quantity(required_piece_qty=400).supplier_sheet_qty == 400


def test_two_molds_and_two_by_two_order_fifty_large_sheets():
    sheet = contract(length_parts=2, width_parts=2, is_die_cut=True, mold_count=2)
    result = sheet.purchase_quantity(required_piece_qty=400)
    assert sheet.supplier_size_mm == (Decimal("680"), Decimal("400"))
    assert sheet.cutting_mode == "一开四"
    assert sheet.yield_per_supplier_sheet == 8
    assert (result.supplier_sheet_qty, result.theoretical_sheet_qty) == (50, 200)
    assert (result.produced_piece_qty, result.surplus_piece_qty) == (400, 0)


def test_four_molds_and_one_cut_in_two_yield_eight_products():
    sheet = contract(length_parts=2, is_die_cut=True, mold_count=4)
    assert sheet.yield_per_supplier_sheet == 8
    assert sheet.purchase_quantity(required_piece_qty=400).supplier_sheet_qty == 50


def test_both_axes_preserve_card_stock_decimal_precision():
    sheet = contract(theoretical_length_mm="298.5", theoretical_width_mm="444.5",
                     length_parts=3, width_parts=2)
    assert sheet.supplier_size_mm == (Decimal("895.5"), Decimal("889.0"))
    assert sheet.cutting_mode == "一开六"


def test_direction_is_not_lost_when_number_of_cuts_matches():
    along_length = contract(length_parts=2)
    along_width = contract(width_parts=2)
    assert along_length.cutting_mode == along_width.cutting_mode
    assert along_length.supplier_size_mm != along_width.supplier_size_mm
    assert along_length.to_snapshot() != along_width.to_snapshot()


def test_double_piece_box_is_expanded_before_purchase_calculation():
    sheet = contract()
    # 141 finished boxes, two physical pieces per box. The contract receives
    # pieces, so it cannot multiply the assembly factor a second time.
    result = sheet.purchase_quantity(required_piece_qty=141 * 2)
    assert result.supplier_sheet_qty == 282


def test_deduct_physical_pieces_before_rounding_supplier_sheets():
    result = contract(length_parts=2, is_die_cut=True, mold_count=4).purchase_quantity(
        required_piece_qty=403, inventory_deducted_piece_qty=10)
    assert result.net_piece_qty == 393
    assert result.supplier_sheet_qty == 50
    assert result.produced_piece_qty == 400
    assert result.surplus_piece_qty == 7
    assert result.produced_piece_qty + 10 == 403 + result.surplus_piece_qty


def test_fully_covered_requirement_does_not_order_or_create_surplus():
    result = contract(length_parts=2).purchase_quantity(
        required_piece_qty=10, inventory_deducted_piece_qty=10)
    assert (result.supplier_sheet_qty, result.produced_piece_qty,
            result.surplus_piece_qty) == (0, 0, 0)


@pytest.mark.parametrize("field,value", [
    ("length_parts", 0), ("length_parts", -1), ("width_parts", True),
    ("width_parts", 1.5), ("mold_count", "2"), ("mold_count", 0),
    ("is_die_cut", 1), ("theoretical_length_mm", "NaN"),
    ("theoretical_width_mm", "Infinity"), ("theoretical_width_mm", "0"),
    ("theoretical_length_mm", "340.001"), ("theoretical_length_mm", True),
])
def test_invalid_dimensions_or_counts_are_explicitly_rejected(field, value):
    with pytest.raises(SheetCuttingContractError):
        contract(**{field: value})


def test_non_die_cut_material_cannot_inherit_a_mold_count():
    with pytest.raises(SheetCuttingContractError, match="非模切"):
        contract(mold_count=2)


def test_over_deduction_is_rejected_instead_of_silently_clamped():
    with pytest.raises(SheetCuttingContractError, match="抵扣"):
        contract().purchase_quantity(required_piece_qty=10, inventory_deducted_piece_qty=11)


def test_snapshot_round_trip_freezes_all_factors():
    sheet = contract(length_parts=2, width_parts=3, is_die_cut=True, mold_count=4)
    snapshot = sheet.to_snapshot()
    assert snapshot["schema_version"] == 2
    assert SheetCuttingContract.from_snapshot(snapshot) == sheet


def test_old_snapshot_is_never_silently_reinterpreted_as_new_contract():
    with pytest.raises(SheetCuttingContractError, match="版本"):
        SheetCuttingContract.from_snapshot({"schema_version": 1, "cutting_mode": "一开二"})


def test_inconsistent_derived_supplier_size_in_snapshot_is_rejected():
    snapshot = contract(length_parts=2).to_snapshot()
    snapshot["supplier_length_mm"] = "340"
    with pytest.raises(SheetCuttingContractError, match="不一致"):
        SheetCuttingContract.from_snapshot(snapshot)


def test_missing_or_extra_snapshot_fields_are_rejected():
    snapshot = contract().to_snapshot()
    del snapshot["mold_count"]
    with pytest.raises(SheetCuttingContractError):
        SheetCuttingContract.from_snapshot(snapshot)
    snapshot = contract().to_snapshot()
    snapshot["future_semantics"] = True
    with pytest.raises(SheetCuttingContractError):
        SheetCuttingContract.from_snapshot(snapshot)


def test_large_count_does_not_use_float_rounding():
    required = 2**53 - 2
    result = contract(length_parts=3).purchase_quantity(required_piece_qty=required)
    assert result.supplier_sheet_qty == (required + 2) // 3


@pytest.mark.parametrize("axis", ["length_parts", "width_parts"])
def test_supplier_dimension_overflow_is_rejected(axis):
    with pytest.raises(SheetCuttingContractError):
        contract(**{axis: 10**10})
