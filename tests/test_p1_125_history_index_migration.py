from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config

from app.core.database import create_sqlite_engine
from app.models import Base


PREVIOUS_REVISION = "gs54v8x9z43"
TARGET_REVISION = "gt55v8x9z44"
EXPECTED_INDEXES = {
    "sales_orders": {
        "ix_sales_orders_created_at_id",
        "ix_sales_orders_updated_at_id",
    },
    "products": {
        "ix_products_product_code",
        "ix_products_customer_material_code",
    },
    "production_completions": {
        "ix_production_completions_completed_at_id",
        "ix_production_completions_status_completed_at_id",
        "ix_production_completions_order_item_id",
    },
}
PREEXISTING_INDEXES = {
    "sales_order_items": {"ix_sales_order_items_snapshot_product_code"},
}


def _config(monkeypatch, database_path: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database_path))
    config = Config("alembic.ini")
    config.set_main_option(
        "sqlalchemy.url",
        f"sqlite+pysqlite:///{database_path.as_posix()}",
    )
    return config


def _indexes(database_path: Path, table_name: str) -> set[str]:
    with sqlite3.connect(database_path) as connection:
        return {
            str(row[1])
            for row in connection.execute(f'PRAGMA index_list("{table_name}")')
        }


def _assert_integrity(database_path: Path) -> None:
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _query_plan(database_path: Path, sql: str, parameters: tuple) -> str:
    with sqlite3.connect(database_path) as connection:
        return "\n".join(
            str(row[3])
            for row in connection.execute(f"EXPLAIN QUERY PLAN {sql}", parameters)
        )


def test_history_capacity_indexes_round_trip_on_isolated_database(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "p1-125-history-indexes.sqlite3"
    config = _config(monkeypatch, database_path)

    engine = create_sqlite_engine(database_path)
    Base.metadata.create_all(engine)
    engine.dispose()
    with sqlite3.connect(database_path) as connection:
        for indexes in EXPECTED_INDEXES.values():
            for index_name in indexes:
                connection.execute(f'DROP INDEX IF EXISTS "{index_name}"')
    command.stamp(config, PREVIOUS_REVISION)
    before = {table: _indexes(database_path, table) for table in EXPECTED_INDEXES}
    preexisting_before = {
        table: _indexes(database_path, table) for table in PREEXISTING_INDEXES
    }

    command.upgrade(config, TARGET_REVISION)
    for table, expected in EXPECTED_INDEXES.items():
        assert expected <= _indexes(database_path, table)
    for table, expected in PREEXISTING_INDEXES.items():
        assert expected <= _indexes(database_path, table)
    _assert_integrity(database_path)
    assert "ix_sales_orders_created_at_id" in _query_plan(
        database_path,
        "SELECT id FROM sales_orders WHERE created_at >= ? "
        "ORDER BY created_at DESC, id DESC LIMIT 50",
        ("2026-08-01 00:00:00",),
    )
    assert "ix_sales_orders_updated_at_id" in _query_plan(
        database_path,
        "SELECT id FROM sales_orders WHERE updated_at >= ? LIMIT 50",
        ("2026-08-01 00:00:00",),
    )
    assert "ix_sales_order_items_snapshot_product_code" in _query_plan(
        database_path,
        "SELECT order_id FROM sales_order_items WHERE snapshot_product_code = ?",
        ("BX-001",),
    )
    assert "ix_products_product_code" in _query_plan(
        database_path,
        "SELECT id FROM products WHERE product_code = ?",
        ("BX-001",),
    )
    assert "ix_products_customer_material_code" in _query_plan(
        database_path,
        "SELECT id FROM products WHERE customer_material_code = ?",
        ("BX-001",),
    )
    assert "ix_production_completions_completed_at_id" in _query_plan(
        database_path,
        "SELECT id FROM production_completions "
        "WHERE completed_at < ? ORDER BY completed_at DESC, id DESC LIMIT 51",
        ("2026-08-01 00:00:00",),
    )
    assert "ix_production_completions_status_completed_at_id" in _query_plan(
        database_path,
        "SELECT id FROM production_completions "
        "WHERE status = ? AND completed_at < ? "
        "ORDER BY completed_at DESC, id DESC LIMIT 51",
        ("posted", "2026-08-01 00:00:00"),
    )
    assert "ix_production_completions_order_item_id" in _query_plan(
        database_path,
        "SELECT id FROM production_completions WHERE order_item_id = ?",
        (1,),
    )

    command.downgrade(config, PREVIOUS_REVISION)
    for table, expected in EXPECTED_INDEXES.items():
        assert _indexes(database_path, table) == before[table]
    for table, expected in PREEXISTING_INDEXES.items():
        assert expected <= _indexes(database_path, table)
        assert _indexes(database_path, table) == preexisting_before[table]
    _assert_integrity(database_path)

    command.upgrade(config, TARGET_REVISION)
    for table, expected in EXPECTED_INDEXES.items():
        assert expected <= _indexes(database_path, table)
    for table, expected in PREEXISTING_INDEXES.items():
        assert expected <= _indexes(database_path, table)
    _assert_integrity(database_path)
