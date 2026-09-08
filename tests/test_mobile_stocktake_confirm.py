from fastapi.testclient import TestClient
from sqlalchemy import select, func
from tests.test_n035_stocktake_api import stocktake_api, _login, _submission_payload
from app.models.warehouse_inventory import InventoryLot, InventoryMovement
import pytest
from datetime import date, datetime
from app.models.order import Order, OrderItem
from app.models.warehouse_inventory import InventoryReservation


def _seed_count_reservations(factory, ids):
    with factory() as db:
        lot = db.get(InventoryLot, ids["lot1"])
        lot.quantity_available = 0
        lot.quantity_reserved = 20
        row_ids = []
        for i in range(2):
            order = Order(order_number=f"COUNT-ORDER-{i}", customer_id=lot.finished_detail.owner_customer_id,
                          order_date=date(2026, 9, 8), status="pending_delivery")
            db.add(order)
            db.flush()
            item = OrderItem(order_id=order.id, product_id=lot.finished_detail.product_id,
                             quantity=100, unit_price=1, subtotal=100,
                             snapshot_product_name="实盘测试纸箱")
            db.add(item)
            db.flush()
            reservation = InventoryReservation(reservation_number=f"COUNT-RES-{i}",
                inventory_lot_id=lot.id, reservation_type="finished_order", order_id=order.id,
                order_item_id=item.id, reserved_stock_quantity=10, credited_requirement_quantity=10,
                yield_factor=1, reserved_at=datetime(2026, 9, 8, i), status="active")
            db.add(reservation)
            db.flush()
            row_ids.append(reservation.id)
        db.commit()
        return row_ids


@pytest.mark.parametrize("counted, released", [(19, [0, 1]), (9, [1, 10]), (0, [10, 10])])
def test_admin_shortage_keeps_oldest_reservation_and_replays_once(stocktake_api, counted, released):
    app, factory, ids = stocktake_api
    reservation_ids = _seed_count_reservations(factory, ids)
    with TestClient(app) as client:
        _login(client, "n035-admin")
        payload = _submission_payload(client, ids["location"], key=f"shortfall-{counted}",
                                      counts={ids["lot1"]: counted})
        result = client.post("/api/warehouse/stocktakes/confirm", json=payload)
        assert result.status_code == 201, result.text
        replay = client.post("/api/warehouse/stocktakes/confirm", json=payload)
        assert replay.status_code == 201, replay.text
        assert result.json()["id"] == replay.json()["id"]
    with factory() as db:
        lot = db.get(InventoryLot, ids["lot1"])
        assert (lot.quantity_available, lot.quantity_reserved) == (0, counted)
        rows = [db.get(InventoryReservation, value) for value in reservation_ids]
        assert [row.released_stock_quantity for row in rows] == released
        assert [row.released_requirement_quantity for row in rows] == released
        assert all(row.reserved_stock_quantity == 10 and row.consumed_stock_quantity == 0 for row in rows)
        assert all(db.get(OrderItem, row.order_item_id).quantity == 100 for row in rows)
        from app.api.deliveries import _inventory_sources_for_order_item
        from app.services.warehouse_inventory import active_finished_reserved_qty
        for row, loss in zip(rows, released):
            item = db.get(OrderItem, row.order_item_id)
            assert active_finished_reserved_qty(db, item.id) == 10 - loss
            sources = _inventory_sources_for_order_item(db, order_item=item, planned_delivery_quantity=10)
            assert sum(source["quantity_to_pick_stock"] for source in sources) == 10 - loss
        movements = list(db.scalars(select(InventoryMovement).order_by(InventoryMovement.id)))
        assert len(movements) == sum(value > 0 for value in released) + 1
        assert movements[-1].movement_type == "adjust"
        assert movements[-1].quantity == 20 - counted
        assert all(m.before_available + m.before_reserved == m.after_available + m.after_reserved
                   for m in movements[:-1])


def test_shortfall_release_and_adjustment_roll_back_together(stocktake_api, monkeypatch):
    from app.services import stocktake
    from app.models.stocktake import StocktakeOrder
    app, factory, ids = stocktake_api
    row_ids = _seed_count_reservations(factory, ids)
    def fail_adjustment():
        raise RuntimeError("injected adjustment failure")
    monkeypatch.setattr(stocktake, "_movement_number", fail_adjustment)
    with TestClient(app) as client:
        _login(client, "n035-admin")
        payload = _submission_payload(client, ids["location"], key="shortfall-rollback",
                                      counts={ids["lot1"]: 19})
        with pytest.raises(RuntimeError, match="injected"):
            client.post("/api/warehouse/stocktakes/confirm", json=payload)
    with factory() as db:
        lot = db.get(InventoryLot, ids["lot1"])
        assert (lot.quantity_available, lot.quantity_reserved) == (0, 20)
        assert all(db.get(InventoryReservation, value).released_stock_quantity == 0 for value in row_ids)
        assert db.scalar(select(func.count(InventoryMovement.id))) == 0
        assert db.scalar(select(func.count(StocktakeOrder.id))) == 0


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


def test_admin_shortage_with_missing_reservation_ledger_rolls_back_submission(stocktake_api):
    from app.models.stocktake import StocktakeOrder
    app, factory, ids = stocktake_api
    with TestClient(app) as client:
        _login(client, "n035-admin")
        payload = _submission_payload(client, ids["location"], key="below-reserved",
                                      counts={ids["lot1"]: 1})
        response = client.post("/api/warehouse/stocktakes/confirm", json=payload)
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "RESERVATION_LEDGER_MISMATCH"
    with factory() as db:
        assert db.scalar(select(func.count(StocktakeOrder.id))) == 0
        assert db.scalar(select(func.count(InventoryMovement.id))) == 0
