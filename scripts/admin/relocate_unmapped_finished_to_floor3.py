"""P1-105 controlled relocation of unmapped positive finished pallets.

The default mode is read-only planning.  ``--apply`` is intentionally guarded
by a task-specific authorization phrase, an exact plan SHA, an explicit
database role and an operator label.  Formal apply acquires SQLite's writer
lock before taking the online backup, re-planning, moving pallets and writing
the batch audit record, so no unrelated writer can slip between backup and
commit.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import csv
from datetime import datetime
from hashlib import sha256
import json
from math import isfinite
from pathlib import Path
import sqlite3
import sys
from typing import Any, Iterable, Mapping
from urllib.parse import quote


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


TASK_ID = "P1-105"
FORMAL_AUTHORIZATION = "P1-105-FORMAL-FINISHED-RELOCATION"
REHEARSAL_AUTHORIZATION = "P1-105-ISOLATED-REHEARSAL"
FORMAL_DATABASE = Path(r"D:\纸箱厂erp软件搭建\data\carton_erp.sqlite3")


AUDIT_SQL = """
SELECT
    lot.id AS lot_id,
    lot.quantity_available,
    lot.quantity_reserved,
    lot.quantity_damaged,
    lot.quantity_scrapped,
    lot.unit,
    lot.status AS lot_status,
    lot.version AS lot_version,
    location.id AS location_id,
    location.location_code,
    location.is_active AS location_is_active,
    location.warehouse_floor,
    location.area_code AS location_area_code,
    location.storage_type,
    location.source_version,
    location.placement_status,
    floor.id AS floor_id,
    floor.construction_status AS floor_construction_status,
    area.id AS area_id,
    area.area_code AS formal_area_code,
    area.construction_status AS area_construction_status,
    policy.id AS policy_id,
    policy.map_feature_id,
    policy.status AS policy_status,
    policy.published_map_revision AS policy_published_map_revision,
    layout.location_id AS geometry_location_id,
    ground_plan.status AS ground_plan_status,
    ground_plan.published_map_revision AS ground_plan_published_map_revision,
    ground_plan.area_id AS ground_plan_area_id,
    ground_slot.location_id AS ground_slot_location_id,
    detail.owner_customer_id,
    detail.product_id,
    detail.inventory_code_snapshot,
    detail.product_name_snapshot,
    detail.box_type_snapshot,
    detail.length_mm,
    detail.width_mm,
    detail.height_mm,
    detail.material_code_snapshot,
    detail.flute_type_snapshot,
    pallet_item.id AS pallet_item_id,
    pallet.id AS pallet_id,
    pallet.pallet_code,
    pallet.location_id AS pallet_location_id,
    pallet.status AS pallet_status,
    pallet.is_current AS pallet_is_current,
    pallet.version AS pallet_version,
    (SELECT count(*) FROM inventory_pallet_items all_items
      WHERE all_items.pallet_id = pallet.id) AS pallet_item_count
FROM inventory_lots AS lot
LEFT JOIN warehouse_locations AS location
  ON location.id = lot.warehouse_location_id
LEFT JOIN warehouse_floors AS floor
  ON floor.floor_number = location.warehouse_floor
LEFT JOIN warehouse_areas AS area
  ON area.floor_id = floor.id
 AND upper(trim(area.area_code)) = upper(trim(location.area_code))
LEFT JOIN warehouse_area_storage_policies AS policy
  ON policy.area_id = area.id
LEFT JOIN floor3_location_layouts AS layout
  ON layout.location_id = location.id
LEFT JOIN warehouse_ground_layout_slots AS ground_slot
  ON ground_slot.id = (
      SELECT candidate_slot.id
      FROM warehouse_ground_layout_slots AS candidate_slot
      JOIN warehouse_ground_layout_plans AS candidate_plan
        ON candidate_plan.id = candidate_slot.plan_id
      WHERE candidate_slot.location_id = location.id
        AND candidate_plan.status = 'published'
        AND candidate_plan.published_map_revision = CASE location.warehouse_floor
              WHEN 1 THEN :runtime_revision_1f
              WHEN 3 THEN :runtime_revision_3f
              ELSE NULL
            END
      ORDER BY candidate_plan.id DESC, candidate_slot.id DESC
      LIMIT 1
  )
LEFT JOIN warehouse_ground_layout_plans AS ground_plan
  ON ground_plan.id = ground_slot.plan_id
LEFT JOIN finished_goods_inventory_details AS detail
  ON detail.inventory_lot_id = lot.id
LEFT JOIN inventory_pallet_items AS pallet_item
  ON pallet_item.inventory_lot_id = lot.id
LEFT JOIN inventory_pallets AS pallet
  ON pallet.id = pallet_item.pallet_id
WHERE lot.inventory_type = 'finished'
  AND lot.status IN ('active', 'frozen')
  AND (
        lot.quantity_available
      + lot.quantity_reserved
      + lot.quantity_damaged
  ) > 0
ORDER BY lot.id
"""


TARGET_SQL = """
SELECT
    location.id AS location_id,
    location.location_code,
    location.location_name,
    location.is_active AS location_is_active,
    location.warehouse_floor,
    location.area_code AS location_area_code,
    location.storage_type,
    location.warehouse_type,
    location.source_version,
    location.placement_status,
    location.sort_order,
    floor.id AS floor_id,
    floor.construction_status AS floor_construction_status,
    area.id AS area_id,
    area.area_code AS formal_area_code,
    area.area_name,
    area.construction_status AS area_construction_status,
    area.capacity_review_status,
    area.capacity_eligible,
    area.confirmed_pallet_capacity,
    policy.id AS policy_id,
    policy.map_feature_id,
    policy.status AS policy_status,
    policy.published_map_revision AS policy_published_map_revision,
    policy.allowed_inventory_types_json,
    policy.storage_layout AS policy_storage_layout,
    layout.location_id AS geometry_location_id,
    layout.version AS layout_version,
    ground_plan.status AS ground_plan_status,
    ground_plan.published_map_revision AS ground_plan_published_map_revision,
    ground_plan.area_id AS ground_plan_area_id,
    ground_slot.location_id AS ground_slot_location_id,
    EXISTS(
      SELECT 1 FROM inventory_pallets current_pallet
      WHERE current_pallet.location_id = location.id
        AND current_pallet.is_current = 1
    ) AS occupied_pallet,
    EXISTS(
      SELECT 1 FROM inventory_lots live_lot
      WHERE live_lot.warehouse_location_id = location.id
        AND live_lot.status IN ('active', 'frozen')
        AND live_lot.quantity_available
          + live_lot.quantity_reserved
          + live_lot.quantity_damaged > 0
    ) AS occupied_inventory,
    EXISTS(
      SELECT 1
      FROM warehouse_ground_occupancy_slots occupancy_slot
      JOIN warehouse_ground_occupancies occupancy
        ON occupancy.id = occupancy_slot.occupancy_id
      WHERE occupancy_slot.location_id = location.id
        AND occupancy_slot.status = 'active'
        AND occupancy.status = 'active'
    ) AS occupied_ground,
    (
      SELECT count(*) FROM inventory_pallets area_pallet
      JOIN warehouse_locations area_location
        ON area_location.id = area_pallet.location_id
      WHERE area_pallet.is_current = 1
        AND area_location.warehouse_floor = location.warehouse_floor
        AND upper(trim(area_location.area_code))
            = upper(trim(location.area_code))
    ) AS area_occupied_pallets
FROM warehouse_locations AS location
LEFT JOIN warehouse_floors AS floor
  ON floor.floor_number = location.warehouse_floor
LEFT JOIN warehouse_areas AS area
  ON area.floor_id = floor.id
 AND upper(trim(area.area_code)) = upper(trim(location.area_code))
LEFT JOIN warehouse_area_storage_policies AS policy
  ON policy.area_id = area.id
LEFT JOIN floor3_location_layouts AS layout
  ON layout.location_id = location.id
LEFT JOIN warehouse_ground_layout_slots AS ground_slot
  ON ground_slot.id = (
      SELECT candidate_slot.id
      FROM warehouse_ground_layout_slots AS candidate_slot
      JOIN warehouse_ground_layout_plans AS candidate_plan
        ON candidate_plan.id = candidate_slot.plan_id
      WHERE candidate_slot.location_id = location.id
        AND candidate_plan.status = 'published'
        AND candidate_plan.published_map_revision = :runtime_revision_3f
      ORDER BY candidate_plan.id DESC, candidate_slot.id DESC
      LIMIT 1
  )
LEFT JOIN warehouse_ground_layout_plans AS ground_plan
  ON ground_plan.id = ground_slot.plan_id
WHERE location.warehouse_floor = 3
ORDER BY
    upper(trim(location.area_code)),
    CASE location.storage_type WHEN 'ground' THEN 0 ELSE 1 END,
    location.sort_order,
    location.location_code,
    location.id
"""


def _sha256_bytes(payload: bytes) -> str:
    return sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _valid_zone_geometry(feature: Mapping[str, Any]) -> bool:
    points = feature.get("points") or []
    if not isinstance(points, list) or len(points) < 3:
        return False
    try:
        normalized = [
            (float(point[0]), float(point[1]))
            for point in points
            if isinstance(point, (list, tuple)) and len(point) >= 2
        ]
    except (TypeError, ValueError, OverflowError):
        return False
    if len(normalized) != len(points):
        return False
    if any(not isfinite(x) or not isfinite(y) for x, y in normalized):
        return False
    if len(set(normalized)) < 3:
        return False
    twice_area = abs(
        sum(
            x1 * y2 - x2 * y1
            for (x1, y1), (x2, y2) in zip(
                normalized,
                normalized[1:] + normalized[:1],
                strict=True,
            )
        )
    )
    return twice_area > 0


def load_runtime_map(path: Path) -> dict[str, Any]:
    resolved = path.resolve(strict=True)
    payload_bytes = resolved.read_bytes()
    payload = json.loads(payload_bytes.decode("utf-8"))
    if payload.get("schema_version") != 1:
        raise RuntimeError("运行地图 schema_version 不是 1")
    floors = payload.get("floors")
    if not isinstance(floors, dict):
        raise RuntimeError("运行地图缺少 floors")
    identities: dict[str, dict[str, Any]] = {}
    for floor_code in ("1F", "3F"):
        floor = floors.get(floor_code)
        if not isinstance(floor, dict):
            continue
        zones_by_id: dict[str, str] = {}
        zone_ids_by_area: dict[str, list[str]] = {}
        for feature in floor.get("features") or []:
            if (
                not isinstance(feature, dict)
                or feature.get("feature_kind") != "zone"
                or not _valid_zone_geometry(feature)
            ):
                continue
            feature_id = str(feature.get("id") or "").strip()
            if not feature_id:
                continue
            area_code = str(feature.get("erp_area_code") or "").strip().upper()
            if feature_id in zones_by_id:
                raise RuntimeError(f"运行地图 {floor_code} 存在重复区域要素 ID")
            zones_by_id[feature_id] = area_code
            if area_code:
                zone_ids_by_area.setdefault(area_code, []).append(feature_id)
        identities[floor_code] = {
            "revision": str(floor.get("revision") or "").strip(),
            "zones_by_id": zones_by_id,
            "zone_ids_by_area": {
                key: tuple(sorted(value))
                for key, value in zone_ids_by_area.items()
            },
        }
    return {
        "path": str(resolved),
        "sha256": _sha256_bytes(payload_bytes),
        "identities": identities,
    }


def open_readonly(database: Path) -> sqlite3.Connection:
    resolved = database.resolve(strict=True)
    uri = "file:" + quote(resolved.as_posix(), safe="/:") + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    if int(connection.execute("PRAGMA query_only").fetchone()[0]) != 1:
        connection.close()
        raise RuntimeError("无法启用 SQLite query_only")
    return connection


def classify_location(
    row: Mapping[str, Any], identities: Mapping[str, Mapping[str, Any]]
) -> tuple[str, str]:
    if row["location_id"] is None:
        return "unlocated", "location_missing"
    if not bool(row["location_is_active"]):
        return "unlocated", "location_disabled"
    if str(row["placement_status"] or "") != "placed":
        return "unlocated", "location_unplaced"
    floor_number = int(row["warehouse_floor"] or 0)
    area_code = str(row["location_area_code"] or "").strip().upper()
    if floor_number <= 0 or not area_code:
        return "unlocated", "location_floor_area_missing"
    if row["floor_id"] is None:
        return "unlocated", "floor_missing"
    if str(row["floor_construction_status"] or "") != "enabled":
        return "unlocated", "floor_not_enabled"
    if row["area_id"] is None:
        return "unlocated", "area_missing"
    if str(row["area_construction_status"] or "") != "enabled":
        return "unlocated", "area_not_enabled"

    floor_code = f"{floor_number}F"
    identity = identities.get(floor_code)
    if not identity:
        return "unlocated", "runtime_floor_missing"
    current_revision = str(identity.get("revision") or "").strip()
    if not current_revision:
        return "unlocated", "runtime_revision_missing"

    source_version = str(row["source_version"] or "").strip().upper()
    legacy_v11 = bool(
        row["policy_id"] is None
        and source_version == "V11"
        and floor_number == 3
    )
    if legacy_v11:
        feature_ids = tuple(
            (identity.get("zone_ids_by_area") or {}).get(area_code, ())
        )
        if len(feature_ids) != 1:
            return "unlocated", "v11_area_not_unique"
    else:
        if source_version != "TWIN_V1":
            return "unlocated", "source_not_twin_v1"
        if row["policy_id"] is None or str(row["policy_status"] or "") != "published":
            return "unlocated", "policy_not_published"
        policy_revision = str(row["policy_published_map_revision"] or "").strip()
        if policy_revision != current_revision:
            return "unlocated", "policy_revision_stale"
        feature_id = str(row["map_feature_id"] or "").strip()
        formal_area_code = str(row["formal_area_code"] or "").strip().upper()
        if not feature_id:
            return "unlocated", "policy_feature_missing"
        if (identity.get("zones_by_id") or {}).get(feature_id) != formal_area_code:
            return "unlocated", "policy_feature_area_mismatch"
        if str(row["storage_type"] or "").strip().lower() in {
            "ground",
            "temporary_aisle",
        }:
            if (
                str(row["ground_plan_status"] or "") != "published"
                or str(row["ground_plan_published_map_revision"] or "").strip()
                != current_revision
                or int(row["ground_plan_area_id"] or 0) != int(row["area_id"] or 0)
                or int(row["ground_slot_location_id"] or 0)
                != int(row["location_id"] or 0)
            ):
                return "unlocated", "ground_layout_not_current"
    if row["geometry_location_id"] is None:
        return "unlocated", "geometry_missing"
    return "mapped", "mapped"


def compatibility_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        int(row["owner_customer_id"] or 0),
        int(row["product_id"] or 0),
        str(row["inventory_code_snapshot"] or "").strip().upper(),
        str(row["box_type_snapshot"] or "").strip().upper(),
        str(row["length_mm"] or ""),
        str(row["width_mm"] or ""),
        str(row["height_mm"] or ""),
        str(row["material_code_snapshot"] or "").strip().upper(),
        str(row["flute_type_snapshot"] or "").strip().upper(),
        str(row["unit"] or "").strip().lower(),
    )


def _target_allowed(row: Mapping[str, Any]) -> bool:
    if int(row["warehouse_floor"] or 0) != 3:
        return False
    if str(row["storage_type"] or "") not in {"ground", "temporary_aisle"}:
        return False
    if str(row["warehouse_type"] or "") not in {"finished", "shared"}:
        return False
    if any(
        bool(row[key])
        for key in ("occupied_pallet", "occupied_inventory", "occupied_ground")
    ):
        return False
    policy_id = row["policy_id"]
    if policy_id is not None:
        try:
            allowed = json.loads(row["allowed_inventory_types_json"] or "[]")
        except json.JSONDecodeError:
            return False
        if "finished" not in allowed:
            return False
        if str(row["policy_storage_layout"] or "") not in {
            "pallet_ground",
            "mixed",
        }:
            return False
    return True


def _capacity_quota(row: Mapping[str, Any]) -> int:
    if (
        str(row["capacity_review_status"] or "") == "confirmed"
        and bool(row["capacity_eligible"])
        and row["confirmed_pallet_capacity"] is not None
    ):
        return max(
            int(row["confirmed_pallet_capacity"])
            - int(row["area_occupied_pallets"] or 0),
            0,
        )
    return 10**9


def _plan_contract(plan: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "task_id": plan["task_id"],
        "alembic_heads": plan["alembic_heads"],
        "runtime_map": plan["runtime_map"],
        "operations": plan["operations"],
    }


def build_plan(database: Path, runtime_map: Path) -> dict[str, Any]:
    database = database.resolve(strict=True)
    runtime = load_runtime_map(runtime_map)
    identities = runtime["identities"]
    revisions = {
        "runtime_revision_1f": str(
            (identities.get("1F") or {}).get("revision") or ""
        ),
        "runtime_revision_3f": str(
            (identities.get("3F") or {}).get("revision") or ""
        ),
    }
    connection = open_readonly(database)
    changes_before = int(connection.total_changes)
    try:
        alembic_heads = [
            str(row[0])
            for row in connection.execute(
                "SELECT version_num FROM alembic_version ORDER BY version_num"
            )
        ]
        source_rows = [
            dict(row) for row in connection.execute(AUDIT_SQL, revisions)
        ]
        target_rows = [
            dict(row) for row in connection.execute(TARGET_SQL, revisions)
        ]
        query_only = int(connection.execute("PRAGMA query_only").fetchone()[0])
        changes_after = int(connection.total_changes)
    finally:
        connection.close()
    if query_only != 1 or changes_before != 0 or changes_after != 0:
        raise RuntimeError("只读计划查询越过 query_only/total_changes 门禁")
    lot_ids = [int(row["lot_id"]) for row in source_rows]
    if len(lot_ids) != len(set(lot_ids)):
        raise RuntimeError("库存联接导致同一批次重复，停止计划")

    unmapped: list[dict[str, Any]] = []
    for row in source_rows:
        status, reason = classify_location(row, identities)
        if status == "unlocated":
            row["unlocated_reason"] = reason
            unmapped.append(row)
    if not unmapped:
        raise RuntimeError("当前没有待定位正数成品，无需执行")

    pallet_groups: dict[int, list[dict[str, Any]]] = {}
    loose_lots: list[dict[str, Any]] = []
    for row in unmapped:
        if int(row["quantity_damaged"] or 0) or int(row["quantity_scrapped"] or 0):
            raise RuntimeError("待定位批次含报损或报废数量，不能自动搬位")
        if row["owner_customer_id"] is None or not (
            row["inventory_code_snapshot"] or row["product_id"]
        ):
            raise RuntimeError("待定位成品缺少客户或存货身份，不能自动分配")
        if row["pallet_id"] is None:
            loose_lots.append(row)
            continue
        pallet_id = int(row["pallet_id"])
        if (
            not bool(row["pallet_is_current"])
            or str(row["pallet_status"] or "") != "active"
            or int(row["pallet_location_id"] or 0) != int(row["location_id"] or 0)
        ):
            raise RuntimeError(f"批次 {row['lot_id']} 的栈板位置或状态不一致")
        pallet_groups.setdefault(pallet_id, []).append(row)

    sources: list[dict[str, Any]] = []
    for pallet_id, rows in sorted(pallet_groups.items()):
        expected_item_count = int(rows[0]["pallet_item_count"] or 0)
        if expected_item_count != len(rows):
            raise RuntimeError(
                f"栈板 {pallet_id} 还含不在本轮待定位集合中的内容，停止整板移动"
            )
        keys = {compatibility_key(row) for row in rows}
        if len(keys) != 1:
            raise RuntimeError(f"栈板 {pallet_id} 已混放不同客户/编码/规格，停止自动移动")
        sources.append(
            {
                "operation": "pallet_move",
                "pallet_id": pallet_id,
                "pallet_code": str(rows[0]["pallet_code"] or ""),
                "expected_version": int(rows[0]["pallet_version"]),
                "source_location_id": int(rows[0]["location_id"]),
                "source_location_code": str(rows[0]["location_code"] or ""),
                "lot_ids": sorted(int(row["lot_id"]) for row in rows),
                "lot_versions": {
                    str(int(row["lot_id"])): int(row["lot_version"])
                    for row in rows
                },
                "quantity": sum(
                    int(row["quantity_available"] or 0)
                    + int(row["quantity_reserved"] or 0)
                    for row in rows
                ),
                "owner_customer_id": int(rows[0]["owner_customer_id"]),
                "product_id": (
                    int(rows[0]["product_id"])
                    if rows[0]["product_id"] is not None
                    else None
                ),
                "inventory_code": str(
                    rows[0]["inventory_code_snapshot"] or ""
                ),
                "product_name": str(rows[0]["product_name_snapshot"] or ""),
                "specification": {
                    "box_type": rows[0]["box_type_snapshot"],
                    "length_mm": rows[0]["length_mm"],
                    "width_mm": rows[0]["width_mm"],
                    "height_mm": rows[0]["height_mm"],
                    "material_code": rows[0]["material_code_snapshot"],
                    "flute_type": rows[0]["flute_type_snapshot"],
                    "unit": rows[0]["unit"],
                },
                "compatibility_key": list(compatibility_key(rows[0])),
                "unlocated_reasons": sorted(
                    {str(row["unlocated_reason"]) for row in rows}
                ),
            }
        )
    for row in loose_lots:
        sources.append(
            {
                "operation": "lot_transfer",
                "lot_id": int(row["lot_id"]),
                "expected_version": int(row["lot_version"]),
                "source_location_id": int(row["location_id"]),
                "source_location_code": str(row["location_code"] or ""),
                "lot_ids": [int(row["lot_id"])],
                "lot_versions": {str(int(row["lot_id"])): int(row["lot_version"])},
                "quantity": int(row["quantity_available"] or 0)
                + int(row["quantity_reserved"] or 0),
                "owner_customer_id": int(row["owner_customer_id"]),
                "product_id": (
                    int(row["product_id"]) if row["product_id"] is not None else None
                ),
                "inventory_code": str(row["inventory_code_snapshot"] or ""),
                "product_name": str(row["product_name_snapshot"] or ""),
                "specification": {
                    "box_type": row["box_type_snapshot"],
                    "length_mm": row["length_mm"],
                    "width_mm": row["width_mm"],
                    "height_mm": row["height_mm"],
                    "material_code": row["material_code_snapshot"],
                    "flute_type": row["flute_type_snapshot"],
                    "unit": row["unit"],
                },
                "compatibility_key": list(compatibility_key(row)),
                "unlocated_reasons": [str(row["unlocated_reason"])],
            }
        )
    sources.sort(
        key=lambda item: (
            tuple(str(value) for value in item["compatibility_key"]),
            -int(item["quantity"]),
            str(item.get("pallet_code") or ""),
            int(item.get("pallet_id") or item.get("lot_id") or 0),
        )
    )

    target_pool: list[dict[str, Any]] = []
    used_by_area: dict[int, int] = {}
    for row in target_rows:
        status, _reason = classify_location(row, identities)
        if status != "mapped" or not _target_allowed(row):
            continue
        area_id = int(row["area_id"])
        used = used_by_area.get(area_id, 0)
        if used >= _capacity_quota(row):
            continue
        target_pool.append(row)
        used_by_area[area_id] = used + 1
    if len(target_pool) < len(sources):
        raise RuntimeError(
            "三楼当前正式空闲容量不足："
            f"需要 {len(sources)} 个栈板位，仅有 {len(target_pool)} 个；"
            "必须另建同编码同规格合板计划，当前脚本停止且零写入"
        )

    operations: list[dict[str, Any]] = []
    for sequence, (source, target) in enumerate(
        zip(sources, target_pool[: len(sources)], strict=True), start=1
    ):
        operation = {
            **source,
            "sequence": sequence,
            "client_item_id": f"p1-105-{sequence:03d}",
            "target_location_id": int(target["location_id"]),
            "target_location_code": str(target["location_code"] or ""),
            "target_location_name": str(target["location_name"] or ""),
            "target_area_id": int(target["area_id"]),
            "target_area_code": str(target["formal_area_code"] or ""),
            "expected_target_layout_version": int(target["layout_version"]),
        }
        operations.append(operation)

    plan: dict[str, Any] = {
        "task_id": TASK_ID,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "database": {
            "path": str(database),
            "query_mode": "SQLite URI mode=ro + query_only=1",
        },
        "alembic_heads": alembic_heads,
        "runtime_map": {
            "path": runtime["path"],
            "sha256": runtime["sha256"],
            "revisions": {
                code: identity["revision"]
                for code, identity in identities.items()
            },
        },
        "summary": {
            "unmapped_lot_count": len(unmapped),
            "source_pallet_count": len(pallet_groups),
            "loose_lot_count": len(loose_lots),
            "operation_count": len(sources),
            "target_pool_count": len(target_pool),
            "total_quantity": sum(int(item["quantity"]) for item in sources),
            "merge_required": False,
        },
        "operations": operations,
    }
    plan["plan_sha256"] = _sha256_bytes(_canonical_json(_plan_contract(plan)))
    return plan


def write_plan(plan: Mapping[str, Any], output: Path) -> tuple[Path, Path]:
    json_path = output if output.is_absolute() else Path.cwd() / output
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    csv_path = json_path.with_suffix(".csv")
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "顺序",
                "客户ID",
                "存货编码",
                "规格",
                "数量",
                "单位",
                "栈板编码",
                "旧位置",
                "三楼新位置",
            ]
        )
        for item in plan["operations"]:
            spec = item["specification"]
            writer.writerow(
                [
                    item["sequence"],
                    item["owner_customer_id"],
                    item["inventory_code"],
                    "×".join(
                        str(spec.get(key) or "")
                        for key in ("length_mm", "width_mm", "height_mm")
                    ),
                    item["quantity"],
                    spec.get("unit") or "",
                    item.get("pallet_code") or "待建栈板",
                    item["source_location_code"],
                    item["target_location_code"],
                ]
            )
    return json_path, csv_path


def database_snapshot(database: Path) -> dict[str, Any]:
    connection = open_readonly(database)
    try:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        finished = connection.execute(
            """
            SELECT count(*),
                   coalesce(sum(quantity_available),0),
                   coalesce(sum(quantity_reserved),0),
                   coalesce(sum(quantity_consumed),0),
                   coalesce(sum(quantity_damaged),0),
                   coalesce(sum(quantity_scrapped),0)
            FROM inventory_lots WHERE inventory_type='finished'
            """
        ).fetchone()
        reservations = connection.execute(
            """
            SELECT count(*),
                   coalesce(sum(reserved_stock_quantity),0),
                   coalesce(sum(consumed_stock_quantity),0),
                   coalesce(sum(released_stock_quantity),0),
                   coalesce(sum(credited_requirement_quantity),0),
                   coalesce(sum(consumed_requirement_quantity),0),
                   coalesce(sum(released_requirement_quantity),0)
            FROM inventory_reservations
            """
        ).fetchone()
        counts = {
            table: int(
                connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            )
            for table in (
                "inventory_pallets",
                "inventory_pallet_items",
                "inventory_location_movements",
                "inventory_lot_transfers",
                "inventory_movements",
                "operation_logs",
            )
        }
        return {
            "integrity_check": integrity,
            "foreign_key_violations": foreign_keys,
            "finished": list(finished),
            "reservations": list(reservations),
            "counts": counts,
        }
    finally:
        connection.close()


def session_snapshot(session) -> dict[str, Any]:
    """Read transaction-local conservation facts before the writer lock is released."""

    from sqlalchemy import text

    finished = session.execute(
        text(
            """
            SELECT count(*),
                   coalesce(sum(quantity_available),0),
                   coalesce(sum(quantity_reserved),0),
                   coalesce(sum(quantity_consumed),0),
                   coalesce(sum(quantity_damaged),0),
                   coalesce(sum(quantity_scrapped),0)
            FROM inventory_lots WHERE inventory_type='finished'
            """
        )
    ).one()
    reservations = session.execute(
        text(
            """
            SELECT count(*),
                   coalesce(sum(reserved_stock_quantity),0),
                   coalesce(sum(consumed_stock_quantity),0),
                   coalesce(sum(released_stock_quantity),0),
                   coalesce(sum(credited_requirement_quantity),0),
                   coalesce(sum(consumed_requirement_quantity),0),
                   coalesce(sum(released_requirement_quantity),0)
            FROM inventory_reservations
            """
        )
    ).one()
    counts = {
        table: int(session.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())
        for table in (
            "inventory_pallets",
            "inventory_pallet_items",
            "inventory_location_movements",
            "inventory_lot_transfers",
            "inventory_movements",
            "operation_logs",
        )
    }
    return {
        "finished": list(finished),
        "reservations": list(reservations),
        "counts": counts,
    }


def create_locked_online_backup(
    database: Path, backup_dir: Path, *, suffix: str
) -> dict[str, Any]:
    backup_dir = backup_dir.resolve()
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup = backup_dir / f"carton_erp_{timestamp}_{suffix}.sqlite3"
    temporary = backup.with_suffix(".sqlite3.tmp")
    if backup.exists() or temporary.exists():
        raise RuntimeError("备份目标已存在，停止覆盖")
    source = open_readonly(database)
    try:
        with closing(sqlite3.connect(temporary, timeout=30)) as target:
            source.backup(target)
            target.commit()
    finally:
        source.close()
    with closing(sqlite3.connect(temporary, timeout=30)) as check:
        integrity = str(check.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = len(check.execute("PRAGMA foreign_key_check").fetchall())
    if integrity.lower() != "ok" or foreign_keys:
        temporary.unlink(missing_ok=True)
        raise RuntimeError("在线备份完整性或外键检查失败")
    temporary.replace(backup)
    return {
        "path": str(backup),
        "sha256": _sha256_file(backup),
        "size": backup.stat().st_size,
        "integrity_check": integrity,
        "foreign_key_violations": foreign_keys,
    }


def _create_write_engine(database: Path):
    from sqlalchemy import create_engine, event

    engine = create_engine(
        f"sqlite+pysqlite:///{database.resolve().as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 30},
        future=True,
    )

    @event.listens_for(engine, "connect")
    def configure(connection: sqlite3.Connection, _record) -> None:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")

    return engine


def apply_plan(
    *,
    database: Path,
    runtime_map: Path,
    expected_plan_sha: str,
    database_role: str,
    authorization: str,
    operator_name: str,
    operator_user_id: int | None,
    backup_dir: Path | None,
) -> dict[str, Any]:
    database = database.resolve(strict=True)
    expected_authorization = (
        FORMAL_AUTHORIZATION
        if database_role == "formal"
        else REHEARSAL_AUTHORIZATION
    )
    if authorization != expected_authorization:
        raise RuntimeError("授权口令与数据库角色不匹配")
    if database_role == "formal":
        if database != FORMAL_DATABASE.resolve(strict=True):
            raise RuntimeError("formal 角色只允许固定正式数据库绝对路径")
        if backup_dir is None:
            raise RuntimeError("正式执行必须指定备份目录")
    elif database_role == "isolated-rehearsal":
        if database == FORMAL_DATABASE.resolve(strict=True):
            raise RuntimeError("隔离演练角色禁止指向正式数据库")
    else:
        raise RuntimeError("database-role 只能是 formal 或 isolated-rehearsal")
    operator_name = operator_name.strip()
    if not operator_name:
        raise RuntimeError("必须记录操作者名称")

    initial_plan = build_plan(database, runtime_map)
    if initial_plan["plan_sha256"] != expected_plan_sha:
        raise RuntimeError("当前计划 SHA 与授权计划不一致，停止写入")
    before = database_snapshot(database)
    if before["integrity_check"].lower() != "ok" or before["foreign_key_violations"]:
        raise RuntimeError("数据库写入前完整性检查失败")

    from sqlalchemy.orm import Session

    from app.models.audit import OperationLog
    from app.models.user import User
    from app.services.warehouse_movement_batch import (
        BATCH_AUDIT_ACTION_CODE,
        WarehouseMovementBatchItem,
        execute_warehouse_movement_batch,
        movement_batch_request_hash,
    )

    engine = _create_write_engine(database)
    backup: dict[str, Any] | None = None
    batch_id = "p1105-" + expected_plan_sha[:48]
    result: dict[str, Any] | None = None
    transaction_before: dict[str, Any] | None = None
    transaction_after: dict[str, Any] | None = None
    transaction_conservation: dict[str, bool] | None = None
    try:
        with Session(
            bind=engine,
            autoflush=False,
            expire_on_commit=False,
        ) as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            locked_plan = build_plan(database, runtime_map)
            if locked_plan["plan_sha256"] != expected_plan_sha:
                raise RuntimeError("取得正式写锁后计划发生变化，整体回滚")
            transaction_before = session_snapshot(session)
            if database_role == "formal":
                assert backup_dir is not None
                backup = create_locked_online_backup(
                    database,
                    backup_dir,
                    suffix="FINAL_P1_105_BEFORE_FINISHED_RELOCATION",
                )
                backup_plan = build_plan(Path(backup["path"]), runtime_map)
                if backup_plan["plan_sha256"] != expected_plan_sha:
                    raise RuntimeError("时点备份与授权计划不一致，整体回滚")

            operator = None
            if operator_user_id is not None:
                operator = session.get(User, int(operator_user_id))
                if operator is None or not operator.is_active:
                    raise RuntimeError("指定操作员不存在或已停用")
                if (
                    operator.role not in {"admin", "boss"}
                    or operator.customer_access_mode != "all"
                ):
                    raise RuntimeError("正式批量搬位操作员必须是全客户范围的老板或管理员")
            items = [
                WarehouseMovementBatchItem(
                    client_item_id=str(item["client_item_id"]),
                    operation=str(item["operation"]),
                    target_location_id=int(item["target_location_id"]),
                    expected_version=int(item["expected_version"]),
                    expected_target_layout_version=int(
                        item["expected_target_layout_version"]
                    ),
                    pallet_id=(
                        int(item["pallet_id"])
                        if item["operation"] == "pallet_move"
                        else None
                    ),
                    lot_id=(
                        int(item["lot_id"])
                        if item["operation"] == "lot_transfer"
                        else None
                    ),
                    quantity=(
                        int(item["quantity"])
                        if item["operation"] == "lot_transfer"
                        else None
                    ),
                    remarks=f"{TASK_ID} 老板授权：待定位正数成品搬入三楼实测空位",
                )
                for item in locked_plan["operations"]
            ]
            request_hash = movement_batch_request_hash(
                batch_id=batch_id,
                items=items,
            )
            result = execute_warehouse_movement_batch(
                session,
                batch_id=batch_id,
                items=items,
                operator_id=(int(operator_user_id) if operator_user_id is not None else None),
            )
            audit_result = {**result, "request_hash": request_hash}
            session.add(
                OperationLog(
                    user_id=(int(operator_user_id) if operator_user_id is not None else None),
                    action="MOVE_BATCH",
                    resource="warehouse/twin-operations/move-batches",
                    details=json.dumps(
                        {
                            "task_id": TASK_ID,
                            "authorization": FORMAL_AUTHORIZATION
                            if database_role == "formal"
                            else REHEARSAL_AUTHORIZATION,
                            "plan_sha256": expected_plan_sha,
                            "result": audit_result,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    username=(operator.username if operator is not None else operator_name),
                    role=(operator.role if operator is not None else "owner_authorized_admin_task"),
                    entity_type="warehouse_movement_batch",
                    description="P1-105 正数成品三楼实测位置受控搬位",
                    event_category="business",
                    result="success",
                    source="admin_script",
                    module_code="warehouse",
                    action_code=BATCH_AUDIT_ACTION_CODE,
                    actor_user_id_snapshot=(
                        int(operator_user_id) if operator_user_id is not None else None
                    ),
                    operator_name_snapshot=(
                        operator.display_name
                        if operator is not None and operator.display_name
                        else operator_name
                    ),
                    object_ref=batch_id,
                    request_id=expected_plan_sha[:64],
                    batch_id=batch_id,
                    schema_version=1,
                )
            )
            session.flush()
            transaction_after = session_snapshot(session)
            loose_count = sum(
                1
                for item in locked_plan["operations"]
                if item["operation"] == "lot_transfer"
            )
            transaction_conservation = {
                "finished_balances": (
                    transaction_before["finished"] == transaction_after["finished"]
                ),
                "reservations": (
                    transaction_before["reservations"]
                    == transaction_after["reservations"]
                ),
                "lot_transfer_count": (
                    transaction_after["counts"]["inventory_lot_transfers"]
                    - transaction_before["counts"]["inventory_lot_transfers"]
                    == loose_count
                ),
                "pallet_count": (
                    transaction_after["counts"]["inventory_pallets"]
                    - transaction_before["counts"]["inventory_pallets"]
                    == loose_count
                ),
                "pallet_item_count": (
                    transaction_after["counts"]["inventory_pallet_items"]
                    - transaction_before["counts"]["inventory_pallet_items"]
                    == loose_count
                ),
                "location_movement_count": (
                    transaction_after["counts"]["inventory_location_movements"]
                    - transaction_before["counts"]["inventory_location_movements"]
                    == sum(
                        1
                        for item in locked_plan["operations"]
                        if item["operation"] == "pallet_move"
                    )
                ),
                "operation_log_count": (
                    transaction_after["counts"]["operation_logs"]
                    - transaction_before["counts"]["operation_logs"]
                    == 1
                ),
            }
            if not all(transaction_conservation.values()):
                raise RuntimeError("写锁内搬位守恒失败，整体回滚")
            session.commit()
    except Exception:
        engine.dispose()
        raise
    engine.dispose()

    after = database_snapshot(database)
    post_plan_error: str | None = None
    try:
        remaining = build_plan(database, runtime_map)
        remaining_summary = remaining["summary"]
    except RuntimeError as error:
        if "当前没有待定位正数成品" in str(error):
            remaining_summary = {
                "unmapped_lot_count": 0,
                "operation_count": 0,
                "total_quantity": 0,
            }
        else:
            post_plan_error = str(error)
            remaining_summary = None

    assert transaction_before is not None
    assert transaction_after is not None
    assert transaction_conservation is not None
    post_commit_checks = {
        "integrity": after["integrity_check"].lower() == "ok",
        "foreign_keys": after["foreign_key_violations"] == 0,
        "all_located": bool(
            remaining_summary is not None
            and int(remaining_summary["unmapped_lot_count"]) == 0
        ),
    }
    post_commit_global_drift = {
        "finished_balances_changed": before["finished"] != after["finished"],
        "reservations_changed": before["reservations"] != after["reservations"],
    }
    status = (
        "committed_verified"
        if all(post_commit_checks.values()) and not post_plan_error
        else "committed_followup_required"
    )
    return {
        "task_id": TASK_ID,
        "status": status,
        "database_role": database_role,
        "database": str(database),
        "plan_sha256": expected_plan_sha,
        "batch_id": batch_id,
        "operator_name": operator_name,
        "operator_user_id": operator_user_id,
        "backup": backup,
        "before": before,
        "after": after,
        "transaction_before": transaction_before,
        "transaction_after": transaction_after,
        "transaction_conservation": transaction_conservation,
        "post_commit_checks": post_commit_checks,
        "post_commit_global_drift": post_commit_global_drift,
        "post_plan_error": post_plan_error,
        "remaining": remaining_summary,
        "result": result,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="P1-105 正数成品三楼实测位置受控搬位"
    )
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--runtime-map", required=True, type=Path)
    parser.add_argument("--plan-output", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--database-role",
        choices=("formal", "isolated-rehearsal"),
    )
    parser.add_argument("--authorization")
    parser.add_argument("--expected-plan-sha")
    parser.add_argument("--operator-name")
    parser.add_argument("--operator-user-id", type=int)
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--result-output", type=Path)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    arguments = build_parser().parse_args(list(argv) if argv is not None else None)
    plan = build_plan(arguments.database, arguments.runtime_map)
    written = None
    if arguments.plan_output is not None:
        written = write_plan(plan, arguments.plan_output)
    if not arguments.apply:
        print(
            json.dumps(
                {
                    "task_id": TASK_ID,
                    "mode": "read-only-plan",
                    "plan_sha256": plan["plan_sha256"],
                    "summary": plan["summary"],
                    "plan_output": [str(path) for path in written] if written else None,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    required = {
        "database_role": arguments.database_role,
        "authorization": arguments.authorization,
        "expected_plan_sha": arguments.expected_plan_sha,
        "operator_name": arguments.operator_name,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError("正式/演练写入缺少参数：" + ", ".join(missing))
    result = apply_plan(
        database=arguments.database,
        runtime_map=arguments.runtime_map,
        expected_plan_sha=str(arguments.expected_plan_sha),
        database_role=str(arguments.database_role),
        authorization=str(arguments.authorization),
        operator_name=str(arguments.operator_name),
        operator_user_id=arguments.operator_user_id,
        backup_dir=arguments.backup_dir,
    )
    if arguments.result_output is not None:
        output = (
            arguments.result_output
            if arguments.result_output.is_absolute()
            else Path.cwd() / arguments.result_output
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(
        json.dumps(
            {
                "task_id": TASK_ID,
                "mode": arguments.database_role,
                "plan_sha256": result["plan_sha256"],
                "batch_id": result["batch_id"],
                "operation_count": len(result["result"]["items"]),
                "status": result["status"],
                "transaction_conservation": result["transaction_conservation"],
                "post_commit_checks": result["post_commit_checks"],
                "post_commit_global_drift": result["post_commit_global_drift"],
                "remaining": result["remaining"],
                "backup": result["backup"],
                "result_output": str(
                    arguments.result_output
                    if arguments.result_output.is_absolute()
                    else Path.cwd() / arguments.result_output
                )
                if arguments.result_output
                else None,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
