import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, insert, select

from app.api.deps import get_db
from app.models.warehouse_inventory import InventoryLot, FinishedGoodsInventoryDetail
from test_p1_29_warehouse_twin_dashboard import twin_dashboard_app, _login


@pytest.fixture
def large_search_app(twin_dashboard_app):
    app, ids = twin_dashboard_app
    dependency = app.dependency_overrides[get_db]()
    db = next(dependency)
    try:
        original = db.scalar(select(FinishedGoodsInventoryDetail).order_by(FinishedGoodsInventoryDetail.inventory_lot_id))
        detail = {col.name: getattr(original, col.name) for col in original.__table__.columns
                  if col.name not in {"id", "inventory_lot_id"}}
        source = db.get(InventoryLot, original.inventory_lot_id)
        lot = {col.name: getattr(source, col.name) for col in source.__table__.columns
               if col.name not in {"id", "lot_number", "quantity_available", "quantity_reserved"}}
        db.execute(insert(InventoryLot.__table__), [dict(lot, id=10000+i, lot_number=f"BULK-{i:04}",
            quantity_available=1, quantity_reserved=0)
            for i in range(2607)])
        db.execute(insert(FinishedGoodsInventoryDetail.__table__), [dict(detail,
            inventory_lot_id=10000+i, inventory_code_snapshot="LATE-MATCH" if i >= 2600 else "EARLY-ONLY")
            for i in range(2607)])
        db.commit()
        yield app, ids, db.get_bind()
    finally:
        dependency.close()


@pytest.mark.parametrize("path", ["twin-dashboard/search", "twin-operations/locate"])
def test_search_after_2500_and_cursor_pages_are_complete_read_only(large_search_app, path):
    app, _, engine = large_search_app
    statements = []
    def capture(_conn, _cursor, sql, params, *_):
        statements.append((sql, params))
    with TestClient(app) as client:
        _login(client, "twin-scoped")
        event.listen(engine, "before_cursor_execute", capture)
        try:
            cursor, seen = None, []
            for expected_size in (3, 3, 1):
                params = dict(keyword="LATE-MATCH", search_type="finished", page_size=3)
                if cursor:
                    params["after_lot_id"] = cursor
                response = client.get("/api/warehouse/"+path, params=params)
                assert response.status_code == 200, response.text
                data = response.json()
                assert len(data["items"]) == expected_size
                assert data["pagination"]["counts_scope"] == "page"
                seen.extend(row["lot_id"] for row in data["items"])
                cursor = data["pagination"]["next_after_lot_id"]
                assert data["pagination"]["has_more"] == (expected_size == 3)
            assert set(seen) == set(range(12600, 12607))
            assert len(seen) == len(set(seen))
            assert cursor is None
        finally:
            event.remove(engine, "before_cursor_execute", capture)
    assert not any(sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for sql, _ in statements)
    lot_selects = [(sql, params) for sql, params in statements
                   if "FROM inventory_lots" in sql and "ORDER BY inventory_lots.id" in sql]
    assert lot_selects
    assert all("LIMIT" in sql and int(params[-2]) <= 200 for sql, params in lot_selects)


@pytest.mark.parametrize("params", [{"page_size": 0}, {"page_size": 501}, {"after_lot_id": 0}, {"after_lot_id": -1}])
def test_invalid_search_paging_parameters(twin_dashboard_app, params):
    app, _ = twin_dashboard_app
    with TestClient(app) as client:
        _login(client, "twin-admin")
        for path in ("twin-dashboard/search", "twin-operations/locate"):
            response = client.get("/api/warehouse/"+path, params=dict(keyword="TM", **params))
            assert response.status_code == 422


def test_cursor_does_not_bypass_customer_scope(twin_dashboard_app):
    app, _ = twin_dashboard_app
    with TestClient(app) as client:
        _login(client, "twin-scoped")
        for path in ("twin-dashboard/search", "twin-operations/locate"):
            response = client.get("/api/warehouse/"+path,
                params=dict(keyword="HIDDEN", search_type="finished", page_size=1, after_lot_id=1))
            assert response.status_code == 200, response.text
            assert response.json()["items"] == []
            assert response.json()["pagination"]["has_more"] is False


def test_more_than_500_matches_can_all_be_reached(large_search_app):
    app, ids, _ = large_search_app
    with TestClient(app) as client:
        _login(client, "twin-scoped")
        params = dict(search_type="finished", keyword="EARLY-ONLY", customer_id=ids["owner"], page_size=500)
        seen = []
        while True:
            response = client.get("/api/warehouse/twin-operations/locate", params=params)
            assert response.status_code == 200, response.text
            page = response.json()
            seen.extend(item["lot_id"] for item in page["items"])
            if not page["pagination"]["has_more"]:
                break
            params["after_lot_id"] = page["pagination"]["next_after_lot_id"]
            assert len(seen) <= 2600
        assert len(seen) == 2600
        assert set(seen) == set(range(10000, 12600))


def test_customer_only_search_stays_scoped_and_empty_last_page(twin_dashboard_app):
    app, ids = twin_dashboard_app
    with TestClient(app) as client:
        _login(client, "twin-scoped")
        response = client.get("/api/warehouse/twin-operations/locate",
            params=dict(search_type="finished", customer_id=ids["owner"], page_size=1))
        assert response.status_code == 200, response.text
        assert len(response.json()["items"]) == 1
        assert response.json()["pagination"]["has_more"]
        denied = client.get("/api/warehouse/twin-operations/locate",
            params=dict(search_type="finished", customer_id=ids["hidden_owner"], page_size=1))
        assert denied.status_code == 403
        empty = client.get("/api/warehouse/twin-operations/locate",
            params=dict(search_type="finished", customer_id=ids["owner"], after_lot_id=999999))
        assert empty.json()["items"] == []
        assert empty.json()["pagination"]["next_after_lot_id"] is None


def test_mold_search_does_not_load_unrelated_inventory(twin_dashboard_app):
    app, _ = twin_dashboard_app
    dependency = app.dependency_overrides[get_db]()
    db = next(dependency)
    engine = db.get_bind()
    statements = []
    def capture(_conn, _cursor, sql, *_):
        statements.append(sql)
    try:
        with TestClient(app) as client:
            _login(client, "twin-admin")
            event.listen(engine, "before_cursor_execute", capture)
            try:
                result = client.get("/api/warehouse/twin-operations/locate",
                    params=dict(search_type="mold", keyword="不存在"))
                assert result.status_code == 200, result.text
                assert result.json()["items"] == []
            finally:
                event.remove(engine, "before_cursor_execute", capture)
        assert not any("FROM inventory_lots" in sql for sql in statements)
    finally:
        dependency.close()


@pytest.mark.parametrize("path", ["twin-dashboard/search", "twin-operations/locate"])
def test_business_terms_keep_existing_matching_and_exact_location_identity(twin_dashboard_app, path):
    app, ids = twin_dashboard_app
    dependency = app.dependency_overrides[get_db]()
    db = next(dependency)
    try:
        for detail in db.scalars(select(FinishedGoodsInventoryDetail).where(
                FinishedGoodsInventoryDetail.owner_customer_id == ids["owner"])):
            detail.length_mm, detail.width_mm, detail.height_mm = 520, 350, 300
        db.commit()
    finally:
        dependency.close()
    with TestClient(app) as client:
        _login(client, "twin-scoped")
        expected = None
        for keyword in ("思迈尔", "SMILE", "TM-FG-001", "五层加强纸箱", "520*350*300mm"):
            response = client.get("/api/warehouse/"+path,
                params=dict(search_type="finished", keyword=keyword, page_size=100))
            assert response.status_code == 200, response.text
            items = [r for r in response.json()["items"] if r["inventory_type"] == "finished"]
            identities = {(r["lot_id"], r["location_id"], r["pallet_id"], r["position_status"])
                          for r in items}
            expected = identities if expected is None else expected
            assert identities == expected, keyword
            assert len(identities) == 3
            assert response.json()["pagination"]["has_more"] is False
