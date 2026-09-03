from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, selectinload

from app.core.time_contract import beijing_now_naive
from app.models.warehouse_inventory import (
    WarehouseArea,
    WarehouseAreaStoragePolicy,
    WarehouseFloor,
    WarehouseGroundLayoutPlan,
    WarehouseGroundLayoutSlot,
    WarehouseLocation,
)
from app.services.warehouse_area_activation import policy_inventory_types
from app.services.warehouse_floor1_candidate_planner import (
    Floor1CandidatePlanningError,
    _percent_round_trip_epsilon,
    validate_capacity_layout_slots_for_zone,
)
from app.services.warehouse_ground_slots import (
    WarehouseGroundSlotError,
    ground_preview_fingerprint,
    number_ground_physical_slots,
)
from app.services.warehouse_pallet_standard import standard_pallet_contract


IMMUTABLE_PLAN_TRIGGER = "trg_ground_plans_published_immutable"
COORDINATE_QUANTUM = Decimal("0.001")


def _canonical_polygon(points: object, *, area_code: str) -> tuple[tuple[Decimal, Decimal], ...]:
    if not isinstance(points, list) or len(points) < 3:
        raise WarehouseGroundSlotError(
            "GROUND_PLAN_MAP_GEOMETRY_INVALID",
            f"{area_code} 地图区域边界无效，已停止发布。",
        )
    try:
        normalized = tuple(
            (
                Decimal(str(point[0])).quantize(COORDINATE_QUANTUM),
                Decimal(str(point[1])).quantize(COORDINATE_QUANTUM),
            )
            for point in points
        )
    except (IndexError, TypeError, ValueError) as error:
        raise WarehouseGroundSlotError(
            "GROUND_PLAN_MAP_GEOMETRY_INVALID",
            f"{area_code} 地图区域边界无效，已停止发布。",
        ) from error
    if len(set(normalized)) != len(normalized):
        raise WarehouseGroundSlotError(
            "GROUND_PLAN_MAP_GEOMETRY_INVALID",
            f"{area_code} 地图区域边界存在重复点，已停止发布。",
        )
    forward = tuple(normalized[index:] + normalized[:index] for index in range(len(normalized)))
    reversed_points = tuple(reversed(normalized))
    reverse = tuple(
        reversed_points[index:] + reversed_points[:index]
        for index in range(len(reversed_points))
    )
    return min((*forward, *reverse))


@dataclass(frozen=True)
class GroundPlanRevisionSync:
    plan_id: int
    area_id: int
    area_code: str
    policy_id: int
    source_policy_version: int
    target_policy_version: int
    map_feature_id: str
    source_revision: str
    target_revision: str
    source_preview_fingerprint: str
    target_preview_fingerprint: str
    source_plan_version: int
    target_plan_version: int
    location_ids: tuple[int, ...]

    def audit_payload(self) -> dict:
        return {
            "plan_id": self.plan_id,
            "area_id": self.area_id,
            "area_code": self.area_code,
            "policy_id": self.policy_id,
            "source_policy_version": self.source_policy_version,
            "target_policy_version": self.target_policy_version,
            "map_feature_id": self.map_feature_id,
            "source_revision": self.source_revision,
            "target_revision": self.target_revision,
            "source_preview_fingerprint": self.source_preview_fingerprint,
            "target_preview_fingerprint": self.target_preview_fingerprint,
            "source_plan_version": self.source_plan_version,
            "target_plan_version": self.target_plan_version,
            "location_ids": list(self.location_ids),
            "inventory_changed": False,
            "map_geometry_changed": False,
        }


def _normalized_feature_contract(feature: dict, *, floor: WarehouseFloor, area: WarehouseArea) -> dict:
    raw_floor_id = feature.get("formal_floor_id")
    raw_area_id = feature.get("formal_area_id")
    if (raw_floor_id in (None, "")) != (raw_area_id in (None, "")):
        raise WarehouseGroundSlotError(
            "GROUND_PLAN_MAP_IDENTITY_STALE",
            f"{area.area_code} 地图区域的正式身份不完整，已停止发布。",
        )
    if raw_floor_id not in (None, ""):
        try:
            formal_floor_id = int(raw_floor_id)
            formal_area_id = int(raw_area_id)
        except (TypeError, ValueError) as error:
            raise WarehouseGroundSlotError(
                "GROUND_PLAN_MAP_IDENTITY_STALE",
                f"{area.area_code} 地图区域的正式身份无效，已停止发布。",
            ) from error
        if formal_floor_id != floor.id or formal_area_id != area.id:
            raise WarehouseGroundSlotError(
                "GROUND_PLAN_MAP_IDENTITY_STALE",
                f"{area.area_code} 地图区域已指向其他正式楼层或区域，已停止发布。",
            )
    return {
        "id": str(feature.get("id") or "").strip(),
        "feature_kind": feature.get("feature_kind"),
        "erp_area_code": str(feature.get("erp_area_code") or "").strip().upper(),
        "points": _canonical_polygon(feature.get("points"), area_code=area.area_code),
        "status": str(feature.get("status") or "").strip(),
        "storage_mode": feature.get("storage_mode"),
        "no_stacking": bool(feature.get("no_stacking")),
        "elevation_mm": feature.get("elevation_mm"),
        "storage_height_mm": feature.get("storage_height_mm"),
        "allowed_inventory_types": sorted(
            str(value).strip()
            for value in feature.get("allowed_inventory_types") or []
            if str(value).strip()
        ),
        "storage_layout": str(feature.get("storage_layout") or "").strip(),
        # Missing legacy IDs and the same correct stable IDs are equivalent.
        "resolved_formal_floor_id": int(floor.id),
        "resolved_formal_area_id": int(area.id),
    }


def _feature(layout: dict, feature_id: str, *, area_code: str) -> dict:
    matches = [
        row
        for row in layout.get("features") or []
        if row.get("feature_kind") == "zone"
        and str(row.get("id") or "").strip() == feature_id
    ]
    if len(matches) != 1:
        raise WarehouseGroundSlotError(
            "GROUND_PLAN_MAP_FEATURE_STALE",
            f"{area_code} 已发布地堆排位对应的地图区域缺失或重复，已停止发布。",
        )
    return matches[0]


def _configuration(plan: WarehouseGroundLayoutPlan) -> dict:
    return {
        "target_slot_count": int(plan.target_slot_count),
        "numbering_origin": plan.numbering_origin,
        "row_direction": plan.row_direction,
        "slot_direction": plan.slot_direction,
        "row_start_no": int(plan.row_start_no),
        "slot_start_no": int(plan.slot_start_no),
    }


def _rebuild_slots(plan: WarehouseGroundLayoutPlan, floor_layout: dict) -> list[dict]:
    slots = sorted(plan.slots, key=lambda row: (row.route_sequence, row.id))
    if len(slots) != int(plan.target_slot_count):
        raise WarehouseGroundSlotError(
            "GROUND_PLAN_LOCATION_SET_CHANGED",
            f"{plan.area.area_code} 的地堆位置数量与已发布计划不一致，已停止发布。",
        )
    layout_payloads: list[dict] = []
    for slot in slots:
        location = slot.location
        layout = location.floor3_layout if location is not None else None
        if location is None or layout is None:
            raise WarehouseGroundSlotError(
                "GROUND_PLAN_LOCATION_LAYOUT_MISSING",
                f"{plan.area.area_code} 已发布地堆排位缺少位置或二维坐标，已停止发布。",
            )
        layout_payloads.append(
            {
                "location_id": int(location.id),
                "left_pct": float(layout.left_pct),
                "top_pct": float(layout.top_pct),
                "width_pct": float(layout.width_pct),
                "height_pct": float(layout.height_pct),
                "z_index": int(layout.z_index),
                "expected_version": int(layout.version),
                "layout_kind": layout.layout_kind,
            }
        )
    try:
        measured = validate_capacity_layout_slots_for_zone(
            floor_layout,
            feature_id=str(plan.area.storage_policy.map_feature_id),
            slots=layout_payloads,
        )
    except Floor1CandidatePlanningError as error:
        raise WarehouseGroundSlotError(
            "GROUND_PLAN_MAP_GEOMETRY_CHANGED",
            f"{plan.area.area_code} 的地图或货位几何已变化：{error}",
        ) from error
    feature = _feature(
        floor_layout,
        str(plan.area.storage_policy.map_feature_id),
        area_code=plan.area.area_code,
    )
    epsilon = _percent_round_trip_epsilon(feature.get("points") or [])
    pallet = standard_pallet_contract()
    standard_width = int(pallet["width_mm"])
    standard_depth = int(pallet["depth_mm"])
    rebuilt: list[dict] = []
    for slot, layout_payload, actual in zip(
        slots, layout_payloads, measured, strict=True
    ):
        actual_width = float(actual["width_mm"])
        actual_depth = float(actual["depth_mm"])
        standard = (
            abs(actual_width - standard_width) <= epsilon
            and abs(actual_depth - standard_depth) <= epsilon
        )
        rotated = (
            abs(actual_width - standard_depth) <= epsilon
            and abs(actual_depth - standard_width) <= epsilon
        )
        if not standard and not rotated:
            raise WarehouseGroundSlotError(
                "GROUND_PLAN_STANDARD_SIZE_CHANGED",
                f"{plan.area.area_code} 的货位不再是标准栈板尺寸，已停止发布。",
            )
        location = slot.location
        layout = location.floor3_layout
        rebuilt.append(
            {
                **layout_payload,
                **actual,
                "x_mm": Decimal(str(actual["x_mm"])).quantize(COORDINATE_QUANTUM),
                "y_mm": Decimal(str(actual["y_mm"])).quantize(COORDINATE_QUANTUM),
                "width_mm": standard_width if standard else standard_depth,
                "depth_mm": standard_depth if standard else standard_width,
                "location_code": location.location_code,
                "existing_location_id": int(location.id),
                "existing_layout_version": int(layout.version),
            }
        )
    return number_ground_physical_slots(
        rebuilt,
        numbering_origin=plan.numbering_origin,
        row_direction=plan.row_direction,
        slot_direction=plan.slot_direction,
        row_start_no=plan.row_start_no,
        slot_start_no=plan.slot_start_no,
    )


def _assert_plan_geometry_matches(
    plan: WarehouseGroundLayoutPlan, rebuilt: list[dict]
) -> None:
    expected = {int(row["existing_location_id"]): row for row in rebuilt}
    if len(expected) != len(rebuilt) or len(plan.slots) != len(rebuilt):
        raise WarehouseGroundSlotError(
            "GROUND_PLAN_LOCATION_SET_CHANGED",
            f"{plan.area.area_code} 的地堆位置集合已变化，已停止发布。",
        )
    for slot in plan.slots:
        row = expected.get(int(slot.location_id))
        if row is None or any(
            (
                int(slot.route_sequence) != int(row["route_sequence"]),
                int(slot.row_no) != int(row["row_no"]),
                int(slot.slot_no) != int(row["slot_no"]),
                int(slot.width_mm) != int(row["width_mm"]),
                int(slot.depth_mm) != int(row["depth_mm"]),
                Decimal(str(slot.x_mm)).quantize(COORDINATE_QUANTUM)
                != Decimal(str(row["x_mm"])).quantize(COORDINATE_QUANTUM),
                Decimal(str(slot.y_mm)).quantize(COORDINATE_QUANTUM)
                != Decimal(str(row["y_mm"])).quantize(COORDINATE_QUANTUM),
            )
        ):
            raise WarehouseGroundSlotError(
                "GROUND_PLAN_MAP_GEOMETRY_CHANGED",
                f"{plan.area.area_code} 的地图几何、位置或排号已变化，已停止发布。",
            )


def prepare_ground_plan_revision_sync(
    db: Session,
    *,
    floor_code: str,
    current_floor_layout: dict,
    proposed_floor_layout: dict,
) -> list[GroundPlanRevisionSync]:
    normalized_floor = floor_code.strip().upper()
    current_revision = str(current_floor_layout.get("revision") or "").strip()
    target_revision = str(proposed_floor_layout.get("revision") or "").strip()
    if not current_revision or not target_revision:
        raise WarehouseGroundSlotError(
            "GROUND_PLAN_MAP_REVISION_MISSING",
            "地图缺少当前或待发布 revision，已停止发布。",
        )
    if current_revision == target_revision:
        return []
    plans = list(
        db.scalars(
            select(WarehouseGroundLayoutPlan)
            .join(WarehouseArea, WarehouseArea.id == WarehouseGroundLayoutPlan.area_id)
            .join(WarehouseFloor, WarehouseFloor.id == WarehouseArea.floor_id)
            .where(
                func.upper(WarehouseFloor.floor_code) == normalized_floor,
                WarehouseGroundLayoutPlan.status == "published",
            )
            .options(
                selectinload(WarehouseGroundLayoutPlan.area).selectinload(
                    WarehouseArea.floor
                ),
                selectinload(WarehouseGroundLayoutPlan.area).selectinload(
                    WarehouseArea.storage_policy
                ),
                selectinload(WarehouseGroundLayoutPlan.slots)
                .selectinload(WarehouseGroundLayoutSlot.location)
                .selectinload(WarehouseLocation.floor3_layout),
            )
            .execution_options(populate_existing=True)
        ).unique()
    )
    result: list[GroundPlanRevisionSync] = []
    for plan in plans:
        area = plan.area
        floor = area.floor
        policy = area.storage_policy
        if policy is None or policy.status != "published":
            raise WarehouseGroundSlotError(
                "GROUND_PLAN_POLICY_STALE",
                f"{area.area_code} 已发布地堆排位缺少已发布区域策略，已停止发布。",
            )
        if (
            plan.draft_map_revision != current_revision
            or plan.published_map_revision != current_revision
            or policy.published_map_revision != current_revision
        ):
            raise WarehouseGroundSlotError(
                "GROUND_PLAN_REVISION_STALE",
                f"{area.area_code} 地堆排位与当前地图 revision 不一致，请先完成一致性修正。",
            )
        current_feature = _feature(
            current_floor_layout, policy.map_feature_id, area_code=area.area_code
        )
        proposed_feature = _feature(
            proposed_floor_layout, policy.map_feature_id, area_code=area.area_code
        )
        current_contract = _normalized_feature_contract(
            current_feature, floor=floor, area=area
        )
        proposed_contract = _normalized_feature_contract(
            proposed_feature, floor=floor, area=area
        )
        if current_contract != proposed_contract:
            raise WarehouseGroundSlotError(
                "GROUND_PLAN_MAP_FEATURE_CHANGED",
                f"{area.area_code} 已有已发布地堆排位，区域边界、身份或用途发生变化，不能随整层地图静默发布。",
            )
        expected_types = sorted(policy_inventory_types(policy))
        if (
            proposed_contract["allowed_inventory_types"] != expected_types
            or proposed_contract["storage_layout"] != policy.storage_layout
        ):
            raise WarehouseGroundSlotError(
                "GROUND_PLAN_POLICY_CHANGED",
                f"{area.area_code} 已有已发布地堆排位，存放策略变化必须走专用调整流程。",
            )
        current_slots = _rebuild_slots(plan, current_floor_layout)
        _assert_plan_geometry_matches(plan, current_slots)
        current_fingerprint = ground_preview_fingerprint(
            area_id=area.id,
            policy_version=policy.version,
            map_revision=current_revision,
            configuration=_configuration(plan),
            slots=current_slots,
        )
        if current_fingerprint != plan.preview_fingerprint:
            raise WarehouseGroundSlotError(
                "GROUND_PLAN_PREVIEW_STALE",
                f"{area.area_code} 地堆排位 fingerprint 无法由当前正式事实重建，已停止发布。",
            )
        proposed_slots = _rebuild_slots(plan, proposed_floor_layout)
        _assert_plan_geometry_matches(plan, proposed_slots)
        target_policy_version = int(policy.version) + 1
        target_fingerprint = ground_preview_fingerprint(
            area_id=area.id,
            policy_version=target_policy_version,
            map_revision=target_revision,
            configuration=_configuration(plan),
            slots=proposed_slots,
        )
        result.append(
            GroundPlanRevisionSync(
                plan_id=int(plan.id),
                area_id=int(area.id),
                area_code=area.area_code,
                policy_id=int(policy.id),
                source_policy_version=int(policy.version),
                target_policy_version=target_policy_version,
                map_feature_id=policy.map_feature_id,
                source_revision=current_revision,
                target_revision=target_revision,
                source_preview_fingerprint=plan.preview_fingerprint,
                target_preview_fingerprint=target_fingerprint,
                source_plan_version=int(plan.version),
                target_plan_version=int(plan.version) + 1,
                location_ids=tuple(sorted(int(slot.location_id) for slot in plan.slots)),
            )
        )
    return result


def _immutable_trigger_sql(db: Session) -> str:
    row = db.execute(
        text(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=:name"
        ),
        {"name": IMMUTABLE_PLAN_TRIGGER},
    ).scalar_one_or_none()
    sql = str(row or "")
    if (
        not sql
        or "warehouse_ground_layout_plans" not in sql
        or "published ground layout plan is immutable" not in sql
    ):
        raise WarehouseGroundSlotError(
            "GROUND_PLAN_IMMUTABLE_TRIGGER_INVALID",
            "地堆计划不可变保护触发器缺失或定义异常，已停止发布。",
        )
    return sql


def apply_ground_plan_revision_sync(
    db: Session,
    *,
    sync_plan: list[GroundPlanRevisionSync],
    operator_id: int,
    published_revision: str,
) -> list[dict]:
    if not sync_plan:
        return []
    if any(row.target_revision != published_revision for row in sync_plan):
        raise WarehouseGroundSlotError(
            "GROUND_PLAN_PUBLISHED_REVISION_CHANGED",
            "实际发布 revision 与地堆计划预检不一致，已停止发布。",
        )
    db.flush()
    for row in sync_plan:
        policy = db.get(WarehouseAreaStoragePolicy, row.policy_id)
        if (
            policy is None
            or policy.area_id != row.area_id
            or policy.status != "published"
            or policy.map_feature_id != row.map_feature_id
            or policy.published_map_revision != row.target_revision
            or int(policy.version) != row.target_policy_version
        ):
            raise WarehouseGroundSlotError(
                "GROUND_PLAN_POLICY_CAS_FAILED",
                f"{row.area_code} 策略在地图发布中发生变化，地堆计划未同步。",
            )
    trigger_sql = _immutable_trigger_sql(db)
    db.execute(text(f'DROP TRIGGER "{IMMUTABLE_PLAN_TRIGGER}"'))
    changed_at = beijing_now_naive()
    for row in sync_plan:
        result = db.execute(
            text(
                """
                UPDATE warehouse_ground_layout_plans
                SET draft_map_revision=:target_revision,
                    published_map_revision=:target_revision,
                    preview_fingerprint=:target_fingerprint,
                    version=:target_version,
                    updated_by=:operator_id,
                    updated_at=:changed_at
                WHERE id=:plan_id AND area_id=:area_id AND status='published'
                  AND draft_map_revision=:source_revision
                  AND published_map_revision=:source_revision
                  AND preview_fingerprint=:source_fingerprint
                  AND version=:source_version
                """
            ),
            {
                "target_revision": row.target_revision,
                "target_fingerprint": row.target_preview_fingerprint,
                "target_version": row.target_plan_version,
                "operator_id": operator_id,
                "changed_at": changed_at,
                "plan_id": row.plan_id,
                "area_id": row.area_id,
                "source_revision": row.source_revision,
                "source_fingerprint": row.source_preview_fingerprint,
                "source_version": row.source_plan_version,
            },
        )
        if result.rowcount != 1:
            raise WarehouseGroundSlotError(
                "GROUND_PLAN_REVISION_CAS_FAILED",
                f"{row.area_code} 地堆计划已变化，整次地图发布已回滚。",
            )
    db.execute(text(trigger_sql))
    for row in sync_plan:
        plan = db.get(WarehouseGroundLayoutPlan, row.plan_id)
        if plan is not None:
            db.expire(plan)
    return [row.audit_payload() for row in sync_plan]
