from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "admin"
    / "apply_order_number_structure.py"
)
spec = importlib.util.spec_from_file_location("apply_order_number_structure", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def _make_source_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(
        """
        CREATE TABLE customers (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL
        );
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL
        );
        CREATE TABLE products (
            id INTEGER PRIMARY KEY,
            customer_id INTEGER NOT NULL,
            product_code TEXT,
            product_name TEXT,
            FOREIGN KEY(customer_id) REFERENCES customers(id)
        );
        CREATE TABLE sales_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_number TEXT NOT NULL UNIQUE,
            customer_id INTEGER NOT NULL,
            customer_po TEXT,
            order_date TEXT NOT NULL,
            delivery_date TEXT,
            status TEXT NOT NULL,
            payment_status TEXT NOT NULL,
            total_amount NUMERIC NOT NULL DEFAULT 0,
            remark TEXT,
            created_by INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT,
            FOREIGN KEY(customer_id) REFERENCES customers(id),
            FOREIGN KEY(created_by) REFERENCES users(id)
        );
        CREATE TABLE sales_order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            quantity INTEGER NOT NULL,
            delivered_quantity INTEGER NOT NULL DEFAULT 0,
            is_force_closed INTEGER NOT NULL DEFAULT 0,
            unit_price NUMERIC NOT NULL,
            subtotal NUMERIC NOT NULL,
            material_status TEXT NOT NULL DEFAULT 'pending',
            snapshot_product_name TEXT NOT NULL,
            snapshot_product_code TEXT,
            snapshot_spec TEXT,
            snapshot_material TEXT,
            inventory_deducted_qty INTEGER NOT NULL DEFAULT 0,
            requisition_status TEXT NOT NULL DEFAULT '未报料',
            special_process TEXT NOT NULL DEFAULT '无',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(order_id) REFERENCES sales_orders(id) ON DELETE CASCADE,
            FOREIGN KEY(product_id) REFERENCES products(id)
        );
        CREATE TABLE order_daily_sequences (
            sequence_date TEXT PRIMARY KEY,
            last_value INTEGER NOT NULL
        );
        CREATE INDEX ix_sales_orders_customer_id ON sales_orders (customer_id);
        CREATE INDEX ix_sales_orders_order_date ON sales_orders (order_date);
        CREATE INDEX ix_sales_order_items_order_id ON sales_order_items (order_id);
        """
    )
    connection.execute("INSERT INTO customers (id, name) VALUES (1, '测试客户')")
    connection.execute("INSERT INTO users (id, username) VALUES (1, 'admin')")
    connection.execute(
        "INSERT INTO products (id, customer_id, product_code, product_name) VALUES (1, 1, 'P-001', '测试纸箱')"
    )
    connection.execute(
        """
        INSERT INTO sales_orders (
            order_number, customer_id, customer_po, order_date, delivery_date,
            status, payment_status, total_amount, remark, created_by
        ) VALUES ('TM20260621001', 1, 'PO-001', '2026-06-21', '2026-06-25', 'pending_production', 'unpaid', 100, 'seed', 1)
        """
    )
    connection.execute(
        """
        INSERT INTO sales_order_items (
            order_id, product_id, quantity, unit_price, subtotal, material_status,
            snapshot_product_name, snapshot_product_code, snapshot_spec, snapshot_material
        ) VALUES (1, 1, 10, 10, 100, 'pending', '测试纸箱', 'P-001', '100x100x100', 'A=A')
        """
    )
    connection.commit()
    connection.close()


def test_script_parser_requires_explicit_paths_and_apply_flag() -> None:
    parser = module.build_parser()
    args = parser.parse_args(["--sqlite-path", "data/carton_erp.sqlite3"])
    assert args.sqlite_path == "data/carton_erp.sqlite3"
    assert args.apply is False


def test_backup_and_apply_migration_is_idempotent(tmp_path: Path) -> None:
    source = tmp_path / "carton_erp.sqlite3"
    backup = tmp_path / "backups" / "carton_erp_before_order_number_structure_apply_20260621_090000.sqlite3"
    _make_source_db(source)

    first = module.apply_order_number_structure(sqlite_path=source, backup_path=backup)
    second = module.apply_order_number_structure(sqlite_path=source, backup_path=backup)

    assert first["backup"]["path"].endswith(backup.name)
    assert first["backup"]["source_sha256"] == first["backup"]["backup_sha256"]
    assert first["precheck"]["integrity_check"] == "ok"
    assert first["postcheck"]["integrity_check"] == "ok"
    assert "sales_order_items.item_order_number" in first["migration"]["added_columns"]
    assert "sales_order_items.item_sequence" in first["migration"]["added_columns"]
    assert "order_item_number_sequences" in first["migration"]["added_tables"]
    assert "ux_sales_order_items_item_order_number" in first["migration"]["added_indexes"]
    assert "ix_sales_orders_customer_po_group" in first["migration"]["added_indexes"]

    assert second["migration"]["added_columns"] == []
    assert second["migration"]["added_tables"] == []
    assert second["migration"]["added_indexes"] == []

    connection = sqlite3.connect(source)
    columns = {
        row[1]
        for row in connection.execute("PRAGMA table_info(sales_order_items)").fetchall()
    }
    order_indexes = {
        row[1]
        for row in connection.execute("PRAGMA index_list(sales_orders)").fetchall()
    }
    item_indexes = {
        row[1]
        for row in connection.execute("PRAGMA index_list(sales_order_items)").fetchall()
    }
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    connection.close()

    assert {"item_order_number", "item_sequence"} <= columns
    assert "order_item_number_sequences" in tables
    assert "ix_sales_orders_customer_po" in order_indexes
    assert "ix_sales_orders_customer_po_group" in order_indexes
    assert "ux_sales_order_items_item_order_number" in item_indexes
