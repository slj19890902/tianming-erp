from decimal import Decimal
from types import SimpleNamespace
import json

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select, text

from test_p1_130_statement_invoice_finance import p1_130_app, _login


def source():
    return dict(customer_id=1, product_id=1, order_item_id=1, source_sha256="a" * 64,
        source_row=6, delivery_date="2026-08-03", customer_receipt_number="CUSTOMER-RECEIPT-1",
        product_code="BOX-001", product_name="客户纸箱名称", specification="客户规格原文",
        quantity=3, unit="只", gross_unit_price="10.000", gross_amount="30.00",
        reconciliation_month="2026-10", original_delivery_item_ids=[1])


def facts(db):
    return {table: [tuple(r) for r in db.execute(text(f"SELECT * FROM {table} ORDER BY id"))]
            for table in ("sales_orders", "sales_order_items", "sales_deliveries",
                          "sales_delivery_items", "inventory_lots", "finance_statements",
                          "finance_statement_items", "finance_invoices", "finance_settlement_records")}


def test_historical_source_keeps_prior_facts_and_rejects_replays_and_overcount(p1_130_app):
    from app.models.user import User
    from app.models.customer_charge import CustomerCharge
    from app.services.historical_delivery_receivables import import_historical_receivable
    _, factory = p1_130_app
    with factory() as db:
        actor = db.get(User, 1)
        before = facts(db)
        for bad_actor in (None, SimpleNamespace(is_active=True, role="finance")):
            with pytest.raises(HTTPException) as error:
                import_historical_receivable(db, actor=bad_actor, source=source(), authorization_reason="明确授权")
            assert error.value.status_code == 403
        with pytest.raises(HTTPException):
            import_historical_receivable(db, actor=actor, source=dict(source(), customer_id=999), authorization_reason="明确授权")
        first = import_historical_receivable(db, actor=actor, source=source(), authorization_reason="保留旧票款，新期独立应收")
        assert import_historical_receivable(db, actor=actor, source=source(), authorization_reason="保留旧票款，新期独立应收").id == first.id
        with pytest.raises(HTTPException):
            import_historical_receivable(db, actor=actor, source=dict(source(), product_name="更改"), authorization_reason="保留旧票款，新期独立应收")
        with pytest.raises(HTTPException):
            import_historical_receivable(db, actor=actor, source=dict(source(), source_row=7, quantity=8, gross_amount="80.00"), authorization_reason="保留旧票款，新期独立应收")
        assert facts(db) == before
        db.rollback()
        assert not list(db.scalars(select(CustomerCharge)))
        assert facts(db) == before


def test_historical_source_statement_export_and_invoice_use_frozen_goods(p1_130_app):
    from app.api.invoice_tasks import customer_router, _snapshot_for_statement
    from app.models.invoice_task import InvoiceSellerEntity
    from app.models.user import User
    from app.models.finance import Statement
    from app.api.finance import (
        create_statement, StatementCreate, _statement_detail_response,
        _customer_statement_export_data, CustomerChargeTransition, _customer_charge_transition)
    from app.models.customer import Customer
    from app.services.historical_delivery_receivables import import_historical_receivable
    from test_fin001_invoice_tasks import _complete_invoice_profile
    app, factory = p1_130_app
    app.include_router(customer_router, prefix="/api/customers")
    with TestClient(app) as client:
        _login(client)
        seller_id = _complete_invoice_profile(client)
    with factory() as db:
        actor = db.get(User, 1)
        charge = import_historical_receivable(db, actor=actor, source=source(), authorization_reason="保留旧票款，新期独立应收")
        db.commit()
        with pytest.raises(HTTPException):
            _customer_charge_transition(db=db, user=actor, charge_id=charge.id,
                payload=CustomerChargeTransition(expected_version=1, idempotency_key="history-no-unfreeze"),
                action="customer_charge_cancel_confirmation")
        statement = create_statement(StatementCreate(customer_id=1, statement_month="2026-10",
            idempotency_key="history-statement-1", customer_charge_ids=[charge.id]), db, actor)
        detail = _statement_detail_response(db, statement["id"], actor)
        assert detail["total_receivable"] == Decimal("30")
        row = detail["items"][0]
        assert row["product_code"] == "BOX-001" and row["source_kind"] == "historical_delivery"
        assert str(row["actual_received_date"]) == "2026-08-03"
        saved = db.get(Statement, statement["id"])
        export = _customer_statement_export_data(db, statement=saved, customer=db.get(Customer, 1), sort_by="order_number")
        assert export["rows"][0]["product_code"] == "BOX-001"
        _, lines, missing = _snapshot_for_statement(db, saved, db.get(InvoiceSellerEntity, seller_id))
        assert not missing
        assert len(lines) == 1 and lines[0]["project_name"] == "BOX-001"
        assert lines[0]["quantity"] == 3 and lines[0]["total_amount"] == 30
        assert lines[0]["specification"] is None


def test_historical_source_requires_reader_and_blocks_destructive_downgrade(tmp_path):
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine
    from desktop_assistant.reader_contract import target_readers
    path = tmp_path / "history.sqlite3"
    engine = create_engine("sqlite:///" + str(path))
    migration = Path(__file__).resolve().parents[1] / "alembic/versions/ev1020hr_historical_receivable_source.py"
    spec = importlib.util.spec_from_file_location("history_source_migration", migration)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with engine.begin() as db:
        db.execute(text("CREATE TABLE finance_customer_charges (id INTEGER PRIMARY KEY, amount NUMERIC)"))
        db.execute(text("INSERT INTO finance_customer_charges VALUES (1,30)"))
        with Operations.context(MigrationContext.configure(db)):
            module.upgrade()
            module.downgrade()
            module.upgrade()
            db.execute(text("UPDATE finance_customer_charges SET historical_source_key='source-1', historical_delivery_snapshot_json='{}'"))
            with pytest.raises(RuntimeError, match="禁止"):
                module.downgrade()
    with pytest.raises(ValueError, match="reader"):
        target_readers(path, {}, {}, None, None, factory=False)
    target_readers(path, {}, {"reader_capabilities": {"historical_delivery_receivable_v1": 1}},
                   None, None, factory=False)
    engine.dispose()


def test_dispatch_activation_checks_actual_database_for_caller_owned_transaction(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from desktop_assistant import delivery_dispatch_contract
    from app.services.delivery_dispatch_commands import require_activation
    path = str(tmp_path / 'atomic.sqlite3')
    engine = create_engine('sqlite:///' + path)
    seen = []
    monkeypatch.setattr(delivery_dispatch_contract, 'require_managed_dispatch_activation', seen.append)
    with engine.connect() as connection:
        connection.exec_driver_sql('BEGIN IMMEDIATE')
        with Session(bind=connection, join_transaction_mode='create_savepoint') as db:
            require_activation(db)
            def deny(_):
                raise ValueError('not activated')
            monkeypatch.setattr(delivery_dispatch_contract, 'require_managed_dispatch_activation', deny)
            with pytest.raises(HTTPException) as failure:
                require_activation(db)
            assert failure.value.status_code == 503
        connection.rollback()
    assert seen == [path]
