"""Read-only transition evidence. No apply mode or historical fact updates."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3


def audit(database, parent_ids):
    ids = sorted(set(parent_ids))
    if not ids or any(type(pid) is not int or pid <= 0 for pid in ids):
        raise ValueError("明确指定有效父产品ID")
    path = Path(database).resolve(strict=True)
    with sqlite3.connect(path.as_uri()+"?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        def rows(sql, params=()):
            return [dict(r) for r in db.execute(sql, params)]
        def marks(values):
            return ",".join("?" for _ in values)
        products, edges, subkits, pending = {}, [], [], ids
        while pending:
            found = rows(f"SELECT id,customer_id,product_code,product_name,unit,version,is_active FROM products WHERE id IN ({marks(pending)})",pending)
            if {r["id"] for r in found} != set(pending):
                raise ValueError("BOM产品身份缺失")
            products.update({r["id"]:r for r in found})
            if len(products)>1000:
                raise ValueError("请缩小本次转换核对范围")
            links = rows(f"SELECT id,parent_product_id,component_product_id,quantity_per_set FROM product_bom_components WHERE parent_product_id IN ({marks(pending)}) ORDER BY id",pending)
            edges.extend(links)
            legacy = rows(f"SELECT parent_product_id,kit_product_id,kits_per_parent,recipe_json,version,enabled FROM product_subkits WHERE parent_product_id IN ({marks(pending)}) ORDER BY parent_product_id",pending)
            subkits.extend(legacy)
            pending = sorted(({r["component_product_id"] for r in links} | {r["kit_product_id"] for r in legacy})-products.keys())
        orders = rows(f"""SELECT i.id,i.order_id,o.order_number,o.status,i.product_id,i.quantity,
            i.delivered_quantity,i.is_force_closed,i.requisition_status,i.material_status
            FROM sales_order_items i JOIN sales_orders o ON o.id=i.order_id
            WHERE i.product_id IN ({marks(ids)}) AND i.delivered_quantity<i.quantity
            AND i.is_force_closed=0 AND o.status NOT IN ('cancelled','closed','dead','completed','archived','delivered','已作废','已结单')
            ORDER BY i.id""",ids)
        nodes = sorted(products)
        lots = rows(f"""SELECT l.id,d.product_id,l.warehouse_location_id,l.status,l.version,
            l.quantity_available,l.quantity_reserved,l.quantity_consumed,l.source_ref_type,l.source_ref_id,
            l.estimated_unit_cost_snapshot,l.cost_snapshot_source,l.cost_snapshot_detail_json,
            w.location_code,w.location_name,w.placement_status
            FROM inventory_lots l JOIN finished_goods_inventory_details d ON d.inventory_lot_id=l.id
            LEFT JOIN warehouse_locations w ON w.id=l.warehouse_location_id
            WHERE d.product_id IN ({marks(nodes)}) ORDER BY l.id""",nodes)
        item_ids = [r["id"] for r in orders]
        snapshots = rows(f"""SELECT id,sales_order_item_id,component_product_id,order_set_quantity,
            quantity_per_set,required_piece_quantity,snapshot_schema_version
            FROM sales_order_item_bom_components WHERE sales_order_item_id IN ({marks(item_ids)}) ORDER BY id""",item_ids) if item_ids else []
        reserves = rows(f"""SELECT id,inventory_lot_id,order_item_id,sales_order_item_bom_component_id,status,
            reserved_stock_quantity,consumed_stock_quantity,released_stock_quantity
            FROM inventory_reservations WHERE order_item_id IN ({marks(item_ids)}) ORDER BY id""",item_ids) if item_ids else []
        for r in reserves:
            r["remaining_stock_quantity"] = r["reserved_stock_quantity"]-r["consumed_stock_quantity"]-r["released_stock_quantity"]
        report = dict(schema=1, parent_ids=ids, products=[products[k] for k in nodes],
            master_edges=edges, legacy_subkits=subkits, unfinished_orders=orders, frozen_components=snapshots,
            inventory=lots, reservations=reserves,
            unplaced_lot_ids=[r["id"] for r in lots if r["quantity_available"]+r["quantity_reserved"]>0 and r["placement_status"]!="placed"],
            cost_review_lot_ids=[r["id"] for r in lots if r["quantity_available"]+r["quantity_reserved"]>0 and r["cost_snapshot_source"]!="purchase_receipt_actual"])
        report["fingerprint"] = hashlib.sha256(json.dumps(report,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--parent-ids", type=int, nargs="+", required=True)
    args = parser.parse_args()
    print(json.dumps(audit(args.database,args.parent_ids),ensure_ascii=False,indent=2))
