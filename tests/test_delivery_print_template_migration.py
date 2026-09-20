from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config


PARENT = "dc0920"
TARGET = "dt0920"


def _config(monkeypatch: pytest.MonkeyPatch, database: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database))
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database.as_posix()}")
    return config


def test_delivery_print_template_migration_round_trip(tmp_path: Path, monkeypatch):
    database = tmp_path / "round-trip.sqlite3"
    config = _config(monkeypatch, database)
    command.upgrade(config, PARENT)
    command.upgrade(config, TARGET)
    with sqlite3.connect(database) as db:
        columns = {
            row[1] for row in db.execute("PRAGMA table_info(sales_deliveries)")
        }
        assert {
            "print_template_profile_key",
            "print_template_version",
            "print_template_payload_json",
            "print_template_payload_hash",
        } <= columns
        assert db.execute(
            "SELECT COUNT(*) FROM delivery_print_template_revisions"
        ).fetchone()[0] == 0
    command.downgrade(config, PARENT)
    command.upgrade(config, TARGET)


def test_delivery_print_template_migration_refuses_lossy_downgrade(
    tmp_path: Path, monkeypatch
):
    database = tmp_path / "guard.sqlite3"
    config = _config(monkeypatch, database)
    command.upgrade(config, TARGET)
    with sqlite3.connect(database) as db:
        user_id = db.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
        db.execute(
            "INSERT INTO delivery_print_template_revisions "
            "(profile_key, customer_id, stream, version, catalog_version, payload_json, "
            "payload_hash, base_release_version, operation_kind, operation_key, request_hash, "
            "source_release_version, created_by) VALUES "
            "('default', NULL, 'release', 1, 'delivery-print-v1', '{}', ?, 0, "
            "'publish', 'migration-guard-op', ?, NULL, ?)",
            ("0" * 64, "1" * 64, user_id[0] if user_id else None),
        )
        db.commit()
    with pytest.raises(Exception, match="禁止删除"):
        command.downgrade(config, PARENT)
