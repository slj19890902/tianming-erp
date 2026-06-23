from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SQLITE = ROOT / "data" / "carton_erp.sqlite3"
DEFAULT_SANDBOX_DIR = ROOT / "data" / "sandboxes"
DEFAULT_REPORT_DIR = ROOT / "docs" / "migration_reports"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Rehearse history-order TM renumbering in a sandbox copy only."
    )
    parser.add_argument("--sqlite-path", type=Path, default=DEFAULT_SQLITE)
    parser.add_argument("--sandbox-path", type=Path)
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    return parser


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def sanitize_history_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value)
    for token in ("RUIDA", "ruida", "Ruida", "瑞达"):
        text = text.replace(token, "旧系统")
    return text


def _copy_sqlite(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise RuntimeError(f"refusing to overwrite sandbox: {target}")
    with sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True) as src:
        with sqlite3.connect(target) as dst:
            src.backup(dst)


def _history_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        {
            "sales_order_id": row[0],
            "legacy_order_id": row[1],
            "old_order_number": row[2],
            "customer_id": row[3],
            "customer_po": row[4],
            "order_date": row[5],
            "status": row[6],
            "remark": row[7],
            "created_at": row[8],
        }
        for row in conn.execute(
            """
            SELECT so.id,
                   map.legacy_order_id,
                   so.order_number,
                   so.customer_id,
                   so.customer_po,
                   so.order_date,
                   so.status,
                   so.remark,
                   so.created_at
            FROM sales_orders so
            JOIN migration_ruida_sales_order_map map
              ON map.sales_order_id = so.id
            ORDER BY so.order_date, map.legacy_order_id, so.id
            """
        ).fetchall()
    ]


def _tm_mapping(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        date_key = row["order_date"] or (row["created_at"] or "")[:10].replace("-", "")
        date_key = date_key.replace("-", "") if date_key else "00000000"
        grouped.setdefault(date_key, []).append(row)

    mapped: list[dict[str, Any]] = []
    for date_key in sorted(grouped):
        day_rows = sorted(
            grouped[date_key],
            key=lambda item: (int(item["legacy_order_id"]), int(item["sales_order_id"])),
        )
        for index, row in enumerate(day_rows, start=1):
            mapped.append(
                {
                    **row,
                    "new_order_number": f"TM{date_key}-{index:04d}",
                }
            )
    return mapped


def rehearse_history_display_and_status(
    sqlite_path: Path,
    sandbox_path: Path | None = None,
    report_dir: Path = DEFAULT_REPORT_DIR,
) -> dict[str, Any]:
    sqlite_path = sqlite_path.resolve()
    if not sqlite_path.is_file():
        raise FileNotFoundError(f"sqlite not found: {sqlite_path}")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    sandbox_path = (
        sandbox_path.resolve()
        if sandbox_path is not None
        else (DEFAULT_SANDBOX_DIR / f"carton_erp_history_display_status_rehearsal_{timestamp}.sqlite3").resolve()
    )
    _copy_sqlite(sqlite_path, sandbox_path)

    with sqlite3.connect(sandbox_path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        rows = _history_rows(conn)
        mapped = _tm_mapping(rows)
        conn.execute("BEGIN")
        try:
            for row in mapped:
                conn.execute(
                    """
                    UPDATE sales_orders
                    SET order_number = ?, remark = ?
                    WHERE id = ?
                    """,
                    (
                        row["new_order_number"],
                        sanitize_history_text(row["remark"]),
                        row["sales_order_id"],
                    ),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_key_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
        duplicate_order_numbers = conn.execute(
            """
            SELECT COUNT(*) FROM (
              SELECT order_number
              FROM sales_orders
              GROUP BY order_number
              HAVING COUNT(*) > 1
            )
            """
        ).fetchone()[0]
        remaining_legacy_orders = conn.execute(
            "SELECT COUNT(*) FROM sales_orders WHERE order_number LIKE 'RUIDA-%'"
        ).fetchone()[0]
        tm_orders = conn.execute(
            "SELECT COUNT(*) FROM sales_orders WHERE order_number LIKE 'TM%'"
        ).fetchone()[0]

    report_dir.mkdir(parents=True, exist_ok=True)
    mapping_csv = report_dir / f"HISTORY_ORDER_TM_RENUMBER_MAPPING_{timestamp}.csv"
    with mapping_csv.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "sales_order_id",
                "legacy_order_id",
                "old_order_number",
                "new_order_number",
                "customer_id",
                "customer_po",
                "order_date",
                "status",
            ],
        )
        writer.writeheader()
        for row in mapped:
            writer.writerow(
                {
                    "sales_order_id": row["sales_order_id"],
                    "legacy_order_id": row["legacy_order_id"],
                    "old_order_number": row["old_order_number"],
                    "new_order_number": row["new_order_number"],
                    "customer_id": row["customer_id"],
                    "customer_po": row["customer_po"],
                    "order_date": row["order_date"],
                    "status": row["status"],
                }
            )

    result = {
        "source_sqlite": str(sqlite_path),
        "source_sha256": sha256(sqlite_path),
        "sandbox_sqlite": str(sandbox_path),
        "sandbox_sha256": sha256(sandbox_path),
        "history_orders": len(mapped),
        "tm_orders": tm_orders,
        "remaining_legacy_orders": remaining_legacy_orders,
        "duplicate_order_numbers": duplicate_order_numbers,
        "integrity_check": integrity,
        "foreign_key_check": len(foreign_key_rows),
        "mapping_csv": str(mapping_csv),
        "main_db_changed": False,
    }
    report_path = report_dir / f"HISTORY_ORDER_TM_RENUMBER_REHEARSAL_{timestamp}.md"
    report_path.write_text(
        "# 历史订单 TM 编号副本演练\n\n"
        f"- 执行时间：{timestamp}\n"
        f"- 主库路径：{sqlite_path}\n"
        f"- 副本路径：{sandbox_path}\n"
        f"- 主库是否改号：否\n"
        f"- 副本 TM 数量：{tm_orders}\n"
        f"- 副本剩余旧前缀数量：{remaining_legacy_orders}\n"
        f"- 完整性检查：{integrity}\n"
        f"- 外键检查：{len(foreign_key_rows)}\n"
        f"- 映射文件：{mapping_csv}\n\n"
        "```json\n"
        + json.dumps(result, ensure_ascii=False, indent=2)
        + "\n```\n",
        encoding="utf-8",
    )
    result["report_path"] = str(report_path)
    return result


def main() -> int:
    args = build_parser().parse_args()
    result = rehearse_history_display_and_status(
        sqlite_path=args.sqlite_path,
        sandbox_path=args.sandbox_path,
        report_dir=args.report_dir,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
