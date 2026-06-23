from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ADDITIVE_REVISION = "f26b7d4a9c10"
REMOVAL_REVISION = "g37c8e5b0d21"
ADDITIVE_PATH = (
    PROJECT_ROOT
    / "alembic"
    / "versions"
    / "f26b7d4a9c10_product_trash_and_drawing_versions.py"
)
REMOVAL_PATH = (
    PROJECT_ROOT
    / "alembic"
    / "versions"
    / "g37c8e5b0d21_remove_legacy_product_drawing_path.py"
)


def _create_pre_phase14_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE alembic_version (
                version_num VARCHAR(32) NOT NULL PRIMARY KEY
            );
            INSERT INTO alembic_version VALUES ('e13a6c4d2f40');

            CREATE TABLE users (
                id INTEGER PRIMARY KEY
            );
            INSERT INTO users VALUES (1);

            CREATE TABLE products (
                id INTEGER PRIMARY KEY,
                customer_id INTEGER NOT NULL,
                product_code VARCHAR(150) NOT NULL,
                drawing_path TEXT,
                created_at DATETIME,
                updated_at DATETIME,
                is_active BOOLEAN NOT NULL DEFAULT 1
            );
            INSERT INTO products VALUES (
                1, 3, '001A',
                '/static/uploads/drawings/product_1_legacy.webp',
                '2026-06-01 08:30:00', NULL, 1
            );
            INSERT INTO products VALUES (
                2, 3, '001B', NULL,
                '2026-06-02 09:00:00', NULL, 1
            );

            CREATE TABLE sales_order_items (
                id INTEGER PRIMARY KEY,
                product_id INTEGER NOT NULL REFERENCES products(id)
            );
            INSERT INTO sales_order_items VALUES (1, 1);
            """
        )
        connection.commit()


def _upgrade(
    monkeypatch: pytest.MonkeyPatch,
    database_path: Path,
    revision: str,
) -> None:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(database_path.parent / "backups"))
    monkeypatch.setenv("ERP_SECRET_KEY", "phase14-migration-test")
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    command.upgrade(config, revision)


def _scalar(database_path: Path, sql: str):
    with sqlite3.connect(database_path) as connection:
        return connection.execute(sql).fetchone()[0]


def test_additive_migration_source_contains_no_destructive_product_operations() -> None:
    source = ADDITIVE_PATH.read_text(encoding="utf-8").lower()

    assert "drop_table" not in source
    assert 'drop_column("products", "drawing_path")' not in source
    assert "delete from sales_order_items" not in source
    assert "create_table" in source
    assert "add_column" in source


def test_additive_migration_preserves_products_orders_and_legacy_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "phase14_additive.sqlite3"
    _create_pre_phase14_database(database_path)

    _upgrade(monkeypatch, database_path, ADDITIVE_REVISION)

    assert _scalar(database_path, "SELECT COUNT(*) FROM products") == 2
    assert _scalar(database_path, "SELECT COUNT(*) FROM sales_order_items") == 1
    assert _scalar(database_path, "SELECT COUNT(*) FROM product_drawings") == 1
    assert _scalar(
        database_path,
        "SELECT drawing_path FROM products WHERE id = 1",
    ) == "/static/uploads/drawings/product_1_legacy.webp"
    assert _scalar(database_path, "PRAGMA integrity_check") == "ok"


def test_removal_migration_drops_only_legacy_path_after_copy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "phase14_removal.sqlite3"
    _create_pre_phase14_database(database_path)

    _upgrade(monkeypatch, database_path, REMOVAL_REVISION)

    with sqlite3.connect(database_path) as connection:
        product_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(products)")
        }
    assert "drawing_path" not in product_columns
    assert _scalar(database_path, "SELECT COUNT(*) FROM products") == 2
    assert _scalar(database_path, "SELECT COUNT(*) FROM sales_order_items") == 1
    assert _scalar(database_path, "SELECT COUNT(*) FROM product_drawings") == 1
    assert _scalar(database_path, "PRAGMA integrity_check") == "ok"


def test_removal_revision_is_scoped_to_products_drawing_path() -> None:
    source = REMOVAL_PATH.read_text(encoding="utf-8").lower()

    assert 'drop_column("drawing_path")' in source
    assert "drop_table" not in source
    assert "sales_order_items" not in source
