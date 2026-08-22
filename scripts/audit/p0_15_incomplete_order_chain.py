"""P0-15 unfinished-order chain audit for an explicit isolated SQLite copy.

There is deliberately no apply or repair mode.  The database is opened with
SQLite ``mode=ro``, ``PRAGMA query_only=ON`` and an authorizer that rejects
write/schema operations.  Only the three explicitly named report files may be
created.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import secrets
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import quote

from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models import Base
from app.services.incomplete_order_chain_audit import audit_incomplete_order_chains


SQLITE_HEADER = b"SQLite format 3\x00"
REQUIRED_TABLES = {
    "alembic_version",
    "sales_orders",
    "sales_order_items",
    "products",
    "sales_order_item_bom_components",
    "sales_order_item_external_components",
    "supplier_requisition_orders",
    "supplier_requisition_order_items",
    "material_requisition_items",
    "purchase_purpose_source_snapshots",
    "incoming_receipt_purpose_allocations",
    "incoming_receipt_purpose_reversals",
    "external_packaging_purchase_batches",
    "external_packaging_purchase_orders",
    "external_packaging_purchase_items",
    "external_packaging_purchase_cancellations",
    "external_packaging_receipt_items",
    "incoming_receipts",
    "incoming_receipt_items",
    "production_tasks",
    "production_completions",
    "inventory_lots",
    "finished_goods_inventory_details",
    "inventory_reservations",
    "inventory_movements",
    "sales_deliveries",
    "sales_delivery_items",
    "delivery_inventory_allocations",
    "finance_return_receipts",
    "finance_return_receipt_items",
    "finance_statements",
    "finance_statement_items",
    "finance_invoices",
    "finance_settlement_records",
}
REQUIRED_COLUMNS = {
    "alembic_version": {"version_num"},
    **{
        table: {column.name for column in Base.metadata.tables[table].columns}
        for table in REQUIRED_TABLES - {"alembic_version"}
    },
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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _sidecar_state(database: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for suffix in ("-journal", "-wal", "-shm"):
        candidate = Path(str(database) + suffix)
        exists = candidate.exists()
        result[suffix] = {
            "exists": exists,
            "size": candidate.stat().st_size if exists else None,
            "mtime_ns": candidate.stat().st_mtime_ns if exists else None,
        }
    return result


def _source_state(database: Path) -> dict[str, Any]:
    stat = database.stat()
    return {
        "sha256": _sha256(database),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sidecars": _sidecar_state(database),
    }


def _is_symlink_path(path: Path) -> bool:
    candidate = path.absolute()
    return any(
        parent.exists()
        and (
            parent.is_symlink()
            or (
                hasattr(parent, "is_junction")
                and parent.is_junction()
            )
        )
        for parent in (candidate, *candidate.parents)
    )


def _readonly_authorizer(
    action: int,
    _arg1: str | None,
    _arg2: str | None,
    _database_name: str | None,
    _trigger_name: str | None,
) -> int:
    return (
        sqlite3.SQLITE_DENY
        if action in WRITE_AUTHORIZER_ACTIONS
        else sqlite3.SQLITE_OK
    )


def _validate_database(database: Path) -> Path:
    requested = database.expanduser().absolute()
    if _is_symlink_path(requested):
        raise ValueError(f"symbolic-link database paths are refused: {requested}")
    resolved = requested.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError(f"database must be a regular file: {resolved}")
    with resolved.open("rb") as handle:
        if handle.read(len(SQLITE_HEADER)) != SQLITE_HEADER:
            raise ValueError(f"file is not an SQLite 3 database: {resolved}")
    return resolved


def _validate_outputs(database: Path, outputs: Sequence[Path]) -> list[Path]:
    requested = [path.expanduser().absolute() for path in outputs]
    for output in requested:
        if _is_symlink_path(output):
            raise ValueError(f"symbolic-link report paths are refused: {output}")
    resolved = [path.resolve(strict=False) for path in requested]
    if len(set(resolved)) != len(resolved):
        raise ValueError("JSON, CSV and Markdown report paths must be distinct")
    if database in resolved:
        raise ValueError("a report path must not overwrite the database")
    sidecars = {Path(str(database) + suffix) for suffix in ("-journal", "-wal", "-shm")}
    if any(output in sidecars for output in resolved):
        raise ValueError("a report path must not overwrite a SQLite sidecar")
    for output in resolved:
        if output.exists():
            raise ValueError(
                f"report paths must be new files and must not already exist: {output}"
            )
    return resolved


def _creator(database: Path):
    uri_path = quote(database.as_posix(), safe="/:\\")

    def connect() -> sqlite3.Connection:
        connection = sqlite3.connect(
            f"file:{uri_path}?mode=ro&immutable=1",
            uri=True,
            check_same_thread=False,
        )
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.set_authorizer(_readonly_authorizer)
        return connection

    return connect


def _table_names(db: Session) -> set[str]:
    return {
        str(row[0])
        for row in db.execute(
            text("SELECT name FROM sqlite_master WHERE type='table'")
        ).all()
    }


def _table_columns(db: Session, table: str) -> set[str]:
    escaped = table.replace('"', '""')
    return {
        str(row[1])
        for row in db.execute(text(f'PRAGMA table_info("{escaped}")')).all()
    }


def _render_markdown(report: dict) -> str:
    summary = report["summary"]
    source = report["source"]
    lines = [
        "# P0-15 未完成订单全链只读扫描报告",
        "",
        f"- 来源标签：`{source['label']}`",
        f"- 数据库 revision：`{source['revision']}`",
        f"- 扫描前 SHA-256：`{source['database_sha256_before']}`",
        f"- 扫描后 SHA-256：`{source['database_sha256_after']}`",
        f"- 数据库未变化：`{source['database_unchanged']}`",
        f"- quick_check：`{source['quick_check']}`",
        f"- 外键异常：`{source['foreign_key_errors']}`",
        f"- 未完成订单：`{report['scope']['unfinished_order_count']}`",
        f"- 未完成明细：`{report['scope']['unfinished_order_item_count']}`",
        f"- 异常/待复核/信息合计：`{summary['finding_count']}`",
        f"- 聚焦对象命中：`{summary['focus_finding_count']}`",
        f"- 聚焦对象工位命中：`{summary.get('focus_route_count', 0)}`",
        f"- 扫描覆盖完整：`{summary['scan_complete']}`",
        f"- 扫描耗时：`{source['elapsed_ms']}` ms",
        "",
        "## 能力边界",
        "",
        "- 本次扫描覆盖 P1-80 采购用途冻结、P1-81 收料用途分流与自动成品；扫描器仍只报告，不自动修复。",
        "- `legacy_unset` 历史正式来源继续按兼容口径读取；只有明确标记为 `frozen` 的来源才强制用途快照与自动分流守恒。",
        "- 片料备库必须进入半成品批次，不得生成当前订单成品；已撤销用途分配不计入当前有效累计。",
        "- P1-84 工位路由按明确任务、产品和组件事实实时评估；不会从名称、编码、备注、图纸或模具推断模切。",
        "- P0-14/P1-85/P1-86/P1-87 的地图、盘点、地址和地面货位由隔离回归契约保护；本扫描器不写位置主数据。",
        "- `RECEIVED_AWAITING_PRODUCTION_CONFIRMATION` 表示现行人工生产确认流程中的待办，不是库存缺失。",
        "- `ORDER_STATUS_SNAPSHOT_DIVERGENCE` 只提示保存状态与事实投影不同，禁止自动改正式状态。",
        "",
        "## 扫描覆盖",
        "",
        "| 能力 | 状态 | 原因 |",
        "|---|---|---|",
    ]
    for capability, detail in report["coverage"].items():
        lines.append(
            f"| `{capability}` | `{detail['status']}` | "
            f"{detail.get('reason') or '-'} |"
        )
    station_coverage = report["coverage"].get("workstation_membership") or {}
    lines.extend(
        [
            "",
            "## 工位路由统计",
            "",
            f"- 规则版本：`{station_coverage.get('rule_version') or '-'}`",
            f"- 待生产任务：`{station_coverage.get('eligible_task_count', 0)}`",
            f"- 印刷/开槽：`{(station_coverage.get('station_task_counts') or {}).get('printing', 0)}`",
            f"- 模切：`{(station_coverage.get('station_task_counts') or {}).get('die_cut', 0)}`",
            f"- 同时进入两个工位：`{station_coverage.get('dual_route_task_count', 0)}`",
            f"- 无工位任务：`{station_coverage.get('unrouted_task_count', 0)}`",
        ]
    )
    focused_routes = [
        row for row in report.get("workstation_routes") or [] if row.get("focus_match")
    ]
    if focused_routes:
        lines.extend(
            [
                "",
                "### 聚焦对象工位证据",
                "",
                "| 任务匿名标识 | 明细匿名标识 | 来源 | 明确模切 | 工位 | 命中字段 |",
                "|---|---|---|---|---|---|",
            ]
        )
        for route in focused_routes:
            focus = route.get("focus_match") or {}
            lines.append(
                f"| `{route['task_ref']}` | `{route['order_item_ref']}` | "
                f"`{route['source_kind']}` | `{route['die_cut_required']}` | "
                f"{' / '.join(route['stations']) or '-'} | "
                f"{' / '.join(focus.get('matched_fields') or []) or '-'} |"
            )
    lines.extend(
        [
        "",
        "## 分类统计",
        "",
        "| 编码 | 数量 |",
        "|---|---:|",
        ]
    )
    for code, count in summary["code_counts"].items():
        lines.append(f"| `{code}` | {count} |")
    lines.extend(
        [
            "",
            "## 明细",
            "",
            "| 等级 | 编码 | 订单匿名标识 | 明细匿名标识 | 摘要 | 聚焦命中 |",
            "|---|---|---|---|---|---|",
        ]
    )
    for finding in report["findings"]:
        focus = finding.get("focus_match") or {}
        focus_text = " / ".join(focus.get("matched_fields") or []) or "-"
        summary_text = str(finding["summary"]).replace("|", "\\|")
        lines.append(
            f"| {finding['severity']} | `{finding['code']}` | "
            f"`{finding['order_ref']}` | `{finding['order_item_ref'] or '-'}` | "
            f"{summary_text} | {focus_text} |"
        )
    lines.extend(
        [
            "",
            "## 处置边界",
            "",
            "- 本报告只读；未修复、未迁移、未刷新 legacy、未写正式业务表。",
            "- error 必须逐条追溯并拆分独立修复任务，不能由扫描器自动回写。",
            "- review 必须结合当前正式业务事实人工核对，不能仅凭保存状态批量改数据。",
            "",
        ]
    )
    return "\n".join(lines)


def _write_reports(report: dict, *, json_path: Path, csv_path: Path, markdown_path: Path) -> None:
    for path in (json_path, csv_path, markdown_path):
        path.parent.mkdir(parents=True, exist_ok=True)
        if _is_symlink_path(path.parent):
            raise ValueError(f"symbolic-link report directories are refused: {path.parent}")
    with json_path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        )
    with csv_path.open("x", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=(
                "severity",
                "code",
                "order_ref",
                "order_item_ref",
                "summary",
                "focus_match",
                "evidence_json",
            ),
        )
        writer.writeheader()
        for finding in report["findings"]:
            writer.writerow(
                {
                    "severity": finding["severity"],
                    "code": finding["code"],
                    "order_ref": finding["order_ref"],
                    "order_item_ref": finding["order_item_ref"],
                    "summary": finding["summary"],
                    "focus_match": json.dumps(
                        finding.get("focus_match"), ensure_ascii=False, sort_keys=True
                    ),
                    "evidence_json": json.dumps(
                        finding["evidence"], ensure_ascii=False, sort_keys=True
                    ),
                }
            )
    with markdown_path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(_render_markdown(report))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--json-output", required=True, type=Path)
    parser.add_argument("--csv-output", required=True, type=Path)
    parser.add_argument("--markdown-output", required=True, type=Path)
    parser.add_argument("--source-label", required=True)
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--focus", action="append", default=[])
    parser.add_argument(
        "--anonymization-key-env",
        default="P0_15_ANONYMIZATION_KEY",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        database = _validate_database(args.database)
        json_path, csv_path, markdown_path = _validate_outputs(
            database,
            (args.json_output, args.csv_output, args.markdown_output),
        )
        before = _source_state(database)
        if before["sha256"].casefold() != str(args.expected_sha256).strip().casefold():
            raise ValueError("database SHA-256 does not match --expected-sha256")
        key_text = os.environ.get(args.anonymization_key_env)
        key = key_text.encode("utf-8") if key_text else secrets.token_bytes(32)
        if not key_text:
            print(
                f"warning: {args.anonymization_key_env} is unset; anonymous references "
                "will be unique to this run",
                file=sys.stderr,
            )
        engine = create_engine("sqlite://", creator=_creator(database))
        query_count = 0

        @event.listens_for(engine, "before_cursor_execute")
        def _count_query(*_args) -> None:
            nonlocal query_count
            query_count += 1

        started = time.perf_counter()
        try:
            with Session(engine) as db:
                if int(db.execute(text("PRAGMA query_only")).scalar_one()) != 1:
                    raise RuntimeError("SQLite query_only is not enabled")
                missing_tables = sorted(REQUIRED_TABLES - _table_names(db))
                if missing_tables:
                    raise ValueError(
                        "database is missing required tables: " + ", ".join(missing_tables)
                    )
                for table, required_columns in REQUIRED_COLUMNS.items():
                    missing_columns = sorted(
                        required_columns - _table_columns(db, table)
                    )
                    if missing_columns:
                        raise ValueError(
                            f"{table} is missing required columns: "
                            + ", ".join(missing_columns)
                        )
                revision = str(
                    db.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                )
                if revision != args.expected_revision:
                    raise ValueError(
                        f"database revision {revision!r} does not match expected "
                        f"{args.expected_revision!r}"
                    )
                quick_check = str(db.execute(text("PRAGMA quick_check")).scalar_one())
                foreign_key_errors = len(
                    db.execute(text("PRAGMA foreign_key_check")).all()
                )
                if quick_check != "ok":
                    raise RuntimeError(f"SQLite quick_check failed: {quick_check}")
                if foreign_key_errors:
                    raise RuntimeError(
                        f"SQLite foreign_key_check found {foreign_key_errors} error(s)"
                    )
                report = audit_incomplete_order_chains(
                    db,
                    anonymization_key=key,
                    focus_terms=args.focus,
                )
        finally:
            engine.dispose()
        elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
        after = _source_state(database)
        unchanged = before == after
        if not unchanged:
            raise RuntimeError("read-only scan changed the database or a SQLite sidecar")
        report["source"] = {
            "label": args.source_label,
            "revision": revision,
            "database_sha256_before": before["sha256"],
            "database_sha256_after": after["sha256"],
            "database_unchanged": True,
            "quick_check": quick_check,
            "foreign_key_errors": foreign_key_errors,
            "query_count": query_count,
            "elapsed_ms": elapsed_ms,
        }
        _write_reports(
            report,
            json_path=json_path,
            csv_path=csv_path,
            markdown_path=markdown_path,
        )
        after_report_write = _source_state(database)
        if after_report_write != before:
            for output in (json_path, csv_path, markdown_path):
                output.unlink(missing_ok=True)
            raise RuntimeError(
                "database or a SQLite sidecar changed while report files were written"
            )
    except SQLAlchemyError as exc:
        print(
            "P0-15 audit failed: database schema/query contract failed "
            f"({exc.__class__.__name__})",
            file=sys.stderr,
        )
        return 2
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        print(f"P0-15 audit failed: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": (
                    "ok" if report["summary"]["scan_complete"] else "partial"
                ),
                "scan_complete": report["summary"]["scan_complete"],
                "coverage": report["coverage"],
                "orders": report["scope"]["unfinished_order_count"],
                "items": report["scope"]["unfinished_order_item_count"],
                "findings": report["summary"]["finding_count"],
                "focus_findings": report["summary"]["focus_finding_count"],
                "focus_routes": report["summary"].get("focus_route_count", 0),
                "database_unchanged": True,
                "json_output": str(json_path),
                "csv_output": str(csv_path),
                "markdown_output": str(markdown_path),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
