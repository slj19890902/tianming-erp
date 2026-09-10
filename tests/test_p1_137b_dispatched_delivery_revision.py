from __future__ import annotations

from datetime import date

from sqlalchemy import select
from fastapi.testclient import TestClient

from tests.test_phase7_deliveries import (
    _create_payload,
    _login,
    _seed_historical_finished_delivery_inventory,
    delivery_api_app,
)
from tests.test_p1_15b_unordered_finished_delivery import (
    _create_unordered_delivery,
    _login as _login_unordered,
    _mixed_delivery_fixture,
    _mixed_payload,
    _seed as _seed_unordered,
    _unordered_payload,
    unordered_finished_delivery_app,
)


def _revision_payload(version: int, *, key: str = "p1-137b-revision-001") -> dict:
    return {
        "source_mode": "order",
        "expected_version": version,
        "idempotency_key": key,
        "vehicle_number": "苏E·54321",
        "items": [
            {"order_item_id": 1, "delivered_quantity": 25, "remarks": "修订一"},
            {"order_item_id": 2, "delivered_quantity": 10, "remarks": "修订二"},
        ],
    }


def test_dispatched_delivery_revision_is_atomic_traceable_and_idempotent(
    delivery_api_app,
) -> None:
    from app.models.delivery import Delivery, DeliveryItem
    from app.models.audit import OperationLog
    from app.models.order import OrderItem
    from app.models.warehouse_inventory import DeliveryInventoryAllocation

    app, session_factory = delivery_api_app
    _seed_historical_finished_delivery_inventory(session_factory, 1, 2)
    with TestClient(app) as client:
        _login(client, "admin")
        created = client.post("/api/deliveries", json=_create_payload())
        assert created.status_code == 201, created.text
        delivery_id = created.json()["id"]
        dispatched = client.put(f"/api/deliveries/{delivery_id}/dispatch")
        assert dispatched.status_code == 200, dispatched.text

        payload = _revision_payload(dispatched.json()["version"])
        revised = client.put(
            f"/api/deliveries/{delivery_id}/revision",
            json=payload,
        )
        replay = client.put(
            f"/api/deliveries/{delivery_id}/revision",
            json=payload,
        )
        stale = client.put(
            f"/api/deliveries/{delivery_id}/revision",
            json=_revision_payload(
                dispatched.json()["version"],
                key="p1-137b-revision-stale",
            ),
        )

    assert revised.status_code == 200, revised.text
    assert revised.json()["id"] == delivery_id
    assert revised.json()["delivery_number"] == created.json()["delivery_number"]
    assert revised.json()["status"] == "dispatched"
    assert revised.json()["total_quantity"] == 35
    assert revised.json()["version"] == dispatched.json()["version"] + 1
    assert revised.json()["reprint_required"] is True
    assert replay.status_code == 200, replay.text
    assert replay.json() == revised.json()
    assert stale.status_code == 409

    with session_factory() as session:
        delivery = session.get(Delivery, delivery_id)
        assert delivery is not None
        assert delivery.status == "dispatched"
        assert delivery.printed_at is None
        assert session.get(OrderItem, 1).delivered_quantity == 45
        assert session.get(OrderItem, 2).delivered_quantity == 10

        rows = session.scalars(
            select(DeliveryItem)
            .where(DeliveryItem.delivery_id == delivery_id)
            .order_by(DeliveryItem.id)
        ).all()
        assert [(row.is_current, row.revision_number) for row in rows] == [
            (False, 1),
            (False, 1),
            (True, 2),
            (True, 2),
        ]
        assert [row.delivered_quantity for row in rows if row.is_current] == [25, 10]
        allocation_rows = session.scalars(
            select(DeliveryInventoryAllocation)
            .where(
                DeliveryInventoryAllocation.delivery_item_id.in_(
                    [row.id for row in rows]
                )
            )
            .order_by(DeliveryInventoryAllocation.id)
        ).all()
        assert any(row.status == "reversed" for row in allocation_rows)
        assert any(row.status in {"active", "consumed"} for row in allocation_rows)
        audit = session.scalar(
            select(OperationLog).where(
                OperationLog.resource == "Delivery",
                OperationLog.entity_id == delivery_id,
                OperationLog.action == "REVISE_DISPATCHED_DELIVERY",
            )
        )
        assert audit is not None
        assert '"reprint_required": true' in audit.details


def test_revision_failure_rolls_back_original_dispatch(
    delivery_api_app,
) -> None:
    from app.models.delivery import Delivery, DeliveryItem
    from app.models.order import OrderItem

    app, session_factory = delivery_api_app
    _seed_historical_finished_delivery_inventory(session_factory, 1, 2)
    with TestClient(app) as client:
        _login(client, "admin")
        created = client.post("/api/deliveries", json=_create_payload())
        delivery_id = created.json()["id"]
        dispatched = client.put(f"/api/deliveries/{delivery_id}/dispatch")
        payload = _revision_payload(
            dispatched.json()["version"],
            key="p1-137b-revision-invalid",
        )
        payload["items"][0]["delivered_quantity"] = 9999
        failed = client.put(
            f"/api/deliveries/{delivery_id}/revision",
            json=payload,
        )

    assert failed.status_code == 409, failed.text
    with session_factory() as session:
        delivery = session.get(Delivery, delivery_id)
        assert delivery is not None
        assert delivery.status == "dispatched"
        assert delivery.total_quantity == 70
        assert session.get(OrderItem, 1).delivered_quantity == 50
        assert session.get(OrderItem, 2).delivered_quantity == 40
        rows = session.scalars(
            select(DeliveryItem).where(DeliveryItem.delivery_id == delivery_id)
        ).all()
        assert len(rows) == 2
        assert all(row.is_current for row in rows)


def test_effective_receipt_blocks_dispatched_delivery_revision(
    delivery_api_app,
) -> None:
    from app.models.finance import ReturnReceipt

    app, session_factory = delivery_api_app
    _seed_historical_finished_delivery_inventory(session_factory, 1, 2)
    with TestClient(app) as client:
        _login(client, "admin")
        created = client.post("/api/deliveries", json=_create_payload())
        delivery_id = created.json()["id"]
        dispatched = client.put(f"/api/deliveries/{delivery_id}/dispatch")
        with session_factory() as session:
            session.add(
                ReturnReceipt(
                    delivery_id=delivery_id,
                    status="confirmed",
                    actual_received_date=date.fromisoformat(
                        created.json()["delivery_date"]
                    ),
                    created_by=1,
                )
            )
            session.commit()
        blocked = client.put(
            f"/api/deliveries/{delivery_id}/revision",
            json=_revision_payload(
                dispatched.json()["version"],
                key="p1-137b-revision-receipt",
            ),
        )

    assert blocked.status_code == 409
    assert "回单" in str(blocked.json())


def test_unordered_finished_revision_restores_then_consumes_exact_new_quantity(
    unordered_finished_delivery_app,
) -> None:
    from app.models.delivery import Delivery, DeliveryItem
    from app.models.warehouse_inventory import (
        InventoryLot,
        UnorderedFinishedDeliveryAllocation,
    )

    app, factory = unordered_finished_delivery_app
    seed = _seed_unordered(app, factory)
    with TestClient(app) as client:
        _login_unordered(client)
        created = _create_unordered_delivery(
            client,
            _unordered_payload(seed, quantity=10),
        )
        delivery_id = created["id"]
        dispatched = client.put(f"/api/deliveries/{delivery_id}/dispatch")
        assert dispatched.status_code == 200, dispatched.text
        revised = client.put(
            f"/api/deliveries/{delivery_id}/revision",
            json={
                "source_mode": "unordered_finished",
                "expected_version": dispatched.json()["version"],
                "idempotency_key": "p1-137b-unordered-revision",
                "items": [
                    {
                        "source_type": "unordered_finished",
                        "product_id": seed.priced_product_id,
                        "delivered_quantity": 6,
                        "unit_price": "3.60",
                        "allocations": [
                            {
                                "inventory_lot_id": seed.free_first_lot_id,
                                "quantity": 6,
                            }
                        ],
                    }
                ],
            },
        )

    assert revised.status_code == 200, revised.text
    assert revised.json()["id"] == delivery_id
    assert revised.json()["status"] == "dispatched"
    assert revised.json()["total_quantity"] == 6
    with factory() as session:
        delivery = session.get(Delivery, delivery_id)
        assert delivery is not None and delivery.status == "dispatched"
        rows = session.scalars(
            select(DeliveryItem)
            .where(DeliveryItem.delivery_id == delivery_id)
            .order_by(DeliveryItem.id)
        ).all()
        assert len(rows) == 2
        assert [(row.is_current, row.revision_number) for row in rows] == [
            (False, 1),
            (True, 2),
        ]
        allocations = session.scalars(
            select(UnorderedFinishedDeliveryAllocation)
            .where(
                UnorderedFinishedDeliveryAllocation.delivery_item_id.in_(
                    [row.id for row in rows]
                )
            )
            .order_by(UnorderedFinishedDeliveryAllocation.id)
        ).all()
        assert any(row.status == "restored" for row in allocations)
        assert any(
            row.status == "dispatched" and row.consumed_quantity == 6
            for row in allocations
        )
        first_lot = session.get(InventoryLot, seed.free_first_lot_id)
        second_lot = session.get(InventoryLot, seed.free_second_lot_id)
        assert first_lot is not None and second_lot is not None
        assert first_lot.quantity_consumed == 6
        assert second_lot.quantity_consumed == 0


def test_mixed_delivery_revision_keeps_order_and_unordered_branches_atomic(
    unordered_finished_delivery_app,
) -> None:
    from app.models.delivery import DeliveryItem
    from app.models.order import OrderItem

    app, factory = unordered_finished_delivery_app
    seed = _seed_unordered(app, factory)
    with TestClient(app) as client:
        _login_unordered(client)
        _order_id, order_item_id, _order_lot_id = _mixed_delivery_fixture(
            client,
            factory,
            seed,
        )
        created = client.post(
            "/api/deliveries",
            json=_mixed_payload(
                seed,
                order_item_id=order_item_id,
                order_quantity=10,
                unordered_quantity=3,
            ),
        )
        assert created.status_code == 201, created.text
        delivery_id = created.json()["id"]
        dispatched = client.put(f"/api/deliveries/{delivery_id}/dispatch")
        assert dispatched.status_code == 200, dispatched.text
        revised = client.put(
            f"/api/deliveries/{delivery_id}/revision",
            json={
                **_mixed_payload(
                    seed,
                    order_item_id=order_item_id,
                    order_quantity=10,
                    unordered_quantity=2,
                ),
                "expected_version": dispatched.json()["version"],
                "idempotency_key": "p1-137b-mixed-revision",
            },
        )

    assert revised.status_code == 200, revised.text
    assert revised.json()["status"] == "dispatched"
    assert revised.json()["source_mode"] == "mixed"
    assert revised.json()["total_quantity"] == 12
    with factory() as session:
        assert session.get(OrderItem, order_item_id).delivered_quantity == 10
        rows = session.scalars(
            select(DeliveryItem)
            .where(DeliveryItem.delivery_id == delivery_id)
            .order_by(DeliveryItem.id)
        ).all()
        assert len([row for row in rows if row.is_current]) == 2
        assert len([row for row in rows if not row.is_current]) == 2


def test_remove_unordered_line_from_dispatched_mixed_delivery_updates_print_and_stock(unordered_finished_delivery_app):
    from app.models.warehouse_inventory import InventoryLot
    from app.models.order import OrderItem
    app, factory = unordered_finished_delivery_app
    seed = _seed_unordered(app, factory)
    with TestClient(app) as client:
        _login_unordered(client)
        _, order_item_id, _ = _mixed_delivery_fixture(client, factory, seed)
        created = client.post("/api/deliveries", json=_mixed_payload(seed, order_item_id=order_item_id, order_quantity=10, unordered_quantity=3))
        assert created.status_code == 201, created.text
        delivery_id = created.json()["id"]
        dispatched = client.put(f"/api/deliveries/{delivery_id}/dispatch")
        assert dispatched.status_code == 200, dispatched.text
        payload = {
            "source_mode":"order", "expected_version":dispatched.json()["version"],
            "idempotency_key":"remove-one-mixed-delivery-line",
            "items":[{"order_item_id":order_item_id,"delivered_quantity":10}],
        }
        revised = client.put(f"/api/deliveries/{delivery_id}/revision", json=payload)
        assert revised.status_code == 200, revised.text
        assert len(revised.json()["items"]) == 1
        assert revised.json()["total_quantity"] == 10
        replay = client.put(f"/api/deliveries/{delivery_id}/revision", json=payload)
        assert replay.json() == revised.json()
        preview = client.get(f"/api/deliveries/{delivery_id}/print")
        assert preview.status_code == 200, preview.text
        assert len(preview.json()["items"]) == 1
    with factory() as db:
        assert db.get(InventoryLot, seed.free_first_lot_id).quantity_available == 12
        assert db.get(OrderItem, order_item_id).delivered_quantity == 10
