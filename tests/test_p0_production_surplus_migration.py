from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PREVIOUS_REVISION = "ch64v8x9z53"
TARGET_REVISION = "ci65v8x9z54"


def _config(monkeypatch: pytest.MonkeyPatch, database: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(database.parent / "backups"))
    monkeypatch.setenv("ERP_SECRET_KEY", "p0-production-surplus-migration-test")
    return Config(str(PROJECT_ROOT / "alembic.ini"))


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _assert_database(database: Path, revision: str) -> None:
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0] == revision


def test_ci65_is_the_only_head_and_descends_from_ch64(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(monkeypatch, tmp_path / "lineage.sqlite3")
    script = ScriptDirectory.from_config(config)

    assert script.get_heads() == [TARGET_REVISION]
    assert script.get_revision(TARGET_REVISION).down_revision == PREVIOUS_REVISION


def test_ch64_to_ci65_upgrade_round_trip_preserves_n081_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "p0-production-surplus.sqlite3"
    config = _config(monkeypatch, database)

    command.upgrade(config, PREVIOUS_REVISION)
    _assert_database(database, PREVIOUS_REVISION)

    command.upgrade(config, TARGET_REVISION)
    _assert_database(database, TARGET_REVISION)
    with sqlite3.connect(database) as connection:
        assert "placement_status" in _columns(connection, "warehouse_locations")
        assert {
            "ordered_quantity_snapshot",
            "material_received_quantity",
            "material_input_quantity",
            "output_factor",
        } <= _columns(connection, "production_tasks")
        assert {
            "completion_type",
            "actual_output_quantity",
            "surplus_finished_quantity",
        } <= _columns(connection, "production_completions")
        assert {
            "ordered_quantity_snapshot",
            "order_remaining_snapshot",
            "over_delivery_quantity",
        } <= _columns(connection, "sales_delivery_items")

    command.downgrade(config, PREVIOUS_REVISION)
    _assert_database(database, PREVIOUS_REVISION)
    with sqlite3.connect(database) as connection:
        assert "placement_status" in _columns(connection, "warehouse_locations")
        assert "surplus_finished_quantity" not in _columns(
            connection, "production_completions"
        )

    command.upgrade(config, TARGET_REVISION)
    _assert_database(database, TARGET_REVISION)
