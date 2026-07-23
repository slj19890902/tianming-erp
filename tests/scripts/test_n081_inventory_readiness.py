from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from scripts.audit.n081_inventory_readiness import audit_database, write_outputs


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _create_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE alembic_version (version_num TEXT NOT NULL);
            INSERT INTO alembic_version VALUES ('cg63v8x9z52');

            CREATE TABLE warehouse_locations (
                id INTEGER PRIMARY KEY,
                location_code TEXT NOT NULL,
                location_name TEXT NOT NULL,
                warehouse_type TEXT NOT NULL,
                is_active INTEGER NOT NULL,
                warehouse_floor INTEGER,
                area_code TEXT,
                storage_type TEXT,
                is_temporary INTEGER NOT NULL,
                placement_status TEXT NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE floor3_location_layouts (location_id INTEGER PRIMARY KEY);
            CREATE TABLE inventory_lots (
                id INTEGER PRIMARY KEY,
                lot_number TEXT NOT NULL,
                inventory_type TEXT NOT NULL,
                warehouse_location_id INTEGER NOT NULL,
                quantity_available INTEGER NOT NULL,
                quantity_reserved INTEGER NOT NULL,
                unit TEXT NOT NULL,
                status TEXT NOT NULL,
                source_type TEXT NOT NULL,
                stock_date TEXT NOT NULL
            );
            CREATE TABLE inventory_pallets (
                id INTEGER PRIMARY KEY,
                pallet_code TEXT NOT NULL,
                location_id INTEGER,
                status TEXT NOT NULL,
                is_current INTEGER NOT NULL,
                needs_relocation INTEGER NOT NULL
            );
            CREATE TABLE inventory_pallet_items (
                id INTEGER PRIMARY KEY,
                pallet_id INTEGER NOT NULL,
                inventory_lot_id INTEGER,
                customer_id INTEGER,
                product_id INTEGER,
                inventory_code TEXT,
                item_type TEXT NOT NULL,
                quantity NUMERIC NOT NULL,
                unit TEXT NOT NULL,
                match_status TEXT NOT NULL
            );
            CREATE TABLE finished_goods_inventory_details (
                inventory_lot_id INTEGER PRIMARY KEY,
                owner_customer_id INTEGER,
                is_general INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                inventory_code_snapshot TEXT NOT NULL
            );
            CREATE TABLE semi_finished_inventory_details (
                inventory_lot_id INTEGER PRIMARY KEY,
                owner_customer_id INTEGER,
                material_code_snapshot TEXT NOT NULL,
                sheet_type TEXT NOT NULL
            );

            INSERT INTO warehouse_locations VALUES
                (1, 'E1-L09', '三楼 E1-L09', 'finished', 1, 3, 'E', 'rack', 0, 'placed', 1),
                (2, 'G1-L02', '待补资料库位', 'shared', 1, NULL, NULL, NULL, 0, 'unplaced', 2);
            INSERT INTO floor3_location_layouts VALUES (1);
            INSERT INTO inventory_lots VALUES
                (1, 'LOT-F-001', 'finished', 1, 100, 0, 'boxes', 'active', 'stocktake', '2026-07-23'),
                (2, 'LOT-S-001', 'semi_finished', 2, 200, 0, 'sheets', 'active', 'stocktake', '2026-07-23');
            INSERT INTO finished_goods_inventory_details VALUES
                (1, 10, 0, 20, 'DEMO-BOX-001');
            INSERT INTO inventory_pallets VALUES
                (1, 'PLT-001', 1, 'active', 1, 0),
                (2, 'PLT-002', NULL, 'active', 1, 1);
            INSERT INTO inventory_pallet_items VALUES
                (1, 1, 1, 10, 20, 'DEMO-BOX-001', 'finished', 100, 'boxes', 'matched'),
                (2, 2, NULL, NULL, NULL, 'DEMO-UNKNOWN', 'semi_finished', 300, 'sheets', 'pending');
            """
        )


def test_n081_readiness_export_is_read_only_and_reports_gaps(tmp_path: Path) -> None:
    database = tmp_path / "uat.sqlite3"
    output = tmp_path / "report"
    _create_database(database)
    before_hash = _sha256(database)

    report = audit_database(database)
    outputs = write_outputs(report, output)

    assert _sha256(database) == before_hash
    assert report["audit"]["database_written"] is False
    assert report["audit"]["connection_mode"] == "sqlite_mode_ro_query_only"
    assert report["audit"]["database_changed_during_scan"] is False
    assert report["audit"]["quick_check"] == "ok"
    assert report["audit"]["foreign_key_errors"] == 0
    assert report["audit"]["alembic"] == ["cg63v8x9z52"]
    assert report["summary"]["table_counts"]["inventory_lots"] == 2
    assert report["summary"]["issue_counts"] == {
        "active_lot_in_unplaced_location": 1,
        "active_lot_missing_type_detail": 1,
        "active_lot_without_pallet": 1,
        "current_pallet_location_requires_review": 1,
        "location_unplaced": 1,
        "pallet_snapshot_requires_review": 1,
    }
    assert report["summary"]["locations_by_placement"] == {
        "placed": 1,
        "unplaced": 1,
    }
    assert all(Path(path).is_file() for path in outputs.values())
    assert "是否写入数据库：`false`" in Path(outputs["markdown"]).read_text(
        encoding="utf-8"
    )


def test_n081_readiness_refuses_incomplete_schema(tmp_path: Path) -> None:
    database = tmp_path / "incomplete.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE warehouse_locations (id INTEGER PRIMARY KEY)")

    with pytest.raises(ValueError, match="missing tables"):
        audit_database(database)
