"""Mobile 3.0 pagination and exact discrepancy receipts on synthetic data."""
from __future__ import annotations

from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from test_p1_21b_mobile_admin_product_search import _login, mobile_erp_app
from test_p1_21f3_mobile_warehouse_map import _add_map_target


PRODUCTS = "/api/mobile/erp/products"
REPORTS = "/api/mobile/erp/warehouse/location-discrepancies"


def _seed_products(factory, ids):
    from app.models.product import Product
    from app.models.warehouse_inventory import FinishedGoodsInventoryDetail, InventoryLot

    visible_stocked = []
    with factory() as db:
        customer_id = db.get(Product, ids["product"]).customer_id
        other_customer_id = db.get(Product, ids["other_product"]).customer_id
        for number in range(40):
            owner = customer_id if number < 38 else other_customer_id
            product = Product(
                customer_id=owner,
                product_code=f"MOBILE3-PAGE-{number:03}",
                customer_material_code="M3-PAGE",
                product_name=f"分页产品{number:03}",
            )
            db.add(product)
            db.flush()
            if 35 <= number < 38:
                continue
            lot = InventoryLot(
                lot_number=f"FG-MOBILE3-PAGE-{number:03}",
                inventory_type="finished",
                warehouse_location_id=ids["mapped_location"],
                quantity_available=1,
                unit="boxes",
                status="active",
                source_type="manual",
                stock_date=date(2026, 10, 22),
                last_movement_at=datetime(2026, 10, 22),
            )
            db.add(lot)
            db.flush()
            db.add(FinishedGoodsInventoryDetail(
                inventory_lot_id=lot.id,
                owner_customer_id=owner,
                product_id=product.id,
                inventory_code_snapshot=product.product_code,
                product_name_snapshot=product.product_name,
            ))
            if number < 35:
                visible_stocked.append(product.id)
        db.commit()
    return visible_stocked


def _business_state(factory):
    from app.models.audit import OperationLog
    from app.models.warehouse_inventory import InventoryLot, WarehouseLocationDiscrepancy

    with factory() as db:
        return {
            "lots": list(db.execute(select(
                InventoryLot.id, InventoryLot.warehouse_location_id,
                InventoryLot.version, InventoryLot.quantity_available,
                InventoryLot.quantity_reserved, InventoryLot.quantity_damaged,
            ).order_by(InventoryLot.id))),
            "reports": db.scalar(select(func.count(WarehouseLocationDiscrepancy.id))),
            "report_audits": db.scalar(select(func.count(OperationLog.id)).where(
                OperationLog.action_code == "warehouse.location_discrepancy.report"
            )),
        }


def test_products_paginate_all_scoped_stock_with_stable_ties_and_no_writes(mobile_erp_app):
    app, ids, factory = mobile_erp_app
    expected = _seed_products(factory, ids)
    with TestClient(app) as client:
        _login(client, "mobile-scoped")
        before = _business_state(factory)
        seen = []
        offset = 0
        for expected_count in (12, 12, 11):
            result = client.get(PRODUCTS, params={"q": "MOBILE3-PAGE", "offset": offset})
            assert result.status_code == 200, result.text
            body = result.json()
            assert body["offset"] == offset
            assert body["limit"] == 12
            assert body["total"] == 35
            assert body["matched_product_count"] == 38
            assert body["zero_stock_match_count"] == 3
            assert body["count"] == expected_count
            assert result.headers["cache-control"] == "private, no-store"
            seen.extend(item["product_id"] for item in body["items"])
            offset += expected_count
            assert body["has_more"] is (offset < 35)
            assert body["next_offset"] == (offset if offset < 35 else None)
        assert seen == expected
        repeated = client.get(PRODUCTS, params={"q": "MOBILE3-PAGE", "offset": 12}).json()
        assert [item["product_id"] for item in repeated["items"]] == expected[12:24]
        empty = client.get(PRODUCTS, params={"q": "MOBILE3-PAGE", "offset": 100}).json()
        assert empty["items"] == []
        assert empty["total"] == 35
        assert empty["count"] == 0
        assert empty["has_more"] is False and empty["next_offset"] is None
        assert _business_state(factory) == before


def test_products_pagination_preserves_zero_stock_permission_and_legacy_default(mobile_erp_app):
    app, ids, factory = mobile_erp_app
    _seed_products(factory, ids)
    with TestClient(app) as client:
        _login(client, "mobile-admin")
        default = client.get(PRODUCTS, params={"q": "MOBILE3-PAGE"}).json()
        assert default["offset"] == 0 and default["limit"] == 12
        assert default["total"] == 37 and default["count"] == 12
        assert default["requires_selection"] is True
        assert default["auto_selected"] is False
        all_products = client.get(PRODUCTS, params={
            "q": "MOBILE3-PAGE", "include_zero": True, "offset": 30, "limit": 30,
        }).json()
        assert all_products["total"] == 40 and all_products["count"] == 10
        assert all_products["next_offset"] is None
        assert client.get(PRODUCTS, params={"q": "MOBILE3-PAGE", "offset": -1}).status_code == 422
        _login(client, "mobile-scoped")
        assert client.get(PRODUCTS, params={
            "q": "MOBILE3-PAGE", "include_zero": True, "offset": 30,
        }).status_code == 403
        _login(client, "mobile-picker")
        assert client.get(PRODUCTS, params={"q": "MOBILE3-PAGE", "offset": 30}).status_code == 403


def _report_request(factory):
    lot_id, target_id = _add_map_target(factory)
    return {
        "inventory_lot_id": lot_id,
        "expected_lot_version": 1,
        "reported_quantity": 10,
        "observed_location_id": target_id,
        "observed_location_layout_version": 1,
        "reason": "现场货物与系统登记位置不一致",
        "idempotency_key": "mobile3-discrepancy-receipt",
    }


@pytest.mark.parametrize("drift", ["lot_version", "closed", "zero", "layout", "resolved"])
def test_exact_report_replays_after_current_state_drift_without_claim_or_writes(mobile_erp_app, monkeypatch, drift):
    from app.api import mobile_erp
    from app.models.warehouse_inventory import InventoryLot, WarehouseLocation, WarehouseLocationDiscrepancy

    app, _ids, factory = mobile_erp_app
    payload = _report_request(factory)
    with TestClient(app) as client:
        _login(client, "mobile-scoped")
        original = client.post(REPORTS, json=payload)
        assert original.status_code == 201, original.text
        report_id = original.json()["report"]["id"]
        with factory() as db:
            lot = db.get(InventoryLot, payload["inventory_lot_id"])
            if drift == "lot_version":
                lot.version += 1
            elif drift == "closed":
                lot.status = "closed"
            elif drift == "zero":
                lot.quantity_available = lot.quantity_reserved = 0
            elif drift == "layout":
                db.get(WarehouseLocation, payload["observed_location_id"]).floor3_layout.version += 1
            else:
                lot.warehouse_location_id = payload["observed_location_id"]
                lot.version += 1
                report = db.get(WarehouseLocationDiscrepancy, report_id)
                report.status = "resolved"
                report.version += 1
                report.resolution_note = "已核对"
            db.commit()
        before = _business_state(factory)
        def no_claim(*args, **kwargs):
            raise AssertionError("Replaying an existing report must not claim a warehouse floor")
        monkeypatch.setattr(mobile_erp, "_claim_mobile_warehouse_floors", no_claim)
        replay = client.post(REPORTS, json=payload)
        assert replay.status_code == 201, replay.text
        body = replay.json()
        assert body["idempotent_replay"] is True
        assert body["report"]["id"] == report_id
        assert body["report"]["reason"] == payload["reason"]
        assert body["report"]["observed_location_layout_version"] == 1
        assert body["report"]["status"] == ("resolved" if drift == "resolved" else "open")
        if drift == "resolved":
            assert "尚未改变库存位置" not in body["message"]
        assert _business_state(factory) == before


@pytest.mark.parametrize("changed", [
    {"reason": "不同的现场说明"},
    {"reported_quantity": 9},
    {"expected_lot_version": 2},
    {"observed_location_layout_version": 2},
])
def test_report_same_key_rejects_changed_recorded_payload(mobile_erp_app, changed):
    app, _ids, factory = mobile_erp_app
    payload = _report_request(factory)
    with TestClient(app) as client:
        _login(client, "mobile-scoped")
        assert client.post(REPORTS, json=payload).status_code == 201
        before = _business_state(factory)
        result = client.post(REPORTS, json={**payload, **changed})
        assert result.status_code == 409, result.text
        assert "请求标识" in result.json()["detail"]
        assert _business_state(factory) == before


def test_report_replay_preserves_original_actor_and_requires_current_scope(mobile_erp_app):
    from app.models.access_control import UserCustomerScope
    from app.models.warehouse_inventory import WarehouseLocationDiscrepancy

    app, _ids, factory = mobile_erp_app
    payload = _report_request(factory)
    with TestClient(app) as client:
        _login(client, "mobile-scoped")
        first = client.post(REPORTS, json=payload)
        assert first.status_code == 201
        with factory() as db:
            original_actor = db.get(WarehouseLocationDiscrepancy, first.json()["report"]["id"]).reported_by
        _login(client, "mobile-admin")
        replay = client.post(REPORTS, json=payload)
        assert replay.status_code == 201 and replay.json()["idempotent_replay"] is True
        with factory() as db:
            row = db.get(WarehouseLocationDiscrepancy, first.json()["report"]["id"])
            assert row.reported_by == original_actor
            for scope in db.scalars(select(UserCustomerScope).where(UserCustomerScope.user_id == original_actor)):
                db.delete(scope)
            db.commit()
        _login(client, "mobile-scoped")
        before = _business_state(factory)
        denied = client.post(REPORTS, json=payload)
        assert denied.status_code == 404
        assert _business_state(factory) == before
        _login(client, "mobile-picker")
        assert client.post(REPORTS, json=payload).status_code == 403


def test_report_legacy_missing_layout_snapshot_is_conflict_not_inferred_from_current_map(mobile_erp_app):
    from app.models.warehouse_inventory import WarehouseLocationDiscrepancy

    app, _ids, factory = mobile_erp_app
    payload = _report_request(factory)
    with TestClient(app) as client:
        _login(client, "mobile-scoped")
        first = client.post(REPORTS, json=payload)
        assert first.status_code == 201
        with factory() as db:
            db.get(WarehouseLocationDiscrepancy, first.json()["report"]["id"]).observed_location_layout_version = None
            db.commit()
        before = _business_state(factory)
        assert client.post(REPORTS, json=payload).status_code == 409
        assert _business_state(factory) == before


def test_report_receipt_construction_failure_rolls_back_report_and_audit(mobile_erp_app, monkeypatch):
    from app.api import mobile_erp

    app, _ids, factory = mobile_erp_app
    payload = _report_request(factory)
    with TestClient(app, raise_server_exceptions=False) as client:
        _login(client, "mobile-scoped")
        before = _business_state(factory)
        def broken_receipt(*args, **kwargs):
            raise RuntimeError("synthetic receipt construction failure")
        monkeypatch.setattr(mobile_erp, "_mobile_discrepancy_payload", broken_receipt)
        result = client.post(REPORTS, json=payload)
        assert result.status_code == 500
        assert _business_state(factory) == before


@pytest.mark.parametrize("invalid", ["lot_version", "quantity", "layout_version", "same_location", "frozen"])
def test_new_report_retains_current_inventory_and_map_guards(mobile_erp_app, invalid):
    from app.models.warehouse_inventory import InventoryLot

    app, _ids, factory = mobile_erp_app
    payload = _report_request(factory)
    if invalid == "lot_version":
        payload["expected_lot_version"] = 2
    elif invalid == "quantity":
        payload["reported_quantity"] = 1000
    elif invalid == "layout_version":
        payload["observed_location_layout_version"] = 2
    else:
        with factory() as db:
            lot = db.get(InventoryLot, payload["inventory_lot_id"])
            if invalid == "same_location":
                payload["observed_location_id"] = lot.warehouse_location_id
            else:
                lot.status = "frozen"
                db.commit()
    with TestClient(app) as client:
        _login(client, "mobile-scoped")
        before = _business_state(factory)
        assert client.post(REPORTS, json=payload).status_code == 409
        assert _business_state(factory) == before


@pytest.mark.parametrize("after_commit", [False, True], ids=["commit-failed", "ack-lost"])
def test_report_commit_outcome_preserves_one_report_and_original_request(mobile_erp_app, monkeypatch, after_commit):
    from sqlalchemy.orm import Session

    app, _ids, factory = mobile_erp_app
    payload = _report_request(factory)
    original_commit = Session.commit
    with TestClient(app, raise_server_exceptions=False) as client:
        _login(client, "mobile-scoped")
        before = _business_state(factory)
        def failed_commit(db):
            if after_commit:
                original_commit(db)
            raise RuntimeError("synthetic commit acknowledgement failure")
        monkeypatch.setattr(Session, "commit", failed_commit)
        assert client.post(REPORTS, json=payload).status_code == 500
        monkeypatch.setattr(Session, "commit", original_commit)
        recorded = _business_state(factory)
        if after_commit:
            assert recorded["reports"] == before["reports"] + 1
            assert recorded["report_audits"] == before["report_audits"] + 1
            assert recorded["lots"] == before["lots"]
            replay = client.post(REPORTS, json=payload)
            assert replay.status_code == 201 and replay.json()["idempotent_replay"] is True
            assert _business_state(factory) == recorded
        else:
            assert recorded == before


def test_concurrent_report_same_key_has_one_report_and_one_audit(mobile_erp_app):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    app, _ids, factory = mobile_erp_app
    payload = _report_request(factory)
    before = _business_state(factory)
    gate = Barrier(2)
    def send(_):
        with TestClient(app) as client:
            _login(client, "mobile-scoped")
            gate.wait(timeout=10)
            return client.post(REPORTS, json=payload)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(send, range(2)))
    assert [response.status_code for response in results] == [201, 201], [response.text for response in results]
    assert results[0].json()["report"]["id"] == results[1].json()["report"]["id"]
    assert sorted(response.json()["idempotent_replay"] for response in results) == [False, True]
    after = _business_state(factory)
    assert after["reports"] == before["reports"] + 1
    assert after["report_audits"] == before["report_audits"] + 1
    assert after["lots"] == before["lots"]
