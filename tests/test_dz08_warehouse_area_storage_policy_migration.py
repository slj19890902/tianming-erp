from __future__ import annotations

import importlib.util
from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
import pytest


ROOT = Path(__file__).resolve().parents[1]
PARENT = "dy07v8x9z96"
TARGET = "dz08v8x9z97"
MIGRATION = ROOT / "alembic" / "versions" / "dz08v8x9z97_warehouse_area_storage_policies.py"


def _config(monkeypatch: pytest.MonkeyPatch, path: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(path))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def test_migration_is_linear() -> None:
    spec = importlib.util.spec_from_file_location("dz08_policy", MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == TARGET
    assert module.down_revision == PARENT


def test_migration_round_trip_and_fails_closed_after_binding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "dz08.sqlite3"
    config = _config(monkeypatch, path)
    command.upgrade(config, PARENT)
    command.upgrade(config, TARGET)
    command.downgrade(config, PARENT)
    command.upgrade(config, TARGET)
    with sqlite3.connect(path) as connection:
        floor_id = connection.execute(
            "SELECT id FROM warehouse_floors ORDER BY id LIMIT 1"
        ).fetchone()[0]
        area_id = connection.execute(
            "INSERT INTO warehouse_areas "
            "(floor_id,area_code,area_name,planned_location_count,planned_pallet_capacity,"
            "construction_status,capacity_review_status,capacity_eligible) "
            "VALUES (?, 'DZ08', '迁移测试区', 0, 0, 'layout_building', 'pending', 0) "
            "RETURNING id",
            (floor_id,),
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO warehouse_area_storage_policies "
            "(area_id,map_feature_id,allowed_inventory_types_json,storage_layout,status,version) "
            "VALUES (?, 'zone-dz08', '[\"finished\"]', 'pallet_ground', 'draft', 1)",
            (area_id,),
        )
        connection.commit()
    with pytest.raises(RuntimeError, match="禁止破坏性降级"):
        command.downgrade(config, PARENT)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
