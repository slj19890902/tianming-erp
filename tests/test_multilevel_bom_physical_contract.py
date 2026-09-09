from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.api.requisition import _bom_snapshot_requirements
from app.models.product_bom import SalesOrderItemBomComponent
from app.services.multilevel_bom_compile import physical_routes
from app.services.bom_physical_quantities import resolve_bom_sheet_yield
from app.services.composite_bom_execution import CompositeBOMExecutionError


def snapshot(**kwargs):
    fields = dict(id=1, sales_order_item_id=1, component_product_id=2,
        quantity_per_set=Decimal(1), required_piece_quantity=Decimal(100),
        snapshot_component_box_style="隔板", snapshot_component_default_cutting_mode="一开一",
        snapshot_component_pieces_per_box=1, is_die_cut=False, mold_max_yield_per_sheet=None,
        snapshot_component_product_name="测试物理片组", spare_sheet_quantity=0)
    fields.update(kwargs)
    return SalesOrderItemBomComponent(**fields)


@pytest.mark.parametrize("fields, expected", [
    ({"is_die_cut": True, "mold_max_yield_per_sheet": 4}, 4),
    ({"is_die_cut": True, "mold_max_yield_per_sheet": 4,
      "snapshot_component_default_cutting_mode": "一开二"}, 2),
    ({"snapshot_component_box_style": "A1普通箱", "snapshot_component_default_cutting_mode": "一开四"}, 1),
    ({"snapshot_component_default_cutting_mode": "一开四"}, 4),
    ({"snapshot_component_box_style": "A3天地盖", "snapshot_component_pieces_per_box": 2}, 1),
])
def test_frozen_graph_matches_actual_requisition_physical_yield(fields, expected):
    row = snapshot(**fields)
    routes = physical_routes(row)
    for route in routes:
        requirement = _bom_snapshot_requirements(SimpleNamespace(get=lambda *args: None), row, component_type=route.key,
            effective_sets_override=100, required_piece_quantity_override=100,
            inventory_coverage_override={"finished_piece_quantity": 0,
                "semi_piece_quantity": 0, "total_piece_quantity": 0})
        assert route.pieces_per_sheet == requirement["yield_per_sheet"] == expected
        assert route.pieces_per_unit == requirement["physical_pieces_per_component"]
        assert requirement["requisition_qty"] == (100 * route.pieces_per_unit + expected - 1) // expected


def test_actual_mold_yield_replaces_cutting_instead_of_multiplying():
    row = snapshot(is_die_cut=True, mold_max_yield_per_sheet=6,
                   snapshot_component_default_cutting_mode="一开四")
    result = resolve_bom_sheet_yield(row, actual_yield_per_sheet=3)
    assert result.cutting_factor == 4
    assert result.yield_per_sheet == 3
    assert physical_routes(row)[0].pieces_per_sheet == 4


@pytest.mark.parametrize("fields,actual", [
    ({}, 2),
    ({"is_die_cut": True}, 2),
    ({"is_die_cut": True, "mold_max_yield_per_sheet": 2}, 3),
    ({"is_die_cut": True, "mold_max_yield_per_sheet": 2,
      "snapshot_component_default_cutting_mode": "一开四"}, None),
    ({"is_die_cut": True, "mold_max_yield_per_sheet": 0}, None),
])
def test_invalid_yield_never_becomes_a_silent_multiplier(fields, actual):
    with pytest.raises(CompositeBOMExecutionError):
        resolve_bom_sheet_yield(snapshot(**fields), actual_yield_per_sheet=actual)
