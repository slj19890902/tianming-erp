"""Read-only source-chain diagnostic for one supplier requisition item.

Run only against an authorized managed database copy, never a production DB.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def rows(db: sqlite3.Connection, table: str, where: str, args: tuple[int, ...]) -> list[dict]:
    return [dict(row) for row in db.execute(f"SELECT * FROM {table} WHERE {where}", args)]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--managed-copy", type=Path, required=True)
    parser.add_argument("--supplier-item-id", type=int, required=True)
    args = parser.parse_args()
    if args.supplier_item_id <= 0 or not args.managed_copy.is_file():
        raise SystemExit("A positive item ID and existing managed copy are required")
    db = sqlite3.connect(f"file:{args.managed_copy}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    item_rows = rows(db, "supplier_requisition_order_items", "id=?", (args.supplier_item_id,))
    if not item_rows:
        print(json.dumps({"supplier_item_id": args.supplier_item_id, "found": False}))
        return
    item = item_rows[0]
    links = rows(db, "procurement_source_links", "supplier_item_id=?", (args.supplier_item_id,))
    order = rows(db, "supplier_requisition_orders", "id=?", (item["supplier_order_id"],))
    output = {
        "supplier_item_id": args.supplier_item_id,
        "found": True,
        "supplier_order_number": order[0]["order_number"] if order else None,
        "supplier_order_status": order[0]["status"] if order else None,
        "product_code": item["product_code"],
        "product_id": item["product_id"],
        "order_item_id": item["order_item_id"],
        "source_key": item["source_key"],
        "item_status": item["status"],
        "source_links": [{k: link[k] for k in (
            "id", "status", "stock_replenishment_item_id", "material_requisition_item_id"
        )} for link in links],
    }
    requisition_ids = [link["material_requisition_item_id"] for link in links
                       if link["material_requisition_item_id"] is not None]
    stock_ids = [link["stock_replenishment_item_id"] for link in links
                 if link["stock_replenishment_item_id"] is not None]
    output["material_sources"] = []
    for source_id in requisition_ids:
        source = rows(db, "material_requisition_items", "id=?", (source_id,))
        bom = rows(db, "requisition_item_bom_sources", "requisition_item_id=?", (source_id,))
        if not source:
            output["material_sources"].append({"id": source_id, "found": False})
            continue
        order_item_id = source[0]["order_item_id"]
        sales_item = rows(db, "sales_order_items", "id=?", (order_item_id,))
        tasks = rows(db, "production_tasks", "order_item_id=?", (order_item_id,))
        output["material_sources"].append({
            "id": source_id, "status": source[0]["status"],
            "order_item_id": order_item_id,
            "fulfillment_mode": sales_item[0]["composite_fulfillment_mode_snapshot"] if sales_item else None,
            "bom_component_ids": [row["sales_order_item_bom_component_id"] for row in bom],
            "tasks": [{k: task[k] for k in ("id", "status", "version", "sales_order_item_bom_component_id", "task_role")} for task in tasks],
        })
    output["stock_sources"] = []
    for source_id in stock_ids:
        source = rows(db, "stock_replenishment_order_items", "id=?", (source_id,))
        output["stock_sources"].append({
            "id": source_id, "found": bool(source),
            "reference_product_id": source[0]["reference_product_id"] if source else None,
            "target_inventory_type": source[0]["target_inventory_type"] if source else None,
            "quantity": source[0]["quantity"] if source else None,
        })
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
