from __future__ import annotations

from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import inspect
from app.core.database import create_sqlite_engine


ROOT = Path(__file__).resolve().parents[1]
PARENT_REVISION = "ci65v8x9z54"
TARGET_REVISION = "co71v8x9z60"


def _config(monkeypatch: pytest.MonkeyPatch, path: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(path))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def _checks(path: Path) -> tuple[str, int]:
    with sqlite3.connect(path) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = len(connection.execute("PRAGMA foreign_key_check").fetchall())
    return integrity, foreign_keys


def test_cutting_snapshot_migration_is_linear_and_round_trips_empty_database(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "bom-cutting-roundtrip.sqlite3"
    config = _config(monkeypatch, path)
    command.upgrade(config, PARENT_REVISION)
    command.upgrade(config, TARGET_REVISION)

    engine = create_sqlite_engine(path)
    inspector = inspect(engine)
    assert {
        "snapshot_component_default_cutting_mode"
    } <= {column["name"] for column in inspector.get_columns(
        "sales_order_item_bom_components"
    )}
    assert {"demand_basis"} <= {
        column["name"]
        for column in inspector.get_columns("requisition_item_bom_sources")
    }
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0] == TARGET_REVISION
    assert _checks(path) == ("ok", 0)

    command.downgrade(config, PARENT_REVISION)
    inspector = inspect(create_sqlite_engine(path))
    assert "snapshot_component_default_cutting_mode" not in {
        column["name"]
        for column in inspector.get_columns("sales_order_item_bom_components")
    }
    assert "demand_basis" not in {
        column["name"]
        for column in inspector.get_columns("requisition_item_bom_sources")
    }
    assert _checks(path) == ("ok", 0)


def test_cutting_snapshot_migration_downgrade_fails_closed_after_new_order_fact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "bom-cutting-fail-closed.sqlite3"
    config = _config(monkeypatch, path)
    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            """
            INSERT INTO sales_order_item_bom_components (
                sales_order_item_id,
                component_product_id,
                snapshot_schema_version,
                order_set_quantity,
                quantity_per_set,
                required_piece_quantity,
                display_order,
                internal_component_code,
                is_die_cut,
                spare_sheet_quantity,
                display_mode,
                is_required,
                snapshot_component_product_code,
                snapshot_component_product_name,
                snapshot_component_box_category,
                snapshot_component_box_style,
                snapshot_component_default_cutting_mode
            ) VALUES (
                1, 1, 3, 3000, 1, 3000, 1, 'UAT-PARENT-S01',
                0, 0, 'internal_only', 1, 'UAT-KNIFE', '匿名刀卡',
                'normal', '刀卡', '一开二'
            )
            """
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="禁止破坏性降级"):
        command.downgrade(config, PARENT_REVISION)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0] == TARGET_REVISION
        connection.execute("DELETE FROM sales_order_item_bom_components")
        connection.commit()
    assert _checks(path) == ("ok", 0)
