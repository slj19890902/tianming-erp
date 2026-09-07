from __future__ import annotations

import json

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models.access_control import UserCustomerScope, UserPermissionOverride
from app.models.audit import OperationLog
from app.models.stocktake import StocktakeOrder
from app.models.user import User
from app.models.warehouse_inventory import (
    FinishedGoodsInventoryDetail, InventoryLot, InventoryMovement, InventoryPallet,
    InventoryPalletItem, InventoryReservation, WarehouseGroundOccupancy, WarehouseGroundOccupancySlot,
)
from app.services.stocktake import _location_identity_snapshot
from test_p1_47d_inventory_adjustment import _login, stocktake_app


ACTION = "warehouse.stocktake_match.confirmed"


def _url(lot_id):
    return f"/api/warehouse/twin-operations/lots/{lot_id}/stocktake-match"


def _payload(db, lot_id, key="s07-count-match"):
    lot = db.get(InventoryLot, lot_id)
    address, _, revision = _location_identity_snapshot(db, lot.location)
    return dict(location_id=lot.warehouse_location_id, expected_version=lot.version,
        expected_layout_version=lot.location.floor3_layout.version, expected_address_version=address,
        expected_map_revision=revision, expected_available=lot.quantity_available,
        expected_reserved=lot.quantity_reserved, expected_damaged=lot.quantity_damaged,
        expected_unit=lot.unit, idempotency_key=key, confirmed=True)


def _stock_facts(db):
    return {model.__tablename__: [dict((c.key, getattr(row, c.key)) for c in model.__table__.columns)
        for row in db.scalars(select(model).order_by(*model.__table__.primary_key.columns))] for model in (
            InventoryLot, FinishedGoodsInventoryDetail, InventoryPallet, InventoryPalletItem,
            InventoryReservation, InventoryMovement, WarehouseGroundOccupancy, WarehouseGroundOccupancySlot)}


def test_match_records_only_selected_batch_with_reserved_and_damaged_breakdown(stocktake_app):
    app, factory, ids, _ = stocktake_app
    with factory() as db:
        db.get(InventoryLot, ids["lot_reserved"]).quantity_damaged = 1
        db.commit()
        payloads = {key: _payload(db, ids["lot_" + key], key="match-" + key)
                    for key in ("normal", "reserved", "to_zero")}
        before = _stock_facts(db)
    with TestClient(app) as client:
        _login(client)
        for key, payload in payloads.items():
            response = client.post(_url(ids["lot_" + key]), json=payload)
            assert response.status_code == 200, response.text
            fact = response.json()["items"][0]
            expected = payload["expected_available"] + payload["expected_reserved"] + payload["expected_damaged"]
            assert fact["lot_id"] == ids["lot_" + key]
            assert fact["book_quantity"] == fact["counted_quantity"] == expected
            assert fact["difference_quantity"] == 0
            assert fact["reserved_quantity"] == payload["expected_reserved"]
            assert fact["damaged_quantity"] == payload["expected_damaged"]
    with factory() as db:
        assert _stock_facts(db) == before
        assert list(db.scalars(select(StocktakeOrder))) == []
        audits = list(db.scalars(select(OperationLog).where(OperationLog.action_code == ACTION)))
        assert len(audits) == 3
        for audit in audits:
            fact = json.loads(audit.details)["result"]
            assert fact["operator_id"] == ids["operator"] and fact["confirmed_at"]
            assert fact["items"][0]["expected_layout_version"] == 1
            assert fact["items"][0]["expected_version"] == 1


def test_match_exact_replay_conflict_customer_scope_and_revoked_permission(stocktake_app):
    app, factory, ids, _ = stocktake_app
    with factory() as db:
        payload = _payload(db, ids["lot_normal"])
    with TestClient(app) as client:
        _login(client)
        first = client.post(_url(ids["lot_normal"]), json=payload)
        assert first.status_code == 200, first.text
        replay = client.post(_url(ids["lot_normal"]), json=payload)
        assert replay.status_code == 200 and replay.json()["idempotent_replay"] is True
        assert replay.json() | {"idempotent_replay": False} == first.json()
        assert client.post(_url(ids["lot_normal"]), json=payload | {"expected_available": 9}).status_code == 409
        _login(client, "p147d-other")
        assert client.post(_url(ids["lot_normal"]), json=payload).status_code == 409
        _login(client)
        with factory() as db:
            db.get(User, ids["operator"]).customer_access_mode = "selected"
            db.add(UserCustomerScope(user_id=ids["operator"], customer_id=ids["other_customer"], assigned_by=ids["admin"]))
            db.commit()
        assert client.post(_url(ids["lot_normal"]), json=payload).status_code == 403
        with factory() as db:
            db.add(UserPermissionOverride(user_id=ids["operator"], permission_code="warehouse.stocktake.submit", is_allowed=False, granted_by=ids["admin"]))
            db.commit()
        assert client.post(_url(ids["lot_normal"]), json=payload).status_code == 403
    with factory() as db:
        assert len(list(db.scalars(select(OperationLog).where(OperationLog.action_code == ACTION)))) == 1


def test_match_rejects_stale_snapshot_and_open_stocktake_without_fact(stocktake_app):
    app, factory, ids, _ = stocktake_app
    with factory() as db:
        payload = _payload(db, ids["lot_normal"])
        before = _stock_facts(db)
    with TestClient(app) as client:
        _login(client)
        for change in ({"expected_version": 2}, {"expected_layout_version": 2},
                       {"expected_address_version": 2}, {"expected_map_revision": "stale-map"},
                       {"expected_available": 9}, {"expected_reserved": 1}, {"expected_damaged": 1},
                       {"location_id": ids["loc_fg1_add"]}):
            response = client.post(_url(ids["lot_normal"]), json=payload | change)
            assert response.status_code == 409, response.text
        assert client.post(_url(ids["lot_normal"]), json=payload | {"confirmed": False}).status_code == 422
        with factory() as db:
            db.add(StocktakeOrder(order_number="S07-PENDING", location_id=ids["loc_fg1"],
                status="submitted", version=1, submitted_by=ids["operator"], idempotency_key="s07-existing-pending"))
            db.commit()
        blocked = client.post(_url(ids["lot_normal"]), json=payload)
        assert blocked.status_code == 409 and "盘点" in blocked.text
    with factory() as db:
        assert _stock_facts(db) == before
        assert list(db.scalars(select(OperationLog).where(OperationLog.action_code == ACTION))) == []


def test_match_audit_failure_rolls_back_without_inventory_changes(stocktake_app, monkeypatch):
    app, factory, ids, _ = stocktake_app
    with factory() as db:
        payload = _payload(db, ids["lot_normal"])
        before = _stock_facts(db)
    def reject(*args, **kwargs):
        raise RuntimeError("isolated match audit failure")
    monkeypatch.setattr("app.services.warehouse_stocktake_batch.append_audit_event", reject)
    with TestClient(app, raise_server_exceptions=False) as client:
        _login(client)
        assert client.post(_url(ids["lot_normal"]), json=payload).status_code == 500
    with factory() as db:
        assert _stock_facts(db) == before
        assert list(db.scalars(select(OperationLog).where(OperationLog.action_code == ACTION))) == []
