from __future__ import annotations

import importlib.util
from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
import pytest


ROOT = Path(__file__).resolve().parents[1]
PARENT = "aaa35v8x9z24"
TARGET = "bbb36v8x9z25"
INTEGRATION_HEAD = "iv57v8x9z46"
MIGRATION = (
    ROOT
    / "alembic"
    / "versions"
    / "bbb36v8x9z25_cancelled_external_purchase_purge_gate.py"
)


def _config(monkeypatch: pytest.MonkeyPatch, path: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(path))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def test_p1_64_migration_is_unique_linear_ancestor(current_alembic_head: str) -> None:
    spec = importlib.util.spec_from_file_location("p1_64_migration", MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == TARGET
    assert module.down_revision == PARENT
    assert current_alembic_head == INTEGRATION_HEAD


def test_p1_64_round_trip_replaces_only_delete_guards(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "p1-64.sqlite3"
    config = _config(monkeypatch, path)
    command.upgrade(config, PARENT)
    command.upgrade(config, TARGET)

    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        triggers = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        }
        assert "external_packaging_purchase_purge_authorizations" in tables
        assert {
            "trg_external_packaging_purchase_purge_authorizations_safe_insert",
            "trg_external_packaging_purchase_batches_immutable_update",
            "trg_external_packaging_purchase_batches_immutable_delete",
            "trg_external_packaging_purchase_orders_immutable_update",
            "trg_external_packaging_purchase_orders_immutable_delete",
            "trg_external_packaging_purchase_items_immutable_update",
            "trg_external_packaging_purchase_items_immutable_delete",
            "trg_external_packaging_purchase_cancellations_immutable_update",
            "trg_external_packaging_purchase_cancellations_immutable_delete",
        } <= triggers
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    command.downgrade(config, PARENT)
    command.upgrade(config, TARGET)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0] == TARGET
