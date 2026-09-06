"""Keep the SQLite settlement scan and its write in one protected transaction."""

from datetime import date

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from tests.test_p0_45_monthly_completeness_gate import _generate, _receipt


@pytest.fixture()
def file_settlement_db(tmp_path):
    from app.core.database import create_sqlite_engine
    from app.models import Base
    from app.models.material import Material
    from app.models.supplier import Supplier
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "p045-concurrent-settlement.sqlite3", busy_timeout_ms=50)
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        user = User(username="p045-concurrent", password_hash="unused", role="admin", real_name="隔离并发测试")
        supplier = Supplier(standard_name="隔离并发供应商", normalized_name="隔离并发供应商", settlement_day=20)
        material = Material(code="P045-CONCURRENT", supplier_name=supplier.standard_name, is_active=True, version=1)
        db.add_all([user, supplier, material])
        db.flush()
        _receipt(db, user, supplier, material, "before-scan")
        db.commit()
        ids = user.id, supplier.id, material.id
    try:
        yield engine, ids
    finally:
        engine.dispose()


def _new_receipt(db, ids, key):
    from app.models.material import Material
    from app.models.supplier import Supplier
    from app.models.user import User

    return _receipt(db, db.get(User, ids[0]), db.get(Supplier, ids[1]), db.get(Material, ids[2]), key)


def _prepare_statement(engine, ids, operation):
    from app.models.supplier_settlement import SupplierMonthlyStatement
    from app.models.user import User

    if operation == "generate":
        return None
    with Session(engine, expire_on_commit=False, autoflush=False) as setup:
        result = _generate(setup, setup.get(User, ids[0]))
        statement = setup.get(SupplierMonthlyStatement, result["items"][0]["id"])
        statement.supplier_statement_date = date(2026, 8, 25)
        statement.supplier_statement_number = "P045-CONCURRENT-BILL"
        statement.supplier_statement_amount = statement.adjusted_amount
        if operation == "regenerate":
            _new_receipt(setup, ids, "before-regeneration")
        setup.commit()
        return statement.id


@pytest.mark.parametrize("operation", ["generate", "regenerate", "confirm"])
@pytest.mark.parametrize("finish", ["commit", "rollback"])
def test_settlement_scan_excludes_concurrent_receipt_until_transaction_ends(
    file_settlement_db, monkeypatch, operation, finish,
):
    import app.services.supplier_monthly_settlement as service
    from app.models.finance_payable import FinancePayable
    from app.models.incoming_receipt import IncomingReceipt
    from app.models.supplier_settlement import SupplierMonthlyStatement, SupplierReceiptSettlementPriceFact
    from app.models.user import User

    engine, ids = file_settlement_db
    statement_id = _prepare_statement(engine, ids, operation)

    original_scan = service._scan_candidates_for_bounds
    scanned = []

    def scan_then_attempt_receipt(db, **kwargs):
        result = original_scan(db, **kwargs)
        scanned.append(True)
        with Session(engine, expire_on_commit=False, autoflush=False) as writer:
            # Both sessions have live, distinct DBAPI connections to the same file.
            assert writer.connection().connection.driver_connection is not db.connection().connection.driver_connection
            with pytest.raises(OperationalError, match="database is locked"):
                _new_receipt(writer, ids, "between-scan-and-write")
                writer.commit()
            writer.rollback()
        return result

    monkeypatch.setattr(service, "_scan_candidates_for_bounds", scan_then_attempt_receipt)
    with Session(engine, expire_on_commit=False, autoflush=False) as db:
        user = db.get(User, ids[0])
        if operation == "generate":
            result = _generate(db, user)
            assert result["changed_statement_count"] == 1
        else:
            statement = db.get(SupplierMonthlyStatement, statement_id)
            kwargs = dict(statement_id=statement.id, expected_version=statement.version, user=user)
            if operation == "regenerate":
                result = service.regenerate_statement(db, reason="隔离并发回归", **kwargs)
                assert result.document_revision == 2
            else:
                result = service.confirm_statement(db, **kwargs)
                assert result.finance_payable_id is not None
        assert scanned
        getattr(db, finish)()

    # A blocked writer left no partial receipt, and the same receipt succeeds
    # once the settlement transaction either commits or rolls back.
    with Session(engine, expire_on_commit=False, autoflush=False) as writer:
        assert writer.scalar(select(func.count(IncomingReceipt.id)).where(
            IncomingReceipt.receipt_number == "IR-between-scan-and-write"
        )) == 0
        item = _new_receipt(writer, ids, "between-scan-and-write")
        writer.commit()
        assert writer.scalar(select(func.count(SupplierReceiptSettlementPriceFact.id)).where(
            SupplierReceiptSettlementPriceFact.incoming_receipt_item_id == item.id
        )) == 1
        expected_statements = (
            int(finish == "commit") if operation == "generate"
            else 1 + int(operation == "regenerate" and finish == "commit")
        )
        assert writer.scalar(select(func.count(SupplierMonthlyStatement.id))) == expected_statements
        assert writer.scalar(select(func.count(FinancePayable.id))) == int(operation == "confirm" and finish == "commit")


@pytest.mark.parametrize("operation", ["generate", "regenerate", "confirm"])
def test_existing_writer_returns_business_409_and_releases_failed_session(file_settlement_db, operation):
    from fastapi import HTTPException
    from app.api.supplier_settlements import _run_mutation
    from app.models.supplier_settlement import SupplierMonthlyStatement
    from app.models.user import User
    import app.services.supplier_monthly_settlement as service

    engine, ids = file_settlement_db
    statement_id = _prepare_statement(engine, ids, operation)
    with Session(engine, expire_on_commit=False, autoflush=False) as writer, Session(
        engine, expire_on_commit=False, autoflush=False,
    ) as db:
        user = db.get(User, ids[0])
        _new_receipt(writer, ids, "other-writer-holds-lock")

        def execute():
            if operation == "generate":
                _generate(db, user)
            else:
                statement = db.get(SupplierMonthlyStatement, statement_id)
                kwargs = dict(statement_id=statement.id, expected_version=statement.version, user=user)
                if operation == "regenerate":
                    service.regenerate_statement(db, reason="隔离繁忙回归", **kwargs)
                else:
                    service.confirm_statement(db, **kwargs)
            return {}, None

        with pytest.raises(HTTPException) as blocked:
            _run_mutation(
                db, user=user, key=f"p045-lock-busy-{operation}", action="P045_LOCK_TEST",
                payload={}, operation=execute, description="隔离并发占锁回归",
            )
        assert blocked.value.status_code == 409
        assert blocked.value.detail["code"] == "SUPPLIER_SETTLEMENT_CONCURRENT_WRITE"
        assert not db.in_transaction()
        writer.rollback()
        execute()
        db.rollback()
