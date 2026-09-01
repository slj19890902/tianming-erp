"""Guarded activation of the existing F34/F12 temporary turnover anchors."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256
import json
from typing import Any

import sqlalchemy as sa


MIGRATION_KEY = "p0-33-liner-finished-temporary-turnover"
SNAPSHOT_TABLE = "warehouse_current_map_migration_snapshots"
MAP_REVISION = "3994317ae14a7f18"
MIGRATED_AT = "2026-09-01 00:00:00"
TARGETS = {
    "F34": {
        "count": 3,
        "feature_id": "d068d43e-58a5-40d8-bb7f-0ea88d714e2e",
    },
    "F12": {
        "count": 8,
        "feature_id": "0a1c6bf6-c0d9-4217-b9a0-2527db20b9d9",
    },
}
TARGET_LOCATION_CODES = tuple(
    [*(f"F34-P{number:02d}" for number in range(1, 4))]
    + [*(f"F12-P{number:02d}" for number in range(1, 9))]
)
TURNOVER_REMARK = (
    "P0-33：衬板直接成品临时周转；每位置仅一块当前栈板，必须待后续归位。"
)


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    return value


def _rows(
    connection: sa.Connection,
    statement: str,
    params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    return [
        {str(key): _json_value(value) for key, value in row.items()}
        for row in connection.execute(
            sa.text(statement), params or {}
        ).mappings().all()
    ]


def _scalar(connection: sa.Connection, statement: str) -> int:
    return int(connection.execute(sa.text(statement)).scalar() or 0)


def _code_filter() -> str:
    return ",".join(f":code_{index}" for index in range(len(TARGET_LOCATION_CODES)))


def _code_params() -> dict[str, str]:
    return {
        f"code_{index}": code
        for index, code in enumerate(TARGET_LOCATION_CODES)
    }


def _target_rows(connection: sa.Connection) -> list[dict[str, Any]]:
    return _rows(
        connection,
        f"""
        SELECT location.id,location.location_code,location.location_name,
               location.warehouse_type,location.is_active,location.remarks,
               location.warehouse_floor,location.area_code,
               location.storage_type,location.is_temporary,
               location.source_version,location.address_kind,
               location.address_area_id,location.placement_status,
               location.updated_at,
               area.id AS area_id,area.construction_status AS area_status,
               area.capacity_review_status,area.capacity_eligible,
               area.confirmed_pallet_capacity,
               floor.construction_status AS floor_status,
               policy.status AS policy_status,
               policy.allowed_inventory_types_json,
               policy.storage_layout AS policy_storage_layout,
               policy.map_feature_id,policy.published_map_revision,
               layout.id AS layout_id,layout.layout_kind,layout.version AS layout_version,
               (SELECT COUNT(*)
                  FROM warehouse_ground_layout_slots ground_slot
                  JOIN warehouse_ground_layout_plans ground_plan
                    ON ground_plan.id=ground_slot.plan_id
                 WHERE ground_slot.location_id=location.id
                   AND ground_plan.status='published') AS published_ground_slot_count
        FROM warehouse_locations location
        JOIN warehouse_floors floor
          ON floor.floor_number=location.warehouse_floor
        JOIN warehouse_areas area
          ON area.floor_id=floor.id
         AND upper(area.area_code)=upper(location.area_code)
        JOIN warehouse_area_storage_policies policy ON policy.area_id=area.id
        LEFT JOIN floor3_location_layouts layout ON layout.location_id=location.id
        WHERE location.location_code IN ({_code_filter()})
        ORDER BY CASE upper(location.area_code) WHEN 'F34' THEN 0 ELSE 1 END,
                 location.location_code,location.id
        """,
        _code_params(),
    )


_STRUCTURAL_LOCATION_REFERENCE_TABLES = frozenset(
    {
        "floor3_location_layouts",
        "warehouse_location_aliases",
        "warehouse_location_address_mutations",
    }
)


def _reference_rows(
    connection: sa.Connection,
    location_ids: list[int],
) -> dict[str, list[dict[str, Any]]]:
    if not location_ids:
        return {}
    inspector = sa.inspect(connection)
    ids = ",".join(str(value) for value in location_ids)
    quote = connection.dialect.identifier_preparer.quote
    references: dict[str, list[dict[str, Any]]] = {}
    for table_name in sorted(inspector.get_table_names()):
        if table_name in _STRUCTURAL_LOCATION_REFERENCE_TABLES:
            continue
        location_columns: list[str] = []
        for foreign_key in inspector.get_foreign_keys(table_name):
            if foreign_key.get("referred_table") != "warehouse_locations":
                continue
            for local_column, remote_column in zip(
                foreign_key.get("constrained_columns") or (),
                foreign_key.get("referred_columns") or (),
                strict=False,
            ):
                if remote_column == "id":
                    location_columns.append(str(local_column))
        if not location_columns:
            continue
        conditions = " OR ".join(
            f"{quote(column)} IN ({ids})" for column in sorted(set(location_columns))
        )
        primary_columns = list(
            (inspector.get_pk_constraint(table_name) or {}).get(
                "constrained_columns"
            )
            or ()
        )
        order_columns = primary_columns or sorted(set(location_columns))
        order_by = ",".join(quote(column) for column in order_columns)
        references[table_name] = _rows(
            connection,
            f"SELECT * FROM {quote(table_name)} WHERE {conditions} ORDER BY {order_by}",
        )
    return references


def _target_document(connection: sa.Connection) -> dict[str, Any]:
    locations = _target_rows(connection)
    return {
        "locations": locations,
        "references": _reference_rows(
            connection, [int(row["id"]) for row in locations]
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
        raise RuntimeError("P0-33 preflight failed: current-map snapshot table is missing.")
    rows = _target_rows(connection)
    if not rows:
        snapshot_count = _scalar(connection, f"SELECT COUNT(*) FROM {SNAPSHOT_TABLE}")
        business_count = _scalar(
            connection,
            "SELECT (SELECT COUNT(*) FROM sales_orders) + "
            "(SELECT COUNT(*) FROM inventory_lots) + "
            "(SELECT COUNT(*) FROM inventory_pallets)",
        )
        if snapshot_count == 0 and business_count == 0:
            return None
        raise RuntimeError(
            "P0-33 preflight failed: formal F34/F12 temporary locations are missing."
        )
    if {str(row["location_code"]) for row in rows} != set(TARGET_LOCATION_CODES):
        raise RuntimeError("P0-33 preflight failed: F34/F12 stable identities drifted.")
    area_counts = {
        area_code: sum(
            str(row["area_code"] or "").upper() == area_code for row in rows
        )
        for area_code in TARGETS
    }
    if any(area_counts[code] != int(profile["count"]) for code, profile in TARGETS.items()):
        raise RuntimeError("P0-33 preflight failed: F34/F12 location counts drifted.")
    for row in rows:
        area_code = str(row["area_code"] or "").upper()
        profile = TARGETS.get(area_code)
        try:
            allowed_types = json.loads(str(row["allowed_inventory_types_json"] or ""))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError(
                "P0-33 preflight failed: F34/F12 storage policy is invalid."
            ) from error
        valid = bool(
            profile
            and int(row["warehouse_floor"] or 0) == 3
            and row["warehouse_type"] in {"finished", "shared"}
            and row["storage_type"] == "temporary_aisle"
            and int(row["is_temporary"] or 0) == 1
            and int(row["is_active"] or 0) == 0
            and row["source_version"] == "CURRENT_MAP"
            and row["address_kind"] == "functional"
            and int(row["address_area_id"] or 0) == int(row["area_id"] or 0)
            and row["placement_status"] == "placed"
            and row["floor_status"] == "enabled"
            and row["area_status"] == "enabled"
            and row["capacity_review_status"] == "confirmed"
            and int(row["capacity_eligible"] or 0) == 1
            and int(row["confirmed_pallet_capacity"] or 0)
            == int(profile["count"])
            and row["policy_status"] == "published"
            and row["policy_storage_layout"] == "pallet_ground"
            and row["map_feature_id"] == profile["feature_id"]
            and row["published_map_revision"] == MAP_REVISION
            and isinstance(allowed_types, list)
            and "finished" in {str(value).strip() for value in allowed_types}
            and row["layout_id"] is not None
            and row["layout_kind"] == "logical_anchor"
            and int(row["published_ground_slot_count"] or 0) == 0
        )
        if not valid:
            raise RuntimeError(
                "P0-33 preflight failed: F34/F12 map, policy, capacity, or anchor facts drifted."
            )
    references = _reference_rows(connection, [int(row["id"]) for row in rows])
    if any(references.values()):
        raise RuntimeError(
            "P0-33 preflight failed: F34/F12 already have inventory or business references."
        )
    return rows


def upgrade_liner_finished_turnover(connection: sa.Connection) -> bool:
    existing = _existing_snapshot(connection)
    if existing is not None:
        if _fingerprint(connection) != existing["post_fingerprint"]:
            raise RuntimeError(
                "P0-33 existing migration snapshot does not match current F34/F12 facts."
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
    result = connection.execute(
        sa.text(
            f"UPDATE warehouse_locations SET is_active=1,is_temporary=1,"
            f"remarks=:remarks,updated_at=:updated_at "
            f"WHERE location_code IN ({_code_filter()}) AND is_active=0"
        ),
        {
            **_code_params(),
            "remarks": TURNOVER_REMARK,
            "updated_at": MIGRATED_AT,
        },
    )
    if result.rowcount != len(TARGET_LOCATION_CODES):
        raise RuntimeError("P0-33 activation row count changed; transaction stopped.")
    post_fingerprint = _fingerprint(connection)
    connection.execute(
        sa.text(
            f"UPDATE {SNAPSHOT_TABLE} SET post_fingerprint=:fingerprint "
            "WHERE migration_key=:key AND post_fingerprint='PENDING'"
        ),
        {"fingerprint": post_fingerprint, "key": MIGRATION_KEY},
    )
    return True


def downgrade_liner_finished_turnover(connection: sa.Connection) -> bool:
    snapshot = _existing_snapshot(connection)
    if snapshot is None:
        return False
    if _fingerprint(connection) != snapshot["post_fingerprint"]:
        raise RuntimeError(
            "Refusing P0-33 downgrade: F34/F12 have changed or gained business references."
        )
    payload = json.loads(str(snapshot["payload_json"]))
    for row in payload["locations"]:
        connection.execute(
            sa.text(
                "UPDATE warehouse_locations SET is_active=:is_active,"
                "is_temporary=:is_temporary,remarks=:remarks,updated_at=:updated_at "
                "WHERE id=:id AND location_code=:location_code"
            ),
            {
                "id": int(row["id"]),
                "location_code": row["location_code"],
                "is_active": int(row["is_active"] or 0),
                "is_temporary": int(row["is_temporary"] or 0),
                "remarks": row["remarks"],
                "updated_at": row["updated_at"],
            },
        )
    if _fingerprint(connection) != payload["pre_fingerprint"]:
        raise RuntimeError(
            "Refusing P0-33 downgrade: the pre-migration F34/F12 profile was not restored."
        )
    connection.execute(
        sa.text(f"DELETE FROM {SNAPSHOT_TABLE} WHERE migration_key=:key"),
        {"key": MIGRATION_KEY},
    )
    return True
