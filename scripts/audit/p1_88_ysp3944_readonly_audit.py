"""Read-only evidence collector for P1-88 / YSP3944.

The connection is opened with SQLite URI ``mode=ro&immutable=1`` and then
forced into ``PRAGMA query_only=ON``.  The script intentionally emits only the
named customer/product and their directly related BOM/order/requisition rows.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote


TARGET_CUSTOMER = "格莱锐"
TARGET_CODE = "YSP3944"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _rows(
    connection: sqlite3.Connection,
    table: str,
    wanted_columns: Iterable[str],
    where: str,
    parameters: Iterable[Any],
) -> list[dict[str, Any]]:
    available = _columns(connection, table)
    selected = [column for column in wanted_columns if column in available]
    if not selected:
        return []
    sql = f"SELECT {', '.join(selected)} FROM {table} WHERE {where} ORDER BY id"
    return [dict(row) for row in connection.execute(sql, tuple(parameters))]


def _ids(rows: Iterable[dict[str, Any]], key: str = "id") -> list[int]:
    return sorted({int(row[key]) for row in rows if row.get(key) is not None})


def _in_clause(ids: list[int]) -> tuple[str, list[int]]:
    return ",".join("?" for _ in ids), ids


def collect(database: Path) -> dict[str, Any]:
    database = database.resolve(strict=True)
    started_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    stat_before = database.stat()
    hash_before = _sha256(database)
    uri = f"file:{quote(database.as_posix(), safe='/:')}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    try:
        query_only = int(connection.execute("PRAGMA query_only").fetchone()[0])
        schema_version = int(connection.execute("PRAGMA schema_version").fetchone()[0])
        alembic_heads = [
            str(row[0])
            for row in connection.execute("SELECT version_num FROM alembic_version")
        ]
        customers = _rows(
            connection,
            "customers",
            ("id", "name", "short_name"),
            "name LIKE ?",
            (f"%{TARGET_CUSTOMER}%",),
        )
        customer_ids = _ids(customers)
        if not customer_ids:
            raise RuntimeError(f"未找到目标客户：{TARGET_CUSTOMER}")
        customer_slots, customer_args = _in_clause(customer_ids)
        products = _rows(
            connection,
            "products",
            (
                "id",
                "customer_id",
                "product_code",
                "customer_material_code",
                "product_name",
                "box_style",
                "is_composite",
                "is_virtual_composite_parent",
                "combination_mode",
                "composite_fulfillment_mode",
                "splice_mode",
                "pieces_per_box",
                "report_length_mm",
                "report_width_mm",
                "crease_type",
                "crease_left_mm",
                "crease_middle_mm",
                "crease_right_mm",
                "material_id",
                "legacy_material_text",
                "layer_count",
                "flute_type",
                "default_cutting_mode",
                "production_process",
                "remark",
                "version",
            ),
            (
                f"customer_id IN ({customer_slots}) AND ("
                "product_code LIKE ? OR customer_material_code LIKE ? OR product_name LIKE ?)"
            ),
            (
                *customer_args,
                f"%{TARGET_CODE}%",
                f"%{TARGET_CODE}%",
                f"%{TARGET_CODE}%",
            ),
        )
        parent_ids = _ids(products)
        bom_templates: list[dict[str, Any]] = []
        child_products: list[dict[str, Any]] = []
        if parent_ids:
            parent_slots, parent_args = _in_clause(parent_ids)
            bom_templates = _rows(
                connection,
                "product_bom_components",
                (
                    "id",
                    "parent_product_id",
                    "component_product_id",
                    "quantity_per_set",
                    "display_order",
                    "show_on_order",
                    "show_on_delivery",
                    "show_on_label",
                    "is_active",
                ),
                f"parent_product_id IN ({parent_slots})",
                parent_args,
            )
            child_ids = sorted(
                {
                    int(row["component_product_id"])
                    for row in bom_templates
                    if row.get("component_product_id") is not None
                }
            )
            if child_ids:
                child_slots, child_args = _in_clause(child_ids)
                child_products = _rows(
                    connection,
                    "products",
                    (
                        "id",
                        "customer_id",
                        "product_code",
                        "customer_material_code",
                        "product_name",
                        "box_style",
                        "splice_mode",
                        "pieces_per_box",
                        "report_length_mm",
                        "report_width_mm",
                        "crease_type",
                        "crease_left_mm",
                        "crease_middle_mm",
                        "crease_right_mm",
                        "material_id",
                        "legacy_material_text",
                        "layer_count",
                        "flute_type",
                        "default_cutting_mode",
                        "production_process",
                        "remark",
                        "version",
                    ),
                    f"id IN ({child_slots})",
                    child_args,
                )

        order_items: list[dict[str, Any]] = []
        orders: list[dict[str, Any]] = []
        bom_snapshots: list[dict[str, Any]] = []
        requisition_items: list[dict[str, Any]] = []
        requisition_sources: list[dict[str, Any]] = []
        requisitions: list[dict[str, Any]] = []
        if parent_ids:
            parent_slots, parent_args = _in_clause(parent_ids)
            order_items = _rows(
                connection,
                "sales_order_items",
                (
                    "id",
                    "order_id",
                    "product_id",
                    "snapshot_product_code",
                    "snapshot_customer_material_code",
                    "snapshot_product_name",
                    "quantity",
                    "snapshot_box_style",
                    "snapshot_splice_mode",
                    "snapshot_pieces_per_box",
                    "snapshot_report_length_mm",
                    "snapshot_report_width_mm",
                    "snapshot_material_id",
                    "snapshot_material_text",
                    "snapshot_layer_count",
                    "snapshot_flute_type",
                    "snapshot_cutting_mode",
                    "snapshot_production_notes",
                    "status",
                    "created_at",
                ),
                f"product_id IN ({parent_slots})",
                parent_args,
            )
            order_ids = _ids(order_items, "order_id")
            if order_ids:
                order_slots, order_args = _in_clause(order_ids)
                orders = _rows(
                    connection,
                    "sales_orders",
                    (
                        "id",
                        "order_number",
                        "customer_id",
                        "order_date",
                        "delivery_date",
                        "status",
                    ),
                    f"id IN ({order_slots})",
                    order_args,
                )
            order_item_ids = _ids(order_items)
            if order_item_ids:
                item_slots, item_args = _in_clause(order_item_ids)
                bom_snapshots = _rows(
                    connection,
                    "sales_order_item_bom_components",
                    (
                        "id",
                        "sales_order_item_id",
                        "product_bom_component_id",
                        "snapshot_component_product_id",
                        "snapshot_component_product_code",
                        "snapshot_component_customer_material_code",
                        "snapshot_component_product_name",
                        "quantity_per_set",
                        "order_set_quantity",
                        "required_piece_quantity",
                        "snapshot_component_box_style",
                        "snapshot_component_splice_mode",
                        "snapshot_component_pieces_per_box",
                        "snapshot_component_report_length_mm",
                        "snapshot_component_report_width_mm",
                        "snapshot_component_material_id",
                        "snapshot_component_material",
                        "snapshot_component_layer_count",
                        "snapshot_component_flute_type",
                        "snapshot_component_default_cutting_mode",
                        "created_at",
                    ),
                    f"sales_order_item_id IN ({item_slots})",
                    item_args,
                )
                requisition_items = _rows(
                    connection,
                    "material_requisition_items",
                    (
                        "id",
                        "requisition_id",
                        "order_item_id",
                        "requisition_qty",
                        "cardboard_len",
                        "cardboard_width",
                        "pieces_per_box",
                        "required_piece_qty",
                        "inventory_deducted_qty",
                        "special_process",
                        "material_snapshot",
                        "product_code_snapshot",
                        "product_name_snapshot",
                        "status",
                        "created_at",
                    ),
                    f"order_item_id IN ({item_slots})",
                    item_args,
                )
                requisition_item_ids = _ids(requisition_items)
                if requisition_item_ids:
                    requisition_item_slots, requisition_item_args = _in_clause(
                        requisition_item_ids
                    )
                    requisition_sources = _rows(
                        connection,
                        "requisition_item_bom_sources",
                        (
                            "id",
                            "requisition_item_id",
                            "sales_order_item_bom_component_id",
                            "component_type",
                            "order_set_quantity",
                            "quantity_per_set",
                            "required_piece_quantity",
                            "calculated_purchase_quantity",
                            "actual_yield_per_sheet",
                            "calculation_rule_version",
                            "active_guard",
                        ),
                        f"requisition_item_id IN ({requisition_item_slots})",
                        requisition_item_args,
                    )
                requisition_ids = _ids(requisition_items, "requisition_id")
                if requisition_ids:
                    requisition_slots, requisition_args = _in_clause(requisition_ids)
                    requisitions = _rows(
                        connection,
                        "material_requisitions",
                        (
                            "id",
                            "requisition_number",
                            "requisition_date",
                            "status",
                            "supplier_name",
                            "created_at",
                        ),
                        f"id IN ({requisition_slots})",
                        requisition_args,
                    )

        evidence = {
            "target": {"customer": TARGET_CUSTOMER, "product_code": TARGET_CODE},
            "customers": customers,
            "products": products,
            "bom_templates": bom_templates,
            "child_products": child_products,
            "orders": orders,
            "order_items": order_items,
            "bom_snapshots": bom_snapshots,
            "requisitions": requisitions,
            "requisition_items": requisition_items,
            "requisition_sources": requisition_sources,
        }
    finally:
        connection.close()

    hash_after = _sha256(database)
    stat_after = database.stat()
    if hash_after != hash_before or stat_after.st_size != stat_before.st_size:
        raise RuntimeError("只读审计前后数据库字节事实发生变化，停止使用审计结果")
    return {
        "query_started_at": started_at,
        "query_finished_at": datetime.now(timezone.utc)
        .astimezone()
        .isoformat(timespec="seconds"),
        "database": {
            "path": str(database),
            "mode": "ro+immutable; PRAGMA query_only=ON",
            "query_only": query_only,
            "size": stat_before.st_size,
            "mtime_ns_before": stat_before.st_mtime_ns,
            "mtime_ns_after": stat_after.st_mtime_ns,
            "sha256_before": hash_before,
            "sha256_after": hash_after,
            "schema_version": schema_version,
            "alembic_heads": alembic_heads,
        },
        "evidence": evidence,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True, type=Path)
    arguments = parser.parse_args()
    print(
        json.dumps(
            collect(arguments.database), ensure_ascii=False, indent=2, default=str
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
