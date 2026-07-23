from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PREVIOUS_REVISION = "cg63v8x9z52"
TARGET_REVISION = "ch64v8x9z53"


def _config(monkeypatch: pytest.MonkeyPatch, database: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(database.parent / "backups"))
    monkeypatch.setenv("ERP_SECRET_KEY", "n081-space-readiness-migration-test")
    return Config(str(PROJECT_ROOT / "alembic.ini"))


def _upgrade(
    monkeypatch: pytest.MonkeyPatch, database: Path, revision: str
) -> None:
    command.upgrade(_config(monkeypatch, database), revision)


def _downgrade(
    monkeypatch: pytest.MonkeyPatch, database: Path, revision: str
) -> None:
    command.downgrade(_config(monkeypatch, database), revision)


def _seed_factory_extension_locations(database: Path) -> None:
    with sqlite3.connect(database) as connection:
        connection.executemany(
            """
            INSERT INTO warehouse_locations (
                location_code, location_name, warehouse_type, is_active,
                remarks, sort_order, is_temporary
            ) VALUES (?, ?, 'finished', 1, ?, 0, 0)
            """,
            (
                ("C1-R12", "三楼 C1-R12", "待补齐空间主数据"),
                ("C1-R13", "三楼 C1-R13", "待补齐空间主数据"),
            ),
        )


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _triggers(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        )
    }


def test_upgrade_places_c1_extension_and_keeps_unplaced_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "n081-a1.sqlite3"
    _upgrade(monkeypatch, database, PREVIOUS_REVISION)
    _seed_factory_extension_locations(database)

    _upgrade(monkeypatch, database, TARGET_REVISION)

    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0] == TARGET_REVISION
        assert "placement_status" in _columns(connection, "warehouse_locations")

        extension = connection.execute(
            """
            SELECT location_code, warehouse_floor, area_code, storage_type,
                   placement_status, source_version
            FROM warehouse_locations
            WHERE location_code IN ('C1-R12','C1-R13')
            ORDER BY location_code
            """
        ).fetchall()
        assert [tuple(row) for row in extension] == [
            ("C1-R12", 3, "C1", "ground", "placed", "V11"),
            ("C1-R13", 3, "C1", "ground", "placed", "V11"),
        ]

        layouts = connection.execute(
            """
            SELECT location.location_code, layout.left_pct, layout.top_pct,
                   layout.width_pct, layout.height_pct
            FROM floor3_location_layouts layout
            JOIN warehouse_locations location ON location.id=layout.location_id
            WHERE location.location_code LIKE 'C1-R%'
            ORDER BY CAST(SUBSTR(location.location_code, 5) AS INTEGER)
            """
        ).fetchall()
        assert len(layouts) == 13
        assert [row["location_code"] for row in layouts] == [
            f"C1-R{number:02d}" for number in range(1, 14)
        ]
        assert all(float(row["left_pct"]) == 50 for row in layouts)
        assert all(float(row["width_pct"]) == 50 for row in layouts)
        assert max(
            float(row["top_pct"]) + float(row["height_pct"]) for row in layouts
        ) <= 100.0001

        sf_temp = connection.execute(
            """
            SELECT placement_status
            FROM warehouse_locations
            WHERE location_code='SF-TEMP'
            """
        ).fetchone()
        assert sf_temp is not None
        assert sf_temp["placement_status"] == "unplaced"

        trigger_names = _triggers(connection)
        assert {
            "trg_warehouse_locations_placement_status_insert",
            "trg_inventory_lots_require_placed_location_insert",
            "trg_inventory_pallets_require_placed_location_insert",
            "trg_warehouse_locations_unplace_reference_guard",
        } <= trigger_names

        sf_temp_id = connection.execute(
            "SELECT id FROM warehouse_locations WHERE location_code='SF-TEMP'"
        ).fetchone()[0]
        with pytest.raises(
            sqlite3.IntegrityError,
            match="current pallet requires an active placed location",
        ):
            connection.execute(
                """
                INSERT INTO inventory_pallets (pallet_code, location_id)
                VALUES ('N081-A1-BLOCKED', ?)
                """,
                (sf_temp_id,),
            )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="invalid warehouse_locations.placement_status",
        ):
            connection.execute(
                """
                UPDATE warehouse_locations SET placement_status='invalid'
                WHERE id=?
                """,
                (sf_temp_id,),
            )


def test_clean_downgrade_restores_extension_without_destroying_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "n081-a1-clean-downgrade.sqlite3"
    _upgrade(monkeypatch, database, PREVIOUS_REVISION)
    _seed_factory_extension_locations(database)
    _upgrade(monkeypatch, database, TARGET_REVISION)

    _downgrade(monkeypatch, database, PREVIOUS_REVISION)

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert "placement_status" not in _columns(connection, "warehouse_locations")
        assert connection.execute(
            """
            SELECT location_code, warehouse_floor, area_code, storage_type,
                   source_version
            FROM warehouse_locations
            WHERE location_code IN ('C1-R12','C1-R13')
            ORDER BY location_code
            """
        ).fetchall() == [
            ("C1-R12", None, None, None, None),
            ("C1-R13", None, None, None, None),
        ]
        assert connection.execute(
            """
            SELECT COUNT(*)
            FROM floor3_location_layouts layout
            JOIN warehouse_locations location ON location.id=layout.location_id
            WHERE location.location_code IN ('C1-R12','C1-R13')
            """
        ).fetchone()[0] == 0


def test_downgrade_fails_closed_after_extension_receives_pallet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "n081-a1-blocked-downgrade.sqlite3"
    _upgrade(monkeypatch, database, PREVIOUS_REVISION)
    _seed_factory_extension_locations(database)
    _upgrade(monkeypatch, database, TARGET_REVISION)
    with sqlite3.connect(database) as connection:
        location_id = connection.execute(
            "SELECT id FROM warehouse_locations WHERE location_code='C1-R13'"
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO inventory_pallets (pallet_code, location_id)
            VALUES ('N081-A1-PALLET', ?)
            """,
            (location_id,),
        )

    with pytest.raises(RuntimeError, match="禁止破坏性降级"):
        _downgrade(monkeypatch, database, PREVIOUS_REVISION)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0] == TARGET_REVISION
        assert "placement_status" in _columns(connection, "warehouse_locations")
