from __future__ import annotations

import argparse
import hashlib
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = ROOT / "data" / "carton_erp.sqlite3"
BACKUP_DIR = ROOT / "data" / "backups"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def scalar(conn: sqlite3.Connection, sql: str) -> int:
    return int(conn.execute(sql).fetchone()[0])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sqlite-path", type=Path, default=DEFAULT_DB)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    database = args.sqlite_path.resolve()
    with sqlite3.connect(database) as conn:
        before = {
            "orders": scalar(conn, "SELECT COUNT(*) FROM sales_orders"),
            "items": scalar(conn, "SELECT COUNT(*) FROM sales_order_items"),
            "target_orders": scalar(
                conn,
                "SELECT COUNT(*) FROM sales_orders WHERE order_date < '2020-01-01'",
            ),
            "target_items": scalar(
                conn,
                """
                SELECT COUNT(*) FROM sales_order_items i
                JOIN sales_orders o ON o.id=i.order_id
                WHERE o.order_date < '2020-01-01'
                """,
            ),
        }
    print(before)
    if not args.apply:
        return 0
    if args.confirm != "DELETE_PRE2020_ORDERS":
        raise SystemExit("missing confirmation phrase")

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = BACKUP_DIR / f"carton_erp_ARCHIVE_before_cleanup_pre2020_{timestamp}.sqlite3"
    shutil.copy2(database, backup)
    if database.stat().st_size != backup.stat().st_size or sha256(database) != sha256(backup):
        raise SystemExit("backup verification failed")
    with sqlite3.connect(backup) as check:
        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise SystemExit("backup integrity check failed")

    conn = sqlite3.connect(database)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        conn.executescript(
            """
            CREATE TEMP TABLE target_orders AS
            SELECT id FROM sales_orders WHERE order_date < '2020-01-01';
            CREATE TEMP TABLE target_items AS
            SELECT id FROM sales_order_items WHERE order_id IN (SELECT id FROM target_orders);
            CREATE TEMP TABLE target_deliveries AS
            SELECT DISTINCT delivery_id FROM sales_delivery_items
            WHERE order_item_id IN (SELECT id FROM target_items);
            """
        )
        mixed_deliveries = scalar(
            conn,
            """
            SELECT COUNT(*) FROM sales_delivery_items
            WHERE delivery_id IN (SELECT delivery_id FROM target_deliveries)
              AND order_item_id NOT IN (SELECT id FROM target_items)
            """,
        )
        if mixed_deliveries:
            raise RuntimeError("pre-2020 orders share deliveries with retained orders")
        conn.executescript(
            """
            CREATE TEMP TABLE target_receipts AS
            SELECT id FROM finance_return_receipts
            WHERE delivery_id IN (SELECT delivery_id FROM target_deliveries);
            CREATE TEMP TABLE target_receipt_items AS
            SELECT id FROM finance_return_receipt_items
            WHERE return_receipt_id IN (SELECT id FROM target_receipts);
            CREATE TEMP TABLE target_statements AS
            SELECT DISTINCT statement_id FROM finance_statement_items
            WHERE return_receipt_item_id IN (SELECT id FROM target_receipt_items);
            """
        )
        mixed_statements = scalar(
            conn,
            """
            SELECT COUNT(*) FROM finance_statement_items
            WHERE statement_id IN (SELECT statement_id FROM target_statements)
              AND return_receipt_item_id NOT IN (SELECT id FROM target_receipt_items)
            """,
        )
        if mixed_statements:
            raise RuntimeError("pre-2020 orders share statements with retained orders")

        conn.executescript(
            """
            DELETE FROM finance_settlement_records
            WHERE statement_id IN (SELECT statement_id FROM target_statements);
            DELETE FROM finance_invoices
            WHERE statement_id IN (SELECT statement_id FROM target_statements);
            DELETE FROM finance_statement_items
            WHERE statement_id IN (SELECT statement_id FROM target_statements);
            DELETE FROM finance_statements
            WHERE id IN (SELECT statement_id FROM target_statements);
            DELETE FROM finance_return_receipt_items
            WHERE return_receipt_id IN (SELECT id FROM target_receipts);
            DELETE FROM finance_return_receipts
            WHERE id IN (SELECT id FROM target_receipts);
            DELETE FROM sales_delivery_items
            WHERE delivery_id IN (SELECT delivery_id FROM target_deliveries);
            DELETE FROM sales_deliveries
            WHERE id IN (SELECT delivery_id FROM target_deliveries);

            CREATE TEMP TABLE target_requisitions AS
            SELECT DISTINCT requisition_id FROM material_requisition_items
            WHERE order_item_id IN (SELECT id FROM target_items);
            DELETE FROM material_requisition_items
            WHERE order_item_id IN (SELECT id FROM target_items);
            DELETE FROM material_requisitions
            WHERE id IN (SELECT requisition_id FROM target_requisitions)
              AND NOT EXISTS (
                SELECT 1 FROM material_requisition_items
                WHERE material_requisition_items.requisition_id=material_requisitions.id
              );

            DELETE FROM migration_ruida_sales_item_map
            WHERE sales_order_item_id IN (SELECT id FROM target_items);
            DELETE FROM migration_ruida_sales_order_map
            WHERE sales_order_id IN (SELECT id FROM target_orders);
            DELETE FROM migration_entity_map
            WHERE (target_table='sales_orders' AND target_id IN (SELECT id FROM target_orders))
               OR (target_table='sales_order_items' AND target_id IN (SELECT id FROM target_items));
            DELETE FROM operation_logs
            WHERE (entity_type='order' AND entity_id IN (SELECT id FROM target_orders))
               OR (entity_type='order_item' AND entity_id IN (SELECT id FROM target_items));
            DELETE FROM sales_order_items WHERE id IN (SELECT id FROM target_items);
            DELETE FROM sales_orders WHERE id IN (SELECT id FROM target_orders);
            """
        )
        if conn.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("foreign key check failed")
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("integrity check failed")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    with sqlite3.connect(database) as conn:
        after = {
            "orders": scalar(conn, "SELECT COUNT(*) FROM sales_orders"),
            "items": scalar(conn, "SELECT COUNT(*) FROM sales_order_items"),
            "target_orders": scalar(
                conn,
                "SELECT COUNT(*) FROM sales_orders WHERE order_date < '2020-01-01'",
            ),
            "pending_requisitions": scalar(
                conn,
                """
                SELECT COUNT(*) FROM sales_order_items
                WHERE requisition_status IN ('已报料','供应商已排单')
                """,
            ),
            "integrity": conn.execute("PRAGMA integrity_check").fetchone()[0],
            "foreign_key_errors": len(conn.execute("PRAGMA foreign_key_check").fetchall()),
        }
    print({"backup": str(backup), "after": after})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
