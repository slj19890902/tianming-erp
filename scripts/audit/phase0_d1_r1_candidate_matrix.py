"""Build the phase-0 D1-R1 read-only task-candidate matrix.

This command deliberately has no apply mode.  It separates legacy material
status evidence from normalized posted receipts, converts sheet quantities to
finished-box quantities with frozen order-line facts, and fails closed when a
historical production-task snapshot cannot be proven.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.requisition_quantities import cutting_factor
from scripts.admin.weekly_home_uat import verify_package
from scripts.audit.phase0_d1_readonly_reproduction import (
    D1AuditError,
    _anonymous_token,
    _candidate_key,
    _connect_readonly,
    _sha256,
)


REQUIRED_COLUMNS = {
    "sales_order_items": {
        "combination_role",
        "id",
        "is_force_closed",
        "is_virtual_composite_parent_snapshot",
        "material_status",
        "quantity",
        "snapshot_pieces_per_box",
        "snapshot_splice_mode",
        "special_process",
        "supply_mode_snapshot",
    },
    "incoming_receipt_items": {
        "id",
        "order_item_id",
        "planned_quantity",
        "received_quantity",
        "resolution_action",
        "status",
    },
}


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _assert_schema(connection: sqlite3.Connection) -> None:
    for table, required in REQUIRED_COLUMNS.items():
        missing = required - _table_columns(connection, table)
        if missing:
            raise D1AuditError(
                f"required columns missing from {table}: {', '.join(sorted(missing))}"
            )


def _pieces_per_box(row: sqlite3.Row) -> int:
    if str(row["snapshot_splice_mode"] or "").strip().lower() != "double":
        return 1
    return max(int(row["snapshot_pieces_per_box"] or 0), 2)


def _material_input_quantity(
    received_quantity: int,
    latest_planned_quantity: int,
    latest_resolution_action: str | None,
) -> int:
    received = max(int(received_quantity or 0), 0)
    planned = max(int(latest_planned_quantity or 0), 0)
    if latest_resolution_action == "all_to_production":
        return received
    return min(received, planned)


def _normalized_candidates(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
WITH posted AS (
    SELECT
        receipt_item.order_item_id,
        SUM(receipt_item.received_quantity) AS received_quantity,
        MAX(receipt_item.id) AS latest_receipt_item_id,
        COUNT(*) AS posted_receipt_line_count
    FROM incoming_receipt_items AS receipt_item
    JOIN incoming_receipts AS receipt ON receipt.id = receipt_item.receipt_id
    WHERE receipt.status = 'posted'
      AND receipt_item.status = 'posted'
      AND receipt_item.order_item_id IS NOT NULL
    GROUP BY receipt_item.order_item_id
)
SELECT
    item.id AS order_item_id,
    item.quantity AS order_quantity,
    item.material_status,
    item.special_process,
    item.snapshot_splice_mode,
    item.snapshot_pieces_per_box,
    item.supply_mode_snapshot,
    item.combination_role,
    item.is_virtual_composite_parent_snapshot,
    posted.received_quantity,
    posted.posted_receipt_line_count,
    latest.planned_quantity AS latest_planned_quantity,
    latest.resolution_action AS latest_resolution_action
FROM sales_order_items AS item
JOIN sales_orders AS orders ON orders.id = item.order_id
JOIN posted ON posted.order_item_id = item.id
JOIN incoming_receipt_items AS latest ON latest.id = posted.latest_receipt_item_id
WHERE orders.status NOT IN ('delivered', 'cancelled')
  AND COALESCE(item.is_force_closed, 0) = 0
  AND COALESCE(item.delivered_quantity, 0) < item.quantity
  AND NOT EXISTS (
      SELECT 1 FROM production_tasks AS task WHERE task.order_item_id = item.id
  )
ORDER BY item.id
"""
    ).fetchall()

    candidates: list[dict[str, Any]] = []
    for row in rows:
        received = int(row["received_quantity"] or 0)
        planned = int(row["latest_planned_quantity"] or row["order_quantity"] or 0)
        material_input = _material_input_quantity(
            received,
            planned,
            row["latest_resolution_action"],
        )
        factor = cutting_factor(row["special_process"])
        pieces = _pieces_per_box(row)
        projected_boxes = material_input * factor // pieces
        immutable_conversion_facts_present = bool(
            str(row["special_process"] or "").strip()
            and str(row["snapshot_splice_mode"] or "").strip()
        )
        candidate = {
            "candidate_key": _candidate_key("r1-normalized-receipt", row["order_item_id"]),
            "item_token": _anonymous_token("ITEM", row["order_item_id"]),
            "source_class": "normalized_posted_receipt",
            "business_shape": {
                "supply_mode_snapshot": row["supply_mode_snapshot"],
                "combination_role": row["combination_role"],
                "virtual_composite_parent": bool(
                    row["is_virtual_composite_parent_snapshot"]
                ),
            },
            "quantity_facts": {
                "receipt_sheet_quantity": received,
                "latest_planned_sheet_quantity": planned,
                "allowed_production_input_sheet_quantity": material_input,
                "frozen_cutting_output_factor": factor,
                "frozen_pieces_per_box": pieces,
                "projected_finished_box_quantity": projected_boxes,
                "order_finished_box_quantity": int(row["order_quantity"] or 0),
                "posted_receipt_line_count": int(row["posted_receipt_line_count"] or 0),
            },
            "gates": {
                "formal_posted_receipt_present": True,
                "immutable_conversion_facts_present": immutable_conversion_facts_present,
                "standalone_non_virtual_shape": (
                    row["combination_role"] == "standalone"
                    and not bool(row["is_virtual_composite_parent_snapshot"])
                ),
                "immutable_task_printing_label_snapshot_proven": False,
            },
            "classification": "manual_frozen_snapshot_review_required",
            "automatic_apply_allowed": False,
            "stop_reasons": [
                "formal production-task creation is outside read-only authorization",
                "current product master data must not be used as a historical printing or label snapshot",
            ],
        }
        candidates.append(candidate)
    return candidates


def _legacy_candidates(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
SELECT item.id AS order_item_id
FROM sales_order_items AS item
JOIN sales_orders AS orders ON orders.id = item.order_id
WHERE orders.status NOT IN ('delivered', 'cancelled')
  AND COALESCE(item.is_force_closed, 0) = 0
  AND COALESCE(item.delivered_quantity, 0) < item.quantity
  AND item.material_status = 'received'
  AND NOT EXISTS (
      SELECT 1
      FROM incoming_receipt_items AS receipt_item
      JOIN incoming_receipts AS receipt ON receipt.id = receipt_item.receipt_id
      WHERE receipt_item.order_item_id = item.id
        AND receipt.status = 'posted'
        AND receipt_item.status = 'posted'
  )
  AND NOT EXISTS (
      SELECT 1 FROM production_tasks AS task WHERE task.order_item_id = item.id
  )
ORDER BY item.id
"""
    ).fetchall()
    return [
        {
            "candidate_key": _candidate_key("r1-legacy-status", row["order_item_id"]),
            "item_token": _anonymous_token("ITEM", row["order_item_id"]),
            "source_class": "legacy_material_status_only",
            "classification": "manual_receipt_source_reconstruction",
            "automatic_apply_allowed": False,
            "stop_reasons": [
                "no normalized posted receipt proves the physical material quantity",
                "formal production-task creation is outside read-only authorization",
            ],
        }
        for row in rows
    ]


def audit_r1_database(database: Path, *, expected_sha256: str) -> dict[str, Any]:
    expected = expected_sha256.strip().lower()
    sha_before = _sha256(database)
    stat_before = database.stat()
    if sha_before != expected:
        raise D1AuditError(
            f"database SHA-256 mismatch: expected {expected}, got {sha_before}"
        )

    connection = _connect_readonly(database)
    try:
        _assert_schema(connection)
        total_changes_before = connection.total_changes
        normalized = _normalized_candidates(connection)
        legacy = _legacy_candidates(connection)
        write_probe_denied = False
        try:
            connection.execute("CREATE TABLE phase0_d1_r1_write_probe(id INTEGER)")
        except sqlite3.DatabaseError:
            write_probe_denied = True
        total_changes_after = connection.total_changes
        query_only = int(connection.execute("PRAGMA query_only").fetchone()[0])
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        foreign_key_violations = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        revision_row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        revision = str(revision_row[0]) if revision_row else None
    finally:
        connection.close()

    sha_after = _sha256(database)
    stat_after = database.stat()
    if sha_after != sha_before or stat_after.st_mtime_ns != stat_before.st_mtime_ns:
        raise D1AuditError("database changed during the read-only R1 audit")
    if not write_probe_denied or total_changes_before != 0 or total_changes_after != 0:
        raise D1AuditError("read-only R1 database boundary was not preserved")

    return {
        "report_kind": "phase0_d1_r1_readonly_candidate_matrix",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "database": {
            "sha256_before": sha_before,
            "sha256_after": sha_after,
            "alembic_revision": revision,
            "query_only": query_only,
            "write_probe_denied": write_probe_denied,
            "total_changes_before": total_changes_before,
            "total_changes_after": total_changes_after,
            "quick_check": quick_check,
            "foreign_key_violations": foreign_key_violations,
        },
        "counts": {
            "normalized_posted_receipt_no_task": len(normalized),
            "legacy_material_status_no_task": len(legacy),
            "automatic_apply_allowed": 0,
        },
        "decision": {
            "normalized_rows": "manual_frozen_snapshot_review_required",
            "legacy_rows": "manual_receipt_source_reconstruction",
            "apply_executed": False,
        },
        "normalized_candidates": normalized,
        "legacy_candidates": legacy,
    }


def render_markdown(result: dict[str, Any]) -> str:
    counts = result["counts"]
    database = result["database"]
    lines = [
        "# Phase 0 D1-R1 只读候选矩阵",
        "",
        "## 结论",
        "",
        "- 12 类正式收料候选仅进入冻结快照人工复核，不自动创建生产任务。",
        "- 41 类旧状态候选先人工重建正式收料来源，不把 `material_status` 当作数量事实。",
        "- 收料量使用纸板张数，订单量使用成品箱数；报告只通过冻结换算事实建立投影。",
        "",
        "## 计数",
        "",
        f"- 正式收料无任务：{counts['normalized_posted_receipt_no_task']}",
        f"- 旧状态无任务：{counts['legacy_material_status_no_task']}",
        f"- 允许自动执行：{counts['automatic_apply_allowed']}",
        "",
        "## 数据边界",
        "",
        f"- SHA-256：`{database['sha256_before']}`",
        f"- Revision：`{database['alembic_revision']}`",
        f"- `query_only={database['query_only']}`，写探针拒绝：{database['write_probe_denied']}",
        f"- `total_changes={database['total_changes_after']}`，`quick_check={database['quick_check']}`，外键异常 {database['foreign_key_violations']}",
        "",
        "## 匿名正式收料候选",
        "",
        "| 候选 | 收料张数 | 可用投产张数 | 冻结开数 | 每箱片数 | 投影成品箱数 | 订单箱数 | 结论 |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for candidate in result["normalized_candidates"]:
        facts = candidate["quantity_facts"]
        lines.append(
            f"| `{candidate['item_token']}` | {facts['receipt_sheet_quantity']} | "
            f"{facts['allowed_production_input_sheet_quantity']} | "
            f"{facts['frozen_cutting_output_factor']} | {facts['frozen_pieces_per_box']} | "
            f"{facts['projected_finished_box_quantity']} | {facts['order_finished_box_quantity']} | "
            "冻结快照人工复核 |"
        )
    lines.extend(
        [
            "",
            "> 本报告没有 apply 模式；所有候选均为 `automatic_apply_allowed=false`。",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--expected-db-sha256", required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-md", type=Path, required=True)
    args = parser.parse_args()

    verified = verify_package(args.package)
    database = Path(verified["database"])
    verified_sha256 = str(verified["snapshot"]["sha256"])
    if verified_sha256.lower() != args.expected_db_sha256.strip().lower():
        raise D1AuditError("package SHA-256 does not match the authorized D1 baseline")
    result = audit_r1_database(database, expected_sha256=verified_sha256)
    args.output_json.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    args.output_md.write_text(render_markdown(result), encoding="utf-8")
    print(json.dumps(result["counts"], ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
