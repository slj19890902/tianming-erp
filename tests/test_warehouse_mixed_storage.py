from fastapi.testclient import TestClient
import pytest
from app.models.warehouse_inventory import InventoryLot, Floor3LocationLayout
from tests.test_p1_47d_inventory_adjustment import stocktake_app, _add, _batch, _login, URL

@pytest.mark.parametrize("location_key,types", [
    ("loc_fg1_add", ["finished", "semi_finished", "raw_material"]),
    ("loc_semi1_add", ["raw_material", "semi_finished", "finished"]),
])
def test_mixed_goods_keep_separate_lots_and_units(stocktake_app, location_key, types):
    app, factory, ids, _ = stocktake_app
    payload = _batch("mixed-" + location_key, *[
        _add(client_item_id=kind, location_id=ids[location_key], inventory_type=kind,
             customer_id=ids["customer"], product_id=ids["product"], quantity=7+i)
        for i, kind in enumerate(types)
    ])
    with TestClient(app) as client:
        _login(client, "p147d-admin")
        response = client.post(URL, json=payload)
        assert response.status_code == 200, response.text
        replay = client.post(URL, json=payload)
        assert replay.status_code == 200, replay.text
        assert replay.json()["idempotent_replay"]
        overview = client.get("/api/warehouse/twin-dashboard/overview?days=30")
        assert overview.status_code == 200, overview.text
        location = next(r for r in overview.json()["locations"] if r["location_id"] == ids[location_key])
        assert any(r.get("inventory_usage") == "raw_material" for r in location["loose_items"])
        with factory() as db:
            version = db.get(Floor3LocationLayout, ids[location_key]).version
        followup = _batch("mixed-followup-"+location_key, _add(client_item_id="next-raw", location_id=ids[location_key], inventory_type="raw_material",customer_id=ids["customer"],product_id=ids["product"],quantity=2,expected_layout_version=version))
        added = client.post(URL, json=followup)
        assert added.status_code == 200, added.text
    with factory() as db:
        lots = {r["client_item_id"]: db.get(InventoryLot, r["lot_id"]) for r in response.json()["items"]}
        assert len({lot.id for lot in lots.values()}) == 3
        assert lots["finished"].inventory_type == "finished"
        assert lots["finished"].unit == "boxes"
        assert lots["raw_material"].inventory_type == "semi_finished"
        assert lots["raw_material"].unit == "sheets"
        assert lots["raw_material"].semi_finished_detail.sheet_type == "raw_board"
        assert lots["semi_finished"].semi_finished_detail.sheet_type != "raw_board"
        assert all(lot.warehouse_location_id == ids[location_key] for lot in lots.values())
        assert [lots[k].quantity_available for k in types] == [7,8,9]


def test_existing_pallet_accepts_raw_at_capacity_but_new_slot_does_not(stocktake_app):
    from sqlalchemy import select
    from app.models.warehouse_inventory import WarehouseArea, WarehouseFloor, WarehouseLocation
    from datetime import datetime
    app, factory, ids, _ = stocktake_app
    with factory() as db:
        location = db.get(WarehouseLocation, ids["loc_fg3"])
        area = db.scalar(select(WarehouseArea).join(WarehouseFloor).where(WarehouseFloor.floor_number == 3, WarehouseArea.area_code == location.area_code))
        area.capacity_review_status = "confirmed"
        area.capacity_eligible = True
        area.confirmed_pallet_capacity = 1
        area.planned_pallet_capacity = 1
        area.capacity_reviewed_by = "test"
        area.capacity_reviewed_at = datetime.now()
        db.commit()
    with TestClient(app) as client:
        _login(client, "p147d-admin")
        for key, expected in [("loc_fg3", 200), ("loc_fg3_add", 409)]:
            response = client.post(URL,json=_batch("capacity-mixed-"+key,_add(client_item_id=key,location_id=ids[key],inventory_type="raw_material",customer_id=ids["customer"],product_id=ids["product"],quantity=2)))
            assert response.status_code == expected, response.text
