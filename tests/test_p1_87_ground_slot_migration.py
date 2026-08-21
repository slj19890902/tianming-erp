from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from app.models.user import User
from app.models.warehouse_inventory import (
    WarehouseArea,
    WarehouseFloor,
    WarehouseGroundLayoutPlan,
)


ROOT = Path(__file__).resolve().parents[1]
PARENT = "bb36v8x9z25"
TARGET = "cc37v8x9z26"


def _config(monkeypatch: pytest.MonkeyPatch, database: Path) -> Config:
    protected = (ROOT / "data" / "carton_erp.sqlite3").resolve()
    actual = database.resolve()
    assert actual != protected
    monkeypatch.setenv("ERP_DATABASE_PATH", str(actual))
    monkeypatch.setenv("ERP_SECRET_KEY", "p1-87-isolated-migration-secret")
    monkeypatch.setenv("ERP_BACKUP_DIR", str((database.parent / "backups").resolve()))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{actual.as_posix()}")
    return config


def _revision(database: Path) -> str:
    with create_engine(f"sqlite:///{database.as_posix()}").connect() as connection:
        return str(connection.execute(text("SELECT version_num FROM alembic_version")).scalar())


def test_p1_87_empty_upgrade_downgrade_upgrade_keeps_single_linear_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "p187-empty.sqlite3"
    config = _config(monkeypatch, database)
    command.upgrade(config, PARENT)
    command.upgrade(config, TARGET)
    assert _revision(database) == TARGET
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    with engine.connect() as connection:
        tables = set(inspect(connection).get_table_names())
        assert {
            "warehouse_ground_layout_plans",
            "warehouse_ground_layout_slots",
            "warehouse_ground_occupancies",
            "warehouse_ground_occupancy_slots",
            "warehouse_ground_placement_mutations",
        } <= tables
        trigger_names = {
            row[0]
            for row in connection.execute(
                text("SELECT name FROM sqlite_master WHERE type='trigger'")
            )
        }
        assert "trg_ground_plans_published_immutable" in trigger_names
        assert "trg_ground_placement_mutations_immutable_delete" in trigger_names
        assert connection.execute(text("PRAGMA integrity_check")).scalar() == "ok"
        assert list(connection.execute(text("PRAGMA foreign_key_check"))) == []
    engine.dispose()

    command.downgrade(config, PARENT)
    assert _revision(database) == PARENT
    command.upgrade(config, TARGET)
    assert _revision(database) == TARGET


def test_p1_87_downgrade_fails_closed_when_a_draft_plan_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "p187-fact.sqlite3"
    config = _config(monkeypatch, database)
    command.upgrade(config, TARGET)
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    with Session(engine) as db:
        user = User(
            username="p187-migration-admin",
            password_hash="not-used",
            role="admin",
            real_name="P1-87 migration admin",
            must_change_password=False,
        )
        floor = db.query(WarehouseFloor).filter(WarehouseFloor.floor_number == 3).one()
        db.add(user)
        db.flush()
        area = WarehouseArea(
            floor_id=floor.id,
            area_code="A01",
            area_name="三楼 A1 地堆区",
            construction_status="enabled",
            address_zone_code="A",
            address_subzone_no=1,
        )
        db.add(area)
        db.flush()
        db.add(
            WarehouseGroundLayoutPlan(
                area_id=area.id,
                status="draft",
                target_slot_count=1,
                numbering_origin="south",
                row_direction="from_aisle_inward",
                slot_direction="left_to_right",
                row_start_no=1,
                slot_start_no=1,
                draft_map_revision="p187-map",
                preview_fingerprint="a" * 64,
                version=1,
                updated_by=user.id,
            )
        )
        db.commit()
    engine.dispose()

    with pytest.raises(RuntimeError, match="P1-87"):
        command.downgrade(config, PARENT)
    assert _revision(database) == TARGET
