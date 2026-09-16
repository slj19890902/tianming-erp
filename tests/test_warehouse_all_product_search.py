from fastapi.testclient import TestClient
from sqlalchemy import select, event

from app.api.deps import get_db
from app.models.product import Product
from app.models.warehouse_inventory import InventoryLot
from test_p1_29_warehouse_twin_dashboard import twin_dashboard_app, _login


def test_pending_with_nominal_floor_is_visible_in_every_floor_and_not_mapped(twin_dashboard_app):
    app, _ = twin_dashboard_app
    dependency = app.dependency_overrides[get_db]()
    db = next(dependency)
    try:
        lot = db.scalar(select(InventoryLot).where(InventoryLot.lot_number == "FG-OWNER"))
        lot.location.warehouse_floor = 1
        lot.location.placement_status = "unplaced"
        db.commit()
        with TestClient(app) as client:
            _login(client, "twin-scoped")
            for floor in ("ALL", "3F", "4F", "1F", "UNLOCATED"):
                response = client.get("/api/warehouse/twin-operations/locate", params={
                    "keyword": "TM-FG-001", "search_type": "inventory", "search_floor": floor})
                assert response.status_code == 200, response.text
                row = next(row for row in response.json()["items"] if row["lot_id"] == lot.id)
                assert row["floor_code"] == "UNLOCATED"
                assert row["location_name"] == "待归位"
                assert row["pending_relocation"] is True
                assert row["map_position"] is None
                assert row["location_id"] == lot.warehouse_location_id
    finally:
        dependency.close()


def test_catalog_no_stock_paging_scope_and_read_only(twin_dashboard_app):
    app, ids = twin_dashboard_app
    dependency = app.dependency_overrides[get_db]()
    db = next(dependency)
    try:
        db.add_all([Product(customer_id=ids["owner"], product_code=f"CATALOG-{i}", customer_material_code=f"CATALOG-{i}",
                            product_name="无库存测试纸箱", length_mm=520, width_mm=350, height_mm=300)
                    for i in range(5)])
        db.add(Product(customer_id=ids["hidden_owner"], product_code="CATALOG-HIDDEN", customer_material_code="CATALOG-HIDDEN", product_name="隐藏"))
        db.commit()
        statements = []
        def capture(_conn, _cursor, sql, *_):
            statements.append(sql)
        with TestClient(app) as client:
            _login(client, "twin-scoped")
            event.listen(db.get_bind(), "before_cursor_execute", capture)
            try:
                cursor, rows = None, []
                for count in (2, 2, 1):
                    params = {"keyword": "CATALOG", "page_size": 2}
                    if cursor:
                        params["after_product_id"] = cursor
                    response = client.get("/api/warehouse/twin-operations/catalog-search", params=params)
                    assert response.status_code == 200, response.text
                    data = response.json()
                    assert len(data["items"]) == count
                    rows.extend(data["items"])
                    cursor = data["next_after_product_id"]
                assert cursor is None
                assert len({r["product_id"] for r in rows}) == 5
                assert all(r["stock_status"] == "no_stock" for r in rows)
                assert not any("HIDDEN" in r["inventory_code"] for r in rows)
                assert client.get("/api/warehouse/twin-operations/catalog-search", params={"keyword": "TM-FG-001"}).json()["items"] == []
                assert len(client.get("/api/warehouse/twin-operations/catalog-search", params={"keyword": "520*350*300"}).json()["items"]) == 5
            finally:
                event.remove(db.get_bind(), "before_cursor_execute", capture)
        assert not any(s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for s in statements)
    finally:
        dependency.close()


def test_catalog_requires_auth_and_valid_paging(twin_dashboard_app):
    app, _ = twin_dashboard_app
    with TestClient(app) as client:
        assert client.get("/api/warehouse/twin-operations/catalog-search?keyword=TM").status_code == 401
        _login(client, "twin-admin")
        for params in ({"keyword": " "}, {"keyword": "TM", "page_size": 501}, {"keyword": "TM", "after_product_id": -1}):
            assert client.get("/api/warehouse/twin-operations/catalog-search", params=params).status_code == 422
