from __future__ import annotations

import hashlib
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.core.time_contract import beijing_now_naive
from app.models.warehouse_inventory import (
    WarehouseArea,
    WarehouseGroundLayoutPlan,
    WarehouseGroundLayoutSlot,
    WarehouseLocation,
)
from app.services.warehouse_area_activation import WarehouseAreaActivationError
from app.services.warehouse_floor1_candidate_planner import (
    Floor1CandidatePlanningError,
    _percent_round_trip_epsilon,
    validate_capacity_layout_slots_for_zone,
)
from app.services.warehouse_ground_slots import (
    WarehouseGroundSlotError,
    canonical_hash as ground_canonical_hash,
    ground_preview_fingerprint,
    number_ground_physical_slots,
)
from app.services.warehouse_pallet_standard import standard_pallet_contract


def _layout_geometry_payload(location: WarehouseLocation) -> dict:
    layout = location.floor3_layout
    if layout is None:
        raise WarehouseAreaActivationError(
            f"货位 {location.location_code} 缺少二维位置，请先核对区域货位",
            status_code=409,
        )
    return {
        "location_id": location.id,
        "left_pct": float(layout.left_pct),
        "top_pct": float(layout.top_pct),
        "width_pct": float(layout.width_pct),
        "height_pct": float(layout.height_pct),
        "z_index": layout.z_index,
        "expected_version": layout.version,
        "layout_kind": layout.layout_kind,
    }


def ensure_one_step_ground_plan(
    db: Session,
    *,
    floor_layout: dict,
    feature_id: str,
    area: WarehouseArea,
    storage_layout: str,
    location_count: int,
    operation_key: str,
    operator_id: int,
    materialize: bool = True,
) -> dict:
    """Materialize real one-step ground positions in the canonical plan ledger.

    Legacy one-step positions created before the ground-plan ledger have
    ``layout_kind=unknown`` and ``source_type=manual``.  They are accepted only
    when the current published measured map proves every saved rectangle is an
    exact 1200x1000 pallet footprint.  Logical anchors and non-standard
    geometry remain planning-only and can never become inventory locations.
    """

    existing_plan_query = (
        select(WarehouseGroundLayoutPlan)
        .where(WarehouseGroundLayoutPlan.area_id == area.id)
        .options(selectinload(WarehouseGroundLayoutPlan.slots))
    )
    if materialize:
        existing_plan_query = existing_plan_query.with_for_update()
    existing_plan = db.scalar(existing_plan_query)
    if storage_layout != "pallet_ground":
        if existing_plan is not None:
            raise WarehouseAreaActivationError(
                "该区域已有正式地堆排位，不能从一次确认改成其他布局",
                status_code=409,
            )
        return {
            "available_location_count": max(0, int(location_count)),
            "ground_plan_id": None,
            "ground_plan_status": None,
            "location_readiness_issue": None,
            "legacy_layout_repaired_count": 0,
            "location_ids": [],
        }
    if location_count <= 0:
        if existing_plan is not None:
            raise WarehouseAreaActivationError(
                "该区域已有正式地堆排位，不能从一次确认清空或停用其库位",
                status_code=409,
            )
        return {
            "available_location_count": 0,
            "ground_plan_id": None,
            "ground_plan_status": None,
            "location_readiness_issue": None,
            "legacy_layout_repaired_count": 0,
            "location_ids": [],
        }

    floor = area.floor
    policy = area.storage_policy
    current_revision = str(floor_layout.get("revision") or "").strip()
    if (
        floor is None
        or policy is None
        or policy.status != "published"
        or str(policy.map_feature_id or "").strip() != str(feature_id).strip()
        or str(policy.published_map_revision or "").strip() != current_revision
        or not current_revision
    ):
        raise WarehouseAreaActivationError(
            "区域地图身份尚未完成正式发布，不能生成地堆排位",
            status_code=409,
        )

    rows = list(
        db.scalars(
            select(WarehouseLocation)
            .where(
                WarehouseLocation.warehouse_floor == floor.floor_number,
                func.upper(WarehouseLocation.area_code) == area.area_code.upper(),
                WarehouseLocation.is_active.is_(True),
                WarehouseLocation.placement_status == "placed",
                WarehouseLocation.storage_type == "ground",
            )
            .options(selectinload(WarehouseLocation.floor3_layout))
            .order_by(WarehouseLocation.sort_order, WarehouseLocation.id)
        ).all()
    )
    if len(rows) != int(location_count):
        raise WarehouseAreaActivationError(
            "区域货位数量在确认过程中发生变化，请刷新后重试",
            status_code=409,
        )

    invalid_layout_rows = [
        row
        for row in rows
        if row.floor3_layout is None
        or (
            row.floor3_layout.layout_kind != "physical_pallet"
            and not (
                row.floor3_layout.layout_kind == "unknown"
                and row.floor3_layout.source_type == "manual"
                and str(row.source_version or "").strip().upper() == "TWIN_V1"
            )
        )
    ]
    if invalid_layout_rows:
        if existing_plan is not None:
            raise WarehouseAreaActivationError(
                "该区域正式地堆排位与当前库位几何已经漂移，不能降级为规划位置",
                status_code=409,
            )
        return {
            "available_location_count": 0,
            "ground_plan_id": None,
            "ground_plan_status": "planning_only",
            "location_readiness_issue": (
                "确认容量中含非标准实测栈板位；这些位置仅作规划展示，"
                "不能用于收料、入库或移位"
            ),
            "legacy_layout_repaired_count": 0,
            "location_ids": [],
        }

    layout_slots = [_layout_geometry_payload(row) for row in rows]
    try:
        validated_slots = validate_capacity_layout_slots_for_zone(
            floor_layout,
            feature_id=feature_id,
            slots=layout_slots,
        )
    except Floor1CandidatePlanningError as error:
        raise WarehouseAreaActivationError(
            str(error), status_code=error.status_code
        ) from error

    feature = next(
        (
            row
            for row in floor_layout.get("features") or []
            if row.get("feature_kind") == "zone"
            and str(row.get("id") or "") == str(feature_id)
        ),
        None,
    )
    points = (feature or {}).get("points") or []
    geometry_epsilon = _percent_round_trip_epsilon(points)
    pallet_contract = standard_pallet_contract()
    standard_width = int(pallet_contract["width_mm"])
    standard_depth = int(pallet_contract["depth_mm"])
    matched_slots: list[dict] = []
    for row, layout_slot, actual_slot in zip(
        rows, layout_slots, validated_slots, strict=True
    ):
        layout = row.floor3_layout
        assert layout is not None
        actual_width = float(actual_slot["width_mm"])
        actual_depth = float(actual_slot["depth_mm"])
        standard_orientation = (
            abs(actual_width - standard_width) <= geometry_epsilon
            and abs(actual_depth - standard_depth) <= geometry_epsilon
        )
        rotated_orientation = (
            abs(actual_width - standard_depth) <= geometry_epsilon
            and abs(actual_depth - standard_width) <= geometry_epsilon
        )
        if not standard_orientation and not rotated_orientation:
            if existing_plan is not None:
                raise WarehouseAreaActivationError(
                    "该区域正式地堆排位与当前库位几何已经漂移，不能降级为规划位置",
                    status_code=409,
                )
            return {
                "available_location_count": 0,
                "ground_plan_id": None,
                "ground_plan_status": "planning_only",
                "location_readiness_issue": (
                    "确认位置无法按当前地图还原为1200×1000毫米标准栈板位；"
                    "这些位置不能用于收料、入库或移位"
                ),
                "legacy_layout_repaired_count": 0,
                "location_ids": [],
            }
        width_mm = standard_width if standard_orientation else standard_depth
        depth_mm = standard_depth if standard_orientation else standard_width
        matched_slots.append(
            {
                **layout_slot,
                "x_mm": Decimal(str(actual_slot["x_mm"])).quantize(
                    Decimal("0.001")
                ),
                "y_mm": Decimal(str(actual_slot["y_mm"])).quantize(
                    Decimal("0.001")
                ),
                "width_mm": width_mm,
                "depth_mm": depth_mm,
                "location_code": row.location_code,
                "existing_location_id": int(row.id),
                "existing_layout_version": int(layout.version),
            }
        )
    try:
        matched_slots = number_ground_physical_slots(
            matched_slots,
            numbering_origin="south",
            row_direction="from_aisle_inward",
            slot_direction="left_to_right",
        )
    except WarehouseGroundSlotError as error:
        raise WarehouseAreaActivationError(
            error.message, status_code=error.status_code
        ) from error

    configuration = {
        "target_slot_count": len(rows),
        "numbering_origin": "south",
        "row_direction": "from_aisle_inward",
        "slot_direction": "left_to_right",
        "row_start_no": 1,
        "slot_start_no": 1,
    }
    fingerprint = ground_preview_fingerprint(
        area_id=area.id,
        policy_version=policy.version,
        map_revision=current_revision,
        configuration=configuration,
        slots=matched_slots,
    )
    expected_location_ids = {int(row.id) for row in rows}
    if existing_plan is not None:
        existing_location_ids = {
            int(slot.location_id) for slot in existing_plan.slots
        }
        expected_by_location_id = {
            int(slot["existing_location_id"]): slot for slot in matched_slots
        }
        epsilon = Decimal(str(geometry_epsilon))
        geometry_matches = all(
            (
                expected := expected_by_location_id.get(int(slot.location_id))
            )
            is not None
            and abs(Decimal(str(slot.x_mm)) - Decimal(str(expected["x_mm"])))
            <= epsilon
            and abs(Decimal(str(slot.y_mm)) - Decimal(str(expected["y_mm"])))
            <= epsilon
            and int(slot.width_mm) == int(expected["width_mm"])
            and int(slot.depth_mm) == int(expected["depth_mm"])
            and int(slot.route_sequence) == int(expected["route_sequence"])
            and int(slot.row_no) == int(expected["row_no"])
            and int(slot.slot_no) == int(expected["slot_no"])
            for slot in existing_plan.slots
        )
        if (
            existing_plan.status == "published"
            and str(existing_plan.published_map_revision or "").strip()
            == current_revision
            and existing_location_ids == expected_location_ids
            and int(existing_plan.target_slot_count) == len(rows)
            and existing_plan.numbering_origin == configuration["numbering_origin"]
            and existing_plan.row_direction == configuration["row_direction"]
            and existing_plan.slot_direction == configuration["slot_direction"]
            and int(existing_plan.row_start_no) == configuration["row_start_no"]
            and int(existing_plan.slot_start_no) == configuration["slot_start_no"]
            and existing_plan.preview_fingerprint == fingerprint
            and geometry_matches
        ):
            return {
                "available_location_count": len(rows),
                "ground_plan_id": int(existing_plan.id),
                "ground_plan_status": "published",
                "location_readiness_issue": None,
                "legacy_layout_repaired_count": 0,
                "location_ids": sorted(expected_location_ids),
            }
        raise WarehouseAreaActivationError(
            "该区域已有其他或已漂移的地堆排位事实，已停止一次确认以避免覆盖真实位置",
            status_code=409,
        )

    if not materialize:
        return {
            "available_location_count": len(rows),
            "ground_plan_id": None,
            "ground_plan_status": "repairable",
            "location_readiness_issue": None,
            "legacy_layout_repaired_count": sum(
                1
                for row in rows
                if row.floor3_layout is not None
                and row.floor3_layout.layout_kind == "unknown"
            ),
            "location_ids": sorted(expected_location_ids),
        }

    legacy_layouts = [
        row.floor3_layout
        for row in rows
        if row.floor3_layout is not None
        and row.floor3_layout.layout_kind == "unknown"
    ]
    now = beijing_now_naive()
    for layout in legacy_layouts:
        layout.layout_kind = "physical_pallet"
        layout.version = int(layout.version) + 1
        layout.updated_by = operator_id
        layout.updated_at = now

    idempotency_key = (
        "one-step-ground:"
        + hashlib.sha256(operation_key.encode("utf-8")).hexdigest()
    )
    request_hash = ground_canonical_hash(
        {
            "operation_key": operation_key,
            "area_id": area.id,
            "map_revision": current_revision,
            "preview_fingerprint": fingerprint,
        }
    )
    plan = WarehouseGroundLayoutPlan(
        area_id=area.id,
        status="published",
        draft_map_revision=current_revision,
        published_map_revision=current_revision,
        preview_fingerprint=fingerprint,
        version=1,
        publish_idempotency_key=idempotency_key,
        publish_request_hash=request_hash,
        updated_by=operator_id,
        published_by=operator_id,
        published_at=now,
        updated_at=now,
        **configuration,
    )
    db.add(plan)
    db.flush()
    for slot in matched_slots:
        db.add(
            WarehouseGroundLayoutSlot(
                plan_id=plan.id,
                location_id=int(slot["existing_location_id"]),
                route_sequence=int(slot["route_sequence"]),
                row_no=int(slot["row_no"]),
                slot_no=int(slot["slot_no"]),
                x_mm=Decimal(str(slot["x_mm"])),
                y_mm=Decimal(str(slot["y_mm"])),
                width_mm=int(slot["width_mm"]),
                depth_mm=int(slot["depth_mm"]),
            )
        )
    db.flush()
    return {
        "available_location_count": len(rows),
        "ground_plan_id": int(plan.id),
        "ground_plan_status": "published",
        "location_readiness_issue": None,
        "legacy_layout_repaired_count": len(legacy_layouts),
        "location_ids": sorted(expected_location_ids),
    }
