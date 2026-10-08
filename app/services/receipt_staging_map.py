"""Carry unchanged, explicitly configured receipt staging across map revisions.

This only updates the area policy's publication/version and audit. It never
creates locations or changes stock, physical layout, area usage or map files.
The caller owns the transaction and the map publication lock.
"""
from __future__ import annotations

import json
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models.audit import OperationLog
from app.models.receipt_putaway import ReceiptStagingArea
from app.models.warehouse_inventory import WarehouseArea, WarehouseFloor, WarehouseLocation
from app.services.audit_log import append_audit_event
from app.services.warehouse_area_activation import WarehouseAreaActivationError, _advance_policy_version
from app.services.warehouse_floor_claim import claim_warehouse_floor_projection

ACTION = "warehouse.receipt_staging.map_carry"


def fingerprint(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), default=str).encode()).hexdigest()


def _reject(message):
    raise WarehouseAreaActivationError(message, status_code=409)


def _bbox(feature):
    points = feature.get("points") or []
    if not points:
        return None
    return (min(p[0] for p in points), min(p[1] for p in points),
            max(p[0] for p in points), max(p[1] for p in points))


def _nearby(floor, zone):
    bounds = _bbox(zone)
    if bounds is None:
        _reject("待入库区域缺少已发布边界，请在仓库地图核对")
    result = {}
    for feature in floor.get("features", []):
        other = _bbox(feature)
        if other and not (other[2] < bounds[0] or other[0] > bounds[2]
                          or other[3] < bounds[1] or other[1] > bounds[3]):
            result[str(feature["id"])] = feature
    return result


def preview_staging_transition(db, *, source_floor, target_floor, area_ids=None):
    if source_floor.get("floor_code") != target_floor.get("floor_code"):
        _reject("地图楼层不一致，不能承接待入库区域")
    source_revision, target_revision = source_floor.get("revision"), target_floor.get("revision")
    if not source_revision or not target_revision:
        _reject("地图缺少正式版本，不能承接待入库区域")
    requested = set(area_ids) if area_ids is not None else None
    result = {"schema_version": 1, "source_revision": source_revision,
              "target_revision": target_revision,
              "source_floor_sha256": fingerprint(source_floor),
              "target_floor_sha256": fingerprint(target_floor), "areas": []}
    if target_floor.get("floor_code") != "1F":
        return result
    areas = list(db.scalars(select(WarehouseArea).join(ReceiptStagingArea)
                 .join(WarehouseFloor, WarehouseFloor.id == WarehouseArea.floor_id)
                 .where(WarehouseFloor.floor_number == 1).order_by(WarehouseArea.id)
                 .options(selectinload(WarehouseArea.storage_policy), selectinload(WarehouseArea.floor))
                 .execution_options(populate_existing=True)))
    if requested is not None and requested - {area.id for area in areas}:
        _reject("所选区域不是正式一楼待入库区域")
    for area in areas:
        if requested is not None and area.id not in requested:
            continue
        policy = area.storage_policy
        if policy and policy.published_map_revision == target_revision:
            continue
        prefix = f"待入库区域 {area.area_code}"
        if (area.construction_status != "enabled" or area.floor.construction_status != "enabled"
                or not policy or policy.status != "published" or policy.draft_map_revision
                or policy.published_map_revision != source_revision):
            _reject(prefix + "发布依据已变化，请在仓库地图核对")
        old = next((x for x in source_floor.get("features", []) if x["id"] == policy.map_feature_id), None)
        new = next((x for x in target_floor.get("features", []) if x["id"] == policy.map_feature_id), None)
        if (not old or not new or old != new or old.get("no_stacking")
                or old.get("feature_kind") != "zone"
                or old.get("erp_area_code") != area.area_code
                or old.get("storage_layout") != policy.storage_layout
                or source_floor.get("bounds_mm") != target_floor.get("bounds_mm")
                or _nearby(source_floor, old) != _nearby(target_floor, new)):
            _reject(prefix + "边界、用途或邻近地图发生变化，须重新核对发布")
        locations = list(db.scalars(select(WarehouseLocation).where(
            WarehouseLocation.warehouse_floor == 1, WarehouseLocation.area_code == area.area_code,
            WarehouseLocation.is_active.is_(True)).order_by(WarehouseLocation.id)
            .options(selectinload(WarehouseLocation.floor3_layout)).execution_options(populate_existing=True)))
        if not locations:
            _reject(prefix + "没有启用货位")
        snapshots = []
        for loc in locations:
            layout = loc.floor3_layout
            if (loc.address_kind != "functional" or loc.source_version != "CURRENT_MAP"
                    or loc.placement_status != "placed" or not layout or int(layout.version) < 1):
                _reject(prefix + "存在非原待入库功能位，须按正式排位流程核对")
            geometry = {key: str(getattr(layout, key)) for key in
                        ("left_pct", "top_pct", "width_pct", "height_pct")}
            if any(float(value) < 0 for value in geometry.values()) or any(
                float(geometry[start]) + float(geometry[size]) > 100.0001
                for start, size in (("left_pct", "width_pct"), ("top_pct", "height_pct"))
            ):
                _reject(prefix + "存在越界货位，不能自动承接")
            snapshots.append({"id": loc.id, "address_version": loc.address_version,
                              "address_area_id": loc.address_area_id, "code": loc.location_code,
                              "warehouse_type": loc.warehouse_type, "storage_type": loc.storage_type,
                              "layout_version": layout.version, "geometry": geometry})
        result["areas"].append({"area_id": area.id, "area_code": area.area_code,
            "policy_id": policy.id, "policy_version": policy.version,
            "allowed_inventory_types_json": policy.allowed_inventory_types_json,
            "feature_id": policy.map_feature_id, "feature_sha256": fingerprint(new),
            "location_ids": [x["id"] for x in snapshots],
            "locations_sha256": fingerprint(snapshots)})
    return result


def apply_staging_transition(db, *, source_floor, target_floor, expected_plan,
                             actor, operation_key, request=None):
    if not operation_key or not actor or not actor.id:
        _reject("待入库地图承接缺少操作身份")
    request_id = fingerprint({"action": ACTION, "key": operation_key})
    expected_hash = fingerprint(expected_plan)
    prior = db.scalar(select(OperationLog).where(OperationLog.action_code == ACTION,
        OperationLog.request_id == request_id, OperationLog.result == "success"))
    if prior is not None:
        details = json.loads(prior.details or "{}")
        if details.get("plan_sha256") != expected_hash:
            _reject("同一地图承接操作已使用其他内容")
        return details
    if not claim_warehouse_floor_projection(db, floor_number=1):
        _reject("一楼地图正在变化，请刷新重试")
    fresh = preview_staging_transition(db, source_floor=source_floor, target_floor=target_floor,
                                     area_ids=[x["area_id"] for x in expected_plan["areas"]])
    if fingerprint(fresh) != expected_hash:
        _reject("待入库区域或货位已变化，请重新预览")
    for row in fresh["areas"]:
        policy = db.get(WarehouseArea, row["area_id"]).storage_policy
        _advance_policy_version(db, policy=policy, operator_id=actor.id,
                                published_map_revision=target_floor["revision"])
    details = {"plan_sha256": expected_hash, "source_revision": source_floor["revision"],
        "target_revision": target_floor["revision"],
        "areas": [{"area_id": row["area_id"], "area_code": row["area_code"],
                   "before_version": row["policy_version"], "after_version": row["policy_version"] + 1,
                   "location_count": len(row["location_ids"]),
                   "locations_sha256": row["locations_sha256"]} for row in fresh["areas"]]}
    saved = append_audit_event(db, actor=actor, request=request, event_category="business",
        result="success", source="web", module_code="warehouse", action_code=ACTION,
        resource="ReceiptStagingArea", entity_type="receipt_staging_map", entity_id=1,
        request_id=request_id, description="核验未变化的待入库区域并承接当前地图；货位和库存保持", details=details)
    if json.loads(saved.details or "{}") != details:
        _reject("地图承接审计未完整保存，本次应撤回")
    db.flush()
    return details


def carry_staging_during_map_publish(db, *, source_floor, target_floor, actor, operation_key, request=None):
    if not source_floor or source_floor.get("revision") == target_floor.get("revision"):
        return 0
    plan = preview_staging_transition(db, source_floor=source_floor, target_floor=target_floor)
    if not plan["areas"]:
        return 0
    apply_staging_transition(db, source_floor=source_floor, target_floor=target_floor,
        expected_plan=plan, actor=actor, operation_key=operation_key + ":receipt-staging", request=request)
    return len(plan["areas"])
