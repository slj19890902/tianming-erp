from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable

from openpyxl import Workbook


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import load_settings
from app.core.database import backup_to_nas


PROTECTED_TIANHUA_TABLES = (
    "tianhua_pre_delivery_import_batches",
    "tianhua_pre_delivery_import_items",
    "tianhua_pre_delivery_drafts",
    "tianhua_pre_delivery_draft_items",
)
CORE_TABLES = (
    "sales_orders",
    "sales_order_items",
    "sales_deliveries",
    "sales_delivery_items",
    "finance_return_receipts",
    "finance_return_receipt_items",
    "finance_statements",
    "finance_statement_items",
    "material_requisitions",
    "material_requisition_items",
    "supplier_requisition_orders",
    "supplier_requisition_order_items",
    *PROTECTED_TIANHUA_TABLES,
)


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def _count(connection: sqlite3.Connection, table: str) -> int:
    return int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])


def _rows(connection: sqlite3.Connection, sql: str, params=()) -> list[dict]:
    cursor = connection.execute(sql, params)
    names = [item[0] for item in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def analyze(connection: sqlite3.Connection) -> dict:
    tables = _tables(connection)
    order_ids = [
        int(row[0])
        for row in connection.execute(
            "SELECT id FROM sales_orders "
            "WHERE UPPER(order_number) LIKE '%RUIDA%' ORDER BY id"
        )
    ]
    item_ids = [
        int(row[0])
        for row in connection.execute(
            "SELECT id FROM sales_order_items "
            "WHERE order_id IN (SELECT id FROM sales_orders "
            "WHERE UPPER(order_number) LIKE '%RUIDA%') ORDER BY id"
        )
    ]
    before_counts = {
        table: _count(connection, table)
        for table in CORE_TABLES
        if table in tables
    }

    associations: dict[str, int] = {}
    queries = {
        "sales_delivery_items": """
            SELECT COUNT(*) FROM sales_delivery_items
            WHERE order_item_id IN (
                SELECT id FROM sales_order_items WHERE order_id IN (
                    SELECT id FROM sales_orders
                    WHERE UPPER(order_number) LIKE '%RUIDA%'
                )
            )
        """,
        "finance_return_receipt_items": """
            SELECT COUNT(*) FROM finance_return_receipt_items
            WHERE delivery_item_id IN (
                SELECT id FROM sales_delivery_items
                WHERE order_item_id IN (
                    SELECT id FROM sales_order_items WHERE order_id IN (
                        SELECT id FROM sales_orders
                        WHERE UPPER(order_number) LIKE '%RUIDA%'
                    )
                )
            )
        """,
        "finance_return_receipts": """
            SELECT COUNT(*) FROM finance_return_receipts
            WHERE id IN (
                SELECT return_receipt_id FROM finance_return_receipt_items
                WHERE delivery_item_id IN (
                    SELECT id FROM sales_delivery_items
                    WHERE order_item_id IN (
                        SELECT id FROM sales_order_items WHERE order_id IN (
                            SELECT id FROM sales_orders
                            WHERE UPPER(order_number) LIKE '%RUIDA%'
                        )
                    )
                )
            )
        """,
        "finance_statement_items": """
            SELECT COUNT(*) FROM finance_statement_items
            WHERE return_receipt_item_id IN (
                SELECT id FROM finance_return_receipt_items
                WHERE delivery_item_id IN (
                    SELECT id FROM sales_delivery_items
                    WHERE order_item_id IN (
                        SELECT id FROM sales_order_items WHERE order_id IN (
                            SELECT id FROM sales_orders
                            WHERE UPPER(order_number) LIKE '%RUIDA%'
                        )
                    )
                )
            )
        """,
        "finance_statements": """
            SELECT COUNT(*) FROM finance_statements
            WHERE id IN (
                SELECT statement_id FROM finance_statement_items
                WHERE return_receipt_item_id IN (
                    SELECT id FROM finance_return_receipt_items
                    WHERE delivery_item_id IN (
                        SELECT id FROM sales_delivery_items
                        WHERE order_item_id IN (
                            SELECT id FROM sales_order_items WHERE order_id IN (
                                SELECT id FROM sales_orders
                                WHERE UPPER(order_number) LIKE '%RUIDA%'
                            )
                        )
                    )
                )
            )
        """,
        "material_requisition_items": """
            SELECT COUNT(*) FROM material_requisition_items
            WHERE order_item_id IN (
                SELECT id FROM sales_order_items WHERE order_id IN (
                    SELECT id FROM sales_orders
                    WHERE UPPER(order_number) LIKE '%RUIDA%'
                )
            )
        """,
        "supplier_requisition_order_items": """
            SELECT COUNT(*) FROM supplier_requisition_order_items
            WHERE order_item_id IN (
                SELECT id FROM sales_order_items WHERE order_id IN (
                    SELECT id FROM sales_orders
                    WHERE UPPER(order_number) LIKE '%RUIDA%'
                )
            )
        """,
        "tianhua_pre_delivery_import_items": """
            SELECT COUNT(*) FROM tianhua_pre_delivery_import_items
            WHERE order_item_id IN (
                SELECT id FROM sales_order_items WHERE order_id IN (
                    SELECT id FROM sales_orders
                    WHERE UPPER(order_number) LIKE '%RUIDA%'
                )
            )
        """,
        "tianhua_pre_delivery_draft_items": """
            SELECT COUNT(*) FROM tianhua_pre_delivery_draft_items
            WHERE order_item_id IN (
                SELECT id FROM sales_order_items WHERE order_id IN (
                    SELECT id FROM sales_orders
                    WHERE UPPER(order_number) LIKE '%RUIDA%'
                )
            )
        """,
        "migration_ruida_sales_order_map": """
            SELECT COUNT(*) FROM migration_ruida_sales_order_map
            WHERE sales_order_id IN (
                SELECT id FROM sales_orders
                WHERE UPPER(order_number) LIKE '%RUIDA%'
            )
        """,
        "migration_ruida_sales_item_map": """
            SELECT COUNT(*) FROM migration_ruida_sales_item_map
            WHERE sales_order_item_id IN (
                SELECT id FROM sales_order_items WHERE order_id IN (
                    SELECT id FROM sales_orders
                    WHERE UPPER(order_number) LIKE '%RUIDA%'
                )
            )
        """,
        "migration_entity_map": """
            SELECT COUNT(*) FROM migration_entity_map
            WHERE (
                LOWER(target_table)='sales_orders'
                AND target_id IN (
                    SELECT id FROM sales_orders
                    WHERE UPPER(order_number) LIKE '%RUIDA%'
                )
            ) OR (
                LOWER(target_table)='sales_order_items'
                AND target_id IN (
                    SELECT id FROM sales_order_items WHERE order_id IN (
                        SELECT id FROM sales_orders
                        WHERE UPPER(order_number) LIKE '%RUIDA%'
                    )
                )
            )
        """,
    }
    for table, sql in queries.items():
        associations[table] = (
            int(connection.execute(sql).fetchone()[0]) if table in tables else 0
        )

    orders = _rows(
        connection,
        """
        SELECT id, order_number, customer_id, customer_po, order_date,
               delivery_date, status, payment_status, total_amount, created_at
        FROM sales_orders
        WHERE UPPER(order_number) LIKE '%RUIDA%'
        ORDER BY id
        """,
    )
    items = _rows(
        connection,
        """
        SELECT i.id, i.order_id, o.order_number, i.product_id,
               i.item_order_number, i.snapshot_product_code,
               i.snapshot_product_name, i.quantity, i.delivered_quantity,
               i.material_status, i.requisition_status,
               i.inventory_deducted_qty
        FROM sales_order_items i
        JOIN sales_orders o ON o.id=i.order_id
        WHERE UPPER(o.order_number) LIKE '%RUIDA%'
        ORDER BY i.id
        """,
    )
    keep_candidates = _rows(
        connection,
        """
        SELECT id, order_number, customer_po, order_date, delivery_date,
               status, created_at
        FROM sales_orders
        WHERE UPPER(order_number) NOT LIKE '%RUIDA%'
        ORDER BY created_at DESC, id DESC
        LIMIT 3
        """,
    )
    estimated_counts = dict(before_counts)
    estimated_counts["sales_orders"] = before_counts["sales_orders"] - len(order_ids)
    estimated_counts["sales_order_items"] = (
        before_counts["sales_order_items"] - len(item_ids)
    )
    for table in (
        "sales_delivery_items",
        "material_requisition_items",
        "supplier_requisition_order_items",
    ):
        if table in estimated_counts:
            estimated_counts[table] -= associations[table]
    return {
        "order_ids": order_ids,
        "item_ids": item_ids,
        "orders": orders,
        "items": items,
        "keep_candidates": keep_candidates,
        "before_counts": before_counts,
        "estimated_counts": estimated_counts,
        "associations": associations,
        "integrity_check": connection.execute(
            "PRAGMA integrity_check"
        ).fetchone()[0],
        "foreign_key_violations": len(
            connection.execute("PRAGMA foreign_key_check").fetchall()
        ),
    }


def _append_sheet(workbook: Workbook, title: str, rows: Iterable[dict]) -> None:
    sheet = workbook.create_sheet(title)
    values = list(rows)
    if not values:
        sheet.append(["无记录"])
        return
    headers = list(values[0])
    sheet.append(headers)
    for row in values:
        sheet.append([row.get(header) for header in headers])
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions


def export_list(result: dict, path: Path) -> None:
    workbook = Workbook()
    workbook.remove(workbook.active)
    _append_sheet(workbook, "拟删除订单", result["orders"])
    _append_sheet(workbook, "拟删除订单明细", result["items"])
    _append_sheet(workbook, "保留订单候选", result["keep_candidates"])
    _append_sheet(
        workbook,
        "关联影响",
        [
            {"table": table, "affected_rows": count}
            for table, count in result["associations"].items()
        ],
    )
    _append_sheet(
        workbook,
        "核心表数量",
        [
            {
                "table": table,
                "before": count,
                "estimated_after": result["estimated_counts"].get(table, count),
            }
            for table, count in result["before_counts"].items()
        ],
    )
    _append_sheet(
        workbook,
        "风险提示",
        [
            {
                "risk": "天华导入引用",
                "detail": (
                    f"{result['associations']['tianhua_pre_delivery_import_items']} "
                    "条导入记录将保留，但解除 RUIDA 订单引用并标记未匹配"
                ),
            },
            {
                "risk": "天华草稿引用",
                "detail": (
                    f"{result['associations']['tianhua_pre_delivery_draft_items']} "
                    "条；大于 0 时 apply 强制中止"
                ),
            },
        ],
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)


def write_report(
    result: dict,
    path: Path,
    *,
    mode: str,
    list_path: Path,
    backup_path: Path | None = None,
    after: dict | None = None,
) -> None:
    source = after or result
    lines = [
        f"# RUIDA DELETE {mode}",
        "",
        f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
        f"- 删除清单：`{list_path}`",
        f"- 备份路径：`{backup_path or 'dry-run 未备份'}`",
        f"- RUIDA 订单：{len(result['order_ids'])}",
        f"- RUIDA 订单明细：{len(result['item_ids'])}",
        f"- 天华导入引用保留并解除绑定：{result['associations']['tianhua_pre_delivery_import_items']}",
        f"- 天华草稿引用：{result['associations']['tianhua_pre_delivery_draft_items']}",
        f"- integrity_check：{source['integrity_check']}",
        f"- foreign_key_check：{source['foreign_key_violations']}",
        "",
        "## 关联影响",
        "",
    ]
    lines.extend(
        f"- {table}: {count}"
        for table, count in result["associations"].items()
    )
    lines.extend(["", "## 核心表数量", ""])
    after_counts = source.get("after_counts", result["estimated_counts"])
    lines.extend(
        f"- {table}: {count} -> {after_counts.get(table, count)}"
        for table, count in result["before_counts"].items()
    )
    lines.extend(
        [
            "",
            "## 安全结论",
            "",
            "- 删除条件固定为 sales_orders.order_number 包含 RUIDA。",
            "- 非 RUIDA 订单数量在 apply 前后必须一致。",
            "- 天华预送货四张独立表不会执行 DELETE。",
            "- 不修改正式库存数量，不生成送货、回单或对账记录。",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _execute_if_table(
    connection: sqlite3.Connection,
    tables: set[str],
    table: str,
    sql: str,
) -> int:
    if table not in tables:
        return 0
    cursor = connection.execute(sql)
    return max(cursor.rowcount, 0)


def apply_delete(connection: sqlite3.Connection, result: dict) -> dict:
    if result["associations"]["tianhua_pre_delivery_draft_items"]:
        raise RuntimeError("存在关联天华草稿明细，禁止删除 RUIDA 订单")

    tables = _tables(connection)
    non_ruida_before = int(
        connection.execute(
            "SELECT COUNT(*) FROM sales_orders "
            "WHERE UPPER(order_number) NOT LIKE '%RUIDA%'"
        ).fetchone()[0]
    )
    deleted: dict[str, int] = {}
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.executescript(
            """
            CREATE TEMP TABLE _ruida_delivery_ids(id INTEGER PRIMARY KEY);
            CREATE TEMP TABLE _ruida_receipt_item_ids(id INTEGER PRIMARY KEY);
            CREATE TEMP TABLE _ruida_receipt_ids(id INTEGER PRIMARY KEY);
            CREATE TEMP TABLE _ruida_statement_ids(id INTEGER PRIMARY KEY);
            """
        )
        if "sales_delivery_items" in tables:
            connection.execute(
                """
                INSERT INTO _ruida_delivery_ids
                SELECT DISTINCT delivery_id FROM sales_delivery_items
                WHERE order_item_id IN (
                    SELECT id FROM sales_order_items WHERE order_id IN (
                        SELECT id FROM sales_orders
                        WHERE UPPER(order_number) LIKE '%RUIDA%'
                    )
                )
                """
            )
        if {"finance_return_receipt_items", "sales_delivery_items"} <= tables:
            connection.executescript(
                """
                INSERT INTO _ruida_receipt_item_ids
                SELECT id FROM finance_return_receipt_items
                WHERE delivery_item_id IN (
                    SELECT id FROM sales_delivery_items
                    WHERE delivery_id IN (SELECT id FROM _ruida_delivery_ids)
                );
                INSERT INTO _ruida_receipt_ids
                SELECT DISTINCT return_receipt_id FROM finance_return_receipt_items
                WHERE id IN (SELECT id FROM _ruida_receipt_item_ids);
                """
            )
        if "finance_statement_items" in tables:
            connection.execute(
                """
                INSERT INTO _ruida_statement_ids
                SELECT DISTINCT statement_id FROM finance_statement_items
                WHERE return_receipt_item_id IN (
                    SELECT id FROM _ruida_receipt_item_ids
                )
                """
            )
        if "tianhua_pre_delivery_import_items" in tables:
            cursor = connection.execute(
                """
                UPDATE tianhua_pre_delivery_import_items
                SET order_item_id=NULL,
                    order_number=NULL,
                    status='not_matched',
                    selected=0,
                    warning=TRIM(
                        COALESCE(warning || '；', '') ||
                        '原匹配订单为 RUIDA 历史订单，已在清理时解除绑定。'
                    )
                WHERE order_item_id IN (
                    SELECT id FROM sales_order_items WHERE order_id IN (
                        SELECT id FROM sales_orders
                        WHERE UPPER(order_number) LIKE '%RUIDA%'
                    )
                )
                """
            )
            deleted["tianhua_links_detached"] = max(cursor.rowcount, 0)

        delete_sql = {
            "finance_statement_items": """
                DELETE FROM finance_statement_items
                WHERE return_receipt_item_id IN (
                    SELECT id FROM _ruida_receipt_item_ids
                )
            """,
            "finance_return_receipt_items": """
                DELETE FROM finance_return_receipt_items
                WHERE id IN (SELECT id FROM _ruida_receipt_item_ids)
            """,
            "migration_ruida_sales_item_map": """
                DELETE FROM migration_ruida_sales_item_map
                WHERE sales_order_item_id IN (
                    SELECT id FROM sales_order_items WHERE order_id IN (
                        SELECT id FROM sales_orders
                        WHERE UPPER(order_number) LIKE '%RUIDA%'
                    )
                )
            """,
            "migration_ruida_sales_order_map": """
                DELETE FROM migration_ruida_sales_order_map
                WHERE sales_order_id IN (
                    SELECT id FROM sales_orders
                    WHERE UPPER(order_number) LIKE '%RUIDA%'
                )
            """,
            "migration_entity_map": """
                DELETE FROM migration_entity_map
                WHERE (
                    LOWER(target_table)='sales_orders'
                    AND target_id IN (
                        SELECT id FROM sales_orders
                        WHERE UPPER(order_number) LIKE '%RUIDA%'
                    )
                ) OR (
                    LOWER(target_table)='sales_order_items'
                    AND target_id IN (
                        SELECT id FROM sales_order_items WHERE order_id IN (
                            SELECT id FROM sales_orders
                            WHERE UPPER(order_number) LIKE '%RUIDA%'
                        )
                    )
                )
            """,
            "supplier_requisition_order_items": """
                DELETE FROM supplier_requisition_order_items
                WHERE order_item_id IN (
                    SELECT id FROM sales_order_items WHERE order_id IN (
                        SELECT id FROM sales_orders
                        WHERE UPPER(order_number) LIKE '%RUIDA%'
                    )
                )
            """,
            "material_requisition_items": """
                DELETE FROM material_requisition_items
                WHERE order_item_id IN (
                    SELECT id FROM sales_order_items WHERE order_id IN (
                        SELECT id FROM sales_orders
                        WHERE UPPER(order_number) LIKE '%RUIDA%'
                    )
                )
            """,
            "sales_delivery_items": """
                DELETE FROM sales_delivery_items
                WHERE order_item_id IN (
                    SELECT id FROM sales_order_items WHERE order_id IN (
                        SELECT id FROM sales_orders
                        WHERE UPPER(order_number) LIKE '%RUIDA%'
                    )
                )
            """,
            "order_item_number_sequences": """
                DELETE FROM order_item_number_sequences
                WHERE order_id IN (
                    SELECT id FROM sales_orders
                    WHERE UPPER(order_number) LIKE '%RUIDA%'
                )
            """,
        }
        for table, sql in delete_sql.items():
            deleted[table] = _execute_if_table(
                connection, tables, table, sql
            )

        deleted["finance_return_receipts"] = _execute_if_table(
            connection,
            tables,
            "finance_return_receipts",
            """
            DELETE FROM finance_return_receipts
            WHERE id IN (SELECT id FROM _ruida_receipt_ids)
              AND NOT EXISTS (
                  SELECT 1 FROM finance_return_receipt_items
                  WHERE return_receipt_id=finance_return_receipts.id
              )
            """,
        )
        deleted["finance_statements"] = _execute_if_table(
            connection,
            tables,
            "finance_statements",
            """
            DELETE FROM finance_statements
            WHERE id IN (SELECT id FROM _ruida_statement_ids)
              AND NOT EXISTS (
                  SELECT 1 FROM finance_statement_items
                  WHERE statement_id=finance_statements.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM finance_invoices
                  WHERE statement_id=finance_statements.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM finance_settlement_records
                  WHERE statement_id=finance_statements.id
              )
            """,
        )

        deleted["sales_order_items"] = connection.execute(
            """
            DELETE FROM sales_order_items
            WHERE order_id IN (
                SELECT id FROM sales_orders
                WHERE UPPER(order_number) LIKE '%RUIDA%'
            )
            """
        ).rowcount
        deleted["sales_orders"] = connection.execute(
            "DELETE FROM sales_orders "
            "WHERE UPPER(order_number) LIKE '%RUIDA%'"
        ).rowcount

        non_ruida_after = int(
            connection.execute(
                "SELECT COUNT(*) FROM sales_orders "
                "WHERE UPPER(order_number) NOT LIKE '%RUIDA%'"
            ).fetchone()[0]
        )
        if non_ruida_before != non_ruida_after:
            raise RuntimeError("非 RUIDA 订单数量发生变化，已回滚")
        if connection.execute(
            "SELECT COUNT(*) FROM sales_orders "
            "WHERE UPPER(order_number) LIKE '%RUIDA%'"
        ).fetchone()[0]:
            raise RuntimeError("仍有 RUIDA 订单未删除，已回滚")
        connection.commit()
    except Exception:
        connection.rollback()
        raise

    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    fk = len(connection.execute("PRAGMA foreign_key_check").fetchall())
    if integrity != "ok" or fk:
        raise RuntimeError(
            f"删除后数据库校验失败：integrity={integrity}, foreign_keys={fk}"
        )
    return {
        "deleted": deleted,
        "after_counts": {
            table: _count(connection, table)
            for table in CORE_TABLES
            if table in tables
        },
        "integrity_check": integrity,
        "foreign_key_violations": fk,
    }


def run(database: Path, report_dir: Path, backup_dir: Path, apply: bool) -> dict:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    list_path = report_dir / f"RUIDA_DELETE_LIST_{timestamp}.xlsx"
    report_path = report_dir / (
        f"RUIDA_DELETE_APPLY_{timestamp}.md"
        if apply
        else f"RUIDA_DELETE_DRY_RUN_{timestamp}.md"
    )
    with sqlite3.connect(database, timeout=60) as connection:
        connection.execute("PRAGMA busy_timeout=60000")
        result = analyze(connection)
    export_list(result, list_path)

    if not apply:
        write_report(
            result,
            report_path,
            mode="DRY RUN",
            list_path=list_path,
        )
        return {
            "mode": "dry-run",
            "report": str(report_path),
            "list": str(list_path),
            "orders": len(result["order_ids"]),
            "items": len(result["item_ids"]),
            "associations": result["associations"],
        }

    if not result["order_ids"]:
        raise RuntimeError("未发现 RUIDA 订单，拒绝执行空清理")
    backup = backup_to_nas(
        source_path=database,
        backup_dir=backup_dir,
        filename_suffix="_BEFORE_DELETE_RUIDA_ORDERS",
        keep_regular=100,
    )
    with sqlite3.connect(database, timeout=60) as connection:
        connection.execute("PRAGMA busy_timeout=60000")
        after = apply_delete(connection, result)
    write_report(
        result,
        report_path,
        mode="APPLY",
        list_path=list_path,
        backup_path=backup.path,
        after=after,
    )
    return {
        "mode": "apply",
        "report": str(report_path),
        "list": str(list_path),
        "backup": str(backup.path),
        "backup_sha256": backup.sha256,
        "orders_deleted": after["deleted"]["sales_orders"],
        "items_deleted": after["deleted"]["sales_order_items"],
        "deleted": after["deleted"],
        "after_counts": after["after_counts"],
        "integrity_check": after["integrity_check"],
        "foreign_key_violations": after["foreign_key_violations"],
    }


def main() -> None:
    settings = load_settings()
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--database", type=Path, default=settings.database_path)
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=ROOT / "docs" / "cleanup_reports",
    )
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=ROOT / "data" / "backups",
    )
    args = parser.parse_args()
    output = run(
        args.database.resolve(),
        args.report_dir.resolve(),
        args.backup_dir.resolve(),
        args.apply,
    )
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
