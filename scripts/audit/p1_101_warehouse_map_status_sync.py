"""Anonymous read-only evidence for P1-101 warehouse-map rule alignment."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote


AUDIT_SQL = {
    "alembic": "SELECT version_num FROM alembic_version ORDER BY version_num",
    "fin_members": """
        SELECT a.area_code,
               p.status AS policy_status,
               p.storage_layout,
               p.published_map_revision AS policy_revision,
               g.status AS plan_status,
               g.published_map_revision AS plan_revision,
               g.target_slot_count,
               COUNT(DISTINCT l.id) AS location_count,
               MIN(l.location_code) AS first_location_code,
               MAX(l.location_code) AS last_location_code,
               SUM(CASE WHEN l.is_active = 1 AND l.placement_status = 'placed'
                        THEN 1 ELSE 0 END) AS active_placed,
               COUNT(DISTINCT fl.id) AS mapped_geometry,
               COUNT(DISTINCT CASE WHEN pal.is_current = 1 THEN pal.id END)
                   AS current_pallets,
               COUNT(DISTINCT CASE
                   WHEN lot.status IN ('active','frozen')
                    AND (lot.quantity_available + lot.quantity_reserved
                         + lot.quantity_damaged) > 0
                   THEN lot.id END) AS live_lots
        FROM warehouse_areas a
        LEFT JOIN warehouse_area_storage_policies p ON p.area_id = a.id
        LEFT JOIN warehouse_ground_layout_plans g ON g.area_id = a.id
        LEFT JOIN warehouse_locations l
          ON l.address_area_id = a.id
          OR (l.warehouse_floor = 1 AND upper(l.area_code) = upper(a.area_code))
        LEFT JOIN floor3_location_layouts fl ON fl.location_id = l.id
        LEFT JOIN inventory_pallets pal ON pal.location_id = l.id
        LEFT JOIN inventory_lots lot ON lot.warehouse_location_id = l.id
        WHERE a.area_code IN ('FIN-001','FIN-002','FIN-003')
        GROUP BY a.id, p.id, g.id
        ORDER BY a.area_code
    """,
    "fin_published_empty_slots": """
        SELECT a.area_code,
               COUNT(DISTINCT s.location_id) AS published_slots,
               COUNT(DISTINCT CASE
                   WHEN pal.id IS NULL AND lot.id IS NULL AND occ.id IS NULL
                   THEN s.location_id END) AS empty_slots
        FROM warehouse_areas a
        JOIN warehouse_area_storage_policies p
          ON p.area_id = a.id AND p.status = 'published'
        JOIN warehouse_ground_layout_plans g
          ON g.area_id = a.id AND g.status = 'published'
         AND g.published_map_revision = p.published_map_revision
        JOIN warehouse_ground_layout_slots s ON s.plan_id = g.id
        JOIN warehouse_locations l ON l.id = s.location_id
        LEFT JOIN inventory_pallets pal
          ON pal.location_id = l.id AND pal.is_current = 1
        LEFT JOIN inventory_lots lot
          ON lot.warehouse_location_id = l.id
         AND lot.status IN ('active','frozen')
         AND (lot.quantity_available + lot.quantity_reserved
              + lot.quantity_damaged) > 0
        LEFT JOIN warehouse_ground_occupancy_slots os
          ON os.location_id = l.id AND os.status = 'active'
        LEFT JOIN warehouse_ground_occupancies occ
          ON occ.id = os.occupancy_id AND occ.status = 'active'
        WHERE a.area_code IN ('FIN-001','FIN-002','FIN-003')
          AND l.is_active = 1 AND l.placement_status = 'placed'
        GROUP BY a.id
        ORDER BY a.area_code
    """,
    "legacy_dispatch": """
        SELECT l.location_code, l.is_active, l.warehouse_floor, l.area_code,
               l.warehouse_type, l.storage_type, l.placement_status,
               l.source_version,
               COUNT(DISTINCT CASE
                   WHEN lot.status IN ('active','frozen')
                    AND (lot.quantity_available + lot.quantity_reserved
                         + lot.quantity_damaged) > 0
                   THEN lot.id END) AS live_lots,
               COALESCE(SUM(CASE WHEN lot.status IN ('active','frozen')
                    THEN lot.quantity_available ELSE 0 END), 0)
                   AS available_quantity,
               COUNT(DISTINCT CASE WHEN pal.is_current = 1 THEN pal.id END)
                   AS current_pallets
        FROM warehouse_locations l
        LEFT JOIN inventory_lots lot ON lot.warehouse_location_id = l.id
        LEFT JOIN inventory_pallets pal ON pal.location_id = l.id
        WHERE l.location_code = 'F1-DISPATCH-01'
        GROUP BY l.id
    """,
    "pending_task_order_matrix": """
        SELECT o.status AS order_status,
               CASE WHEN i.is_force_closed = 1 THEN 'force_closed'
                    WHEN i.delivered_quantity >= i.quantity
                    THEN 'fully_delivered'
                    ELSE 'open_remaining' END AS item_state,
               COUNT(DISTINCT t.id) AS task_count
        FROM production_tasks t
        JOIN sales_order_items i ON i.id = t.order_item_id
        JOIN sales_orders o ON o.id = i.order_id
        WHERE t.status = 'pending'
        GROUP BY o.status, item_state
        ORDER BY o.status, item_state
    """,
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _query_bundle_sha256() -> str:
    canonical = "\n".join(
        f"-- {name}\n{sql.strip()}" for name, sql in sorted(AUDIT_SQL.items())
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def collect(database: Path) -> dict[str, Any]:
    database = database.resolve(strict=True)
    started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    stat_before = database.stat()
    hash_before = _sha256(database)
    uri = f"file:{quote(database.as_posix(), safe='/:')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=3000")
    try:
        query_only = int(connection.execute("PRAGMA query_only").fetchone()[0])
        connection.execute("BEGIN")
        results = {
            name: [dict(row) for row in connection.execute(sql).fetchall()]
            for name, sql in AUDIT_SQL.items()
        }
        connection.rollback()
    finally:
        connection.close()
    hash_after = _sha256(database)
    stat_after = database.stat()
    return {
        "query_started_at": started_at,
        "query_finished_at": datetime.now()
        .astimezone()
        .isoformat(timespec="seconds"),
        "query_bundle_sha256": _query_bundle_sha256(),
        "database": {
            "path": str(database),
            "mode": "SQLite URI mode=ro; PRAGMA query_only=ON",
            "query_only": query_only,
            "size_before": stat_before.st_size,
            "size_after": stat_after.st_size,
            "mtime_ns_before": stat_before.st_mtime_ns,
            "mtime_ns_after": stat_after.st_mtime_ns,
            "sha256_before": hash_before,
            "sha256_after": hash_after,
            "byte_facts_unchanged_during_audit": (
                stat_before.st_size == stat_after.st_size
                and stat_before.st_mtime_ns == stat_after.st_mtime_ns
                and hash_before == hash_after
            ),
        },
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True, type=Path)
    arguments = parser.parse_args()
    print(json.dumps(collect(arguments.database), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
