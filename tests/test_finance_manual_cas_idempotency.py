from __future__ import annotations

from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from pathlib import Path
from threading import Barrier

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture()
def manual_finance_app(tmp_path: Path):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.finance import router as finance_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.customer import Customer
    from app.models.finance import Statement
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "manual-finance.sqlite3")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as session:
        users = [
            User(
                username=username,
                password_hash=hash_password("RolePass123!"),
                role=role,
                real_name=username,
                display_name=username,
                must_change_password=False,
            )
            for username, role in (
                ("finance_a", "finance"),
                ("finance_b", "finance"),
                ("sales_user", "sales"),
            )
        ]
        customer = Customer(
            customer_number=1,
            customer_code="FIN-CAS",
            name="苏州思迈尔包装有限公司",
            payment_term_days=30,
            credit_limit=Decimal("100000"),
        )
        session.add_all([*users, customer])
        session.flush()
        session.add_all(
            [
                Statement(
                    statement_number="ST-202608-CAS-001",
                    customer_id=customer.id,
                    statement_month="2026-08",
                    total_receivable=Decimal("100.00"),
                    total_gross_profit=Decimal("20.00"),
                    invoiced_amount=Decimal("0.00"),
                    settled_amount=Decimal("0.00"),
                    status="unsettled",
                    confirmation_status="confirmed",
                    version=1,
                    confirmed_by=users[0].id,
                    created_by=users[0].id,
                ),
                Statement(
                    statement_number="ST-202608-DRAFT-002",
                    customer_id=customer.id,
                    statement_month="2026-08",
                    total_receivable=Decimal("100.00"),
                    total_gross_profit=Decimal("20.00"),
                    invoiced_amount=Decimal("0.00"),
                    settled_amount=Decimal("0.00"),
                    status="unsettled",
                    confirmation_status="draft",
                    version=1,
                    created_by=users[0].id,
                ),
            ]
        )
        session.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(finance_router, prefix="/api/finance")

    def override_get_db() -> Generator[Session, None, None]:
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    return app, session_factory


def _login(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": "RolePass123!"},
    )
    assert response.status_code == 200, response.text


def _invoice_payload(
    *,
    key: str,
    expected_version: int = 1,
    expected_ledger_version: int = 1,
    invoice_number: str = "INV-CAS-001",
    amount: str = "60.00",
) -> dict:
    return {
        "statement_id": 1,
        "invoice_number": invoice_number,
        "invoice_date": "2026-08-27",
        "invoice_amount": amount,
        "expected_version": expected_version,
        "expected_ledger_version": expected_ledger_version,
        "idempotency_key": key,
    }


def _settlement_payload(
    *,
    key: str,
    expected_version: int = 1,
    expected_ledger_version: int = 1,
    amount: str = "40.00",
    account: str | None = "中国银行 6688",
) -> dict:
    payload = {
        "amount": amount,
        "settlement_date": "2026-08-27",
        "expected_version": expected_version,
        "expected_ledger_version": expected_ledger_version,
        "idempotency_key": key,
    }
    if account is not None:
        payload["account"] = account
    return payload


@pytest.mark.parametrize(
    "key",
    [
        "system:invoice-task-result:42",
        " SYSTEM:invoice-task-result:42 ",
        "SyStEm:any-future-reserved-key",
    ],
)
def test_manual_finance_rejects_system_reserved_idempotency_namespace(
    manual_finance_app,
    key: str,
) -> None:
    from app.models.finance import FinanceManualMutation, Invoice, SettlementRecord

    app, session_factory = manual_finance_app
    with TestClient(app) as client:
        _login(client, "finance_a")
        invoice = client.post(
            "/api/finance/invoices",
            json=_invoice_payload(key=key),
        )
        settlement = client.put(
            "/api/finance/statements/1/settle",
            json=_settlement_payload(key=key),
        )

    assert invoice.status_code == 422, invoice.text
    assert settlement.status_code == 422, settlement.text
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Invoice)) == 0
        assert session.scalar(select(func.count()).select_from(SettlementRecord)) == 0
        assert (
            session.scalar(select(func.count()).select_from(FinanceManualMutation))
            == 0
        )


def test_invoice_exact_replay_and_key_conflicts_are_database_backed(
    manual_finance_app,
) -> None:
    from app.models.audit import OperationLog
    from app.models.finance import FinanceManualMutation, Invoice, Statement

    app, session_factory = manual_finance_app
    payload = _invoice_payload(key="manual-invoice-cas-001")
    with TestClient(app) as client:
        _login(client, "finance_a")
        created = client.post("/api/finance/invoices", json=payload)
        replay = client.post("/api/finance/invoices", json=payload)
        changed_payload = client.post(
            "/api/finance/invoices",
            json={**payload, "invoice_amount": "59.00"},
        )
        _login(client, "finance_b")
        changed_operator = client.post("/api/finance/invoices", json=payload)

    assert created.status_code == 201, created.text
    assert replay.status_code == 201, replay.text
    assert replay.json() == created.json()
    assert created.json()["version"] == 1
    assert created.json()["ledger_version"] == 2
    assert changed_payload.status_code == 409
    assert "幂等键" in str(changed_payload.json()["detail"])
    assert changed_operator.status_code == 409
    assert "幂等键" in str(changed_operator.json()["detail"])
    with session_factory() as session:
        statement = session.get(Statement, 1)
        assert statement.invoiced_amount == Decimal("60.00")
        assert statement.version == 1
        assert statement.ledger_version == 2
        assert session.scalar(select(func.count()).select_from(Invoice)) == 1
        assert (
            session.scalar(select(func.count()).select_from(FinanceManualMutation))
            == 1
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(OperationLog)
                .where(OperationLog.action == "REGISTER_INVOICE")
            )
            == 1
        )


def test_partial_and_full_settlement_increment_version_once_per_fact(
    manual_finance_app,
) -> None:
    from app.models.audit import OperationLog
    from app.models.finance import FinanceManualMutation, SettlementRecord, Statement

    app, session_factory = manual_finance_app
    partial_payload = _settlement_payload(key="manual-payment-cas-001")
    with TestClient(app) as client:
        _login(client, "finance_a")
        partial = client.put(
            "/api/finance/statements/1/settle", json=partial_payload
        )
        replay = client.put(
            "/api/finance/statements/1/settle", json=partial_payload
        )
        full = client.put(
            "/api/finance/statements/1/settle",
            json=_settlement_payload(
                key="manual-payment-cas-002",
                expected_ledger_version=2,
                amount="60.00",
                account=None,
            ),
        )

    assert partial.status_code == 200, partial.text
    assert replay.status_code == 200, replay.text
    assert replay.json() == partial.json()
    assert partial.json()["version"] == 1
    assert partial.json()["ledger_version"] == 2
    assert full.status_code == 200, full.text
    assert full.json()["version"] == 1
    assert full.json()["ledger_version"] == 3
    assert full.json()["status"] == "settled"
    with session_factory() as session:
        statement = session.get(Statement, 1)
        assert statement.settled_amount == Decimal("100.00")
        assert statement.version == 1
        assert statement.ledger_version == 3
        assert (
            session.scalar(select(func.count()).select_from(SettlementRecord)) == 2
        )
        assert (
            session.scalar(select(func.count()).select_from(FinanceManualMutation))
            == 2
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(OperationLog)
                .where(OperationLog.action == "SETTLE_STATEMENT")
            )
            == 2
        )


def test_draft_stale_and_amount_cap_fail_without_orphan_facts(
    manual_finance_app,
) -> None:
    from app.models.audit import OperationLog
    from app.models.finance import (
        FinanceManualMutation,
        Invoice,
        SettlementRecord,
        Statement,
    )

    app, session_factory = manual_finance_app
    with TestClient(app) as client:
        _login(client, "finance_a")
        draft_invoice = client.post(
            "/api/finance/invoices",
            json={
                **_invoice_payload(key="manual-draft-invoice-001"),
                "statement_id": 2,
            },
        )
        draft_settlement = client.put(
            "/api/finance/statements/2/settle",
            json=_settlement_payload(key="manual-draft-payment-001"),
        )
        stale = client.put(
            "/api/finance/statements/1/settle",
            json=_settlement_payload(
                key="manual-stale-payment-001", expected_ledger_version=9
            ),
        )
        over_cap = client.post(
            "/api/finance/invoices",
            json=_invoice_payload(
                key="manual-over-invoice-001", amount="100.01"
            ),
        )

    assert draft_invoice.status_code == 409
    assert draft_settlement.status_code == 409
    assert stale.status_code == 409
    assert over_cap.status_code == 409
    with session_factory() as session:
        confirmed = session.get(Statement, 1)
        draft = session.get(Statement, 2)
        assert (
            confirmed.invoiced_amount,
            confirmed.settled_amount,
            confirmed.version,
            confirmed.ledger_version,
        ) == (
            Decimal("0.00"),
            Decimal("0.00"),
            1,
            1,
        )
        assert (
            draft.invoiced_amount,
            draft.settled_amount,
            draft.version,
            draft.ledger_version,
        ) == (
            Decimal("0.00"),
            Decimal("0.00"),
            1,
            1,
        )
        assert session.scalar(select(func.count()).select_from(Invoice)) == 0
        assert (
            session.scalar(select(func.count()).select_from(SettlementRecord)) == 0
        )
        assert (
            session.scalar(select(func.count()).select_from(FinanceManualMutation))
            == 0
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(OperationLog)
                .where(
                    OperationLog.action.in_(
                        ["REGISTER_INVOICE", "SETTLE_STATEMENT"]
                    )
                )
            )
            == 0
        )


def test_concurrent_same_version_allows_only_one_distinct_invoice(
    manual_finance_app,
) -> None:
    from app.models.finance import FinanceManualMutation, Invoice, Statement

    app, session_factory = manual_finance_app
    barrier = Barrier(3)
    with TestClient(app) as first_client, TestClient(app) as second_client:
        _login(first_client, "finance_a")
        _login(second_client, "finance_a")

        def submit(client: TestClient, payload: dict):
            barrier.wait()
            return client.post("/api/finance/invoices", json=payload)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(
                    submit,
                    first_client,
                    _invoice_payload(
                        key="manual-race-invoice-001",
                        invoice_number="INV-RACE-001",
                    ),
                ),
                pool.submit(
                    submit,
                    second_client,
                    _invoice_payload(
                        key="manual-race-invoice-002",
                        invoice_number="INV-RACE-002",
                    ),
                ),
            ]
            barrier.wait()
            responses = [future.result() for future in futures]

    assert sorted(response.status_code for response in responses) == [201, 409]
    with session_factory() as session:
        statement = session.get(Statement, 1)
        assert statement.invoiced_amount == Decimal("60.00")
        assert statement.version == 1
        assert statement.ledger_version == 2
        assert session.scalar(select(func.count()).select_from(Invoice)) == 1
        assert (
            session.scalar(select(func.count()).select_from(FinanceManualMutation))
            == 1
        )


def test_concurrent_same_key_replays_one_committed_invoice(
    manual_finance_app,
) -> None:
    from app.models.finance import FinanceManualMutation, Invoice, Statement

    app, session_factory = manual_finance_app
    payload = _invoice_payload(
        key="manual-race-replay-001", invoice_number="INV-RACE-REPLAY-001"
    )
    barrier = Barrier(3)
    with TestClient(app) as first_client, TestClient(app) as second_client:
        _login(first_client, "finance_a")
        _login(second_client, "finance_a")

        def submit(client: TestClient):
            barrier.wait()
            return client.post("/api/finance/invoices", json=payload)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(submit, first_client),
                pool.submit(submit, second_client),
            ]
            barrier.wait()
            responses = [future.result() for future in futures]

    assert [response.status_code for response in responses] == [201, 201]
    assert responses[0].json() == responses[1].json()
    with session_factory() as session:
        statement = session.get(Statement, 1)
        assert statement.version == 1
        assert statement.ledger_version == 2
        assert session.scalar(select(func.count()).select_from(Invoice)) == 1
        assert (
            session.scalar(select(func.count()).select_from(FinanceManualMutation))
            == 1
        )


def test_audit_failure_rolls_back_statement_business_and_idempotency_fact(
    manual_finance_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.api.finance as finance_api
    from app.models.audit import OperationLog
    from app.models.finance import FinanceManualMutation, Invoice, Statement

    app, session_factory = manual_finance_app

    def fail_audit(*_args, **_kwargs) -> None:
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(finance_api, "_audit", fail_audit)
    with TestClient(app, raise_server_exceptions=False) as client:
        _login(client, "finance_a")
        response = client.post(
            "/api/finance/invoices",
            json=_invoice_payload(key="manual-audit-rollback-001"),
        )

    assert response.status_code == 500
    with session_factory() as session:
        statement = session.get(Statement, 1)
        assert statement.invoiced_amount == Decimal("0.00")
        assert statement.version == 1
        assert statement.ledger_version == 1
        assert session.scalar(select(func.count()).select_from(Invoice)) == 0
        assert (
            session.scalar(select(func.count()).select_from(FinanceManualMutation))
            == 0
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(OperationLog)
                .where(OperationLog.action == "REGISTER_INVOICE")
            )
            == 0
        )


def test_manual_finance_mutations_keep_finance_permission_gate(
    manual_finance_app,
) -> None:
    app, _ = manual_finance_app
    with TestClient(app) as client:
        _login(client, "sales_user")
        invoice = client.post(
            "/api/finance/invoices",
            json=_invoice_payload(key="manual-forbidden-invoice-001"),
        )
        settlement = client.put(
            "/api/finance/statements/1/settle",
            json=_settlement_payload(key="manual-forbidden-payment-001"),
        )

    assert invoice.status_code == 403
    assert settlement.status_code == 403


def test_source_and_ledger_preconditions_are_both_required_and_can_differ(
    manual_finance_app,
) -> None:
    from app.models.finance import FinanceManualMutation, Statement

    app, session_factory = manual_finance_app
    missing_ledger = _invoice_payload(key="manual-ledger-required-001")
    missing_ledger.pop("expected_ledger_version")
    missing_source = _invoice_payload(key="manual-source-required-001")
    missing_source.pop("expected_version")
    with session_factory() as session:
        statement = session.get(Statement, 1)
        statement.version = 2
        session.commit()
    with TestClient(app) as client:
        _login(client, "finance_a")
        rejected_ledger = client.post(
            "/api/finance/invoices", json=missing_ledger
        )
        rejected_source = client.post(
            "/api/finance/invoices", json=missing_source
        )
        accepted = client.put(
            "/api/finance/statements/1/settle",
            json=_settlement_payload(
                key="manual-dual-version-001",
                expected_version=2,
                expected_ledger_version=1,
            ),
        )
        listed = client.get("/api/finance/statements")
        detailed = client.get("/api/finance/statements/1")

    assert rejected_ledger.status_code == 422
    assert rejected_source.status_code == 422
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["version"] == 2
    assert accepted.json()["ledger_version"] == 2
    assert listed.status_code == 200, listed.text
    listed_statement = next(
        item for item in listed.json()["items"] if item["id"] == 1
    )
    assert listed_statement["version"] == 2
    assert listed_statement["ledger_version"] == 2
    assert detailed.status_code == 200, detailed.text
    assert detailed.json()["ledger_version"] == 2
    with session_factory() as session:
        statement = session.get(Statement, 1)
        assert statement.version == 2
        assert statement.ledger_version == 2
        assert (
            session.scalar(select(func.count()).select_from(FinanceManualMutation))
            == 1
        )
