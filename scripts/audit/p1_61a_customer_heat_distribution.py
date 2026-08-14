"""P1-61A customer-activity distribution audit (read-only, anonymized).

This command deliberately has no apply mode.  It accepts only an explicit
SQLite isolated-copy path, opens that database with ``mode=ro`` and
``PRAGMA query_only=ON``, and installs an authorizer that rejects every write
or schema action.  Only the requested JSON and Markdown reports are written.

The core customer roll-up is one SQL statement.  Sales orders are first
collapsed to one row per master order, so a multi-line order contributes one
frequency event.  Historical ``sales_order_items.unit_price`` / ``subtotal``
values are treated as the frozen amount evidence; a missing or non-positive
line price makes the whole order amount incomplete instead of silently zero.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import secrets
import sqlite3
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import quote


EXPECTED_REVISION = "mm21v8x9z10"
SQLITE_HEADER = b"SQLite format 3\x00"
REQUIRED_COLUMNS = {
    "customers": {"id", "status", "is_active"},
    "sales_orders": {"id", "order_number", "customer_id", "order_date", "status"},
    "sales_order_items": {
        "id",
        "order_id",
        "quantity",
        "unit_price",
        "subtotal",
    },
    "alembic_version": {"version_num"},
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


CORE_AGGREGATE_SQL = """
WITH valid_master_orders AS (
    SELECT
        o.id AS order_id,
        o.customer_id AS customer_id,
        date(o.order_date) AS order_day,
        CASE
            WHEN COUNT(i.id) = 0 THEN 0
            WHEN SUM(
                CASE
                    WHEN i.unit_price IS NULL
                      OR CAST(i.unit_price AS REAL) <= 0
                      OR i.subtotal IS NULL
                    THEN 1 ELSE 0
                END
            ) > 0 THEN 0
            ELSE 1
        END AS amount_complete,
        CASE
            WHEN COUNT(i.id) = 0 THEN NULL
            WHEN SUM(
                CASE
                    WHEN i.unit_price IS NULL
                      OR CAST(i.unit_price AS REAL) <= 0
                      OR i.subtotal IS NULL
                    THEN 1 ELSE 0
                END
            ) > 0 THEN NULL
            ELSE ROUND(SUM(CAST(i.subtotal AS REAL)), 2)
        END AS frozen_order_amount
    FROM sales_orders AS o
    LEFT JOIN sales_order_items AS i ON i.order_id = o.id
    WHERE lower(trim(o.status)) NOT IN (
        'dead', 'cancelled', 'canceled', 'void', 'voided', 'draft', 'test'
    )
      AND o.order_number IS NOT NULL
      AND length(trim(o.order_number)) > 0
      AND o.order_date IS NOT NULL
      AND date(o.order_date) <= date(:as_of)
    GROUP BY o.id, o.customer_id, date(o.order_date)
), customer_rollup AS (
    SELECT
        c.id AS customer_id,
        MIN(v.order_day) AS first_valid_order_date,
        MAX(v.order_day) AS last_valid_order_date,
        COUNT(v.order_id) AS valid_master_order_count_all,
        CAST(
            CASE
                WHEN MAX(v.order_day) IS NULL THEN NULL
                ELSE julianday(date(:as_of)) - julianday(MAX(v.order_day))
            END AS INTEGER
        ) AS recency_days,
        COUNT(DISTINCT CASE
            WHEN v.order_day >= date(:as_of, '-89 days') THEN v.order_day
        END) AS valid_order_days_90,
        COUNT(CASE
            WHEN v.order_day >= date(:as_of, '-364 days') THEN 1
        END) AS valid_master_orders_365,
        COUNT(CASE
            WHEN v.order_day >= date(:as_of, '-364 days')
             AND v.amount_complete = 1 THEN 1
        END) AS amount_complete_orders_365,
        COUNT(CASE
            WHEN v.order_day >= date(:as_of, '-364 days')
             AND v.amount_complete = 0 THEN 1
        END) AS amount_incomplete_orders_365,
        ROUND(SUM(CASE
            WHEN v.order_day >= date(:as_of, '-364 days')
             AND v.amount_complete = 1 THEN v.frozen_order_amount
            ELSE 0
        END), 2) AS known_frozen_amount_365
    FROM customers AS c
    LEFT JOIN valid_master_orders AS v ON v.customer_id = c.id
    WHERE c.is_active = 1 AND lower(trim(c.status)) = 'active'
    GROUP BY c.id
)
SELECT
    customer_id,
    first_valid_order_date,
    last_valid_order_date,
    valid_master_order_count_all,
    recency_days,
    valid_order_days_90,
    valid_master_orders_365,
    amount_complete_orders_365,
    amount_incomplete_orders_365,
    known_frozen_amount_365,
    CASE
        WHEN valid_master_orders_365 = 0 THEN NULL
        ELSE ROUND(
            CAST(amount_complete_orders_365 AS REAL)
            / CAST(valid_master_orders_365 AS REAL),
            6
        )
    END AS amount_completeness_rate_365,
    CASE
        WHEN valid_master_orders_365 = 0 THEN NULL
        WHEN amount_incomplete_orders_365 > 0 THEN NULL
        ELSE known_frozen_amount_365
    END AS complete_frozen_amount_365,
    CASE
        WHEN valid_master_order_count_all = 0 THEN 0
        WHEN valid_master_order_count_all < 3 THEN 1
        WHEN julianday(last_valid_order_date) - julianday(first_valid_order_date) < 30 THEN 1
        ELSE 0
    END AS is_new_customer,
    CASE
        WHEN recency_days IS NULL OR recency_days > 365 THEN 1 ELSE 0
    END AS is_dormant_over_365
FROM customer_rollup
ORDER BY customer_id
""".strip()


@dataclass(frozen=True)
class SourceState:
    sha256: str
    size: int
    mtime_ns: int
    sidecars: dict[str, dict[str, int | bool | None]]


def _is_symlink_path(path: Path) -> bool:
    candidate = path.absolute()
    return any(
        parent.exists() and parent.is_symlink()
        for parent in (candidate, *candidate.parents)
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _sidecar_state(database: Path) -> dict[str, dict[str, int | bool | None]]:
    state: dict[str, dict[str, int | bool | None]] = {}
    for suffix in ("-journal", "-wal", "-shm"):
        candidate = Path(str(database) + suffix)
        exists = candidate.exists()
        state[suffix] = {
            "exists": exists,
            "size": candidate.stat().st_size if exists else None,
            "mtime_ns": candidate.stat().st_mtime_ns if exists else None,
        }
    return state


def _source_state(database: Path) -> SourceState:
    stat = database.stat()
    return SourceState(
        sha256=_sha256(database),
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        sidecars=_sidecar_state(database),
    )


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


def connect_readonly(database: Path) -> sqlite3.Connection:
    """Open an existing, non-symlink SQLite file with two write barriers."""

    if not database.is_file():
        raise ValueError(f"database must be an existing regular file: {database}")
    if _is_symlink_path(database):
        raise ValueError(f"symbolic-link database paths are refused: {database}")
    with database.open("rb") as handle:
        if handle.read(len(SQLITE_HEADER)) != SQLITE_HEADER:
            raise ValueError(f"file is not an SQLite 3 database: {database}")

    uri_path = quote(database.resolve().as_posix(), safe="/:")
    connection = sqlite3.connect(f"file:{uri_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    if connection.execute("PRAGMA query_only").fetchone()[0] != 1:
        connection.close()
        raise RuntimeError("could not enable SQLite query_only mode")
    connection.set_authorizer(_readonly_authorizer)
    return connection


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    escaped = table.replace('"', '""')
    return {
        str(row[1])
        for row in connection.execute(f'PRAGMA table_info("{escaped}")')
    }


def _foreign_key_error_count(database: Path) -> int:
    """Run FK introspection on a separate read-only connection.

    A few Windows SQLite builds reject ``foreign_key_check`` when the main
    query connection has a strict authorizer installed.  The auxiliary URI is
    still ``mode=ro`` with ``query_only`` enabled, so it cannot write; the
    primary audit connection retains both write barriers for all business SQL.
    """

    uri_path = quote(database.resolve().as_posix(), safe="/:\\\\")
    connection = sqlite3.connect(f"file:{uri_path}?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only = ON")
        return len(connection.execute("PRAGMA foreign_key_check").fetchall())
    finally:
        connection.close()


def _validate_schema(connection: sqlite3.Connection) -> None:
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    missing_tables = sorted(set(REQUIRED_COLUMNS) - tables)
    if missing_tables:
        raise ValueError("missing required tables: " + ", ".join(missing_tables))
    for table, required in REQUIRED_COLUMNS.items():
        missing_columns = sorted(required - _table_columns(connection, table))
        if missing_columns:
            raise ValueError(
                f"{table} is missing required columns: {', '.join(missing_columns)}"
            )


def _revision(connection: sqlite3.Connection) -> str:
    row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    if row is None or not str(row[0] or "").strip():
        raise ValueError("alembic_version is empty")
    return str(row[0]).strip()


def _index_inventory(connection: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for table in ("customers", "sales_orders", "sales_order_items"):
        indexes: list[dict[str, Any]] = []
        escaped_table = table.replace('"', '""')
        for row in connection.execute(f'PRAGMA index_list("{escaped_table}")'):
            name = str(row[1])
            escaped_name = name.replace('"', '""')
            columns = [
                str(info[2])
                for info in connection.execute(f'PRAGMA index_info("{escaped_name}")')
            ]
            indexes.append(
                {"name": name, "columns": columns, "unique": bool(row[2])}
            )
        result[table] = sorted(indexes, key=lambda entry: entry["name"])
    return result


def _anonymize_customer(customer_id: int, key: bytes) -> str:
    digest = hmac.new(key, str(customer_id).encode("ascii"), hashlib.sha256).hexdigest()
    return "CUS-" + digest[:12].upper()


def _percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[low]
    weight = position - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def _percentiles(values: Iterable[int | float | None]) -> dict[str, float | None]:
    concrete = [float(value) for value in values if value is not None]
    return {
        label: None if (value := _percentile(concrete, fraction)) is None else round(value, 4)
        for label, fraction in (
            ("p20", 0.20),
            ("p40", 0.40),
            ("p50", 0.50),
            ("p60", 0.60),
            ("p80", 0.80),
            ("p90", 0.90),
        )
    }


def _row_to_metric(
    row: sqlite3.Row,
    *,
    anonymization_key: bytes,
    include_amounts: bool,
) -> dict[str, Any]:
    valid_count = int(row["valid_master_order_count_all"] or 0)
    first_date = row["first_valid_order_date"]
    last_date = row["last_valid_order_date"]
    history_span_days = None
    if first_date and last_date:
        history_span_days = (
            date.fromisoformat(str(last_date)) - date.fromisoformat(str(first_date))
        ).days
    metric = {
        "customer": _anonymize_customer(int(row["customer_id"]), anonymization_key),
        "first_valid_order_date": first_date,
        "last_valid_order_date": last_date,
        "history_span_days": history_span_days,
        "valid_master_order_count_all": valid_count,
        "recency_days": row["recency_days"],
        "valid_order_days_90": int(row["valid_order_days_90"] or 0),
        "valid_master_orders_365": int(row["valid_master_orders_365"] or 0),
        "amount_complete_orders_365": int(row["amount_complete_orders_365"] or 0),
        "amount_incomplete_orders_365": int(row["amount_incomplete_orders_365"] or 0),
        "amount_completeness_rate_365": row["amount_completeness_rate_365"],
        "is_new_customer": bool(row["is_new_customer"]),
        "is_dormant_over_365": bool(row["is_dormant_over_365"]),
    }
    if include_amounts:
        metric["known_frozen_amount_365"] = row["known_frozen_amount_365"]
        metric["complete_frozen_amount_365"] = row["complete_frozen_amount_365"]
    else:
        metric["known_frozen_amount_365"] = None
        metric["complete_frozen_amount_365"] = None
    return metric


def _distribution(metrics: Sequence[dict[str, Any]], *, include_amounts: bool) -> dict[str, Any]:
    established = [
        metric
        for metric in metrics
        if not metric["is_new_customer"] and not metric["is_dormant_over_365"]
    ]
    amount_complete = [
        metric
        for metric in established
        if metric["valid_master_orders_365"] > 0
        and metric["amount_incomplete_orders_365"] == 0
    ]
    total_recent_orders = sum(metric["valid_master_orders_365"] for metric in metrics)
    complete_recent_orders = sum(metric["amount_complete_orders_365"] for metric in metrics)
    return {
        "active_customer_count": len(metrics),
        "new_customer_count": sum(bool(metric["is_new_customer"]) for metric in metrics),
        "dormant_or_no_valid_order_count": sum(
            bool(metric["is_dormant_over_365"]) for metric in metrics
        ),
        "established_recent_customer_count": len(established),
        "customers_with_incomplete_amount_365": sum(
            metric["amount_incomplete_orders_365"] > 0 for metric in metrics
        ),
        "order_amount_completeness_rate_365": (
            None
            if total_recent_orders == 0
            else round(complete_recent_orders / total_recent_orders, 6)
        ),
        "recency_days": _percentiles(
            metric["recency_days"] for metric in established
        ),
        "valid_order_days_90": _percentiles(
            metric["valid_order_days_90"] for metric in established
        ),
        "valid_master_orders_365": _percentiles(
            metric["valid_master_orders_365"] for metric in established
        ),
        "complete_frozen_amount_365": (
            _percentiles(metric["complete_frozen_amount_365"] for metric in amount_complete)
            if include_amounts
            else {key: None for key in ("p20", "p40", "p50", "p60", "p80", "p90")}
        ),
        "amount_distribution_customer_count": len(amount_complete) if include_amounts else 0,
    }


def _query_plan(connection: sqlite3.Connection, as_of: date) -> list[str]:
    return [
        str(row[3])
        for row in connection.execute(
            "EXPLAIN QUERY PLAN " + CORE_AGGREGATE_SQL,
            {"as_of": as_of.isoformat()},
        )
    ]


def _performance_runs(
    connection: sqlite3.Connection, *, as_of: date, iterations: int
) -> tuple[list[sqlite3.Row], dict[str, Any]]:
    timings: list[float] = []
    sql_statements: list[str] = []

    def trace(statement: str) -> None:
        normalized = statement.lstrip().upper()
        if normalized.startswith(("SELECT", "WITH")):
            sql_statements.append(statement)

    last_rows: list[sqlite3.Row] = []
    for _ in range(iterations):
        sql_statements.clear()
        connection.set_trace_callback(trace)
        started = time.perf_counter()
        last_rows = list(
            connection.execute(CORE_AGGREGATE_SQL, {"as_of": as_of.isoformat()})
        )
        elapsed_ms = (time.perf_counter() - started) * 1000
        connection.set_trace_callback(None)
        if len(sql_statements) != 1:
            raise RuntimeError(
                f"core aggregation must execute exactly one SQL statement, got {len(sql_statements)}"
            )
        timings.append(elapsed_ms)

    ordered = sorted(timings)
    p95_index = max(0, math.ceil(len(ordered) * 0.95) - 1)
    return last_rows, {
        "iterations": iterations,
        "sql_statements_per_iteration": 1,
        "response_ms": {
            "min": round(min(timings), 3),
            "median": round(statistics.median(timings), 3),
            "p95": round(ordered[p95_index], 3),
            "max": round(max(timings), 3),
        },
    }


def _threshold_candidates(distribution: dict[str, Any]) -> dict[str, Any]:
    """Return explainable percentile observations, never final heat tiers."""

    return {
        "status": "diagnostic_candidates_only_not_approved_thresholds",
        "fixed_rules": {
            "dormant_when_recency_days_gt": 365,
            "new_customer_when_valid_orders_lt": 3,
            "new_customer_when_history_span_days_lt": 30,
        },
        "recency_candidate_breaks": distribution["recency_days"],
        "frequency_90_day_candidate_breaks": distribution["valid_order_days_90"],
        "master_order_365_candidate_breaks": distribution["valid_master_orders_365"],
        "amount_tiebreak_candidate_breaks": distribution["complete_frozen_amount_365"],
        "note": (
            "P1-61B/C must not embed these observations as final weights or tiers "
            "until the owner confirms them on a current formal isolated copy."
        ),
    }


def run_audit(
    *,
    database: Path,
    as_of: date,
    expected_revision: str,
    expected_sha256: str | None,
    include_amounts: bool,
    amount_authorized: bool,
    iterations: int,
    anonymization_key: bytes,
    source_label: str,
) -> dict[str, Any]:
    """Run the read-only audit and return a JSON-serializable report."""

    if include_amounts and not amount_authorized:
        raise ValueError("--include-amounts requires --amount-authorized")
    if iterations < 1 or iterations > 20:
        raise ValueError("iterations must be between 1 and 20")
    if not source_label.strip():
        raise ValueError("source_label is required")

    before = _source_state(database)
    if expected_sha256 and before.sha256 != expected_sha256.strip().upper():
        raise ValueError(
            f"database SHA-256 mismatch: expected {expected_sha256.strip().upper()}, got {before.sha256}"
        )

    with connect_readonly(database) as connection:
        _validate_schema(connection)
        revision = _revision(connection)
        if revision != expected_revision:
            raise ValueError(
                f"database revision mismatch: expected {expected_revision}, got {revision}"
            )
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        foreign_key_error_count = _foreign_key_error_count(database)
        indexes = _index_inventory(connection)
        plan = _query_plan(connection, as_of)
        rows, performance = _performance_runs(
            connection, as_of=as_of, iterations=iterations
        )

    after = _source_state(database)
    if before != after:
        raise RuntimeError("source database or SQLite sidecars changed during read-only audit")
    if quick_check.lower() != "ok" or foreign_key_error_count:
        raise RuntimeError(
            f"isolated copy failed integrity gates: quick_check={quick_check}, "
            f"foreign_key_errors={foreign_key_error_count}"
        )

    metrics = [
        _row_to_metric(
            row,
            anonymization_key=anonymization_key,
            include_amounts=include_amounts,
        )
        for row in rows
    ]
    distribution = _distribution(metrics, include_amounts=include_amounts)
    has_customer_order_date_index = any(
        entry["columns"][:2] == ["customer_id", "order_date"]
        for entry in indexes["sales_orders"]
    )
    return {
        "contract_version": "P1-61A-v1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "as_of_date": as_of.isoformat(),
        "source": {
            "label": source_label,
            "database_revision": revision,
            "database_sha256_before": before.sha256,
            "database_sha256_after": after.sha256,
            "database_size_bytes": before.size,
            "query_only": True,
            "write_authorizer": True,
            "sidecars_unchanged": before.sidecars == after.sidecars,
            "quick_check": quick_check,
            "foreign_key_error_count": foreign_key_error_count,
        },
        "metric_contract": {
            "customer_population": "active customer masters only",
            "valid_order": (
                "formal sales_orders row with non-empty order_number, order_date <= as_of, "
                "and status not in dead/cancelled/canceled/void/voided/draft/test"
            ),
            "test_order_boundary": (
                "current schema has no is_test marker; formal sales_orders must not contain "
                "test rows because name/number heuristics are prohibited"
            ),
            "master_order_deduplication": "group by sales_orders.id before customer aggregation",
            "order_days_90": "distinct order_date in inclusive as_of-89..as_of window",
            "master_orders_365": "master orders in inclusive as_of-364..as_of window",
            "frozen_amount": (
                "sum persisted sales_order_items.subtotal only when every line has unit_price > 0"
            ),
            "missing_amount": (
                "any missing/non-positive line price makes complete_frozen_amount_365 null; "
                "known subtotal is reported separately and never substituted as zero"
            ),
            "new_customer": "1-2 valid master orders or valid-order history span < 30 days",
            "no_valid_order": "not new; separately dormant/no-valid-history",
            "dormant": "no valid order or recency_days > 365",
            "permission_boundary": (
                "P1-61B must apply customer scope and cost.view-equivalent amount permission "
                "before aggregation/result construction"
            ),
        },
        "amounts_included": include_amounts,
        "customers": metrics,
        "distribution": distribution,
        "threshold_candidates": _threshold_candidates(distribution),
        "performance": performance,
        "query_plan": plan,
        "indexes": indexes,
        "index_observation": {
            "has_customer_order_date_composite_index": has_customer_order_date_index,
            "recommendation": (
                "No migration in P1-61A.  Benchmark the future scoped/paged P1-61B query "
                "before considering a customer_id/order_date composite index."
            ),
        },
        "limitations": [
            "This stage does not define final heat weights, tier thresholds, labels, or colors.",
            "The current schema has no explicit test-order flag; formal-data governance is the fail-closed boundary.",
            "No API, UI, customer master, order, amount, status, or schema was changed.",
        ],
    }


def render_markdown(report: dict[str, Any]) -> str:
    distribution = report["distribution"]
    performance = report["performance"]
    source = report["source"]
    lines = [
        "# P1-61A 客户活跃热力只读分布诊断",
        "",
        "> 本报告只提供匿名分布与可解释指标候选，不是 P1-61B/C 的最终热度阈值或页面验收。",
        "",
        "## 数据门槛",
        "",
        f"- 来源标签：`{source['label']}`",
        f"- 数据时点：`{report['as_of_date']}`",
        f"- Alembic revision：`{source['database_revision']}`",
        f"- SHA-256 前后一致：`{source['database_sha256_before'] == source['database_sha256_after']}`",
        f"- SQLite `query_only` + 写操作 authorizer：`{source['query_only'] and source['write_authorizer']}`",
        f"- `quick_check={source['quick_check']}`；外键异常：`{source['foreign_key_error_count']}`",
        "",
        "## 指标口径",
        "",
        "- 有效订单：正式主单号非空、下单日不晚于统计日、状态不是 `dead/cancelled/canceled/void/voided/draft/test`。",
        "- 主单去重：先按 `sales_orders.id` 聚合；一张主单无论几条明细，频次只算一次。",
        "- 近 90 天下单天数：统计日及之前 89 天内，不重复的下单日期数。",
        "- 近 365 天订单数：统计日及之前 364 天内的有效主单数。",
        "- 冻结金额：使用保存于 `sales_order_items` 的历史单价/小计；任一行单价缺失或不大于 0，完整金额为 `null`，不能当 0。",
        "- 新客户：有效主单不足 3 张，或第一张到最近一张的历史跨度不足 30 天；从未有有效订单的客户单列为无有效历史。",
        "- 沉睡：无有效订单，或最近有效订单已超过 365 天。",
        "- 当前表结构没有明确 `is_test` 字段，因此禁止按客户名/单号猜测测试单；正式表不得混入测试单是进入 P1-61B 前的数据治理门槛。",
        "",
        "## 匿名分布",
        "",
        f"- 活跃客户主档：`{distribution['active_customer_count']}`",
        f"- 新客户：`{distribution['new_customer_count']}`",
        f"- 沉睡或无有效订单：`{distribution['dormant_or_no_valid_order_count']}`",
        f"- 可用于常规分位诊断的近期成熟客户：`{distribution['established_recent_customer_count']}`",
        f"- 近 365 天订单金额完整率：`{distribution['order_amount_completeness_rate_365']}`",
        f"- 有金额缺口的客户：`{distribution['customers_with_incomplete_amount_365']}`",
        "",
        "| 指标 | P20 | P40 | P50 | P60 | P80 | P90 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for label, key in (
        ("最近下单间隔（天）", "recency_days"),
        ("近 90 天下单天数", "valid_order_days_90"),
        ("近 365 天主单数", "valid_master_orders_365"),
        ("近 365 天完整冻结金额", "complete_frozen_amount_365"),
    ):
        values = distribution[key]
        lines.append(
            "| " + label + " | " + " | ".join(
                "-" if values[column] is None else str(values[column])
                for column in ("p20", "p40", "p50", "p60", "p80", "p90")
            ) + " |"
        )
    lines.extend(
        [
            "",
            "## 查询与性能",
            "",
            f"- 核心客户聚合 SQL：每次 `1` 条；共测 `{performance['iterations']}` 次。",
            f"- 响应时间（毫秒）：min `{performance['response_ms']['min']}`，median `{performance['response_ms']['median']}`，p95 `{performance['response_ms']['p95']}`，max `{performance['response_ms']['max']}`。",
            f"- 已存在客户+下单日复合索引：`{report['index_observation']['has_customer_order_date_composite_index']}`。",
            "- P1-61A 不新增索引或迁移；P1-61B 必须先把客户范围放进 SQL，再按真实分页查询复测后才能决定是否加索引。",
            "",
            "查询计划：",
            "",
        ]
    )
    lines.extend(f"- `{entry}`" for entry in report["query_plan"])
    lines.extend(
        [
            "",
            "## 阈值建议边界",
            "",
            "- 固定规则只保留任务书已确认的：最近订单超过 365 天为最低档；有效主单不足 3 张或历史不足 30 天为新客户。",
            "- 表中 P20/P40/P60/P80 仅是候选切点观察，不是最终热1～热5阈值。",
            "- 最终权重、阈值、色值必须等 P1-61C 人工原型验收，不得在 P1-61A 中写死。",
            "",
            "## 本阶段未做",
            "",
            "- 未新增客户热力 API、页面、颜色、权重或持久化配置。",
            "- 未修改客户、订单、金额、状态或数据库 schema。",
            "- 未连接或写入工厂正式数据库。",
            "",
        ]
    )
    return "\n".join(lines)


def _write_report(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def _validate_output_paths(
    database: Path, json_output: Path, markdown_output: Path
) -> tuple[Path, Path, Path]:
    resolved = tuple(
        path.expanduser().resolve()
        for path in (database, json_output, markdown_output)
    )
    if len(set(resolved)) != 3:
        raise ValueError("database, JSON output, and Markdown output must be distinct paths")
    for output in resolved[1:]:
        if _is_symlink_path(output):
            raise ValueError(f"symbolic-link report paths are refused: {output}")
    return resolved


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--json-output", required=True, type=Path)
    parser.add_argument("--markdown-output", required=True, type=Path)
    parser.add_argument("--source-label", required=True)
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    parser.add_argument("--expected-revision", default=EXPECTED_REVISION)
    parser.add_argument("--expected-sha256")
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--include-amounts", action="store_true")
    parser.add_argument("--amount-authorized", action="store_true")
    parser.add_argument(
        "--anonymization-key-env",
        default="P1_61A_ANONYMIZATION_KEY",
        help="environment variable holding the report-only HMAC key",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    key_text = os.environ.get(args.anonymization_key_env)
    anonymization_key = (
        key_text.encode("utf-8") if key_text else secrets.token_bytes(32)
    )
    if not key_text:
        print(
            f"warning: {args.anonymization_key_env} is unset; anonymous customer tokens "
            "will be unique to this run",
            file=sys.stderr,
        )
    try:
        database, json_output, markdown_output = _validate_output_paths(
            args.database, args.json_output, args.markdown_output
        )
        report = run_audit(
            database=database,
            as_of=args.as_of,
            expected_revision=args.expected_revision,
            expected_sha256=args.expected_sha256,
            include_amounts=args.include_amounts,
            amount_authorized=args.amount_authorized,
            iterations=args.iterations,
            anonymization_key=anonymization_key,
            source_label=args.source_label,
        )
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        print(f"P1-61A audit failed: {exc}", file=sys.stderr)
        return 2

    _write_report(
        json_output,
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    _write_report(markdown_output, render_markdown(report))
    print(
        json.dumps(
            {
                "status": "ok",
                "customers": report["distribution"]["active_customer_count"],
                "sql_statements_per_iteration": report["performance"]["sql_statements_per_iteration"],
                "median_ms": report["performance"]["response_ms"]["median"],
                "source_sha256_unchanged": (
                    report["source"]["database_sha256_before"]
                    == report["source"]["database_sha256_after"]
                ),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
