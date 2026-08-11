from __future__ import annotations

from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
import pytest


ROOT = Path(__file__).resolve().parents[1]
PARENT_REVISION = "ec11v8x9z00"
TARGET_REVISION = "ed12v8x9z01"
SNAPSHOT_TRIGGERS = {
    "trg_sales_order_item_bom_components_immutable_update",
    "trg_sales_order_item_bom_components_source_unlink_only",
}


def _config(monkeypatch: pytest.MonkeyPatch, path: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(path))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def _table_sql(connection: sqlite3.Connection, table: str) -> str:
    return str(
        connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()[0]
    )


def _snapshot_triggers(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='trigger' AND tbl_name='sales_order_item_bom_components'"
        ).fetchall()
    }


def test_positive_integer_cutting_mode_migration_round_trip_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "cutting-mode-positive-integer.sqlite3"
    config = _config(monkeypatch, path)
    command.upgrade(config, TARGET_REVISION)

    with sqlite3.connect(path) as connection:
        assert "substr(default_cutting_mode, 1, 2) = '一开'" in _table_sql(
            connection, "products"
        )
        assert "substr(snapshot_component_default_cutting_mode, 1, 2) = '一开'" in _table_sql(
            connection, "sales_order_item_bom_components"
        )
        assert SNAPSHOT_TRIGGERS.issubset(_snapshot_triggers(connection))

    command.downgrade(config, PARENT_REVISION)
    command.upgrade(config, TARGET_REVISION)

    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO customers (id,name) VALUES (1,'P1-41迁移测试客户')"
        )
        connection.execute(
            "INSERT INTO products "
            "(id,customer_id,product_code,customer_material_code,product_name,"
            "box_category,unit,default_cutting_mode) "
            "VALUES (900001,1,'P1-41-12','P1-41-12','一开十二迁移测试','normal','只','一开12')"
        )
        connection.commit()
        for invalid in ("一开0", "一开01", "一开十二"):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE products SET default_cutting_mode=? WHERE id=900001",
                    (invalid,),
                )
            connection.rollback()
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert SNAPSHOT_TRIGGERS.issubset(_snapshot_triggers(connection))

    with pytest.raises(RuntimeError, match="禁止破坏性降级"):
        command.downgrade(config, PARENT_REVISION)

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == TARGET_REVISION
        assert connection.execute(
            "SELECT default_cutting_mode FROM products WHERE id=900001"
        ).fetchone()[0] == "一开12"
