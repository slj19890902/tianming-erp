from fastapi.testclient import TestClient
from sqlalchemy import select
from tests.test_p1_21f3_mobile_warehouse_map import _add_map_target, _mobile_move_identity
from test_p1_21b_mobile_admin_product_search import mobile_erp_app, _login
from app.models.warehouse_inventory import InventoryLot
from app.models.product import Product


def test_admin_can_partially_move_different_product_to_occupied_location(mobile_erp_app):
    app, _, factory = mobile_erp_app
    lot_id, target_id = _add_map_target(factory, code="C1-MIXED", with_existing=True)
    with factory() as db:
        target = db.scalar(select(InventoryLot).where(InventoryLot.warehouse_location_id == target_id))
        product = db.scalar(select(Product).where(Product.product_code == "MOBILE-BOX-002"))
        target.finished_detail.product_id = product.id
        target.finished_detail.inventory_code_snapshot = product.product_code
        db.commit()
    with TestClient(app) as client:
        _login(client, "mobile-admin")
        payload = {**_mobile_move_identity(client, source_lot_id=lot_id, target_location_id=target_id),
                   "expected_version": 1, "quantity": 20, "target_location_id": target_id,
                   "idempotency_key": "admin-mixed-partial", "physical_move_confirmed": True}
        response = client.post(f"/api/mobile/erp/warehouse/lots/{lot_id}/moves", json=payload)
        assert response.status_code == 200, response.text
        assert client.post(f"/api/mobile/erp/warehouse/lots/{lot_id}/moves", json=payload).status_code == 200
    with factory() as db:
        source = db.get(InventoryLot, lot_id)
        assert source.quantity_available + source.quantity_reserved == 60
        rows = list(db.scalars(select(InventoryLot).where(InventoryLot.warehouse_location_id == target_id,
                                                        InventoryLot.quantity_available > 0)))
        assert len({row.finished_detail.product_id for row in rows}) == 2


def test_employee_selects_product_and_reports_without_changing_inventory(mobile_erp_app):
    from sqlalchemy import func
    from app.models.warehouse_inventory import WarehouseUnmatchedInventoryObservation
    app, ids, factory = mobile_erp_app
    _, target_id = _add_map_target(factory, code="C1-REPORT", with_existing=False)
    with TestClient(app) as client:
        _login(client, "mobile-scoped")
        response = client.get("/api/mobile/erp/warehouse/observation-products", params={"q": "MOBILE-BOX"})
        assert response.status_code == 200, response.text
        assert any(row["product_id"] == ids["product"] for row in response.json()["items"])
        hidden = client.get("/api/mobile/erp/warehouse/observation-products", params={"q": "OTHER-MOBILE"})
        assert hidden.json()["items"] == []
        payload = {"product_id": ids["product"], "observed_location_id": target_id,
                   "observed_location_layout_version": 1, "inventory_keyword": "selected",
                   "reported_quantity": 15, "reported_unit": "只", "reason": "选定产品",
                   "idempotency_key": "selected-product-report"}
        saved = client.post("/api/mobile/erp/warehouse/unmatched-inventory-observations", json=payload)
        assert saved.status_code == 201, saved.text
        assert client.post("/api/mobile/erp/warehouse/unmatched-inventory-observations", json=payload).status_code == 201
    with factory() as db:
        report = db.scalar(select(WarehouseUnmatchedInventoryObservation))
        assert report.inventory_keyword == "MOBILE-BOX-001"
        assert report.reported_quantity == 15
        assert db.scalar(select(func.count(InventoryLot.id)).where(InventoryLot.warehouse_location_id == target_id)) == 0


def test_floor_overview_contains_only_regions_not_lots(mobile_erp_app, monkeypatch):
    from app.api import mobile_erp
    monkeypatch.setattr(mobile_erp, "load_warehouse_twin_floor", lambda _: {
        "floor_code": "3F", "features": [{"id": "test-c1", "feature_kind": "zone", "erp_area_code": "C1",
                                           "points": [[0, 0], [100, 0], [100, 100], [0, 100]]}]})
    app, _, factory = mobile_erp_app
    _add_map_target(factory, code="C1-OVERVIEW", with_existing=False)
    with TestClient(app) as client:
        _login(client, "mobile-admin")
        response = client.get("/api/mobile/erp/warehouse/map/overview/3F")
        assert response.status_code == 200, response.text
        assert response.json()["areas"]
        assert all(set(area) == {"feature_id", "area_code", "area_name", "points"} for area in response.json()["areas"])
        assert "location_id" not in response.text
