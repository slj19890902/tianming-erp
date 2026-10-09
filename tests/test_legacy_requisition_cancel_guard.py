"""Legacy cancellation cannot detach a live frozen supplier purchase."""

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.test_phase11_requisition import requisition_app, _login, _batch_payload
from tests.test_p1_81_receipt_purpose_flow import (
    _p181_published_map_identity, _seed_material_and_staging,
    _create_frozen_sources, _create_frozen_source_batch,
    _freeze_receipt_fact, _receive,
)


def state(factory):
    from app.models.order import OrderItem
    from app.models.requisition import RequisitionItem
    from app.models.supplier_requisition_order import SupplierRequisitionOrder, SupplierRequisitionOrderItem, PurchasePurposeSourceSnapshot
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.warehouse_inventory import InventoryReservation
    with factory() as db:
        return {
            "orders": [{"id": r.id, "status": r.requisition_status, "quantity": r.requisition_qty,
                        "supplier_order_number": r.supplier_order_number}
                       for r in db.scalars(select(OrderItem).order_by(OrderItem.id))],
            "purchases": [{"id": r.id, "status": r.status, "number": r.order_number}
                          for r in db.scalars(select(SupplierRequisitionOrder))],
            "purchase_items": [{"id": r.id, "order_item_id": r.order_item_id, "status": r.status,
                               "quantity": r.requisition_qty, "source_key": r.source_key}
                              for r in db.scalars(select(SupplierRequisitionOrderItem))],
            "legacy_items": [{"id": r.id, "status": r.status} for r in db.scalars(select(RequisitionItem))],
            "frozen_purpose_ids": [r.id for r in db.scalars(select(PurchasePurposeSourceSnapshot))],
            "receipts": [{"id": r.id, "status": r.status, "quantity": r.received_quantity}
                         for r in db.scalars(select(IncomingReceiptItem))],
            "reservations": [{"id": r.id, "status": r.status, "reserved": r.reserved_stock_quantity,
                              "released": r.released_stock_quantity}
                             for r in db.scalars(select(InventoryReservation))],
        }


@pytest.mark.parametrize("kind", ["legacy", "frozen", "partial_receipt", "multi_order_purchase"])
def test_legacy_cancel_keeps_formal_purchase_contract(requisition_app, kind):
    app, factory = requisition_app
    _seed_material_and_staging(factory)
    with TestClient(app) as client:
        _login(client, "admin")
        if kind == "legacy":
            response = client.post("/api/requisition/batches", json=_batch_payload())
            assert response.status_code == 201, response.text
        elif kind == "multi_order_purchase":
            _create_frozen_source_batch(client, factory, count=2)
        else:
            source = _create_frozen_sources(client, factory, order_quantity=500,
                purchase_total=600, order_purpose=500, stock_purpose=100)[0]
            if kind == "partial_receipt":
                frozen = _freeze_receipt_fact(client, source, idempotency_key="review-cancel-price")
                assert frozen.status_code == 200, frozen.text
                received = _receive(client, source, frozen.json(), quantity=40,
                                    idempotency_key="review-cancel-receipt")
                assert received.status_code == 200, received.text
        before = state(factory)
        cancelled = client.put("/api/requisition/items/1/cancel", json={})
        after = state(factory)
        evidence = {"kind": kind, "http_status": cancelled.status_code, "response": cancelled.json(),
                    "before": before, "after": after}
        if kind == "legacy":
            assert cancelled.status_code == 200, cancelled.text
            assert after["orders"][0]["status"] == "未报料"
            assert all(r["status"] == "已取消" for r in after["legacy_items"])
        else:
            assert cancelled.status_code == 409, json.dumps(evidence, ensure_ascii=False)
            assert after == before


def test_void_one_of_two_purchases_preserves_order_inventory_reservation(requisition_app):
    from tests.test_p1_81_receipt_purpose_flow import _seed_order_semi_reservation
    from tests.test_p1_80_purchase_purpose_allocation import (
        _prepare_order_item, _selection, _single_line, _set_purpose_plan, _save_draft, _created_order_id,
    )
    from tests.test_phase11_requisition import _preview_supplier_order_draft
    from app.models.warehouse_inventory import InventoryLot

    app, factory = requisition_app
    _seed_material_and_staging(factory)
    _prepare_order_item(factory, quantity=10)
    _seed_order_semi_reservation(factory, credited_piece_quantity=2, pieces_per_box=1)
    with TestClient(app) as client:
        _login(client, "admin")
        purchase_ids = []
        for _ in range(2):
            draft = _preview_supplier_order_draft(client, [_selection()])
            _set_purpose_plan(_single_line(draft), purchase_total=4, order_purpose=4, stock_purpose=0)
            saved = _save_draft(client, draft)
            assert saved.status_code == 201, saved.text
            purchase_ids.append(_created_order_id(saved))
        before = state(factory)
        response = client.put(f"/api/requisition/supplier-orders/{purchase_ids[0]}/void")
        after = state(factory)
        with factory() as db:
            lot = db.scalar(select(InventoryLot).where(InventoryLot.lot_number == "P181-DOUBLE-SEMI-LOT"))
            stock = {"available": lot.quantity_available, "reserved": lot.quantity_reserved,
                     "consumed": lot.quantity_consumed}
        evidence = {"http_status": response.status_code, "before": before, "after": after,
                    "stock_after": stock, "response": response.json()}
        assert response.status_code == 200, response.text
        assert after["purchases"][1]["status"] == "confirmed"
        assert after["orders"][0]["quantity"] == 4
        assert after["reservations"] == before["reservations"], json.dumps(evidence, ensure_ascii=False)
        assert stock == {"available": 0, "reserved": 2, "consumed": 0}
        last_void = client.put(f"/api/requisition/supplier-orders/{purchase_ids[1]}/void")
        assert last_void.status_code == 200, last_void.text
        final = state(factory)
        assert final["orders"][0]["status"] == "未报料"
        assert final["reservations"][0]["status"] == "released"
        with factory() as db:
            lot = db.scalar(select(InventoryLot).where(InventoryLot.lot_number == "P181-DOUBLE-SEMI-LOT"))
            assert (lot.quantity_available, lot.quantity_reserved, lot.quantity_consumed) == (2, 0, 0)


def test_voided_supplier_history_does_not_block_new_legacy_cancel(requisition_app):
    app, factory = requisition_app
    _seed_material_and_staging(factory)
    with TestClient(app) as client:
        _login(client, "admin")
        source = _create_frozen_sources(client, factory, order_quantity=500,
            purchase_total=600, order_purpose=500, stock_purpose=100)[0]
        from app.models.supplier_requisition_order import SupplierRequisitionOrder
        with factory() as db:
            purchase_id = db.scalar(select(SupplierRequisitionOrder.id))
        assert client.put(f"/api/requisition/supplier-orders/{purchase_id}/void").status_code == 200
        created = client.post("/api/requisition/batches", json=_batch_payload())
        assert created.status_code == 201, created.text
        cancelled = client.put("/api/requisition/items/1/cancel", json={})
        assert cancelled.status_code == 200, cancelled.text
        final = state(factory)
        assert final["orders"][0]["status"] == "未报料"
        assert final["purchases"][0]["status"] == "voided"


def test_legacy_cancel_rechecks_live_item_after_claim(requisition_app, monkeypatch):
    from sqlalchemy import update
    from app.models.order import OrderItem
    from app.services import production_workflow
    app, factory = requisition_app
    _seed_material_and_staging(factory)
    original = production_workflow.lock_order_rows_for_production_transition
    with TestClient(app) as client:
        _login(client, "admin")
        assert client.post("/api/requisition/batches", json=_batch_payload()).status_code == 201
        before = state(factory)
        calls = []
        def changed_before_claim(db, order_ids):
            calls.append(order_ids)
            db.execute(update(OrderItem).where(OrderItem.id == 1)
                       .values(material_status="received").execution_options(synchronize_session=False))
            return original(db, order_ids)
        monkeypatch.setattr(production_workflow, "lock_order_rows_for_production_transition", changed_before_claim)
        result = client.put("/api/requisition/items/1/cancel", json={})
        assert calls
        assert result.status_code == 409, result.text
        assert state(factory) == before

from tests.test_n039_composite_bom_requisition import composite_requisition_app, _login as bom_login, _parent_payload, _component_payload

def test_unified_bom_legacy_cancel_cannot_leave_active_contract(composite_requisition_app):
    from app.models.supplier_requisition_order import SupplierRequisitionOrder, SupplierRequisitionOrderItem
    from app.models.requisition import RequisitionItem
    from app.models.procurement_source import ProcurementSourceLink
    from app.api.requisition import _active_requisition_facts_by_item_ids
    from app.models.order import OrderItem
    app, factory = composite_requisition_app
    def snap():
        with factory() as db:
            return {'headers':[(r.id,r.status) for r in db.scalars(select(SupplierRequisitionOrder))],
                    'lines':[(r.id,r.order_item_id,r.status,r.source_key) for r in db.scalars(select(SupplierRequisitionOrderItem))],
                    'legacy':[(r.id,r.order_item_id,r.status) for r in db.scalars(select(RequisitionItem))],
                    'links':[(r.id,r.material_requisition_item_id,r.status) for r in db.scalars(select(ProcurementSourceLink))],
                    'orders':[(r.id,r.requisition_qty,r.requisition_status) for r in db.scalars(select(OrderItem))],
                    'facts':_active_requisition_facts_by_item_ids(db,[1])}
    with TestClient(app) as client:
        bom_login(client)
        created = client.post('/api/requisition/supplier-orders/from-pending-selection', json={'supplier_groups':[{'supplier_name':'N039 供应商','request_key':'review-bom-cancel-001','bom_items':[_parent_payload(),_component_payload(1),_component_payload(2)]}]})
        assert created.status_code==201,created.text
        before = snap()
        response=client.put('/api/requisition/items/1/cancel',json={})
        after=snap()
        evidence={'status':response.status_code,'response':response.json(),'before':before,'after':after}
        assert response.status_code==409,evidence
        assert before==after
