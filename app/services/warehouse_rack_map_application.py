"""Carry unchanged, already-published rack areas across a scoped map publish.

The floor revision changes even when a different area's policy is edited. Keep
the strict current-revision read gate; advance only proven unchanged bindings.
"""
from __future__ import annotations

import json
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models.warehouse_inventory import WarehouseArea, WarehouseFloor
from app.services.audit_log import append_audit_event
from app.services.warehouse_area_activation import (
    WarehouseAreaActivationError, _advance_policy_version, policy_inventory_types, warehouse_floor_for_code,
)


def unchanged_rack_area(area, source, target):
    policy = area.storage_policy
    if not (policy and area.construction_status == "enabled"
            and policy.status == "published" and policy.storage_layout == "rack"
            and not policy.draft_map_revision
            and source.get("floor_code") == target.get("floor_code")
            and source.get("revision") and target.get("revision")
            and policy.published_map_revision == source["revision"]
            and source["revision"] != target["revision"]):
        return False
    feature_id = policy.map_feature_id
    features = []
    for floor in (source, target):
        matches = [f for f in floor.get("features", []) if f.get("id") == feature_id]
        if len(matches) != 1:
            return False
        feature = matches[0]
        if (feature.get("feature_kind") != "zone"
                or feature.get("erp_area_code") != area.area_code
                or feature.get("storage_layout") != policy.storage_layout
                or sorted(feature.get("allowed_inventory_types") or []) != sorted(policy_inventory_types(policy))
                or feature.get("formal_area_name") != area.area_name
                or feature.get("formal_area_id") not in (None, area.id)
                or feature.get("formal_floor_id") not in (None, area.floor_id)
                or sum(f.get("erp_area_code") == area.area_code for f in floor.get("features", [])) != 1):
            return False
        features.append(feature)
    if features[0] != features[1]:
        return False
    racks = []
    for floor in (source, target):
        selected = [r for r in floor.get("racks", []) if r.get("area_feature_id") == feature_id]
        ids = [r.get("id") for r in selected]
        if (not selected or not all(ids) or len(set(ids)) != len(ids)
                or any(r.get("area_code") != area.area_code for r in selected)
                or any(sum(r.get("id") == key for r in floor.get("racks", [])) != 1 for key in ids)):
            return False
        racks.append(sorted(selected, key=lambda r: r["id"]))
    return racks[0] == racks[1]


def carry_unchanged_rack_policies(db, *, previous_floor_layout, floor_layout,
                                actor, operation_key, request=None,
                                excluded_feature_id=None, area_ids=None, audit_source="web"):
    """No commit: caller owns map/SQL rollback and the publication transaction."""
    floor = warehouse_floor_for_code(db, floor_layout["floor_code"])
    if floor is None or floor.construction_status != "enabled":
        return 0
    query = (select(WarehouseArea)
             .where(WarehouseArea.floor_id == floor.id)
             .options(selectinload(WarehouseArea.storage_policy)))
    if area_ids is not None:
        query = query.where(WarehouseArea.id.in_(area_ids))
    changed = 0
    for area in db.scalars(query):
        policy = area.storage_policy
        if (not policy or policy.map_feature_id == excluded_feature_id
                or not unchanged_rack_area(area, previous_floor_layout, floor_layout)):
            continue
        old_version = policy.version
        old_revision = policy.published_map_revision
        _advance_policy_version(db, policy=policy, operator_id=actor.id,
                                published_map_revision=floor_layout["revision"])
        details = dict(area_id=area.id, map_feature_id=policy.map_feature_id,
                       previous_map_revision=old_revision, map_revision=floor_layout["revision"],
                       previous_policy_version=old_version, policy_version=policy.version,
                       unchanged_area_and_racks=True)
        saved = append_audit_event(db, actor=actor, request=request,
            event_category="business", result="success", source=audit_source, module_code="warehouse",
            action_code="warehouse.rack_area.map_application", resource="WarehouseAreaStoragePolicy",
            entity_type="warehouse_area_storage_policy", entity_id=policy.id,
            request_id=sha256(f"{operation_key}:{policy.id}".encode()).hexdigest(),
            description="核验货架区域及整架未变化，同步地图版本关联；货位及库存未改写", details=details)
        if json.loads(saved.details or "{}") != details:
            raise WarehouseAreaActivationError("货架地图应用回执未完整保存，已撤回本次应用", status_code=409)
        changed += 1
    return changed
