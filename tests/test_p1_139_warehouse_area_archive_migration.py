from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy.orm import sessionmaker

from app.core.database import create_sqlite_engine
from app.models.user import User
from app.models.warehouse_inventory import (
    WarehouseArea,
    WarehouseAreaStoragePolicy,
    WarehouseFloor,
)


OLD_HEAD = "jc64v8x9z53"
NEW_HEAD = "jf65v8x9z54"
TARGET_TABLES = (
    "warehouse_areas",
    "warehouse_area_storage_policies",
)


def _config(database_path: Path) -> Config:
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option(
        "sqlalchemy.url", f"sqlite+pysqlite:///{database_path.as_posix()}"
    )
    return config


def _related_trigger_definitions(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "select tbl_name, sql from sqlite_master "
        "where type = 'trigger' and sql is not null"
    ).fetchall()
    return {
        str(sql)
        for table_name, sql in rows
        if str(table_name) in TARGET_TABLES
        or any(table in str(sql).lower() for table in TARGET_TABLES)
    }


def _assert_healthy(connection: sqlite3.Connection, revision: str) -> None:
    assert connection.execute("pragma integrity_check").fetchone()[0] == "ok"
    assert not connection.execute("pragma foreign_key_check").fetchall()
    assert connection.execute("select version_num from alembic_version").fetchone()[0] == revision


def test_p1_139_migration_roundtrip_preserves_sqlite_guards(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "p1-139-roundtrip.sqlite3"
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database_path))
    config = _config(database_path)

    command.upgrade(config, OLD_HEAD)
    with sqlite3.connect(database_path) as connection:
        _assert_healthy(connection, OLD_HEAD)
        trigger_definitions = _related_trigger_definitions(connection)
        assert trigger_definitions

    command.upgrade(config, NEW_HEAD)
    with sqlite3.connect(database_path) as connection:
        _assert_healthy(connection, NEW_HEAD)
        columns = {
            row[1]
            for row in connection.execute(
                "pragma table_info(warehouse_area_storage_policies)"
            )
        }
        assert {
            "archived_at",
            "archived_by",
            "archive_operation_key",
            "archive_request_hash",
            "archive_feature_snapshot_json",
        }.issubset(columns)
        assert _related_trigger_definitions(connection) == trigger_definitions

    command.downgrade(config, OLD_HEAD)
    with sqlite3.connect(database_path) as connection:
        _assert_healthy(connection, OLD_HEAD)
        assert _related_trigger_definitions(connection) == trigger_definitions

    command.upgrade(config, NEW_HEAD)
    with sqlite3.connect(database_path) as connection:
        _assert_healthy(connection, NEW_HEAD)
        assert _related_trigger_definitions(connection) == trigger_definitions

    engine = create_sqlite_engine(database_path)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with factory() as db:
            user = User(
                username="p1-139-migration-admin",
                password_hash="test-only",
                role="admin",
                real_name="P1-139 管理员",
                is_active=True,
                must_change_password=False,
                customer_access_mode="all",
                ui_mode="standard",
            )
            floor = WarehouseFloor(
                floor_code="9F",
                floor_name="隔离测试楼层",
                floor_number=9,
                construction_status="enabled",
                planning_reference_pallet_capacity=0,
            )
            db.add_all([user, floor])
            db.flush()
            area = WarehouseArea(
                floor_id=floor.id,
                area_code="ARCHIVE-TEST",
                area_name="已归档隔离区域",
                planned_location_count=0,
                planned_pallet_capacity=0,
                construction_status="archived",
                capacity_review_status="pending",
                capacity_eligible=False,
            )
            db.add(area)
            db.flush()
            db.add(
                WarehouseAreaStoragePolicy(
                    area_id=area.id,
                    map_feature_id="zone-migration-archive-test",
                    allowed_inventory_types_json='["finished"]',
                    storage_layout="pallet_ground",
                    status="archived",
                    version=2,
                    updated_by=user.id,
                    archived_at=datetime(2026, 9, 1, 9, 0),
                    archived_by=user.id,
                    archive_operation_key="p1-139-migration-downgrade-guard",
                    archive_request_hash="a" * 64,
                    archive_feature_snapshot_json="{}",
                )
            )
            db.commit()
    finally:
        engine.dispose()

    with pytest.raises(RuntimeError, match="archived warehouse areas exist"):
        command.downgrade(config, OLD_HEAD)
    with sqlite3.connect(database_path) as connection:
        _assert_healthy(connection, NEW_HEAD)
        assert _related_trigger_definitions(connection) == trigger_definitions
