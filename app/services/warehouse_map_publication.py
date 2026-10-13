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

from app.models.warehouse_inventory import WarehouseArea, WarehouseLocation
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


def carry_retired_rack_area(db, *, previous_floor_layout, floor_layout, retired_rack_id,
                           actor, operation_key, request=None):
    """Carry one explicitly retired empty rack; all remaining assets must match.

    The API claims/archives the exact empty location set before calling this.
    Recheck the absence of active references and retain an explicit retirement
    audit instead of describing this changed area as wholly unchanged.
    """
    source_racks = [r for r in previous_floor_layout.get("racks", []) if r.get("id") == retired_rack_id]
    tombstones = [r for r in floor_layout.get("retired_racks", []) if r.get("id") == retired_rack_id]
    if (len(source_racks) != 1 or len(tombstones) != 1
            or any(r.get("id") == retired_rack_id for r in floor_layout.get("racks", []))
            or any(tombstones[0].get(k) != v for k, v in source_racks[0].items())
            or db.scalar(select(WarehouseLocation.id).where(
                WarehouseLocation.map_rack_id == retired_rack_id,
                WarehouseLocation.is_active.is_(True)).limit(1)) is not None):
        raise WarehouseAreaActivationError("删除货架的正式身份或库位归档未完成", status_code=409)
    source = dict(previous_floor_layout, racks=[r for r in previous_floor_layout.get("racks", [])
                                              if r.get("id") != retired_rack_id])
    changed = 0
    for area in published_floor_areas(db, floor_layout["floor_code"]):
        policy = area.storage_policy
        if not policy or policy.map_feature_id != source_racks[0].get("area_feature_id"):
            continue
        if not unchanged_area(area, source, floor_layout):
            # The final full-floor assertion rejects an unproved published area.
            continue
        old_revision, old_version = policy.published_map_revision, policy.version
        _advance_policy_version(db, policy=policy, operator_id=actor.id,
                                published_map_revision=floor_layout["revision"])
        details = dict(area_id=area.id, map_feature_id=policy.map_feature_id,
            previous_map_revision=old_revision, map_revision=floor_layout["revision"],
            previous_policy_version=old_version, policy_version=policy.version,
            retired_rack_id=retired_rack_id, remaining_area_and_assets_unchanged=True)
        saved = append_audit_event(db, actor=actor, request=request,
            event_category="business", result="success", source="web", module_code="warehouse",
            action_code=ACTION, resource="WarehouseAreaStoragePolicy",
            entity_type="warehouse_area_storage_policy", entity_id=policy.id,
            request_id=sha256(f"{operation_key}:retire:{policy.id}:{old_revision}:{floor_layout['revision']}".encode()).hexdigest(),
            description="空架及库位受控归档后核验其余区域身份并同步地图版本", details=details)
        if json.loads(saved.details or "{}") != details:
            raise WarehouseAreaActivationError("地图同步审计未完整保存，已撤回本次发布", status_code=409)
        changed += 1
    return changed


def complete_floor_publication(db, *, previous_floor_layout, floor_layout, actor,
                               operation_key, request=None, isolated_area_feature_id=None,
                               coordinate_adjustments=None, validate_geometry=None,
                               retired_rack_id=None):
    """One finalization gate for standard publish and scoped destructive edits.

    Caller holds the floor/file lock and owns the existing file/DB rollback.
    No commit here: bindings, ground/staging receipts and business audit share
    the caller's transaction.
    """
    area_count = carry_unchanged_area_policies(db,
        previous_floor_layout=previous_floor_layout, floor_layout=floor_layout,
        actor=actor, operation_key=operation_key, request=request,
        excluded_feature_id=isolated_area_feature_id)
    if retired_rack_id is not None:
        area_count += carry_retired_rack_area(db,
            previous_floor_layout=previous_floor_layout, floor_layout=floor_layout,
            retired_rack_id=retired_rack_id, actor=actor, operation_key=operation_key, request=request)
    if validate_geometry is not None:
        validate_geometry()
    from app.services.warehouse_ground_map_application import record_map_applications
    ground_count = record_map_applications(db, floor_layout=floor_layout, actor=actor,
        operation_key=operation_key, request=request, previous_floor_layout=previous_floor_layout,
        coordinate_adjustments=coordinate_adjustments, isolated_area_feature_id=isolated_area_feature_id)
    rack_count = 0
    if isolated_area_feature_id is not None:
        from app.services.warehouse_rack_map_application import carry_unchanged_rack_policies
        rack_count = carry_unchanged_rack_policies(db,
            previous_floor_layout=previous_floor_layout, floor_layout=floor_layout,
            actor=actor, operation_key=operation_key, request=request,
            excluded_feature_id=isolated_area_feature_id)
    assert_current_floor_bindings(db, floor_layout)
    return area_count, ground_count, rack_count
