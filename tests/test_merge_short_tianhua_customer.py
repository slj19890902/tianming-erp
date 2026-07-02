from __future__ import annotations

import sqlite3
from pathlib import Path

from scripts.admin.merge_short_tianhua_customer import (
    SOURCE_NAME,
    TARGET_NAME,
    run_merge,
)


OTHER_TIANHUA = "苏州天华新能源科技股份有限公司"


def _database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(
            """
            CREATE TABLE customers (
                id INTEGER PRIMARY KEY,
                customer_number INTEGER,
                customer_code TEXT,
                name TEXT NOT NULL UNIQUE
            );
            CREATE TABLE products (
                id INTEGER PRIMARY KEY,
                customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE RESTRICT,
                product_code TEXT NOT NULL
            );
            CREATE TABLE sales_orders (
                id INTEGER PRIMARY KEY,
                customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE RESTRICT,
                order_number TEXT NOT NULL
            );
            CREATE TABLE company_file_index (
                id INTEGER PRIMARY KEY,
                customer_id INTEGER REFERENCES customers(id),
                inferred_customer_name TEXT
            );
            """
        )
        connection.executemany(
            "INSERT INTO customers(id, customer_number, customer_code, name) "
            "VALUES (?, ?, ?, ?)",
            [
                (1, 1, "SHORT", SOURCE_NAME),
                (2, 2, "TH", TARGET_NAME),
                (3, 3, "THXN", OTHER_TIANHUA),
            ],
        )
        connection.execute(
            "INSERT INTO products(id, customer_id, product_code) VALUES (1, 1, 'P1')"
        )
        connection.execute(
            "INSERT INTO sales_orders(id, customer_id, order_number) "
            "VALUES (1, 1, 'SO1')"
        )
        connection.execute(
            "INSERT INTO company_file_index"
            "(id, customer_id, inferred_customer_name) VALUES (1, 1, ?)",
            (SOURCE_NAME,),
        )
        connection.commit()


def test_dry_run_reports_impact_without_changing_data(tmp_path: Path) -> None:
    database = tmp_path / "merge-dry-run.sqlite3"
    _database(database)

    report = run_merge(database, apply=False)

    assert report["source_found"] is True
    assert report["foreign_key_updates"] == {
        "company_file_index.customer_id": 1,
        "products.customer_id": 1,
        "sales_orders.customer_id": 1,
    }
    assert report["customer_name_updates"] == {
        "company_file_index.inferred_customer_name": 1
    }
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM customers WHERE name = ?", (SOURCE_NAME,)
        ).fetchone()[0] == 1


def test_apply_merges_short_customer_and_preserves_formal_customers(
    tmp_path: Path,
) -> None:
    database = tmp_path / "merge-apply.sqlite3"
    _database(database)

    report = run_merge(database, apply=True, create_backup=False)

    assert report["changed"] is True
    assert report["integrity_check"] == "ok"
    with sqlite3.connect(database) as connection:
        names = {
            row[0] for row in connection.execute("SELECT name FROM customers")
        }
        assert SOURCE_NAME not in names
        assert TARGET_NAME in names
        assert OTHER_TIANHUA in names
        assert connection.execute(
            "SELECT customer_id FROM products WHERE id = 1"
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT customer_id FROM sales_orders WHERE id = 1"
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT customer_id, inferred_customer_name "
            "FROM company_file_index WHERE id = 1"
        ).fetchone() == (2, TARGET_NAME)
