from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker


def test_settings_read_paths_cors_and_secret_from_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from app.core.config import load_settings

    database_path = tmp_path / "erp.sqlite3"
    backup_dir = tmp_path / "nas-backups"
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(backup_dir))
    monkeypatch.setenv(
        "ERP_ALLOWED_ORIGINS",
        "http://192.168.1.2:8000, http://192.168.1.3:8000",
    )
    monkeypatch.setenv("ERP_SECRET_KEY", "test-secret-key")

    settings = load_settings()

    assert settings.database_path == database_path.resolve()
    assert settings.backup_dir == backup_dir.resolve()
    assert settings.allowed_origins == (
        "http://192.168.1.2:8000",
        "http://192.168.1.3:8000",
    )
    assert settings.secret_key == "test-secret-key"


def test_generated_secret_is_persisted_and_reused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from app.core.config import load_settings

    secret_file = tmp_path / "session_secret.key"
    monkeypatch.delenv("ERP_SECRET_KEY", raising=False)
    monkeypatch.setenv("ERP_SECRET_KEY_FILE", str(secret_file))
    monkeypatch.setenv("ERP_DATABASE_PATH", str(tmp_path / "erp.sqlite3"))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(tmp_path / "backups"))

    first = load_settings()
    second = load_settings()

    assert secret_file.exists()
    assert first.secret_key == second.secret_key
    assert len(first.secret_key) >= 32


def test_sqlite_engine_applies_required_pragmas(tmp_path: Path) -> None:
    from app.core.database import create_sqlite_engine

    engine = create_sqlite_engine(tmp_path / "erp.sqlite3")
    with engine.connect() as connection:
        journal_mode = connection.execute(text("PRAGMA journal_mode")).scalar_one()
        busy_timeout = connection.execute(text("PRAGMA busy_timeout")).scalar_one()
        foreign_keys = connection.execute(text("PRAGMA foreign_keys")).scalar_one()

    assert journal_mode.lower() == "delete"
    assert busy_timeout == 5_000
    assert foreign_keys == 1


def test_backup_to_nas_creates_verified_timestamped_snapshot(tmp_path: Path) -> None:
    from app.core.database import backup_to_nas

    source = tmp_path / "source.sqlite3"
    backup_dir = tmp_path / "nas" / "backups"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
        connection.execute("INSERT INTO sample (name) VALUES ('订单一'), ('订单二')")
        connection.commit()

    result = backup_to_nas(source_path=source, backup_dir=backup_dir)

    assert result.path.parent == backup_dir
    assert result.path.name.startswith("carton_erp_")
    assert result.path.suffix == ".sqlite3"
    assert result.integrity_check == "ok"
    assert len(result.sha256) == 64
    with sqlite3.connect(result.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM sample").fetchone()[0] == 2


def test_password_hash_is_salted_and_verifiable() -> None:
    from app.core.security import hash_password, verify_password

    first_hash = hash_password("ChangeMe123!")
    second_hash = hash_password("ChangeMe123!")

    assert first_hash != second_hash
    assert verify_password("ChangeMe123!", first_hash)
    assert not verify_password("wrong-password", first_hash)


def test_user_model_accepts_only_blueprint_roles(tmp_path: Path) -> None:
    from app.core.database import create_sqlite_engine
    from app.models import Base
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "users.sqlite3")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with session_factory() as session:
        session.add(
            User(
                username="admin",
                password_hash="hashed",
                role="admin",
                real_name="系统管理员",
                must_change_password=True,
            )
        )
        session.commit()

        session.add(
            User(
                username="legacy-boss",
                password_hash="hashed",
                role="boss",
                real_name="旧角色",
                must_change_password=True,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_initialize_users_creates_four_accounts_once(tmp_path: Path) -> None:
    from app.core.database import create_sqlite_engine
    from app.models import Base
    from app.models.user import User
    from init_db import initialize_users

    engine = create_sqlite_engine(tmp_path / "init.sqlite3")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    assert initialize_users(session_factory, initial_password="ChangeMe123!") == 4
    assert initialize_users(session_factory, initial_password="ChangeMe123!") == 0

    with session_factory() as session:
        users = session.query(User).order_by(User.username).all()

    assert {user.role for user in users} == {
        "admin",
        "finance",
        "sales",
        "workshop",
    }
    assert all(user.must_change_password for user in users)
    assert all(user.password_hash != "ChangeMe123!" for user in users)


def test_alembic_baseline_creates_users_without_dropping_existing_tables(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from alembic import command
    from alembic.config import Config

    database_path = tmp_path / "migration.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE legacy_history (id INTEGER PRIMARY KEY, source_no TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO legacy_history (source_no) VALUES ('OLD-001')")
        connection.commit()

    monkeypatch.setenv("ERP_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("ERP_SECRET_KEY", "migration-test-secret")
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))

    command.upgrade(config, "head")

    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "legacy_history" in tables
        assert "users" in tables
        assert connection.execute("SELECT COUNT(*) FROM legacy_history").fetchone()[0] == 1

    inspection_engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    engine_tables = set(inspect(inspection_engine).get_table_names())
    assert {"alembic_version", "legacy_history", "users"} <= engine_tables
