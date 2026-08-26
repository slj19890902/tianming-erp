from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from test_p1_81_receipt_purpose_flow import (
    _create_frozen_sources,
    _freeze_receipt_fact,
    _receive,
    _seed_material_and_staging,
    _use_p181_published_map_identity,
)
from test_n036_delivery_pick import (
    _create_task as _create_pick_task,
    _login as _pick_login,
    pick_app,
)
from test_p1_45a_mobile_measured_map_redesign import _add_measured_pick_location
from test_phase11_requisition import _login, requisition_app


@pytest.fixture(autouse=True)
def _receipt_auto_delivery_map_identity(monkeypatch) -> None:
    _use_p181_published_map_identity(monkeypatch)


def _seed_pending_receipt_auto(
    client: TestClient,
    session_factory,
    *,
    ordered_quantity: int,
    received_quantity: int,
) -> None:
    _seed_material_and_staging(session_factory)
    source = _create_frozen_sources(
        client,
        session_factory,
        order_quantity=ordered_quantity,
        purchase_total=ordered_quantity,
        order_purpose=ordered_quantity,
        stock_purpose=0,
    )[0]
    frozen = _freeze_receipt_fact(
        client,
        source,
        idempotency_key=f"p1102-delivery-price-{ordered_quantity}-{received_quantity}",
    )
    assert frozen.status_code == 200, frozen.text
    received = _receive(
        client,
        source,
        frozen.json(),
        quantity=received_quantity,
        idempotency_key=f"p1102-delivery-receive-{ordered_quantity}-{received_quantity}",
    )
    assert received.status_code == 200, received.text


@pytest.mark.parametrize(
    ("ordered_quantity", "received_quantity"),
    ((100, 62), (250, 100)),
)
def test_receipt_auto_finished_inventory_is_in_stock_and_delivery_not_production_queue(
    requisition_app,
    ordered_quantity: int,
    received_quantity: int,
) -> None:
    from app.api.deliveries import (
        _delivery_remaining_quantity,
        router as deliveries_router,
    )
    from app.api.production import router as production_router
    from app.models.order import OrderItem
    from app.models.production import ProductionCompletion, ProductionTask
    from app.models.warehouse_inventory import InventoryLot
    from app.services.location_candidates import current_same_location_pallet

    app, session_factory = requisition_app
    app.include_router(deliveries_router, prefix="/api/deliveries")
    app.include_router(production_router, prefix="/api/production")

    with TestClient(app) as client:
        _login(client, "admin")
        _seed_pending_receipt_auto(
            client,
            session_factory,
            ordered_quantity=ordered_quantity,
            received_quantity=received_quantity,
        )

        with session_factory() as session:
            item = session.get(OrderItem, 1)
            task = session.scalar(
                select(ProductionTask).where(
                    ProductionTask.order_item_id == 1,
                    ProductionTask.task_role == "order_main",
                )
            )
            assert item is not None and task is not None
            assert task.status == "pending"
            assert _delivery_remaining_quantity(session, item) == received_quantity
            task_id = int(task.id)
            task_version = int(task.version)
            completion = session.scalar(
                select(ProductionCompletion).where(
                    ProductionCompletion.task_id == task_id,
                    ProductionCompletion.origin == "receipt_auto",
                    ProductionCompletion.status == "posted",
                )
            )
            assert completion is not None
            lot = session.get(InventoryLot, completion.inventory_lot_id)
            assert lot is not None
            assert lot.inventory_type == "finished"
            assert lot.status == "active"
            assert lot.warehouse_location_id is not None
            assert (
                int(lot.quantity_available or 0)
                + int(lot.quantity_reserved or 0)
                + int(lot.quantity_damaged or 0)
            ) == received_quantity
            pallet = current_same_location_pallet(lot)
            assert pallet is not None
            assert int(pallet.location_id) == int(lot.warehouse_location_id)
            completion_count = int(
                session.scalar(select(func.count(ProductionCompletion.id))) or 0
            )

        pending = client.get(
            "/api/deliveries/pending-items/search",
            params={"customer_id": 1, "order_item_id": 1},
        )
        assert pending.status_code == 200, pending.text
        rows = pending.json()["items"]
        assert len(rows) == 1
        assert rows[0]["order_item_id"] == 1
        assert rows[0]["order_remaining_quantity"] == ordered_quantity
        assert rows[0]["deliverable_quantity"] == received_quantity
        assert rows[0]["remaining_quantity"] == received_quantity

        production = client.get(
            "/api/production/tasks",
            params={"status": "pending", "page": 1, "page_size": 25},
        )
        assert production.status_code == 200, production.text
        assert production.json()["items"] == []
        assert production.json()["total"] == 0

        waiting = client.get(
            "/api/production/tasks",
            params={"status": "waiting_material", "page": 1, "page_size": 25},
        )
        assert waiting.status_code == 200, waiting.text
        assert waiting.json()["total"] == 1
        waiting_row = waiting.json()["items"][0]
        assert waiting_row["id"] == task_id
        assert waiting_row["status"] == "waiting_material"
        assert waiting_row["receipt_purpose_managed"] is True
        assert waiting_row["completion_actionable"] is False
        assert waiting_row["delivery_ready_quantity"] == received_quantity
        assert waiting_row["delivery_actionable"] is True

        repeated_completion = client.post(
            "/api/production/completion-batches",
            json={
                "idempotency_key": f"p1102-no-repeat-{ordered_quantity}",
                "items": [
                    {
                        "task_id": task_id,
                        "expected_version": task_version,
                        "disposition": "direct",
                        "material_input_quantity": 1,
                        "actual_output_quantity": 1,
                        "defective_quantity": 0,
                        "direct_delivery_quantity": 1,
                    }
                ],
            },
        )
        assert repeated_completion.status_code == 409, repeated_completion.text

        with session_factory() as session:
            assert int(
                session.scalar(select(func.count(ProductionCompletion.id))) or 0
            ) == completion_count


def test_historical_completed_receipt_auto_stays_completed_while_delivery_finds_stock(
    requisition_app,
) -> None:
    from app.api.deliveries import router as deliveries_router
    from app.api.production import router as production_router
    from app.models.production import ProductionTask

    app, session_factory = requisition_app
    app.include_router(deliveries_router, prefix="/api/deliveries")
    app.include_router(production_router, prefix="/api/production")

    with TestClient(app) as client:
        _login(client, "admin")
        _seed_pending_receipt_auto(
            client,
            session_factory,
            ordered_quantity=100,
            received_quantity=62,
        )
        with session_factory() as session:
            task = session.scalar(select(ProductionTask))
            assert task is not None
            task.status = "completed"
            session.commit()
            task_id = int(task.id)

        pending = client.get(
            "/api/production/tasks",
            params={"status": "pending", "page": 1, "page_size": 25},
        )
        assert pending.status_code == 200, pending.text
        assert pending.json()["items"] == []
        assert pending.json()["total"] == 0

        waiting = client.get(
            "/api/production/tasks",
            params={"status": "waiting_material", "page": 1, "page_size": 25},
        )
        assert waiting.status_code == 200, waiting.text
        assert waiting.json()["items"] == []
        assert waiting.json()["total"] == 0

        completed = client.get(
            "/api/production/tasks",
            params={"status": "completed", "page": 1, "page_size": 25},
        )
        assert completed.status_code == 200, completed.text
        assert completed.json()["total"] == 1
        row = completed.json()["items"][0]
        assert row["id"] == task_id
        assert row["status"] == "completed"
        assert row["delivery_ready_quantity"] == 62
        assert row["delivery_actionable"] is True

        delivery = client.get(
            "/api/deliveries/pending-items/search",
            params={"customer_id": 1, "order_item_id": 1},
        )
        assert delivery.status_code == 200, delivery.text
        assert delivery.json()["items"][0]["remaining_quantity"] == 62


def test_partial_then_full_dispatch_uses_current_credit_without_surplus_replay(
    requisition_app,
) -> None:
    from app.api.deliveries import (
        _delivery_remaining_quantity,
        router as deliveries_router,
    )
    from app.api.production import router as production_router
    from app.models.order import OrderItem
    from app.models.production import ProductionCompletion

    app, session_factory = requisition_app
    app.include_router(deliveries_router, prefix="/api/deliveries")
    app.include_router(production_router, prefix="/api/production")

    with TestClient(app) as client:
        _login(client, "admin")
        _seed_pending_receipt_auto(
            client,
            session_factory,
            ordered_quantity=100,
            received_quantity=62,
        )
        created = client.post(
            "/api/deliveries",
            json={
                "customer_id": 1,
                "items": [{"order_item_id": 1, "delivered_quantity": 20}],
            },
        )
        assert created.status_code == 201, created.text
        dispatched = client.put(f"/api/deliveries/{created.json()['id']}/dispatch")
        assert dispatched.status_code == 200, dispatched.text

        with session_factory() as session:
            item = session.get(OrderItem, 1)
            assert item is not None
            assert _delivery_remaining_quantity(session, item) == 42
            completion = session.scalar(
                select(ProductionCompletion).where(
                    ProductionCompletion.order_item_id == 1,
                    ProductionCompletion.origin == "receipt_auto",
                )
            )
            assert completion is not None
            # A completion counter is historical, not a live inventory balance.
            completion.order_reserved_quantity = 45
            completion.surplus_finished_quantity = 17
            session.commit()

        remaining = client.get(
            "/api/deliveries/pending-items/search",
            params={"customer_id": 1, "order_item_id": 1},
        )
        assert remaining.status_code == 200, remaining.text
        assert remaining.json()["items"][0]["remaining_quantity"] == 42

        production = client.get(
            "/api/production/tasks",
            params={"status": "pending", "page": 1, "page_size": 25},
        )
        assert production.status_code == 200, production.text
        assert production.json()["items"] == []
        assert production.json()["total"] == 0

        waiting = client.get(
            "/api/production/tasks",
            params={"status": "waiting_material", "page": 1, "page_size": 25},
        )
        assert waiting.status_code == 200, waiting.text
        waiting_row = waiting.json()["items"][0]
        assert waiting_row["status"] == "waiting_material"
        assert waiting_row["delivery_ready_quantity"] == 42
        assert waiting_row["delivery_actionable"] is True

        final_created = client.post(
            "/api/deliveries",
            json={
                "customer_id": 1,
                "items": [{"order_item_id": 1, "delivered_quantity": 42}],
            },
        )
        assert final_created.status_code == 201, final_created.text
        final_dispatched = client.put(
            f"/api/deliveries/{final_created.json()['id']}/dispatch"
        )
        assert final_dispatched.status_code == 200, final_dispatched.text

        with session_factory() as session:
            item = session.get(OrderItem, 1)
            assert item is not None
            assert _delivery_remaining_quantity(session, item) == 0

        pending = client.get(
            "/api/deliveries/pending-items/search",
            params={"customer_id": 1, "order_item_id": 1},
        )
        assert pending.status_code == 200, pending.text
        assert pending.json()["items"] == []

        production = client.get(
            "/api/production/tasks",
            params={"status": "pending", "page": 1, "page_size": 25},
        )
        assert production.status_code == 200, production.text
        assert production.json()["items"] == []
        assert production.json()["total"] == 0

        waiting = client.get(
            "/api/production/tasks",
            params={"status": "waiting_material", "page": 1, "page_size": 25},
        )
        assert waiting.status_code == 200, waiting.text
        assert waiting.json()["total"] == 1
        waiting_row = waiting.json()["items"][0]
        assert waiting_row["order_item_id"] == 1
        assert waiting_row["status"] == "waiting_material"
        assert waiting_row["delivery_ready_quantity"] == 0
        assert waiting_row["delivery_actionable"] is False

        cancelled = client.put(
            f"/api/deliveries/{final_created.json()['id']}/cancel"
        )
        assert cancelled.status_code == 200, cancelled.text

        restored_pending = client.get(
            "/api/production/tasks",
            params={"status": "pending", "page": 1, "page_size": 25},
        )
        assert restored_pending.status_code == 200, restored_pending.text
        assert restored_pending.json()["items"] == []
        assert restored_pending.json()["total"] == 0

        waiting_after_cancel = client.get(
            "/api/production/tasks",
            params={"status": "waiting_material", "page": 1, "page_size": 25},
        )
        assert waiting_after_cancel.status_code == 200, waiting_after_cancel.text
        assert waiting_after_cancel.json()["total"] == 1
        restored_row = waiting_after_cancel.json()["items"][0]
        assert restored_row["order_item_id"] == 1
        assert restored_row["status"] == "waiting_material"
        assert restored_row["delivery_ready_quantity"] == 42
        assert restored_row["delivery_actionable"] is True


def test_receipt_auto_order_path_caps_credit_at_current_order_remainder(
    requisition_app,
) -> None:
    from app.api.deliveries import (
        _delivery_remaining_quantity,
        router as deliveries_router,
    )
    from app.api.production import router as production_router
    from app.models.order import OrderItem

    app, session_factory = requisition_app
    app.include_router(deliveries_router, prefix="/api/deliveries")
    app.include_router(production_router, prefix="/api/production")

    with TestClient(app) as client:
        _login(client, "admin")
        _seed_pending_receipt_auto(
            client,
            session_factory,
            ordered_quantity=100,
            received_quantity=62,
        )
        # Delivered quantity can be reconciled independently from reservation
        # history.  The order path must still never propose an over-delivery.
        with session_factory() as session:
            item = session.get(OrderItem, 1)
            assert item is not None
            item.delivered_quantity = 50
            session.commit()
        with session_factory() as session:
            item = session.get(OrderItem, 1)
            assert item is not None
            assert _delivery_remaining_quantity(session, item) == 50

        pending = client.get(
            "/api/deliveries/pending-items/search",
            params={"customer_id": 1, "order_item_id": 1},
        )
        assert pending.status_code == 200, pending.text
        assert pending.json()["items"][0]["remaining_quantity"] == 50

        production = client.get(
            "/api/production/tasks",
            params={"status": "pending", "page": 1, "page_size": 25},
        )
        assert production.status_code == 200, production.text
        assert production.json()["items"] == []
        assert production.json()["total"] == 0

        waiting = client.get(
            "/api/production/tasks",
            params={"status": "waiting_material", "page": 1, "page_size": 25},
        )
        assert waiting.status_code == 200, waiting.text
        waiting_row = waiting.json()["items"][0]
        assert waiting_row["status"] == "waiting_material"
        assert waiting_row["delivery_ready_quantity"] == 50
        assert waiting_row["delivery_actionable"] is True


def test_receipt_auto_delivery_keeps_terminal_and_permission_gates(
    requisition_app,
) -> None:
    from app.api.deliveries import router as deliveries_router
    from app.models.order import Order, OrderItem

    app, session_factory = requisition_app
    app.include_router(deliveries_router, prefix="/api/deliveries")

    with TestClient(app) as client:
        _login(client, "admin")
        _seed_pending_receipt_auto(
            client,
            session_factory,
            ordered_quantity=100,
            received_quantity=62,
        )
        with session_factory() as session:
            item = session.get(OrderItem, 1)
            assert item is not None
            order = session.get(Order, item.order_id)
            assert order is not None
            order.status = "closed"
            session.commit()

        pending = client.get(
            "/api/deliveries/pending-items/search",
            params={"customer_id": 1, "order_item_id": 1},
        )
        assert pending.status_code == 200, pending.text
        assert pending.json()["items"] == []
        blocked = client.post(
            "/api/deliveries",
            json={
                "customer_id": 1,
                "items": [{"order_item_id": 1, "delivered_quantity": 1}],
            },
        )
        assert blocked.status_code == 409, blocked.text

        _login(client, "workshop")
        denied = client.get(
            "/api/deliveries/pending-items/search",
            params={"customer_id": 1, "order_item_id": 1},
        )
        assert denied.status_code == 403, denied.text


def test_delivery_pick_map_uses_central_floor3_employee_area_name(
    pick_app,
    monkeypatch,
) -> None:
    from app.services.production_workflow import list_temporary_locations

    app, factory, ids, _operation_log = pick_app
    location_id, _lot_id = _add_measured_pick_location(
        pick_app,
        monkeypatch,
        floor=3,
        area_code="A1",
        location_code="F3-A1-P102",
    )
    with factory() as db:
        production_location = next(
            row
            for row in list_temporary_locations(db)
            if int(row["id"]) == location_id
        )
        assert production_location["area_name"] == "右区A1"

    with TestClient(app) as client:
        _pick_login(client, "admin")
        task = _create_pick_task(client, ids["delivery"])
        floors = client.get(
            f"/api/delivery-picks/{task['id']}/measured-map/floors"
        )
        assert floors.status_code == 200, floors.text
        floor_area = next(
            area
            for floor in floors.json()["floors"]
            for area in floor["areas"]
            if floor["floor_code"] == "3F" and area["area_code"] == "A1"
        )
        assert floor_area["area_name"] == "右区A1"

        area = client.get(
            f"/api/delivery-picks/{task['id']}/measured-map/floors/3F",
            params={"area_code": "A1"},
        )
        assert area.status_code == 200, area.text
        assert area.json()["area_name"] == "右区A1"
