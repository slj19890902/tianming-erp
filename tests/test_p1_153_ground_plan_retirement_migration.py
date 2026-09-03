from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.user import User
from app.models.warehouse_inventory import (
    WarehouseArea,
    WarehouseFloor,
    WarehouseGroundLayoutPlan,
    WarehouseGroundLayoutPlanRetirement,
)


ROOT = Path(__file__).resolve().parents[1]
PARENT = "jo73v8x9z62"
TARGET = "jp74v8x9z63"


def _config(monkeypatch: pytest.MonkeyPatch, database: Path) -> Config:
    protected = (ROOT / "data" / "carton_erp.sqlite3").resolve()
    actual = database.resolve()
    assert actual != protected
    monkeypatch.setenv("ERP_DATABASE_PATH", str(actual))
    monkeypatch.setenv("ERP_SECRET_KEY", "p1-153-isolated-migration-secret")
    monkeypatch.setenv("ERP_BACKUP_DIR", str((database.parent / "backups").resolve()))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{actual.as_posix()}")
    return config


def _revision(database: Path) -> str:
    with create_engine(f"sqlite:///{database.as_posix()}").connect() as connection:
        return str(connection.execute(text("SELECT version_num FROM alembic_version")).scalar())


def test_p1_153_empty_retirement_table_roundtrips(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "p1153-empty.sqlite3"
    config = _config(monkeypatch, database)
    command.upgrade(config, TARGET)
    assert _revision(database) == TARGET
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    with engine.connect() as connection:
        assert "warehouse_ground_layout_plan_retirements" in set(
            inspect(connection).get_table_names()
        )
        trigger_names = {
            row[0]
            for row in connection.execute(
                text("SELECT name FROM sqlite_master WHERE type='trigger'")
            )
        }
        assert "trg_ground_plan_retirements_immutable_update" in trigger_names
        assert "trg_ground_plan_retirements_immutable_delete" in trigger_names
        assert connection.execute(text("PRAGMA integrity_check")).scalar() == "ok"
        assert list(connection.execute(text("PRAGMA foreign_key_check"))) == []
    engine.dispose()

    command.downgrade(config, PARENT)
    assert _revision(database) == PARENT
    command.upgrade(config, TARGET)
    assert _revision(database) == TARGET


def test_p1_153_retirement_is_immutable_and_blocks_downgrade(
    tmp_path: Path, monkeypatch
) -> None:
    database = tmp_path / "p1153-fact.sqlite3"
    config = _config(monkeypatch, database)
    command.upgrade(config, TARGET)
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    with Session(engine) as db:
        user = User(
            username="p1153-migration-admin",
            password_hash="not-used",
            role="admin",
            real_name="P1-153 migration admin",
            must_change_password=False,
        )
        floor = db.query(WarehouseFloor).filter(WarehouseFloor.floor_number == 3).one()
        db.add(user)
        db.flush()
        area = WarehouseArea(
            floor_id=floor.id,
            area_code="P1153",
            area_name="P1-153 test area",
            construction_status="enabled",
            capacity_review_status="pending",
            capacity_eligible=False,
        )
        db.add(area)
        db.flush()
        plan = WarehouseGroundLayoutPlan(
            area_id=area.id,
            status="published",
            target_slot_count=1,
            numbering_origin="south",
            row_direction="from_aisle_inward",
            slot_direction="left_to_right",
            row_start_no=1,
            slot_start_no=1,
            draft_map_revision="p1153-map",
            published_map_revision="p1153-map",
            preview_fingerprint="a" * 64,
            version=1,
            publish_idempotency_key="p1153-plan",
            publish_request_hash="b" * 64,
            updated_by=user.id,
            published_by=user.id,
            published_at=datetime(2026, 9, 3, 15, 0),
        )
        db.add(plan)
        db.flush()
        retirement = WarehouseGroundLayoutPlanRetirement(
            plan_id=plan.id,
            area_id=area.id,
            operation_key="p1153-retire-plan",
            request_hash="c" * 64,
            reason="test",
            snapshot_json='{"plan_id":1}',
            retired_by=user.id,
        )
        db.add(retirement)
        db.commit()

        retirement.reason = "changed"
        with pytest.raises(IntegrityError, match="retirement is immutable"):
            db.commit()
        db.rollback()

    engine.dispose()
    with pytest.raises(RuntimeError, match="P1-153"):
        command.downgrade(config, PARENT)
    assert _revision(database) == TARGET
