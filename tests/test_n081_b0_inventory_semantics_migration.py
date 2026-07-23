from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PREVIOUS_REVISION = "ci65v8x9z54"
TARGET_REVISION = "cj66v8x9z55"


def _config(monkeypatch: pytest.MonkeyPatch, database: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(database.parent / "backups"))
    monkeypatch.setenv("ERP_SECRET_KEY", "n081-b0-migration-test")
    return Config(str(PROJECT_ROOT / "alembic.ini"))


def _health(connection: sqlite3.Connection, revision: str) -> None:
    assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute(
        "SELECT version_num FROM alembic_version"
    ).fetchone() == (revision,)


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def test_cj66_is_the_only_head_and_linearly_descends_from_ci65(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = ScriptDirectory.from_config(
        _config(monkeypatch, tmp_path / "lineage.sqlite3")
    )

    assert script.get_heads() == [TARGET_REVISION]
    assert script.get_revision(TARGET_REVISION).down_revision == PREVIOUS_REVISION


def test_ci65_to_cj66_marks_legacy_dates_unknown_and_round_trips(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "n081-b0.sqlite3"
    config = _config(monkeypatch, database)
    command.upgrade(config, PREVIOUS_REVISION)

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        location_id = int(
            connection.execute(
            """
            INSERT INTO warehouse_locations (
                location_code, location_name, warehouse_type, warehouse_floor,
                area_code, storage_type, placement_status
            ) VALUES (
                'N081-B0-01', 'N081 B0 migration test',
                'semi_finished', 1, 'B0', 'ground', 'placed'
            )
            """
            ).lastrowid
        )
        connection.execute(
            """
            INSERT INTO inventory_lots (
                id, lot_number, inventory_type, warehouse_location_id,
                quantity_available, quantity_reserved, quantity_consumed,
                quantity_damaged, quantity_scrapped, unit, status, source_type,
                stock_date, last_movement_at, version, created_at
            ) VALUES (
                10001, 'N081-B0-LEGACY', 'semi_finished', ?,
                20, 0, 0, 0, 0, 'sheets', 'active', 'manual',
                '2026-07-01', '2026-07-01 08:00:00', 1,
                '2026-07-01 08:00:00'
            )
            """,
            (location_id,),
        )
        connection.commit()
        _health(connection, PREVIOUS_REVISION)

    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(database) as connection:
        _health(connection, TARGET_REVISION)
        assert {
            "stock_date_accuracy",
            "stock_date_original_text",
        } <= _columns(connection, "inventory_lots")
        assert connection.execute(
            """
            SELECT stock_date_accuracy, stock_date_original_text
            FROM inventory_lots WHERE id = 10001
            """
        ).fetchone() == ("unknown", None)
        with pytest.raises(
            sqlite3.IntegrityError,
            match="new unknown stock date requires original text",
        ):
            connection.execute(
                """
                INSERT INTO inventory_lots (
                    id, lot_number, inventory_type, warehouse_location_id,
                    quantity_available, quantity_reserved, quantity_consumed,
                    quantity_damaged, quantity_scrapped, unit, status,
                    source_type, stock_date, stock_date_accuracy,
                    stock_date_original_text, last_movement_at, version,
                    created_at
                ) VALUES (
                    10003, 'N081-B0-INVALID-UNKNOWN', 'semi_finished', ?,
                    1, 0, 0, 0, 0, 'sheets', 'active', 'stocktake',
                    '2026-07-23', 'unknown', NULL,
                    '2026-07-23 08:00:00', 1, '2026-07-23 08:00:00'
                )
                """,
                (location_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                UPDATE inventory_lots
                SET stock_date_accuracy = 'guessed'
                WHERE id = 10001
                """
            )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="new unknown stock date requires original text",
        ):
            connection.execute(
                """
                UPDATE inventory_lots
                SET stock_date = '2026-07-02'
                WHERE id = 10001
                """
            )

    command.downgrade(config, PREVIOUS_REVISION)
    with sqlite3.connect(database) as connection:
        _health(connection, PREVIOUS_REVISION)
        assert "stock_date_accuracy" not in _columns(connection, "inventory_lots")
        assert connection.execute(
            "SELECT lot_number, quantity_available FROM inventory_lots WHERE id = 10001"
        ).fetchone() == ("N081-B0-LEGACY", 20)

    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(database) as connection:
        _health(connection, TARGET_REVISION)


def test_cj66_downgrade_fails_closed_after_date_fact_is_recorded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "n081-b0-downgrade-guard.sqlite3"
    config = _config(monkeypatch, database)
    command.upgrade(config, TARGET_REVISION)

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        location_id = int(
            connection.execute(
            """
            INSERT INTO warehouse_locations (
                location_code, location_name, warehouse_type, warehouse_floor,
                area_code, storage_type, placement_status
            ) VALUES (
                'N081-B0-02', 'N081 B0 downgrade test',
                'semi_finished', 1, 'B0', 'ground', 'placed'
            )
            """
            ).lastrowid
        )
        connection.execute(
            """
            INSERT INTO inventory_lots (
                id, lot_number, inventory_type, warehouse_location_id,
                quantity_available, quantity_reserved, quantity_consumed,
                quantity_damaged, quantity_scrapped, unit, status, source_type,
                stock_date, stock_date_accuracy, stock_date_original_text,
                last_movement_at, version, created_at
            ) VALUES (
                10002, 'N081-B0-FACT', 'semi_finished', ?,
                20, 0, 0, 0, 0, 'sheets', 'active', 'stocktake',
                '2026-07-01', 'unknown', '未提供',
                '2026-07-23 08:00:00', 1, '2026-07-23 08:00:00'
            )
            """,
            (location_id,),
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="stock-date facts exist"):
        command.downgrade(config, PREVIOUS_REVISION)

    with sqlite3.connect(database) as connection:
        _health(connection, TARGET_REVISION)
        assert connection.execute(
            "SELECT stock_date_accuracy FROM inventory_lots WHERE id = 10002"
        ).fetchone() == ("unknown",)
