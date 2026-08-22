from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from app.core.database import create_sqlite_engine
from app.models import Base


ROOT = Path(__file__).resolve().parents[1]
PARENT_REVISION = "bbb36v8x9z25"
TARGET_REVISION = "ccc37v8x9z26"


def _config(monkeypatch: pytest.MonkeyPatch, path: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(path))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    return config


def _columns(path: Path, table: str) -> set[str]:
    engine = create_sqlite_engine(path)
    try:
        return {column["name"] for column in inspect(engine).get_columns(table)}
    finally:
        engine.dispose()


def _checks(path: Path) -> tuple[str, int]:
    with sqlite3.connect(path) as connection:
        return (
            connection.execute("PRAGMA integrity_check").fetchone()[0],
            len(connection.execute("PRAGMA foreign_key_check").fetchall()),
        )


def test_models_register_nullable_product_and_component_production_notes() -> None:
    product_column = Base.metadata.tables["products"].c["production_notes"]
    component_column = Base.metadata.tables["sales_order_item_bom_components"].c[
        "snapshot_component_production_notes"
    ]
    assert product_column.nullable is True
    assert component_column.nullable is True
    assert product_column.server_default is None
    assert component_column.server_default is None


def test_production_note_migration_round_trips_empty_historical_rows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "p1-88-roundtrip.sqlite3"
    config = _config(monkeypatch, path)
    command.upgrade(config, PARENT_REVISION)
    command.upgrade(config, TARGET_REVISION)
    assert "production_notes" in _columns(path, "products")
    assert "snapshot_component_production_notes" in _columns(
        path, "sales_order_item_bom_components"
    )
    assert _checks(path) == ("ok", 0)

    command.downgrade(config, PARENT_REVISION)
    assert "production_notes" not in _columns(path, "products")
    assert "snapshot_component_production_notes" not in _columns(
        path, "sales_order_item_bom_components"
    )
    assert _checks(path) == ("ok", 0)
    command.upgrade(config, TARGET_REVISION)


def test_production_note_migration_downgrade_fails_closed_after_fact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "p1-88-fail-closed.sqlite3"
    config = _config(monkeypatch, path)
    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(path) as connection:
        customer_id = connection.execute(
            "INSERT INTO customers(name) VALUES (?) RETURNING id",
            ("P1-88 迁移测试客户",),
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO products(
                customer_id, product_code, customer_material_code, product_name,
                production_notes
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                customer_id,
                "P1-88-NOTE",
                "P1-88-NOTE",
                "生产备注迁移测试",
                "模切边缘重点检查",
            ),
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="拒绝破坏性降级"):
        command.downgrade(config, PARENT_REVISION)
    assert "production_notes" in _columns(path, "products")
    assert _checks(path) == ("ok", 0)
