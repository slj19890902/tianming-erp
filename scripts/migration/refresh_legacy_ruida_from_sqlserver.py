"""Safely compare or append missing Ruida legacy rows from SQL Server.

The default mode is read-only dry-run. Database writes require --apply plus
explicit confirmation. Existing legacy rows are never updated by this script.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
from collections import Counter
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, NamedTuple

import pyodbc


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CANONICAL_SQLITE = PROJECT_ROOT / "data" / "carton_erp.sqlite3"
REPORT_DIR = PROJECT_ROOT / "docs" / "migration_reports"
BACKUP_DIR = PROJECT_ROOT / "data" / "backups"
DEFAULT_SQL_SERVER = r".\BOXERP"
DEFAULT_SQL_DATABASE = "BoxDB20_REPRO"
APPLY_CONFIRMATION = "APPLY_LEGACY_RUIDA_REFRESH"

TABLE_SPECS = {
    "orders": {
        "source": "Orders",
        "target": "legacy_ruida_orders",
        "source_key": "ID",
        "target_key": "legacy_order_id",
    },
    "items": {
        "source": "OrderXLs",
        "target": "legacy_ruida_order_items",
        "source_key": "ID",
        "target_key": "legacy_item_id",
    },
    "customers": {
        "source": "Customers",
        "target": "legacy_ruida_customers",
        "source_key": "ID",
        "target_key": "legacy_customer_id",
    },
}


class SafetyError(RuntimeError):
    """Raised when an apply-mode safety requirement is not satisfied."""


class KeyDiff(NamedTuple):
    source_only: tuple[int, ...]
    target_only: tuple[int, ...]
    source_duplicates: tuple[int, ...]
    target_duplicates: tuple[int, ...]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Dry-run by default. Compare BoxDB20_REPRO with SQLite legacy_ruida_* "
            "and optionally append only missing source IDs."
        ),
        epilog=(
            "Dry-run: python scripts/migration/refresh_legacy_ruida_from_sqlserver.py\n"
            "Apply to an isolated copy: python scripts/migration/"
            "refresh_legacy_ruida_from_sqlserver.py --target-sqlite "
            "data/work/legacy_refresh.sqlite3 --apply "
            f"--confirm-apply {APPLY_CONFIRMATION}\n"
            "Applying to data/carton_erp.sqlite3 additionally requires "
            "--allow-main-sandbox and must only occur after explicit authorization.\n"
            "Rollback: stop the ERP, verify the selected backup SHA-256, rename the "
            "failed target for evidence, copy the verified backup back to the exact "
            "target path, then run PRAGMA integrity_check and recount all five tables."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--sql-server", default=DEFAULT_SQL_SERVER)
    parser.add_argument("--sql-database", default=DEFAULT_SQL_DATABASE)
    parser.add_argument(
        "--target-sqlite",
        "--sqlite-path",
        dest="target_sqlite",
        type=Path,
        default=CANONICAL_SQLITE,
    )
    parser.add_argument("--report-dir", type=Path, default=REPORT_DIR)
    parser.add_argument("--backup-dir", type=Path, default=BACKUP_DIR)
    parser.add_argument("--apply", action="store_true", help="Enable append-only writes.")
    parser.add_argument(
        "--confirm-apply",
        help=f"Required with --apply; must equal {APPLY_CONFIRMATION!r}.",
    )
    parser.add_argument(
        "--allow-main-sandbox",
        action="store_true",
        help="Additional guard required to write data/carton_erp.sqlite3.",
    )
    parser.add_argument(
        "--refresh-customers",
        action="store_true",
        help="Append missing customers. With zero customer diff, customers are skipped.",
    )
    return parser


def resolved(path: Path) -> Path:
    return path.expanduser().resolve()


def validate_apply_safety(
    *,
    apply: bool,
    target_sqlite: Path,
    canonical_sqlite: Path,
    allow_main_sandbox: bool,
    confirm_apply: str | None,
) -> None:
    if not apply:
        return
    if confirm_apply != APPLY_CONFIRMATION:
        raise SafetyError(
            f"--apply requires --confirm-apply {APPLY_CONFIRMATION}"
        )
    if resolved(target_sqlite) == resolved(canonical_sqlite) and not allow_main_sandbox:
        raise SafetyError(
            "writing the canonical SQLite sandbox requires --allow-main-sandbox"
        )


def compute_missing_ids(source_ids: Iterable[int], target_ids: Iterable[int]) -> KeyDiff:
    source_list = list(source_ids)
    target_list = list(target_ids)
    source_counts = Counter(source_list)
    target_counts = Counter(target_list)
    source_set = set(source_list)
    target_set = set(target_list)
    return KeyDiff(
        source_only=tuple(sorted(source_set - target_set)),
        target_only=tuple(sorted(target_set - source_set)),
        source_duplicates=tuple(sorted(key for key, count in source_counts.items() if count > 1)),
        target_duplicates=tuple(sorted(key for key, count in target_counts.items() if count > 1)),
    )


def build_insert_sql(
    *, table: str, columns: tuple[str, ...], conflict_key: str
) -> str:
    column_sql = ", ".join(columns)
    placeholders = ", ".join("?" for _ in columns)
    return (
        f"INSERT INTO {table} ({column_sql}) VALUES ({placeholders}) "
        f"ON CONFLICT({conflict_key}) DO NOTHING"
    )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def sqlite_uri(path: Path, *, immutable: bool) -> str:
    normalized = path.resolve().as_posix()
    suffix = "?mode=ro&immutable=1" if immutable else "?mode=rw"
    return f"file:{normalized}{suffix}"


def open_sqlite_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(sqlite_uri(path, immutable=True), uri=True)
    connection.execute("PRAGMA query_only=ON")
    return connection


def open_sql_server(server: str, database: str) -> pyodbc.Connection:
    connection = pyodbc.connect(
        "DRIVER={ODBC Driver 17 for SQL Server};"
        f"SERVER={server};DATABASE={database};"
        "Trusted_Connection=yes;TrustServerCertificate=yes;",
        autocommit=False,
        timeout=15,
    )
    connection.cursor().execute("SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED")
    return connection


def require_sqlite_tables(connection: sqlite3.Connection) -> None:
    expected = {spec["target"] for spec in TABLE_SPECS.values()}
    found = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    missing = sorted(expected - found)
    if missing:
        raise SafetyError(f"SQLite is missing required tables: {', '.join(missing)}")


def require_sql_server_tables(connection: pyodbc.Connection) -> None:
    cursor = connection.cursor()
    missing = []
    for spec in TABLE_SPECS.values():
        cursor.execute(
            "SELECT COUNT_BIG(*) FROM sys.tables WHERE name=?",
            spec["source"],
        )
        if cursor.fetchone()[0] != 1:
            missing.append(spec["source"])
    if missing:
        raise SafetyError(f"SQL Server is missing required tables: {', '.join(missing)}")


def sql_server_rows(
    connection: pyodbc.Connection, table: str
) -> list[dict[str, Any]]:
    cursor = connection.cursor()
    cursor.execute(f"SELECT * FROM dbo.[{table}]")
    columns = [description[0] for description in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def target_ids(
    connection: sqlite3.Connection, table: str, key: str
) -> list[int]:
    return [row[0] for row in connection.execute(f"SELECT {key} FROM {table}")]


def table_counts(connection: sqlite3.Connection) -> dict[str, int]:
    tables = [spec["target"] for spec in TABLE_SPECS.values()]
    tables.extend(["sales_orders", "sales_order_items"])
    counts: dict[str, int] = {}
    for table in tables:
        exists = connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()[0]
        counts[table] = (
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            if exists
            else -1
        )
    return counts


def file_snapshot(path: Path, connection: sqlite3.Connection) -> dict[str, Any]:
    stat = path.stat()
    return {
        "path": str(path.resolve()),
        "size_bytes": stat.st_size,
        "modified_at": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(),
        "sha256": sha256(path),
        "integrity_check": connection.execute("PRAGMA integrity_check").fetchone()[0],
        "table_counts": table_counts(connection),
    }


def clean(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return value


def legacy_text(value: Any) -> str | None:
    value = clean(value)
    if value is None:
        return None
    if isinstance(value, bool):
        return "True" if value else "False"
    return str(value)


def legacy_datetime(value: Any) -> str | None:
    value = clean(value)
    if value is None:
        return None
    if isinstance(value, datetime):
        return (
            f"{value.year}/{value.month}/{value.day} "
            f"{value.hour}:{value.minute:02d}:{value.second:02d}"
        )
    if isinstance(value, date):
        return f"{value.year}/{value.month}/{value.day} 0:00:00"
    return str(value)


def json_safe(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat(sep=" ")
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return {"binary_bytes": len(value)}
    return value


def source_json(row: dict[str, Any]) -> str:
    return json.dumps(
        {key: json_safe(value) for key, value in row.items()},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def sqlite_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, memoryview):
        return bytes(value)
    return value


def map_customer(row: dict[str, Any], batch_time: str) -> dict[str, Any]:
    fields = {
        "legacy_customer_id": row["ID"],
        "customer_id": None,
        "customer_code": clean(row.get("编码")),
        "short_name": clean(row.get("简称")),
        "customer_name": clean(row.get("全称")),
        "phone": clean(row.get("电话")),
        "mobile": clean(row.get("手机")),
        "fax": clean(row.get("传真")),
        "contact_person": clean(row.get("联系人")),
        "website": clean(row.get("网址")),
        "address": clean(row.get("地址")),
        "bank_name": clean(row.get("开户银行")),
        "bank_account": clean(row.get("银行账号")),
        "tax_id": clean(row.get("税务登记证号")),
        "settlement_method": clean(row.get("结算方式")),
        "currency": clean(row.get("币别")),
        "opening_receivable": row.get("期初应收"),
        "credit_limit": row.get("信用额度"),
        "discount": clean(row.get("折扣")),
        "salesperson": clean(row.get("业务员")),
        "source_updated_at": legacy_datetime(row.get("opTime")),
        "address2": clean(row.get("address2")),
        "address3": clean(row.get("address3")),
        "address_add": clean(row.get("addressAdd")),
        "payment_term_days": row.get("收款账期"),
        "payment_due_reminder_days": row.get("收款到期提醒"),
        "wechat": clean(row.get("微信")),
        "delivery_distance": row.get("送货距离"),
        "address4": clean(row.get("address4")),
        "address5": clean(row.get("address5")),
        "address6": clean(row.get("address6")),
        "address7": clean(row.get("address7")),
        "price_discount_flag": row.get("打折"),
        "is_hidden": legacy_text(row.get("isHide")),
        "invoice_enabled": legacy_text(row.get("是否开票")),
        "gross_margin_limit": row.get("毛利限制"),
        "gross_margin_blocked": legacy_text(row.get("毛利禁止")),
        "source_json": source_json(row),
        "imported_at": batch_time,
    }
    return fields


def legacy_customer_link(
    connection: sqlite3.Connection, legacy_customer_id: int | None
) -> int | None:
    if legacy_customer_id is None:
        return None
    row = connection.execute(
        "SELECT customer_id FROM legacy_ruida_customers WHERE legacy_customer_id=?",
        (legacy_customer_id,),
    ).fetchone()
    return row[0] if row else None


def map_order(
    row: dict[str, Any], batch_time: str, customer_id: int | None
) -> dict[str, Any]:
    return {
        "legacy_order_id": row["ID"],
        "customer_id": customer_id,
        "customer_legacy_id": row.get("客户_FK"),
        "customer_name": clean(row.get("客户名称")),
        "computer_order_no": clean(row.get("电脑单号")),
        "po": clean(row.get("PO")),
        "approved": legacy_text(row.get("是否通过审核")),
        "approved_at": legacy_datetime(row.get("审核时间")),
        "approved_by": clean(row.get("审核人")),
        "image_path": None,
        "remark": clean(row.get("备注")),
        "source_updated_at": legacy_datetime(row.get("opTime")),
        "source_json": source_json(row),
        "imported_at": batch_time,
    }


def order_customer_link(
    connection: sqlite3.Connection, legacy_order_id: int | None
) -> int | None:
    if legacy_order_id is None:
        return None
    row = connection.execute(
        "SELECT customer_id FROM legacy_ruida_orders WHERE legacy_order_id=?",
        (legacy_order_id,),
    ).fetchone()
    return row[0] if row else None


def map_item(
    row: dict[str, Any], batch_time: str, customer_id: int | None
) -> dict[str, Any]:
    return {
        "legacy_item_id": row["ID"],
        "legacy_order_id": row.get("Order_FK"),
        "customer_id": customer_id,
        "product_archive_id": None,
        "supplier_id": None,
        "customer_order_no": clean(row.get("客户单号")),
        "project_no": legacy_text(row.get("专案编号")),
        "purchase_batch_no": legacy_text(row.get("采购批号")),
        "production_batch_no": legacy_text(row.get("生产批号")),
        "spec_model": clean(row.get("规格型号")),
        "style_no": clean(row.get("款号")),
        "box_type": clean(row.get("箱型")),
        "legacy_product_id": row.get("箱类报价_FK"),
        "unit": clean(row.get("单位")),
        "material": clean(row.get("材质")),
        "supplier_quote_id": row.get("供方报价_FK"),
        "length_mm": row.get("订单长"),
        "width_mm": row.get("订单宽"),
        "height_mm": row.get("订单高"),
        "size_note": clean(row.get("教尺")),
        "delivery_date": legacy_datetime(row.get("交货日期")),
        "order_date": legacy_datetime(row.get("下单日期")),
        "order_quantity": row.get("订单数量"),
        "unit_price": row.get("单价"),
        "amount": row.get("金额"),
        "supplier_name": clean(row.get("供方")),
        "board_length": row.get("纸长"),
        "board_width": row.get("纸宽"),
        "web_width_used": row.get("耗用门幅"),
        "board_piece_qty": row.get("纸片数量"),
        "print_layout": clean(row.get("版面")),
        "package_rule": clean(row.get("打包")),
        "nail_count": clean(row.get("打钉数")),
        "nail_type": clean(row.get("钉类")),
        "color": clean(row.get("颜色")),
        "web_width_multiple": legacy_text(row.get("门幅倍数")),
        "join_method": clean(row.get("结合")),
        "split_crease": legacy_text(row.get("分压")),
        "laminating_glue": legacy_text(row.get("裱糊")),
        "print_required": legacy_text(row.get("印刷")),
        "slotting_required": legacy_text(row.get("开槽")),
        "die_cut_required": legacy_text(row.get("模切")),
        "image_type": clean(row.get("图片类型")),
        "is_two_piece": legacy_text(row.get("is两片成型")),
        "created_by": clean(row.get("开单人")),
        "supplier_quote": row.get("供方报价"),
        "remark": clean(row.get("备注")),
        "front_mark": clean(row.get("正麦")),
        "back_mark": clean(row.get("反麦")),
        "purchase_rough_piece": legacy_text(row.get("采购毛片")),
        "source_updated_at": legacy_datetime(row.get("opTime")),
        "is_main_item": legacy_text(row.get("是否主单")),
        "is_accessory": legacy_text(row.get("是否配件")),
        "is_closed": legacy_text(row.get("是否结单")),
        "main_item_no": clean(row.get("主单单号")),
        "drawing_no": clean(row.get("drawNumber")),
        "die_cut_no": clean(row.get("toolNumber")),
        "print_no": clean(row.get("printNumber")),
        "rough_piece_amount": row.get("毛片金额"),
        "square_price": row.get("平方价"),
        "delivery_note": clean(row.get("送货备注")),
        "material_normalized": clean(row.get("材质_处理后")),
        "product_note": clean(row.get("ProductNote")),
        "is_urgent": legacy_text(row.get("是否加急")),
        "print_slotting": legacy_text(row.get("印刷开槽")),
        "print_die_cut": legacy_text(row.get("印刷模切")),
        "linkage_line": legacy_text(row.get("联动线")),
        "source_json": source_json(row),
        "imported_at": batch_time,
    }


def append_rows(
    connection: sqlite3.Connection,
    *,
    table: str,
    conflict_key: str,
    rows: list[dict[str, Any]],
) -> int:
    if not rows:
        return 0
    columns = tuple(rows[0].keys())
    sql = build_insert_sql(table=table, columns=columns, conflict_key=conflict_key)
    before = connection.total_changes
    connection.executemany(
        sql,
        [
            tuple(sqlite_value(row[column]) for column in columns)
            for row in rows
        ],
    )
    return connection.total_changes - before


def verified_backup(target: Path, backup_dir: Path, timestamp: str) -> dict[str, Any]:
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"carton_erp_before_legacy_refresh_{timestamp}.sqlite3"
    if backup.exists():
        raise SafetyError(f"backup already exists: {backup}")
    shutil.copy2(target, backup)
    source_hash = sha256(target)
    backup_hash = sha256(backup)
    if source_hash != backup_hash or target.stat().st_size != backup.stat().st_size:
        raise SafetyError("backup size or SHA-256 verification failed")
    with open_sqlite_read_only(backup) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise SafetyError(f"backup integrity_check failed: {integrity}")
    return {
        "path": str(backup.resolve()),
        "size_bytes": backup.stat().st_size,
        "sha256": backup_hash,
        "integrity_check": integrity,
    }


def write_report(report_dir: Path, mode: str, timestamp: str, payload: dict[str, Any]) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"LEGACY_REFRESH_{mode}_{timestamp}.md"
    title = "Dry-run" if mode == "DRY_RUN" else "Apply"
    path.write_text(
        "\n".join(
            [
                f"# Legacy Ruida Refresh {title} Report",
                "",
                f"- Batch ID: `{payload['batch_id']}`",
                f"- Mode: `{payload['mode']}`",
                f"- Database modified: `{'yes' if payload['database_modified'] else 'no'}`",
                f"- SQL Server: `{payload['sql_server']}`",
                f"- SQLite: `{payload['sqlite']}`",
                "",
                "## Complete log",
                "",
                "```json",
                json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                "```",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def run(args: argparse.Namespace) -> dict[str, Any]:
    target = resolved(args.target_sqlite)
    canonical = resolved(CANONICAL_SQLITE)
    if not target.is_file():
        raise SafetyError(f"target SQLite file does not exist: {target}")
    if args.sql_database != DEFAULT_SQL_DATABASE:
        raise SafetyError(
            f"source database must be {DEFAULT_SQL_DATABASE}, got {args.sql_database}"
        )
    validate_apply_safety(
        apply=args.apply,
        target_sqlite=target,
        canonical_sqlite=canonical,
        allow_main_sandbox=args.allow_main_sandbox,
        confirm_apply=args.confirm_apply,
    )

    started = datetime.now().astimezone()
    timestamp = started.strftime("%Y%m%d_%H%M%S")
    batch_id = f"legacy-ruida-{timestamp}"
    sql_connection = open_sql_server(args.sql_server, args.sql_database)
    try:
        require_sql_server_tables(sql_connection)
        source_rows = {
            name: sql_server_rows(sql_connection, spec["source"])
            for name, spec in TABLE_SPECS.items()
        }
        with open_sqlite_read_only(target) as sqlite_read:
            require_sqlite_tables(sqlite_read)
            before = file_snapshot(target, sqlite_read)
            diffs = {
                name: compute_missing_ids(
                    (row[spec["source_key"]] for row in source_rows[name]),
                    target_ids(sqlite_read, spec["target"], spec["target_key"]),
                )
                for name, spec in TABLE_SPECS.items()
            }
        sql_connection.rollback()

        expected_adds = {
            "orders": len(diffs["orders"].source_only),
            "items": len(diffs["items"].source_only),
            "customers": (
                len(diffs["customers"].source_only) if args.refresh_customers else 0
            ),
        }
        payload: dict[str, Any] = {
            "batch_id": batch_id,
            "started_at": started.isoformat(),
            "mode": "apply" if args.apply else "dry-run",
            "database_modified": False,
            "sql_server": f"{args.sql_server}/{args.sql_database}",
            "sqlite": str(target),
            "before": before,
            "source_counts": {name: len(rows) for name, rows in source_rows.items()},
            "diffs": {
                name: {
                    "source_only_count": len(diff.source_only),
                    "target_only_count": len(diff.target_only),
                    "source_duplicate_keys": list(diff.source_duplicates),
                    "target_duplicate_keys": list(diff.target_duplicates),
                    "source_only_id_min": min(diff.source_only) if diff.source_only else None,
                    "source_only_id_max": max(diff.source_only) if diff.source_only else None,
                }
                for name, diff in diffs.items()
            },
            "planned_inserts": expected_adds,
            "customer_policy": (
                "append missing customers"
                if args.refresh_customers
                else "skip customers unless --refresh-customers is supplied"
            ),
            "existing_row_policy": "report only; never update existing legacy rows",
        }

        if not args.apply:
            return payload

        if any(diff.source_duplicates or diff.target_duplicates for diff in diffs.values()):
            raise SafetyError("duplicate source or target id detected; apply aborted")
        if diffs["customers"].source_only and not args.refresh_customers:
            raise SafetyError(
                "missing customers detected; rerun with --refresh-customers after review"
            )

        backup = verified_backup(target, resolved(args.backup_dir), timestamp)
        payload["backup"] = backup
        batch_time = started.strftime("%Y-%m-%d %H:%M:%S")
        source_by_id = {
            name: {row[spec["source_key"]]: row for row in source_rows[name]}
            for name, spec in TABLE_SPECS.items()
        }
        write_connection = sqlite3.connect(sqlite_uri(target, immutable=False), uri=True)
        try:
            write_connection.execute("PRAGMA foreign_keys=ON")
            write_connection.execute("BEGIN IMMEDIATE")

            customer_rows = []
            if args.refresh_customers:
                customer_rows = [
                    map_customer(source_by_id["customers"][key], batch_time)
                    for key in diffs["customers"].source_only
                ]
            inserted_customers = append_rows(
                write_connection,
                table="legacy_ruida_customers",
                conflict_key="legacy_customer_id",
                rows=customer_rows,
            )

            order_rows = []
            for key in diffs["orders"].source_only:
                source = source_by_id["orders"][key]
                local_customer_id = legacy_customer_link(
                    write_connection, source.get("客户_FK")
                )
                order_rows.append(map_order(source, batch_time, local_customer_id))
            inserted_orders = append_rows(
                write_connection,
                table="legacy_ruida_orders",
                conflict_key="legacy_order_id",
                rows=order_rows,
            )

            item_rows = []
            for key in diffs["items"].source_only:
                source = source_by_id["items"][key]
                local_customer_id = order_customer_link(
                    write_connection, source.get("Order_FK")
                )
                item_rows.append(map_item(source, batch_time, local_customer_id))
            inserted_items = append_rows(
                write_connection,
                table="legacy_ruida_order_items",
                conflict_key="legacy_item_id",
                rows=item_rows,
            )

            inserted = {
                "orders": inserted_orders,
                "items": inserted_items,
                "customers": inserted_customers,
            }
            if inserted != expected_adds:
                raise SafetyError(
                    f"insert counts do not match plan: planned={expected_adds}, actual={inserted}"
                )
            quick_check = write_connection.execute("PRAGMA quick_check").fetchone()[0]
            if quick_check != "ok":
                raise SafetyError(f"pre-commit quick_check failed: {quick_check}")
            write_connection.commit()
            payload["inserted"] = inserted
            payload["database_modified"] = any(inserted.values())
        except Exception:
            write_connection.rollback()
            raise
        finally:
            write_connection.close()

        with open_sqlite_read_only(target) as sqlite_after:
            after = file_snapshot(target, sqlite_after)
            require_sqlite_tables(sqlite_after)
        payload["after"] = after
        if after["integrity_check"] != "ok":
            raise SafetyError(
                "post-write integrity_check failed; stop and restore the verified backup"
            )
        for name, spec in TABLE_SPECS.items():
            expected_count = before["table_counts"][spec["target"]] + expected_adds[name]
            actual_count = after["table_counts"][spec["target"]]
            if actual_count != expected_count:
                raise SafetyError(
                    f"post-write count mismatch for {spec['target']}: "
                    f"expected {expected_count}, got {actual_count}"
                )
        return payload
    finally:
        try:
            sql_connection.rollback()
        finally:
            sql_connection.close()


def main() -> int:
    args = build_parser().parse_args()
    timestamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    try:
        payload = run(args)
        timestamp = payload["batch_id"].removeprefix("legacy-ruida-")
        mode = "APPLY" if args.apply else "DRY_RUN"
        report = write_report(resolved(args.report_dir), mode, timestamp, payload)
        print(json.dumps(payload["planned_inserts"], ensure_ascii=False))
        print(f"mode: {payload['mode']}")
        print(f"database modified: {payload['database_modified']}")
        print(f"report: {report}")
        return 0
    except Exception as exc:
        failure = {
            "batch_id": f"legacy-ruida-{timestamp}",
            "mode": "apply" if args.apply else "dry-run",
            "database_modified": False,
            "error": f"{type(exc).__name__}: {exc}",
            "sql_server": f"{args.sql_server}/{args.sql_database}",
            "sqlite": str(resolved(args.target_sqlite)),
        }
        report = write_report(
            resolved(args.report_dir), "FAILED", timestamp, failure
        )
        print(f"ERROR: {exc}", file=sys.stderr)
        print(f"failure report: {report}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
