from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PREVIOUS_REVISION = "as46v7w8x9o36"
TARGET_REVISION = "at47v7w8x9p37"


def _config(monkeypatch: pytest.MonkeyPatch, database_path: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(database_path.parent / "backups"))
    monkeypatch.setenv("ERP_SECRET_KEY", "floor3-inventory-binding-test")
    return Config(str(PROJECT_ROOT / "alembic.ini"))


def test_floor3_inventory_binding_migration_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "floor3-binding.sqlite3"
    config = _config(monkeypatch, database_path)
    command.upgrade(config, PREVIOUS_REVISION)
    command.upgrade(config, TARGET_REVISION)

    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(inventory_pallet_items)")
        }
        assert "inventory_lot_id" in columns
        foreign_keys = connection.execute(
            "PRAGMA foreign_key_list(inventory_pallet_items)"
        ).fetchall()
        assert any(
            row[2] == "inventory_lots"
            and row[3] == "inventory_lot_id"
            and row[6] == "CASCADE"
            for row in foreign_keys
        )
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    command.downgrade(config, PREVIOUS_REVISION)
    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(inventory_pallet_items)")
        }
        assert "inventory_lot_id" not in columns
    command.upgrade(config, TARGET_REVISION)


def test_floor3_inventory_binding_downgrade_refuses_linked_lot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "floor3-binding-used.sqlite3"
    config = _config(monkeypatch, database_path)
    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        location_id = connection.execute(
            "SELECT id FROM warehouse_locations WHERE source_version='V11' "
            "AND warehouse_floor=3 ORDER BY id LIMIT 1"
        ).fetchone()[0]
        lot_id = connection.execute(
            """
            INSERT INTO inventory_lots (
                lot_number, inventory_type, warehouse_location_id,
                quantity_available, quantity_reserved, quantity_consumed,
                quantity_damaged, quantity_scrapped, unit, status, source_type,
                stock_date, last_movement_at, version
            ) VALUES (
                'FG-MIGRATION-LINK', 'finished', ?, 1, 0, 0, 0, 0,
                'boxes', 'active', 'manual', '2026-07-16',
                '2026-07-16 10:00:00', 1
            ) RETURNING id
            """,
            (location_id,),
        ).fetchone()[0]
        pallet_id = connection.execute(
            "INSERT INTO inventory_pallets (pallet_code, location_id) "
            "VALUES ('PLT-MIGRATION-LINK', ?) RETURNING id",
            (location_id,),
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO inventory_pallet_items (
                pallet_id, inventory_lot_id, item_type, quantity, unit, match_status
            ) VALUES (?, ?, 'finished', 1, 'boxes', 'matched')
            """,
            (pallet_id, lot_id),
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="linked official inventory lots still exist"):
        command.downgrade(config, PREVIOUS_REVISION)

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            TARGET_REVISION,
        )
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
