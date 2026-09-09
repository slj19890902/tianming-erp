from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from tests.test_phase11_requisition import requisition_app, _login
from tests.test_p1_132_supplier_monthly_settlement import _paperboard_receipt
from tests.test_p1_81_receipt_purpose_flow import _use_p181_published_map_identity


def test_due_worker_replays_and_revises_drafts_with_atomic_audit(requisition_app, monkeypatch):
    import app.services.supplier_monthly_settlement as service
    import app.services.supplier_settlement_automation as automation
    from app.models.supplier_settlement import SupplierMonthlyStatement
    from app.models.audit import OperationLog

    app, factory = requisition_app
    _use_p181_published_map_identity(monkeypatch)
    with TestClient(app) as client:
        _login(client, 'admin')
        _paperboard_receipt(client, factory)
    monkeypatch.setattr(service, 'beijing_today', lambda: date(2026, 8, 20))
    assert automation.run_due_cycle(factory)['changed_statement_count'] == 0
    monkeypatch.setattr(service, 'beijing_today', lambda: date(2026, 8, 21))
    assert automation.run_due_cycle(factory)['changed_statement_count'] == 1
    assert automation.run_due_cycle(factory)['changed_statement_count'] == 0
    with factory() as db:
        row = db.scalar(select(SupplierMonthlyStatement))
        assert row.generated_by is None
        row.source_hash = '0' * 64
        db.commit()
    original_log = automation.OperationLog
    def fail_audit(**kwargs):
        raise RuntimeError('audit unavailable')
    monkeypatch.setattr(automation, 'OperationLog', fail_audit)
    with pytest.raises(RuntimeError, match='audit unavailable'):
        automation.run_due_cycle(factory)
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(SupplierMonthlyStatement)) == 1
        assert db.scalar(select(SupplierMonthlyStatement)).status == 'draft'
    monkeypatch.setattr(automation, 'OperationLog', original_log)
    assert automation.run_due_cycle(factory)['changed_statement_count'] == 1
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(SupplierMonthlyStatement)) == 2
        assert db.scalar(select(func.count()).select_from(OperationLog).where(
            OperationLog.action_code == 'supplier_settlement.auto_draft')) == 2
        active = db.scalar(select(SupplierMonthlyStatement).where(SupplierMonthlyStatement.active_guard == 1))
        from datetime import datetime
        active.reviewed_at = datetime(2026, 8, 21)
        active.source_hash = '1' * 64
        db.commit()
    assert automation.run_due_cycle(factory)['changed_statement_count'] == 0
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(SupplierMonthlyStatement)) == 2


def test_supplier_day_edit_revises_current_draft(requisition_app, monkeypatch):
    import app.services.supplier_monthly_settlement as service
    import app.core.time_contract as clock
    from app.api.suppliers import router
    from app.models.supplier import Supplier
    from app.models.supplier_settlement import SupplierMonthlyStatement
    from app.services.supplier_settlement_automation import run_due_cycle

    app, factory = requisition_app
    app.include_router(router, prefix='/api/suppliers')
    _use_p181_published_map_identity(monkeypatch)
    monkeypatch.setattr(service, 'beijing_today', lambda: date(2026, 8, 26))
    monkeypatch.setattr(clock, 'beijing_today', lambda: date(2026, 8, 26))
    with TestClient(app) as client:
        _login(client, 'admin')
        _paperboard_receipt(client, factory)
        run_due_cycle(factory)
        with factory() as db:
            statement = db.scalar(select(SupplierMonthlyStatement))
            supplier = db.get(Supplier, statement.supplier_id)
            supplier_id = supplier.id
            payload = dict(standard_name=supplier.standard_name,
                           expected_version=supplier.version, settlement_day=25)
        response = client.put(f'/api/suppliers/{supplier_id}', json=payload)
        assert response.status_code == 200, response.text
        with factory() as db:
            rows = list(db.scalars(select(SupplierMonthlyStatement).order_by(SupplierMonthlyStatement.id)))
            assert len(rows) == 2
            assert rows[0].status == 'voided'
            assert rows[1].settlement_day_snapshot == 25
            assert rows[1].period_end == date(2026, 8, 25)
            assert rows[1].erp_amount == rows[0].erp_amount
            assert rows[1].supersedes_statement_id == rows[0].id
        # A cutoff before the only receipt produces an auditable zero revision,
        # not a stale payable amount or an invented source line.
        payload.update(expected_version=response.json()['version'], settlement_day=10)
        changed = client.put(f'/api/suppliers/{supplier_id}', json=payload)
        assert changed.status_code == 200, changed.text
        with factory() as db:
            assert db.get(Supplier, supplier_id).settlement_day == 10
            assert db.scalar(select(func.count()).select_from(SupplierMonthlyStatement)) == 3
            active = db.scalar(select(SupplierMonthlyStatement).where(SupplierMonthlyStatement.active_guard == 1))
            assert active.erp_amount == 0
            assert len(active.lines) == 0
