from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select, func

from tests.test_p1_47d_inventory_adjustment import stocktake_app, _login, _add, _batch, URL
from app.models.warehouse_inventory import InventoryLot


def _context(client, ids, location="fg1_add"):
    response = client.get("/api/warehouse/twin-operations/initial-stock-context", params={
        "location_id": ids[f"loc_{location}"], "product_id": ids["product"],
    })
    assert response.status_code == 200, response.text
    return response.json()


def _initial(ids, context):
    return {**_batch("mobile-initial-test", _add(
        client_item_id="mobile-initial", location_id=ids["loc_fg1_add"],
        inventory_type="finished", customer_id=ids["customer"],
        product_id=ids["product"], quantity=13,
    )), "initial_inventory_snapshot": context["snapshot"], "existing_inventory_acknowledged": True}


def test_initial_inbound_snapshot_replay_and_add_to_occupied_location(stocktake_app):
    app, factory, ids, _ = stocktake_app
    with TestClient(app) as client:
        _login(client, "p147d-admin")
        context = _context(client, ids)
        assert context["can_add"]
        assert context["existing_quantity"] > 0
        payload = _initial(ids, context)
        unacknowledged = client.post(URL, json={**payload, "existing_inventory_acknowledged": False})
        assert unacknowledged.status_code == 409
        saved = client.post(URL, json=payload)
        assert saved.status_code == 200, saved.text
        replay = client.post(URL, json=payload)
        assert replay.status_code == 200, replay.text
        assert replay.json()["idempotent_replay"]
        assert client.post(URL, json={**payload, "initial_inventory_snapshot": "0" * 64}).status_code == 409
        occupied = _context(client, ids)
        assert occupied["can_add"]
        second = {**_initial(ids, occupied), "idempotency_key": "mobile-initial-second"}
        added = client.post(URL, json=second)
        assert added.status_code == 200, added.text
        with factory() as db:
            assert db.scalar(select(func.sum(InventoryLot.quantity_available)).where(
                InventoryLot.warehouse_location_id == ids["loc_fg1_add"])) == 26


def test_initial_inbound_rejects_stale_stock_and_non_admin(stocktake_app):
    app, factory, ids, _ = stocktake_app
    with TestClient(app) as client:
        _login(client)
        assert client.get("/api/warehouse/twin-operations/initial-stock-context", params={
            "location_id": ids["loc_fg1_add"], "product_id": ids["product"],
        }).status_code == 403
        _login(client, "p147d-admin")
        payload = _initial(ids, _context(client, ids))
        with factory() as db:
            lot = db.scalar(select(InventoryLot).where(InventoryLot.warehouse_location_id == ids["loc_fg1"]))
            lot.version += 1
            db.commit()
        response = client.post(URL, json=payload)
        assert response.status_code == 409, response.text
        with factory() as db:
            assert not db.scalar(select(InventoryLot.id).where(InventoryLot.warehouse_location_id == ids["loc_fg1_add"]))


def test_admin_adds_different_product_to_same_location(stocktake_app):
    app, factory, ids, _ = stocktake_app
    with TestClient(app) as client:
        _login(client, "p147d-admin")
        first = _initial(ids, _context(client, ids))
        assert client.post(URL, json=first).status_code == 200
        context = client.get("/api/warehouse/twin-operations/initial-stock-context", params={
            "location_id": ids["loc_fg1_add"], "product_id": ids["other_product"]}).json()
        second = _initial(ids, context)
        second["idempotency_key"] = "initial-different-product"
        second["items"][0].update(customer_id=ids["other_customer"], product_id=ids["other_product"])
        saved = client.post(URL, json=second)
        assert saved.status_code == 200, saved.text
    with factory() as db:
        lots = list(db.scalars(select(InventoryLot).where(InventoryLot.warehouse_location_id == ids["loc_fg1_add"])))
        assert {lot.finished_detail.product_id for lot in lots} == {ids["product"], ids["other_product"]}
        assert sum(lot.quantity_available for lot in lots) == 26


def test_mobile_product_create_uses_master_validation_and_rejects_existing_code(stocktake_app):
    from app.api.products import router
    from app.models.product import Product
    app, factory, ids, _ = stocktake_app
    app.include_router(router, prefix="/api/products")
    payload = {"customer_id": ids["customer"], "product_code": "INITIAL-NEW-01",
               "customer_material_code": "INITIAL-NEW-01", "product_name": "现场新产品", "box_category": "normal"}
    with TestClient(app) as client:
        _login(client, "p147d-admin")
        response = client.post("/api/products/stocktake-create", json=payload)
        assert response.status_code == 201, response.text
        assert client.post("/api/products/stocktake-create", json={**payload, "product_name": "另一个名字"}).status_code == 409
        with factory() as db:
            assert db.scalar(select(func.count(Product.id)).where(Product.product_code == "INITIAL-NEW-01")) == 1
