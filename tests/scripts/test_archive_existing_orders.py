from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "admin" / "archive_existing_orders.py"
spec = importlib.util.spec_from_file_location("archive_existing_orders", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(module)


def _make_source_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE sales_orders (
                id INTEGER PRIMARY KEY,
                order_number TEXT NOT NULL,
                status TEXT NOT NULL,
                payment_status TEXT NOT NULL
            );
            CREATE TABLE sales_order_items (
                id INTEGER PRIMARY KEY,
                order_id INTEGER NOT NULL,
                requisition_status TEXT NOT NULL,
                material_status TEXT NOT NULL,
                FOREIGN KEY(order_id) REFERENCES sales_orders(id) ON DELETE CASCADE
            );
            INSERT INTO sales_orders VALUES
                (1, 'RUIDA-10001', 'delivered', 'unpaid'),
                (2, 'PO-001', 'pending_production', 'unpaid'),
                (3, 'TM20260622001', 'pending_production', 'unpaid');
            INSERT INTO sales_order_items VALUES
                (11, 1, '未报料', 'pending'),
                (12, 2, '已报料', 'pending'),
                (13, 3, '未报料', 'pending');
            """
        )
        connection.commit()


def test_parser_requires_sqlite_path() -> None:
    parser = module.build_parser()
    args = parser.parse_args(["--sqlite-path", "data/carton_erp.sqlite3"])
    assert args.sqlite_path == "data/carton_erp.sqlite3"
    assert args.apply is False


def test_archive_existing_orders_dry_run_uses_current_max_id(tmp_path: Path) -> None:
    source = tmp_path / "carton_erp.sqlite3"
    _make_source_db(source)

    result = module.archive_existing_orders(
        sqlite_path=source,
        backup_path=tmp_path / "backups" / "archive.sqlite3",
        cutoff_order_id=None,
        apply=False,
    )

    assert result["cutoff_order_id"] == 3
    assert result["plan"]["orders_to_mark_paid"] == 3
    assert result["plan"]["items_to_archive"] == 3
    assert result["applied"] is False


def test_archive_existing_orders_apply_only_updates_cutoff_scope(tmp_path: Path) -> None:
    source = tmp_path / "carton_erp.sqlite3"
    backup = tmp_path / "backups" / "archive.sqlite3"
    _make_source_db(source)

    result = module.archive_existing_orders(
        sqlite_path=source,
        backup_path=backup,
        cutoff_order_id=2,
        apply=True,
    )

    assert result["backup"]["hash_match"] is True
    assert result["postcheck"]["integrity_check"] == "ok"
    assert result["applied"] is True

    with sqlite3.connect(source) as connection:
        orders = connection.execute(
            "SELECT id, payment_status FROM sales_orders ORDER BY id"
        ).fetchall()
        items = connection.execute(
            "SELECT id, requisition_status FROM sales_order_items ORDER BY id"
        ).fetchall()

    assert orders == [(1, "paid"), (2, "paid"), (3, "unpaid")]
    assert items == [(11, "已结算"), (12, "已结算"), (13, "未报料")]
