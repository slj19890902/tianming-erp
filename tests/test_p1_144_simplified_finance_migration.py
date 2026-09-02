from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PREVIOUS_REVISION = "jk69v8x9z58"
TARGET_REVISION = "jl70v8x9z59"
NEW_TABLES = {
    "finance_recurring_rules",
    "finance_utility_readings",
    "finance_acceptance_notes",
}


def _config(monkeypatch: pytest.MonkeyPatch, database: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(database.parent / "backups"))
    monkeypatch.setenv("ERP_SECRET_KEY", "p1-144-simplified-finance-migration-test")
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    return config


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _assert_health(connection: sqlite3.Connection, revision: str) -> None:
    connection.execute("PRAGMA foreign_keys = ON")
    assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
        revision,
    )
    assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_p1_144_migration_upgrade_downgrade_upgrade_round_trip(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = tmp_path / "p1-144-round-trip.sqlite3"
    config = _config(monkeypatch, database)
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == [TARGET_REVISION]
    assert script.get_revision(TARGET_REVISION).down_revision == PREVIOUS_REVISION

    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(database) as connection:
        _assert_health(connection, TARGET_REVISION)
        assert NEW_TABLES <= _tables(connection)
        assert {"payment_method", "acceptance_note_id"} <= _columns(
            connection, "supplier_monthly_payments"
        )

    command.downgrade(config, PREVIOUS_REVISION)
    with sqlite3.connect(database) as connection:
        _assert_health(connection, PREVIOUS_REVISION)
        assert NEW_TABLES.isdisjoint(_tables(connection))
        assert {"payment_method", "acceptance_note_id"}.isdisjoint(
            _columns(connection, "supplier_monthly_payments")
        )

    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(database) as connection:
        _assert_health(connection, TARGET_REVISION)
        assert NEW_TABLES <= _tables(connection)


def test_p1_144_migration_blocks_downgrade_when_new_facts_exist(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = tmp_path / "p1-144-downgrade-guard.sqlite3"
    config = _config(monkeypatch, database)
    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(database) as connection:
        center_id = connection.execute(
            "SELECT id FROM finance_cost_centers WHERE code = 'FINANCE'"
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO finance_recurring_rules (
                rule_type, name, cost_center_id, cost_category,
                start_month, monthly_amount, due_day, is_active, version
            ) VALUES (
                'fixed_monthly', '车辆月供', ?, 'finance_expense',
                '2026-09', 8000, 20, 1, 1
            )
            """,
            (center_id,),
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="facts would be lost"):
        command.downgrade(config, PREVIOUS_REVISION)

    with sqlite3.connect(database) as connection:
        _assert_health(connection, TARGET_REVISION)
        assert connection.execute(
            "SELECT name, monthly_amount FROM finance_recurring_rules"
        ).fetchone() == ("车辆月供", 8000)
