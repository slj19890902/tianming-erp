from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text, update

from tests.test_phase6_incoming import incoming_api_app, _login
from tests.test_phase11_requisition import requisition_app


@pytest.fixture()
def receipt_app(incoming_api_app):
    from app.core.receipt_price_guard import ReceiptPriceGuardSession
    from app.models.material import Material
    from app.models.order import OrderItem
    from app.models.requisition import Requisition, RequisitionItem
    from app.models.supplier import Supplier

    app, factory = incoming_api_app
    factory.class_ = ReceiptPriceGuardSession
    with factory() as db:
        db.add(Supplier(standard_name="P045纸板", normalized_name="p045纸板", is_active=True))
        material = Material(
            code="P045-A", supplier_name="P045纸板", quote_price=Decimal("2.80"),
            price_unit="元/㎡", purchase_currency="CNY", purchase_tax_included=True,
            purchase_tax_rate=Decimal("0.13"), is_active=True, version=1,
        )
        db.add(material)
        db.flush()
        header = Requisition(
            requisition_number="REQ-P045", requisition_date=date(2026, 9, 7),
            supplier_name="P045纸板", status="已报料", created_by=1,
        )
        db.add(header)
        db.flush()
        for item_id in (1, 2):
            item = db.get(OrderItem, item_id)
            item.material_id = material.id
            db.add(RequisitionItem(
                requisition_id=header.id, order_item_id=item.id,
                requisition_qty=item.quantity, cardboard_len=1000, cardboard_width=500,
                product_name_snapshot="P045纸箱", material_snapshot="P045-A", status="有效",
            ))
        db.commit()
    return app, factory


def _counts(factory):
    from app.models.audit import OperationLog
    from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem
    from app.models.supplier_settlement import SupplierReceiptSettlementPriceFact
    from app.models.warehouse_inventory import InventoryLot, InventoryMovement
    with factory() as db:
        return {
            model.__tablename__: db.scalar(select(func.count()).select_from(model))
            for model in (IncomingReceipt, IncomingReceiptItem, SupplierReceiptSettlementPriceFact,
                          InventoryLot, InventoryMovement, OperationLog)
        }


def _receive(client, key="p045-retry", quantity=100, action=None):
    return client.put("/api/incoming/receive/r1", json={
        "received_quantity": quantity, "resolution_action": action, "idempotency_key": key,
    })


def test_legacy_missing_quote_rolls_back_and_same_key_retries_once(receipt_app):
    from app.models.material import Material
    from app.models.order import OrderItem
    from app.models.supplier_settlement import SupplierReceiptSettlementPriceFact
    app, factory = receipt_app
    with TestClient(app) as client:
        _login(client, "workshop")
        with factory() as db:
            db.get(Material, 1).quote_price = None
            db.commit()
        before = _counts(factory)
        blocked = _receive(client)
        assert blocked.status_code == 422, blocked.text
        assert blocked.json()["detail"]["code"] == "SUPPLIER_RECEIPT_MASTER_PRICE_INVALID"
        assert _counts(factory) == before
        with factory() as db:
            assert db.get(OrderItem, 1).material_status == "pending"
            db.get(Material, 1).quote_price = Decimal("2.80")
            db.commit()
        received = _receive(client)
        assert received.status_code == 200, received.text
        posted = _counts(factory)
        replay = _receive(client)
        assert replay.status_code == 200, replay.text
        assert _counts(factory) == posted
        with factory() as db:
            fact = db.scalar(select(SupplierReceiptSettlementPriceFact))
            assert fact.fact_origin == "receipt_frozen"
            assert fact.received_quantity_snapshot == 100
            assert fact.unit_price == Decimal("2.80")
        from scripts.admin.check_supplier_receipt_prices import check_database
        health = check_database(factory.kw["bind"].url.database)
        assert health["ok"], health
        assert health["effective_receipt_count"] == health["frozen_price_count"] == 1


def test_legacy_snapshot_survives_quote_change_new_partial_uses_new_quote(receipt_app):
    from app.models.material import Material
    from app.models.supplier_settlement import SupplierReceiptSettlementPriceFact
    app, factory = receipt_app
    with TestClient(app) as client:
        _login(client, "admin")
        first = _receive(client, "p045-first", 40, "await_supplier")
        assert first.status_code == 200, first.text
        with factory() as db:
            db.get(Material, 1).quote_price = Decimal("4.00")
            db.commit()
        second = _receive(client, "p045-second", 60)
        assert second.status_code == 200, second.text
    with factory() as db:
        facts = list(db.scalars(select(SupplierReceiptSettlementPriceFact).order_by(
            SupplierReceiptSettlementPriceFact.id
        )))
        assert [(f.received_quantity_snapshot, f.unit_price) for f in facts] == [
            (Decimal(40), Decimal("2.8")), (Decimal(60), Decimal("4"))
        ]


def test_exception_after_freeze_rolls_back_receipt_price_progress_and_audit(receipt_app, monkeypatch):
    import app.services.incoming_receipts as service
    from app.models.order import OrderItem
    from app.models.supplier_settlement import SupplierReceiptSettlementPriceFact
    app, factory = receipt_app
    def fail_after_freeze(db, *_args, **_kwargs):
        assert db.scalar(select(func.count()).select_from(SupplierReceiptSettlementPriceFact)) == 1
        raise RuntimeError("p045 injected after price freeze")
    monkeypatch.setattr(service, "_mark_order_progress", fail_after_freeze)
    with TestClient(app) as client:
        _login(client, "admin")
        before = _counts(factory)
        with pytest.raises(RuntimeError, match="p045 injected"):
            _receive(client)
        assert _counts(factory) == before
        with factory() as db:
            assert db.get(OrderItem, 1).material_status == "pending"


def test_commit_gate_catches_missing_freeze_even_after_flush(receipt_app, monkeypatch):
    import app.services.incoming_receipts as service
    from types import SimpleNamespace
    app, factory = receipt_app
    monkeypatch.setattr(service, "freeze_receipt_settlement_price", lambda *_a, **_k: SimpleNamespace(id=None))
    with TestClient(app) as client:
        _login(client, "admin")
        before = _counts(factory)
        blocked = _receive(client)
        assert blocked.status_code == 409, blocked.text
        assert blocked.json()["detail"]["code"] == "SUPPLIER_RECEIPT_PRICE_COMMIT_BLOCKED"
        assert _counts(factory) == before


@pytest.mark.parametrize("sql", [False, True])
def test_session_bulk_sql_cannot_bypass_receipt_tracking(receipt_app, sql):
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.services.incoming_receipts import IncomingReceiptError
    _, factory = receipt_app
    with factory() as db:
        statement = text("UPDATE incoming_receipt_items SET received_quantity = 200 WHERE id = 1") if sql else (
            update(IncomingReceiptItem).where(IncomingReceiptItem.id == 1).values(received_quantity=200)
        )
        with pytest.raises(IncomingReceiptError, match="SQL 旁路"):
            db.execute(statement)
        db.rollback()


def test_commit_gate_rejects_quantity_change_and_price_mutation(receipt_app):
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.supplier_settlement import SupplierReceiptSettlementPriceFact
    from app.services.incoming_receipts import IncomingReceiptError
    app, factory = receipt_app
    with TestClient(app) as client:
        _login(client, "admin")
        result = _receive(client)
        assert result.status_code == 200, result.text
    with factory() as db:
        db.get(IncomingReceiptItem, 1).received_quantity += 1
        db.flush()
        with pytest.raises(IncomingReceiptError, match="整笔提交已阻止"):
            db.commit()
        db.rollback()
        assert db.get(IncomingReceiptItem, 1).received_quantity == 100
        db.get(SupplierReceiptSettlementPriceFact, 1).unit_price = Decimal("99")
        with pytest.raises(IncomingReceiptError, match="不可修改"):
            db.commit()
        db.rollback()


def test_untouched_historical_gap_does_not_block_unrelated_commit(receipt_app):
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.material import Material
    from app.models.supplier_settlement import SupplierReceiptSettlementPriceFact
    from app.services.incoming_receipts import IncomingReceiptError
    app, factory = receipt_app
    with TestClient(app) as client:
        _login(client, "admin")
        assert _receive(client).status_code == 200
    # Simulate a pre-upgrade historical gap using a disposable fixture connection.
    with factory.kw["bind"].begin() as connection:
        connection.execute(SupplierReceiptSettlementPriceFact.__table__.delete())
    with factory() as db:
        db.get(Material, 1).remarks = "unrelated master edit"
        db.commit()
        with pytest.raises(RuntimeError):
            with db.begin_nested():
                db.get(IncomingReceiptItem, 1).received_quantity += 1
                db.flush()
                raise RuntimeError("rolled back historical edit")
        db.get(Material, 1).remarks = "unrelated edit after savepoint rollback"
        db.commit()
        row = db.get(IncomingReceiptItem, 1)
        row.status = "reversed"
        db.commit()
        row.status = "posted"
        with pytest.raises(IncomingReceiptError, match="整笔提交已阻止"):
            db.commit()
        db.rollback()


def test_failed_savepoint_does_not_poison_next_valid_receipt(receipt_app):
    from app.models.user import User
    from app.services.incoming_receipts import receive_one
    _, factory = receipt_app
    with factory() as db:
        args = dict(user=db.get(User, 1), item_key="r1", received_quantity=100,
                    resolution_action=None, resolution_reason=None, surplus_location_id=None,
                    idempotency_key="p045-savepoint")
        with pytest.raises(RuntimeError):
            with db.begin_nested():
                receive_one(db, **args)
                raise RuntimeError("rollback savepoint")
        receive_one(db, **args)
        db.commit()
    assert _counts(factory)["incoming_receipt_items"] == 1


def test_legacy_supplier_order_source_also_gets_frozen_price(receipt_app):
    from app.models.order import OrderItem
    from app.models.supplier_requisition_order import SupplierRequisitionOrder, SupplierRequisitionOrderItem
    from app.models.supplier_settlement import SupplierReceiptSettlementPriceFact
    app, factory = receipt_app
    with factory() as db:
        header = SupplierRequisitionOrder(order_number="SRO-P045", supplier_name="P045纸板")
        db.add(header)
        db.flush()
        db.add(SupplierRequisitionOrderItem(
            supplier_order_id=header.id, order_item_id=1, quantity=100, requisition_qty=100,
            material_id=1, material_code_snapshot="P045-A", supplier_name_snapshot="P045纸板",
            report_length_mm=1000, report_width_mm=500,
        ))
        db.get(OrderItem, 1).supplier_order_number = header.order_number
        db.commit()
    with TestClient(app) as client:
        _login(client, "admin")
        result = client.put("/api/incoming/receive/so1", json={
            "received_quantity": 100, "idempotency_key": "p045-legacy-supplier",
        })
        assert result.status_code == 200, result.text
    with factory() as db:
        fact = db.scalar(select(SupplierReceiptSettlementPriceFact))
        assert fact.source_kind == "supplier_order_item"
        assert fact.purchase_document_number_snapshot == "SRO-P045"


def test_batch_savepoint_keeps_priced_row_and_rolls_back_missing_price_row(receipt_app):
    from app.models.material import Material
    from app.models.order import OrderItem
    app, factory = receipt_app
    with factory() as db:
        unpriced = Material(code="P045-NO-PRICE", supplier_name="P045纸板", is_active=True)
        db.add(unpriced)
        db.flush()
        db.get(OrderItem, 2).material_id = unpriced.id
        db.commit()
    with TestClient(app) as client:
        _login(client, "admin")
        result = client.put("/api/incoming/batch-receive", json={"items": [
            {"item_id": "r1", "received_quantity": 100, "idempotency_key": "p045-batch-good"},
            {"item_id": "r2", "received_quantity": 50, "idempotency_key": "p045-batch-bad"},
        ]})
        assert result.status_code == 200, result.text
        assert result.json()["succeeded"] == 1
        assert result.json()["failed"] == 1
    counts = _counts(factory)
    assert counts["incoming_receipt_items"] == counts["supplier_receipt_settlement_price_facts"] == 1
    with factory() as db:
        assert db.get(OrderItem, 2).material_status == "pending"


def test_batch_commit_gate_rolls_back_released_sqlite_savepoints(receipt_app, monkeypatch):
    import app.services.incoming_receipts as service
    from types import SimpleNamespace
    app, factory = receipt_app
    monkeypatch.setattr(service, "freeze_receipt_settlement_price", lambda *_a, **_k: SimpleNamespace(id=None))
    with TestClient(app, raise_server_exceptions=False) as client:
        _login(client, "admin")
        before = _counts(factory)
        result = client.put("/api/incoming/batch-receive", json={"items": [
            {"item_id": "r1", "received_quantity": 100, "idempotency_key": "p045-batch-bypass"},
        ]})
        assert _counts(factory) == before
        assert result.status_code == 409, result.text
        assert result.json()["detail"]["code"] == "SUPPLIER_RECEIPT_PRICE_COMMIT_BLOCKED"


def test_batch_final_audit_failure_rolls_back_priced_receipt_and_released_savepoint(receipt_app, monkeypatch):
    import app.api.incoming as api
    app, factory = receipt_app
    def fail_batch_audit(*_args, **_kwargs):
        raise RuntimeError("p045 final batch audit failure")
    monkeypatch.setattr(api, "append_audit_event", fail_batch_audit)
    with TestClient(app, raise_server_exceptions=False) as client:
        _login(client, "admin")
        before = _counts(factory)
        result = client.put("/api/incoming/batch-receive", json={"items": [
            {"item_id": "r1", "received_quantity": 100, "idempotency_key": "p045-batch-audit"},
        ]})
        assert result.status_code == 500
        assert _counts(factory) == before


def test_modern_receipt_runs_through_production_session_guard(requisition_app, monkeypatch):
    from app.core.receipt_price_guard import ReceiptPriceGuardSession
    from app.models.supplier_settlement import SupplierReceiptSettlementPriceFact
    from tests.test_p1_81_receipt_purpose_flow import (
        _create_frozen_sources, _freeze_receipt_fact, _receive as modern_receive,
        _seed_material_and_staging, _use_p181_published_map_identity,
    )
    app, factory = requisition_app
    factory.class_ = ReceiptPriceGuardSession
    _use_p181_published_map_identity(monkeypatch)
    _seed_material_and_staging(factory)
    with TestClient(app) as client:
        _login(client, "admin")
        source = _create_frozen_sources(client, factory, order_quantity=10,
                                       purchase_total=10, order_purpose=10, stock_purpose=0)[0]
        price = _freeze_receipt_fact(client, source, idempotency_key="p045-modern-price")
        assert price.status_code == 200, price.text
        first = modern_receive(client, source, price.json(), quantity=10, idempotency_key="p045-modern")
        assert first.status_code == 200, first.text
        counts = _counts(factory)
        replay = modern_receive(client, source, price.json(), quantity=10, idempotency_key="p045-modern")
        assert replay.status_code == 200, replay.text
        assert _counts(factory) == counts
    with factory() as db:
        fact = db.scalar(select(SupplierReceiptSettlementPriceFact))
        assert fact.unit_price == Decimal("2.5")
        assert fact.match_strategy == "purchase_receipt_fact"


def test_saved_receipt_cannot_rebind_to_another_line_in_same_document(receipt_app):
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.services.incoming_receipts import IncomingReceiptError
    app, factory = receipt_app
    with TestClient(app) as client:
        _login(client, "admin")
        assert _receive(client).status_code == 200
    with factory() as db:
        db.get(IncomingReceiptItem, 1).requisition_item_id = 2
        with pytest.raises(IncomingReceiptError, match="不可换绑"):
            db.commit()
        db.rollback()
        assert db.get(IncomingReceiptItem, 1).requisition_item_id == 1


@pytest.mark.parametrize("method", ["bulk_save_objects", "bulk_insert_mappings", "bulk_update_mappings"])
def test_legacy_bulk_apis_cannot_bypass_commit_gate(receipt_app, method):
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.services.incoming_receipts import IncomingReceiptError
    _, factory = receipt_app
    with factory() as db:
        with pytest.raises(IncomingReceiptError, match="不能使用"):
            if method == "bulk_save_objects":
                db.bulk_save_objects([IncomingReceiptItem()])
            else:
                getattr(db, method)(IncomingReceiptItem, [{"id": 1}])
        db.rollback()
