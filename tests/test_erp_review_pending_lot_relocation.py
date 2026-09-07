from __future__ import annotations

import json
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.models.audit import OperationLog
from app.models.user import User
from app.models.warehouse_inventory import (
    InventoryLot, InventoryMovement, InventoryPallet, InventoryPalletItem,
    InventoryReservation, WarehouseGroundOccupancy, WarehouseGroundOccupancySlot,
    WarehouseLocation,
)
from app.services.warehouse_relocation_pending import LOT_ACTION, is_pending_relocation_location
from test_p1_47c_warehouse_move_mode import _finished_lot, _login, move_batch_app


def _url(lot_id):
    return f"/api/warehouse/twin-operations/lots/{lot_id}/pending-relocation"


def _payload(ids, **changes):
    return dict(source_location_id=ids["floor1_source"], expected_version=1,
                expected_layout_version=1, expected_pallet_id=ids["normal_pallet"],
                expected_pallet_version=1, idempotency_key="single-pending-test", confirmed=True) | changes


def _facts(row, excluded=()):
    return {column.key: getattr(row, column.key) for column in row.__table__.columns
            if column.key not in excluded}


def _attach_occupancy(db, ids):
    lot = db.get(InventoryLot, ids["normal_lot"])
    actor = db.scalar(select(User).where(User.username == "p147c-admin"))
    occupancy = WarehouseGroundOccupancy(pallet_id=ids["normal_pallet"],
        primary_location_id=ids["floor1_source"], customer_id=lot.finished_detail.owner_customer_id,
        product_id=lot.finished_detail.product_id, footprint_kind="single", capacity_quantity=100,
        status="active", version=1, created_by=actor.id)
    occupancy.slots = [WarehouseGroundOccupancySlot(location_id=ids["floor1_source"],
        slot_sequence=1, status="active")]
    db.add(occupancy)
    db.flush()
    return occupancy.id


@pytest.mark.parametrize("container", ["whole", "mixed", "mixed_empty", "loose"])
def test_single_lot_preserves_all_stock_identity_and_other_pallet_items(move_batch_app, container):
    app, factory, ids, _ = move_batch_app
    with factory() as db:
        target = db.get(InventoryLot, ids["normal_lot"])
        target.quantity_available, target.quantity_reserved, target.quantity_damaged = 15, 5, 2
        target.quantity_consumed, target.quantity_scrapped = 7, 1
        target.pallet_item.quantity = Decimal(22)
        item_id = target.pallet_item.id
        if container.startswith("mixed"):
            detail = target.finished_detail
            other = _finished_lot(number="S07-OTHER", location=db.get(WarehouseLocation, ids["floor1_source"]),
                customer=target.pallet_item.customer, product=target.pallet_item.product,
                available=0 if container == "mixed_empty" else 4, reserved=0, source_ref_id=707)
            db.add(other)
            db.flush()
            db.add(InventoryPalletItem(pallet_id=ids["normal_pallet"], inventory_lot_id=other.id,
                customer_id=detail.owner_customer_id, product_id=detail.product_id, inventory_code="S07-OTHER",
                product_name="同板另一批虚构货物", item_type="finished", quantity=4, unit="boxes", match_status="matched"))
        occupancy_id = _attach_occupancy(db, ids) if container != "loose" else None
        if container == "loose":
            db.delete(target.pallet_item)
            pallet = db.get(InventoryPallet, ids["normal_pallet"])
            pallet.is_current, pallet.status, pallet.location_id = False, "closed", None
        db.commit()
        before_lots = {lot.id: _facts(lot) for lot in db.scalars(select(InventoryLot))}
        before_details = {lot.id: _facts(lot.finished_detail) for lot in db.scalars(select(InventoryLot))}
        before_items = {item.id: _facts(item) for item in db.scalars(select(InventoryPalletItem))}
        before_reservations = [_facts(row) for row in db.scalars(select(InventoryReservation))]
    payload = _payload(ids, **({"expected_pallet_id": None, "expected_pallet_version": None} if container == "loose" else {}))
    with TestClient(app) as client:
        _login(client, "p147c-admin")
        response = client.post(_url(ids["normal_lot"]), json=payload)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["quantity"] == 22 and result["lot_id"] == ids["normal_lot"]
    with factory() as db:
        assert is_pending_relocation_location(db.get(WarehouseLocation, result["pending_location_id"]))
        for lot in db.scalars(select(InventoryLot)):
            expected = before_lots[lot.id].copy()
            if lot.id == ids["normal_lot"]:
                expected.update(warehouse_location_id=result["pending_location_id"], version=2,
                                last_movement_at=lot.last_movement_at, updated_at=lot.updated_at)
            assert _facts(lot) == expected
            assert _facts(lot.finished_detail) == before_details[lot.id]
        assert [_facts(row) for row in db.scalars(select(InventoryReservation))] == before_reservations
        for item in db.scalars(select(InventoryPalletItem)):
            expected = before_items[item.id].copy()
            if item.id == item_id and container.startswith("mixed"):
                expected.update(pallet_id=result["pallet_id"], updated_at=item.updated_at)
            assert _facts(item) == expected
        if occupancy_id:
            occupancy = db.get(WarehouseGroundOccupancy, occupancy_id)
            expected_status = "active" if container == "mixed" else "released"
            assert occupancy.status == expected_status
            assert all(slot.status == expected_status for slot in occupancy.slots)
            source_pallet = db.get(InventoryPallet, ids["normal_pallet"])
            expected_location = (ids["floor1_source"] if container == "mixed" else
                                 None if container == "mixed_empty" else result["pending_location_id"])
            assert source_pallet.location_id == expected_location
        movement = db.scalar(select(InventoryMovement).where(InventoryMovement.inventory_lot_id == ids["normal_lot"]))
        assert movement.quantity == 22 and movement.unit == "boxes"
        for name in ("available", "reserved", "damaged", "consumed", "scrapped"):
            assert getattr(movement, "before_" + name) == getattr(movement, "after_" + name)
        audit = db.scalar(select(OperationLog).where(OperationLog.action_code == LOT_ACTION))
        assert json.loads(audit.details)["result"]["lot_id"] == ids["normal_lot"]


def test_single_lot_replay_conflicts_and_actor_scope(move_batch_app):
    app, factory, ids, _ = move_batch_app
    with TestClient(app) as client:
        _login(client, "p147c-admin")
        first = client.post(_url(ids["normal_lot"]), json=_payload(ids))
        assert first.status_code == 200, first.text
        replay = client.post(_url(ids["normal_lot"]), json=_payload(ids))
        assert replay.status_code == 200 and replay.json()["replayed"] is True
        assert replay.json() | {"replayed": False} == first.json()
        assert client.post(_url(ids["normal_lot"]), json=_payload(ids, expected_version=2)).status_code == 409
        assert client.post(_url(ids["system_lot"]), json=_payload(ids)).status_code == 409
        # A different administrator must not receive the old actor's replay result.
        with factory() as db:
            operator = db.scalar(select(User).where(User.username == "p147c-operator"))
            operator.role = "admin"
            db.commit()
        _login(client, "p147c-operator")
        assert client.post(_url(ids["normal_lot"]), json=_payload(ids)).status_code == 409
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(InventoryMovement)) == 1
        assert db.scalar(select(func.count()).select_from(OperationLog).where(OperationLog.action_code == LOT_ACTION)) == 1


def test_single_lot_permission_confirmation_and_stale_snapshots_are_zero_write(move_batch_app):
    app, factory, ids, _ = move_batch_app
    with TestClient(app) as client:
        for username in ("p147c-viewer", "p147c-operator"):
            _login(client, username)
            assert client.post(_url(ids["normal_lot"]), json=_payload(ids)).status_code == 403
        _login(client, "p147c-admin")
        assert client.post(_url(ids["normal_lot"]), json=_payload(ids, confirmed=False)).status_code == 422
        for changes in ({"expected_version": 2}, {"expected_layout_version": 2},
                        {"expected_pallet_version": 2}, {"expected_pallet_id": None},
                        {"source_location_id": ids["floor1_target"]}):
            response = client.post(_url(ids["normal_lot"]), json=_payload(ids, **changes))
            assert response.status_code == 409, response.text
    with factory() as db:
        assert db.get(InventoryLot, ids["normal_lot"]).warehouse_location_id == ids["floor1_source"]
        assert db.get(InventoryPallet, ids["normal_pallet"]).version == 1
        assert db.scalar(select(func.count()).select_from(InventoryMovement)) == 0
        assert db.scalar(select(func.count()).select_from(WarehouseLocation).where(WarehouseLocation.location_code == "RECOUNT-PENDING")) == 0


def test_single_lot_audit_failure_rolls_back_mapping_location_and_occupancy(move_batch_app, monkeypatch):
    app, factory, ids, _ = move_batch_app
    with factory() as db:
        occupancy_id = _attach_occupancy(db, ids)
        db.commit()
    def reject_audit(*args, **kwargs):
        raise RuntimeError("isolated audit failure")
    monkeypatch.setattr("app.services.warehouse_relocation_pending.append_audit_event", reject_audit)
    with TestClient(app, raise_server_exceptions=False) as client:
        _login(client, "p147c-admin")
        assert client.post(_url(ids["normal_lot"]), json=_payload(ids)).status_code == 500
    with factory() as db:
        assert db.get(InventoryLot, ids["normal_lot"]).warehouse_location_id == ids["floor1_source"]
        assert db.get(InventoryPallet, ids["normal_pallet"]).location_id == ids["floor1_source"]
        assert db.get(InventoryPallet, ids["normal_pallet"]).version == 1
        assert db.get(WarehouseGroundOccupancy, occupancy_id).status == "active"
        assert db.scalar(select(func.count()).select_from(InventoryMovement)) == 0
