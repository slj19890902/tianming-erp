from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256
import json
import math
from typing import Any

import sqlalchemy as sa


MIGRATION_KEY = "p0-26-current-map-e5f192ba605185db"
MAP_REVISION = "e5f192ba605185db"
MIGRATED_SOURCE_VERSION = "CURRENT_MAP"
MIGRATED_AT = "2026-08-26 21:30:00"
SNAPSHOT_TABLE = "warehouse_current_map_migration_snapshots"


# area_id: (area_code, name, capacity, feature_id, storage_layout, inventory types)
AREA_POLICIES: dict[int, tuple[str, str, int | None, str, str, list[str]]] = {
    1: ("A1", "A1 成品存放区（主通道北侧）", 10, "3342ab3d-b489-41d2-8b48-a11563ef5b94", "pallet_ground", ["finished"]),
    2: ("A2", "A2 成品存放区（主通道南侧）", 20, "9617506b-9c95-449a-9c76-c5f87d192238", "pallet_ground", ["finished"]),
    3: ("AB1", "AB1 成品存放区", 1, "bbf91868-304c-4bf6-8475-4b7c7a460b1a", "pallet_ground", ["finished"]),
    4: ("AB2", "AB2 成品存放区", 2, "8d9ada1a-06e0-4f42-9154-a73565d42a43", "pallet_ground", ["finished"]),
    5: ("B1", "B1 成品存放区（主通道北侧）", 14, "63f9b124-89b4-42ad-b476-7891228f7e5d", "pallet_ground", ["finished"]),
    6: ("B2", "B2 成品存放区（主通道南侧）", 23, "2a25e98b-7ccf-4882-b574-2565c1fb2b9e", "pallet_ground", ["finished"]),
    7: ("C1", "C1 成品存放区（主通道北侧）", 24, "b78bad82-b86f-43fb-adb7-88e7cb551da4", "pallet_ground", ["finished"]),
    8: ("C2", "C2 成品存放区（主通道南侧）", 33, "97f9c9d3-5c6f-42c8-94fa-9d13b82c3e2a", "pallet_ground", ["finished"]),
    9: ("CD1", "CD1 成品存放区", 14, "3f473721-94ca-4c36-8c98-5d2e6448d279", "pallet_ground", ["finished"]),
    10: ("D1", "D1 栈板成品存放区（无货架）", 28, "a2ec14a6-ecda-40c1-9eef-177339e50aed", "pallet_ground", ["finished"]),
    11: ("D2", "D2 货架为主·栈板混合存放区", 8, "3b662b8e-a457-4544-9ccb-98dacdafd835", "mixed", ["finished"]),
    12: ("DE1", "DE1 成品存放区", 5, "58068eb6-d94b-4e24-9173-a16146806303", "pallet_ground", ["finished"]),
    13: ("E1", "E1 成品存放区（主通道北侧）", 27, "cd40e190-223f-4407-a586-e79b323941ad", "pallet_ground", ["finished"]),
    14: ("E2", "E2 成品存放区（主通道南侧）", 33, "0d284916-63cd-4a03-85ef-1e93e36725c5", "pallet_ground", ["finished"]),
    15: ("E3", "E3 成品存放区", 6, "c5302a99-abb0-427d-879d-30153e5ebf54", "pallet_ground", ["finished"]),
    16: ("FIN-LOOSE-001", "送货剩余零散库存暂存区（无栈板）", None, "c399826c-0049-41bc-a687-04993152608f", "functional", ["finished"]),
    17: ("F1", "F1 两排2货架区（货架可独立移动）", 12, "a9825b6e-60f6-455d-9e09-db91f0ec5ff6", "rack", ["finished"]),
    18: ("F12", "F1/F2之间临时周转区", 8, "0a1c6bf6-c0d9-4217-b9a0-2527db20b9d9", "pallet_ground", ["finished"]),
    19: ("F2", "F2 货架区", 30, "289e5e49-a3b5-4661-ad9f-a493452f967b", "rack", ["finished"]),
    20: ("F3", "F3 货架区", 30, "8909683d-a64e-4af1-8089-c53b33532c31", "rack", ["finished"]),
    21: ("F34", "F3/F4之间临时周转区", 3, "d068d43e-58a5-40d8-bb7f-0ea88d714e2e", "pallet_ground", ["finished"]),
    22: ("F4", "F4 货架区（双排1100mm）", 30, "89c04b37-18c1-46a5-8998-b3913f7e9ea4", "rack", ["finished"]),
    25: ("RAW-001", "左区L3 原料区（上段）", None, "98ddeb13-805f-4a63-83e1-120ebf38b27f", "rack", ["raw_material"]),
    26: ("FG-004", "成品存放（可临时混放）堆放区", 12, "36330234-afbf-4e25-979f-19b835d81541", "pallet_ground", ["finished", "semi_finished", "raw_material"]),
    27: ("SEMI-006", "左区L5 半成品区（西中整合）", 12, "8c9b4318-8697-4f89-befb-427e2c8982d2", "pallet_ground", ["semi_finished"]),
    35: ("FG-009", "成品存放（可临时混放）堆放区", 19, "700d8903-5387-45ef-b5e8-9593cbd31c03", "pallet_ground", ["finished"]),
    36: ("SEMI-010", "左区L7 半成品区（中部整合）", 30, "a939e56e-f616-4048-bc4c-28127304fb3f", "pallet_ground", ["semi_finished"]),
    37: ("FG-005", "成品存放（可临时混放）堆放区", 15, "3259069c-6462-43e4-9293-c33f8b7b765f", "pallet_ground", ["finished"]),
    38: ("FG-006", "成品存放（可临时混放）堆放区", 10, "94888628-fe97-442d-9e22-2609be0d296e", "pallet_ground", ["finished"]),
    39: ("FG-007", "成品存放（可临时混放）堆放区", 10, "0d579673-47c9-47df-9a80-a281526752c1", "pallet_ground", ["finished"]),
    40: ("SEMI-008", "左区L9 半成品区（最南整合）", 20, "26849258-36f1-4620-815d-29d50e683578", "pallet_ground", ["semi_finished"]),
    41: ("RAW-004", "左区L8 原料区（南侧）", 10, "210984c6-23ef-4784-afc1-82162b758993", "pallet_ground", ["raw_material"]),
    48: ("SEMI-011", "SEMI-011 半成品堆放区", 4, "502a6fbf-c5c2-486c-bd42-e7e96320c135", "pallet_ground", ["semi_finished"]),
    56: ("FG-008", "成品存放（可临时混放）堆放区", 5, "d89b19bb-a899-4949-98ef-fdc11e7c8c30", "pallet_ground", ["finished"]),
}


MEASURED_BOUNDS: dict[int, tuple[float, float, float, float]] = {
    1: (30229.0, -3678.0, 3100.0, 7430.0),
    2: (30229.0, -19615.5, 3100.0, 14000.0),
    3: (25929.0, -25750.0, 2600.0, 4100.0),
    4: (25929.0, -28465.0, 2600.0, 1600.0),
    5: (25729.0, -3698.5, 3000.0, 10800.0),
    6: (25729.0, -19644.0, 3000.0, 14000.0),
    7: (20729.0, -3870.0, 3500.0, 13700.0),
    8: (20729.0, -19681.5, 3500.0, 14000.0),
    9: (17629.0, 10507.5, 6600.0, 4000.0),
    10: (16679.0, -5042.0, 2550.0, 15000.0),
    11: (16629.0, -19600.0, 2600.0, 14000.0),
    12: (8879.0, 9142.0, 6300.0, 1200.0),
    13: (10179.0, -4374.5, 5000.0, 12000.0),
    14: (10129.0, -19652.5, 5000.0, 14000.0),
    15: (11229.0, -25460.5, 2800.0, 4000.0),
    48: (9979.0, -27929.0, 5300.0, 1200.0),
}


# Bounds of already confirmed current-map zones.  Their existing physical
# layouts are preserved; logical placeholder layouts are repacked below.
CURRENT_BOUNDS: dict[int, tuple[float, float, float, float]] = {
    27: (-23636.0, -14987.0, 10384.0, 4800.0),
    35: (-20778.0, -25457.0, 7632.0, 8989.0),
    40: (-16610.0, -30014.0, 14142.0, 4562.0),
    56: (-23650.0, -20520.0, 2892.0, 5527.0),
}


# Only owner-measured A-E/SEMI-011 rectangles and current zones whose layouts
# already represent exact 1200x1000 footprints become selectable pallet plans.
# Logical placeholders remain current-map source anchors until measured; the
# migration must not invent physical slots from a zone's bounding box.
GROUND_AREA_IDS = set(range(1, 16)) | {27, 35, 40, 48, 56}
RACK_AREA_IDS = {17, 19, 20, 22, 25}


SNAPSHOT_TABLES = (
    "warehouse_areas",
    "warehouse_area_storage_policies",
    "warehouse_locations",
    "floor3_location_layouts",
    "inventory_pallets",
    "inventory_lots",
)
FINGERPRINT_TABLES = SNAPSHOT_TABLES + (
    "warehouse_ground_layout_plans",
    "warehouse_ground_layout_slots",
    "warehouse_ground_occupancies",
    "warehouse_ground_occupancy_slots",
    "inventory_pallet_items",
    "inventory_location_movements",
)


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    return value


def _rows(connection: sa.Connection, table_name: str) -> list[dict[str, Any]]:
    result = connection.execute(sa.text(f'SELECT * FROM "{table_name}" ORDER BY id'))
    return [
        {key: _json_value(value) for key, value in row.items()}
        for row in result.mappings().all()
    ]


def _fingerprint(connection: sa.Connection) -> str:
    payload = {table: _rows(connection, table) for table in FINGERPRINT_TABLES}
    rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(rendered.encode("utf-8")).hexdigest()


def _scalar(connection: sa.Connection, sql: str, **params: Any) -> int:
    return int(connection.execute(sa.text(sql), params).scalar_one() or 0)


def _preflight(connection: sa.Connection) -> None:
    if connection.dialect.name != "sqlite":
        raise RuntimeError("P0-26 current-map migration is approved only for the formal SQLite database.")
    if _scalar(connection, f"SELECT COUNT(*) FROM {SNAPSHOT_TABLE}"):
        raise RuntimeError("P0-26 current-map snapshot already exists; refusing duplicate upgrade.")
    if _scalar(connection, "SELECT COUNT(*) FROM warehouse_locations WHERE source_version='V11'") != 398:
        raise RuntimeError("P0-26 preflight failed: formal V11 location baseline is not 398 rows.")
    if _scalar(connection, "SELECT COUNT(*) FROM warehouse_locations WHERE source_version='TWIN_V1'") != 230:
        raise RuntimeError("P0-26 preflight failed: formal TWIN_V1 location baseline is not 230 rows.")
    if _scalar(connection, "SELECT COUNT(*) FROM warehouse_locations WHERE address_kind='legacy'") != 631:
        raise RuntimeError("P0-26 preflight failed: location address baseline has drifted.")
    if _scalar(connection, "SELECT COUNT(*) FROM warehouse_ground_layout_plans WHERE area_id IN (%s)" % ",".join(map(str, sorted(GROUND_AREA_IDS)))):
        raise RuntimeError("P0-26 preflight failed: a target three-floor ground plan already exists.")
    forbidden = _scalar(
        connection,
        """
        SELECT COUNT(*) FROM inventory_pallets p
        JOIN warehouse_locations l ON l.id=p.location_id
        WHERE p.is_current=1 AND l.area_code IN ('E4','SEMI-011')
        """,
    )
    forbidden += _scalar(
        connection,
        """
        SELECT COUNT(*) FROM inventory_lots x
        JOIN warehouse_locations l ON l.id=x.warehouse_location_id
        WHERE x.status IN ('active','frozen')
          AND x.quantity_available+x.quantity_reserved>0
          AND l.area_code IN ('E4','SEMI-011')
        """,
    )
    if forbidden:
        raise RuntimeError("P0-26 preflight failed: E4/SEMI-011 gained inventory after the audit.")
    admin = _scalar(connection, "SELECT COUNT(*) FROM users WHERE id=1 AND is_active=1")
    if admin != 1:
        raise RuntimeError("P0-26 preflight failed: migration audit user 1 is unavailable.")


def _snapshot(connection: sa.Connection) -> dict[str, Any]:
    payload = {
        "migration_key": MIGRATION_KEY,
        "captured_at": MIGRATED_AT,
        "tables": {table: _rows(connection, table) for table in SNAPSHOT_TABLES},
        "inserted": {
            "warehouse_area_storage_policies": [],
            "warehouse_locations": [],
            "floor3_location_layouts": [],
            "warehouse_ground_layout_plans": [],
            "warehouse_ground_layout_slots": [],
            "warehouse_ground_occupancies": [],
            "warehouse_ground_occupancy_slots": [],
            "inventory_location_movements": [],
        },
    }
    connection.execute(
        sa.text(
            f"INSERT INTO {SNAPSHOT_TABLE} "
            "(id,migration_key,payload_json,post_fingerprint,created_at) "
            "VALUES (1,:key,:payload,'PENDING',:created_at)"
        ),
        {
            "key": MIGRATION_KEY,
            "payload": json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            "created_at": MIGRATED_AT,
        },
    )
    return payload


def _update_areas(connection: sa.Connection) -> None:
    for area_id, (code, name, capacity, _feature, layout, _types) in AREA_POLICIES.items():
        values: dict[str, Any] = {
            "id": area_id,
            "code": code,
            "name": name,
            "planned_locations": 1 if layout == "functional" else int(capacity or 0),
            "planned_capacity": int(capacity or 0),
            "review_status": "excluded" if layout in {"functional", "rack"} and capacity is None else "confirmed",
            "eligible": 0 if layout == "functional" or capacity is None else 1,
            "capacity": capacity,
            "reviewed_by": "owner_confirmed_2026-08-26",
            "reviewed_at": MIGRATED_AT,
            "updated_at": MIGRATED_AT,
        }
        connection.execute(
            sa.text(
                """
                UPDATE warehouse_areas
                SET area_code=:code,area_name=:name,
                    planned_location_count=:planned_locations,
                    planned_pallet_capacity=:planned_capacity,
                    capacity_review_status=:review_status,
                    capacity_eligible=:eligible,
                    confirmed_pallet_capacity=:capacity,
                    capacity_reviewed_by=:reviewed_by,
                    capacity_reviewed_at=:reviewed_at,
                    updated_at=:updated_at,address_version=address_version+1
                WHERE id=:id
                """
            ),
            values,
        )
        if connection.execute(sa.text("SELECT changes()" if connection.dialect.name == "sqlite" else "SELECT 1")).scalar_one() == 0:
            raise RuntimeError(f"P0-26 formal area {area_id} is missing.")


def _upsert_policies(connection: sa.Connection, inserted: dict[str, list[int]]) -> None:
    for area_id, (_code, _name, _capacity, feature_id, layout, types) in AREA_POLICIES.items():
        existing = connection.execute(
            sa.text("SELECT id,version FROM warehouse_area_storage_policies WHERE area_id=:area_id"),
            {"area_id": area_id},
        ).mappings().one_or_none()
        values = {
            "area_id": area_id,
            "feature_id": feature_id,
            "types": json.dumps(types, ensure_ascii=False, separators=(",", ":")),
            "layout": layout,
            "revision": MAP_REVISION,
            "updated_by": 1,
            "updated_at": MIGRATED_AT,
        }
        if existing:
            values.update({"id": existing["id"], "version": int(existing["version"]) + 1})
            connection.execute(
                sa.text(
                    """
                    UPDATE warehouse_area_storage_policies
                    SET map_feature_id=:feature_id,
                        allowed_inventory_types_json=:types,
                        storage_layout=:layout,status='published',
                        draft_map_revision=:revision,published_map_revision=:revision,
                        version=:version,updated_by=:updated_by,updated_at=:updated_at
                    WHERE id=:id
                    """
                ),
                values,
            )
        else:
            result = connection.execute(
                sa.text(
                    """
                    INSERT INTO warehouse_area_storage_policies
                    (area_id,map_feature_id,allowed_inventory_types_json,storage_layout,
                     status,draft_map_revision,published_map_revision,version,updated_by,
                     created_at,updated_at)
                    VALUES (:area_id,:feature_id,:types,:layout,'published',:revision,
                            :revision,1,:updated_by,:updated_at,:updated_at)
                    """
                ),
                values,
            )
            inserted["warehouse_area_storage_policies"].append(int(result.lastrowid))


def _live_location_ids(connection: sa.Connection) -> tuple[set[int], set[int]]:
    pallet_ids = {
        int(row[0])
        for row in connection.execute(
            sa.text("SELECT DISTINCT location_id FROM inventory_pallets WHERE is_current=1 AND location_id IS NOT NULL")
        )
    }
    lot_ids = {
        int(row[0])
        for row in connection.execute(
            sa.text(
                """
                SELECT DISTINCT warehouse_location_id FROM inventory_lots
                WHERE status IN ('active','frozen')
                  AND quantity_available+quantity_reserved>0
                """
            )
        )
    }
    return pallet_ids, lot_ids


def _set_functional_location(
    connection: sa.Connection,
    location_id: int,
    *,
    active: bool,
    area_id: int | None,
    area_code: str | None = None,
) -> None:
    connection.execute(
        sa.text(
            """
            UPDATE warehouse_locations
            SET source_version=:source_version,address_kind='functional',
                address_area_id=:area_id,rack_code=NULL,ground_row_no=NULL,
                slot_no=NULL,storage_type='temporary_aisle',
                placement_status=CASE
                    WHEN warehouse_floor IS NOT NULL
                     AND trim(COALESCE(:area_code,area_code,''))<>'' THEN 'placed'
                    ELSE 'unplaced'
                END,
                is_active=:active,area_code=COALESCE(:area_code,area_code),
                address_version=address_version+1,updated_at=:updated_at
            WHERE id=:id
            """
        ),
        {
            "source_version": MIGRATED_SOURCE_VERSION,
            "area_id": area_id,
            "active": 1 if active else 0,
            "area_code": area_code,
            "updated_at": MIGRATED_AT,
            "id": location_id,
        },
    )


def _update_layout(
    connection: sa.Connection,
    location_id: int,
    *,
    layout_kind: str,
    left_pct: float | None = None,
    top_pct: float | None = None,
    width_pct: float | None = None,
    height_pct: float | None = None,
) -> None:
    values = {
        "id": location_id,
        "kind": layout_kind,
        "left": left_pct,
        "top": top_pct,
        "width": width_pct,
        "height": height_pct,
        "updated_at": MIGRATED_AT,
    }
    connection.execute(
        sa.text(
            """
            UPDATE floor3_location_layouts
            SET layout_kind=:kind,source_type='manual',version=version+1,
                left_pct=COALESCE(:left,left_pct),top_pct=COALESCE(:top,top_pct),
                width_pct=COALESCE(:width,width_pct),height_pct=COALESCE(:height,height_pct),
                updated_by=1,updated_at=:updated_at
            WHERE location_id=:id
            """
        ),
        values,
    )


def _pack_slots(bounds: tuple[float, float, float, float], target: int) -> list[dict[str, Any]]:
    min_x, min_y, zone_width, zone_height = bounds
    candidates: list[tuple[int, int, int, int, int]] = []
    for width_mm, depth_mm in ((1200, 1000), (1000, 1200)):
        columns = math.floor(zone_width / width_mm)
        rows = math.floor(zone_height / depth_mm)
        capacity = columns * rows
        if capacity >= target:
            candidates.append((capacity - target, -columns, width_mm, depth_mm, columns))
    if not candidates:
        raise RuntimeError(f"P0-26 measured zone {bounds} cannot hold {target} standard pallets.")
    _unused, _prefer_columns, width_mm, depth_mm, columns = min(candidates)
    result: list[dict[str, Any]] = []
    for index in range(target):
        row = index // columns
        column = index % columns
        x_mm = min_x + column * width_mm
        y_mm = min_y + row * depth_mm
        result.append(
            {
                "route": index + 1,
                "row": row + 1,
                "slot": column + 1,
                "x": round(x_mm, 3),
                "y": round(y_mm, 3),
                "width": width_mm,
                "depth": depth_mm,
                "left_pct": round((x_mm - min_x) / zone_width * 100, 4),
                "top_pct": round((y_mm - min_y) / zone_height * 100, 4),
                "width_pct": round(width_mm / zone_width * 100, 4),
                "height_pct": round(depth_mm / zone_height * 100, 4),
            }
        )
    return result


def _new_location(
    connection: sa.Connection,
    *,
    area_id: int,
    code: str,
    warehouse_type: str,
    sort_order: int,
    inserted: dict[str, list[int]],
) -> int:
    result = connection.execute(
        sa.text(
            """
            INSERT INTO warehouse_locations
            (location_code,location_name,warehouse_type,is_active,remarks,
             warehouse_floor,area_code,storage_type,level_no,side_code,sort_order,
             is_temporary,source_version,address_kind,address_area_id,rack_code,
             ground_row_no,slot_no,address_version,placement_status,created_at,updated_at)
            VALUES (:location_code,:location_name,:warehouse_type,1,:remarks,3,:area_code,
                    'ground',NULL,NULL,:sort_order,0,:source_version,'functional',:area_id,
                    NULL,NULL,NULL,1,'placed',:created_at,:created_at)
            """
        ),
        {
            "location_code": code,
            "location_name": f"{code} 当前地图栈板位",
            "warehouse_type": warehouse_type,
            "remarks": "P0-26 当前实测地图补充物理位；无历史库存事实。",
            "area_code": AREA_POLICIES[area_id][0],
            "sort_order": sort_order,
            "source_version": MIGRATED_SOURCE_VERSION,
            "area_id": area_id,
            "created_at": MIGRATED_AT,
        },
    )
    location_id = int(result.lastrowid)
    inserted["warehouse_locations"].append(location_id)
    layout = connection.execute(
        sa.text(
            """
            INSERT INTO floor3_location_layouts
            (location_id,left_pct,top_pct,width_pct,height_pct,z_index,version,
             source_type,layout_kind,created_by,updated_by,created_at,updated_at)
            VALUES (:location_id,0,0,1,1,0,1,'manual','logical_anchor',1,1,:created_at,:created_at)
            """
        ),
        {"location_id": location_id, "created_at": MIGRATED_AT},
    )
    inserted["floor3_location_layouts"].append(int(layout.lastrowid))
    return location_id


def _location_pool(
    connection: sa.Connection,
    *,
    area_id: int,
    count: int,
    pallet_locations: set[int],
    lot_locations: set[int],
    inserted: dict[str, list[int]],
) -> tuple[list[int], set[int]]:
    code = AREA_POLICIES[area_id][0]
    query_code = "E4" if area_id == 16 else code
    rows = connection.execute(
        sa.text(
            """
            SELECT id FROM warehouse_locations
            WHERE upper(coalesce(area_code,''))=:code
            ORDER BY CASE WHEN id IN (SELECT location_id FROM inventory_pallets WHERE is_current=1) THEN 0
                          WHEN id IN (SELECT warehouse_location_id FROM inventory_lots
                                     WHERE status IN ('active','frozen')
                                       AND quantity_available+quantity_reserved>0) THEN 2
                          ELSE 1 END,
                     is_active DESC,id
            """
        ),
        {"code": query_code.upper()},
    ).scalars().all()
    usable = [int(value) for value in rows if int(value) not in lot_locations or int(value) in pallet_locations]
    selected = usable[:count]
    warehouse_type = "semi_finished" if code.startswith("SEMI-") else "finished"
    while len(selected) < count:
        sequence = len(selected) + 1
        selected.append(
            _new_location(
                connection,
                area_id=area_id,
                code=f"{code}-CM-G{sequence:03d}",
                warehouse_type=warehouse_type,
                sort_order=9000 + area_id * 100 + sequence,
                inserted=inserted,
            )
        )
    return selected, {int(value) for value in rows} - set(selected)


def _create_plan(
    connection: sa.Connection,
    *,
    area_id: int,
    selected: list[int],
    slots: list[dict[str, Any]],
    inserted: dict[str, list[int]],
) -> None:
    canonical_slots = [
        {key: value for key, value in slot.items() if key in {"route", "row", "slot", "x", "y", "width", "depth"}}
        for slot in slots
    ]
    preview = sha256(json.dumps(canonical_slots, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    request_hash = sha256(f"{MIGRATION_KEY}:{area_id}:{preview}".encode()).hexdigest()
    plan_result = connection.execute(
        sa.text(
            """
            INSERT INTO warehouse_ground_layout_plans
            (area_id,status,target_slot_count,numbering_origin,row_direction,slot_direction,
             row_start_no,slot_start_no,draft_map_revision,published_map_revision,
             preview_fingerprint,version,publish_idempotency_key,publish_request_hash,
             updated_by,published_by,created_at,updated_at,published_at)
            VALUES (:area_id,'published',:count,'south','from_aisle_inward','left_to_right',
                    1,1,:revision,:revision,:preview,1,:idem,:request_hash,
                    1,1,:created_at,:created_at,:created_at)
            """
        ),
        {
            "area_id": area_id,
            "count": len(selected),
            "revision": MAP_REVISION,
            "preview": preview,
            "idem": f"{MIGRATION_KEY}:publish:{area_id}",
            "request_hash": request_hash,
            "created_at": MIGRATED_AT,
        },
    )
    plan_id = int(plan_result.lastrowid)
    inserted["warehouse_ground_layout_plans"].append(plan_id)
    for location_id, slot in zip(selected, slots, strict=True):
        slot_result = connection.execute(
            sa.text(
                """
                INSERT INTO warehouse_ground_layout_slots
                (plan_id,location_id,route_sequence,row_no,slot_no,x_mm,y_mm,width_mm,depth_mm)
                VALUES (:plan_id,:location_id,:route,:row,:slot,:x,:y,:width,:depth)
                """
            ),
            {"plan_id": plan_id, "location_id": location_id, **slot},
        )
        inserted["warehouse_ground_layout_slots"].append(int(slot_result.lastrowid))
        connection.execute(
            sa.text(
                """
                UPDATE warehouse_locations
                SET source_version=:source_version,warehouse_floor=3,area_code=:area_code,
                    storage_type='ground',is_active=1,address_kind='ground_slot',
                    address_area_id=:area_id,rack_code=NULL,level_no=NULL,side_code=NULL,
                    ground_row_no=:row,slot_no=:slot,placement_status='placed',
                    address_version=address_version+1,updated_at=:updated_at
                WHERE id=:location_id
                """
            ),
            {
                "source_version": MIGRATED_SOURCE_VERSION,
                "area_code": AREA_POLICIES[area_id][0],
                "area_id": area_id,
                "row": slot["row"],
                "slot": slot["slot"],
                "updated_at": MIGRATED_AT,
                "location_id": location_id,
            },
        )
        _update_layout(
            connection,
            location_id,
            layout_kind="physical_pallet",
            left_pct=slot["left_pct"],
            top_pct=slot["top_pct"],
            width_pct=slot["width_pct"],
            height_pct=slot["height_pct"],
        )


def _create_occupancies(connection: sa.Connection, inserted: dict[str, list[int]]) -> None:
    rows = connection.execute(
        sa.text(
            """
            SELECT p.id pallet_id,p.location_id,
                   MIN(i.customer_id) customer_id,MIN(i.product_id) product_id,
                   CAST(ROUND(SUM(i.quantity),0) AS INTEGER) capacity_quantity
            FROM inventory_pallets p
            JOIN inventory_pallet_items i ON i.pallet_id=p.id
            JOIN warehouse_ground_layout_slots s ON s.location_id=p.location_id
            LEFT JOIN warehouse_ground_occupancies o ON o.pallet_id=p.id AND o.status='active'
            WHERE p.is_current=1 AND o.id IS NULL
            GROUP BY p.id,p.location_id
            ORDER BY p.id
            """
        )
    ).mappings().all()
    for row in rows:
        if row["customer_id"] is None or row["product_id"] is None:
            raise RuntimeError(f"P0-26 pallet {row['pallet_id']} lacks customer/product spatial facts.")
        occupancy = connection.execute(
            sa.text(
                """
                INSERT INTO warehouse_ground_occupancies
                (pallet_id,primary_location_id,customer_id,product_id,footprint_kind,
                 capacity_quantity,status,version,created_by,created_at)
                VALUES (:pallet_id,:location_id,:customer_id,:product_id,'single',
                        :quantity,'active',1,1,:created_at)
                """
            ),
            {
                "pallet_id": row["pallet_id"],
                "location_id": row["location_id"],
                "customer_id": row["customer_id"],
                "product_id": row["product_id"],
                "quantity": max(1, int(row["capacity_quantity"] or 0)),
                "created_at": MIGRATED_AT,
            },
        )
        occupancy_id = int(occupancy.lastrowid)
        inserted["warehouse_ground_occupancies"].append(occupancy_id)
        occupancy_slot = connection.execute(
            sa.text(
                """
                INSERT INTO warehouse_ground_occupancy_slots
                (occupancy_id,location_id,slot_sequence,status,created_at)
                VALUES (:occupancy_id,:location_id,1,'active',:created_at)
                """
            ),
            {
                "occupancy_id": occupancy_id,
                "location_id": row["location_id"],
                "created_at": MIGRATED_AT,
            },
        )
        inserted["warehouse_ground_occupancy_slots"].append(int(occupancy_slot.lastrowid))


def _relocate_unmeasured_temporary_pallets(
    connection: sa.Connection,
    inserted: dict[str, list[int]],
) -> None:
    """Move legacy F12 pallets into empty measured E2 slots without changing stock.

    F12's current outline has no confirmed physical pallet grid.  The owner
    authorized old positions to be projected onto the measured map, and E2
    has sufficient published spare capacity.  Pallet/lot identities and every
    quantity/status fact stay unchanged; a normal movement audit is appended.
    """

    sources = connection.execute(
        sa.text(
            """
            SELECT p.id pallet_id,p.location_id,p.version
            FROM inventory_pallets p
            JOIN warehouse_locations l ON l.id=p.location_id
            WHERE p.is_current=1 AND upper(coalesce(l.area_code,''))='F12'
            ORDER BY p.id
            """
        )
    ).mappings().all()
    if not sources:
        return
    targets = connection.execute(
        sa.text(
            """
            SELECT s.location_id
            FROM warehouse_ground_layout_slots s
            JOIN warehouse_ground_layout_plans p ON p.id=s.plan_id
            WHERE p.area_id=14 AND p.status='published'
              AND NOT EXISTS (
                SELECT 1 FROM inventory_pallets pallet
                WHERE pallet.location_id=s.location_id AND pallet.is_current=1
              )
              AND NOT EXISTS (
                SELECT 1 FROM inventory_lots lot
                WHERE lot.warehouse_location_id=s.location_id
                  AND lot.status IN ('active','frozen')
                  AND lot.quantity_available+lot.quantity_reserved>0
              )
            ORDER BY s.route_sequence
            LIMIT :count
            """
        ),
        {"count": len(sources)},
    ).scalars().all()
    if len(targets) != len(sources):
        raise RuntimeError("P0-26 cannot relocate all unmeasured F12 pallets into measured E2 slots.")
    for source, target_value in zip(sources, targets, strict=True):
        pallet_id = int(source["pallet_id"])
        source_location_id = int(source["location_id"])
        target_location_id = int(target_value)
        linked_lot_ids = [
            int(value)
            for value in connection.execute(
                sa.text(
                    """
                    SELECT inventory_lot_id FROM inventory_pallet_items
                    WHERE pallet_id=:pallet_id AND inventory_lot_id IS NOT NULL
                    ORDER BY id
                    """
                ),
                {"pallet_id": pallet_id},
            ).scalars().all()
        ]
        if not linked_lot_ids:
            raise RuntimeError(f"P0-26 F12 pallet {pallet_id} has no formal inventory lot.")
        mismatched = _scalar(
            connection,
            "SELECT COUNT(*) FROM inventory_lots WHERE id IN (%s) AND warehouse_location_id<>:source"
            % ",".join(map(str, linked_lot_ids)),
            source=source_location_id,
        )
        if mismatched:
            raise RuntimeError(f"P0-26 F12 pallet {pallet_id} contains an off-location lot.")
        connection.execute(
            sa.text(
                "UPDATE inventory_lots SET warehouse_location_id=:target,version=version+1,"
                "updated_at=:updated_at WHERE id IN (%s)" % ",".join(map(str, linked_lot_ids))
            ),
            {"target": target_location_id, "updated_at": MIGRATED_AT},
        )
        connection.execute(
            sa.text(
                """
                UPDATE inventory_pallets
                SET location_id=:target,version=version+1,updated_by=1,updated_at=:updated_at
                WHERE id=:pallet_id AND location_id=:source AND is_current=1
                """
            ),
            {
                "target": target_location_id,
                "updated_at": MIGRATED_AT,
                "pallet_id": pallet_id,
                "source": source_location_id,
            },
        )
        movement = connection.execute(
            sa.text(
                """
                INSERT INTO inventory_location_movements
                (pallet_id,from_location_id,to_location_id,movement_type,operator_id,
                 moved_at,idempotency_key,confirmed_at,pallet_version_before,
                 pallet_version_after,remarks)
                VALUES (:pallet_id,:source,:target,'move',1,:moved_at,:idem,:moved_at,
                        :before,:after,:remarks)
                """
            ),
            {
                "pallet_id": pallet_id,
                "source": source_location_id,
                "target": target_location_id,
                "moved_at": MIGRATED_AT,
                "idem": f"{MIGRATION_KEY}:relocate:{pallet_id}",
                "before": int(source["version"]),
                "after": int(source["version"]) + 1,
                "remarks": "P0-26：从未确认物理格的 F12 来源锚点迁入当前实测 E2 栈板位",
            },
        )
        inserted["inventory_location_movements"].append(int(movement.lastrowid))
        _set_functional_location(
            connection,
            source_location_id,
            active=False,
            area_id=18,
            area_code="F12",
        )


def _normalize_locations(connection: sa.Connection, inserted: dict[str, list[int]]) -> None:
    pallet_locations, lot_locations = _live_location_ids(connection)
    live_locations = pallet_locations | lot_locations

    # Start from a fail-closed current-map baseline. Physical plans and rack
    # addresses are activated explicitly below.
    all_locations = connection.execute(sa.text("SELECT id,address_area_id,area_code FROM warehouse_locations ORDER BY id")).mappings().all()
    area_id_by_code = {row[0].upper(): area_id for area_id, row in AREA_POLICIES.items()}
    for row in all_locations:
        code = str(row["area_code"] or "").upper()
        area_id = area_id_by_code.get("FIN-LOOSE-001" if code == "E4" else code)
        _set_functional_location(
            connection,
            int(row["id"]),
            active=int(row["id"]) in live_locations,
            area_id=area_id or row["address_area_id"],
            area_code="FIN-LOOSE-001" if code == "E4" else None,
        )
        _update_layout(connection, int(row["id"]), layout_kind="logical_anchor")

    # The no-pallet loose area has one selectable functional anchor, but never
    # a ground slot or pallet capacity.
    loose_rows = connection.execute(
        sa.text("SELECT id FROM warehouse_locations WHERE area_code='FIN-LOOSE-001' ORDER BY id")
    ).scalars().all()
    if not loose_rows:
        raise RuntimeError("P0-26 could not preserve the former E4 functional anchor.")
    loose_id = int(loose_rows[0])
    _set_functional_location(connection, loose_id, active=True, area_id=16, area_code="FIN-LOOSE-001")
    connection.execute(
        sa.text(
            """
            UPDATE warehouse_locations
            SET location_code='FIN-LOOSE-001',location_name='送货剩余零散库存暂存（无栈板）',
                remarks='仅少量送货剩余零散库存；本区无栈板位。数量多时请选择正常成品栈板位。',
                warehouse_type='finished',warehouse_floor=3
            WHERE id=:id
            """
        ),
        {"id": loose_id},
    )
    for archive_serial, archive_id in enumerate(loose_rows[1:], start=1):
        connection.execute(
            sa.text(
                """
                UPDATE warehouse_locations
                SET location_code=:location_code,
                    location_name='已停用历史位置（仅保留审计）',
                    remarks='旧E4栈板位已取消；本记录仅保留历史关联，不可入库、收料或移库。',
                    warehouse_type='finished',warehouse_floor=3
                WHERE id=:id
                """
            ),
            {
                "location_code": f"FIN-LOOSE-ARCHIVE-{archive_serial:03d}",
                "id": int(archive_id),
            },
        )

    # Existing published floor-one plan remains authoritative and is merely
    # normalized away from the legacy address/version labels.
    floor_one_slots = connection.execute(
        sa.text(
            """
            SELECT s.location_id,p.area_id,s.row_no,s.slot_no
            FROM warehouse_ground_layout_slots s
            JOIN warehouse_ground_layout_plans p ON p.id=s.plan_id
            WHERE p.area_id NOT IN (%s)
            """ % ",".join(map(str, sorted(GROUND_AREA_IDS)))
        )
    ).mappings().all()
    for row in floor_one_slots:
        connection.execute(
            sa.text(
                """
                UPDATE warehouse_locations
                SET source_version=:source_version,address_kind='ground_slot',
                    address_area_id=:area_id,ground_row_no=:row_no,slot_no=:slot_no,
                    storage_type='ground',placement_status='placed',is_active=1,
                    address_version=address_version+1,updated_at=:updated_at
                WHERE id=:location_id
                """
            ),
            {**row, "source_version": MIGRATED_SOURCE_VERSION, "updated_at": MIGRATED_AT},
        )
        _update_layout(connection, int(row["location_id"]), layout_kind="physical_pallet")

    # Rack paths are deterministic and stable within each current-map zone.
    for area_id in sorted(RACK_AREA_IDS):
        code = AREA_POLICIES[area_id][0]
        rows = connection.execute(
            sa.text("SELECT id,level_no,side_code FROM warehouse_locations WHERE upper(coalesce(area_code,''))=:code ORDER BY id"),
            {"code": code},
        ).mappings().all()
        for index, row in enumerate(rows):
            level = int(row["level_no"] or 1)
            side = str(row["side_code"] or "").upper()
            rack_code = "B" if side == "R" else "A"
            slot_no = index + 1
            if side in {"L", "R"}:
                same = [candidate for candidate in rows[: index + 1] if int(candidate["level_no"] or 1) == level and str(candidate["side_code"] or "").upper() == side]
                slot_no = len(same)
            elif level:
                same = [candidate for candidate in rows[: index + 1] if int(candidate["level_no"] or 1) == level]
                slot_no = len(same)
            connection.execute(
                sa.text(
                    """
                    UPDATE warehouse_locations
                    SET source_version=:source_version,warehouse_floor=3,area_code=:area_code,
                        storage_type='rack',is_active=1,address_kind='rack_slot',
                        address_area_id=:area_id,rack_code=:rack_code,level_no=:level,
                        ground_row_no=NULL,slot_no=:slot_no,placement_status='placed',
                        address_version=address_version+1,updated_at=:updated_at
                    WHERE id=:id
                    """
                ),
                {
                    "source_version": MIGRATED_SOURCE_VERSION,
                    "area_code": code,
                    "area_id": area_id,
                    "rack_code": rack_code,
                    "level": level,
                    "slot_no": slot_no,
                    "updated_at": MIGRATED_AT,
                    "id": row["id"],
                },
            )
            _update_layout(connection, int(row["id"]), layout_kind="physical_rack")

    # Repack all confirmed ground zones. Existing pallet locations win, live
    # loose-only anchors remain functional, and empty surplus rows retire.
    for area_id in sorted(GROUND_AREA_IDS):
        capacity = int(AREA_POLICIES[area_id][2] or 0)
        selected, surplus = _location_pool(
            connection,
            area_id=area_id,
            count=capacity,
            pallet_locations=pallet_locations,
            lot_locations=lot_locations,
            inserted=inserted,
        )
        if area_id in MEASURED_BOUNDS:
            bounds = MEASURED_BOUNDS[area_id]
        else:
            bounds = CURRENT_BOUNDS[area_id]
        slots = _pack_slots(bounds, capacity)
        _create_plan(connection, area_id=area_id, selected=selected, slots=slots, inserted=inserted)
        for location_id in surplus:
            code = AREA_POLICIES[area_id][0]
            _set_functional_location(
                connection,
                location_id,
                active=location_id in live_locations,
                area_id=area_id,
                area_code=code,
            )
            _update_layout(connection, location_id, layout_kind="logical_anchor")

    _relocate_unmeasured_temporary_pallets(connection, inserted)
    _create_occupancies(connection, inserted)


def _validate_post_state(connection: sa.Connection) -> None:
    checks = {
        "旧版 V11 库位": _scalar(connection, "SELECT COUNT(*) FROM warehouse_locations WHERE source_version='V11'"),
        "旧版 TWIN_V1 库位": _scalar(connection, "SELECT COUNT(*) FROM warehouse_locations WHERE source_version='TWIN_V1'"),
        "旧式地址": _scalar(connection, "SELECT COUNT(*) FROM warehouse_locations WHERE address_kind='legacy'"),
        "未知布局": _scalar(connection, "SELECT COUNT(*) FROM floor3_location_layouts WHERE layout_kind='unknown'"),
        "旧 E4 业务码": _scalar(connection, "SELECT COUNT(*) FROM warehouse_areas WHERE area_code='E4'"),
        "旧 E4 栈板位编码": _scalar(connection, "SELECT COUNT(*) FROM warehouse_locations WHERE upper(location_code) LIKE 'E4-%'"),
    }
    failures = [f"{name}={count}" for name, count in checks.items() if count]
    orphan_physical = _scalar(
        connection,
        """
        SELECT COUNT(*) FROM warehouse_locations l
        WHERE l.is_active=1 AND l.address_kind='ground_slot'
          AND NOT EXISTS (
            SELECT 1 FROM warehouse_ground_layout_slots s
            JOIN warehouse_ground_layout_plans p ON p.id=s.plan_id
            WHERE s.location_id=l.id AND p.status='published'
          )
        """,
    )
    if orphan_physical:
        failures.append(f"无已发布平面图的在用地面位={orphan_physical}")
    invalid_pallets = _scalar(
        connection,
        """
        SELECT COUNT(*) FROM inventory_pallets p JOIN warehouse_locations l ON l.id=p.location_id
        WHERE p.is_current=1 AND (l.is_active=0 OR l.source_version<>'CURRENT_MAP')
        """,
    )
    if invalid_pallets:
        failures.append(f"未投影到当前地图的在库栈板={invalid_pallets}")
    invalid_lots = _scalar(
        connection,
        """
        SELECT COUNT(*) FROM inventory_lots x JOIN warehouse_locations l ON l.id=x.warehouse_location_id
        WHERE x.status IN ('active','frozen') AND x.quantity_available+x.quantity_reserved>0
          AND (l.is_active=0 OR l.source_version<>'CURRENT_MAP')
        """,
    )
    if invalid_lots:
        failures.append(f"未投影到当前地图的正库存批次={invalid_lots}")
    loose_capacity = connection.execute(
        sa.text("SELECT capacity_eligible,confirmed_pallet_capacity FROM warehouse_areas WHERE id=16")
    ).one()
    if bool(loose_capacity[0]) or loose_capacity[1] is not None:
        failures.append("FIN-LOOSE-001 仍具有栈板容量")
    semi_capacity = connection.execute(
        sa.text("SELECT confirmed_pallet_capacity FROM warehouse_areas WHERE id=48")
    ).scalar_one()
    if int(semi_capacity or 0) != 4:
        failures.append("SEMI-011 栈板容量不是 4")
    if failures:
        raise RuntimeError("P0-26 post validation failed: " + "；".join(failures))


def upgrade_current_map(connection: sa.Connection) -> None:
    _preflight(connection)
    snapshot = _snapshot(connection)
    _update_areas(connection)
    _upsert_policies(connection, snapshot["inserted"])
    _normalize_locations(connection, snapshot["inserted"])
    _validate_post_state(connection)
    fingerprint = _fingerprint(connection)
    connection.execute(
        sa.text(
            f"UPDATE {SNAPSHOT_TABLE} SET payload_json=:payload,post_fingerprint=:fingerprint WHERE id=1"
        ),
        {
            "payload": json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            "fingerprint": fingerprint,
        },
    )


def _restore_rows(connection: sa.Connection, table_name: str, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    columns = [str(row[1]) for row in connection.execute(sa.text(f'PRAGMA table_info("{table_name}")')).all()]
    assignments = ",".join(f'"{column}"=:{column}' for column in columns if column != "id")
    statement = sa.text(f'UPDATE "{table_name}" SET {assignments} WHERE id=:id')
    for row in rows:
        connection.execute(statement, {column: row.get(column) for column in columns})


def downgrade_current_map(connection: sa.Connection) -> None:
    row = connection.execute(
        sa.text(f"SELECT migration_key,payload_json,post_fingerprint FROM {SNAPSHOT_TABLE} WHERE id=1")
    ).mappings().one_or_none()
    if not row or row["migration_key"] != MIGRATION_KEY:
        raise RuntimeError("P0-26 downgrade snapshot is missing or belongs to another migration.")
    current_fingerprint = _fingerprint(connection)
    if current_fingerprint != row["post_fingerprint"]:
        raise RuntimeError(
            "Refusing P0-26 downgrade: warehouse or inventory state changed after migration. "
            "Create a new verified backup/mapping plan instead of discarding live facts."
        )
    payload = json.loads(row["payload_json"])
    inserted: dict[str, list[int]] = payload["inserted"]
    trigger_rows = connection.execute(
        sa.text(
            """
            SELECT name,sql FROM sqlite_master
            WHERE type='trigger' AND tbl_name IN (
              'warehouse_ground_layout_plans','warehouse_ground_layout_slots',
              'warehouse_ground_occupancies','warehouse_ground_occupancy_slots',
              'inventory_location_movements'
            )
            ORDER BY name
            """
        )
    ).mappings().all()
    for trigger in trigger_rows:
        connection.execute(sa.text(f'DROP TRIGGER "{trigger["name"]}"'))
    try:
        for table in (
            "warehouse_ground_occupancy_slots",
            "warehouse_ground_occupancies",
            "warehouse_ground_layout_slots",
            "warehouse_ground_layout_plans",
            "inventory_location_movements",
            "floor3_location_layouts",
            "warehouse_locations",
            "warehouse_area_storage_policies",
        ):
            ids = [int(value) for value in inserted.get(table, [])]
            if ids:
                connection.execute(
                    sa.text(f'DELETE FROM "{table}" WHERE id IN ({",".join(map(str, ids))})')
                )
        for table in SNAPSHOT_TABLES:
            _restore_rows(connection, table, payload["tables"][table])
    finally:
        for trigger in trigger_rows:
            if trigger["sql"]:
                connection.execute(sa.text(str(trigger["sql"])))
    if _scalar(connection, "SELECT COUNT(*) FROM warehouse_locations WHERE source_version='V11'") != 398:
        raise RuntimeError("P0-26 downgrade did not restore the audited V11 baseline.")
    if _scalar(connection, "SELECT COUNT(*) FROM warehouse_locations WHERE address_kind='legacy'") != 631:
        raise RuntimeError("P0-26 downgrade did not restore the audited address baseline.")
