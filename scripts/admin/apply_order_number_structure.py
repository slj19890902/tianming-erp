from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Backup and apply order number structure migration to a SQLite database."
    )
    parser.add_argument("--sqlite-path", required=True)
    parser.add_argument("--backup-path")
    parser.add_argument("--apply", action="store_true")
    return parser


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_handle:
        for block in iter(lambda: file_handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def integrity_snapshot(path: Path) -> dict[str, object]:
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return {
            "integrity_check": connection.execute("PRAGMA integrity_check").fetchone()[0],
            "foreign_key_check": [
                tuple(row)
                for row in connection.execute("PRAGMA foreign_key_check").fetchall()
            ],
            "sales_orders": connection.execute(
                "SELECT COUNT(*) FROM sales_orders"
            ).fetchone()[0],
            "sales_order_items": connection.execute(
                "SELECT COUNT(*) FROM sales_order_items"
            ).fetchone()[0],
        }


def backup_database(source_path: Path, backup_path: Path) -> dict[str, object]:
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, backup_path)
    source_sha = sha256_file(source_path)
    backup_sha = sha256_file(backup_path)
    return {
        "path": str(backup_path.resolve()),
        "size": backup_path.stat().st_size,
        "mtime": datetime.fromtimestamp(backup_path.stat().st_mtime).isoformat(timespec="seconds"),
        "source_sha256": source_sha,
        "backup_sha256": backup_sha,
        "hash_match": source_sha == backup_sha,
        "integrity_check": integrity_snapshot(backup_path)["integrity_check"],
    }


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        row["name"]
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }


def _index_names(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        row["name"]
        for row in connection.execute(f"PRAGMA index_list({table})").fetchall()
    }


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def migrate_schema(connection: sqlite3.Connection) -> dict[str, list[str]]:
    added_columns: list[str] = []
    added_tables: list[str] = []
    added_indexes: list[str] = []

    item_columns = _table_columns(connection, "sales_order_items")
    if "item_order_number" not in item_columns:
        connection.execute(
            "ALTER TABLE sales_order_items ADD COLUMN item_order_number TEXT"
        )
        added_columns.append("sales_order_items.item_order_number")
    if "item_sequence" not in item_columns:
        connection.execute(
            "ALTER TABLE sales_order_items ADD COLUMN item_sequence INTEGER"
        )
        added_columns.append("sales_order_items.item_sequence")

    if "order_item_number_sequences" not in _table_names(connection):
        connection.execute(
            """
            CREATE TABLE order_item_number_sequences (
                order_id INTEGER PRIMARY KEY,
                last_item_sequence INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(order_id) REFERENCES sales_orders(id) ON DELETE CASCADE
            )
            """
        )
        added_tables.append("order_item_number_sequences")

    order_indexes = _index_names(connection, "sales_orders")
    item_indexes = _index_names(connection, "sales_order_items")
    index_sql = {
        "ix_sales_orders_customer_po": (
            "sales_orders",
            "CREATE INDEX ix_sales_orders_customer_po ON sales_orders (customer_po)",
        ),
        "ix_sales_orders_customer_po_group": (
            "sales_orders",
            "CREATE INDEX ix_sales_orders_customer_po_group ON sales_orders (customer_id, customer_po)",
        ),
        "ux_sales_order_items_item_order_number": (
            "sales_order_items",
            "CREATE UNIQUE INDEX ux_sales_order_items_item_order_number ON sales_order_items (item_order_number)",
        ),
        "ix_sales_order_items_snapshot_product_code": (
            "sales_order_items",
            "CREATE INDEX ix_sales_order_items_snapshot_product_code ON sales_order_items (snapshot_product_code)",
        ),
        "ix_sales_order_items_snapshot_product_name": (
            "sales_order_items",
            "CREATE INDEX ix_sales_order_items_snapshot_product_name ON sales_order_items (snapshot_product_name)",
        ),
    }
    for name, (table_name, sql) in index_sql.items():
        existing = order_indexes if table_name == "sales_orders" else item_indexes
        if name not in existing:
            connection.execute(sql)
            added_indexes.append(name)

    return {
        "added_columns": added_columns,
        "added_tables": added_tables,
        "added_indexes": added_indexes,
    }


def apply_order_number_structure(
    *,
    sqlite_path: Path,
    backup_path: Path,
) -> dict[str, object]:
    source = sqlite_path.resolve()
    if not source.is_file():
        raise FileNotFoundError(f"SQLite 文件不存在: {source}")

    precheck = integrity_snapshot(source)
    backup = backup_database(source, backup_path.resolve())
    if not backup["hash_match"]:
        raise RuntimeError("备份 SHA-256 与主库不一致，停止执行。")
    if str(precheck["integrity_check"]).lower() != "ok":
        raise RuntimeError(f"主库完整性异常: {precheck['integrity_check']}")
    if str(backup["integrity_check"]).lower() != "ok":
        raise RuntimeError(f"备份完整性异常: {backup['integrity_check']}")

    with closing(sqlite3.connect(source)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN")
        try:
            migration = migrate_schema(connection)
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    return {
        "sqlite_path": str(source),
        "backup": backup,
        "precheck": precheck,
        "migration": migration,
        "postcheck": integrity_snapshot(source),
    }


def main() -> int:
    args = build_parser().parse_args()
    sqlite_path = Path(args.sqlite_path)
    backup_path = (
        Path(args.backup_path)
        if args.backup_path
        else PROJECT_ROOT
        / "data"
        / "backups"
        / f"carton_erp_before_order_number_structure_apply_{datetime.now():%Y%m%d_%H%M%S}.sqlite3"
    )

    payload = {
        "apply": args.apply,
        "sqlite_path": str(sqlite_path.resolve()),
        "backup_path": str(backup_path.resolve()),
    }
    if args.apply:
        payload["result"] = apply_order_number_structure(
            sqlite_path=sqlite_path,
            backup_path=backup_path,
        )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
