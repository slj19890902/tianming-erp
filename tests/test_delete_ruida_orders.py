import hashlib
import sqlite3
from pathlib import Path


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _database(path: Path) -> Path:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE sales_orders (
                id INTEGER PRIMARY KEY,
                order_number TEXT NOT NULL,
                customer_id INTEGER,
                customer_po TEXT,
                order_date TEXT,
                delivery_date TEXT,
                status TEXT,
                payment_status TEXT,
                total_amount NUMERIC,
                created_at TEXT
            );
            CREATE TABLE sales_order_items (
                id INTEGER PRIMARY KEY,
                order_id INTEGER NOT NULL REFERENCES sales_orders(id),
                product_id INTEGER,
                item_order_number TEXT,
                snapshot_product_code TEXT,
                snapshot_product_name TEXT,
                quantity INTEGER,
                delivered_quantity INTEGER,
                material_status TEXT,
                requisition_status TEXT,
                inventory_deducted_qty INTEGER
            );
            CREATE TABLE migration_ruida_sales_order_map (
                legacy_order_id INTEGER,
                sales_order_id INTEGER
            );
            CREATE TABLE migration_ruida_sales_item_map (
                legacy_item_id INTEGER,
                sales_order_item_id INTEGER
            );
            CREATE TABLE tianhua_pre_delivery_import_items (
                id INTEGER PRIMARY KEY,
                order_item_id INTEGER REFERENCES sales_order_items(id),
                order_number TEXT,
                status TEXT,
                selected INTEGER,
                warning TEXT
            );
            CREATE TABLE tianhua_pre_delivery_draft_items (
                id INTEGER PRIMARY KEY,
                order_item_id INTEGER REFERENCES sales_order_items(id)
            );
            INSERT INTO sales_orders VALUES
                (1,'RUIDA-1',1,'PO-OLD','2020-01-01','2020-01-02',
                 'pending_production','unpaid',0,'2020-01-01'),
                (2,'TM-NEW',1,'PO-NEW','2026-06-30','2026-07-01',
                 'pending_delivery','unpaid',0,'2026-06-30');
            INSERT INTO sales_order_items VALUES
                (11,1,1,'R-1','21301877','旧订单',200,0,'received','未报料',0),
                (12,2,1,'T-1','21301877','新订单',200,0,'received','未报料',0);
            INSERT INTO migration_ruida_sales_order_map VALUES (101,1);
            INSERT INTO migration_ruida_sales_item_map VALUES (201,11);
            INSERT INTO tianhua_pre_delivery_import_items
                VALUES (1,11,'RUIDA-1','ok',1,'');
            """
        )
    return path


def test_ruida_dry_run_does_not_delete_and_apply_is_safe(tmp_path):
    from scripts.admin.delete_ruida_orders import run

    database = _database(tmp_path / "ruida.sqlite3")
    report_dir = tmp_path / "reports"
    backup_dir = tmp_path / "backups"
    before = _hash(database)

    dry_run = run(database, report_dir, backup_dir, False)
    assert dry_run["orders"] == 1
    assert dry_run["items"] == 1
    assert _hash(database) == before
    assert Path(dry_run["report"]).exists()
    assert Path(dry_run["list"]).exists()

    applied = run(database, report_dir, backup_dir, True)
    assert Path(applied["backup"]).exists()
    assert "BEFORE_DELETE_RUIDA_ORDERS" in Path(applied["backup"]).name
    assert applied["orders_deleted"] == 1
    assert applied["items_deleted"] == 1
    assert applied["integrity_check"] == "ok"
    assert applied["foreign_key_violations"] == 0

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT order_number FROM sales_orders"
        ).fetchall() == [("TM-NEW",)]
        preserved = connection.execute(
            """
            SELECT order_item_id, order_number, status, selected, warning
            FROM tianhua_pre_delivery_import_items
            """
        ).fetchone()
        assert preserved[0] is None
        assert preserved[1] is None
        assert preserved[2] == "not_matched"
        assert preserved[3] == 0
        assert "解除绑定" in preserved[4]
        assert connection.execute(
            "SELECT COUNT(*) FROM tianhua_pre_delivery_import_items"
        ).fetchone()[0] == 1
