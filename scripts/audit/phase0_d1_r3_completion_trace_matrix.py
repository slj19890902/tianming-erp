"""Build the phase-0 D1-R3 completion-trace action matrix read-only."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.admin.weekly_home_uat import verify_package
from scripts.audit.phase0_d1_readonly_reproduction import (
    D1AuditError,
    _anonymous_token,
    _candidate_key,
    _connect_readonly,
    _sha256,
)


def _gap_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        """
WITH gaps AS (
    SELECT
        completion.id AS completion_id,
        completion.task_id,
        completion.order_item_id,
        item.order_id,
        orders.status AS order_status,
        item.quantity AS order_quantity,
        COALESCE(item.delivered_quantity, 0) AS delivered_quantity,
        completion.initial_disposition,
        completion.actual_output_quantity,
        completion.direct_delivery_quantity,
        completion.stock_quantity,
        completion.order_reserved_quantity,
        CASE
            WHEN orders.status NOT IN ('delivered', 'cancelled')
             AND COALESCE(item.is_force_closed, 0) = 0
             AND COALESCE(item.delivered_quantity, 0) < item.quantity
            THEN 1 ELSE 0
        END AS in_current_unfinished_scope
    FROM production_completions AS completion
    JOIN sales_order_items AS item ON item.id = completion.order_item_id
    JOIN sales_orders AS orders ON orders.id = item.order_id
    WHERE completion.status = 'posted'
      AND completion.inventory_lot_id IS NULL
      AND NOT EXISTS (
          SELECT 1 FROM inventory_lots AS lot
          WHERE lot.source_ref_type = 'production_completion'
            AND lot.source_ref_id = completion.id
      )
      AND NOT EXISTS (
          SELECT 1 FROM inventory_movements AS movement
          WHERE movement.movement_type = 'manual_in'
            AND movement.related_order_item_id = completion.order_item_id
      )
), delivery_facts AS (
    SELECT
        delivery_item.order_item_id,
        SUM(CASE WHEN delivery.status = 'dispatched'
                 THEN delivery_item.delivered_quantity ELSE 0 END) AS dispatched_quantity,
        SUM(CASE WHEN delivery.status = 'pending'
                 THEN delivery_item.delivered_quantity ELSE 0 END) AS pending_quantity,
        SUM(CASE WHEN delivery.status = 'voided'
                 THEN delivery_item.delivered_quantity ELSE 0 END) AS voided_quantity,
        SUM(CASE WHEN delivery.status = 'dispatched' THEN 1 ELSE 0 END) AS dispatched_line_count
    FROM sales_delivery_items AS delivery_item
    JOIN sales_deliveries AS delivery ON delivery.id = delivery_item.delivery_id
    GROUP BY delivery_item.order_item_id
), inventory_allocation_facts AS (
    SELECT
        delivery_item.order_item_id,
        COUNT(allocation.id) AS allocation_count,
        COALESCE(SUM(
            allocation.consumed_stock_quantity - allocation.reversed_stock_quantity
        ), 0) AS net_consumed_stock_quantity,
        COUNT(DISTINCT allocation.consume_movement_id) AS consume_movement_count
    FROM sales_delivery_items AS delivery_item
    LEFT JOIN delivery_inventory_allocations AS allocation
      ON allocation.delivery_item_id = delivery_item.id
    GROUP BY delivery_item.order_item_id
), component_direct_facts AS (
    SELECT
        production_completion_id,
        COUNT(*) AS allocation_count,
        COALESCE(SUM(consumed_quantity - reversed_quantity), 0) AS net_consumed_quantity
    FROM bom_component_direct_delivery_allocations
    GROUP BY production_completion_id
)
SELECT
    gaps.*,
    COALESCE(delivery_facts.dispatched_quantity, 0) AS dispatched_quantity,
    COALESCE(delivery_facts.pending_quantity, 0) AS pending_quantity,
    COALESCE(delivery_facts.voided_quantity, 0) AS voided_quantity,
    COALESCE(delivery_facts.dispatched_line_count, 0) AS dispatched_line_count,
    COALESCE(inventory_allocation_facts.allocation_count, 0) AS inventory_allocation_count,
    COALESCE(inventory_allocation_facts.net_consumed_stock_quantity, 0) AS net_inventory_consumed_quantity,
    COALESCE(inventory_allocation_facts.consume_movement_count, 0) AS consume_movement_count,
    COALESCE(component_direct_facts.allocation_count, 0) AS component_direct_allocation_count,
    COALESCE(component_direct_facts.net_consumed_quantity, 0) AS component_direct_consumed_quantity
FROM gaps
LEFT JOIN delivery_facts ON delivery_facts.order_item_id = gaps.order_item_id
LEFT JOIN inventory_allocation_facts
  ON inventory_allocation_facts.order_item_id = gaps.order_item_id
LEFT JOIN component_direct_facts
  ON component_direct_facts.production_completion_id = gaps.completion_id
ORDER BY gaps.completion_id
"""
    ).fetchall()


def _classify(row: sqlite3.Row) -> tuple[str, str]:
    actual = int(row["actual_output_quantity"] or 0)
    direct = int(row["direct_delivery_quantity"] or 0)
    stock = int(row["stock_quantity"] or 0)
    order_quantity = int(row["order_quantity"] or 0)
    delivered = int(row["delivered_quantity"] or 0)
    dispatched = int(row["dispatched_quantity"] or 0)
    inventory_consumed = int(row["net_inventory_consumed_quantity"] or 0)
    component_direct = int(row["component_direct_consumed_quantity"] or 0)
    direct_only = stock == 0 and actual > 0 and direct == actual
    fully_delivered = delivered >= order_quantity and dispatched == delivered and delivered > 0

    if not direct_only:
        return (
            "quantity_contradiction_stop",
            "completion disposition is not a zero-stock direct-output fact",
        )
    if not fully_delivered:
        if dispatched == 0 and bool(row["in_current_unfinished_scope"]):
            return (
                "physical_stocktake_required_before_trace",
                "direct output has no effective dispatch and physical location is unproven",
            )
        return (
            "quantity_contradiction_stop",
            "completion and effective delivery scope do not close arithmetically",
        )
    if component_direct == actual and component_direct > 0:
        return (
            "trace_reconstruction_after_delivery_confirmation",
            "explicit component direct-delivery allocation consumes the full completion output",
        )
    if actual == dispatched:
        return (
            "trace_reconstruction_after_delivery_confirmation",
            "ordinary direct completion equals the effective dispatched quantity",
        )
    if actual + inventory_consumed == dispatched:
        return (
            "trace_reconstruction_after_delivery_confirmation",
            "direct completion plus recorded inventory allocation equals the effective dispatch",
        )
    return (
        "quantity_contradiction_stop",
        "delivered quantity is not explained by completion and allocation facts",
    )


def audit_r3_database(database: Path, *, expected_sha256: str) -> dict[str, Any]:
    sha_before = _sha256(database)
    stat_before = database.stat()
    if sha_before != expected_sha256.strip().lower():
        raise D1AuditError("database SHA-256 does not match the authorized D1 baseline")
    connection = _connect_readonly(database)
    try:
        total_changes_before = connection.total_changes
        candidates: list[dict[str, Any]] = []
        for row in _gap_rows(connection):
            action_class, reason = _classify(row)
            candidates.append(
                {
                    "candidate_key": _candidate_key(
                        "r3-completion-trace", row["completion_id"]
                    ),
                    "order_token": _anonymous_token("ORD", row["order_id"]),
                    "item_token": _anonymous_token("ITEM", row["order_item_id"]),
                    "task_token": _anonymous_token("TASK", row["task_id"]),
                    "completion_token": _anonymous_token(
                        "COMP", row["completion_id"]
                    ),
                    "scope": {
                        "order_status": row["order_status"],
                        "in_current_unfinished_scope": bool(
                            row["in_current_unfinished_scope"]
                        ),
                    },
                    "completion_facts": {
                        "initial_disposition": row["initial_disposition"],
                        "actual_output_quantity": int(
                            row["actual_output_quantity"] or 0
                        ),
                        "direct_delivery_quantity": int(
                            row["direct_delivery_quantity"] or 0
                        ),
                        "stock_quantity": int(row["stock_quantity"] or 0),
                        "order_reserved_quantity": int(
                            row["order_reserved_quantity"] or 0
                        ),
                    },
                    "delivery_facts": {
                        "order_quantity": int(row["order_quantity"] or 0),
                        "item_delivered_quantity": int(
                            row["delivered_quantity"] or 0
                        ),
                        "effective_dispatched_quantity": int(
                            row["dispatched_quantity"] or 0
                        ),
                        "pending_delivery_quantity": int(
                            row["pending_quantity"] or 0
                        ),
                        "voided_delivery_quantity": int(row["voided_quantity"] or 0),
                        "dispatched_line_count": int(
                            row["dispatched_line_count"] or 0
                        ),
                    },
                    "allocation_facts": {
                        "inventory_allocation_count": int(
                            row["inventory_allocation_count"] or 0
                        ),
                        "net_inventory_consumed_quantity": int(
                            row["net_inventory_consumed_quantity"] or 0
                        ),
                        "consume_movement_count": int(
                            row["consume_movement_count"] or 0
                        ),
                        "component_direct_allocation_count": int(
                            row["component_direct_allocation_count"] or 0
                        ),
                        "component_direct_consumed_quantity": int(
                            row["component_direct_consumed_quantity"] or 0
                        ),
                    },
                    "action_class": action_class,
                    "reason": reason,
                    "automatic_apply_allowed": False,
                }
            )
        write_probe_denied = False
        try:
            connection.execute("CREATE TABLE phase0_d1_r3_write_probe(id INTEGER)")
        except sqlite3.DatabaseError:
            write_probe_denied = True
        total_changes_after = connection.total_changes
        query_only = int(connection.execute("PRAGMA query_only").fetchone()[0])
    finally:
        connection.close()
    sha_after = _sha256(database)
    if sha_after != sha_before or database.stat().st_mtime_ns != stat_before.st_mtime_ns:
        raise D1AuditError("database changed during the read-only R3 audit")
    if not write_probe_denied or total_changes_before or total_changes_after:
        raise D1AuditError("read-only R3 database boundary was not preserved")
    action_counts = Counter(row["action_class"] for row in candidates)
    return {
        "report_kind": "phase0_d1_r3_completion_trace_action_matrix",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "database": {
            "sha256_before": sha_before,
            "sha256_after": sha_after,
            "query_only": query_only,
            "write_probe_denied": write_probe_denied,
            "total_changes": total_changes_after,
        },
        "counts": {
            "completion_trace_gaps": len(candidates),
            "trace_reconstruction_after_delivery_confirmation": action_counts[
                "trace_reconstruction_after_delivery_confirmation"
            ],
            "physical_stocktake_required_before_trace": action_counts[
                "physical_stocktake_required_before_trace"
            ],
            "quantity_contradiction_stop": action_counts[
                "quantity_contradiction_stop"
            ],
            "automatic_apply_allowed": 0,
        },
        "decision": {
            "create_inventory_now": False,
            "create_trace_now": False,
            "apply_executed": False,
            "factory_confirmation_required": True,
        },
        "candidates": candidates,
    }


def render_markdown(result: dict[str, Any]) -> str:
    counts = result["counts"]
    lines = [
        "# Phase 0 D1-R3 完工追溯缺口动作矩阵",
        "",
        "## 结论",
        "",
        f"- 全历史缺口：{counts['completion_trace_gaps']}。",
        f"- 已有有效送货事实、待人工确认后仅重建追溯：{counts['trace_reconstruction_after_delivery_confirmation']}。",
        f"- 尚无有效送货、必须先现场盘点：{counts['physical_stocktake_required_before_trace']}。",
        f"- 数量矛盾停止：{counts['quantity_contradiction_stop']}。",
        "- 以上动作均不允许自动执行，本报告没有补库存或补流水。",
        "",
        "## 匿名动作矩阵",
        "",
        "| 完工 | 当前范围 | 产出 | 已送 | 有效发货 | 库存分配消耗 | 组件直送分配 | 动作 |",
        "|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for row in result["candidates"]:
        lines.append(
            f"| `{row['completion_token']}` | "
            f"{row['scope']['in_current_unfinished_scope']} | "
            f"{row['completion_facts']['actual_output_quantity']} | "
            f"{row['delivery_facts']['item_delivered_quantity']} | "
            f"{row['delivery_facts']['effective_dispatched_quantity']} | "
            f"{row['allocation_facts']['net_inventory_consumed_quantity']} | "
            f"{row['allocation_facts']['component_direct_consumed_quantity']} | "
            f"{row['action_class']} |"
        )
    lines.extend(
        [
            "",
            "## 数据边界",
            "",
            f"- 数据库 SHA-256：`{result['database']['sha256_before']}`",
            f"- `query_only={result['database']['query_only']}`，写探针拒绝：{result['database']['write_probe_denied']}，`total_changes={result['database']['total_changes']}`",
            "",
            "> 缺追溯不等于库存仍在，也不等于送货未扣；正式动作必须由工厂端逐项确认。",
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
    result = audit_r3_database(Path(verified["database"]), expected_sha256=verified_sha)
    args.output_json.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    args.output_md.write_text(render_markdown(result), encoding="utf-8")
    print(json.dumps(result["counts"], ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
