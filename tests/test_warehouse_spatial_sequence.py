from types import SimpleNamespace as N

from app.services.warehouse_location_sequence import spatial_sequences
from app.services.warehouse_location_address import format_location_address
from app.services.warehouse_location_sequence import applied_ground_geometry


def test_numbering_follows_rows_then_columns_not_creation_order():
    def row(id, x, y, active=True):
        return (N(id=id, warehouse_floor=3, area_code="A1", is_active=active, address_kind="ground_slot", sort_order=id),
                N(left_pct=x, top_pct=y, height_pct=10))
    rows = [row(10, 50, 1), row(20, 10, 0), row(30, 20, 20), row(40, 0, 0, False)]
    assert spatial_sequences(rows) == {20: 1, 10: 2, 30: 3}
    assert spatial_sequences(list(reversed(rows))) == {20: 1, 10: 2, 30: 3}


def test_employee_ground_name_hides_internal_identity():
    from app.models.warehouse_inventory import WarehouseLocation, WarehouseArea, WarehouseFloor
    floor = WarehouseFloor(floor_number=3, floor_code="3F", floor_name="三楼")
    area = WarehouseArea(area_code="EDIT-056", area_name="南A1", floor=floor)
    location = WarehouseLocation(location_code="INTERNAL-123", address_kind="ground_slot", warehouse_floor=3, area_code="EDIT-056")
    assert format_location_address(location, area=area, floor=floor, area_sequence=1) == ("INTERNAL-123", "三楼 南A1-01")


def test_applied_map_positions_replace_stale_percent_boxes_without_mutating_them():
    layout = N(left_pct=50, top_pct=50, width_pct=40, height_pct=30)
    feature = {"points": [[0,0], [10000,0], [10000,10000], [0,10000]],
        "ground_location_draft": {"slots": [{"location_id": 5, "x_mm": 2000, "y_mm": 8000, "width_mm": 1200, "depth_mm": 1000}]}}
    assert applied_ground_geometry(5, layout, feature) == {"left_pct":20, "top_pct":10, "width_pct":12, "height_pct":10}
    assert layout.width_pct == 40
    assert applied_ground_geometry(6, layout, feature)["width_pct"] == 40
