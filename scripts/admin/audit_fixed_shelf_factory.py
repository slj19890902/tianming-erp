"""Read-only, explicit-copy fixed shelf integration audit. Never writes business data."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session
from app.models.warehouse_inventory import WarehouseLocation, InventoryLot, FinishedGoodsInventoryDetail
from app.models.fixed_shelf import ShelfBinding, ShelfProfile
from app.models.product import Product
from app.services.fixed_shelf import location_issue, profile_info
from app.services.location_candidates import load_warehouse_location_projection_contexts
from app.services.warehouse_rack_cells import _rack_cell_counts


def audit(database, published, draft):
    engine = create_engine("sqlite://", creator=lambda: sqlite3.connect(
        database.as_uri() + "?mode=ro", uri=True))
    @event.listens_for(engine, "connect")
    def read_only(connection, _):
        connection.execute("PRAGMA query_only=ON")
    document = json.loads(published.read_text(encoding="utf-8"))
    floors = document["floors"]
    expected = {}
    floor_rows = []
    for code, floor in floors.items():
        for rack in floor.get("racks", []):
            counts = _rack_cell_counts(rack)
            if counts is not None:
                for level, slots in enumerate(counts, 1):
                    for slot in range(1, slots + 1):
                        expected[(code, str(rack["id"]), level, slot)] = rack
        floor_rows.append({"floor": code, "revision": floor.get("revision"),
                           "regions": sum(f.get("feature_kind") == "zone" for f in floor.get("features", [])),
                           "racks": len(floor.get("racks", []))})
    with Session(engine) as db:
        locations = list(db.scalars(select(WarehouseLocation)))
        racks = [row for row in locations if row.is_active and row.storage_type == "rack"]
        contexts = load_warehouse_location_projection_contexts(db, racks)
        issues = []
        actual = Counter()
        for row in racks:
            key = (f"{row.warehouse_floor}F", str(row.map_rack_id), row.level_no, row.slot_no)
            actual[key] += 1
            issue = location_issue(db, row, contexts.get(row.id))
            if key not in expected:
                issue = issue or "正式货位未对应当前地图货架层格"
            if issue:
                issues.append({"location_id": row.id, "area": row.area_code, "name": row.location_name,
                               "rack": row.map_rack_id, "level": row.level_no, "slot": row.slot_no, "issue": issue})
        bindings = list(db.scalars(select(ShelfBinding)))
        profiles = list(db.scalars(select(ShelfProfile)))
        products = list(db.scalars(select(Product).join(FinishedGoodsInventoryDetail,
            FinishedGoodsInventoryDetail.product_id == Product.id).join(InventoryLot,
            InventoryLot.id == FinishedGoodsInventoryDetail.inventory_lot_id).where(
            InventoryLot.inventory_type == "finished", InventoryLot.quantity_available + InventoryLot.quantity_reserved
            + InventoryLot.quantity_damaged > 0).distinct().order_by(Product.id)))
        product_rows = []
        for product in products:
            info = profile_info(db, product)
            product_rows.append({key: info[key] for key in (
                "product_id", "customer_short_name", "inventory_code", "product_name", "units_per_bundle",
                "warning_quantity", "warning_policy_count", "staging_location_id", "version")})
        invalid = []
        for binding in bindings:
            row = db.get(WarehouseLocation, binding.location_id)
            issue = location_issue(db, row)
            if issue:
                invalid.append({"location_id": binding.location_id, "product_id": binding.product_id, "issue": issue})
    with sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchall()
        fk = connection.execute("PRAGMA foreign_key_check").fetchall()
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchall()
    return {"database": str(database), "revision": revision, "integrity": integrity, "foreign_keys": fk,
        "published_sha256": hashlib.sha256(published.read_bytes()).hexdigest(),
        "draft_exists_at_audit": draft.exists(),
        "draft_sha256": hashlib.sha256(draft.read_bytes()).hexdigest() if draft.exists() else None,
        "floors": floor_rows, "formal_locations_total": len(locations),
        "formal_locations_active": sum(bool(row.is_active) for row in locations),
        "active_rack_cells": len(racks), "published_rack_cells": len(expected),
        "missing_formal_cells": [list(key) for key in expected if key not in actual],
        "duplicate_formal_cells": [list(key) for key, count in actual.items() if count > 1 and key in expected],
        "rack_location_issues": issues,
        "rack_issue_counts_by_area": dict(Counter(row["area"] for row in issues)),
        "missing_formal_cells_by_floor": dict(Counter(key[0] for key in expected if key not in actual)),
        "fixed_profiles": len(profiles), "fixed_bindings": len(bindings),
        "valid_fixed_bindings": len(bindings) - len(invalid), "invalid_fixed_bindings": invalid,
        "product_scope": "positive physical finished inventory only; not inferred fixed assignments",
        "products": product_rows,
        "missing_customer_short_name": sum(not p["customer_short_name"] for p in product_rows),
        "missing_customer_inventory_code": sum(not p["inventory_code"] for p in product_rows),
        "missing_bundle": sum(p["units_per_bundle"] is None for p in product_rows),
        "missing_or_ambiguous_warning": sum(p["warning_policy_count"] != 1 for p in product_rows)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--published", type=Path, required=True)
    parser.add_argument("--draft", type=Path, required=True)
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()
    result = audit(args.database.resolve(), args.published.resolve(), args.draft.resolve())
    if args.summary:
        for name in ("products", "rack_location_issues", "missing_formal_cells", "duplicate_formal_cells", "invalid_fixed_bindings"):
            result[name + "_count"] = len(result[name])
            result[name] = result[name][:5]
    print(json.dumps(result, ensure_ascii=False, indent=2))
