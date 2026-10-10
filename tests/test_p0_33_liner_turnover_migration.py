from __future__ import annotations

from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
import pytest


ROOT = Path(__file__).resolve().parents[1]
PARENT_REVISION = "iz61v8x9z50"
TARGET_REVISION = "ja62v8x9z51"
MAP_REVISION = "3994317ae14a7f18"
OLD_AREA_REMARKS = "V11 过道临放位；不是普通长期货位，现有布局与业务门禁保持不变。"
AREA_SPECS = {
    "F34": (3, "d068d43e-58a5-40d8-bb7f-0ea88d714e2e"),
    "F12": (8, "0a1c6bf6-c0d9-4217-b9a0-2527db20b9d9"),
}


def _config(monkeypatch: pytest.MonkeyPatch, database_path: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database_path))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option(
        "sqlalchemy.url", f"sqlite:///{database_path.as_posix()}"
    )
    return config


def _turnover_rows(database_path: Path) -> list[tuple]:
    with sqlite3.connect(database_path) as connection:
        return connection.execute(
            """
            SELECT location.location_code, location.is_active,
                   location.storage_type, location.is_temporary,
                   location.address_kind, layout.layout_kind,
                   location.remarks
            FROM warehouse_locations AS location
            JOIN floor3_location_layouts AS layout
              ON layout.location_id=location.id
            WHERE location.area_code IN ('F34','F12')
            ORDER BY CASE location.area_code WHEN 'F34' THEN 0 ELSE 1 END,
                     location.location_code
            """
        ).fetchall()


def _health(database_path: Path) -> tuple[str, list[tuple]]:
    with sqlite3.connect(database_path) as connection:
        return (
            connection.execute("PRAGMA integrity_check").fetchone()[0],
            connection.execute("PRAGMA foreign_key_check").fetchall(),
        )


def _prepare_formal_current_map_baseline(database_path: Path) -> None:
    """Reproduce only the audited F34/F12 slice that fg42 skips on empty DBs."""

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO warehouse_current_map_migration_snapshots (
                migration_key,payload_json,post_fingerprint,created_at
            ) VALUES (
                'p0-26-current-map-test','{}',:fingerprint,CURRENT_TIMESTAMP
            )
            """,
            {"fingerprint": "0" * 64},
        )
        for area_code, (capacity, feature_id) in AREA_SPECS.items():
            area_id = connection.execute(
                """
                SELECT area.id
                FROM warehouse_areas AS area
                JOIN warehouse_floors AS floor ON floor.id=area.floor_id
                WHERE floor.floor_number=3 AND upper(area.area_code)=:area_code
                """,
                {"area_code": area_code},
            ).fetchone()[0]
            connection.execute(
                """
                UPDATE warehouse_areas
                SET planned_location_count=:capacity,
                    planned_pallet_capacity=:capacity,
                    construction_status='enabled',
                    remarks=:remarks,
                    capacity_review_status='confirmed',
                    capacity_eligible=1,
                    confirmed_pallet_capacity=:capacity,
                    capacity_reviewed_by='P0-33 migration test',
                    capacity_reviewed_at=CURRENT_TIMESTAMP
                WHERE id=:area_id
                """,
                {
                    "area_id": area_id,
                    "capacity": capacity,
                    "remarks": OLD_AREA_REMARKS,
                },
            )
            connection.execute(
                """
                INSERT INTO warehouse_area_storage_policies (
                    area_id,map_feature_id,allowed_inventory_types_json,
                    storage_layout,status,published_map_revision,version
                ) VALUES (
                    :area_id,:feature_id,'["finished"]',
                    'pallet_ground','published',:map_revision,1
                )
                """,
                {
                    "area_id": area_id,
                    "feature_id": feature_id,
                    "map_revision": MAP_REVISION,
                },
            )
            connection.execute(
                """
                UPDATE warehouse_locations
                SET warehouse_type='finished', warehouse_floor=3,
                    storage_type='temporary_aisle', is_temporary=1,
                    source_version='CURRENT_MAP', placement_status='placed',
                    is_active=0, address_kind='functional',
                    address_area_id=:area_id, remarks=NULL
                WHERE upper(area_code)=:area_code
                """,
                {"area_id": area_id, "area_code": area_code},
            )
            connection.execute(
                """
                UPDATE floor3_location_layouts
                SET source_type='manual', layout_kind='logical_anchor'
                WHERE location_id IN (
                    SELECT id FROM warehouse_locations
                    WHERE upper(area_code)=:area_code
                )
                """,
                {"area_code": area_code},
            )
        connection.commit()


def test_liner_turnover_migration_is_linear_and_round_trips(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "liner-turnover-roundtrip.sqlite3"
    config = _config(monkeypatch, database_path)
    command.upgrade(config, PARENT_REVISION)
    _prepare_formal_current_map_baseline(database_path)
    before = _turnover_rows(database_path)
    assert len(before) == 11
    assert all(row[1] == 0 for row in before)

    command.upgrade(config, TARGET_REVISION)
    after = _turnover_rows(database_path)
    assert [row[0] for row in after[:3]] == ["F34-P01", "F34-P02", "F34-P03"]
    assert [row[0] for row in after[3:]] == [
        f"F12-P{index:02d}" for index in range(1, 9)
    ]
    assert all(
        row[1:6] == (1, "temporary_aisle", 1, "functional", "logical_anchor")
        for row in after
    )
    assert all("P0-33" in row[6] for row in after)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            """
            SELECT COUNT(*)
            FROM warehouse_ground_layout_slots AS slot
            JOIN warehouse_locations AS location ON location.id=slot.location_id
            WHERE location.area_code IN ('F34','F12')
            """
        ).fetchone()[0] == 0
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == TARGET_REVISION
    assert _health(database_path) == ("ok", [])

    command.downgrade(config, PARENT_REVISION)
    downgraded = _turnover_rows(database_path)
    assert all(row[1] == 0 and row[6] is None for row in downgraded)
    assert _health(database_path) == ("ok", [])

    command.upgrade(config, TARGET_REVISION)
    assert all(row[1] == 1 for row in _turnover_rows(database_path))
    assert _health(database_path) == ("ok", [])


def test_liner_turnover_downgrade_fails_closed_with_current_pallet(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "liner-turnover-fail-closed.sqlite3"
    config = _config(monkeypatch, database_path)
    command.upgrade(config, PARENT_REVISION)
    _prepare_formal_current_map_baseline(database_path)
    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(database_path) as connection:
        location_id = connection.execute(
            "SELECT id FROM warehouse_locations WHERE location_code='F34-P01'"
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO inventory_pallets (
                pallet_code,location_id,location_occupancy_key,status,
                is_current,needs_relocation,version
            ) VALUES (
                'P0-33-DOWNGRADE-GUARD',?,'PRIMARY','active',1,1,1
            )
            """,
            (location_id,),
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="当前栈板"):
        command.downgrade(config, PARENT_REVISION)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == TARGET_REVISION
        assert connection.execute(
            "SELECT is_active FROM warehouse_locations WHERE location_code='F34-P01'"
        ).fetchone()[0] == 1
    assert _health(database_path) == ("ok", [])


def test_liner_turnover_upgrade_rejects_layout_drift_before_writing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "liner-turnover-drift.sqlite3"
    config = _config(monkeypatch, database_path)
    command.upgrade(config, PARENT_REVISION)
    _prepare_formal_current_map_baseline(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            UPDATE floor3_location_layouts
            SET layout_kind='physical_pallet'
            WHERE location_id=(
                SELECT id FROM warehouse_locations WHERE location_code='F34-P01'
            )
            """
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="F34-P01"):
        command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == PARENT_REVISION
        assert connection.execute(
            """
            SELECT COUNT(*) FROM warehouse_locations
            WHERE area_code IN ('F34','F12') AND is_active=1
            """
        ).fetchone()[0] == 0
    assert _health(database_path) == ("ok", [])


def test_liner_turnover_migration_is_noop_on_empty_installation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "liner-turnover-empty.sqlite3"
    config = _config(monkeypatch, database_path)
    command.upgrade(config, TARGET_REVISION)
    at_target = _turnover_rows(database_path)
    assert len(at_target) == 11
    assert all(row[1] == 1 and row[5] == "unknown" for row in at_target)

    command.downgrade(config, PARENT_REVISION)
    after_downgrade = _turnover_rows(database_path)
    assert after_downgrade == at_target
    assert _health(database_path) == ("ok", [])
