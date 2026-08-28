"""Normalize the current-map raw-material rack for receipt staging.

The current published map moved customer reserve board storage from the retired
first-floor RAW-006 ground positions to the third-floor RAW-001 rack.  The map
policy is already authoritative, but the four stable location rows still carry
the historical ``finished`` warehouse type.  This guarded data migration makes
those rows agree with the published raw-material policy without moving stock or
creating an inbound fact.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256
import json
from typing import Any

import sqlalchemy as sa


MIGRATION_KEY = "p0-25-current-map-raw-001-rack-normalization"
SNAPSHOT_TABLE = "warehouse_current_map_migration_snapshots"
FLOOR_NUMBER = 3
AREA_CODE = "RAW-001"
MAP_REVISION = "3994317ae14a7f18"
SOURCE_VERSION = "CURRENT_MAP"
PREVIOUS_WAREHOUSE_TYPE = "finished"
TARGET_WAREHOUSE_TYPE = "semi_finished"
MIGRATED_AT = "2026-08-27 00:00:00"
TARGET_LOCATION_CODES = (
    "3F-RAW-001-L001",
    "3F-RAW-001-L002",
    "3F-RAW-001-L003",
    "3F-RAW-001-L004",
)


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    return value


def _rows(
    connection: sa.Connection,
    statement: str,
    params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    return [
        {str(key): _json_value(value) for key, value in row.items()}
        for row in connection.execute(sa.text(statement), params or {}).mappings().all()
    ]


def _scalar(
    connection: sa.Connection,
    statement: str,
    params: dict[str, Any] | None = None,
) -> int:
    return int(connection.execute(sa.text(statement), params or {}).scalar_one() or 0)


def _target_locations(connection: sa.Connection) -> list[dict[str, Any]]:
    return _rows(
        connection,
        """
        SELECT l.id,l.location_code,l.location_name,l.warehouse_type,
               l.warehouse_floor,l.area_code,l.storage_type,l.is_active,
               l.source_version,l.placement_status,l.address_kind,
               l.address_area_id,l.rack_code,l.level_no,l.slot_no,
               a.id AS area_id,a.construction_status AS area_status,
               f.construction_status AS floor_status,
               p.status AS policy_status,p.allowed_inventory_types_json,
               p.storage_layout AS policy_storage_layout,
               p.map_feature_id,p.published_map_revision,
               x.id AS layout_id,x.layout_kind,x.source_type AS layout_source_type
        FROM warehouse_locations l
        JOIN warehouse_floors f ON f.floor_number=l.warehouse_floor
        JOIN warehouse_areas a
          ON a.floor_id=f.id AND upper(a.area_code)=upper(l.area_code)
        JOIN warehouse_area_storage_policies p ON p.area_id=a.id
        LEFT JOIN floor3_location_layouts x ON x.location_id=l.id
        WHERE f.floor_number=:floor_number
          AND upper(a.area_code)=:area_code
        ORDER BY l.location_code,l.id
        """,
        {"floor_number": FLOOR_NUMBER, "area_code": AREA_CODE},
    )


def _target_document(connection: sa.Connection) -> dict[str, Any]:
    locations = _target_locations(connection)
    ids = [int(row["id"]) for row in locations]
    if not ids:
        return {"locations": [], "inventory_lots": [], "pallets": []}
    placeholders = ",".join(str(value) for value in ids)
    return {
        "locations": locations,
        "inventory_lots": _rows(
            connection,
            f"SELECT * FROM inventory_lots WHERE warehouse_location_id IN ({placeholders}) ORDER BY id",
        ),
        "pallets": _rows(
            connection,
            f"SELECT * FROM inventory_pallets WHERE location_id IN ({placeholders}) ORDER BY id",
        ),
    }


def _fingerprint(connection: sa.Connection) -> str:
    encoded = json.dumps(
        _target_document(connection),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(encoded.encode("utf-8")).hexdigest()


def _existing_snapshot(connection: sa.Connection) -> dict[str, Any] | None:
    if not sa.inspect(connection).has_table(SNAPSHOT_TABLE):
        return None
    row = connection.execute(
        sa.text(
            f"SELECT payload_json,post_fingerprint FROM {SNAPSHOT_TABLE} "
            "WHERE migration_key=:key"
        ),
        {"key": MIGRATION_KEY},
    ).mappings().one_or_none()
    return dict(row) if row is not None else None


def _preflight(connection: sa.Connection) -> list[dict[str, Any]] | None:
    if not sa.inspect(connection).has_table(SNAPSHOT_TABLE):
        raise RuntimeError("P0-25 preflight failed: current-map snapshot table is missing.")
    rows = _target_locations(connection)
    if not rows:
        snapshot_count = _scalar(
            connection,
            f"SELECT COUNT(*) FROM {SNAPSHOT_TABLE}",
        )
        business_fact_count = _scalar(
            connection,
            "SELECT (SELECT COUNT(*) FROM inventory_lots) + "
            "(SELECT COUNT(*) FROM inventory_pallets) + "
            "(SELECT COUNT(*) FROM sales_orders)",
        )
        # fg42 deliberately skips the owner-confirmed current-map rewrite for
        # brand-new installations.  Earlier generic migrations still seed the
        # 398 inactive/legacy V11 location rows, so location count alone cannot
        # distinguish that supported path from a drifted factory replica.
        # A formal/copy database either has the audited current-map snapshot or
        # order/inventory facts and must continue to fail closed.
        if snapshot_count == 0 and business_fact_count == 0:
            return None
        raise RuntimeError("P0-25 preflight failed: current RAW-001 rack is missing.")
    if tuple(row["location_code"] for row in rows) != TARGET_LOCATION_CODES:
        raise RuntimeError("P0-25 preflight failed: current RAW-001 rack identity drifted.")
    for row in rows:
        try:
            allowed_types = json.loads(str(row["allowed_inventory_types_json"] or ""))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError(
                "P0-25 preflight failed: RAW-001 storage policy is invalid."
            ) from error
        valid = (
            row["warehouse_type"] == PREVIOUS_WAREHOUSE_TYPE
            and int(row["warehouse_floor"] or 0) == FLOOR_NUMBER
            and str(row["area_code"] or "").upper() == AREA_CODE
            and row["storage_type"] == "rack"
            and int(row["is_active"] or 0) == 1
            and row["source_version"] == SOURCE_VERSION
            and row["placement_status"] == "placed"
            and row["address_kind"] == "rack_slot"
            and int(row["address_area_id"] or 0) == int(row["area_id"] or 0)
            and row["area_status"] == "enabled"
            and row["floor_status"] == "enabled"
            and row["policy_status"] == "published"
            and row["policy_storage_layout"] == "rack"
            and row["published_map_revision"] == MAP_REVISION
            and isinstance(allowed_types, list)
            and "raw_material" in allowed_types
            and row["layout_id"] is not None
            and row["layout_kind"] == "physical_rack"
        )
        if not valid:
            raise RuntimeError(
                "P0-25 preflight failed: RAW-001 location, policy, or map geometry drifted."
            )
    ids = [int(row["id"]) for row in rows]
    placeholders = ",".join(str(value) for value in ids)
    live_lots = _scalar(
        connection,
        f"""
        SELECT COUNT(*) FROM inventory_lots
        WHERE warehouse_location_id IN ({placeholders})
          AND status IN ('active','frozen')
          AND quantity_available+quantity_reserved+quantity_damaged>0
        """,
    )
    current_pallets = _scalar(
        connection,
        f"SELECT COUNT(*) FROM inventory_pallets WHERE location_id IN ({placeholders}) AND is_current=1",
    )
    if live_lots or current_pallets:
        raise RuntimeError(
            "P0-25 preflight failed: RAW-001 is no longer empty; refusing classification repair."
        )
    return rows


def upgrade_current_map_raw_staging(connection: sa.Connection) -> bool:
    existing = _existing_snapshot(connection)
    if existing is not None:
        if _fingerprint(connection) != existing["post_fingerprint"]:
            raise RuntimeError(
                "P0-25 existing migration snapshot does not match current RAW-001 facts."
            )
        return False
    rows = _preflight(connection)
    if rows is None:
        return False
    pre_fingerprint = _fingerprint(connection)
    payload = {
        "migration_key": MIGRATION_KEY,
        "pre_fingerprint": pre_fingerprint,
        "locations": rows,
    }
    connection.execute(
        sa.text(
            f"INSERT INTO {SNAPSHOT_TABLE} "
            "(migration_key,payload_json,post_fingerprint,created_at) "
            "VALUES (:key,:payload,'PENDING',:created_at)"
        ),
        {
            "key": MIGRATION_KEY,
            "payload": json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            "created_at": MIGRATED_AT,
        },
    )
    ids = [int(row["id"]) for row in rows]
    placeholders = ",".join(str(value) for value in ids)
    result = connection.execute(
        sa.text(
            f"UPDATE warehouse_locations SET warehouse_type=:target_type "
            f"WHERE id IN ({placeholders}) AND warehouse_type=:previous_type"
        ),
        {
            "target_type": TARGET_WAREHOUSE_TYPE,
            "previous_type": PREVIOUS_WAREHOUSE_TYPE,
        },
    )
    if result.rowcount != len(ids):
        raise RuntimeError("P0-25 post validation failed: RAW-001 rows were not updated exactly once.")
    if _scalar(
        connection,
        f"SELECT COUNT(*) FROM warehouse_locations WHERE id IN ({placeholders}) AND warehouse_type=:target_type",
        {"target_type": TARGET_WAREHOUSE_TYPE},
    ) != len(ids):
        raise RuntimeError("P0-25 post validation failed: RAW-001 type is not normalized.")
    post_fingerprint = _fingerprint(connection)
    connection.execute(
        sa.text(
            f"UPDATE {SNAPSHOT_TABLE} SET post_fingerprint=:fingerprint "
            "WHERE migration_key=:key"
        ),
        {"fingerprint": post_fingerprint, "key": MIGRATION_KEY},
    )
    return True


def downgrade_current_map_raw_staging(connection: sa.Connection) -> bool:
    existing = _existing_snapshot(connection)
    if existing is None:
        return False
    if _fingerprint(connection) != existing["post_fingerprint"]:
        raise RuntimeError(
            "Refusing P0-25 downgrade: RAW-001 or its inventory facts changed after migration."
        )
    payload = json.loads(str(existing["payload_json"]))
    locations = payload.get("locations") or []
    ids = [int(row["id"]) for row in locations]
    if not ids:
        raise RuntimeError("Refusing P0-25 downgrade: migration snapshot has no locations.")
    placeholders = ",".join(str(value) for value in ids)
    result = connection.execute(
        sa.text(
            f"UPDATE warehouse_locations SET warehouse_type=:previous_type "
            f"WHERE id IN ({placeholders}) AND warehouse_type=:target_type"
        ),
        {
            "previous_type": PREVIOUS_WAREHOUSE_TYPE,
            "target_type": TARGET_WAREHOUSE_TYPE,
        },
    )
    if result.rowcount != len(ids):
        raise RuntimeError("P0-25 downgrade failed: RAW-001 rows drifted during restore.")
    connection.execute(
        sa.text(f"DELETE FROM {SNAPSHOT_TABLE} WHERE migration_key=:key"),
        {"key": MIGRATION_KEY},
    )
    if _fingerprint(connection) != payload["pre_fingerprint"]:
        raise RuntimeError("P0-25 downgrade did not restore the exact pre-migration facts.")
    return True
