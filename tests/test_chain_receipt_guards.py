"""Regression cases found in the 2026-10-09 order-to-delivery audit."""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from test_stock_replenishment_flow import stock_replenishment_app, _login, _customer_replenishment_payload
from test_p1_33c5_external_packaging_receiving import purchase_app, _confirm, _pending, _root_line, _login as _purchase_login
from test_phase11_requisition import requisition_app, _login as _legacy_login
from test_p1_81_receipt_purpose_flow import _p181_published_map_identity


def _confirm_stock(client, factory, item_id):
    from app.models.stock_replenishment import StockReplenishmentOrderItem
    from app.services.unified_procurement import stock_snapshot, fingerprint
    with factory() as db:
        item = db.get(StockReplenishmentOrderItem, item_id)
        supplier = item.order.supplier_name
        proof = fingerprint(stock_snapshot(item, item.order))
    response = client.post("/api/requisition/supplier-orders/from-pending-selection", json={"supplier_groups": [{
        "supplier_name": supplier, "request_key": "chain-audit-stock-purchase", "stock_sources": [
            {"stock_replenishment_item_id": item_id, "source_fingerprint": proof}]}]})
    assert response.status_code == 201, response.text


def test_external_receipt_replay_rechecks_current_customer_scope(purchase_app, monkeypatch):
    with TestClient(purchase_app) as client:
        _purchase_login(client, "purchase-admin")
        _confirm(client, purchase_app.state.fixture["order_id"])
        purchase, line = _root_line(_pending(client))
        url = f"/api/external-packaging-purchases/{purchase['id']}/receipts"
        payload = {"idempotency_key": "scope-replay", "lines": [
            {"purchase_item_id": line["purchase_item_id"], "received_quantity": "40"}]}
        assert client.post(url, json=payload).status_code == 200
        monkeypatch.setattr("app.api.external_packaging_purchases._visible_customer_ids", lambda *_: set())
        replay = client.post(url, json=payload)
        assert replay.status_code == 403, replay.text
    from app.models.external_packaging_purchase import ExternalPackagingReceipt
    with purchase_app.state.session_factory() as db:
        assert len(db.scalars(select(ExternalPackagingReceipt)).all()) == 1


def test_default_remaining_replenishment_receipt_replays_after_partial(stock_replenishment_app):
    app, factory = stock_replenishment_app
    with TestClient(app) as client:
        _login(client)
        created = client.post("/api/requisition/stock-replenishment/orders", json=_customer_replenishment_payload(quantity=100))
        assert created.status_code == 201, created.text
        item_id = created.json()["items"][0]["id"]
        _confirm_stock(client, factory, item_id)
        url = f"/api/incoming/receive/sr{item_id}"
        first = client.put(url, json={"received_quantity": 60, "resolution_action": "await_supplier", "idempotency_key": "partial"})
        assert first.status_code == 200, first.text
        payload = {"idempotency_key": "remaining"}
        second = client.put(url, json=payload)
        assert second.status_code == 200, second.text
        retry = client.put(url, json=payload)
        assert retry.status_code == 200, retry.text
        assert retry.json() == second.json()
    from app.models.incoming_receipt import IncomingReceiptItem
    with factory() as db:
        assert [r.received_quantity for r in db.scalars(select(IncomingReceiptItem).order_by(IncomingReceiptItem.id))] == [60, 40]


def test_batch_cannot_receive_same_stock_source_under_numeric_aliases(stock_replenishment_app):
    app, factory = stock_replenishment_app
    with TestClient(app) as client:
        _login(client)
        created = client.post("/api/requisition/stock-replenishment/orders", json=_customer_replenishment_payload(quantity=100))
        assert created.status_code == 201, created.text
        item_id = created.json()["items"][0]["id"]
        _confirm_stock(client, factory, item_id)
        response = client.put("/api/incoming/batch-receive", json={"items": [
            {"item_id": key, "received_quantity": 10, "resolution_action": "await_supplier", "idempotency_key": f"alias-{idx}"}
            for idx, key in enumerate((f"sr{item_id}", f"sr0{item_id}"))]})
        assert response.status_code == 200, response.text
        assert response.json()["succeeded"] == 1, response.text
    from app.models.incoming_receipt import IncomingReceiptItem
    with factory() as db:
        assert sum(r.received_quantity for r in db.scalars(select(IncomingReceiptItem))) == 10


@pytest.mark.parametrize("header_status,row_status", [("reversed", "reversed"), ("posted", "reversed")])
def test_reversed_incoming_receipt_cannot_be_reported_as_success_on_retry(header_status, row_status):
    from app.services.incoming_receipts import IncomingReceiptError, _idempotent_receipt_item
    row = SimpleNamespace(status=row_status, stock_replenishment_item_id=None,
        supplier_order_item_id=None, requisition_item_id=None, order_item_id=1,
        id=1, resolution_action=None, resolution_reason=None, planned_quantity=100,
        received_quantity=100, cumulative_received_quantity=100)
    receipt = SimpleNamespace(status=header_status, received_by=1, items=[row])
    with pytest.raises(IncomingReceiptError, match="撤销"):
        _idempotent_receipt_item(SimpleNamespace(scalar=lambda *_: None), receipt=receipt,
            item_key=1, received_quantity=100, resolution_action=None, resolution_reason=None,
            surplus_disposition=None, surplus_location_id=None, user=SimpleNamespace(id=1))


@pytest.mark.parametrize("change", ["reverse", "revoke_cost"])
def test_receipt_batch_replay_respects_current_facts_and_cost_access(requisition_app, monkeypatch, change):
    from test_p1_81_receipt_purpose_flow import _seed_material_and_staging, _create_frozen_sources, _freeze_receipt_fact
    app, factory = requisition_app
    _seed_material_and_staging(factory)
    with TestClient(app) as client:
        _legacy_login(client, "admin")
        source = _create_frozen_sources(client, factory, order_quantity=500,
            purchase_total=600, order_purpose=500, stock_purpose=100)[0]
        frozen = _freeze_receipt_fact(client, source, idempotency_key="reversed-batch-price")
        assert frozen.status_code == 200, frozen.text
        fact = frozen.json()
        payload = {"idempotency_key": "reversed-batch", "items": [{"item_id": source.route_key,
            "received_quantity": 40, "idempotency_key": "reversed-line",
            "expected_receipt_fact_version": fact["receipt_fact_version"],
            "purchase_purpose_source_snapshot_id": source.purpose_snapshot_id,
            "expected_purpose_snapshot_version": source.purpose_snapshot_version,
            "receipt_plan_fingerprint": fact["receipt_plan_fingerprint"],
            "expected_actual_material_version": fact["actual_material_version"],
            "actual_material_fingerprint": fact["actual_material_fingerprint"]}]}
        posted = client.put("/api/incoming/batch-receive", json=payload)
        assert posted.status_code == 200 and posted.json()["succeeded"] == 1, posted.text
        receipt_id = posted.json()["results"][0]["item"]["receipt_item_id"]
        if change == "reverse":
            reversed_result = client.put(f"/api/incoming/receipt-items/{receipt_id}/revert", json={"idempotency_key": "reverse-batch-line"})
            assert reversed_result.status_code == 200, reversed_result.text
        else:
            monkeypatch.setattr("app.api.incoming.has_permission", lambda _user, permission: permission != "cost.view")
        retry = client.put("/api/incoming/batch-receive", json=payload)
        if change == "reverse":
            assert retry.status_code == 409, retry.text
        else:
            assert retry.status_code == 200, retry.text
            allocation = retry.json()["results"][0]["item"]["purpose_allocation"]
            assert "order_cost" not in allocation, retry.text
            assert allocation["order_sheet_delta"] == 40
