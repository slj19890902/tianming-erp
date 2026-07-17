from __future__ import annotations

from collections.abc import Generator
import json
from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
import bcrypt
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session, sessionmaker


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PREVIOUS_REVISION = "ay52v8x9z42"
TARGET_REVISION = "az53v8x9z43"


def _test_password_hash(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=4)).decode(
        "ascii"
    )


def _alembic_config(monkeypatch: pytest.MonkeyPatch, database_path: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(database_path.parent / "backups"))
    monkeypatch.setenv("ERP_SECRET_KEY", "p5-boss-ui-migration-test")
    return Config(str(PROJECT_ROOT / "alembic.ini"))


@pytest.fixture()
def p5_api(tmp_path: Path):
    from app.api.auth import router as auth_router
    from app.api.dashboard import router as dashboard_router
    from app.api.deps import get_db
    from app.core.database import create_sqlite_engine
    from app.models import Base
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "p5-api.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        db.add_all(
            [
                User(
                    username="p5-admin",
                    password_hash=_test_password_hash("P5AdminPass123!"),
                    role="admin",
                    real_name="P5 Admin",
                    must_change_password=False,
                ),
                User(
                    username="p5-boss",
                    password_hash=_test_password_hash("P5BossPass123!"),
                    role="boss",
                    real_name="P5 Boss",
                    must_change_password=False,
                ),
                User(
                    username="p5-workshop",
                    password_hash=_test_password_hash("P5WorkshopPass123!"),
                    role="workshop",
                    real_name="P5 Workshop",
                    must_change_password=False,
                ),
            ]
        )
        db.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(dashboard_router, prefix="/api/dashboard")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield app, factory, engine
    finally:
        engine.dispose()


def _login(client: TestClient, username: str, password: str) -> dict:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200
    return response.json()


def test_ui_mode_api_payloads_validation_and_operation_log(p5_api) -> None:
    from app.models.audit import OperationLog
    from app.models.user import User

    app, factory, _engine = p5_api
    with TestClient(app) as client:
        login = _login(client, "p5-boss", "P5BossPass123!")
        assert login["user"]["ui_mode"] == "large"
        assert client.get("/api/auth/me").json()["user"]["ui_mode"] == "large"

        changed = client.put(
            "/api/auth/me/ui-mode",
            json={"ui_mode": "standard"},
        )
        invalid = client.put(
            "/api/auth/me/ui-mode",
            json={"ui_mode": "oversized"},
        )

    assert changed.status_code == 200
    assert set(changed.json()) == {"ok", "user"}
    assert changed.json()["ok"] is True
    assert changed.json()["user"]["ui_mode"] == "standard"
    assert invalid.status_code == 422

    with factory() as db:
        boss = db.scalar(select(User).where(User.username == "p5-boss"))
        log = db.scalar(
            select(OperationLog).where(OperationLog.action == "CHANGE_UI_MODE")
        )
        assert boss is not None and boss.ui_mode == "standard"
        assert log is not None and log.user_id == boss.id
        assert json.loads(log.details or "{}") == {
            "target_user_id": boss.id,
            "old_ui_mode": "large",
            "new_ui_mode": "standard",
        }


def test_boss_defaults_role_promotion_and_user_list_payload(
    p5_api,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.api.auth as auth_api

    monkeypatch.setattr(auth_api, "hash_password", _test_password_hash)
    app, _factory, _engine = p5_api
    with TestClient(app) as client:
        _login(client, "p5-admin", "P5AdminPass123!")
        boss = client.post(
            "/api/auth/users",
            json={
                "username": "new-boss",
                "password": "NewBossPass123!",
                "role": "boss",
                "real_name": "New Boss",
            },
        )
        sales = client.post(
            "/api/auth/users",
            json={
                "username": "promoted-sales",
                "password": "SalesPass123!",
                "role": "sales",
                "real_name": "Promoted Sales",
            },
        )
        promoted = client.put(
            f"/api/auth/users/{sales.json()['user']['id']}",
            json={"role": "boss"},
        )
        users = client.get("/api/auth/users")

    assert boss.status_code == 201
    assert boss.json()["user"]["ui_mode"] == "large"
    assert sales.status_code == 201
    assert sales.json()["user"]["ui_mode"] == "standard"
    assert promoted.status_code == 200
    assert promoted.json()["user"]["ui_mode"] == "large"
    assert users.status_code == 200
    assert all("ui_mode" in user for user in users.json()["items"])


def test_boss_dashboard_cards_are_permission_gated_cost_free_and_read_only(
    p5_api,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.api.dashboard as dashboard_api
    from app.models.access_control import UserPermissionOverride
    from app.models.user import User

    app, factory, engine = p5_api
    calls: list[Session] = []

    def fake_inventory_insights(db: Session) -> dict:
        calls.append(db)
        assert not db.new and not db.dirty and not db.deleted
        return {
            "action_item_count": 503,
            "high_priority_action_item_count": 302,
            "action_items": [
                {"priority": 0, "estimated_unit_cost": "88.00"},
                {"priority": 1, "estimated_value": "99.00"},
                {"priority": 2},
            ]
        }

    monkeypatch.setattr(dashboard_api, "build_inventory_insights", fake_inventory_insights)
    writes: list[str] = []

    def capture_writes(_conn, _cursor, statement, _parameters, _context, _many) -> None:
        operation = statement.lstrip().split(None, 1)[0].upper()
        if operation in {"INSERT", "UPDATE", "DELETE"}:
            writes.append(statement)

    with TestClient(app) as client:
        _login(client, "p5-boss", "P5BossPass123!")
        event.listen(engine, "before_cursor_execute", capture_writes)
        try:
            response = client.get("/api/dashboard/overview")
        finally:
            event.remove(engine, "before_cursor_execute", capture_writes)

        assert response.status_code == 200
        cards = {card["key"]: card for card in response.json()["cards"]}
        assert cards["inventory_risk"]["count"] == 503
        assert cards["business_anomaly"]["count"] == 302
        assert "estimated_unit_cost" not in response.text
        assert "estimated_value" not in response.text
        assert len(calls) == 1
        assert writes == []

        with factory() as db:
            boss = db.scalar(select(User).where(User.username == "p5-boss"))
            assert boss is not None
            db.add(
                UserPermissionOverride(
                    user_id=boss.id,
                    permission_code="warehouse.view",
                    is_allowed=False,
                )
            )
            db.commit()

        denied = client.get("/api/dashboard/overview")
        assert {card["key"] for card in denied.json()["cards"]}.isdisjoint(
            {"inventory_risk", "business_anomaly"}
        )
        assert len(calls) == 1

        client.post("/api/auth/logout")
        _login(client, "p5-workshop", "P5WorkshopPass123!")
        non_boss = client.get("/api/dashboard/overview")
        assert {card["key"] for card in non_boss.json()["cards"]}.isdisjoint(
            {"inventory_risk", "business_anomaly"}
        )
        assert len(calls) == 1


def test_ui_mode_migration_backfills_constraints_and_round_trips(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "p5-ui-mode-roundtrip.sqlite3"
    command.upgrade(_alembic_config(monkeypatch, database_path), PREVIOUS_REVISION)
    with sqlite3.connect(database_path) as connection:
        connection.executemany(
            """
            INSERT INTO users (
                username, password_hash, role, real_name, display_name,
                is_active, must_change_password, customer_access_mode
            ) VALUES (?, ?, ?, ?, NULL, 1, 0, 'all')
            """,
            [
                ("legacy-boss", "hash", "boss", "Legacy Boss"),
                ("legacy-admin", "hash", "admin", "Legacy Admin"),
            ],
        )
        connection.commit()

    command.upgrade(_alembic_config(monkeypatch, database_path), TARGET_REVISION)
    with sqlite3.connect(database_path) as connection:
        columns = {row[1]: row for row in connection.execute("PRAGMA table_info(users)")}
        assert columns["ui_mode"][3] == 1
        assert str(columns["ui_mode"][4]).strip("'\"") == "standard"
        assert connection.execute(
            "SELECT username, ui_mode FROM users ORDER BY username"
        ).fetchall() == [
            ("legacy-admin", "standard"),
            ("legacy-boss", "large"),
        ]
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE users SET ui_mode = 'invalid' WHERE username = 'legacy-admin'"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE users SET ui_mode = NULL WHERE username = 'legacy-admin'"
            )

    command.downgrade(_alembic_config(monkeypatch, database_path), PREVIOUS_REVISION)
    with sqlite3.connect(database_path) as connection:
        assert "ui_mode" not in {
            row[1] for row in connection.execute("PRAGMA table_info(users)")
        }
        assert connection.execute(
            "SELECT username, role FROM users ORDER BY username"
        ).fetchall() == [
            ("legacy-admin", "admin"),
            ("legacy-boss", "boss"),
        ]

    command.upgrade(_alembic_config(monkeypatch, database_path), TARGET_REVISION)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT username, ui_mode FROM users ORDER BY username"
        ).fetchall() == [
            ("legacy-admin", "standard"),
            ("legacy-boss", "large"),
        ]
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
