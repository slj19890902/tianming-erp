from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import delete, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, selectinload

from app.models.customer import Customer
from app.models.customer_finished_storage_preference import (
    CustomerFinishedStoragePreference,
)
from app.models.user import User
from app.models.warehouse_inventory import (
    InventoryPallet,
    WarehouseArea,
    WarehouseFloor,
    WarehouseLocation,
)
from app.services.location_candidates import (
    OperationalLocationRow,
    claim_warehouse_floor_projection,
    list_operational_locations,
    operational_location_issue,
)
from app.services.master_data_versioning import apply_versioned_update
from app.services.warehouse_area_activation import (
    WarehouseAreaActivationError,
    policy_inventory_types,
)
from app.services.warehouse_location_address import employee_area_name


MAX_PREFERRED_FINISHED_STORAGE_AREAS = 20
_FINISHED_LOCATION_TYPES = {"finished", "shared"}


class CustomerFinishedStoragePreferenceError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = status.HTTP_409_CONFLICT,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def ordered_preferred_area_ids(db: Session, customer_id: int) -> list[int]:
    """Return the customer's stable formal area identities in saved order."""

    return [
        int(area_id)
        for area_id in db.scalars(
            select(CustomerFinishedStoragePreference.warehouse_area_id)
            .where(CustomerFinishedStoragePreference.customer_id == customer_id)
            .order_by(
                CustomerFinishedStoragePreference.priority,
                CustomerFinishedStoragePreference.id,
            )
        ).all()
    ]


def _normalized_area_ids(area_ids: Iterable[int]) -> list[int]:
    normalized = [int(value) for value in area_ids]
    if len(normalized) > MAX_PREFERRED_FINISHED_STORAGE_AREAS:
        raise CustomerFinishedStoragePreferenceError(
            "CUSTOMER_FINISHED_STORAGE_TOO_MANY_AREAS",
            f"每个客户最多选择 {MAX_PREFERRED_FINISHED_STORAGE_AREAS} 个成品区域",
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    if any(value <= 0 for value in normalized):
        raise CustomerFinishedStoragePreferenceError(
            "CUSTOMER_FINISHED_STORAGE_AREA_ID_INVALID",
            "成品区域编号必须是正整数",
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    if len(set(normalized)) != len(normalized):
        raise CustomerFinishedStoragePreferenceError(
            "CUSTOMER_FINISHED_STORAGE_AREA_DUPLICATE",
            "同一个成品区域不能重复选择",
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    return normalized


def _area_rows(db: Session) -> dict[int, WarehouseArea]:
    rows = db.scalars(
        select(WarehouseArea)
        .options(
            selectinload(WarehouseArea.floor),
            selectinload(WarehouseArea.storage_policy),
        )
        .order_by(WarehouseArea.id)
    ).all()
    return {int(row.id): row for row in rows}


def _static_area_blockers(area: WarehouseArea) -> list[str]:
    blockers: list[str] = []
    floor = area.floor
    policy = area.storage_policy
    if floor is None:
        blockers.append("所属楼层不存在")
    elif floor.construction_status != "enabled":
        blockers.append("所属楼层未启用")
    if area.construction_status != "enabled":
        blockers.append("区域未启用")
    if policy is None:
        blockers.append("区域尚未绑定正式存放策略")
    elif policy.status != "published":
        blockers.append("区域存放策略尚未发布")
    else:
        try:
            allowed_types = policy_inventory_types(policy)
        except WarehouseAreaActivationError:
            blockers.append("区域正式存放策略已损坏")
        else:
            if "finished" not in allowed_types:
                blockers.append("区域不允许存放成品")
        if not str(policy.published_map_revision or "").strip():
            blockers.append("区域缺少已发布地图版本")
    return list(dict.fromkeys(blockers))


def _occupied_pallet_counts(db: Session) -> dict[tuple[int, str], int]:
    return {
        (int(floor_number), str(area_code or "").strip().upper()): int(count)
        for floor_number, area_code, count in db.execute(
            select(
                WarehouseLocation.warehouse_floor,
                func.upper(WarehouseLocation.area_code),
                func.count(InventoryPallet.id),
            )
            .join(
                InventoryPallet,
                InventoryPallet.location_id == WarehouseLocation.id,
            )
            .where(InventoryPallet.is_current.is_(True))
            .group_by(
                WarehouseLocation.warehouse_floor,
                func.upper(WarehouseLocation.area_code),
            )
        ).all()
        if floor_number is not None and str(area_code or "").strip()
    }


def _qualified_location(
    db: Session,
    row: OperationalLocationRow,
    *,
    require_empty: bool,
    occupied_pallet_count: int,
) -> bool:
    location = row.location
    if (
        row.floor is None
        or row.area is None
        or location.placement_status != "placed"
        or bool(location.is_temporary)
        or str(location.address_kind or "").strip().lower() == "functional"
        or str(location.storage_type or "").strip().lower()
        not in {"ground", "rack"}
    ):
        return False
    # Area-option eligibility must survive a temporary full-capacity state.
    # Passing the same-area source only for the non-empty eligibility pass
    # bypasses the capacity admission check without weakening published-map,
    # floor, area, policy, inventory-type, or geometry validation.  The second
    # pass below performs the real empty/capacity check used for the displayed
    # available count.
    capacity_source_location_id = None if require_empty else int(location.id)
    return (
        operational_location_issue(
            db,
            location,
            warehouse_types=_FINISHED_LOCATION_TYPES,
            require_published=True,
            require_map_geometry=True,
            required_inventory_type="finished",
            require_empty=require_empty,
            capacity_source_location_id=capacity_source_location_id,
            projection_context=row.projection_context,
            known_occupied=row.occupied,
            area_occupied_pallet_count=occupied_pallet_count,
        )
        is None
    )


def _area_summary(
    area: WarehouseArea,
    *,
    formal_location_count: int,
    available_location_count: int,
    blockers: Sequence[str],
) -> dict[str, Any]:
    floor = area.floor
    policy = area.storage_policy
    return {
        "area_id": int(area.id),
        "floor_id": int(floor.id) if floor is not None else None,
        "floor_code": floor.floor_code if floor is not None else None,
        "floor_name": floor.floor_name if floor is not None else None,
        "floor_number": int(floor.floor_number) if floor is not None else None,
        "area_code": area.area_code,
        "area_name": employee_area_name(
            area,
            floor_number=(int(floor.floor_number) if floor is not None else None),
        ),
        "area_master_name": area.area_name,
        "published_map_revision": (
            policy.published_map_revision if policy is not None else None
        ),
        "policy_version": int(policy.version) if policy is not None else None,
        "formal_location_count": int(formal_location_count),
        "available_location_count": int(available_location_count),
        "is_valid": not blockers,
        "blockers": list(blockers),
    }


def _finished_area_catalog(
    db: Session,
) -> tuple[dict[int, dict[str, Any]], dict[int, WarehouseArea]]:
    areas = _area_rows(db)
    valid_counts: dict[int, int] = defaultdict(int)
    available_counts: dict[int, int] = defaultdict(int)
    occupied_counts = _occupied_pallet_counts(db)
    rows = list_operational_locations(
        db,
        warehouse_types=_FINISHED_LOCATION_TYPES,
        empty_only=False,
        pallet_storage_only=False,
    )
    for row in rows:
        area = row.area
        floor = row.floor
        if area is None or floor is None or _static_area_blockers(area):
            continue
        occupied_count = occupied_counts.get(
            (int(floor.floor_number), str(area.area_code).strip().upper()),
            0,
        )
        if not _qualified_location(
            db,
            row,
            require_empty=False,
            occupied_pallet_count=occupied_count,
        ):
            continue
        valid_counts[int(area.id)] += 1
        if _qualified_location(
            db,
            row,
            require_empty=True,
            occupied_pallet_count=occupied_count,
        ):
            available_counts[int(area.id)] += 1

    eligible: dict[int, dict[str, Any]] = {}
    for area_id, count in valid_counts.items():
        if count <= 0:
            continue
        area = areas[area_id]
        eligible[area_id] = _area_summary(
            area,
            formal_location_count=count,
            available_location_count=available_counts.get(area_id, 0),
            blockers=[],
        )
    return eligible, areas


def _grouped_area_options(
    eligible: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[int, dict[str, Any]] = {}
    ordered = sorted(
        eligible.values(),
        key=lambda item: (
            int(item["floor_number"] or 0),
            str(item["area_code"] or ""),
            int(item["area_id"]),
        ),
    )
    for item in ordered:
        floor_id = int(item["floor_id"])
        floor = grouped.setdefault(
            floor_id,
            {
                "floor_id": floor_id,
                "floor_code": item["floor_code"],
                "floor_name": item["floor_name"],
                "floor_number": item["floor_number"],
                "areas": [],
            },
        )
        floor["areas"].append(item)
    return sorted(
        grouped.values(),
        key=lambda item: (int(item["floor_number"] or 0), int(item["floor_id"])),
    )


def grouped_finished_storage_area_options(db: Session) -> list[dict[str, Any]]:
    eligible, _areas = _finished_area_catalog(db)
    return _grouped_area_options(eligible)


def _summaries_for_area_ids(
    preferred_ids: Sequence[int],
    *,
    eligible: dict[int, dict[str, Any]],
    areas: dict[int, WarehouseArea],
) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for priority, area_id in enumerate(preferred_ids, start=1):
        if area_id in eligible:
            item = dict(eligible[area_id])
        else:
            area = areas.get(area_id)
            if area is None:
                item = {
                    "area_id": area_id,
                    "floor_id": None,
                    "floor_code": None,
                    "floor_name": None,
                    "floor_number": None,
                    "area_code": None,
                    "area_name": "区域不存在",
                    "area_master_name": None,
                    "published_map_revision": None,
                    "policy_version": None,
                    "formal_location_count": 0,
                    "available_location_count": 0,
                    "is_valid": False,
                    "blockers": ["区域不存在或引用已损坏"],
                }
            else:
                blockers = _static_area_blockers(area)
                if not blockers:
                    blockers = ["区域没有已发布且已放置的正式成品位置"]
                item = _area_summary(
                    area,
                    formal_location_count=0,
                    available_location_count=0,
                    blockers=blockers,
                )
        item["priority"] = priority
        summaries.append(item)
    return summaries


def preferred_area_summaries_by_customer_ids(
    db: Session,
    customer_ids: Iterable[int],
) -> dict[int, list[dict[str, Any]]]:
    """Batch-load ordered preferences without one query/catalog pass per customer."""

    normalized_ids = list(dict.fromkeys(int(value) for value in customer_ids))
    normalized_ids = [value for value in normalized_ids if value > 0]
    if not normalized_ids:
        return {}
    preferred_by_customer: dict[int, list[int]] = {
        customer_id: [] for customer_id in normalized_ids
    }
    rows = db.execute(
        select(
            CustomerFinishedStoragePreference.customer_id,
            CustomerFinishedStoragePreference.warehouse_area_id,
        )
        .where(
            CustomerFinishedStoragePreference.customer_id.in_(normalized_ids)
        )
        .order_by(
            CustomerFinishedStoragePreference.customer_id,
            CustomerFinishedStoragePreference.priority,
            CustomerFinishedStoragePreference.id,
        )
    ).all()
    if not rows:
        # Production/task reads call this helper on every page.  Most small
        # factories will configure only a few customers, so avoid scanning the
        # full warehouse catalog when this page has no configured route.
        return preferred_by_customer
    for customer_id, area_id in rows:
        preferred_by_customer[int(customer_id)].append(int(area_id))
    eligible, areas = _finished_area_catalog(db)
    return {
        customer_id: _summaries_for_area_ids(
            preferred_by_customer[customer_id],
            eligible=eligible,
            areas=areas,
        )
        for customer_id in normalized_ids
    }


def preference_summaries(db: Session, customer_id: int) -> list[dict[str, Any]]:
    return preferred_area_summaries_by_customer_ids(db, [customer_id]).get(
        int(customer_id), []
    )


def validate_preferred_area_ids(
    db: Session,
    area_ids: Iterable[int],
) -> list[dict[str, Any]]:
    normalized = _normalized_area_ids(area_ids)
    eligible, areas = _finished_area_catalog(db)
    invalid: list[str] = []
    result: list[dict[str, Any]] = []
    for area_id in normalized:
        item = eligible.get(area_id)
        if item is not None:
            result.append(dict(item))
            continue
        area = areas.get(area_id)
        label = (
            f"{area.floor.floor_name} / {employee_area_name(area, floor_number=area.floor.floor_number)}"
            if area is not None and area.floor is not None
            else f"区域 #{area_id}"
        )
        blockers = _static_area_blockers(area) if area is not None else ["区域不存在"]
        if not blockers:
            blockers = ["没有已发布且已放置的正式成品位置"]
        invalid.append(f"{label}：{'；'.join(blockers)}")
    if invalid:
        raise CustomerFinishedStoragePreferenceError(
            "CUSTOMER_FINISHED_STORAGE_AREA_UNAVAILABLE",
            "所选成品区域当前不可配置：" + "；".join(invalid),
        )
    return result


def replace_preferred_areas(
    db: Session,
    *,
    customer: Customer,
    area_ids: Iterable[int],
    expected_version: int,
    user: User,
) -> bool:
    normalized = _normalized_area_ids(area_ids)
    existing_area_ids = ordered_preferred_area_ids(db, int(customer.id))
    involved_area_ids = sorted({*existing_area_ids, *normalized})
    floor_numbers = sorted(
        {
            int(value)
            for value in db.scalars(
                select(WarehouseFloor.floor_number)
                .join(WarehouseArea, WarehouseArea.floor_id == WarehouseFloor.id)
                .where(WarehouseArea.id.in_(involved_area_ids))
            ).all()
        }
    ) if involved_area_ids else []
    try:
        for floor_number in floor_numbers:
            if not claim_warehouse_floor_projection(
                db,
                floor_number=floor_number,
            ):
                raise CustomerFinishedStoragePreferenceError(
                    "CUSTOMER_FINISHED_STORAGE_FLOOR_CHANGED",
                    "仓库楼层台账已变化，请刷新后重试",
                )
    except OperationalError as error:
        raise CustomerFinishedStoragePreferenceError(
            "CUSTOMER_FINISHED_STORAGE_FLOOR_BUSY",
            "仓库区域正在调整，请稍后刷新重试",
        ) from error
    current_version = int(customer.version or 1)
    if current_version != int(expected_version):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "MASTER_VERSION_CONFLICT",
                "expected_version": int(expected_version),
                "current_version": current_version,
            },
        )
    validate_preferred_area_ids(db, normalized)
    before = ordered_preferred_area_ids(db, int(customer.id))
    if before == normalized:
        return False

    apply_versioned_update(
        db,
        object_type="customer",
        entity=customer,
        updates={},
        expected_version=int(expected_version),
        user=user,
        reason="修改客户默认成品存放区域",
        source="api.customers.finished_storage_preferences",
        action="storage_preference_update",
        force_version=True,
    )
    db.execute(
        delete(CustomerFinishedStoragePreference).where(
            CustomerFinishedStoragePreference.customer_id == customer.id
        )
    )
    db.flush()
    db.add_all(
        [
            CustomerFinishedStoragePreference(
                customer_id=int(customer.id),
                warehouse_area_id=area_id,
                priority=priority,
                created_by=int(user.id) if user.id is not None else None,
            )
            for priority, area_id in enumerate(normalized, start=1)
        ]
    )
    db.flush()
    return True


def finished_storage_preference_payload(
    db: Session,
    customer: Customer,
) -> dict[str, Any]:
    preferred_ids = ordered_preferred_area_ids(db, int(customer.id))
    eligible, areas = _finished_area_catalog(db)
    return {
        "customer_id": int(customer.id),
        "customer_version": int(customer.version or 1),
        "selected_area_ids": preferred_ids,
        "preferences": _summaries_for_area_ids(
            preferred_ids,
            eligible=eligible,
            areas=areas,
        ),
        "floors": _grouped_area_options(eligible),
    }
