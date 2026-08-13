from __future__ import annotations

from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import inspect

from app.core.database import create_sqlite_engine
from app.models import Base
from app.models.production import ProductionTask


ROOT = Path(__file__).resolve().parents[1]
PARENT_REVISION = "jj18v8x9z07"
TARGET_REVISION = "kk19v8x9z08"
COLUMN = "printing_colors_snapshot"


def _config(monkeypatch: pytest.MonkeyPatch, path: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(path))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    return config


def _checks(path: Path) -> tuple[str, int]:
    with sqlite3.connect(path) as connection:
        return (
            connection.execute("PRAGMA integrity_check").fetchone()[0],
            len(connection.execute("PRAGMA foreign_key_check").fetchall()),
        )


def _columns(path: Path) -> set[str]:
    engine = create_sqlite_engine(path)
    try:
        return {column["name"] for column in inspect(engine).get_columns("production_tasks")}
    finally:
        engine.dispose()


def _seed_task(path: Path) -> int:
    # Seed through the parent revision's real schema.  Current ORM models may
    # contain later, model-only compatibility fields that are intentionally not
    # present in a jj18 database.
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        customer_id = connection.execute(
            "INSERT INTO customers(name) VALUES (?) RETURNING id",
            ("P1-51C 迁移测试客户",),
        ).fetchone()[0]
        product_id = connection.execute(
            """INSERT INTO products(
            customer_id,product_code,customer_material_code,product_name
            ) VALUES (?,?,?,?) RETURNING id""",
            (customer_id, "P151C-MIGRATION", "P151C-MIGRATION", "P1-51C 迁移测试纸箱"),
        ).fetchone()[0]
        order_id = connection.execute(
            """INSERT INTO sales_orders(
            order_number,customer_id,order_date
            ) VALUES (?,?,?) RETURNING id""",
            ("P1-51C-MIGRATION-001", customer_id, "2026-08-13"),
        ).fetchone()[0]
        item_id = connection.execute(
            """INSERT INTO sales_order_items(
            order_id,product_id,quantity,unit_price,subtotal,snapshot_product_name
            ) VALUES (?,?,?,?,?,?) RETURNING id""",
            (order_id, product_id, 1, 1, 1, "P1-51C 迁移测试纸箱"),
        ).fetchone()[0]
        task_id = connection.execute(
            "INSERT INTO production_tasks(order_item_id) VALUES (?) RETURNING id",
            (item_id,),
        ).fetchone()[0]
        connection.commit()
        return int(task_id)


def test_p1_51c_model_registers_nullable_snapshot_without_default() -> None:
    column = Base.metadata.tables["production_tasks"].c[COLUMN]
    assert column.nullable is True
    assert column.default is None
    assert column.server_default is None

    task = ProductionTask(order_item_id=1)
    assert task.printing_colors_snapshot is None


def test_p1_51c_upgrade_preserves_old_tasks_as_unknown_and_round_trips(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "p1-51c-roundtrip.sqlite3"
    config = _config(monkeypatch, path)
    command.upgrade(config, PARENT_REVISION)
    task_id = _seed_task(path)

    command.upgrade(config, TARGET_REVISION)
    assert COLUMN in _columns(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            f"SELECT {COLUMN} FROM production_tasks WHERE id = ?", (task_id,)
        ).fetchone() == (None,)
    assert _checks(path) == ("ok", 0)

    command.downgrade(config, PARENT_REVISION)
    assert COLUMN not in _columns(path)
    assert _checks(path) == ("ok", 0)

    command.upgrade(config, TARGET_REVISION)
    assert COLUMN in _columns(path)
    assert _checks(path) == ("ok", 0)


def test_p1_51c_downgrade_fails_closed_after_snapshot_fact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "p1-51c-fail-closed.sqlite3"
    config = _config(monkeypatch, path)
    command.upgrade(config, PARENT_REVISION)
    task_id = _seed_task(path)
    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(path) as connection:
        connection.execute(
            f"UPDATE production_tasks SET {COLUMN} = ? WHERE id = ?",
            ('["专红","PANTONE 286 C"]', task_id),
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="拒绝破坏性降级"):
        command.downgrade(config, PARENT_REVISION)
    assert COLUMN in _columns(path)
    assert _checks(path) == ("ok", 0)
