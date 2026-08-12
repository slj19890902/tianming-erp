from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest


ROOT = Path(__file__).resolve().parents[1]
PARENT = "hh16v8x9z05"
TARGET = "ii17v8x9z06"
MIGRATION = ROOT / "alembic" / "versions" / "ii17v8x9z06_contract_seal_archives.py"


def _config(monkeypatch: pytest.MonkeyPatch, path: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(path))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def test_contract_seal_migration_is_linear() -> None:
    spec = importlib.util.spec_from_file_location("p135b_migration", MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == TARGET
    assert module.down_revision == PARENT


def test_contract_seal_migration_round_trip_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "p1-35b-migration.sqlite3"
    config = _config(monkeypatch, path)
    command.upgrade(config, PARENT)
    command.upgrade(config, TARGET)
    with sqlite3.connect(path) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"contract_seal_asset_versions", "contract_seal_selection", "contract_sealed_pdf_archives"} <= tables
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    command.downgrade(config, PARENT)
    command.upgrade(config, TARGET)
    with sqlite3.connect(path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(users)")}
        values = {
            "username": "p135b-migration-user",
            "password_hash": "not-used",
            "role": "admin",
            "real_name": "迁移测试",
            "is_active": 1,
            "auth_version": 1,
            "must_change_password": 0,
            "customer_access_mode": "all",
            "ui_mode": "standard",
        }
        selected = [(name, value) for name, value in values.items() if name in columns]
        placeholders = ",".join("?" for _ in selected)
        connection.execute(
            f"INSERT INTO users ({','.join(name for name, _value in selected)}) VALUES ({placeholders})",
            tuple(value for _name, value in selected),
        )
        user_id = connection.execute("SELECT id FROM users ORDER BY id DESC LIMIT 1").fetchone()[0]
        connection.execute(
            "INSERT INTO contract_seal_asset_versions "
            "(version,original_filename,mime_type,png_content,sha256,width_px,height_px,uploaded_by,uploaded_by_snapshot) "
            "VALUES (1,'seal.png','image/png',X'89504E47',?,16,16,?,'迁移测试')",
            ("1" * 64, user_id),
        )
        connection.commit()
    with pytest.raises(RuntimeError, match="拒绝破坏性降级"):
        command.downgrade(config, PARENT)
    with sqlite3.connect(path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("UPDATE contract_seal_asset_versions SET original_filename='changed.png' WHERE version=1")
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
