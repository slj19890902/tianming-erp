from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from tests.test_p1_131_cost_pool import _login as login_cost
from tests.test_p1_131_cost_pool import p1_131_cost_app
from tests.test_p1_132_supplier_monthly_settlement import (
    _include_supplier_router,
    _paperboard_receipt,
)
from tests.test_p1_144_simplified_finance import (
    _enable_router as enable_simple_finance_router,
    _seed_payable_statement,
)
from tests.test_p1_81_receipt_purpose_flow import _use_p181_published_map_identity
from tests.test_phase11_requisition import _login, requisition_app


def _include_all_p1_149_finance_routers(app) -> None:
    _include_supplier_router(app)
    enable_simple_finance_router(app)


def _error_code(response) -> str | None:
    detail = response.json().get("detail")
    if isinstance(detail, dict):
        return str(detail.get("code") or "") or None
    return None


def _freeze_beijing_date(monkeypatch: pytest.MonkeyPatch, value: date) -> None:
    import app.api.finance_simplified as simplified_api

    moment = datetime(value.year, value.month, value.day, 10, 0, 0)
    monkeypatch.setattr(simplified_api, "beijing_now_naive", lambda: moment)
    monkeypatch.setattr(simplified_api, "beijing_today", lambda: value, raising=False)


def _seed_order(
    factory,
    *,
    customer_id: int,
    order_number: str,
    order_date: date,
    status: str = "pending_production",
) -> int:
    from app.models.order import Order

    with factory() as db:
        row = Order(
            order_number=order_number,
            customer_id=customer_id,
            order_date=order_date,
            delivery_date=order_date,
            status=status,
            payment_status="unpaid",
            total_amount=Decimal("100.00"),
        )
        db.add(row)
        db.commit()
        return int(row.id)


def _create_acceptance(
    client: TestClient,
    *,
    customer_id: int,
    bill_number: str,
    amount: str,
) -> dict:
    response = client.post(
        "/api/finance/simple-finance/acceptances",
        json={
            "bill_number": bill_number,
            "customer_id": customer_id,
            "amount": amount,
            "received_date": "2026-09-01",
            "maturity_date": "2027-03-01",
            "idempotency_key": f"p1149-{bill_number.lower()}",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _seed_supplier_credit(
    factory,
    *,
    statement_id: int,
    amount: str,
    other_supplier: bool = False,
) -> int:
    from app.models.supplier import Supplier
    from app.models.supplier_settlement import (
        SupplierCreditLot,
        SupplierMonthlyStatement,
    )
    from app.services.supplier_master import normalize_supplier_identity

    with factory() as db:
        statement = db.get(SupplierMonthlyStatement, statement_id)
        assert statement is not None
        supplier = db.get(Supplier, statement.supplier_id)
        assert supplier is not None
        if other_supplier:
            supplier = Supplier(
                standard_name="跨供应商贷项来源",
                normalized_name=normalize_supplier_identity("跨供应商贷项来源"),
                display_name="跨供应商贷项来源",
                is_active=True,
            )
            db.add(supplier)
            db.flush()
            statement = SupplierMonthlyStatement(
                statement_number=f"{statement.statement_number}-FOREIGN",
                supplier_id=supplier.id,
                supplier_name_snapshot=supplier.display_name or supplier.standard_name,
                settlement_month=statement.settlement_month,
                period_start=statement.period_start,
                period_end=statement.period_end,
                currency=statement.currency,
                tax_basis=statement.tax_basis,
                status="paid",
                erp_amount=Decimal(amount),
                adjustment_amount=Decimal("0.00"),
                adjusted_amount=Decimal(amount),
                supplier_statement_amount=Decimal(amount),
                confirmed_amount=Decimal(amount),
                invoice_allocated_amount=Decimal(amount),
                paid_amount=Decimal(amount),
                generation_origin="manual",
                generated_by=statement.generated_by,
            )
            db.add(statement)
            db.flush()
        credit = SupplierCreditLot(
            supplier_id=supplier.id,
            supplier_name_snapshot=supplier.display_name or supplier.standard_name,
            source_type="statement_adjustment",
            source_statement_id=statement.id,
            original_amount=Decimal(amount),
            available_amount=Decimal(amount),
            status="available",
            created_by=statement.generated_by,
        )
        db.add(credit)
        db.commit()
        return int(credit.id)


@pytest.fixture()
def p1_149_migrated_supplier_app(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    """Exercise migration-only guards that ``metadata.create_all`` cannot install."""
    from alembic import command
    from fastapi import FastAPI
    from sqlalchemy.orm import sessionmaker

    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models.user import User
    from tests.test_p1_149_supplier_settlement_migration import TARGET, _config

    database = tmp_path / "p1-149-adjustment-immutability.sqlite3"
    command.upgrade(_config(monkeypatch, database), TARGET)
    engine = create_sqlite_engine(database)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        db.add(
            User(
                username="p1131-admin",
                password_hash=hash_password("RolePass123!"),
                role="admin",
                real_name="Admin",
                must_change_password=False,
            )
        )
        db.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    _include_supplier_router(app)

    def override_get_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield app, factory
    finally:
        engine.dispose()


def test_supplier_settlement_day_defaults_to_twenty_and_database_rejects_out_of_range(
    requisition_app,
) -> None:
    from app.models.supplier import Supplier
    from app.services.supplier_master import normalize_supplier_identity

    _app, factory = requisition_app
    with factory() as db:
        supplier = Supplier(
            standard_name="P1-149 默认账日供应商",
            normalized_name=normalize_supplier_identity("P1-149 默认账日供应商"),
            display_name="默认账日供应商",
            is_active=True,
        )
        db.add(supplier)
        db.commit()
        db.refresh(supplier)
        assert supplier.settlement_day == 20

        supplier.settlement_day = 31
        db.commit()
        assert supplier.settlement_day == 31

        supplier.settlement_day = 0
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        supplier = db.get(Supplier, supplier.id)
        supplier.settlement_day = 32
        with pytest.raises(IntegrityError):
            db.commit()


def test_supplier_period_clamps_month_end_and_closes_only_after_cutoff_day() -> None:
    from app.services.supplier_monthly_settlement import (
        SupplierSettlementError,
        default_closed_settlement_month,
        settlement_period,
    )

    assert settlement_period("2026-02", settlement_day=31) == (
        date(2026, 2, 1),
        date(2026, 2, 28),
    )
    assert settlement_period("2024-02", settlement_day=31) == (
        date(2024, 2, 1),
        date(2024, 2, 29),
    )
    assert settlement_period("2026-03", settlement_day=31) == (
        date(2026, 3, 1),
        date(2026, 3, 31),
    )
    assert settlement_period("2026-01", settlement_day=15) == (
        date(2025, 12, 16),
        date(2026, 1, 15),
    )
    with pytest.raises(SupplierSettlementError):
        settlement_period("2026-08", settlement_day=0)
    with pytest.raises(SupplierSettlementError):
        settlement_period("2026-08", settlement_day=32)

    assert default_closed_settlement_month(
        date(2026, 8, 20), settlement_day=20
    ) == "2026-07"
    assert default_closed_settlement_month(
        date(2026, 8, 21), settlement_day=20
    ) == "2026-08"


def test_supplier_period_day_change_anchors_to_previous_actual_cycle_not_same_month_draft(
    p1_131_cost_app,
) -> None:
    from app.models.supplier import Supplier
    from app.models.supplier_settlement import SupplierMonthlyStatement
    from app.services.supplier_master import normalize_supplier_identity
    from app.services.supplier_monthly_settlement import supplier_settlement_period

    _app, factory = p1_131_cost_app
    with factory() as db:
        supplier = Supplier(
            standard_name="P1-149 改账日供应商",
            normalized_name=normalize_supplier_identity("P1-149 改账日供应商"),
            display_name="改账日供应商",
            settlement_day=20,
            is_active=True,
        )
        db.add(supplier)
        db.flush()
        db.add_all(
            [
                SupplierMonthlyStatement(
                    statement_number="P1149-ACTUAL-202608",
                    supplier_id=supplier.id,
                    supplier_name_snapshot=supplier.standard_name,
                    settlement_month="2026-08",
                    period_start=date(2026, 7, 21),
                    period_end=date(2026, 8, 20),
                    currency="CNY",
                    tax_basis="tax_inclusive",
                    status="confirmed_pending_invoice",
                    erp_amount=Decimal("100.00"),
                    adjusted_amount=Decimal("100.00"),
                    confirmed_amount=Decimal("100.00"),
                    settlement_day_snapshot=20,
                ),
                # This active September draft was generated before the master
                # cutoff changed. It is the document being replaced, not the
                # previous business cycle that should anchor regeneration.
                SupplierMonthlyStatement(
                    statement_number="P1149-STALE-DRAFT-202609",
                    supplier_id=supplier.id,
                    supplier_name_snapshot=supplier.standard_name,
                    settlement_month="2026-09",
                    period_start=date(2026, 8, 21),
                    period_end=date(2026, 9, 20),
                    currency="CNY",
                    tax_basis="tax_inclusive",
                    status="draft",
                    erp_amount=Decimal("100.00"),
                    adjusted_amount=Decimal("100.00"),
                    settlement_day_snapshot=20,
                ),
            ]
        )
        db.commit()
        supplier.settlement_day = 25
        db.commit()
        supplier_id = int(supplier.id)

    with factory() as db:
        assert supplier_settlement_period(
            db,
            supplier_id=supplier_id,
            settlement_month="2026-09",
            settlement_day=25,
        ) == (date(2026, 8, 21), date(2026, 9, 25))


def test_supplier_period_generation_waits_for_the_complete_cutoff_day(
    requisition_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.supplier_monthly_settlement as settlement_service

    app, factory = requisition_app
    _use_p181_published_map_identity(monkeypatch)
    _include_supplier_router(app)
    with TestClient(app) as client:
        _login(client, "admin")
        _paperboard_receipt(client, factory)

        monkeypatch.setattr(
            settlement_service, "beijing_today", lambda: date(2026, 8, 20)
        )
        still_open = client.post(
            "/api/finance/supplier-settlements/generate",
            json={
                "settlement_month": "2026-08",
                "idempotency_key": "p1149-cutoff-still-open",
            },
        )
        assert still_open.status_code == 409, still_open.text
        assert _error_code(still_open) == "SUPPLIER_SETTLEMENT_PERIOD_OPEN"

        monkeypatch.setattr(
            settlement_service, "beijing_today", lambda: date(2026, 8, 21)
        )
        closed = client.post(
            "/api/finance/supplier-settlements/generate",
            json={
                "settlement_month": "2026-08",
                "idempotency_key": "p1149-cutoff-closed-next-day",
            },
        )
        assert closed.status_code == 200, closed.text
        assert closed.json()["items"][0]["settlement_day_snapshot"] == 20


def test_regenerate_creates_revision_n_plus_one_and_keeps_old_statement_and_lines(
    requisition_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.models.supplier_settlement import (
        SupplierMonthlyStatement,
        SupplierMonthlyStatementLine,
    )

    app, factory = requisition_app
    _use_p181_published_map_identity(monkeypatch)
    _include_supplier_router(app)
    with TestClient(app) as client:
        _login(client, "admin")
        _paperboard_receipt(client, factory)
        generated = client.post(
            "/api/finance/supplier-settlements/generate",
            json={
                "settlement_month": "2026-08",
                "idempotency_key": "p1149-revision-one",
            },
        )
        assert generated.status_code == 200, generated.text
        revision_one = generated.json()["items"][0]
        assert revision_one["document_revision"] == 1

        # Simulate the source fingerprint changing after a late valid receipt.
        # The regeneration contract must preserve the old business document and
        # its lines even when the replacement resolves to the same source rows.
        with factory() as db:
            stale = db.get(SupplierMonthlyStatement, revision_one["id"])
            stale.source_hash = "0" * 64
            db.commit()

        payload = {
            "expected_version": revision_one["version"],
            "reason": "供应商补交本期收料明细",
            "idempotency_key": "p1149-regenerate-two",
        }
        regenerated = client.post(
            f"/api/finance/supplier-settlements/{revision_one['id']}/regenerate",
            json=payload,
        )
        assert regenerated.status_code in {200, 201}, regenerated.text
        revision_two = regenerated.json()
        assert revision_two["id"] != revision_one["id"]
        assert revision_two["document_revision"] == 2
        assert revision_two["supersedes_statement_id"] == revision_one["id"]
        assert revision_two["generation_origin"] == "regenerate"

        replay = client.post(
            f"/api/finance/supplier-settlements/{revision_one['id']}/regenerate",
            json=payload,
        )
        assert replay.status_code == regenerated.status_code, replay.text
        assert replay.json() == revision_two

    with factory() as db:
        statements = list(
            db.scalars(
                select(SupplierMonthlyStatement)
                .where(SupplierMonthlyStatement.settlement_month == "2026-08")
                .order_by(SupplierMonthlyStatement.document_revision)
            ).all()
        )
        assert [row.document_revision for row in statements] == [1, 2]
        assert statements[0].status == "voided"
        assert statements[0].active_guard is None
        assert statements[1].active_guard == 1
        lines = list(
            db.scalars(
                select(SupplierMonthlyStatementLine).order_by(
                    SupplierMonthlyStatementLine.id
                )
            ).all()
        )
        assert len(lines) == 2
        assert lines[0].source_key == lines[1].source_key
        assert lines[0].active_guard is None
        assert lines[1].active_guard == 1


def test_regenerate_uses_atomic_statement_cas_before_releasing_old_lines(
    requisition_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.models.user import User
    from app.models.supplier_settlement import (
        SupplierMonthlyStatement,
        SupplierMonthlyStatementLine,
    )
    from app.services.supplier_monthly_settlement import (
        SupplierSettlementError,
        regenerate_statement,
    )

    app, factory = requisition_app
    _use_p181_published_map_identity(monkeypatch)
    _include_supplier_router(app)
    with TestClient(app) as client:
        _login(client, "admin")
        _paperboard_receipt(client, factory)
        generated = client.post(
            "/api/finance/supplier-settlements/generate",
            json={
                "settlement_month": "2026-08",
                "idempotency_key": "p1149-regenerate-cas-seed",
            },
        )
        assert generated.status_code == 200, generated.text
        statement_id = int(generated.json()["items"][0]["id"])

    with factory() as db:
        statement = db.get(SupplierMonthlyStatement, statement_id)
        statement.source_hash = "0" * 64
        db.commit()

    with factory() as stale_db:
        stale_statement = stale_db.get(SupplierMonthlyStatement, statement_id)
        stale_version = int(stale_statement.version)
        user = stale_db.scalar(select(User).where(User.username == "admin"))
        assert user is not None

        with factory() as concurrent_db:
            result = concurrent_db.execute(
                update(SupplierMonthlyStatement)
                .where(
                    SupplierMonthlyStatement.id == statement_id,
                    SupplierMonthlyStatement.version == stale_version,
                )
                .values(version=stale_version + 1)
            )
            assert result.rowcount == 1
            concurrent_db.commit()

        with pytest.raises(SupplierSettlementError) as error:
            regenerate_statement(
                stale_db,
                statement_id=statement_id,
                expected_version=stale_version,
                reason="并发重生成测试",
                user=user,
            )
        stale_db.rollback()
        assert error.value.code == "SUPPLIER_SETTLEMENT_STALE"

    with factory() as db:
        statement = db.get(SupplierMonthlyStatement, statement_id)
        assert statement.status == "draft"
        assert statement.active_guard == 1
        assert statement.version == stale_version + 1
        assert (
            db.scalar(
                select(func.count(SupplierMonthlyStatement.id)).where(
                    SupplierMonthlyStatement.supersedes_statement_id == statement_id
                )
            )
            == 0
        )
        assert (
            db.scalar(
                select(func.count(SupplierMonthlyStatementLine.id)).where(
                    SupplierMonthlyStatementLine.statement_id == statement_id,
                    SupplierMonthlyStatementLine.active_guard == 1,
                )
            )
            > 0
        )


@pytest.mark.parametrize("with_payment", [False, True], ids=["invoice", "payment"])
def test_regenerate_rejects_statement_with_invoice_or_payment(
    p1_131_cost_app,
    with_payment: bool,
) -> None:
    from app.models.supplier_settlement import (
        SupplierMonthlyPayment,
        SupplierMonthlyStatement,
    )

    app, factory = p1_131_cost_app
    _include_supplier_router(app)
    _customer_id, statement_id = _seed_payable_statement(factory)
    if with_payment:
        with factory() as db:
            statement = db.get(SupplierMonthlyStatement, statement_id)
            statement.status = "partial_payment"
            statement.paid_amount = Decimal("100.00")
            statement.version = 2
            db.add(
                SupplierMonthlyPayment(
                    statement_id=statement.id,
                    payment_date=date(2026, 9, 2),
                    amount=Decimal("100.00"),
                    payment_method="bank",
                    reference="历史银行付款",
                    created_by=statement.generated_by,
                )
            )
            db.commit()

    with TestClient(app) as client:
        login_cost(client)
        with factory() as db:
            before = db.get(SupplierMonthlyStatement, statement_id)
            expected_version = before.version
            before_status = before.status
        response = client.post(
            f"/api/finance/supplier-settlements/{statement_id}/regenerate",
            json={
                "expected_version": expected_version,
                "reason": "不得覆盖已有正式财务事实",
                "idempotency_key": f"p1149-regenerate-block-{with_payment}",
            },
        )
        assert response.status_code == 409, response.text

    with factory() as db:
        protected = db.get(SupplierMonthlyStatement, statement_id)
        assert protected.status == before_status
        assert protected.active_guard == 1
        assert protected.document_revision == 1
        assert db.scalar(select(func.count(SupplierMonthlyStatement.id))) == 1


def test_payment_batch_applies_existing_credit_acceptance_and_bank_atomically(
    p1_131_cost_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.models.finance_simplified import FinanceAcceptanceNote
    from app.models.supplier_settlement import (
        SupplierCreditLot,
        SupplierMonthlyPayment,
        SupplierMonthlyStatement,
        SupplierPaymentBatch,
    )

    app, factory = p1_131_cost_app
    _include_all_p1_149_finance_routers(app)
    _freeze_beijing_date(monkeypatch, date(2026, 9, 3))
    customer_id, statement_id = _seed_payable_statement(factory)
    _seed_order(
        factory,
        customer_id=customer_id,
        order_number="P1149-RECENT-COMBINATION",
        order_date=date(2026, 8, 15),
    )
    credit_id = _seed_supplier_credit(
        factory, statement_id=statement_id, amount="100.00"
    )

    with TestClient(app) as client:
        login_cost(client)
        acceptance = _create_acceptance(
            client,
            customer_id=customer_id,
            bill_number="P1149-COMBINED-ACCEPTANCE",
            amount="600.00",
        )
        payload = {
            "expected_version": 1,
            "payment_date": "2026-09-03",
            "credit_applications": [
                {
                    "credit_id": credit_id,
                    "amount": "100.00",
                    "expected_version": 1,
                }
            ],
            "acceptance_note_id": acceptance["id"],
            "expected_acceptance_version": acceptance["version"],
            "bank_amount": "300.00",
            "bank_reference": "BANK-P1149-COMBINED",
            "idempotency_key": "p1149-payment-combined",
        }
        paid = client.post(
            f"/api/finance/supplier-settlements/{statement_id}/payment-batches",
            json=payload,
        )
        assert paid.status_code == 201, paid.text
        replay = client.post(
            f"/api/finance/supplier-settlements/{statement_id}/payment-batches",
            json=payload,
        )
        assert replay.status_code == 201, replay.text
        assert replay.json() == paid.json()

    with factory() as db:
        statement = db.get(SupplierMonthlyStatement, statement_id)
        assert statement.status == "paid"
        assert statement.paid_amount == Decimal("1000.00")
        payments = list(
            db.scalars(
                select(SupplierMonthlyPayment)
                .where(SupplierMonthlyPayment.statement_id == statement_id)
                .order_by(SupplierMonthlyPayment.id)
            ).all()
        )
        assert {row.payment_method: row.amount for row in payments} == {
            "credit": Decimal("100.00"),
            "acceptance": Decimal("600.00"),
            "bank": Decimal("300.00"),
        }
        assert len({row.payment_batch_id for row in payments}) == 1
        assert None not in {row.payment_batch_id for row in payments}
        assert db.scalar(select(func.count(SupplierPaymentBatch.id))) == 1
        credit = db.get(SupplierCreditLot, credit_id)
        assert credit.available_amount == Decimal("0.00")
        assert credit.status == "exhausted"
        note = db.get(FinanceAcceptanceNote, acceptance["id"])
        assert note.status == "endorsed"
        assert note.supplier_statement_id == statement_id


def test_acceptance_overage_settles_statement_and_creates_same_supplier_credit(
    p1_131_cost_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.models.finance_simplified import FinanceAcceptanceNote
    from app.models.supplier_settlement import (
        SupplierCreditLot,
        SupplierMonthlyPayment,
        SupplierMonthlyStatement,
        SupplierPaymentBatch,
    )

    app, factory = p1_131_cost_app
    _include_all_p1_149_finance_routers(app)
    _freeze_beijing_date(monkeypatch, date(2026, 9, 3))
    customer_id, statement_id = _seed_payable_statement(factory)
    _seed_order(
        factory,
        customer_id=customer_id,
        order_number="P1149-RECENT-OVERAGE",
        order_date=date(2026, 8, 15),
    )

    with TestClient(app) as client:
        login_cost(client)
        acceptance = _create_acceptance(
            client,
            customer_id=customer_id,
            bill_number="P1149-OVERAGE-ACCEPTANCE",
            amount="1200.00",
        )
        response = client.post(
            f"/api/finance/supplier-settlements/{statement_id}/payment-batches",
            json={
                "expected_version": 1,
                "payment_date": "2026-09-03",
                "credit_applications": [],
                "acceptance_note_id": acceptance["id"],
                "expected_acceptance_version": acceptance["version"],
                "bank_amount": "0",
                "idempotency_key": "p1149-acceptance-overage",
            },
        )
        assert response.status_code == 201, response.text

    with factory() as db:
        statement = db.get(SupplierMonthlyStatement, statement_id)
        note = db.get(FinanceAcceptanceNote, acceptance["id"])
        batch = db.scalar(select(SupplierPaymentBatch))
        payment = db.scalar(select(SupplierMonthlyPayment))
        credit = db.scalar(select(SupplierCreditLot))
        assert statement.status == "paid"
        assert statement.paid_amount == Decimal("1000.00")
        assert payment.payment_method == "acceptance"
        assert payment.amount == Decimal("1000.00")
        assert batch.acceptance_face_amount == Decimal("1200.00")
        assert batch.acceptance_applied_amount == Decimal("1000.00")
        assert batch.credit_created_amount == Decimal("200.00")
        assert credit.supplier_id == statement.supplier_id
        assert credit.source_acceptance_note_id == note.id
        assert credit.original_amount == Decimal("200.00")
        assert credit.available_amount == Decimal("200.00")
        assert credit.status == "available"


def test_payment_batch_rejects_bank_overpayment_and_cross_supplier_credit_without_partial_writes(
    p1_131_cost_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.models.finance_simplified import FinanceAcceptanceNote
    from app.models.supplier_settlement import (
        SupplierCreditLot,
        SupplierMonthlyPayment,
        SupplierMonthlyStatement,
        SupplierPaymentBatch,
    )

    app, factory = p1_131_cost_app
    _include_all_p1_149_finance_routers(app)
    _freeze_beijing_date(monkeypatch, date(2026, 9, 3))
    customer_id, statement_id = _seed_payable_statement(factory)
    _seed_order(
        factory,
        customer_id=customer_id,
        order_number="P1149-RECENT-ROLLBACK",
        order_date=date(2026, 8, 15),
    )
    foreign_credit_id = _seed_supplier_credit(
        factory,
        statement_id=statement_id,
        amount="100.00",
        other_supplier=True,
    )

    with TestClient(app) as client:
        login_cost(client)
        bank_overpay = client.post(
            f"/api/finance/supplier-settlements/{statement_id}/payment-batches",
            json={
                "expected_version": 1,
                "payment_date": "2026-09-03",
                "credit_applications": [],
                "bank_amount": "1000.01",
                "bank_reference": "BANK-P1149-OVERPAY",
                "idempotency_key": "p1149-bank-overpayment",
            },
        )
        assert bank_overpay.status_code == 422, bank_overpay.text

        acceptance = _create_acceptance(
            client,
            customer_id=customer_id,
            bill_number="P1149-CROSS-SUPPLIER",
            amount="600.00",
        )
        cross_supplier = client.post(
            f"/api/finance/supplier-settlements/{statement_id}/payment-batches",
            json={
                "expected_version": 1,
                "payment_date": "2026-09-03",
                "credit_applications": [
                    {
                        "credit_id": foreign_credit_id,
                        "amount": "100.00",
                        "expected_version": 1,
                    }
                ],
                "acceptance_note_id": acceptance["id"],
                "expected_acceptance_version": acceptance["version"],
                "bank_amount": "300.00",
                "bank_reference": "BANK-P1149-CROSS",
                "idempotency_key": "p1149-cross-supplier-credit",
            },
        )
        assert cross_supplier.status_code in {409, 422}, cross_supplier.text

    with factory() as db:
        statement = db.get(SupplierMonthlyStatement, statement_id)
        note = db.get(FinanceAcceptanceNote, acceptance["id"])
        credit = db.get(SupplierCreditLot, foreign_credit_id)
        assert statement.status == "invoiced_pending_payment"
        assert statement.version == 1
        assert statement.paid_amount == Decimal("0.00")
        assert note.status == "held"
        assert note.version == 1
        assert credit.available_amount == Decimal("100.00")
        assert credit.version == 1
        assert db.scalar(select(func.count(SupplierPaymentBatch.id))) == 0
        assert db.scalar(select(func.count(SupplierMonthlyPayment.id))) == 0


def test_paid_statement_adjustments_reallocate_payment_create_credit_and_are_immutable(
    p1_149_migrated_supplier_app,
) -> None:
    from app.models.finance_payable import FinancePayable
    from app.models.supplier_settlement import (
        SupplierCreditLot,
        SupplierMonthlyAdjustment,
        SupplierMonthlyPayment,
        SupplierMonthlyStatement,
    )

    app, factory = p1_149_migrated_supplier_app
    _customer_id, statement_id = _seed_payable_statement(factory)

    with TestClient(app) as client:
        login_cost(client)
        paid = client.post(
            f"/api/finance/supplier-settlements/{statement_id}/payments",
            json={
                "expected_version": 1,
                "payment_date": "2026-09-03",
                "amount": "1000.00",
                "reference": "BANK-P1149-PAID-BEFORE-ADJUSTMENT",
                "idempotency_key": "p1149-paid-before-adjustment",
            },
        )
        assert paid.status_code == 201, paid.text
        assert paid.json()["status"] == "paid"
        assert paid.json()["version"] == 2

        reduced = client.post(
            f"/api/finance/supplier-settlements/{statement_id}/adjustments",
            json={
                "expected_version": 2,
                "statement_line_id": None,
                "difference_type": "other",
                "amount": "-200.00",
                "note": "月结确认后供应商折让",
                "idempotency_key": "p1149-post-confirm-negative-adjustment",
            },
        )
        assert reduced.status_code == 201, reduced.text
        reduced_body = reduced.json()
        assert reduced_body["status"] == "paid"
        assert reduced_body["confirmed_amount"] == "800.00"
        assert reduced_body["paid_amount"] == "800.00"
        assert reduced_body["remaining_payable_amount"] == "0.00"
        assert reduced_body["version"] == 3
        negative_adjustment_id = int(reduced_body["created_adjustment_id"])
        negative_adjustment = next(
            item
            for item in reduced_body["adjustments"]
            if item["id"] == negative_adjustment_id
        )
        assert negative_adjustment["statement_version_before"] == 2
        assert negative_adjustment["amount_before"] == "1000.00"
        assert negative_adjustment["amount_after"] == "800.00"
        assert negative_adjustment["is_post_confirmation"] is True

    with factory() as db:
        statement = db.get(SupplierMonthlyStatement, statement_id)
        payable = db.get(FinancePayable, statement.finance_payable_id)
        payment = db.scalar(
            select(SupplierMonthlyPayment).where(
                SupplierMonthlyPayment.statement_id == statement_id
            )
        )
        credit = db.scalar(
            select(SupplierCreditLot).where(
                SupplierCreditLot.source_statement_id == statement_id,
                SupplierCreditLot.source_type == "statement_adjustment",
            )
        )
        adjustment = db.get(SupplierMonthlyAdjustment, negative_adjustment_id)

        assert statement.confirmed_amount == Decimal("800.00")
        assert statement.paid_amount == Decimal("800.00")
        assert payable.amount == Decimal("800.00")
        assert payable.status == "paid"
        # Payment rows are immutable cash history; paid_amount is the current
        # allocation after the post-confirmation reduction.
        assert payment.amount == Decimal("1000.00")
        assert credit.supplier_id == statement.supplier_id
        assert credit.source_type == "statement_adjustment"
        assert credit.source_statement_id == statement.id
        assert credit.original_amount == Decimal("200.00")
        assert credit.available_amount == Decimal("200.00")
        assert credit.status == "available"
        assert adjustment.statement_version_before == 2
        assert adjustment.amount_before == Decimal("1000.00")
        assert adjustment.amount_after == Decimal("800.00")
        assert adjustment.is_post_confirmation is True

        with pytest.raises(
            IntegrityError, match="supplier monthly adjustment facts are immutable"
        ):
            db.execute(
                update(SupplierMonthlyAdjustment)
                .where(SupplierMonthlyAdjustment.id == negative_adjustment_id)
                .values(note="篡改调整事实")
            )
            db.commit()
        db.rollback()
        with pytest.raises(
            IntegrityError, match="supplier monthly adjustment facts are immutable"
        ):
            db.execute(
                delete(SupplierMonthlyAdjustment).where(
                    SupplierMonthlyAdjustment.id == negative_adjustment_id
                )
            )
            db.commit()
        db.rollback()
        protected = db.get(SupplierMonthlyAdjustment, negative_adjustment_id)
        assert protected.note == "月结确认后供应商折让"

    with TestClient(app) as client:
        login_cost(client)
        increased = client.post(
            f"/api/finance/supplier-settlements/{statement_id}/adjustments",
            json={
                "expected_version": 3,
                "statement_line_id": None,
                "difference_type": "other",
                "amount": "150.00",
                "note": "月结确认后补计费用",
                "idempotency_key": "p1149-post-confirm-positive-adjustment",
            },
        )
        assert increased.status_code == 201, increased.text
        increased_body = increased.json()
        assert increased_body["status"] == "partial_payment"
        assert increased_body["confirmed_amount"] == "950.00"
        assert increased_body["paid_amount"] == "800.00"
        assert increased_body["remaining_payable_amount"] == "150.00"
        assert increased_body["version"] == 4
        positive_adjustment = next(
            item
            for item in increased_body["adjustments"]
            if item["id"] == increased_body["created_adjustment_id"]
        )
        assert positive_adjustment["statement_version_before"] == 3
        assert positive_adjustment["amount_before"] == "800.00"
        assert positive_adjustment["amount_after"] == "950.00"
        assert positive_adjustment["is_post_confirmation"] is True

    with factory() as db:
        statement = db.get(SupplierMonthlyStatement, statement_id)
        payable = db.get(FinancePayable, statement.finance_payable_id)
        assert statement.status == "partial_payment"
        assert statement.confirmed_amount == Decimal("950.00")
        assert statement.paid_amount == Decimal("800.00")
        assert payable.status == "confirmed"
        assert payable.amount == Decimal("950.00")
        assert (
            db.scalar(
                select(func.count(SupplierCreditLot.id)).where(
                    SupplierCreditLot.source_statement_id == statement_id,
                    SupplierCreditLot.source_type == "statement_adjustment",
                )
            )
            == 1
        )


def test_negative_adjustment_that_closes_partial_payment_sets_payable_audit_fields(
    p1_149_migrated_supplier_app,
) -> None:
    from app.models.finance_payable import FinancePayable
    from app.models.supplier_settlement import (
        SupplierMonthlyPayment,
        SupplierMonthlyStatement,
    )

    app, factory = p1_149_migrated_supplier_app
    _customer_id, statement_id = _seed_payable_statement(factory)

    with TestClient(app) as client:
        login_cost(client)
        payment = client.post(
            f"/api/finance/supplier-settlements/{statement_id}/payments",
            json={
                "expected_version": 1,
                "payment_date": "2026-09-03",
                "amount": "600.00",
                "reference": "BANK-P1149-PARTIAL-ADJUSTMENT",
                "idempotency_key": "p1149-partial-before-negative-adjustment",
            },
        )
        assert payment.status_code == 201, payment.text
        assert payment.json()["status"] == "partial_payment"

        adjusted = client.post(
            f"/api/finance/supplier-settlements/{statement_id}/adjustments",
            json={
                "expected_version": 2,
                "statement_line_id": None,
                "difference_type": "other",
                "amount": "-400.00",
                "note": "供应商折让后恰好结清",
                "idempotency_key": "p1149-partial-negative-closes-payable",
            },
        )
        assert adjusted.status_code == 201, adjusted.text
        assert adjusted.json()["status"] == "paid"
        assert adjusted.json()["confirmed_amount"] == "600.00"
        assert adjusted.json()["paid_amount"] == "600.00"

    with factory() as db:
        statement = db.get(SupplierMonthlyStatement, statement_id)
        payable = db.get(FinancePayable, statement.finance_payable_id)
        assert payable.status == "paid"
        assert payable.amount == Decimal("600.00")
        assert payable.paid_by is not None
        assert payable.paid_at is not None
        assert (
            db.scalar(
                select(func.count(SupplierMonthlyPayment.id)).where(
                    SupplierMonthlyPayment.statement_id == statement_id
                )
            )
            == 1
        )


def test_acceptance_metadata_and_create_both_enforce_recent_valid_order_window(
    p1_131_cost_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.models.customer import Customer
    from app.models.finance_simplified import FinanceAcceptanceNote
    from app.models.order import Order

    app, factory = p1_131_cost_app
    enable_simple_finance_router(app)
    _freeze_beijing_date(monkeypatch, date(2026, 9, 3))
    recent_customer_id, _statement_id = _seed_payable_statement(factory)
    with factory() as db:
        customers = [
            Customer(
                name="P1-149 过期订单客户",
                customer_code="P1149-OLD",
                chinese_short_name="过期客户",
                is_active=True,
            ),
            Customer(
                name="P1-149 作废订单客户",
                customer_code="P1149-CANCELLED",
                chinese_short_name="作废客户",
                is_active=True,
            ),
            Customer(
                name="P1-149 停用客户",
                customer_code="P1149-INACTIVE",
                chinese_short_name="停用客户",
                is_active=False,
                status="inactive",
            ),
        ]
        db.add_all(customers)
        db.flush()
        old_id, cancelled_id, inactive_id = (row.id for row in customers)
        db.add_all(
            [
                Order(
                    order_number="P1149-ORDER-OLD",
                    customer_id=old_id,
                    order_date=date(2026, 5, 1),
                    status="completed",
                    payment_status="paid",
                    total_amount=Decimal("100.00"),
                ),
                Order(
                    order_number="P1149-ORDER-CANCELLED",
                    customer_id=cancelled_id,
                    order_date=date(2026, 8, 20),
                    status="cancelled",
                    payment_status="unpaid",
                    total_amount=Decimal("100.00"),
                ),
                Order(
                    order_number="P1149-ORDER-INACTIVE",
                    customer_id=inactive_id,
                    order_date=date(2026, 8, 20),
                    status="pending_production",
                    payment_status="unpaid",
                    total_amount=Decimal("100.00"),
                ),
            ]
        )
        db.commit()
    recent_order_id = _seed_order(
        factory,
        customer_id=recent_customer_id,
        order_number="P1149-ORDER-RECENT",
        order_date=date(2026, 8, 20),
    )

    with TestClient(app) as client:
        login_cost(client)
        metadata = client.get("/api/finance/simple-finance/metadata")
        assert metadata.status_code == 200, metadata.text
        candidate_ids = {row["id"] for row in metadata.json()["customers"]}
        assert candidate_ids == {recent_customer_id}

        rejected = client.post(
            "/api/finance/simple-finance/acceptances",
            json={
                "bill_number": "P1149-OLD-CUSTOMER-REJECTED",
                "customer_id": old_id,
                "amount": "100.00",
                "received_date": "2026-09-01",
                "maturity_date": "2027-03-01",
                "idempotency_key": "p1149-old-customer-rejected",
            },
        )
        assert rejected.status_code in {409, 422}, rejected.text

        accepted = _create_acceptance(
            client,
            customer_id=recent_customer_id,
            bill_number="P1149-RECENT-CUSTOMER",
            amount="100.00",
        )

        with factory() as db:
            order = db.get(Order, recent_order_id)
            order.order_date = date(2026, 5, 1)
            db.commit()

        refreshed = client.get("/api/finance/simple-finance/metadata")
        assert recent_customer_id not in {
            row["id"] for row in refreshed.json()["customers"]
        }
        history = client.get("/api/finance/simple-finance/acceptances")
        assert history.status_code == 200, history.text
        assert accepted["id"] in {row["id"] for row in history.json()["items"]}

    with factory() as db:
        assert db.scalar(select(func.count(FinanceAcceptanceNote.id))) == 1


def test_combined_utility_expense_is_one_per_month_and_mutually_exclusive_with_legacy_rows(
    p1_131_cost_app,
) -> None:
    from app.models.finance_simplified import (
        FinanceUtilityExpense,
        FinanceUtilityReading,
    )

    app, factory = p1_131_cost_app
    enable_simple_finance_router(app)
    from tests.test_p1_144_simplified_finance import _centers

    centers = _centers(factory)
    with TestClient(app) as client:
        login_cost(client)
        legacy_water = client.post(
            "/api/finance/simple-finance/utility-readings",
            json={
                "cost_month": "2026-08",
                "utility_type": "water",
                "cost_center_id": centers["PROD"],
                "previous_reading": "100.000",
                "current_reading": "110.000",
                "unit_price": "4.0000",
                "paid_amount": "0",
                "idempotency_key": "p1149-legacy-water",
            },
        )
        assert legacy_water.status_code == 200, legacy_water.text
        blocked_combined = client.post(
            "/api/finance/simple-finance/utility-expenses",
            json={
                "cost_month": "2026-08",
                "cost_center_id": centers["PROD"],
                "total_amount": "500.00",
                "paid_amount": "0",
                "idempotency_key": "p1149-combined-blocked-by-legacy",
            },
        )
        assert blocked_combined.status_code == 409, blocked_combined.text

        payload = {
            "cost_month": "2026-09",
            "cost_center_id": centers["PROD"],
            "total_amount": "500.00",
            "invoice_number": "UTILITY-202609",
            "invoice_date": "2026-09-30",
            "invoice_amount": "500.00",
            "paid_amount": "200.00",
            "payment_date": "2026-10-05",
            "note": "九月水电合并账单",
            "idempotency_key": "p1149-combined-utility-september",
        }
        combined = client.post(
            "/api/finance/simple-finance/utility-expenses", json=payload
        )
        assert combined.status_code in {200, 201}, combined.text
        replay = client.post(
            "/api/finance/simple-finance/utility-expenses", json=payload
        )
        assert replay.status_code == combined.status_code, replay.text
        assert replay.json() == combined.json()

        duplicate = client.post(
            "/api/finance/simple-finance/utility-expenses",
            json={
                **payload,
                "total_amount": "501.00",
                "invoice_amount": "501.00",
                "idempotency_key": "p1149-combined-utility-duplicate",
            },
        )
        assert duplicate.status_code in {409, 422}, duplicate.text

        blocked_legacy = client.post(
            "/api/finance/simple-finance/utility-readings",
            json={
                "cost_month": "2026-09",
                "utility_type": "electricity",
                "cost_center_id": centers["PROD"],
                "previous_reading": "100.000",
                "current_reading": "110.000",
                "unit_price": "1.0000",
                "paid_amount": "0",
                "idempotency_key": "p1149-legacy-blocked-by-combined",
            },
        )
        assert blocked_legacy.status_code == 409, blocked_legacy.text

    with factory() as db:
        combined_rows = list(db.scalars(select(FinanceUtilityExpense)).all())
        legacy_rows = list(db.scalars(select(FinanceUtilityReading)).all())
        assert len(combined_rows) == 1
        assert combined_rows[0].cost_month == "2026-09"
        assert combined_rows[0].total_amount == Decimal("500.00")
        assert combined_rows[0].paid_amount == Decimal("200.00")
        assert [(row.cost_month, row.utility_type) for row in legacy_rows] == [
            ("2026-08", "water")
        ]
