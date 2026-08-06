from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3


ProductionTaskProvider = Callable[[], dict]
ACTIVE_ORDER_STATUSES = (
    "pending_confirmation",
    "pending_production",
    "production",
    "pending_delivery",
)


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[1])
        for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    }


def _column(
    columns: set[str],
    alias: str,
    name: str,
    fallback: str = "NULL",
) -> str:
    return f'{alias}."{name}"' if name in columns else fallback


def _text(value) -> str:
    return "" if value is None else str(value)


def read_pending_production_tasks(database_path: Path) -> dict:
    """Read current ERP task facts through SQLite query-only mode.

    Only stable task/order/customer/product columns are selected so a read-only
    factory snapshot can remain usable when optional workflow columns lag the
    code checkout by one migration.
    """

    fetched_at = datetime.now(timezone.utc).isoformat()
    connection: sqlite3.Connection | None = None
    try:
        resolved = database_path.resolve(strict=True)
        connection = sqlite3.connect(
            f"file:{resolved.as_posix()}?mode=ro",
            uri=True,
            check_same_thread=False,
            timeout=10,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA busy_timeout = 10000")

        task_columns = _table_columns(connection, "production_tasks")
        item_columns = _table_columns(connection, "sales_order_items")
        order_columns = _table_columns(connection, "sales_orders")
        customer_columns = _table_columns(connection, "customers")
        product_columns = _table_columns(connection, "products")
        component_columns = _table_columns(
            connection, "sales_order_item_bom_components"
        )
        required = {
            "production_tasks": {"id", "order_item_id", "status", "planned_quantity"},
            "sales_order_items": {"id", "order_id", "product_id"},
            "sales_orders": {"id", "order_number", "customer_id"},
            "customers": {"id", "name"},
            "products": {"id", "product_code", "product_name"},
        }
        actual = {
            "production_tasks": task_columns,
            "sales_order_items": item_columns,
            "sales_orders": order_columns,
            "customers": customer_columns,
            "products": product_columns,
        }
        missing = {
            table: sorted(columns - actual[table])
            for table, columns in required.items()
            if not columns.issubset(actual[table])
        }
        if missing:
            raise RuntimeError(f"ERP生产任务表结构不完整：{missing}")

        component_enabled = (
            "sales_order_item_bom_component_id" in task_columns
            and "id" in component_columns
        )
        component_join = (
            "LEFT JOIN sales_order_item_bom_components bc "
            "ON bc.id = t.sales_order_item_bom_component_id"
            if component_enabled
            else ""
        )
        component_id = _column(
            task_columns,
            "t",
            "sales_order_item_bom_component_id",
        )
        component_code = (
            _column(component_columns, "bc", "snapshot_component_product_code")
            if component_enabled
            else "NULL"
        )
        component_name = (
            _column(component_columns, "bc", "snapshot_component_product_name")
            if component_enabled
            else "NULL"
        )
        component_spec = (
            _column(component_columns, "bc", "snapshot_component_spec")
            if component_enabled
            else "NULL"
        )
        item_code = _column(item_columns, "i", "snapshot_product_code")
        item_name = _column(item_columns, "i", "snapshot_product_name")
        item_spec = _column(item_columns, "i", "snapshot_spec")
        item_order_number = _column(
            item_columns,
            "i",
            "item_order_number",
            "o.order_number",
        )
        source_version = _column(task_columns, "t", "version", "1")
        task_updated_at = _column(
            task_columns,
            "t",
            "updated_at",
            _column(task_columns, "t", "created_at"),
        )
        delivery_date = _column(order_columns, "o", "delivery_date")
        where = ["t.status = 'pending'"]
        parameters: list[str] = []
        if "status" in order_columns:
            placeholders = ",".join("?" for _ in ACTIVE_ORDER_STATUSES)
            where.append(f"o.status IN ({placeholders})")
            parameters.extend(ACTIVE_ORDER_STATUSES)
        if "is_force_closed" in item_columns:
            where.append("COALESCE(i.is_force_closed, 0) = 0")
        rows = connection.execute(
            f"""
            SELECT
                t.id AS source_task_id,
                {source_version} AS source_version,
                o.id AS order_id,
                COALESCE({item_order_number}, o.order_number) AS order_number,
                c.name AS customer_name,
                COALESCE({component_code}, {item_code}, p.product_code, '') AS product_code,
                COALESCE({component_name}, {item_name}, p.product_name, '') AS product_name,
                COALESCE({component_spec}, {item_spec}, '') AS specification,
                t.status AS status,
                t.planned_quantity AS planned_quantity,
                CASE WHEN {component_id} IS NULL THEN 'sets' ELSE 'pieces' END
                    AS production_quantity_unit,
                {task_updated_at} AS task_updated_at,
                {delivery_date} AS delivery_date
            FROM production_tasks t
            JOIN sales_order_items i ON i.id = t.order_item_id
            JOIN sales_orders o ON o.id = i.order_id
            JOIN customers c ON c.id = o.customer_id
            JOIN products p ON p.id = i.product_id
            {component_join}
            WHERE {' AND '.join(where)}
            ORDER BY {delivery_date} IS NULL, {delivery_date}, o.id, i.id
            """,
            parameters,
        ).fetchall()
        items = [
            {
                "source_task_id": int(row["source_task_id"]),
                "source_version": int(row["source_version"] or 1),
                "order_id": int(row["order_id"]),
                "order_number": _text(row["order_number"]),
                "customer_name": _text(row["customer_name"]),
                "product_code": _text(row["product_code"]),
                "product_name": _text(row["product_name"]),
                "specification": _text(row["specification"]),
                "status": _text(row["status"]),
                "planned_quantity": int(row["planned_quantity"] or 0),
                "production_quantity_unit": _text(row["production_quantity_unit"]),
                "task_updated_at": _text(row["task_updated_at"]) or None,
                "delivery_date": _text(row["delivery_date"]) or None,
            }
            for row in rows
        ]
        return {
            "available": True,
            "source_label": f"ERP生产任务 · {database_path.name}",
            "source_read_only": True,
            "fetched_at": fetched_at,
            "items": items,
            "error": None,
        }
    except Exception as error:
        return {
            "available": False,
            "source_label": "ERP生产任务",
            "source_read_only": True,
            "fetched_at": fetched_at,
            "items": [],
            "error": f"ERP只读源不可用：{error}",
        }
    finally:
        if connection is not None:
            connection.close()


def default_production_task_provider(project_root: Path) -> ProductionTaskProvider:
    configured = os.getenv("FACTORY_TWIN_ERP_READONLY_DB", "").strip()
    database_path = (
        Path(configured).expanduser()
        if configured
        else project_root / "data" / "carton_erp.sqlite3"
    )
    return lambda: read_pending_production_tasks(database_path)
