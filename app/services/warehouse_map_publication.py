"""Keep all published area bindings in the same floor publication transaction.

This advances only proved, unchanged identities. Geometry, locations, stock and
drafts are never rewritten here. The publisher owns the floor claim, commit and
map-file rollback; controlled historical repair supplies the exact source map.
"""
from __future__ import annotations

import json
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models.warehouse_inventory import WarehouseArea
from app.services.audit_log import append_audit_event
from app.services.warehouse_area_activation import (
    WarehouseAreaActivationError, _advance_policy_version,
    policy_inventory_types, warehouse_floor_for_code,
)

ACTION = "warehouse.area.map_application"


def published_floor_areas(db, floor_code):
    floor = warehouse_floor_for_code(db, floor_code)
    if floor is None or floor.construction_status != "enabled":
        return []
    return list(db.scalars(select(WarehouseArea).where(
        WarehouseArea.floor_id == floor.id,
        WarehouseArea.construction_status == "enabled",
    ).options(selectinload(WarehouseArea.storage_policy)).order_by(WarehouseArea.id)))


def unchanged_area(area, source, target):
    policy = area.storage_policy
    if not (policy and policy.status == "published" and not policy.draft_map_revision
            and area.construction_status == "enabled"
            and source.get("floor_code") == target.get("floor_code")
            and source.get("revision") and target.get("revision")
            and policy.published_map_revision == source["revision"]
            and source["revision"] != target["revision"]
            and source.get("bounds_mm") == target.get("bounds_mm")):
        return False
    feature_id = policy.map_feature_id
    selected = []
    for floor in (source, target):
        features = [f for f in floor.get("features", []) if f.get("id") == feature_id]
        if len(features) != 1:
            return False
        feature = features[0]
        if (feature.get("feature_kind") != "zone"
                or feature.get("erp_area_code") != area.area_code
                or feature.get("storage_layout") != policy.storage_layout
                or sorted(feature.get("allowed_inventory_types") or []) != sorted(policy_inventory_types(policy))
                or feature.get("formal_area_name") != area.area_name
                or feature.get("formal_area_id") not in (None, area.id)
                or feature.get("formal_floor_id") not in (None, area.floor_id)
                or sum(f.get("erp_area_code") == area.area_code for f in floor.get("features", [])) != 1):
            return False
        selected.append(feature)
    if selected[0] != selected[1]:
        return False
    for collection in ("racks", "pallets"):
        objects = []
        for floor in (source, target):
            members = [r for r in floor.get(collection, []) if r.get("area_feature_id") == feature_id]
            ids = [r.get("id") for r in members]
            if (not all(ids) or len(ids) != len(set(ids))
                    or any(sum(r.get("id") == key for r in floor.get(collection, [])) != 1 for key in ids)
                    or any(r.get("area_code") not in (None, area.area_code) for r in members)):
                return False
            objects.append(sorted(members, key=lambda r: r["id"]))
        if objects[0] != objects[1]:
            return False
    return True


def carry_unchanged_area_policies(db, *, previous_floor_layout, floor_layout, actor,
                                 operation_key, request=None, area_ids=None,
                                 excluded_feature_id=None, audit_source="web"):
    if not operation_key or not actor or not actor.id:
        raise WarehouseAreaActivationError("地图同步缺少操作身份", status_code=409)
    changed = 0
    for area in published_floor_areas(db, floor_layout["floor_code"]):
        policy = area.storage_policy
        if (area_ids is not None and area.id not in area_ids
                or not policy or policy.map_feature_id == excluded_feature_id
                or not unchanged_area(area, previous_floor_layout, floor_layout)):
            continue
        old_version, old_revision = policy.version, policy.published_map_revision
        _advance_policy_version(db, policy=policy, operator_id=actor.id,
                                published_map_revision=floor_layout["revision"])
        details = dict(area_id=area.id, map_feature_id=policy.map_feature_id,
                       previous_map_revision=old_revision, map_revision=floor_layout["revision"],
                       previous_policy_version=old_version, policy_version=policy.version,
                       unchanged_area_and_assets=True)
        saved = append_audit_event(db, actor=actor, request=request,
            event_category="business", result="success", source=audit_source, module_code="warehouse",
            action_code=ACTION, resource="WarehouseAreaStoragePolicy",
            entity_type="warehouse_area_storage_policy", entity_id=policy.id,
            request_id=sha256(f"{operation_key}:{policy.id}:{old_revision}:{floor_layout['revision']}".encode()).hexdigest(),
            description="核验区域及所属货架未变化，统一正式地图版本；位置及库存保留", details=details)
        if json.loads(saved.details or "{}") != details:
            raise WarehouseAreaActivationError("地图同步审计未完整保存，已撤回本次发布", status_code=409)
        changed += 1
    return changed


def assert_current_floor_bindings(db, floor_layout):
    """Never commit a partial publication while an enabled published area is stale."""
    problems = []
    revision = floor_layout.get("revision")
    for area in published_floor_areas(db, floor_layout["floor_code"]):
        policy = area.storage_policy
        if not policy or policy.status != "published":
            continue
        features = [f for f in floor_layout.get("features", []) if f.get("id") == policy.map_feature_id]
        if (not revision or policy.published_map_revision != revision
                or len(features) != 1 or features[0].get("feature_kind") != "zone"
                or features[0].get("erp_area_code") != area.area_code):
            problems.append(area.area_name or area.area_code)
    if problems:
        raise WarehouseAreaActivationError(
            "地图尚有区域未同步，本次发布已撤回：" + "、".join(problems[:8]), status_code=409)
