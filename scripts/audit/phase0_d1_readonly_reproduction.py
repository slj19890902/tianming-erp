"""Reproduce the phase-0 D1 order-chain gaps from a verified UAT package.

The command has no apply mode.  It verifies the three-file weekly UAT package,
opens its SQLite snapshot with ``mode=ro`` plus ``query_only``, installs an
authorizer that rejects writes/schema changes, and emits anonymized evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.admin.weekly_home_uat import verify_package


SQLITE_HEADER = b"SQLite format 3\x00"
REQUIRED_TABLES = {
    "alembic_version",
    "incoming_receipt_items",
    "incoming_receipts",
    "inventory_lots",
    "inventory_movements",
    "production_completions",
    "production_tasks",
    "sales_order_items",
    "sales_orders",
}
WRITE_AUTHORIZER_ACTIONS = {
    sqlite3.SQLITE_CREATE_INDEX,
    sqlite3.SQLITE_CREATE_TABLE,
    sqlite3.SQLITE_CREATE_TEMP_INDEX,
    sqlite3.SQLITE_CREATE_TEMP_TABLE,
    sqlite3.SQLITE_CREATE_TEMP_TRIGGER,
    sqlite3.SQLITE_CREATE_TEMP_VIEW,
    sqlite3.SQLITE_CREATE_TRIGGER,
    sqlite3.SQLITE_CREATE_VIEW,
    sqlite3.SQLITE_DELETE,
    sqlite3.SQLITE_DROP_INDEX,
    sqlite3.SQLITE_DROP_TABLE,
    sqlite3.SQLITE_DROP_TEMP_INDEX,
    sqlite3.SQLITE_DROP_TEMP_TABLE,
    sqlite3.SQLITE_DROP_TEMP_TRIGGER,
    sqlite3.SQLITE_DROP_TEMP_VIEW,
    sqlite3.SQLITE_DROP_TRIGGER,
    sqlite3.SQLITE_DROP_VIEW,
    sqlite3.SQLITE_INSERT,
    sqlite3.SQLITE_UPDATE,
    sqlite3.SQLITE_ALTER_TABLE,
    sqlite3.SQLITE_REINDEX,
    sqlite3.SQLITE_ANALYZE,
    sqlite3.SQLITE_ATTACH,
    sqlite3.SQLITE_DETACH,
}

UNFINISHED_CTE = """
WITH unfinished AS (
    SELECT
        item.*,
        orders.order_number,
        orders.status AS order_status
    FROM sales_order_items AS item
    JOIN sales_orders AS orders ON orders.id = item.order_id
    WHERE orders.status NOT IN ('delivered', 'cancelled')
      AND COALESCE(item.is_force_closed, 0) = 0
      AND COALESCE(item.delivered_quantity, 0) < item.quantity
),
receipt_quantity AS (
    SELECT
        receipt_item.order_item_id,
        SUM(
            CASE
                WHEN receipt.status = 'posted'
                 AND receipt_item.status = 'posted'
                THEN receipt_item.received_quantity
                ELSE 0
            END
        ) AS received_quantity
    FROM incoming_receipt_items AS receipt_item
    JOIN incoming_receipts AS receipt ON receipt.id = receipt_item.receipt_id
    GROUP BY receipt_item.order_item_id
)
"""


class D1AuditError(RuntimeError):
    """Raised when the D1 read-only evidence gate cannot be satisfied."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _readonly_authorizer(
    action: int,
    _arg1: str | None,
    _arg2: str | None,
    _database_name: str | None,
    _trigger_name: str | None,
) -> int:
    if action in WRITE_AUTHORIZER_ACTIONS:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _connect_readonly(database: Path) -> sqlite3.Connection:
    database = database.absolute()
    if not database.is_file():
        raise D1AuditError(f"database does not exist: {database}")
    if any(path.exists() and path.is_symlink() for path in (database, *database.parents)):
        raise D1AuditError(f"symbolic-link database paths are refused: {database}")
    with database.open("rb") as handle:
        if handle.read(len(SQLITE_HEADER)) != SQLITE_HEADER:
            raise D1AuditError(f"file is not an SQLite database: {database}")

    uri_path = quote(database.as_posix(), safe="/:")
    connection = sqlite3.connect(f"file:{uri_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    if int(connection.execute("PRAGMA query_only").fetchone()[0]) != 1:
        connection.close()
        raise D1AuditError("could not enable SQLite query_only")
    connection.set_authorizer(_readonly_authorizer)
    return connection


def _anonymous_token(prefix: str, *parts: object) -> str:
    raw = "|".join(str(part) for part in parts)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def _candidate_key(category: str, *parts: object) -> str:
    raw = "|".join((category, *(str(part) for part in parts)))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    return f"phase0-d1:{category}:{digest}"


def _rows(connection: sqlite3.Connection, sql: str) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(sql)]


def _received_without_task(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    return _rows(
        connection,
        UNFINISHED_CTE
        + """
SELECT
    unfinished.id AS order_item_id,
    unfinished.order_id,
    unfinished.order_number,
    unfinished.order_status,
    unfinished.snapshot_product_code,
    unfinished.material_status,
    unfinished.quantity,
    unfinished.delivered_quantity,
    COALESCE(receipt_quantity.received_quantity, 0) AS received_quantity
FROM unfinished
LEFT JOIN receipt_quantity
  ON receipt_quantity.order_item_id = unfinished.id
WHERE (
        COALESCE(receipt_quantity.received_quantity, 0) > 0
        OR unfinished.material_status = 'received'
      )
  AND NOT EXISTS (
        SELECT 1
        FROM production_tasks AS task
        WHERE task.order_item_id = unfinished.id
      )
ORDER BY unfinished.id
""",
    )


def _received_with_all_tasks_waiting(
    connection: sqlite3.Connection,
) -> list[dict[str, Any]]:
    return _rows(
        connection,
        UNFINISHED_CTE
        + """,
task_state AS (
    SELECT
        order_item_id,
        COUNT(*) AS task_count,
        SUM(CASE WHEN status = 'waiting_material' THEN 1 ELSE 0 END) AS waiting_count
    FROM production_tasks
    GROUP BY order_item_id
)
SELECT
    unfinished.id AS order_item_id,
    unfinished.order_id,
    unfinished.order_number,
    unfinished.order_status,
    unfinished.snapshot_product_code,
    unfinished.material_status,
    unfinished.quantity,
    unfinished.delivered_quantity,
    receipt_quantity.received_quantity,
    task_state.task_count,
    task_state.waiting_count
FROM unfinished
JOIN receipt_quantity ON receipt_quantity.order_item_id = unfinished.id
JOIN task_state ON task_state.order_item_id = unfinished.id
WHERE receipt_quantity.received_quantity > 0
  AND task_state.task_count > 0
  AND task_state.task_count = task_state.waiting_count
ORDER BY unfinished.id
""",
    )


def _received_task_partition(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    return _rows(
        connection,
        UNFINISHED_CTE
        + """,
task_state AS (
    SELECT
        order_item_id,
        COUNT(*) AS task_count,
        SUM(CASE WHEN status = 'waiting_material' THEN 1 ELSE 0 END) AS waiting_count,
        SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending_count,
        SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed_count,
        SUM(CASE WHEN status = 'not_required' THEN 1 ELSE 0 END) AS not_required_count
    FROM production_tasks
    GROUP BY order_item_id
),
received_items AS (
    SELECT
        unfinished.*,
        COALESCE(receipt_quantity.received_quantity, 0) AS received_quantity,
        COALESCE(task_state.task_count, 0) AS task_count,
        COALESCE(task_state.waiting_count, 0) AS waiting_count,
        COALESCE(task_state.pending_count, 0) AS pending_count,
        COALESCE(task_state.completed_count, 0) AS completed_count,
        COALESCE(task_state.not_required_count, 0) AS not_required_count
    FROM unfinished
    LEFT JOIN receipt_quantity ON receipt_quantity.order_item_id = unfinished.id
    LEFT JOIN task_state ON task_state.order_item_id = unfinished.id
    WHERE COALESCE(receipt_quantity.received_quantity, 0) > 0
       OR unfinished.material_status = 'received'
),
classified AS (
    SELECT
        *,
        CASE
            WHEN task_count = 0 THEN 'no_task'
            WHEN waiting_count = task_count THEN 'all_waiting_material'
            WHEN pending_count > 0 THEN 'pending'
            WHEN completed_count + not_required_count = task_count
                THEN 'completed_or_not_required'
            ELSE 'mixed_other'
        END AS task_class
    FROM received_items
)
SELECT
    task_class,
    COUNT(*) AS item_count,
    COUNT(DISTINCT order_id) AS order_count
FROM classified
GROUP BY task_class
ORDER BY task_class
""",
    )


def _completion_trace_gaps(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    return _rows(
        connection,
        """
SELECT
    completion.id AS completion_id,
    completion.task_id,
    completion.order_item_id,
    item.order_id,
    orders.order_number,
    orders.status AS order_status,
    item.snapshot_product_code,
    item.quantity AS order_quantity,
    item.delivered_quantity,
    item.is_force_closed,
    completion.quantity AS completion_quantity,
    completion.actual_output_quantity,
    completion.direct_delivery_quantity,
    completion.stock_quantity,
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
        SELECT 1
        FROM inventory_lots AS lot
        WHERE lot.source_ref_type = 'production_completion'
          AND lot.source_ref_id = completion.id
      )
  AND NOT EXISTS (
        SELECT 1
        FROM inventory_movements AS movement
        WHERE movement.movement_type = 'manual_in'
          AND movement.related_order_item_id = completion.order_item_id
      )
ORDER BY completion.id
""",
    )


def _scope_summary(connection: sqlite3.Connection) -> dict[str, Any]:
    summary = dict(
        connection.execute(
            """
SELECT
    COUNT(DISTINCT item.order_id) AS order_count,
    COUNT(*) AS item_count
FROM sales_order_items AS item
JOIN sales_orders AS orders ON orders.id = item.order_id
WHERE orders.status NOT IN ('delivered', 'cancelled')
  AND COALESCE(item.is_force_closed, 0) = 0
  AND COALESCE(item.delivered_quantity, 0) < item.quantity
"""
        ).fetchone()
    )
    summary["status_counts"] = {
        str(row["status"]): int(row["item_count"])
        for row in connection.execute(
            """
SELECT orders.status, COUNT(*) AS item_count
FROM sales_order_items AS item
JOIN sales_orders AS orders ON orders.id = item.order_id
WHERE orders.status NOT IN ('delivered', 'cancelled')
  AND COALESCE(item.is_force_closed, 0) = 0
  AND COALESCE(item.delivered_quantity, 0) < item.quantity
GROUP BY orders.status
ORDER BY orders.status
"""
        )
    }
    return summary


def _anonymize_received_candidate(row: dict[str, Any]) -> dict[str, Any]:
    normalized = int(row["received_quantity"] or 0) > 0
    subtype = "normalized_receipt_no_task" if normalized else "legacy_status_no_task"
    return {
        "candidate_key": _candidate_key("received-no-task", row["order_item_id"]),
        "order_token": _anonymous_token("ORD", row["order_id"]),
        "item_token": _anonymous_token("ITEM", row["order_item_id"]),
        "product_token": _anonymous_token("PROD", row["snapshot_product_code"] or ""),
        "subtype": subtype,
        "order_status": row["order_status"],
        "order_quantity": int(row["quantity"]),
        "delivered_quantity": int(row["delivered_quantity"] or 0),
        "received_quantity": int(row["received_quantity"] or 0),
        "material_status": row["material_status"],
        "proposed_action": (
            "review_task_creation_from_normalized_receipt"
            if normalized
            else "manual_source_trace_review"
        ),
        "automatic_apply_allowed": False,
    }


def _anonymize_waiting_candidate(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "candidate_key": _candidate_key("waiting-material-trace", row["order_item_id"]),
        "order_token": _anonymous_token("ORD", row["order_id"]),
        "item_token": _anonymous_token("ITEM", row["order_item_id"]),
        "product_token": _anonymous_token("PROD", row["snapshot_product_code"] or ""),
        "subtype": "normalized_receipt_all_tasks_waiting_material",
        "order_status": row["order_status"],
        "received_quantity": int(row["received_quantity"] or 0),
        "task_count": int(row["task_count"]),
        "waiting_task_count": int(row["waiting_count"]),
        "proposed_action": "review_component_material_projection",
        "automatic_apply_allowed": False,
    }


def _anonymize_completion_candidate(row: dict[str, Any]) -> dict[str, Any]:
    direct_only = (
        int(row["stock_quantity"] or 0) == 0
        and int(row["direct_delivery_quantity"] or 0)
        == int(row["actual_output_quantity"] or 0)
    )
    return {
        "candidate_key": _candidate_key("completion-no-inventory", row["completion_id"]),
        "order_token": _anonymous_token("ORD", row["order_id"]),
        "item_token": _anonymous_token("ITEM", row["order_item_id"]),
        "task_token": _anonymous_token("TASK", row["task_id"]),
        "completion_token": _anonymous_token("COMP", row["completion_id"]),
        "product_token": _anonymous_token("PROD", row["snapshot_product_code"] or ""),
        "subtype": (
            "direct_delivery_only_without_inventory_trace"
            if direct_only
            else "other_disposition_without_inventory_trace"
        ),
        "order_status": row["order_status"],
        "completion_quantity": int(row["completion_quantity"] or 0),
        "actual_output_quantity": int(row["actual_output_quantity"] or 0),
        "direct_delivery_quantity": int(row["direct_delivery_quantity"] or 0),
        "stock_quantity": int(row["stock_quantity"] or 0),
        "proposed_action": "manual_inventory_trace_reconciliation",
        "automatic_apply_allowed": False,
    }


def audit_database(database: Path, *, expected_sha256: str | None = None) -> dict[str, Any]:
    database = database.absolute()
    stat_before = database.stat()
    sha_before = _sha256(database)
    if expected_sha256 and sha_before.casefold() != expected_sha256.casefold():
        raise D1AuditError("database SHA-256 does not match the verified package")

    connection = _connect_readonly(database)
    total_changes_before = int(connection.total_changes)
    try:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        missing_tables = sorted(REQUIRED_TABLES - tables)
        if missing_tables:
            raise D1AuditError(f"required tables missing: {', '.join(missing_tables)}")

        revision_row = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()
        revision = str(revision_row[0]) if revision_row else ""
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        foreign_key_violations = sum(
            1 for _row in connection.execute("PRAGMA foreign_key_check")
        )

        authorizer_denied_write = False
        try:
            connection.execute("CREATE TEMP TABLE phase0_d1_write_probe(id INTEGER)")
        except sqlite3.DatabaseError:
            authorizer_denied_write = True
        if not authorizer_denied_write:
            raise D1AuditError("SQLite authorizer did not reject the write probe")

        scope = _scope_summary(connection)
        received_rows = _received_without_task(connection)
        waiting_rows = _received_with_all_tasks_waiting(connection)
        task_partition = _received_task_partition(connection)
        completion_rows = _completion_trace_gaps(connection)
        current_completion_rows = [
            row for row in completion_rows if int(row["in_current_unfinished_scope"]) == 1
        ]

        received_candidates = [
            _anonymize_received_candidate(row) for row in received_rows
        ]
        waiting_candidates = [
            _anonymize_waiting_candidate(row) for row in waiting_rows
        ]
        completion_candidates = [
            _anonymize_completion_candidate(row) for row in current_completion_rows
        ]
        counts = {
            "unfinished_orders": int(scope["order_count"]),
            "unfinished_items": int(scope["item_count"]),
            "received_without_task": len(received_candidates),
            "trace_interruptions": len(received_candidates) + len(waiting_candidates),
            "posted_completion_gaps_current_unfinished": len(completion_candidates),
            "posted_completion_gaps_all_history": len(completion_rows),
        }
        total_changes_after = int(connection.total_changes)
        query_only = int(connection.execute("PRAGMA query_only").fetchone()[0])
    finally:
        connection.close()

    stat_after = database.stat()
    sha_after = _sha256(database)
    if total_changes_before != 0 or total_changes_after != 0:
        raise D1AuditError("read-only audit unexpectedly changed SQLite total_changes")
    if sha_before != sha_after:
        raise D1AuditError("database SHA-256 changed during read-only audit")
    if (
        stat_before.st_size != stat_after.st_size
        or stat_before.st_mtime_ns != stat_after.st_mtime_ns
    ):
        raise D1AuditError("database file state changed during read-only audit")

    subtype_counts: dict[str, int] = {}
    for candidate in received_candidates:
        subtype = str(candidate["subtype"])
        subtype_counts[subtype] = subtype_counts.get(subtype, 0) + 1
    completion_subtypes: dict[str, int] = {}
    for candidate in completion_candidates:
        subtype = str(candidate["subtype"])
        completion_subtypes[subtype] = completion_subtypes.get(subtype, 0) + 1

    before_counts = dict(counts)
    return {
        "schema_version": 1,
        "task": "phase0-d1-readonly-reproduction",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "database": {
            "path": str(database),
            "size": int(stat_before.st_size),
            "mtime_ns_before": int(stat_before.st_mtime_ns),
            "mtime_ns_after": int(stat_after.st_mtime_ns),
            "sha256_before": sha_before,
            "sha256_after": sha_after,
            "revision": revision,
            "quick_check": quick_check,
            "foreign_key_violations": foreign_key_violations,
            "uri_mode": "ro",
            "query_only": query_only,
            "write_authorizer_installed": True,
            "write_probe_denied": authorizer_denied_write,
            "total_changes_before": total_changes_before,
            "total_changes_after": total_changes_after,
        },
        "scope": {
            **scope,
            "definition": (
                "order status not delivered/cancelled; item not force-closed; "
                "delivered quantity below ordered quantity"
            ),
        },
        "counts": counts,
        "classification": {
            "received_without_task_subtypes": subtype_counts,
            "received_evidence_task_partition": {
                str(row["task_class"]): {
                    "item_count": int(row["item_count"]),
                    "order_count": int(row["order_count"]),
                }
                for row in task_partition
            },
            "completion_gap_subtypes": completion_subtypes,
        },
        "dry_run": {
            "apply_executed": False,
            "before_counts": before_counts,
            "after_counts": dict(before_counts),
            "rollback_required": False,
            "rollback_reason": "No database write was attempted or authorized.",
        },
        "candidates": {
            "received_without_task": received_candidates,
            "normalized_receipt_all_tasks_waiting": waiting_candidates,
            "posted_completion_gaps_current_unfinished": completion_candidates,
        },
        "future_repair_contracts": {
            "received_without_task": {
                "idempotency_key": "phase0-d1:received-no-task:<sha256(category|order_item_id)>",
                "transaction_boundary": "one order item and its task/projection facts",
                "preconditions": [
                    "re-read current order/item/receipt/task state",
                    "confirm normalized receipt source before task creation",
                    "legacy-status-only rows require manual source confirmation",
                ],
                "rollback": "rollback the single transaction; never rewrite receipt history",
            },
            "waiting_material_trace": {
                "idempotency_key": "phase0-d1:waiting-material-trace:<sha256(category|order_item_id)>",
                "transaction_boundary": "one order item and all frozen component tasks",
                "preconditions": [
                    "reconcile physical receipt quantities with every frozen component demand",
                    "do not reinterpret task snapshots from current product master data",
                ],
                "rollback": "rollback the single projection transaction",
            },
            "completion_no_inventory": {
                "idempotency_key": "phase0-d1:completion-no-inventory:<sha256(category|completion_id)>",
                "transaction_boundary": "one posted completion plus any provenance lot/movements",
                "preconditions": [
                    "re-read completion, delivery and inventory allocation facts",
                    "confirm physical disposition and quantity with a human",
                    "refuse if a later inventory trace already exists",
                ],
                "rollback": "rollback the single reconciliation transaction and leave completion unchanged",
            },
        },
    }


def _markdown_table(rows: Iterable[tuple[str, object, object]]) -> str:
    lines = ["| 指标 | 阶段 0 时点 | 新副本 |", "|---|---:|---:|"]
    lines.extend(f"| {label} | {old} | {new} |" for label, old, new in rows)
    return "\n".join(lines)


def render_markdown(result: dict[str, Any]) -> str:
    counts = result["counts"]
    database = result["database"]
    classification = result["classification"]
    package = result.get("package", {})
    lines = [
        "# 2026-08-20 Phase 0 D1 readonly reproduction",
        "",
        "## Result",
        "",
        _markdown_table(
            [
                ("unfinished orders", 79, counts["unfinished_orders"]),
                ("unfinished items", 250, counts["unfinished_items"]),
                ("received without task", 53, counts["received_without_task"]),
                ("trace interruptions", 54, counts["trace_interruptions"]),
                (
                    "posted completion gaps in current unfinished scope",
                    27,
                    counts["posted_completion_gaps_current_unfinished"],
                ),
            ]
        ),
        "",
        (
            "Counts are a new snapshot, not fixed expectations. A count leaving the "
            "unfinished scope does not prove that its historical inventory trace was repaired."
        ),
        "",
        "## Classification",
        "",
        "- received-without-task subtypes: `"
        + json.dumps(
            classification["received_without_task_subtypes"],
            ensure_ascii=False,
            sort_keys=True,
        )
        + "`",
        "- received-evidence task partition: `"
        + json.dumps(
            classification["received_evidence_task_partition"],
            ensure_ascii=False,
            sort_keys=True,
        )
        + "`",
        "- current completion-gap subtypes: `"
        + json.dumps(
            classification["completion_gap_subtypes"],
            ensure_ascii=False,
            sort_keys=True,
        )
        + "`",
        f"- all-history posted completion trace gaps: `{counts['posted_completion_gaps_all_history']}`",
        "",
        "The received-evidence partition is a state inventory, not an assertion that every "
        "received item lacks finished stock. Pending/completed/not-required states are not "
        "silently upgraded to errors.",
        "",
        "## Read-only proof",
        "",
        f"- package: `{package.get('package_id', '')}`",
        f"- package Git SHA: `{package.get('git_sha', '')}`",
        f"- database revision: `{database['revision']}`",
        f"- database SHA-256 before/after: `{database['sha256_before']}`",
        f"- size: `{database['size']}` bytes",
        f"- mode/query_only: `ro/{database['query_only']}`",
        f"- write probe denied: `{str(database['write_probe_denied']).lower()}`",
        f"- total_changes before/after: `{database['total_changes_before']}/{database['total_changes_after']}`",
        f"- quick_check/foreign keys: `{database['quick_check']}/{database['foreign_key_violations']}`",
        "",
        "## Dry-run boundary",
        "",
        "- No apply mode exists and no business row was changed.",
        "- Candidate keys and business identities are anonymized in the JSON companion.",
        "- Every candidate is marked `automatic_apply_allowed=false`.",
        "- Any future repair requires a new factory-side authorization, live precondition "
        "recheck, one-object transaction, audit reason and rollback on mismatch.",
        "",
        "## Manual acceptance",
        "",
        "1. Factory reviewers map anonymized candidates to restricted identifiers locally.",
        "2. Confirm normalized receipt source before considering task creation; legacy-status-only "
        "rows remain manual trace reviews.",
        "3. Reconcile component quantities before changing any waiting-material projection.",
        "4. For completion gaps, confirm physical disposition, delivery and inventory allocation; "
        "do not manufacture stock merely because a historical trace is absent.",
        "5. Open a separate authorized repair/migration task if schema or formal data writes are required.",
        "",
    ]
    return "\n".join(lines)


def _write_new(path: Path, content: str) -> None:
    path = path.absolute()
    if path.exists():
        raise D1AuditError(f"refusing to overwrite existing report: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Reproduce phase-0 D1 gaps from a verified read-only UAT package"
    )
    parser.add_argument("--package-dir", type=Path, required=True)
    parser.add_argument("--json-out", type=Path, required=True)
    parser.add_argument("--markdown-out", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        verified = verify_package(args.package_dir)
        manifest = verified["manifest"]
        result = audit_database(
            verified["database"],
            expected_sha256=str(verified["snapshot"]["sha256"]),
        )
        result["package"] = {
            "package_id": manifest["package_id"],
            "created_at": manifest["created_at"],
            "git_sha": manifest["source"]["git_sha"],
            "revision": manifest["database"]["revision"],
            "database_sha256": manifest["database"]["sha256"],
        }
        json_content = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        markdown_content = render_markdown(result)
        _write_new(args.json_out, json_content)
        _write_new(args.markdown_out, markdown_content)
        print(
            json.dumps(
                {
                    "ok": True,
                    "package_id": manifest["package_id"],
                    "counts": result["counts"],
                    "json_out": str(args.json_out.absolute()),
                    "markdown_out": str(args.markdown_out.absolute()),
                    "database_sha256": result["database"]["sha256_after"],
                    "total_changes": result["database"]["total_changes_after"],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    except Exception as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
