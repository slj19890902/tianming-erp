from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "migration" / "rehearse_history_display_and_status.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "rehearse_history_display_and_status", SCRIPT
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _seed_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE sales_orders (
              id INTEGER PRIMARY KEY,
              order_number TEXT NOT NULL UNIQUE,
              customer_id INTEGER NOT NULL,
              customer_po TEXT,
              order_date TEXT NOT NULL,
              delivery_date TEXT,
              status TEXT NOT NULL,
              payment_status TEXT NOT NULL,
              total_amount NUMERIC NOT NULL,
              remark TEXT,
              created_by INTEGER,
              created_at TEXT DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT
            );
            CREATE TABLE sales_order_items (
              id INTEGER PRIMARY KEY,
              order_id INTEGER NOT NULL,
              product_id INTEGER NOT NULL,
              quantity INTEGER NOT NULL,
              delivered_quantity INTEGER NOT NULL DEFAULT 0,
              is_force_closed INTEGER NOT NULL DEFAULT 0,
              unit_price NUMERIC NOT NULL,
              subtotal NUMERIC NOT NULL,
              material_status TEXT NOT NULL,
              snapshot_product_name TEXT NOT NULL,
              FOREIGN KEY(order_id) REFERENCES sales_orders(id)
            );
            CREATE TABLE migration_ruida_sales_order_map (
              legacy_order_id INTEGER PRIMARY KEY,
              sales_order_id INTEGER NOT NULL UNIQUE
            );
            CREATE TABLE migration_ruida_sales_item_map (
              legacy_item_id INTEGER PRIMARY KEY,
              sales_order_item_id INTEGER NOT NULL UNIQUE,
              legacy_order_id INTEGER NOT NULL
            );
            INSERT INTO sales_orders
              (id, order_number, customer_id, order_date, status, payment_status, total_amount, remark, created_at)
            VALUES
              (1, 'RUIDA-50001', 1, '2026-06-01', 'pending_delivery', 'unpaid', 100, '[瑞达历史订单 50001] A', '2026-06-01 08:00:00'),
              (2, 'RUIDA-50002', 1, '2026-06-01', 'partially_delivered', 'unpaid', 200, '[RUIDA 50002] B', '2026-06-01 09:00:00'),
              (3, 'RUIDA-50003', 2, '2026-06-02', 'delivered', 'paid', 300, 'normal', '2026-06-02 09:00:00'),
              (4, 'PO-20260620-001', 2, '2026-06-20', 'pending_production', 'unpaid', 88, 'today', '2026-06-20 09:00:00');
            INSERT INTO sales_order_items
              (id, order_id, product_id, quantity, unit_price, subtotal, material_status, snapshot_product_name)
            VALUES
              (11, 1, 101, 10, 10, 100, 'received', '箱1'),
              (12, 2, 102, 20, 10, 200, 'received', '箱2'),
              (13, 3, 103, 30, 10, 300, 'received', '箱3');
            INSERT INTO migration_ruida_sales_order_map VALUES (50001, 1), (50002, 2), (50003, 3);
            INSERT INTO migration_ruida_sales_item_map VALUES (90001, 11, 50001), (90002, 12, 50002), (90003, 13, 50003);
            """
        )
        conn.commit()


def test_rehearsal_converts_history_numbers_to_tm_and_preserves_source(tmp_path: Path) -> None:
    module = _load_module()
    source = tmp_path / "source.sqlite3"
    sandbox = tmp_path / "sandbox.sqlite3"
    report_dir = tmp_path / "reports"
    _seed_db(source)

    result = module.rehearse_history_display_and_status(
        sqlite_path=source,
        sandbox_path=sandbox,
        report_dir=report_dir,
    )

    assert result["tm_orders"] == 3
    assert result["remaining_legacy_orders"] == 0
    assert result["duplicate_order_numbers"] == 0
    assert result["integrity_check"] == "ok"
    assert result["foreign_key_check"] == 0
    assert sandbox.exists()
    assert result["mapping_csv"].endswith(".csv")
    assert result["report_path"].endswith(".md")

    with sqlite3.connect(source) as conn:
        source_orders = [
            row[0]
            for row in conn.execute(
                "SELECT order_number FROM sales_orders ORDER BY id"
            ).fetchall()
        ]
    with sqlite3.connect(sandbox) as conn:
        sandbox_orders = [
            row[0]
            for row in conn.execute(
                "SELECT order_number FROM sales_orders ORDER BY id"
            ).fetchall()
        ]
        sandbox_remarks = [
            row[0]
            for row in conn.execute("SELECT remark FROM sales_orders WHERE id IN (1,2)")
        ]

    assert source_orders[:3] == ["RUIDA-50001", "RUIDA-50002", "RUIDA-50003"]
    assert sandbox_orders[:3] == [
        "TM20260601-0001",
        "TM20260601-0002",
        "TM20260602-0001",
    ]
    assert sandbox_orders[3] == "PO-20260620-001"
    assert all("RUIDA" not in (remark or "") and "瑞达" not in (remark or "") for remark in sandbox_remarks)
