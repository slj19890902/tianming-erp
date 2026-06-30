from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import load_settings
from app.core.database import backup_to_nas


REVISION = "y17s4t5u6v05"
PROTECTED = (
    "sales_orders",
    "sales_order_items",
    "sales_deliveries",
    "sales_delivery_items",
    "finance_return_receipts",
    "finance_return_receipt_items",
    "finance_statements",
    "finance_statement_items",
)
ITEM_COLUMNS = {
    "image_order_no",
    "order_id",
    "customer_order_no",
    "match_reason",
    "match_score",
    "candidate_count",
}


def snapshot(path: Path) -> dict:
    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        return {
            "revision": connection.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()[0],
            "counts": {
                table: connection.execute(
                    f'SELECT COUNT(*) FROM "{table}"'
                ).fetchone()[0]
                for table in PROTECTED
                if table in tables
            },
            "item_columns": {
                row[1]
                for row in connection.execute(
                    'PRAGMA table_info("tianhua_pre_delivery_import_items")'
                )
            },
            "batch_columns": {
                row[1]
                for row in connection.execute(
                    'PRAGMA table_info("tianhua_pre_delivery_import_batches")'
                )
            },
            "integrity": connection.execute(
                "PRAGMA integrity_check"
            ).fetchone()[0],
            "foreign_keys": len(
                connection.execute("PRAGMA foreign_key_check").fetchall()
            ),
        }


def apply(database: Path, backup_dir: Path) -> dict:
    before = snapshot(database)
    if before["revision"] == REVISION:
        if (
            not ITEM_COLUMNS <= before["item_columns"]
            or "pre_delivery_date" not in before["batch_columns"]
        ):
            raise RuntimeError("迁移版本已更新，但天华匹配字段缺失")
        return {"changed": False, "revision": REVISION, "backup": None}

    backup = backup_to_nas(
        source_path=database,
        backup_dir=backup_dir,
        filename_suffix="_TIANHUA_ORDER_MATCHING_BEFORE_MIGRATION",
        keep_regular=100,
    )
    previous = os.environ.get("ERP_DATABASE_PATH")
    os.environ["ERP_DATABASE_PATH"] = str(database)
    try:
        command.upgrade(Config(str(ROOT / "alembic.ini")), REVISION)
    finally:
        if previous is None:
            os.environ.pop("ERP_DATABASE_PATH", None)
        else:
            os.environ["ERP_DATABASE_PATH"] = previous
    after = snapshot(database)
    if (
        after["revision"] != REVISION
        or not ITEM_COLUMNS <= after["item_columns"]
        or "pre_delivery_date" not in after["batch_columns"]
        or before["counts"] != after["counts"]
        or after["integrity"] != "ok"
        or after["foreign_keys"]
    ):
        raise RuntimeError("迁移后验证失败，请使用迁移前备份恢复")
    return {
        "changed": True,
        "revision_before": before["revision"],
        "revision": REVISION,
        "backup": {
            "path": str(backup.path),
            "sha256": backup.sha256,
            "size": backup.size,
            "integrity_check": backup.integrity_check,
        },
        "protected_counts": after["counts"],
        "integrity_check": after["integrity"],
        "foreign_key_violations": after["foreign_keys"],
    }


def main() -> None:
    settings = load_settings()
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, default=settings.database_path)
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=ROOT / "data" / "backups",
    )
    args = parser.parse_args()
    print(
        json.dumps(
            apply(args.database.resolve(), args.backup_dir.resolve()),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
