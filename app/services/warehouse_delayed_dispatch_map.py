from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256
import json
from typing import Any

import sqlalchemy as sa

from app.services.warehouse_current_map_baseline import _fingerprint


MIGRATION_KEY = "p1-107-delayed-dispatch-left-area-3994317ae14a7f18"
PREVIOUS_MAP_REVISION = "e5f192ba605185db"
MAP_REVISION = "3994317ae14a7f18"
MIGRATED_AT = "2026-08-27 12:00:00"
SNAPSHOT_TABLE = "warehouse_current_map_migration_snapshots"
TARGET_AREA_ID = 40
TARGET_AREA_CODE = "SEMI-008"
TARGET_AREA_NAME = "左区·延期待送周转区"
TARGET_FEATURE_ID = "26849258-36f1-4620-815d-29d50e683578"
TARGET_ALLOWED_TYPES = ["finished", "semi_finished"]
TARGET_LOCATION_COUNT = 20


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
        for row in connection.execute(sa.text(statement), params or {}).mappings().all()
    ]


def _scalar(
    connection: sa.Connection,
    statement: str,
    params: dict[str, Any] | None = None,
) -> int:
    return int(connection.execute(sa.text(statement), params or {}).scalar_one() or 0)


def _table_exists(connection: sa.Connection, table_name: str) -> bool:
    return bool(sa.inspect(connection).has_table(table_name))


def _immutable_plan_triggers(connection: sa.Connection) -> list[dict[str, Any]]:
    if connection.dialect.name != "sqlite":
        return []
    return _rows(
        connection,
        """
        SELECT name,sql FROM sqlite_master
        WHERE type='trigger' AND tbl_name='warehouse_ground_layout_plans'
        ORDER BY name
        """,
    )


def _drop_triggers(connection: sa.Connection, triggers: list[dict[str, Any]]) -> None:
    for trigger in triggers:
        name = str(trigger["name"]).replace('"', '""')
        connection.execute(sa.text(f'DROP TRIGGER "{name}"'))


def _restore_triggers(connection: sa.Connection, triggers: list[dict[str, Any]]) -> None:
    for trigger in triggers:
        if trigger.get("sql"):
            connection.execute(sa.text(str(trigger["sql"])))


def _target_live_fact_count(connection: sa.Connection) -> int:
    params = {"area_code": TARGET_AREA_CODE, "area_id": TARGET_AREA_ID}
    current_pallets = _scalar(
        connection,
        """
        SELECT COUNT(*)
        FROM inventory_pallets p
        JOIN warehouse_locations l ON l.id=p.location_id
        WHERE p.is_current=1 AND p.status='active'
          AND (l.area_code=:area_code OR l.address_area_id=:area_id)
        """,
        params,
    )
    positive_lots = _scalar(
        connection,
        """
        SELECT COUNT(*)
        FROM inventory_lots x
        JOIN warehouse_locations l ON l.id=x.warehouse_location_id
        WHERE x.status IN ('active','frozen')
          AND x.quantity_available+x.quantity_reserved+x.quantity_damaged>0
          AND (l.area_code=:area_code OR l.address_area_id=:area_id)
        """,
        params,
    )
    active_occupancies = _scalar(
        connection,
        """
        SELECT COUNT(*)
        FROM warehouse_ground_occupancy_slots s
        JOIN warehouse_locations l ON l.id=s.location_id
        WHERE s.status='active'
          AND (l.area_code=:area_code OR l.address_area_id=:area_id)
        """,
        params,
    )
    return current_pallets + positive_lots + active_occupancies


def _preflight(connection: sa.Connection) -> bool:
    if not _table_exists(connection, "warehouse_areas"):
        return False
    area = connection.execute(
        sa.text(
            """
            SELECT a.id,a.area_code,a.area_name,a.confirmed_pallet_capacity,
                   a.construction_status,f.floor_number
            FROM warehouse_areas a
            JOIN warehouse_floors f ON f.id=a.floor_id
            WHERE a.id=:area_id
            """
        ),
        {"area_id": TARGET_AREA_ID},
    ).mappings().one_or_none()
    if area is None:
        business_fact_count = _scalar(
            connection,
            "SELECT (SELECT COUNT(*) FROM inventory_lots) + "
            "(SELECT COUNT(*) FROM inventory_pallets)",
        )
        if business_fact_count == 0:
            return False
        raise RuntimeError("P1-107 preflight failed: SEMI-008 formal area is missing.")
    if (
        str(area["area_code"] or "").upper() != TARGET_AREA_CODE
        or int(area["floor_number"] or 0) != 3
        or area["construction_status"] != "enabled"
        or int(area["confirmed_pallet_capacity"] or 0) != TARGET_LOCATION_COUNT
    ):
        raise RuntimeError("P1-107 preflight failed: SEMI-008 identity or measured capacity drifted.")
    if not _table_exists(connection, SNAPSHOT_TABLE):
        raise RuntimeError("P1-107 preflight failed: current-map snapshot table is missing.")
    if _scalar(
        connection,
        f"SELECT COUNT(*) FROM {SNAPSHOT_TABLE} WHERE migration_key=:key",
        {"key": "p0-26-current-map-e5f192ba605185db"},
    ) != 1:
        raise RuntimeError("P1-107 preflight failed: P0-26 current-map snapshot is missing.")
    policy = connection.execute(
        sa.text(
            """
            SELECT map_feature_id,allowed_inventory_types_json,storage_layout,
                   status,draft_map_revision,published_map_revision
            FROM warehouse_area_storage_policies
            WHERE area_id=:area_id
            """
        ),
        {"area_id": TARGET_AREA_ID},
    ).mappings().one_or_none()
    try:
        current_types = json.loads(str(policy["allowed_inventory_types_json"])) if policy else None
    except (TypeError, ValueError, json.JSONDecodeError):
        current_types = None
    if (
        policy is None
        or policy["map_feature_id"] != TARGET_FEATURE_ID
        or current_types != ["semi_finished"]
        or policy["storage_layout"] != "pallet_ground"
        or policy["status"] != "published"
        or policy["published_map_revision"] != PREVIOUS_MAP_REVISION
    ):
        raise RuntimeError("P1-107 preflight failed: SEMI-008 published policy drifted.")
    location_count = _scalar(
        connection,
        """
        SELECT COUNT(*) FROM warehouse_locations
        WHERE address_area_id=:area_id AND area_code=:area_code
          AND warehouse_floor=3 AND source_version='CURRENT_MAP'
          AND is_active=1 AND address_kind='ground_slot'
        """,
        {"area_id": TARGET_AREA_ID, "area_code": TARGET_AREA_CODE},
    )
    physical_slot_count = _scalar(
        connection,
        """
        SELECT COUNT(*)
        FROM warehouse_ground_layout_plans p
        JOIN warehouse_ground_layout_slots s ON s.plan_id=p.id
        JOIN warehouse_locations l ON l.id=s.location_id
        WHERE p.area_id=:area_id AND p.status='published'
          AND p.target_slot_count=:target_count
          AND p.published_map_revision=:revision
          AND l.address_area_id=:area_id
        """,
        {
            "area_id": TARGET_AREA_ID,
            "target_count": TARGET_LOCATION_COUNT,
            "revision": PREVIOUS_MAP_REVISION,
        },
    )
    if location_count != TARGET_LOCATION_COUNT or physical_slot_count != TARGET_LOCATION_COUNT:
        raise RuntimeError("P1-107 preflight failed: SEMI-008 physical 20-slot plan drifted.")
    stale_policies = _scalar(
        connection,
        """
        SELECT COUNT(*)
        FROM warehouse_area_storage_policies p
        JOIN warehouse_areas a ON a.id=p.area_id
        JOIN warehouse_floors f ON f.id=a.floor_id
        WHERE f.floor_number=3 AND p.status='published'
          AND p.published_map_revision<>:revision
        """,
        {"revision": PREVIOUS_MAP_REVISION},
    )
    stale_plans = _scalar(
        connection,
        """
        SELECT COUNT(*)
        FROM warehouse_ground_layout_plans p
        JOIN warehouse_areas a ON a.id=p.area_id
        JOIN warehouse_floors f ON f.id=a.floor_id
        WHERE f.floor_number=3 AND p.status='published'
          AND p.published_map_revision<>:revision
        """,
        {"revision": PREVIOUS_MAP_REVISION},
    )
    if stale_policies or stale_plans:
        raise RuntimeError("P1-107 preflight failed: published three-floor map revisions drifted.")
    if _target_live_fact_count(connection):
        raise RuntimeError("P1-107 preflight failed: SEMI-008 is no longer physically empty.")
    return True


def _snapshot(connection: sa.Connection) -> dict[str, Any]:
    existing = connection.execute(
        sa.text(
            f"SELECT payload_json,post_fingerprint FROM {SNAPSHOT_TABLE} "
            "WHERE migration_key=:key"
        ),
        {"key": MIGRATION_KEY},
    ).mappings().one_or_none()
    if existing is not None:
        if _fingerprint(connection) != existing["post_fingerprint"]:
            raise RuntimeError("P1-107 existing migration snapshot does not match current state.")
        return json.loads(str(existing["payload_json"]))
    payload = {
        "migration_key": MIGRATION_KEY,
        "captured_at": MIGRATED_AT,
        "pre_fingerprint": _fingerprint(connection),
        "tables": {
            "warehouse_areas": _rows(
                connection,
                "SELECT * FROM warehouse_areas WHERE id=:area_id",
                {"area_id": TARGET_AREA_ID},
            ),
            "warehouse_area_storage_policies": _rows(
                connection,
                """
                SELECT p.* FROM warehouse_area_storage_policies p
                JOIN warehouse_areas a ON a.id=p.area_id
                JOIN warehouse_floors f ON f.id=a.floor_id
                WHERE f.floor_number=3 AND p.status='published'
                ORDER BY p.id
                """,
            ),
            "warehouse_ground_layout_plans": _rows(
                connection,
                """
                SELECT p.* FROM warehouse_ground_layout_plans p
                JOIN warehouse_areas a ON a.id=p.area_id
                JOIN warehouse_floors f ON f.id=a.floor_id
                WHERE f.floor_number=3 AND p.status='published'
                ORDER BY p.id
                """,
            ),
            "warehouse_locations": _rows(
                connection,
                "SELECT * FROM warehouse_locations WHERE address_area_id=:area_id ORDER BY id",
                {"area_id": TARGET_AREA_ID},
            ),
        },
    }
    connection.execute(
        sa.text(
            f"INSERT INTO {SNAPSHOT_TABLE} "
            "(migration_key,payload_json,post_fingerprint,created_at) "
            "VALUES (:key,:payload,'PENDING',:created_at)"
        ),
        {
            "key": MIGRATION_KEY,
            "payload": json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            "created_at": MIGRATED_AT,
        },
    )
    return payload


def _validate_post_state(connection: sa.Connection) -> None:
    failures = []
    area = connection.execute(
        sa.text("SELECT area_name,confirmed_pallet_capacity FROM warehouse_areas WHERE id=:id"),
        {"id": TARGET_AREA_ID},
    ).one()
    if area[0] != TARGET_AREA_NAME or int(area[1] or 0) != TARGET_LOCATION_COUNT:
        failures.append("左区名称或20个实测栈板位容量不正确")
    policy = connection.execute(
        sa.text(
            "SELECT allowed_inventory_types_json,published_map_revision "
            "FROM warehouse_area_storage_policies WHERE area_id=:id"
        ),
        {"id": TARGET_AREA_ID},
    ).one()
    try:
        policy_types = json.loads(str(policy[0]))
    except (TypeError, ValueError, json.JSONDecodeError):
        policy_types = None
    if policy_types != TARGET_ALLOWED_TYPES or policy[1] != MAP_REVISION:
        failures.append("左区成品/半成品共享策略或地图版本不正确")
    shared_locations = _scalar(
        connection,
        """
        SELECT COUNT(*) FROM warehouse_locations
        WHERE address_area_id=:area_id AND warehouse_type='shared'
          AND is_active=1 AND source_version='CURRENT_MAP'
        """,
        {"area_id": TARGET_AREA_ID},
    )
    if shared_locations != TARGET_LOCATION_COUNT:
        failures.append("左区20个稳定库位未全部切换为共享用途")
    stale_policies = _scalar(
        connection,
        """
        SELECT COUNT(*) FROM warehouse_area_storage_policies p
        JOIN warehouse_areas a ON a.id=p.area_id
        JOIN warehouse_floors f ON f.id=a.floor_id
        WHERE f.floor_number=3 AND p.status='published'
          AND p.published_map_revision<>:revision
        """,
        {"revision": MAP_REVISION},
    )
    stale_plans = _scalar(
        connection,
        """
        SELECT COUNT(*) FROM warehouse_ground_layout_plans p
        JOIN warehouse_areas a ON a.id=p.area_id
        JOIN warehouse_floors f ON f.id=a.floor_id
        WHERE f.floor_number=3 AND p.status='published'
          AND p.published_map_revision<>:revision
        """,
        {"revision": MAP_REVISION},
    )
    if stale_policies or stale_plans:
        failures.append("三楼发布策略或地堆排位仍引用旧地图版本")
    if _target_live_fact_count(connection):
        failures.append("迁移意外向左区写入了栈板、库存或地堆占用")
    if failures:
        raise RuntimeError("P1-107 post validation failed: " + "；".join(failures))


def upgrade_delayed_dispatch_map(connection: sa.Connection) -> bool:
    if _table_exists(connection, SNAPSHOT_TABLE):
        existing = connection.execute(
            sa.text(
                f"SELECT post_fingerprint FROM {SNAPSHOT_TABLE} WHERE migration_key=:key"
            ),
            {"key": MIGRATION_KEY},
        ).mappings().one_or_none()
        if existing is not None:
            if _fingerprint(connection) != existing["post_fingerprint"]:
                raise RuntimeError("P1-107 existing migration snapshot does not match current state.")
            return False
    if not _preflight(connection):
        return False
    snapshot = _snapshot(connection)
    connection.execute(
        sa.text(
            """
            UPDATE warehouse_areas
            SET area_name=:name,updated_at=:updated_at,address_version=address_version+1
            WHERE id=:area_id AND area_code=:area_code
            """
        ),
        {
            "name": TARGET_AREA_NAME,
            "updated_at": MIGRATED_AT,
            "area_id": TARGET_AREA_ID,
            "area_code": TARGET_AREA_CODE,
        },
    )
    connection.execute(
        sa.text(
            """
            UPDATE warehouse_locations
            SET warehouse_type='shared',updated_at=:updated_at,
                address_version=address_version+1
            WHERE address_area_id=:area_id AND area_code=:area_code
              AND warehouse_floor=3 AND is_active=1
            """
        ),
        {
            "updated_at": MIGRATED_AT,
            "area_id": TARGET_AREA_ID,
            "area_code": TARGET_AREA_CODE,
        },
    )
    connection.execute(
        sa.text(
            """
            UPDATE warehouse_area_storage_policies
            SET allowed_inventory_types_json=:types
            WHERE area_id=:area_id
            """
        ),
        {
            "types": json.dumps(TARGET_ALLOWED_TYPES, ensure_ascii=False, separators=(",", ":")),
            "area_id": TARGET_AREA_ID,
        },
    )
    connection.execute(
        sa.text(
            """
            UPDATE warehouse_area_storage_policies
            SET draft_map_revision=:revision,published_map_revision=:revision,
                version=version+1,updated_by=1,updated_at=:updated_at
            WHERE id IN (
              SELECT p.id FROM warehouse_area_storage_policies p
              JOIN warehouse_areas a ON a.id=p.area_id
              JOIN warehouse_floors f ON f.id=a.floor_id
              WHERE f.floor_number=3 AND p.status='published'
            )
            """
        ),
        {"revision": MAP_REVISION, "updated_at": MIGRATED_AT},
    )
    plan_triggers = _immutable_plan_triggers(connection)
    _drop_triggers(connection, plan_triggers)
    try:
        connection.execute(
            sa.text(
                """
                UPDATE warehouse_ground_layout_plans
                SET draft_map_revision=:revision,published_map_revision=:revision,
                    version=version+1,updated_by=1,updated_at=:updated_at
                WHERE id IN (
                  SELECT p.id FROM warehouse_ground_layout_plans p
                  JOIN warehouse_areas a ON a.id=p.area_id
                  JOIN warehouse_floors f ON f.id=a.floor_id
                  WHERE f.floor_number=3 AND p.status='published'
                )
                """
            ),
            {"revision": MAP_REVISION, "updated_at": MIGRATED_AT},
        )
    finally:
        _restore_triggers(connection, plan_triggers)
    _validate_post_state(connection)
    fingerprint = _fingerprint(connection)
    connection.execute(
        sa.text(
            f"UPDATE {SNAPSHOT_TABLE} SET payload_json=:payload,post_fingerprint=:fingerprint "
            "WHERE migration_key=:key"
        ),
        {
            "payload": json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            "fingerprint": fingerprint,
            "key": MIGRATION_KEY,
        },
    )
    return True


def _restore_rows(
    connection: sa.Connection,
    table_name: str,
    rows: list[dict[str, Any]],
) -> None:
    if not rows:
        return
    columns = [str(row[1]) for row in connection.execute(sa.text(f'PRAGMA table_info("{table_name}")')).all()]
    assignments = ",".join(f'"{column}"=:{column}' for column in columns if column != "id")
    statement = sa.text(f'UPDATE "{table_name}" SET {assignments} WHERE id=:id')
    for row in rows:
        connection.execute(statement, {column: row.get(column) for column in columns})


def downgrade_delayed_dispatch_map(connection: sa.Connection) -> bool:
    if not _table_exists(connection, SNAPSHOT_TABLE):
        return False
    row = connection.execute(
        sa.text(
            f"SELECT payload_json,post_fingerprint FROM {SNAPSHOT_TABLE} "
            "WHERE migration_key=:key"
        ),
        {"key": MIGRATION_KEY},
    ).mappings().one_or_none()
    if row is None:
        return False
    if _target_live_fact_count(connection):
        raise RuntimeError(
            "Refusing P1-107 downgrade: SEMI-008 contains live inventory after migration. "
            "Move it through the audited warehouse flow before changing map semantics."
        )
    if _fingerprint(connection) != row["post_fingerprint"]:
        raise RuntimeError(
            "Refusing P1-107 downgrade: warehouse or inventory facts changed after migration. "
            "Create a new verified backup and mapping plan instead of discarding live facts."
        )
    payload = json.loads(str(row["payload_json"]))
    plan_triggers = _immutable_plan_triggers(connection)
    _drop_triggers(connection, plan_triggers)
    try:
        for table_name in (
            "warehouse_areas",
            "warehouse_area_storage_policies",
            "warehouse_ground_layout_plans",
            "warehouse_locations",
        ):
            _restore_rows(connection, table_name, payload["tables"][table_name])
    finally:
        _restore_triggers(connection, plan_triggers)
    connection.execute(
        sa.text(f"DELETE FROM {SNAPSHOT_TABLE} WHERE migration_key=:key"),
        {"key": MIGRATION_KEY},
    )
    if _fingerprint(connection) != payload["pre_fingerprint"]:
        raise RuntimeError("P1-107 downgrade did not restore the exact pre-migration state.")
    return True
