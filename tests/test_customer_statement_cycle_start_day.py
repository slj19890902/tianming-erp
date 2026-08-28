from __future__ import annotations

import sqlite3
from collections.abc import Generator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture()
def customer_api_app(tmp_path: Path) -> FastAPI:
    from app.api.auth import router as auth_router
    from app.api.customers import router as customers_router
    from app.api.deps import get_db
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "customer_api.sqlite3")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as session:
        session.add(
            User(
                username="admin",
                password_hash=hash_password("RolePass123!"),
                role="admin",
                real_name="Admin",
                display_name="Admin",
                must_change_password=False,
            )
        )
        session.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(customers_router, prefix="/api/master/customers")

    def override_get_db() -> Generator[Session, None, None]:
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    return app


def _login(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "RolePass123!"},
    )
    assert response.status_code == 200


def test_customer_payload_defaults_and_validates_statement_cycle_start_day() -> None:
    from app.api.customers import CustomerPayload

    payload = CustomerPayload(
        customer_number=1,
        customer_code="TEST",
        name="Test Customer",
    )

    assert payload.statement_cycle_start_day == 20
    for invalid_day in (0, 29):
        with pytest.raises(ValidationError):
            CustomerPayload(
                customer_number=1,
                customer_code="TEST",
                name="Test Customer",
                statement_cycle_start_day=invalid_day,
            )


def test_customer_api_reads_and_writes_statement_cycle_start_day(
    customer_api_app: FastAPI,
) -> None:
    with TestClient(customer_api_app) as client:
        _login(client)
        created_default = client.post(
            "/api/master/customers",
            json={
                "customer_number": 1,
                "customer_code": "DEFAULT",
                "name": "Default Cycle Customer",
            },
        )
        created_custom = client.post(
            "/api/master/customers",
            json={
                "customer_number": 2,
                "customer_code": "CUSTOM",
                "name": "Custom Cycle Customer",
                "statement_cycle_start_day": 28,
            },
        )
        customer_id = created_custom.json()["id"]
        update_payload = {
            "customer_number": 2,
            "customer_code": "CUSTOM",
            "name": "Custom Cycle Customer",
            "statement_cycle_start_day": 1,
            "expected_version": 1,
            "change_reason": "调整客户对账周期起始日",
        }
        update_preview = client.put(
            f"/api/master/customers/{customer_id}",
            json=update_payload,
        )
        updated = client.put(
            f"/api/master/customers/{customer_id}",
            json={
                **update_payload,
                "confirmation_token": update_preview.json()["detail"][
                    "confirmation_token"
                ],
            },
        )
        invalid = client.post(
            "/api/master/customers",
            json={
                "customer_number": 3,
                "customer_code": "INVALID",
                "name": "Invalid Cycle Customer",
                "statement_cycle_start_day": 29,
            },
        )
        listed = client.get("/api/master/customers")

    assert created_default.status_code == 201, created_default.text
    assert created_default.json()["statement_cycle_start_day"] == 20
    assert created_custom.status_code == 201, created_custom.text
    assert created_custom.json()["statement_cycle_start_day"] == 28
    assert updated.status_code == 200, updated.text
    assert updated.json()["statement_cycle_start_day"] == 1
    assert invalid.status_code == 422
    assert {
        item["customer_code"]: item["statement_cycle_start_day"]
        for item in listed.json()["items"]
    } == {"DEFAULT": 20, "CUSTOM": 1}


def test_customer_statement_cycle_start_day_migration_upgrade_and_downgrade(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "customer_statement_cycle.sqlite3"
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("ERP_SECRET_KEY", "customer-statement-cycle-test")
    project_root = Path(__file__).resolve().parents[1]
    config = Config(str(project_root / "alembic.ini"))

    command.upgrade(config, "aq44v7w8x9m34")

    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1]: row for row in connection.execute("PRAGMA table_info(customers)")
        }
        connection.execute("INSERT INTO customers (name) VALUES ('Migration Default')")
        default_day = connection.execute(
            "SELECT statement_cycle_start_day FROM customers WHERE name = ?",
            ("Migration Default",),
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO customers (name, statement_cycle_start_day) "
                "VALUES ('Invalid Migration Day', 0)"
            )
        connection.rollback()

    assert columns["statement_cycle_start_day"][3] == 1
    assert columns["statement_cycle_start_day"][4] == "20"
    assert default_day == 20

    command.downgrade(config, "an41v7w8x9j31")
    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(customers)")
        }
        version = connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    assert "statement_cycle_start_day" not in columns
    assert version == "an41v7w8x9j31"


def test_customer_statement_cycle_start_day_round_trip_through_last_data_independent_revision(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "customer_statement_cycle_round_trip.sqlite3"
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("ERP_SECRET_KEY", "customer-statement-cycle-round-trip")
    # This test uses a throw-away SQLite file and intentionally exercises a
    # full historical downgrade.  N031 correctly refuses that operation in a
    # real environment unless the operational acknowledgement is explicit.
    monkeypatch.setenv(
        "N031_AUTH_VERSION_DOWNGRADE_CONFIRM",
        "DOWNTIME_COMPLETE_AND_SESSION_SECRET_ROTATED",
    )
    project_root = Path(__file__).resolve().parents[1]
    config = Config(str(project_root / "alembic.ini"))
    # fj45 and later intentionally require the owner-confirmed RAW-001 rack.
    # Keep this blank-database schema round-trip at its direct predecessor so
    # the test still crosses every data-independent migration after aq44
    # without fabricating current-map production evidence.
    target_revision = "fi44v8x9z33"

    command.upgrade(config, target_revision)
    command.downgrade(config, "an41v7w8x9j31")

    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(customers)")
        }
        version = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0]
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    assert "statement_cycle_start_day" not in columns
    assert version == "an41v7w8x9j31"

    command.upgrade(config, target_revision)
    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(customers)")
        }
        version = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0]
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    assert "statement_cycle_start_day" in columns
    assert version == target_revision
