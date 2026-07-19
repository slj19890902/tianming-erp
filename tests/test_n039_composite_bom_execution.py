from decimal import Decimal

import pytest

from app.services.composite_bom_execution import (
    COMPLETION_DESTINATION_DIRECT_KIT,
    COMPLETION_DESTINATION_STOCK,
    CompositeBOMExecutionError,
    build_delivery_consumption_plan,
    calculate_effective_component_demands,
    calculate_kit_availability,
    calculate_requisition,
    component_signature,
    format_missing_component_message,
    validate_completion_destination,
    validate_order_adjustment,
)


def _component(**overrides):
    component = {
        "component_product_id": 10,
        "component_name": "面纸",
        "specification": "1000x800",
        "quantity_per_set": Decimal("2"),
        "is_required": True,
        "is_die_cut": True,
        "mold_max_yield_per_sheet": 4,
        "spare_sheet_quantity": 2,
    }
    component.update(overrides)
    return component


def test_demands_keep_different_specifications_separate_and_apply_adjustments():
    small = _component(specification="1000x800")
    large = _component(specification="1200x900")
    result = calculate_effective_component_demands(
        Decimal("10"), [small, large], [{"delta_sets": Decimal("-2")}]
    )

    assert result["effective_sets"] == 8
    assert len(result["components"]) == 2
    assert {row["demand_quantity"] for row in result["components"]} == {16}
    assert component_signature(small) != component_signature(large)


def test_real_snapshot_and_adjustment_field_names_are_supported():
    component = {
        "component_product_id": 11,
        "snapshot_component_product_code": "INNER-01",
        "snapshot_component_product_name": "内衬",
        "snapshot_component_spec": "300x200",
        "snapshot_component_material": "K6K / B",
        "snapshot_mold_tool_id": 9,
        "quantity_per_set": 2,
        "is_required": True,
    }

    result = calculate_effective_component_demands(
        10,
        [component],
        [{"delta_order_set_quantity": -1}],
    )

    row = result["components"][0]
    assert result["effective_sets"] == 9
    assert row["demand_quantity"] == 18
    signature = dict(row["signature"])
    assert signature["product_code"] == "INNER-01"
    assert signature["specification"] == "300x200"


def test_matching_signatures_merge_per_set_usage_for_kit_capacity():
    component = _component(component_product_id=11, quantity_per_set=1)
    demands = calculate_effective_component_demands(3, [component, component])
    availability = calculate_kit_availability(demands, {11: 7})

    assert demands["components"][0]["quantity_per_set"] == 2
    assert demands["components"][0]["demand_quantity"] == 6
    assert availability["max_complete_sets"] == 3


def test_requisition_uses_actual_or_snapshot_yield_and_fixed_spares_only():
    demand = calculate_effective_component_demands(10, [_component()])["components"][0]
    actual = calculate_requisition(demand, actual_yield_per_sheet=3)
    snapshot = calculate_requisition(demand)
    non_die_cut = calculate_requisition(
        calculate_effective_component_demands(5, [_component(is_die_cut=False)])["components"][0],
        actual_yield_per_sheet=99,
    )

    assert (actual["yield_per_sheet"], actual["net_sheets"], actual["purchase_sheets"]) == (3, 7, 9)
    assert (snapshot["yield_per_sheet"], snapshot["net_sheets"], snapshot["purchase_sheets"]) == (4, 5, 7)
    assert (non_die_cut["yield_per_sheet"], non_die_cut["net_sheets"], non_die_cut["purchase_sheets"]) == (1, 10, 12)


def test_partial_inventory_optional_components_and_missing_message():
    required = _component(component_product_id=11, component_name="纸板", quantity_per_set=2)
    optional = _component(component_product_id=12, component_name="说明卡", quantity_per_set=1, is_required=False)
    demands = calculate_effective_component_demands(3, [required, optional])
    availability = calculate_kit_availability(demands, {11: 5, 12: 0})

    assert availability["max_complete_sets"] == 2
    assert availability["is_kit_complete"] is False
    assert [row["status"] for row in availability["components"]] == ["shortage", "optional_shortage"]
    assert "【纸板】缺1个（需求6个，可用5个）" in format_missing_component_message(availability)
    assert "说明卡" not in format_missing_component_message(availability)


def test_delivery_consumption_is_atomic_when_a_required_component_is_short():
    plan = build_delivery_consumption_plan(
        2,
        [_component(component_product_id=11, quantity_per_set=2), _component(component_product_id=12, quantity_per_set=1)],
        {11: 3, 12: 10},
    )

    assert plan["executable"] is False
    assert [row["planned_consumption_quantity"] for row in plan["consumption_plan"]] == [0, 0]
    assert "无法齐套" in plan["missing_message"]


def test_negative_adjustment_cannot_reduce_below_processed_sets():
    with pytest.raises(CompositeBOMExecutionError, match="不能小于已处理套数"):
        validate_order_adjustment(10, {"delta_sets": -4}, processed_sets=7)


def test_positive_integer_and_destination_contracts_are_strict():
    with pytest.raises(CompositeBOMExecutionError, match="每套用量必须是整数"):
        calculate_effective_component_demands(1, [_component(quantity_per_set=Decimal("1.5"))])
    assert validate_completion_destination(COMPLETION_DESTINATION_DIRECT_KIT) == "direct_kit"
    assert validate_completion_destination(COMPLETION_DESTINATION_STOCK) == "stock"
    with pytest.raises(CompositeBOMExecutionError):
        validate_completion_destination("direct")
