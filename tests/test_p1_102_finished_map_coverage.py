from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from scripts.audit.p1_102_finished_map_coverage import (
    _open_readonly,
    collect,
    main,
)


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "audit"
    / "p1_102_finished_map_coverage.py"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _runtime_map(path: Path) -> None:
    payload = {
        "schema_version": 1,
        "generated_at": "2026-08-25T12:00:00+08:00",
        "floors": {
            "1F": {
                "floor_code": "1F",
                "revision": "runtime-1f-current",
                "bounds_mm": {
                    "min_x": 0,
                    "min_y": 0,
                    "max_x": 30000,
                    "max_y": 20000,
                },
                "features": [
                    {
                        "id": "zone-fin",
                        "feature_kind": "zone",
                        "erp_area_code": "FIN",
                        "points": [[0, 0], [8000, 0], [8000, 6000], [0, 6000]],
                    },
                    {
                        "id": "zone-g1",
                        "feature_kind": "zone",
                        "erp_area_code": "G1",
                        "points": [[9000, 0], [17000, 0], [17000, 6000], [9000, 6000]],
                    },
                    {
                        "id": "zone-outside",
                        "feature_kind": "zone",
                        "erp_area_code": "OUT",
                        "points": [[40000, 0], [48000, 0], [48000, 6000], [40000, 6000]],
                    },
                ],
            },
            "3F": {
                "floor_code": "3F",
                "revision": "runtime-3f-current",
                "bounds_mm": {
                    "min_x": 0,
                    "min_y": 0,
                    "max_x": 30000,
                    "max_y": 20000,
                },
                "features": [
                    {
                        "id": "zone-a1",
                        "feature_kind": "zone",
                        "erp_area_code": "A1",
                        "points": [[0, 0], [8000, 0], [8000, 6000], [0, 6000]],
                    },
                    {
                        "id": "zone-b1-left",
                        "feature_kind": "zone",
                        "erp_area_code": "B1",
                        "points": [[9000, 0], [13000, 0], [13000, 6000], [9000, 6000]],
                    },
                    {
                        "id": "zone-b1-right",
                        "feature_kind": "zone",
                        "erp_area_code": "B1",
                        "points": [[14000, 0], [18000, 0], [18000, 6000], [14000, 6000]],
                    },
                ],
            },
        },
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _database(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE alembic_version (version_num TEXT NOT NULL);
            INSERT INTO alembic_version VALUES ('de39v8x9z28');

            CREATE TABLE warehouse_floors (
                id INTEGER PRIMARY KEY,
                floor_number INTEGER NOT NULL,
                construction_status TEXT NOT NULL
            );
            INSERT INTO warehouse_floors VALUES
                (1, 1, 'enabled'),
                (3, 3, 'enabled');

            CREATE TABLE warehouse_areas (
                id INTEGER PRIMARY KEY,
                floor_id INTEGER NOT NULL,
                area_code TEXT NOT NULL,
                construction_status TEXT NOT NULL
            );
            INSERT INTO warehouse_areas VALUES
                (1, 1, 'FIN', 'enabled'),
                (2, 1, 'OLD', 'enabled'),
                (3, 3, 'A1', 'enabled'),
                (4, 3, 'B1', 'enabled'),
                (5, 1, 'G1', 'enabled'),
                (6, 1, 'OUT', 'enabled');

            CREATE TABLE warehouse_area_storage_policies (
                id INTEGER PRIMARY KEY,
                area_id INTEGER NOT NULL,
                map_feature_id TEXT,
                status TEXT,
                published_map_revision TEXT
            );
            INSERT INTO warehouse_area_storage_policies VALUES
                (1, 1, 'zone-fin', 'published', 'runtime-1f-current'),
                (2, 2, 'zone-old', 'published', 'runtime-1f-stale'),
                (3, 5, 'zone-g1', 'published', 'runtime-1f-current'),
                (4, 6, 'zone-outside', 'published', 'runtime-1f-current');

            CREATE TABLE warehouse_locations (
                id INTEGER PRIMARY KEY,
                location_code TEXT,
                is_active INTEGER,
                warehouse_floor INTEGER,
                area_code TEXT,
                storage_type TEXT,
                source_version TEXT,
                placement_status TEXT
            );
            INSERT INTO warehouse_locations VALUES
                (1, '1F-FIN-01', 1, 1, 'FIN', 'rack', 'CURRENT_MAP', 'placed'),
                (2, '1F-OLD-01', 1, 1, 'OLD', 'rack', 'TWIN_V1', 'placed'),
                (3, '3F-A1-01', 1, 3, 'A1', 'rack', 'V11', 'placed'),
                (4, '3F-B1-01', 1, 3, 'B1', 'rack', 'V11', 'placed'),
                (5, '1F-G1-01', 1, 1, 'G1', 'ground', 'TWIN_V1', 'placed'),
                (6, '1F-G1-02', 1, 1, 'G1', 'ground', 'TWIN_V1', 'placed'),
                (7, '1F-OUT-01', 1, 1, 'OUT', 'rack', 'TWIN_V1', 'placed'),
                (8, '3F-A1-EMPTY', 1, 3, 'A1', 'rack', 'V11', 'placed');

            CREATE TABLE floor3_location_layouts (
                id INTEGER PRIMARY KEY,
                location_id INTEGER NOT NULL
            );
            INSERT INTO floor3_location_layouts VALUES
                (1, 1), (2, 2), (3, 3), (4, 4), (5, 5), (6, 6), (7, 7);

            CREATE TABLE warehouse_ground_layout_plans (
                id INTEGER PRIMARY KEY,
                area_id INTEGER NOT NULL,
                status TEXT,
                published_map_revision TEXT
            );
            INSERT INTO warehouse_ground_layout_plans VALUES
                (1, 5, 'published', 'runtime-1f-current'),
                (2, 5, 'published', 'runtime-1f-historical'),
                (3, 5, 'draft', 'runtime-1f-current');
            CREATE TABLE warehouse_ground_layout_slots (
                id INTEGER PRIMARY KEY,
                plan_id INTEGER NOT NULL,
                location_id INTEGER NOT NULL
            );
            INSERT INTO warehouse_ground_layout_slots VALUES
                (1, 1, 5),
                (2, 2, 5),
                (3, 3, 5);

            CREATE TABLE inventory_lots (
                id INTEGER PRIMARY KEY,
                inventory_type TEXT NOT NULL,
                warehouse_location_id INTEGER,
                quantity_available INTEGER NOT NULL,
                quantity_reserved INTEGER NOT NULL,
                quantity_damaged INTEGER NOT NULL,
                status TEXT NOT NULL
            );
            INSERT INTO inventory_lots VALUES
                (1, 'finished', 1, 10, 5, 2, 'active'),
                (2, 'finished', 2, 0, 6, 0, 'frozen'),
                (3, 'finished', 3, 0, 0, 4, 'active'),
                (4, 'finished', 4, 3, 0, 0, 'active'),
                (5, 'finished', 5, 8, 2, 1, 'active'),
                (6, 'finished', 6, 1, 0, 0, 'active'),
                (7, 'finished', 7, 2, 0, 0, 'active'),
                (8, 'finished', 1, 99, 0, 0, 'closed'),
                (9, 'semi_finished', 1, 99, 0, 0, 'active'),
                (10, 'finished', 1, 0, 0, 0, 'active'),
                (11, 'finished', 1, 2, 0, 0, 'active');

            CREATE TABLE inventory_pallets (
                id INTEGER PRIMARY KEY,
                location_id INTEGER,
                status TEXT NOT NULL,
                is_current INTEGER NOT NULL
            );
            INSERT INTO inventory_pallets VALUES
                (1, 1, 'active', 1),
                (2, 2, 'active', 1),
                (3, 3, 'active', 1),
                (4, 4, 'active', 1),
                (5, 5, 'active', 1),
                (6, 6, 'active', 1),
                (7, 7, 'active', 1),
                (8, 8, 'active', 1);

            CREATE TABLE inventory_pallet_items (
                id INTEGER PRIMARY KEY,
                pallet_id INTEGER NOT NULL,
                inventory_lot_id INTEGER,
                quantity NUMERIC NOT NULL
            );
            INSERT INTO inventory_pallet_items VALUES
                (1, 1, 1, 17),
                (2, 2, 2, 6),
                (3, 3, 3, 4),
                (4, 4, 4, 3),
                (5, 5, 5, 11),
                (6, 6, 6, 1),
                (7, 7, 7, 2);

            CREATE TABLE warehouse_ground_occupancies (
                id INTEGER PRIMARY KEY,
                pallet_id INTEGER NOT NULL,
                primary_location_id INTEGER NOT NULL,
                capacity_quantity INTEGER NOT NULL,
                status TEXT NOT NULL
            );
            INSERT INTO warehouse_ground_occupancies VALUES
                (1, 5, 5, 11, 'active');

            CREATE TABLE warehouse_ground_occupancy_slots (
                id INTEGER PRIMARY KEY,
                occupancy_id INTEGER NOT NULL,
                location_id INTEGER NOT NULL,
                status TEXT NOT NULL
            );
            INSERT INTO warehouse_ground_occupancy_slots VALUES
                (1, 1, 5, 'active');
            """
        )
        connection.commit()
    finally:
        connection.close()


@pytest.fixture
def isolated_inputs(tmp_path: Path) -> tuple[Path, Path]:
    database = tmp_path / "isolated.sqlite3"
    runtime_map = tmp_path / "runtime-twin.json"
    _database(database)
    _runtime_map(runtime_map)
    return database, runtime_map


def test_collect_is_read_only_anonymous_and_quantity_conserving(
    isolated_inputs: tuple[Path, Path],
) -> None:
    database, runtime_map = isolated_inputs
    before = (database.stat().st_size, database.stat().st_mtime_ns, _sha256(database))

    report = collect(database, runtime_map)

    after = (database.stat().st_size, database.stat().st_mtime_ns, _sha256(database))
    assert after == before
    assert report["database"]["connection_mode"] == "SQLite URI mode=ro"
    assert report["database"]["query_only"] == 1
    assert report["database"]["total_changes_before"] == 0
    assert report["database"]["total_changes_after"] == 0
    assert report["database"]["file_bytes_unchanged_during_audit"] is True
    assert report["audit_contract"]["each_lot_counted_once"] is True
    assert report["alembic_heads"] == ["de39v8x9z28"]
    assert report["runtime_map"]["revisions"] == {
        "1F": "runtime-1f-current",
        "3F": "runtime-3f-current",
    }
    assert report["runtime_map"]["zone_counts"] == {"1F": 2, "3F": 3}
    assert report["script"]["sha256"] == _sha256(SCRIPT_PATH)
    assert len(report["database"]["sha256_before"]) == 64
    assert len(report["runtime_map"]["sha256"]) == 64

    assert report["summary"] == {
        "total": {
            "lot_count": 8,
            "available_reserved_quantity": 39,
            "physical_quantity_including_damaged": 46,
        },
        "mapped": {
            "lot_count": 4,
            "available_reserved_quantity": 27,
            "physical_quantity_including_damaged": 34,
        },
        "unlocated": {
            "lot_count": 4,
            "available_reserved_quantity": 12,
            "physical_quantity_including_damaged": 12,
        },
        "all_located": False,
        "conservation": {
            "lot_count": True,
            "available_reserved_quantity": True,
            "physical_quantity_including_damaged": True,
        },
        "pallet_coverage": {
            "total": {
                "lot_count": 8,
                "available_reserved_quantity": 39,
                "physical_quantity_including_damaged": 46,
            },
            "current_same_location_pallet": {
                "lot_count": 7,
                "available_reserved_quantity": 37,
                "physical_quantity_including_damaged": 44,
            },
            "without_current_same_location_pallet": {
                "lot_count": 1,
                "available_reserved_quantity": 2,
                "physical_quantity_including_damaged": 2,
            },
            "mapped_location_without_current_pallet": {
                "lot_count": 1,
                "available_reserved_quantity": 2,
                "physical_quantity_including_damaged": 2,
            },
            "all_on_current_same_location_pallet": False,
            "conservation": {
                "lot_count": True,
                "available_reserved_quantity": True,
                "physical_quantity_including_damaged": True,
            },
        },
        "empty_current_pallet_residue_count": 1,
        "twin_ground_occupancy_coverage": {
            "required": {
                "lot_count": 1,
                "available_reserved_quantity": 10,
                "physical_quantity_including_damaged": 11,
            },
            "valid": {
                "lot_count": 1,
                "available_reserved_quantity": 10,
                "physical_quantity_including_damaged": 11,
            },
            "invalid": {
                "lot_count": 0,
                "available_reserved_quantity": 0,
                "physical_quantity_including_damaged": 0,
            },
            "without_active_occupancy": {
                "lot_count": 0,
                "available_reserved_quantity": 0,
                "physical_quantity_including_damaged": 0,
            },
            "all_valid": True,
            "conservation": {
                "lot_count": True,
                "available_reserved_quantity": True,
                "physical_quantity_including_damaged": True,
            },
        },
        "twin_ground_without_active_occupancy": {
            "lot_count": 0,
            "available_reserved_quantity": 0,
            "physical_quantity_including_damaged": 0,
        },
        "projection_complete": False,
    }
    assert report["pallet_issues"] == {
        "mapped_location_without_current_pallet": [
            {
                "floor_code": "1F",
                "area_code": "FIN",
                "location_code": "1F-FIN-01",
                "lot_count": 1,
                "available_reserved_quantity": 2,
                "physical_quantity_including_damaged": 2,
            }
        ],
        "empty_current_pallet_residue": [
            {
                "floor_code": "3F",
                "area_code": "A1",
                "location_code": "3F-A1-EMPTY",
            }
        ],
        "twin_ground_without_valid_active_occupancy": [],
        "twin_ground_without_active_occupancy": [],
    }
    reasons = {
        row["location_code"]: row["reason_code"]
        for row in report["location_aggregates"]
    }
    assert reasons == {
        "1F-FIN-01": "mapped",
        "1F-G1-01": "mapped",
        "1F-G1-02": "ground_layout_not_current",
        "1F-OLD-01": "policy_revision_stale",
        "1F-OUT-01": "policy_feature_area_mismatch",
        "3F-A1-01": "mapped",
        "3F-B1-01": "v11_area_not_unique",
    }
    fin = next(
        row
        for row in report["location_aggregates"]
        if row["location_code"] == "1F-FIN-01"
    )
    assert fin["lot_count"] == 2
    assert fin["available_reserved_quantity"] == 17
    assert fin["physical_quantity_including_damaged"] == 19
    serialized = json.dumps(report, ensure_ascii=False).lower()
    assert "customer" not in serialized
    assert "product" not in serialized
    assert all("lot_id" not in row for row in report["location_aggregates"])


def test_historical_ground_plans_do_not_duplicate_finished_lots(
    isolated_inputs: tuple[Path, Path],
) -> None:
    database, runtime_map = isolated_inputs
    connection = sqlite3.connect(database)
    try:
        plan_count = int(
            connection.execute(
                """
                SELECT count(*)
                FROM warehouse_ground_layout_slots
                WHERE location_id = 5
                """
            ).fetchone()[0]
        )
    finally:
        connection.close()
    assert plan_count == 3

    report = collect(database, runtime_map)

    ground = next(
        row
        for row in report["location_aggregates"]
        if row["location_code"] == "1F-G1-01"
    )
    assert ground["classification"] == "mapped"
    assert ground["lot_count"] == 1
    assert ground["available_reserved_quantity"] == 10
    assert ground["physical_quantity_including_damaged"] == 11
    assert report["summary"]["total"] == {
        "lot_count": 8,
        "available_reserved_quantity": 39,
        "physical_quantity_including_damaged": 46,
    }


def test_twin_ground_missing_active_occupancy_is_a_visible_blocker(
    isolated_inputs: tuple[Path, Path],
) -> None:
    database, runtime_map = isolated_inputs
    connection = sqlite3.connect(database)
    try:
        connection.execute("DELETE FROM warehouse_ground_occupancy_slots")
        connection.execute("DELETE FROM warehouse_ground_occupancies")
        connection.commit()
    finally:
        connection.close()

    report = collect(database, runtime_map)

    assert report["summary"]["twin_ground_without_active_occupancy"] == {
        "lot_count": 1,
        "available_reserved_quantity": 10,
        "physical_quantity_including_damaged": 11,
    }
    assert report["summary"]["twin_ground_occupancy_coverage"]["all_valid"] is False
    assert report["summary"]["projection_complete"] is False
    assert report["pallet_issues"]["twin_ground_without_active_occupancy"] == [
        {
            "floor_code": "1F",
            "area_code": "G1",
            "location_code": "1F-G1-01",
            "reason_code": "active_occupancy_missing",
            "lot_count": 1,
            "available_reserved_quantity": 10,
            "physical_quantity_including_damaged": 11,
        }
    ]


def test_readonly_connection_rejects_write_probe_and_keeps_total_changes_zero(
    isolated_inputs: tuple[Path, Path],
) -> None:
    database, _runtime_map_path = isolated_inputs
    connection = _open_readonly(database)
    try:
        assert int(connection.execute("PRAGMA query_only").fetchone()[0]) == 1
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("CREATE TABLE forbidden_write (id INTEGER)")
        assert connection.total_changes == 0
    finally:
        connection.close()


def test_cli_prints_same_anonymous_report(
    isolated_inputs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    database, runtime_map = isolated_inputs

    assert main(["--database", str(database), "--runtime-map", str(runtime_map)]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["task_id"] == "P1-102"
    assert payload["summary"]["mapped"]["lot_count"] == 4
    assert payload["summary"]["unlocated"]["lot_count"] == 4
