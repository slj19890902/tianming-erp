from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Archive current existing ERP orders into settled history view."
    )
    parser.add_argument("--sqlite-path", required=True)
    parser.add_argument("--backup-path")
    parser.add_argument("--cutoff-order-id", type=int)
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


def _resolve_cutoff(connection: sqlite3.Connection, cutoff_order_id: int | None) -> int:
    if cutoff_order_id is not None:
        return cutoff_order_id
    value = connection.execute("SELECT COALESCE(MAX(id), 0) FROM sales_orders").fetchone()[0]
    return int(value or 0)


def _build_plan(connection: sqlite3.Connection, cutoff_order_id: int) -> dict[str, int]:
    orders_to_mark_paid = connection.execute(
        """
        SELECT COUNT(*)
        FROM sales_orders
        WHERE id <= ?
          AND payment_status <> 'paid'
        """,
        (cutoff_order_id,),
    ).fetchone()[0]
    items_to_archive = connection.execute(
        """
        SELECT COUNT(*)
        FROM sales_order_items
        WHERE order_id IN (SELECT id FROM sales_orders WHERE id <= ?)
          AND requisition_status <> '已结算'
        """,
        (cutoff_order_id,),
    ).fetchone()[0]
    return {
        "orders_to_mark_paid": int(orders_to_mark_paid),
        "items_to_archive": int(items_to_archive),
    }


def archive_existing_orders(
    *,
    sqlite_path: Path,
    backup_path: Path,
    cutoff_order_id: int | None,
    apply: bool,
) -> dict[str, object]:
    source = sqlite_path.resolve()
    if not source.is_file():
        raise FileNotFoundError(f"SQLite 文件不存在: {source}")

    with closing(sqlite3.connect(source)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        resolved_cutoff = _resolve_cutoff(connection, cutoff_order_id)
        plan = _build_plan(connection, resolved_cutoff)

    payload: dict[str, object] = {
        "sqlite_path": str(source),
        "cutoff_order_id": resolved_cutoff,
        "plan": plan,
        "applied": False,
        "precheck": integrity_snapshot(source),
    }

    if not apply:
        return payload

    backup = backup_database(source, backup_path.resolve())
    if not backup["hash_match"]:
        raise RuntimeError("备份 SHA-256 与主库不一致，停止执行。")
    if str(payload["precheck"]["integrity_check"]).lower() != "ok":
        raise RuntimeError(f"主库完整性异常: {payload['precheck']['integrity_check']}")
    if str(backup["integrity_check"]).lower() != "ok":
        raise RuntimeError(f"备份完整性异常: {backup['integrity_check']}")

    with closing(sqlite3.connect(source)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN")
        try:
            connection.execute(
                """
                UPDATE sales_orders
                SET payment_status = 'paid'
                WHERE id <= ?
                """,
                (resolved_cutoff,),
            )
            connection.execute(
                """
                UPDATE sales_order_items
                SET requisition_status = '已结算'
                WHERE order_id IN (SELECT id FROM sales_orders WHERE id <= ?)
                """,
                (resolved_cutoff,),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    payload["backup"] = backup
    payload["applied"] = True
    payload["postcheck"] = integrity_snapshot(source)
    return payload


def main() -> int:
    args = build_parser().parse_args()
    sqlite_path = Path(args.sqlite_path)
    backup_path = (
        Path(args.backup_path)
        if args.backup_path
        else PROJECT_ROOT
        / "data"
        / "backups"
        / f"carton_erp_before_archive_existing_orders_{datetime.now():%Y%m%d_%H%M%S}.sqlite3"
    )
    payload = archive_existing_orders(
        sqlite_path=sqlite_path,
        backup_path=backup_path,
        cutoff_order_id=args.cutoff_order_id,
        apply=args.apply,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
