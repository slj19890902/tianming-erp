from __future__ import annotations

from collections.abc import Generator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session, sessionmaker


PASSWORD = "Opt10Quotation123!"
INTERNAL_COST_FIELDS = {
    "estimated_unit_cost",
    "estimated_gross_profit",
    "margin_rate",
    "material_square_price",
    "material_effective_square_price",
    "suggested_unit_price",
}


@pytest.fixture(scope="module")
def quotation_scope_app(tmp_path_factory: pytest.TempPathFactory):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.quotations import router as quotations_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.access_control import UserCustomerScope
    from app.models.customer import Customer
    from app.models.quotation import QuotationItem, QuotationOrder
    from app.models.user import User

    root = tmp_path_factory.mktemp("opt10-quotation-scope")
    engine = create_sqlite_engine(root / "opt10-quotation-scope.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        scoped = User(
            username="opt10-scoped",
            password_hash=hash_password(PASSWORD),
            role="sales",
            real_name="Scoped sales",
            must_change_password=False,
            customer_access_mode="selected",
        )
        empty = User(
            username="opt10-empty",
            password_hash=hash_password(PASSWORD),
            role="sales",
            real_name="Empty scope sales",
            must_change_password=False,
            customer_access_mode="selected",
        )
        all_scope = User(
            username="opt10-all",
            password_hash=hash_password(PASSWORD),
            role="sales",
            real_name="All customer sales",
            must_change_password=False,
            customer_access_mode="all",
        )
        customers = [Customer(name=f"OPT10 customer {index:02d}") for index in range(26)]
        db.add_all([scoped, empty, all_scope, *customers])
        db.flush()
        db.add(UserCustomerScope(user_id=scoped.id, customer_id=customers[0].id))
        for index, customer in enumerate(customers):
            quotation = QuotationOrder(
                quotation_no=f"OPT10-Q-{index:03d}",
                customer_id=customer.id,
                customer_name=customer.name,
                quotation_date=date(2026, 9, 30),
                status="draft",
                total_amount=Decimal("250.00"),
                created_by=scoped.id,
            )
            quotation.items.append(
                QuotationItem(
                    product_name=f"Scoped quotation carton {index:02d}",
                    box_type="A1/0201",
                    quantity=100,
                    estimated_unit_cost=Decimal("1.5000"),
                    margin_rate=Decimal("20.0000"),
                    suggested_unit_price=Decimal("2.0000"),
                    final_unit_price=Decimal("2.5000"),
                )
            )
            db.add(quotation)
        db.commit()
        ids = {
            "visible_customer": customers[0].id,
            "hidden_customer": customers[1].id,
            "quotation_count": len(customers),
        }

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(quotations_router, prefix="/api/quotations")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield app, factory, engine, ids
    finally:
        engine.dispose()


def _login(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text


def _scope_denial_count(factory: sessionmaker) -> int:
    from app.models.audit import OperationLog

    with factory() as db:
        return int(
            db.scalar(
                select(func.count())
                .select_from(OperationLog)
                .where(OperationLog.action_code == "customer_scope.denied")
            )
            or 0
        )


def _measured_get(client: TestClient, engine, url: str):
    statements: list[str] = []

    def record_sql(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record_sql)
    try:
        response = client.get(url)
    finally:
        event.remove(engine, "before_cursor_execute", record_sql)
    select_count = sum(
        statement.lstrip().lower().startswith("select") for statement in statements
    )
    return response, select_count


def test_selected_scope_filters_in_sql_with_bounded_queries(
    quotation_scope_app,
) -> None:
    app, _factory, engine, ids = quotation_scope_app
    with TestClient(app) as client:
        _login(client, "opt10-scoped")
        response, select_count = _measured_get(client, engine, "/api/quotations")

    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["customer_id"] == ids["visible_customer"]
    assert select_count <= 8


def test_selected_scope_list_does_not_record_false_customer_denials(
    quotation_scope_app,
) -> None:
    app, factory, _engine, ids = quotation_scope_app
    with TestClient(app) as client:
        _login(client, "opt10-scoped")
        before = _scope_denial_count(factory)
        response = client.get("/api/quotations")

    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["customer_id"] == ids["visible_customer"]
    assert _scope_denial_count(factory) == before


def test_empty_selected_scope_returns_empty_without_scanning_or_false_denials(
    quotation_scope_app,
) -> None:
    app, factory, engine, _ids = quotation_scope_app
    with TestClient(app) as client:
        _login(client, "opt10-empty")
        before = _scope_denial_count(factory)
        response, select_count = _measured_get(client, engine, "/api/quotations")

    assert response.status_code == 200, response.text
    assert response.json() == {"items": [], "total": 0}
    assert _scope_denial_count(factory) == before
    assert select_count <= 8


def test_explicit_out_of_scope_customer_still_returns_403_and_one_real_audit(
    quotation_scope_app,
) -> None:
    app, factory, _engine, ids = quotation_scope_app
    with TestClient(app) as client:
        _login(client, "opt10-scoped")
        before = _scope_denial_count(factory)
        response = client.get(
            f"/api/quotations?customer_id={ids['hidden_customer']}"
        )

    assert response.status_code == 403
    assert _scope_denial_count(factory) == before + 1


def test_all_customer_scope_keeps_full_list_and_redacts_internal_costs(
    quotation_scope_app,
) -> None:
    app, factory, engine, ids = quotation_scope_app
    with TestClient(app) as client:
        _login(client, "opt10-all")
        before = _scope_denial_count(factory)
        response, select_count = _measured_get(client, engine, "/api/quotations")

    assert response.status_code == 200, response.text
    assert response.json()["total"] == ids["quotation_count"]
    assert select_count <= 8
    for quotation in response.json()["items"]:
        assert INTERNAL_COST_FIELDS.isdisjoint(quotation["items"][0])
    assert _scope_denial_count(factory) == before
