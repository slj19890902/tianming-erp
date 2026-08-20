"""Diagnose the D1-R2 virtual-composite receipt projection gap read-only."""

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


def _candidate_items(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        """
WITH receipt_quantity AS (
    SELECT receipt_item.order_item_id, SUM(receipt_item.received_quantity) AS quantity
    FROM incoming_receipt_items AS receipt_item
    JOIN incoming_receipts AS receipt ON receipt.id = receipt_item.receipt_id
    WHERE receipt.status = 'posted' AND receipt_item.status = 'posted'
    GROUP BY receipt_item.order_item_id
), task_state AS (
    SELECT
        order_item_id,
        COUNT(*) AS task_count,
        SUM(CASE WHEN status = 'waiting_material' THEN 1 ELSE 0 END) AS waiting_count
    FROM production_tasks
    GROUP BY order_item_id
)
SELECT
    item.id AS order_item_id,
    item.material_status,
    item.is_virtual_composite_parent_snapshot,
    item.combination_role,
    receipt_quantity.quantity AS parent_linked_receipt_quantity,
    task_state.task_count,
    task_state.waiting_count
FROM sales_order_items AS item
JOIN sales_orders AS orders ON orders.id = item.order_id
JOIN receipt_quantity ON receipt_quantity.order_item_id = item.id
JOIN task_state ON task_state.order_item_id = item.id
WHERE orders.status NOT IN ('delivered', 'cancelled')
  AND COALESCE(item.is_force_closed, 0) = 0
  AND COALESCE(item.delivered_quantity, 0) < item.quantity
  AND task_state.task_count > 0
  AND task_state.task_count = task_state.waiting_count
ORDER BY item.id
"""
    ).fetchall()


def _component_facts(
    connection: sqlite3.Connection,
    order_item_id: int,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
SELECT
    snapshot.id AS snapshot_id,
    snapshot.is_required,
    snapshot.required_piece_quantity,
    snapshot.snapshot_component_default_cutting_mode,
    COUNT(DISTINCT CASE
        WHEN receipt.status = 'posted' AND receipt_item.status = 'posted'
        THEN receipt_item.id END
    ) AS posted_receipt_line_count,
    COALESCE(SUM(CASE
        WHEN receipt.status = 'posted' AND receipt_item.status = 'posted'
        THEN receipt_item.received_quantity ELSE 0 END
    ), 0) AS posted_receipt_sheet_quantity,
    COUNT(DISTINCT CASE
        WHEN source.active_guard = 1 THEN source.id END
    ) AS active_source_count
FROM sales_order_item_bom_components AS snapshot
LEFT JOIN requisition_item_bom_sources AS source
  ON source.sales_order_item_bom_component_id = snapshot.id
 AND source.active_guard = 1
LEFT JOIN incoming_receipt_items AS receipt_item
  ON receipt_item.requisition_item_id = source.requisition_item_id
LEFT JOIN incoming_receipts AS receipt ON receipt.id = receipt_item.receipt_id
WHERE snapshot.sales_order_item_id = ?
GROUP BY snapshot.id
ORDER BY snapshot.display_order, snapshot.id
""",
        (order_item_id,),
    ).fetchall()
    facts: list[dict[str, Any]] = []
    for row in rows:
        factor = cutting_factor(row["snapshot_component_default_cutting_mode"])
        sheets = int(row["posted_receipt_sheet_quantity"] or 0)
        required = int(row["required_piece_quantity"] or 0)
        projected = sheets * factor
        facts.append(
            {
                "component_token": _anonymous_token("COMPONENT", row["snapshot_id"]),
                "is_required": bool(row["is_required"]),
                "required_piece_quantity": required,
                "posted_receipt_sheet_quantity": sheets,
                "frozen_output_factor": factor,
                "projected_component_piece_quantity": projected,
                "posted_receipt_line_count": int(row["posted_receipt_line_count"] or 0),
                "active_source_count": int(row["active_source_count"] or 0),
                "required_source_fully_received": (
                    not bool(row["is_required"])
                    or (
                        int(row["active_source_count"] or 0) == 1
                        and int(row["posted_receipt_line_count"] or 0) > 0
                        and projected >= required
                    )
                ),
            }
        )
    return facts


def audit_r2_database(database: Path, *, expected_sha256: str) -> dict[str, Any]:
    sha_before = _sha256(database)
    stat_before = database.stat()
    if sha_before != expected_sha256.strip().lower():
        raise D1AuditError("database SHA-256 does not match the authorized D1 baseline")
    connection = _connect_readonly(database)
    try:
        total_changes_before = connection.total_changes
        candidates = []
        for row in _candidate_items(connection):
            components = _component_facts(connection, int(row["order_item_id"]))
            required = [component for component in components if component["is_required"]]
            all_required_received = bool(required) and all(
                component["required_source_fully_received"] for component in required
            )
            bug_signature = (
                bool(row["is_virtual_composite_parent_snapshot"])
                and row["material_status"] != "received"
                and all_required_received
            )
            candidates.append(
                {
                    "candidate_key": _candidate_key(
                        "r2-composite-projection", row["order_item_id"]
                    ),
                    "item_token": _anonymous_token("ITEM", row["order_item_id"]),
                    "shape": {
                        "combination_role": row["combination_role"],
                        "virtual_composite_parent": bool(
                            row["is_virtual_composite_parent_snapshot"]
                        ),
                    },
                    "task_facts": {
                        "task_count": int(row["task_count"]),
                        "waiting_material_count": int(row["waiting_count"]),
                    },
                    "receipt_facts": {
                        "parent_linked_receipt_sheet_quantity": int(
                            row["parent_linked_receipt_quantity"] or 0
                        ),
                        "all_required_component_sources_received": all_required_received,
                    },
                    "component_facts": components,
                    "classification": (
                        "virtual_parent_false_parent_receipt_gate"
                        if bug_signature
                        else "manual_review_required"
                    ),
                    "automatic_apply_allowed": False,
                }
            )
        write_probe_denied = False
        try:
            connection.execute("CREATE TABLE phase0_d1_r2_write_probe(id INTEGER)")
        except sqlite3.DatabaseError:
            write_probe_denied = True
        total_changes_after = connection.total_changes
        query_only = int(connection.execute("PRAGMA query_only").fetchone()[0])
    finally:
        connection.close()
    sha_after = _sha256(database)
    if sha_after != sha_before or database.stat().st_mtime_ns != stat_before.st_mtime_ns:
        raise D1AuditError("database changed during the read-only R2 audit")
    if not write_probe_denied or total_changes_before or total_changes_after:
        raise D1AuditError("read-only R2 database boundary was not preserved")
    bug_candidates = [
        row
        for row in candidates
        if row["classification"] == "virtual_parent_false_parent_receipt_gate"
    ]
    return {
        "report_kind": "phase0_d1_r2_composite_receipt_projection",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "database": {
            "sha256_before": sha_before,
            "sha256_after": sha_after,
            "query_only": query_only,
            "write_probe_denied": write_probe_denied,
            "total_changes": total_changes_after,
        },
        "counts": {
            "all_waiting_with_posted_receipt": len(candidates),
            "virtual_parent_false_parent_receipt_gate": len(bug_candidates),
            "automatic_apply_allowed": 0,
        },
        "decision": {
            "minimal_code_fix": "virtual composite parents do not require a physical parent-board receipt after every required component source is received",
            "create_duplicate_task": False,
            "apply_executed": False,
        },
        "candidates": candidates,
    }


def render_markdown(result: dict[str, Any]) -> str:
    candidate = result["candidates"][0] if result["candidates"] else None
    lines = [
        "# Phase 0 D1-R2 组合组件收料投影中断",
        "",
        "## 结论",
        "",
        "- 唯一中断由虚拟组合父项的错误父纸板收料门槛造成。",
        "- 必需组件均有冻结来源和正式收料，既有组件任务不得重复创建。",
        "- 最小代码修复只跳过虚拟父项不存在的物理父纸板要求。",
        "",
        "## 只读计数",
        "",
        f"- 正式收料且任务全等待：{result['counts']['all_waiting_with_posted_receipt']}",
        f"- 命中虚拟父项错误门槛：{result['counts']['virtual_parent_false_parent_receipt_gate']}",
        f"- 允许自动写入：{result['counts']['automatic_apply_allowed']}",
    ]
    if candidate is not None:
        lines.extend(
            [
                "",
                "## 匿名组件事实",
                "",
                "| 组件 | 必需片数 | 收料张数 | 冻结开数 | 投影片数 | 来源完整 |",
                "|---|---:|---:|---:|---:|---|",
            ]
        )
        for component in candidate["component_facts"]:
            lines.append(
                f"| `{component['component_token']}` | {component['required_piece_quantity']} | "
                f"{component['posted_receipt_sheet_quantity']} | "
                f"{component['frozen_output_factor']} | "
                f"{component['projected_component_piece_quantity']} | "
                f"{component['required_source_fully_received']} |"
            )
    lines.extend(
        [
            "",
            f"- 数据库 SHA-256：`{result['database']['sha256_before']}`",
            f"- `query_only={result['database']['query_only']}`，写探针拒绝：{result['database']['write_probe_denied']}，`total_changes={result['database']['total_changes']}`",
            "",
            "> 本报告没有 apply 模式，没有修改正式副本或创建生产任务。",
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
    verified_sha = str(verified["snapshot"]["sha256"])
    if verified_sha.lower() != args.expected_db_sha256.strip().lower():
        raise D1AuditError("package SHA-256 does not match the authorized D1 baseline")
    result = audit_r2_database(Path(verified["database"]), expected_sha256=verified_sha)
    args.output_json.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    args.output_md.write_text(render_markdown(result), encoding="utf-8")
    print(json.dumps(result["counts"], ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
