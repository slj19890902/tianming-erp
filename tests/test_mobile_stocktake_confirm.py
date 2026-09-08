from fastapi.testclient import TestClient
from sqlalchemy import select, func
from tests.test_n035_stocktake_api import stocktake_api, _login, _submission_payload
from app.models.warehouse_inventory import InventoryLot, InventoryMovement


def test_admin_confirm_applies_once_and_rejects_changed_retry(stocktake_api):
    app, factory, ids = stocktake_api
    with TestClient(app) as client:
        _login(client, "n035-admin")
        payload = _submission_payload(client, ids["location"], key="direct-confirm",
                                      counts={ids["lot1"]: 15})
        saved = client.post("/api/warehouse/stocktakes/confirm", json=payload)
        assert saved.status_code == 201, saved.text
        assert saved.json()["status"] == "approved"
        replay = client.post("/api/warehouse/stocktakes/confirm", json=payload)
        assert replay.status_code == 201, replay.text
        assert replay.json()["id"] == saved.json()["id"]
        payload["items"][0]["counted_quantity"] += 1
        assert client.post("/api/warehouse/stocktakes/confirm", json=payload).status_code == 409
    with factory() as db:
        lot = db.get(InventoryLot, ids["lot1"])
        assert (lot.quantity_available, lot.quantity_reserved) == (13, 2)
        assert db.scalar(select(func.count(InventoryMovement.id))) == 1


def test_employee_cannot_confirm_directly(stocktake_api):
    app, factory, ids = stocktake_api
    with TestClient(app) as client:
        _login(client, "n035-workshop")
        payload = _submission_payload(client, ids["location"], key="denied-direct")
        assert client.post("/api/warehouse/stocktakes/confirm", json=payload).status_code == 403
    with factory() as db:
        assert db.scalar(select(func.count(InventoryMovement.id))) == 0


def test_admin_count_above_reserved_keeps_surplus_available_once(stocktake_api):
    """A 20-unit reserved balance does not cap an administrator's count at 20."""
    app, factory, ids = stocktake_api
    with factory() as db:
        lot = db.get(InventoryLot, ids["lot1"])
        lot.quantity_available = 0
        lot.quantity_reserved = 20
        db.commit()
    with TestClient(app) as client:
        _login(client, "n035-admin")
        payload = _submission_payload(client, ids["location"], key="count-surplus-21",
                                      counts={ids["lot1"]: 21})
        saved = client.post("/api/warehouse/stocktakes/confirm", json=payload)
        assert saved.status_code == 201, saved.text
        assert saved.json()["status"] == "approved"
        replay = client.post("/api/warehouse/stocktakes/confirm", json=payload)
        assert replay.status_code == 201, replay.text
        assert replay.json()["id"] == saved.json()["id"]
    with factory() as db:
        lot = db.get(InventoryLot, ids["lot1"])
        assert (lot.quantity_available, lot.quantity_reserved) == (1, 20)
        movements = list(db.scalars(select(InventoryMovement)))
        assert len(movements) == 1
        assert movements[0].quantity == 1
        assert movements[0].before_reserved == movements[0].after_reserved == 20


def test_admin_shortage_below_reserved_rolls_back_submission(stocktake_api):
    from app.models.stocktake import StocktakeOrder
    app, factory, ids = stocktake_api
    with TestClient(app) as client:
        _login(client, "n035-admin")
        payload = _submission_payload(client, ids["location"], key="below-reserved",
                                      counts={ids["lot1"]: 1})
        response = client.post("/api/warehouse/stocktakes/confirm", json=payload)
        assert response.status_code == 409, response.text
    with factory() as db:
        assert db.scalar(select(func.count(StocktakeOrder.id))) == 0
        assert db.scalar(select(func.count(InventoryMovement.id))) == 0
