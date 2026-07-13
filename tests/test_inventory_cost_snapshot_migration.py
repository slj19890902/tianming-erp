from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PREVIOUS_REVISION = "af33v7w8x9b23"
TARGET_REVISION = "ai36v7w8x9e26"
SNAPSHOT_COLUMNS = {
    "estimated_unit_cost_snapshot",
    "estimated_square_price_snapshot",
    "estimated_cost_area_m2_snapshot",
    "cost_snapshot_source",
    "cost_snapshot_detail_json",
    "cost_snapshot_at",
}
ORIGINAL_COLUMNS = {
    "id",
    "lot_number",
    "inventory_type",
    "warehouse_location_id",
    "quantity_available",
    "quantity_reserved",
    "quantity_consumed",
    "quantity_damaged",
    "quantity_scrapped",
    "unit",
    "status",
    "source_type",
    "source_ref_type",
    "source_ref_id",
    "stock_date",
    "last_movement_at",
    "version",
    "remarks",
    "created_by",
    "created_at",
    "updated_at",
}


def _alembic_config(monkeypatch: pytest.MonkeyPatch, database_path: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(database_path.parent / "backups"))
    monkeypatch.setenv("ERP_SECRET_KEY", "inventory-cost-snapshot-migration-test")
    return Config(str(PROJECT_ROOT / "alembic.ini"))


def _run_migration(
    monkeypatch: pytest.MonkeyPatch,
    database_path: Path,
    revision: str,
) -> None:
    command.upgrade(_alembic_config(monkeypatch, database_path), revision)


def _downgrade_migration(
    monkeypatch: pytest.MonkeyPatch,
    database_path: Path,
    revision: str,
) -> None:
    command.downgrade(_alembic_config(monkeypatch, database_path), revision)


def _table_columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table_name})")}


def _assert_database_health(
    connection: sqlite3.Connection,
    expected_revision: str,
) -> None:
    assert connection.execute(
        "SELECT version_num FROM alembic_version"
    ).fetchone() == (expected_revision,)
    assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_inventory_cost_snapshot_migration_round_trip_isolated_from_formal_db(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "inventory_cost_snapshot.sqlite3"

    _run_migration(monkeypatch, database_path, PREVIOUS_REVISION)

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """
            INSERT INTO warehouse_locations (
                id, location_code, location_name, warehouse_type
            ) VALUES (1, 'TEST-01', 'Migration test location', 'semi_finished')
            """
        )
        connection.execute(
            """
            INSERT INTO inventory_lots (
                id, lot_number, inventory_type, warehouse_location_id,
                quantity_available, quantity_reserved, quantity_consumed,
                quantity_damaged, quantity_scrapped, unit, status, source_type,
                source_ref_type, source_ref_id, stock_date, last_movement_at,
                version, remarks, created_at
            ) VALUES (
                1, 'N027-OLD-LOT', 'semi_finished', 1,
                120, 7, 3, 2, 1, 'sheets', 'active', 'manual',
                'migration-test', 2701, '2026-07-12',
                '2026-07-12 08:30:00', 4, 'old inventory data',
                '2026-07-12 08:00:00'
            )
            """
        )
        connection.commit()
        _assert_database_health(connection, PREVIOUS_REVISION)
        assert _table_columns(connection, "inventory_lots") >= ORIGINAL_COLUMNS
        assert not SNAPSHOT_COLUMNS & _table_columns(connection, "inventory_lots")

    _run_migration(monkeypatch, database_path, TARGET_REVISION)

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _assert_database_health(connection, TARGET_REVISION)
        columns = _table_columns(connection, "inventory_lots")
        assert SNAPSHOT_COLUMNS <= columns
        assert ORIGINAL_COLUMNS <= columns
        assert connection.execute(
            """
            SELECT lot_number, quantity_available, quantity_reserved,
                   quantity_consumed, quantity_damaged, quantity_scrapped,
                   remarks
            FROM inventory_lots WHERE id = 1
            """
        ).fetchone() == (
            "N027-OLD-LOT",
            120,
            7,
            3,
            2,
            1,
            "old inventory data",
        )
        connection.execute(
            """
            UPDATE inventory_lots
            SET estimated_unit_cost_snapshot = 12.3456,
                estimated_square_price_snapshot = 7.8901,
                estimated_cost_area_m2_snapshot = 3.456789,
                cost_snapshot_source = 'migration-test',
                cost_snapshot_detail_json = '{"source":"test"}',
                cost_snapshot_at = '2026-07-13 09:00:00'
            WHERE id = 1
            """
        )
        connection.commit()

    _downgrade_migration(monkeypatch, database_path, PREVIOUS_REVISION)

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _assert_database_health(connection, PREVIOUS_REVISION)
        columns = _table_columns(connection, "inventory_lots")
        assert not SNAPSHOT_COLUMNS & columns
        assert ORIGINAL_COLUMNS <= columns
        assert connection.execute(
            "SELECT COUNT(*) FROM inventory_lots"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT lot_number, quantity_available, remarks FROM inventory_lots WHERE id = 1"
        ).fetchone() == ("N027-OLD-LOT", 120, "old inventory data")

    _run_migration(monkeypatch, database_path, TARGET_REVISION)

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        _assert_database_health(connection, TARGET_REVISION)
        assert SNAPSHOT_COLUMNS <= _table_columns(connection, "inventory_lots")
        assert ORIGINAL_COLUMNS <= _table_columns(connection, "inventory_lots")
        assert connection.execute(
            """
            SELECT lot_number, quantity_available, remarks,
                   estimated_unit_cost_snapshot, cost_snapshot_source
            FROM inventory_lots WHERE id = 1
            """
        ).fetchone() == ("N027-OLD-LOT", 120, "old inventory data", None, None)
