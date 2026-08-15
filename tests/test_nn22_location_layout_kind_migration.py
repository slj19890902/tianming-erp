from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest


ROOT = Path(__file__).resolve().parents[1]
PARENT_REVISION = "mm21v8x9z10"
TARGET_REVISION = "nn22v8x9z11"


def _config(monkeypatch: pytest.MonkeyPatch, path: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(path))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def _columns(
    connection: sqlite3.Connection,
    table: str = "floor3_location_layouts",
) -> set[str]:
    return {
        row[1]
        for row in connection.execute(
            f"PRAGMA table_info('{table}')"
        )
    }


def test_nn22_layout_kind_roundtrip_defaults_old_rows_and_guards_used_semantics(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "nn22-location-layout-kind.sqlite3"
    config = _config(monkeypatch, path)
    command.upgrade(config, PARENT_REVISION)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO warehouse_locations("
            "location_code,location_name,warehouse_type,is_active,warehouse_floor,"
            "area_code,storage_type,source_version,placement_status"
            ") VALUES ('3F-NN22-L001','NN22 old layout','finished',1,3,'NN22',"
            "'ground','TWIN_V1','placed')"
        )
        location_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        connection.execute(
            "INSERT INTO floor3_location_layouts("
            "location_id,left_pct,top_pct,width_pct,height_pct,z_index,version,source_type"
            ") VALUES (?,10,10,12,10,0,1,'manual')",
            (location_id,),
        )
        connection.commit()

    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(path) as connection:
        assert "layout_kind" in _columns(connection)
        assert "location_layout_version" in _columns(
            connection, "stocktake_orders"
        )
        assert "observed_location_layout_version" in _columns(
            connection, "warehouse_location_discrepancies"
        )
        assert connection.execute(
            "SELECT layout_kind FROM floor3_location_layouts WHERE location_id=?",
            (location_id,),
        ).fetchone()[0] == "unknown"
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        connection.execute(
            "INSERT INTO inventory_lots("
            "lot_number,inventory_type,warehouse_location_id,"
            "quantity_available,quantity_reserved,quantity_consumed,"
            "quantity_damaged,quantity_scrapped,unit,status,source_type,"
            "stock_date,stock_date_accuracy,last_movement_at,version"
            ") VALUES ('NN22-ZERO','finished',?,0,0,1,0,0,'boxes',"
            "'active','manual','2026-08-14','exact','2026-08-14 12:00:00',1)",
            (location_id,),
        )
        connection.execute(
            "UPDATE warehouse_locations SET is_active=0 WHERE id=?",
            (location_id,),
        )
        with pytest.raises(sqlite3.IntegrityError, match="active placed location"):
            connection.execute(
                "UPDATE inventory_lots SET quantity_available=1 "
                "WHERE lot_number='NN22-ZERO'"
            )
        connection.execute(
            "UPDATE warehouse_locations SET is_active=1 WHERE id=?",
            (location_id,),
        )
        connection.execute(
            "UPDATE inventory_lots SET quantity_available=1 "
            "WHERE lot_number='NN22-ZERO'"
        )
        with pytest.raises(sqlite3.IntegrityError, match="referenced warehouse location"):
            connection.execute(
                "UPDATE warehouse_locations SET is_active=0 WHERE id=?",
                (location_id,),
            )
        connection.execute(
            "UPDATE inventory_lots SET quantity_available=0 "
            "WHERE lot_number='NN22-ZERO'"
        )
        connection.commit()

    command.downgrade(config, PARENT_REVISION)
    with sqlite3.connect(path) as connection:
        assert "layout_kind" not in _columns(connection)
        assert "location_layout_version" not in _columns(
            connection, "stocktake_orders"
        )
        assert "observed_location_layout_version" not in _columns(
            connection, "warehouse_location_discrepancies"
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM floor3_location_layouts WHERE location_id=?",
            (location_id,),
        ).fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError, match="referenced warehouse location"):
            connection.execute(
                "UPDATE warehouse_locations SET is_active=0 WHERE id=?",
                (location_id,),
            )

    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE floor3_location_layouts SET layout_kind='logical_anchor' "
            "WHERE location_id=?",
            (location_id,),
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="拒绝破坏性降级"):
        command.downgrade(config, PARENT_REVISION)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0] == TARGET_REVISION
        assert connection.execute(
            "SELECT layout_kind FROM floor3_location_layouts WHERE location_id=?",
            (location_id,),
        ).fetchone()[0] == "logical_anchor"
