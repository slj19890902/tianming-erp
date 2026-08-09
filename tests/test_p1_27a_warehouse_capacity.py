from __future__ import annotations

from datetime import datetime
import importlib.util
from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
import pytest

from app.models.warehouse_inventory import WarehouseArea, WarehouseFloor
from app.services.warehouse_twin_dashboard import warehouse_capacity_summary


ROOT = Path(__file__).resolve().parents[1]
PARENT_REVISION = "dt02v8x9z91"
TARGET_REVISION = "du03v8x9z92"
MIGRATION = ROOT / "alembic" / "versions" / "du03v8x9z92_warehouse_capacity_reviews.py"


def _config(monkeypatch: pytest.MonkeyPatch, path: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(path))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def _checks(path: Path) -> tuple[str, int]:
    with sqlite3.connect(path) as connection:
        return (
            connection.execute("PRAGMA integrity_check").fetchone()[0],
            len(connection.execute("PRAGMA foreign_key_check").fetchall()),
        )


def _seed_parent_floor_data(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executemany(
            """
            INSERT OR IGNORE INTO warehouse_floors (
                floor_code, floor_name, floor_number, construction_status
            ) VALUES (?, ?, ?, 'enabled')
            """,
            (("1F", "一楼生产与周转区", 1), ("3F", "三楼成品仓", 3)),
        )
        floor_ids = dict(
            connection.execute("SELECT floor_number, id FROM warehouse_floors").fetchall()
        )
        connection.executemany(
            """
            INSERT OR IGNORE INTO warehouse_areas (
                floor_id, area_code, area_name, planned_location_count,
                planned_pallet_capacity, construction_status
            ) VALUES (?, ?, ?, 1, ?, 'enabled')
            """,
            (
                (floor_ids[1], "D1", "一楼成品区", 34),
                (floor_ids[3], "A1", "三楼成品区", 250),
            ),
        )
        connection.commit()


def test_capacity_migration_is_linear_after_printing_plate_head() -> None:
    spec = importlib.util.spec_from_file_location("warehouse_capacity_reviews", MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == TARGET_REVISION
    assert module.down_revision == PARENT_REVISION


def test_capacity_migration_round_trip_seeds_only_planning_references(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "warehouse-capacity.sqlite3"
    config = _config(monkeypatch, path)
    command.upgrade(config, PARENT_REVISION)
    _seed_parent_floor_data(path)

    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(path) as connection:
        assert dict(
            connection.execute(
                "SELECT floor_number, planning_reference_pallet_capacity FROM warehouse_floors"
            ).fetchall()
        ) == {1: 34, 3: 250}
        total_areas = connection.execute(
            "SELECT COUNT(*) FROM warehouse_areas"
        ).fetchone()[0]
        assert connection.execute(
            "SELECT COUNT(*) FROM warehouse_areas WHERE "
            "capacity_review_status = 'pending' AND capacity_eligible = 0 "
            "AND confirmed_pallet_capacity IS NULL AND capacity_reviewed_at IS NULL"
        ).fetchone()[0] == total_areas
    assert _checks(path) == ("ok", 0)

    command.downgrade(config, PARENT_REVISION)
    with sqlite3.connect(path) as connection:
        floor_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(warehouse_floors)")
        }
        area_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(warehouse_areas)")
        }
        assert "planning_reference_pallet_capacity" not in floor_columns
        assert "capacity_review_status" not in area_columns
    assert _checks(path) == ("ok", 0)

    command.upgrade(config, TARGET_REVISION)
    assert _checks(path) == ("ok", 0)


def test_capacity_migration_downgrade_fails_closed_after_review(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "warehouse-capacity-reviewed.sqlite3"
    config = _config(monkeypatch, path)
    command.upgrade(config, PARENT_REVISION)
    _seed_parent_floor_data(path)
    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            UPDATE warehouse_areas SET
                capacity_review_status = 'confirmed',
                capacity_eligible = 1,
                confirmed_pallet_capacity = 34,
                capacity_reviewed_by = 'admin',
                capacity_reviewed_at = '2026-08-09 12:00:00'
            WHERE area_code = 'D1'
            """
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="禁止破坏性降级"):
        command.downgrade(config, PARENT_REVISION)
    assert _checks(path) == ("ok", 0)


def test_planning_thresholds_match_confirmed_factory_reference() -> None:
    floor = WarehouseFloor(
        floor_code="1F",
        floor_name="一楼",
        floor_number=1,
        planning_reference_pallet_capacity=34,
        construction_status="enabled",
    )
    planning = warehouse_capacity_summary(floor, occupied_pallets=28, visible=True)
    assert planning["basis"] == "planning"
    assert planning["thresholds"] == {"attention": 28, "warning": 31, "critical": 33}
    assert planning["alert_level"] == "attention"
    assert planning["safe_pallet_capacity"] is None

    area = WarehouseArea(
        floor=floor,
        floor_id=1,
        area_code="D1",
        area_name="成品区",
        planned_location_count=34,
        planned_pallet_capacity=34,
        construction_status="enabled",
        capacity_review_status="confirmed",
        capacity_eligible=True,
        confirmed_pallet_capacity=34,
        capacity_reviewed_by="admin",
        capacity_reviewed_at=datetime(2026, 8, 9, 12, 0, 0),
    )
    floor.areas = [area]
    confirmed = warehouse_capacity_summary(floor, occupied_pallets=33, visible=True)
    assert confirmed["basis"] == "confirmed"
    assert confirmed["safe_pallet_capacity"] == 34
    assert confirmed["coverage_percent"] == 100.0
    assert confirmed["alert_level"] == "critical"
