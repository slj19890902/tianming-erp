"""Verify preserved ground positions against a newly applied measured map.

Original plans/slots remain historical facts. The existing audit ledger records
which unchanged location snapshots were verified during map application.
"""
from __future__ import annotations

from hashlib import sha256
from decimal import Decimal
import json

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.models.audit import OperationLog
from app.models.warehouse_inventory import (
    WarehouseArea, WarehouseFloor, WarehouseGroundLayoutPlan,
    WarehouseGroundLayoutSlot, WarehouseLocation,
)
from app.services.audit_log import append_audit_event
from app.services.warehouse_area_activation import WarehouseAreaActivationError
from app.services.warehouse_floor1_candidate_planner import (
    Floor1CandidatePlanningError, _percent_round_trip_epsilon, validate_capacity_layout_slots_for_zone,
)

ACTION = "warehouse.ground_layout.map_application"


def location_signature(location, layout):
    fields = [location.id, location.is_active, location.warehouse_floor,
              location.area_code, location.address_area_id, location.source_version,
              location.address_kind, location.ground_row_no, location.slot_no,
              location.placement_status, location.storage_type, layout.version,
              *(format(Decimal(str(getattr(layout, key))) or Decimal(0), '.4f') for key in
                ('left_pct', 'top_pct', 'width_pct', 'height_pct')), layout.layout_kind]
    return sha256(json.dumps(fields, separators=(",", ":")).encode()).hexdigest()


def load_map_applications(db, plan_ids):
    if not plan_ids:
        return {}
    latest = select(func.max(OperationLog.id)).where(
        OperationLog.module_code == "warehouse", OperationLog.action_code == ACTION,
        OperationLog.result == "success", OperationLog.entity_id.in_(plan_ids),
    ).group_by(OperationLog.entity_id)
    return {row.entity_id: json.loads(row.details or "{}") for row in db.scalars(
        select(OperationLog).where(OperationLog.id.in_(latest)))}


def application_matches(receipt, *, plan_id, plan_version, area_id, policy,
                        revision, location, layout):
    return bool(receipt and policy and location.is_active and layout
        and receipt.get("plan_id") == plan_id and receipt.get("plan_version") == plan_version
        and receipt.get("area_id") == area_id and receipt.get("map_revision") == revision
        and receipt.get("policy_version") == policy.version
        and receipt.get("map_feature_id") == policy.map_feature_id
        and receipt.get("locations", {}).get(str(location.id)) == location_signature(location, layout))


def record_map_applications(db, *, floor_layout, actor, operation_key, request=None, previous_floor_layout=None, coordinate_adjustments=None):
    plans = list(db.scalars(select(WarehouseGroundLayoutPlan)
        .join(WarehouseArea, WarehouseArea.id == WarehouseGroundLayoutPlan.area_id)
        .join(WarehouseFloor, WarehouseFloor.id == WarehouseArea.floor_id)
        .where(WarehouseFloor.floor_code == floor_layout["floor_code"],
               WarehouseGroundLayoutPlan.status == "published")
        .options(selectinload(WarehouseGroundLayoutPlan.area).selectinload(WarehouseArea.storage_policy),
                 selectinload(WarehouseGroundLayoutPlan.slots)
                 .selectinload(WarehouseGroundLayoutSlot.location)
                 .selectinload(WarehouseLocation.floor3_layout))))
    previous = load_map_applications(db, [p.id for p in plans])
    changed = 0
    adjustment_by_location = {row['location_id']: row for row in (coordinate_adjustments or [])}
    for plan in plans:
        policy = plan.area.storage_policy
        if not policy or policy.status != "published" or policy.storage_layout not in {"pallet_ground", "mixed"}:
            continue
        slots = [s for s in plan.slots if s.location.is_active]
        if not slots:
            continue
        if policy.published_map_revision != floor_layout["revision"]:
            raise WarehouseAreaActivationError("区域与应用地图版本不一致，未确认旧货位", status_code=409)
        feature = next((f for f in floor_layout.get("features", []) if f["id"] == policy.map_feature_id), None)
        if feature is None or any(s.location.floor3_layout is None for s in slots):
            raise WarehouseAreaActivationError("旧排位缺少区域或货位几何，不能应用地图", status_code=409)
        from app.services.warehouse_ground_slots import effective_ground_slot_geometries
        effective = effective_ground_slot_geometries(plan, floor_layout)
        payloads = [{**effective[s.location_id], "location_id": s.location_id,
            "layout_kind": s.location.floor3_layout.layout_kind} for s in slots]
        adjustment_candidate = bool(slots) and all(
            slot.location_id in adjustment_by_location for slot in slots
        )
        if adjustment_candidate:
            xs = [float(point[0]) for point in feature["points"]]
            ys = [float(point[1]) for point in feature["points"]]
            left, bottom = min(xs), min(ys)
            width, height = max(xs) - left, max(ys) - bottom
            payloads = []
            for slot in slots:
                absolute = adjustment_by_location[slot.location_id]["absolute"]
                payloads.append({
                    "location_id": slot.location_id,
                    "left_pct": (float(absolute["x_mm"]) - left) / width * 100,
                    "top_pct": (bottom + height - float(absolute["y_mm"]) - float(absolute["depth_mm"])) / height * 100,
                    "width_pct": float(absolute["width_mm"]) / width * 100,
                    "height_pct": float(absolute["depth_mm"]) / height * 100,
                    "layout_kind": slot.location.floor3_layout.layout_kind,
                })
        try:
            measured = validate_capacity_layout_slots_for_zone(
                floor_layout,
                feature_id=policy.map_feature_id,
                slots=payloads,
                # A later map save must preserve previously accepted red
                # diagnostics. Verify coordinate identity below, not occupancy.
                allow_spatial_conflicts=True,
            )
        except Floor1CandidatePlanningError as error:
            raise WarehouseAreaActivationError(str(error), status_code=error.status_code) from error
        tolerance = float(_percent_round_trip_epsilon(feature["points"]))
        direct = all(all(abs(float(getattr(original, key)) - float(actual[key])) <= tolerance
                         for key in ("x_mm", "y_mm", "width_mm", "depth_mm"))
                     for original, actual in zip(slots, measured, strict=True))
        previous_feature = next((f for f in (previous_floor_layout or floor_layout).get("features", [])
                                 if f["id"] == policy.map_feature_id), None)
        # The imported P0-26 plans in the September export retain a min-Y
        # row origin while their current location percentages use max-Y/top.
        # Accept only the exact area-wide reflection, with unchanged boundaries.
        # Reflection preserves adjacency; do not rewrite or renumber old slots.
        legacy_reflected = (not direct and str(plan.publish_idempotency_key or "").startswith("p0-26-current-map-e5f192ba605185db:publish:")
            and previous_feature is not None and previous_feature["points"] == feature["points"]
            and all(s.location.source_version == "CURRENT_MAP" for s in slots))
        if legacy_reflected:
            y_sum = min(p[1] for p in feature["points"]) + max(p[1] for p in feature["points"])
            legacy_reflected = all(
                abs(y_sum - float(original.y_mm) - float(original.depth_mm) - float(actual["y_mm"])) <= tolerance
                and all(abs(float(getattr(original, key)) - float(actual[key])) <= tolerance
                        for key in ("x_mm", "width_mm", "depth_mm"))
                for original, actual in zip(slots, measured, strict=True))
        # An explicit coordinate draft was checked against the source map and
        # every active location signature in this same publish transaction.
        # Old plans/slots are immutable; verify the complete after-image here.
        authorized_adjustment = bool(slots) and all(
            (proof := adjustment_by_location.get(original.location_id)) is not None
            and proof['feature_id'] == policy.map_feature_id
            and proof['source_map_revision'] == (previous_floor_layout or {}).get('revision')
            and proof['target_map_revision'] == floor_layout['revision']
            and proof['after_version'] == original.location.floor3_layout.version
            and all(abs(float(getattr(original.location.floor3_layout, key))-float(proof['after'][key])) < 0.00001
                    for key in ('left_pct','top_pct','width_pct','height_pct'))
            and all(abs(float(actual[key])-float(proof['absolute'][key])) <= tolerance
                    for key in ('x_mm','y_mm','width_mm','depth_mm'))
            for original, actual in zip(slots, measured, strict=True))
        prior = previous.get(plan.id) or {}
        previously_verified = bool(previous_floor_layout and previous_feature
            and previous_feature['points'] == feature['points']
            and prior.get('plan_id') == plan.id and prior.get('plan_version') == plan.version
            and prior.get('area_id') == plan.area_id and prior.get('map_feature_id') == policy.map_feature_id
            and prior.get('map_revision') == previous_floor_layout.get('revision')
            and prior.get('locations') == {str(s.location_id): location_signature(s.location, s.location.floor3_layout) for s in slots})
        unchanged_feature_geometry = bool(
            previous_floor_layout
            and previous_feature
            and previous_feature['points'] == feature['points']
        )
        if not direct and not legacy_reflected and not authorized_adjustment and not previously_verified and not unchanged_feature_geometry:
            raise WarehouseAreaActivationError(
                f"区域 {plan.area.area_code} 保留货位与原排位物理坐标不一致，不能随地图应用", status_code=409)
        details = {"plan_id": plan.id, "plan_version": plan.version, "area_id": plan.area_id,
            "original_map_revision": plan.published_map_revision, "map_revision": floor_layout["revision"],
            "legacy_y_reflection": bool(legacy_reflected),
            "unchanged_feature_geometry": unchanged_feature_geometry,
            # Detailed before/after coordinates are in TWIN_LAYOUT_PUBLISH.
            # Keep the operational receipt small enough for the audit serializer.
            "coordinate_adjustment_count": len(slots) if authorized_adjustment else 0,
            "coordinate_adjustments_sha256": sha256(json.dumps(
                [adjustment_by_location[s.location_id] for s in slots] if authorized_adjustment else [],
                sort_keys=True, default=str).encode()).hexdigest(),
            "policy_version": policy.version, "map_feature_id": policy.map_feature_id,
            "locations": {str(s.location_id): location_signature(s.location, s.location.floor3_layout) for s in slots}}
        if previous.get(plan.id) == details:
            continue
        saved = append_audit_event(db, actor=actor, request=request, event_category="business", result="success",
            source="web", module_code="warehouse", action_code=ACTION,
            resource="WarehouseGroundLayoutPlan", entity_type="warehouse_ground_layout_plan", entity_id=plan.id,
            request_id=sha256(f"{operation_key}:{plan.id}".encode()).hexdigest(),
            description="应用地图时核验保留货位；原排位、编号及库存未改写", details=details)
        if json.loads(saved.details or '{}') != details:
            raise WarehouseAreaActivationError('地图应用回执未完整保存，已撤回本次应用；草稿保留', status_code=409)
        changed += 1
    return changed
