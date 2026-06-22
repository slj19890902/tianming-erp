from __future__ import annotations

import sqlite3
from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker


def _write_marker(database_path: Path, value: str) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS restore_marker (value TEXT NOT NULL)"
        )
        connection.execute("DELETE FROM restore_marker")
        connection.execute(
            "INSERT INTO restore_marker (value) VALUES (?)",
            (value,),
        )
        connection.commit()


def _read_marker(database_path: Path) -> str:
    with sqlite3.connect(database_path) as connection:
        return str(
            connection.execute(
                "SELECT value FROM restore_marker"
            ).fetchone()[0]
        )


def test_restore_forces_verified_pre_restore_backup(tmp_path: Path) -> None:
    from app.core.database import backup_to_nas, restore_from_backup

    database_path = tmp_path / "carton_erp.sqlite3"
    backup_dir = tmp_path / "nas"
    _write_marker(database_path, "backup-version")
    source_backup = backup_to_nas(
        source_path=database_path,
        backup_dir=backup_dir,
    )
    _write_marker(database_path, "current-version")

    result = restore_from_backup(
        source_backup.path,
        target_path=database_path,
        backup_dir=backup_dir,
    )

    assert result.integrity_check == "ok"
    assert result.emergency_backup.name.endswith("_pre_restore.sqlite3")
    assert _read_marker(database_path) == "backup-version"
    assert _read_marker(result.emergency_backup) == "current-version"


@pytest.fixture()
def system_api_app(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.system import router as system_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.user import User

    database_path = tmp_path / "system.sqlite3"
    backup_dir = tmp_path / "nas"
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(backup_dir))
    monkeypatch.setenv("ERP_SECRET_KEY", "phase9-system-secret")
    engine = create_sqlite_engine(database_path)
    Base.metadata.create_all(engine)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"
        )
        connection.execute(
            "INSERT INTO alembic_version VALUES ('f4b2c9d7a110')"
        )
        connection.commit()
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        session.add_all(
            User(
                username=role,
                password_hash=hash_password("RolePass123!"),
                role=role,
                real_name=role,
                must_change_password=False,
            )
            for role in ("admin", "finance")
        )
        session.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(system_router, prefix="/api/system")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    yield app, database_path, backup_dir
    engine.dispose()


def _login(client: TestClient, role: str) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": role, "password": "RolePass123!"},
    )
    assert response.status_code == 200, response.text


def test_system_backup_api_is_admin_only_and_lists_created_backup(
    system_api_app,
) -> None:
    app, _, _ = system_api_app
    with TestClient(app) as client:
        _login(client, "finance")
        denied = client.get("/api/system/backups")
        _login(client, "admin")
        empty = client.get("/api/system/backups")
        created = client.post("/api/system/backups")
        listed = client.get("/api/system/backups")

    assert denied.status_code == 403
    assert empty.status_code == 200
    assert empty.json()["items"] == []
    assert created.status_code == 201, created.text
    assert listed.status_code == 200, listed.text
    assert listed.json()["items"][0]["filename"] == created.json()["filename"]
    assert listed.json()["items"][0]["size"] > 0


def test_restore_api_restores_data_and_writes_audit(system_api_app) -> None:
    app, database_path, backup_dir = system_api_app
    _write_marker(database_path, "backup-version")
    with TestClient(app) as client:
        _login(client, "admin")
        created = client.post("/api/system/backups")
        assert created.status_code == 201, created.text
        _write_marker(database_path, "current-version")
        restored = client.post(
            "/api/system/backups/restore",
            json={"filename": created.json()["filename"]},
        )

    assert restored.status_code == 200, restored.text
    assert restored.json()["integrity_check"] == "ok"
    emergency = backup_dir / restored.json()["pre_restore_backup"]
    assert emergency.name.endswith("_pre_restore.sqlite3")
    assert _read_marker(database_path) == "backup-version"
    assert _read_marker(emergency) == "current-version"
    with sqlite3.connect(database_path) as connection:
        action = connection.execute(
            "SELECT action FROM operation_logs "
            "WHERE action = 'RESTORE_DATABASE' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert action == ("RESTORE_DATABASE",)


@pytest.mark.parametrize(
    "filename",
    ["../outside.sqlite3", r"C:\outside.sqlite3", "backup.txt"],
)
def test_restore_rejects_unsafe_backup_filename(
    system_api_app,
    filename: str,
) -> None:
    app, _, _ = system_api_app
    with TestClient(app) as client:
        _login(client, "admin")
        response = client.post(
            "/api/system/backups/restore",
            json={"filename": filename},
        )

    assert response.status_code == 400


def test_wildcard_and_public_cors_origins_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.config import load_settings

    for value in ("*", "https://erp.example.com", "ftp://192.168.1.20"):
        monkeypatch.setenv("ERP_ALLOWED_ORIGINS", value)
        with pytest.raises(ValueError, match="ERP_ALLOWED_ORIGINS"):
            load_settings()

    monkeypatch.setenv(
        "ERP_ALLOWED_ORIGINS",
        "http://192.168.1.20:8000,http://10.0.0.8:8000",
    )
    assert load_settings().allowed_origins == (
        "http://192.168.1.20:8000",
        "http://10.0.0.8:8000",
    )
