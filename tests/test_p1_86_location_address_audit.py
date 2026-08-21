from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3

from scripts.audit.p1_86_location_address_audit import audit_database, main


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _legacy_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE warehouse_floors (
                id INTEGER PRIMARY KEY,
                floor_number INTEGER NOT NULL
            );
            CREATE TABLE warehouse_areas (
                id INTEGER PRIMARY KEY,
                floor_id INTEGER,
                area_code TEXT,
                area_name TEXT
            );
            CREATE TABLE warehouse_locations (
                id INTEGER PRIMARY KEY,
                location_code TEXT,
                location_name TEXT,
                warehouse_floor INTEGER,
                area_code TEXT,
                storage_type TEXT,
                is_active INTEGER,
                placement_status TEXT,
                level_no INTEGER
            );
            INSERT INTO warehouse_floors VALUES (1, 1), (3, 3);
            INSERT INTO warehouse_areas VALUES (1, 1, 'D', '一楼D区');
            INSERT INTO warehouse_locations VALUES
                (11, '1F-D-001', '旧库位一', 1, 'D', 'finished', 1, 'placed', NULL),
                (12, '3F-D02-A-02-03', '三楼 D2区·A架·2层·3格', 3, 'D02', 'finished', 1, 'placed', 2),
                (13, ' free text ', '待治理', NULL, '', 'finished', 0, 'unplaced', NULL);
            """
        )
        connection.commit()
    finally:
        connection.close()


def _structured_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE warehouse_floors (
                id INTEGER PRIMARY KEY,
                floor_number INTEGER NOT NULL
            );
            CREATE TABLE warehouse_areas (
                id INTEGER PRIMARY KEY,
                floor_id INTEGER,
                area_code TEXT,
                area_name TEXT,
                address_zone_code TEXT,
                address_subzone_no INTEGER
            );
            CREATE TABLE warehouse_locations (
                id INTEGER PRIMARY KEY,
                location_code TEXT,
                location_name TEXT,
                warehouse_floor INTEGER,
                area_code TEXT,
                storage_type TEXT,
                is_active INTEGER,
                placement_status TEXT,
                level_no INTEGER,
                address_kind TEXT,
                address_area_id INTEGER,
                rack_code TEXT,
                ground_row_no INTEGER,
                slot_no INTEGER
            );
            INSERT INTO warehouse_floors VALUES (3, 3);
            INSERT INTO warehouse_areas VALUES (7, 3, 'D02', '三楼D2区', 'D', 2);
            INSERT INTO warehouse_locations VALUES
                (21, 'OLD-D2-A-2-3', '旧显示', 3, 'D02', 'finished', 1, 'placed', 2,
                 'rack_slot', 7, 'A', NULL, 3);
            """
        )
        connection.commit()
    finally:
        connection.close()


def test_readonly_audit_supports_pre_migration_schema_without_guessing(tmp_path: Path) -> None:
    database = tmp_path / "legacy.sqlite3"
    _legacy_database(database)
    before = (database.stat().st_size, database.stat().st_mtime_ns, _sha256(database))

    report = audit_database(database)

    after = (database.stat().st_size, database.stat().st_mtime_ns, _sha256(database))
    assert after == before
    assert report["read_only"] is True
    assert report["summary"]["location_total"] == 3
    assert report["summary"]["manual_mapping_required"] == 2
    canonical = next(row for row in report["mappings"] if row["stable_location_id"] == 12)
    assert canonical["status"] == "already_canonical"
    legacy = next(row for row in report["mappings"] if row["stable_location_id"] == 11)
    assert legacy["status"] == "manual_required"
    assert legacy["recommended_address"] is None
    assert legacy["decision"] == "do_not_guess"


def test_structured_facts_can_propose_mapping_without_changing_stable_identity(tmp_path: Path) -> None:
    database = tmp_path / "structured.sqlite3"
    _structured_database(database)

    report = audit_database(database)

    assert report["mappings"] == [
        {
            "stable_location_id": 21,
            "current_code": "OLD-D2-A-2-3",
            "current_name": "旧显示",
            "warehouse_floor": 3,
            "area_code": "D02",
            "format": "free_text_or_other",
            "status": "structured_candidate",
            "recommended_address": "3F-D02-A-02-03",
            "old_alias": "OLD-D2-A-2-3",
            "decision": "verify_map_and_physical_label",
        }
    ]


def test_cli_writes_exclusive_reports_and_keeps_database_byte_identical(tmp_path: Path) -> None:
    database = tmp_path / "factory-copy.sqlite3"
    output_dir = tmp_path / "reports"
    _legacy_database(database)
    before = (database.stat().st_size, database.stat().st_mtime_ns, _sha256(database))

    assert main(["--database", str(database), "--output-dir", str(output_dir)]) == 0

    assert (database.stat().st_size, database.stat().st_mtime_ns, _sha256(database)) == before
    payload = json.loads(
        (output_dir / "p1_86_location_address_audit.json").read_text(encoding="utf-8")
    )
    assert payload["task_id"] == "P1-86A"
    assert (output_dir / "p1_86_location_address_mapping.csv").exists()
    assert (output_dir / "p1_86_location_address_audit.md").exists()
    assert main(["--database", str(database), "--output-dir", str(output_dir)]) == 2
    assert (database.stat().st_size, database.stat().st_mtime_ns, _sha256(database)) == before
