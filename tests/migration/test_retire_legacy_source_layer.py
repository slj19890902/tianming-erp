from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine


ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = (
    ROOT / "alembic" / "versions" / "gn49v8x9z38_retire_legacy_source_layer.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("retire_legacy_source_layer", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Operations:
    def __init__(self, connection):
        self.connection = connection

    def get_bind(self):
        return self.connection

    def drop_table(self, table_name: str) -> None:
        self.connection.exec_driver_sql(f'DROP TABLE "{table_name}"')


def _table_names(path: Path) -> set[str]:
    with sqlite3.connect(path) as connection:
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }


def test_upgrade_drops_only_retired_source_tables(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "retire.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE sales_orders (
                id INTEGER PRIMARY KEY,
                order_number TEXT NOT NULL,
                remark TEXT
            );
            CREATE TABLE protected_business_fact (
                id INTEGER PRIMARY KEY,
                order_id INTEGER NOT NULL,
                quantity INTEGER NOT NULL
            );
            CREATE TABLE legacy_ruida_customers (id INTEGER PRIMARY KEY);
            CREATE TABLE legacy_ruida_orders (id INTEGER PRIMARY KEY);
            CREATE TABLE legacy_ruida_order_items (id INTEGER PRIMARY KEY);
            CREATE TABLE migration_ruida_sales_order_map (legacy_order_id INTEGER PRIMARY KEY);
            CREATE TABLE migration_ruida_sales_item_map (legacy_item_id INTEGER PRIMARY KEY);
            INSERT INTO sales_orders VALUES (9544, 'TM20260627003', NULL);
            INSERT INTO sales_orders VALUES (9545, 'TM20260629003', NULL);
            INSERT INTO protected_business_fact VALUES (1, 9544, 100);
            INSERT INTO protected_business_fact VALUES (2, 9545, 200);
            INSERT INTO legacy_ruida_orders VALUES (1);
            """
        )

    migration = _load_migration()
    engine = create_engine(f"sqlite:///{database}")
    try:
        with engine.begin() as connection:
            monkeypatch.setattr(migration, "op", _Operations(connection))
            migration.upgrade()
    finally:
        engine.dispose()

    names = _table_names(database)
    assert "sales_orders" in names
    assert "protected_business_fact" in names
    assert not set(migration._SOURCE_TABLES_IN_DROP_ORDER) & names
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT id, order_number FROM sales_orders ORDER BY id"
        ).fetchall() == [
            (9544, "TM20260627003"),
            (9545, "TM20260629003"),
        ]
        assert connection.execute(
            "SELECT order_id, quantity FROM protected_business_fact ORDER BY id"
        ).fetchall() == [(9544, 100), (9545, 200)]


def test_upgrade_refuses_named_formal_business_order(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "guard.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE sales_orders (
                id INTEGER PRIMARY KEY,
                order_number TEXT NOT NULL,
                remark TEXT
            );
            CREATE TABLE legacy_ruida_orders (id INTEGER PRIMARY KEY);
            INSERT INTO sales_orders VALUES (1, 'RUIDA-UNREVIEWED', NULL);
            INSERT INTO legacy_ruida_orders VALUES (1);
            """
        )

    migration = _load_migration()
    engine = create_engine(f"sqlite:///{database}")
    try:
        with engine.begin() as connection:
            monkeypatch.setattr(migration, "op", _Operations(connection))
            with pytest.raises(RuntimeError, match="formal sales orders"):
                migration.upgrade()
    finally:
        engine.dispose()

    assert "legacy_ruida_orders" in _table_names(database)


def test_downgrade_is_fail_closed() -> None:
    migration = _load_migration()
    with pytest.raises(RuntimeError, match="verified pre-upgrade database backup"):
        migration.downgrade()
