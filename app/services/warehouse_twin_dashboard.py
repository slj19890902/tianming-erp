from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from math import ceil
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.time_contract import (
    beijing_date_bounds_utc_naive,
    beijing_naive_to_api,
    utc_naive_to_api,
    utc_naive_to_beijing_date,
    utc_now_naive,
)
from app.models.warehouse_inventory import (
    InventoryLot,
    InventoryMovement,
    InventoryPallet,
    WarehouseFloor,
    WarehouseLocation,
)


AGE_BUCKETS = (
    ("0_7", "0～7天", 0, 7),
    ("8_30", "8～30天", 8, 30),
    ("31_60", "31～60天", 31, 60),
    ("61_90", "61～90天", 61, 90),
    ("91_180", "91～180天", 91, 180),
    ("over_180", "180天以上", 181, None),
)
EXTERNAL_INBOUND_MOVEMENTS = {"manual_in", "return_in"}
EXTERNAL_OUTBOUND_MOVEMENTS = {"consume", "return_reconsume", "scrap"}
CAPACITY_THRESHOLDS = {"attention": 0.80, "warning": 0.90, "critical": 0.95}


def _number(value: Decimal | int | float | None) -> float:
    return round(float(value or 0), 3)


def _quantity_key(inventory_type: str, unit: str) -> str:
    return f"{inventory_type}:{unit}"


def _quantity_label(inventory_type: str, unit: str) -> str:
    type_label = {
        "finished": "成品",
        "semi_finished": "半成品/原料",
    }.get(inventory_type, inventory_type)
    unit_label = {"boxes": "只", "sheets": "张"}.get(unit, unit)
    return f"{type_label}（{unit_label}）"


def _physical_quantity(row: InventoryLot) -> int:
    return int(row.quantity_available + row.quantity_reserved + row.quantity_damaged)


def _usable_quantity(row: InventoryLot) -> int:
    return int(row.quantity_available + row.quantity_reserved)


def _age_days(row: InventoryLot, as_of: date) -> int | None:
    if row.stock_date_accuracy == "unknown" or row.stock_date is None:
        return None
    return max(0, (as_of - row.stock_date).days)


def _age_bucket_key(days: int | None) -> str:
    if days is None:
        return "unknown"
    for key, _label, lower, upper in AGE_BUCKETS:
        if days >= lower and (upper is None or days <= upper):
            return key
    return "unknown"


def _lot_business_fields(row: InventoryLot) -> dict:
    if row.finished_detail is not None:
        detail = row.finished_detail
        return {
            "inventory_code": detail.inventory_code_snapshot,
            "product_name": detail.product_name_snapshot,
            "customer_id": detail.owner_customer_id,
            "customer_name": detail.owner_customer_name_snapshot or "通用库存",
            "specification": "×".join(
                str(value)
                for value in (detail.length_mm, detail.width_mm, detail.height_mm)
                if value is not None
            )
            or None,
            "material": detail.material_code_snapshot,
        }
    if row.semi_finished_detail is not None:
        detail = row.semi_finished_detail
        dimensions = (
            f"{detail.board_length_mm}×{detail.board_width_mm}mm"
            if detail.board_length_mm and detail.board_width_mm
            else None
        )
        return {
            "inventory_code": detail.material_code_snapshot,
            "product_name": "客户专用纸板备料" if detail.owner_customer_id else "通用半成品片料",
            "customer_id": detail.owner_customer_id,
            "customer_name": detail.owner_customer_name_snapshot or "通用库存",
            "specification": dimensions,
            "material": detail.normalized_material_code or detail.material_code_snapshot,
        }
    return {
        "inventory_code": None,
        "product_name": "待补充库存名称",
        "customer_id": None,
        "customer_name": "待确认",
        "specification": None,
        "material": None,
    }


def _lot_payload(row: InventoryLot, as_of: date) -> dict:
    business = _lot_business_fields(row)
    age_days = _age_days(row, as_of)
    return {
        "lot_id": row.id,
        "lot_number": row.lot_number,
        "inventory_type": row.inventory_type,
        **business,
        "quantity": _physical_quantity(row),
        "available_quantity": int(row.quantity_available),
        "reserved_quantity": int(row.quantity_reserved),
        "damaged_quantity": int(row.quantity_damaged),
        "unit": row.unit,
        "age_days": age_days,
        "age_bucket": _age_bucket_key(age_days),
        "stock_date_accuracy": row.stock_date_accuracy,
        "status": row.status,
        "version": row.version,
    }


def _pallet_visible(
    pallet: InventoryPallet,
    visible_customer_ids: set[int] | None,
) -> bool:
    if visible_customer_ids is None:
        return True
    customer_ids = {item.customer_id for item in pallet.items if item.customer_id is not None}
    return bool(customer_ids) and customer_ids.issubset(visible_customer_ids)


def _location_position(row: WarehouseLocation) -> tuple[str, dict | None]:
    if not row.is_active:
        return "disabled", None
    if (row.placement_status or "placed") == "unplaced":
        return "unplaced", None
    if row.floor3_layout is not None:
        layout = row.floor3_layout
        return (
            "mapped",
            {
                "left_pct": _number(layout.left_pct),
                "top_pct": _number(layout.top_pct),
                "width_pct": _number(layout.width_pct),
                "height_pct": _number(layout.height_pct),
                "z_index": layout.z_index,
                "version": layout.version,
            },
        )
    if row.warehouse_floor and row.area_code:
        return "area_only", None
    return "unlocated", None


def _floor_key(value: int | None) -> str:
    return f"{value}F" if value else "UNLOCATED"


def warehouse_capacity_summary(
    floor: WarehouseFloor | None,
    *,
    occupied_pallets: int,
    visible: bool,
) -> dict:
    """Return the single capacity projection used by ledgers and dashboards.

    A planning reference may drive an explicitly labelled planning alert while
    field review is incomplete.  It never becomes the confirmed safe capacity.
    """

    if not visible:
        return {
            "visible": False,
            "confirmed": False,
            "basis": "hidden",
            "safe_pallet_capacity": None,
            "confirmed_area_capacity": None,
            "planned_pallet_capacity": None,
            "reference_pallet_capacity": None,
            "occupied_pallets": None,
            "empty_pallet_slots": None,
            "utilization_percent": None,
            "coverage_percent": None,
            "reviewed_area_count": None,
            "review_required_area_count": None,
            "unreviewed_area_count": None,
            "last_reviewed_at": None,
            "alert_level": "hidden",
            "alert_label": "按权限隐藏",
            "status": "hidden",
            "label": "按权限隐藏",
            "thresholds": None,
        }

    areas = list(floor.areas) if floor is not None else []
    review_required = [area for area in areas if area.construction_status == "enabled"]
    reviewed = [
        area
        for area in review_required
        if area.capacity_review_status in {"confirmed", "excluded"}
        and area.capacity_reviewed_at is not None
    ]
    included = [
        area
        for area in reviewed
        if area.capacity_review_status == "confirmed"
        and area.capacity_eligible
        and area.confirmed_pallet_capacity is not None
    ]
    confirmed_area_capacity = sum(int(area.confirmed_pallet_capacity or 0) for area in included)
    coverage_percent = (
        round(len(reviewed) * 100 / len(review_required), 1) if review_required else 0.0
    )
    confirmed = bool(review_required) and len(reviewed) == len(review_required) and confirmed_area_capacity > 0
    safe_capacity = confirmed_area_capacity if confirmed else None
    planned_capacity = int(
        (floor.planning_reference_pallet_capacity if floor is not None else 0)
        or sum(int(area.planned_pallet_capacity or 0) for area in areas)
    )
    reference_capacity = safe_capacity or planned_capacity or None
    utilization = (
        round(occupied_pallets * 100 / reference_capacity, 1)
        if reference_capacity
        else None
    )
    if utilization is None:
        alert_level, alert_label = "unknown", "容量资料待补"
    elif utilization > 100:
        alert_level, alert_label = "over_capacity", "已超过容量"
    elif utilization >= 95:
        alert_level, alert_label = "critical", "红色临界"
    elif utilization >= 90:
        alert_level, alert_label = "warning", "橙色紧张"
    elif utilization >= 80:
        alert_level, alert_label = "attention", "黄色关注"
    else:
        alert_level, alert_label = "normal", "正常"
    reviewed_times = [area.capacity_reviewed_at for area in reviewed if area.capacity_reviewed_at]
    return {
        "visible": True,
        "confirmed": confirmed,
        "basis": "confirmed" if confirmed else ("planning" if reference_capacity else "missing"),
        "safe_pallet_capacity": safe_capacity,
        "confirmed_area_capacity": confirmed_area_capacity,
        "planned_pallet_capacity": planned_capacity,
        "reference_pallet_capacity": reference_capacity,
        "occupied_pallets": occupied_pallets,
        "empty_pallet_slots": (
            max(reference_capacity - occupied_pallets, 0) if reference_capacity else None
        ),
        "utilization_percent": utilization,
        "coverage_percent": coverage_percent,
        "reviewed_area_count": len(reviewed),
        "review_required_area_count": len(review_required),
        "unreviewed_area_count": max(len(review_required) - len(reviewed), 0),
        "last_reviewed_at": (
            beijing_naive_to_api(max(reviewed_times)) if reviewed_times else None
        ),
        "alert_level": alert_level,
        "alert_label": alert_label,
        "status": "confirmed" if confirmed else "awaiting_field_confirmation",
        "label": "现场安全容量已确认" if confirmed else "规划容量预警（现场待复核）",
        "thresholds": (
            {
                key: ceil(reference_capacity * ratio)
                for key, ratio in CAPACITY_THRESHOLDS.items()
            }
            if reference_capacity
            else None
        ),
    }


def _location_payload(
    row: WarehouseLocation,
    *,
    lots: list[InventoryLot],
    pallet: InventoryPallet | None,
    as_of: date,
) -> dict:
    position_status, map_position = _location_position(row)
    pallet_payload = None
    if pallet is not None:
        items = [_lot_payload(lot, as_of) for lot in lots if lot.pallet_item is not None]
        if not items:
            items = [
                {
                    "lot_id": item.inventory_lot_id,
                    "inventory_code": item.inventory_code,
                    "product_name": item.product_name or "待匹配货物",
                    "customer_id": item.customer_id,
                    "customer_name": item.customer_name_snapshot or "待确认",
                    "quantity": _number(item.quantity),
                    "available_quantity": _number(item.quantity),
                    "reserved_quantity": 0,
                    "unit": item.unit,
                    "age_days": None,
                    "age_bucket": "unknown",
                    "stock_date_accuracy": "unknown",
                    "status": item.match_status,
                }
                for item in pallet.items
                if item.inventory_lot_id is None
            ]
        pallet_payload = {
            "pallet_id": pallet.id,
            "pallet_code": pallet.pallet_code,
            "version": pallet.version,
            "needs_relocation": pallet.needs_relocation,
            "item_count": len(items),
            "items": items,
        }
    loose_items = [
        _lot_payload(lot, as_of)
        for lot in lots
        if lot.pallet_item is None or pallet is None
    ]
    occupied = pallet is not None or any(_physical_quantity(lot) > 0 for lot in lots)
    return {
        "location_id": row.id,
        "location_code": row.location_code,
        "location_name": row.location_name,
        "floor_code": _floor_key(row.warehouse_floor),
        "floor_number": row.warehouse_floor,
        "area_code": row.area_code,
        "warehouse_type": row.warehouse_type,
        "storage_type": row.storage_type,
        "is_temporary": row.is_temporary,
        "is_active": row.is_active,
        "position_status": position_status,
        "map_position": map_position,
        "layout_draft_position": (
            {
                "left_pct": _number(row.floor3_layout.left_pct),
                "top_pct": _number(row.floor3_layout.top_pct),
                "width_pct": _number(row.floor3_layout.width_pct),
                "height_pct": _number(row.floor3_layout.height_pct),
                "z_index": row.floor3_layout.z_index,
                "version": row.floor3_layout.version,
            }
            if row.floor3_layout is not None and position_status == "unplaced"
            else None
        ),
        "occupancy_status": "occupied" if occupied else "empty",
        "pallet": pallet_payload,
        "loose_items": loose_items,
    }


def _quantity_groups(lots: Iterable[InventoryLot]) -> list[dict]:
    grouped: dict[str, dict] = {}
    for row in lots:
        key = _quantity_key(row.inventory_type, row.unit)
        item = grouped.setdefault(
            key,
            {
                "key": key,
                "inventory_type": row.inventory_type,
                "unit": row.unit,
                "label": _quantity_label(row.inventory_type, row.unit),
                "available": 0,
                "reserved": 0,
                "damaged": 0,
                "lot_count": 0,
            },
        )
        item["available"] += int(row.quantity_available)
        item["reserved"] += int(row.quantity_reserved)
        item["damaged"] += int(row.quantity_damaged)
        item["lot_count"] += 1
    return sorted(grouped.values(), key=lambda row: row["key"])


def _age_distribution(lots: Iterable[InventoryLot], as_of: date) -> list[dict]:
    buckets = {
        key: {"key": key, "label": label, "lot_count": 0, "pallet_count": 0, "quantities": {}}
        for key, label, _lower, _upper in AGE_BUCKETS
    }
    buckets["unknown"] = {
        "key": "unknown",
        "label": "库龄待确认",
        "lot_count": 0,
        "pallet_count": 0,
        "quantities": {},
    }
    pallet_ids: dict[str, set[int]] = defaultdict(set)
    for row in lots:
        key = _age_bucket_key(_age_days(row, as_of))
        bucket = buckets[key]
        bucket["lot_count"] += 1
        quantity_key = _quantity_key(row.inventory_type, row.unit)
        bucket["quantities"][quantity_key] = (
            bucket["quantities"].get(quantity_key, 0) + _physical_quantity(row)
        )
        if row.pallet_item is not None:
            pallet_ids[key].add(row.pallet_item.pallet_id)
    for key, ids in pallet_ids.items():
        buckets[key]["pallet_count"] = len(ids)
    order = [row[0] for row in AGE_BUCKETS] + ["unknown"]
    return [buckets[key] for key in order]


def _trend_and_throughput(
    db: Session,
    *,
    all_lots: list[InventoryLot],
    current_lots: list[InventoryLot],
    days: int,
    as_of: date,
) -> tuple[list[dict], list[dict]]:
    dates = [as_of - timedelta(days=offset) for offset in range(days - 1, -1, -1)]
    lot_map = {row.id: row for row in all_lots}
    if not lot_map:
        return ([{"date": value.isoformat(), "values": {}} for value in dates], [])
    start_utc, _ = beijing_date_bounds_utc_naive(dates[0])
    _unused, end_utc = beijing_date_bounds_utc_naive(as_of)
    movements = db.scalars(
        select(InventoryMovement)
        .where(
            InventoryMovement.inventory_lot_id.in_(list(lot_map)),
            InventoryMovement.created_at >= start_utc,
            InventoryMovement.created_at < end_utc,
        )
        .order_by(InventoryMovement.created_at, InventoryMovement.id)
    ).all()
    net_by_date: dict[date, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    throughput: dict[tuple[date, str, str], dict[str, int]] = defaultdict(
        lambda: {"inbound": 0, "outbound": 0, "adjustment": 0}
    )
    for movement in movements:
        lot = lot_map.get(movement.inventory_lot_id)
        if lot is None:
            continue
        business_date = utc_naive_to_beijing_date(movement.created_at)
        key = _quantity_key(lot.inventory_type, movement.unit)
        before = int(movement.before_available + movement.before_reserved)
        after = int(movement.after_available + movement.after_reserved)
        delta = after - before
        net_by_date[business_date][key] += delta
        throughput_key = (business_date, lot.inventory_type, movement.unit)
        if movement.movement_type in EXTERNAL_INBOUND_MOVEMENTS and delta > 0:
            throughput[throughput_key]["inbound"] += delta
        elif movement.movement_type in EXTERNAL_OUTBOUND_MOVEMENTS and delta < 0:
            throughput[throughput_key]["outbound"] += abs(delta)
        elif delta:
            throughput[throughput_key]["adjustment"] += abs(delta)

    current: dict[str, int] = defaultdict(int)
    for row in current_lots:
        current[_quantity_key(row.inventory_type, row.unit)] += _usable_quantity(row)
    trend_by_date: dict[date, dict[str, int]] = {}
    running = dict(current)
    for business_date in reversed(dates):
        trend_by_date[business_date] = dict(running)
        for key, delta in net_by_date.get(business_date, {}).items():
            running[key] = running.get(key, 0) - delta
    trend = [
        {"date": business_date.isoformat(), "values": trend_by_date[business_date]}
        for business_date in dates
    ]
    throughput_rows = [
        {
            "date": business_date.isoformat(),
            "inventory_type": inventory_type,
            "unit": unit,
            **values,
        }
        for (business_date, inventory_type, unit), values in sorted(throughput.items())
    ]
    return trend, throughput_rows


def build_warehouse_twin_dashboard(
    db: Session,
    *,
    lots: list[InventoryLot],
    locations: list[WarehouseLocation],
    pallets: list[InventoryPallet],
    floors: list[WarehouseFloor],
    visible_customer_ids: set[int] | None,
    days: int,
    as_of: date,
) -> dict:
    """Build one read-only projection from formal lots, pallets, locations and movements."""

    current_lots = [
        row
        for row in lots
        if row.status in {"active", "frozen"} and _physical_quantity(row) > 0
    ]
    lots_by_location: dict[int, list[InventoryLot]] = defaultdict(list)
    for row in current_lots:
        lots_by_location[row.warehouse_location_id].append(row)

    visible_pallets = [
        row
        for row in pallets
        if row.is_current and _pallet_visible(row, visible_customer_ids)
    ]
    pallet_by_location = {
        row.location_id: row for row in visible_pallets if row.location_id is not None
    }
    visible_location_ids = set(lots_by_location) | set(pallet_by_location)
    location_rows = []
    for location in locations:
        if visible_customer_ids is not None and location.id not in visible_location_ids:
            continue
        location_rows.append(
            _location_payload(
                location,
                lots=lots_by_location.get(location.id, []),
                pallet=pallet_by_location.get(location.id),
                as_of=as_of,
            )
        )

    floor_records = {row.floor_number: row for row in floors}
    floor_summaries = []
    for floor_number in (1, 3):
        floor = floor_records.get(floor_number)
        floor_locations = [
            row for row in location_rows if row["floor_number"] == floor_number
        ]
        occupied = [row for row in floor_locations if row["occupancy_status"] == "occupied"]
        empty = [
            row
            for row in floor_locations
            if row["occupancy_status"] == "empty" and row["position_status"] == "mapped"
        ]
        unlocated = [
            row
            for row in floor_locations
            if row["position_status"] in {"unplaced", "unlocated", "area_only"}
        ]
        occupied_pallets = len(
            {
                row["pallet"]["pallet_id"]
                for row in occupied
                if row["pallet"] is not None
            }
        )
        capacity = warehouse_capacity_summary(
            floor,
            occupied_pallets=occupied_pallets,
            visible=visible_customer_ids is None,
        )
        floor_summaries.append(
            {
                "floor_code": f"{floor_number}F",
                "floor_name": floor.floor_name if floor is not None else f"{floor_number}楼",
                "construction_status": floor.construction_status if floor is not None else "not_started",
                "occupied_pallets": occupied_pallets,
                "occupied_locations": len(occupied),
                "empty_mapped_locations": len(empty),
                "unlocated_locations": len(unlocated),
                "active_lots": sum(len(lots_by_location.get(row["location_id"], [])) for row in floor_locations),
                "capacity": capacity,
            }
        )

    floor_distribution = []
    for floor in floor_summaries:
        floor_code = floor["floor_code"]
        floor_lots = [
            row
            for row in current_lots
            if _floor_key(row.location.warehouse_floor if row.location else None) == floor_code
        ]
        floor_distribution.append(
            {
                "floor_code": floor_code,
                "occupied_pallets": floor["occupied_pallets"],
                "active_lots": len(floor_lots),
                "quantities": _quantity_groups(floor_lots),
            }
        )

    area_distribution = []
    area_groups: dict[tuple[str, str], list[InventoryLot]] = defaultdict(list)
    for row in current_lots:
        floor_code = _floor_key(row.location.warehouse_floor if row.location else None)
        area_groups[(floor_code, row.location.area_code if row.location else "UNLOCATED")].append(row)
    for (floor_code, area_code), area_lots in sorted(area_groups.items()):
        area_distribution.append(
            {
                "floor_code": floor_code,
                "area_code": area_code or "UNLOCATED",
                "lot_count": len(area_lots),
                "quantities": _quantity_groups(area_lots),
            }
        )

    trend, throughput = _trend_and_throughput(
        db,
        all_lots=lots,
        current_lots=current_lots,
        days=days,
        as_of=as_of,
    )
    age_distribution = _age_distribution(current_lots, as_of)
    old_lot_ids = {
        row.id for row in current_lots if (_age_days(row, as_of) or 0) > 90
    }
    old_pallet_ids = {
        row.pallet_item.pallet_id
        for row in current_lots
        if row.id in old_lot_ids and row.pallet_item is not None
    }
    unresolved_lots = [
        row
        for row in current_lots
        if row.location is None
        or row.location.warehouse_floor is None
        or not row.location.area_code
        or (row.location.placement_status or "placed") == "unplaced"
    ]
    visible_capacities = [
        floor["capacity"]
        for floor in floor_summaries
        if floor["capacity"]["visible"] and floor["capacity"]["reference_pallet_capacity"]
    ]
    capacity_reference_total = sum(
        int(row["reference_pallet_capacity"] or 0) for row in visible_capacities
    )
    capacity_occupied_total = sum(int(row["occupied_pallets"] or 0) for row in visible_capacities)
    tightest_floor = max(
        (
            floor
            for floor in floor_summaries
            if floor["capacity"]["visible"]
            and floor["capacity"]["utilization_percent"] is not None
        ),
        key=lambda floor: float(floor["capacity"]["utilization_percent"]),
        default=None,
    )
    capacity_alerts = []
    if visible_customer_ids is None:
        planning_floors = [
            floor for floor in floor_summaries if floor["capacity"]["basis"] == "planning"
        ]
        if planning_floors:
            capacity_alerts.append(
                {
                    "code": "capacity_planning_basis",
                    "level": "warning",
                    "message": "当前按规划容量预警；区域现场复核完成后自动切换为安全容量。",
                }
            )
        for floor in floor_summaries:
            capacity = floor["capacity"]
            alert_level = capacity["alert_level"]
            if alert_level == "unknown":
                capacity_alerts.append(
                    {
                        "code": f"capacity_missing_{floor['floor_code'].lower()}",
                        "level": "warning",
                        "floor_code": floor["floor_code"],
                        "message": f"{floor['floor_code']}尚未填写规划容量，暂不能计算容量预警。",
                    }
                )
            elif alert_level != "normal":
                threshold_label = {
                    "attention": "达到80%",
                    "warning": "达到90%",
                    "critical": "达到95%",
                    "over_capacity": "超过100%",
                }[alert_level]
                capacity_alerts.append(
                    {
                        "code": f"capacity_{alert_level}_{floor['floor_code'].lower()}",
                        "level": "error" if alert_level in {"critical", "over_capacity"} else "warning",
                        "floor_code": floor["floor_code"],
                        "message": (
                            f"{floor['floor_code']}已占 {capacity['occupied_pallets']}/"
                            f"{capacity['reference_pallet_capacity']} 个栈板位，"
                            f"利用率 {capacity['utilization_percent']}%，{threshold_label}。"
                        ),
                    }
                )
    temporary_occupied_count = sum(
        1
        for row in location_rows
        if row["is_temporary"] and row["occupancy_status"] == "occupied"
    )
    if temporary_occupied_count and visible_customer_ids is None:
        capacity_alerts.append(
            {
                "code": "temporary_capacity_pressure",
                "level": "error",
                "message": f"有 {temporary_occupied_count} 个临时位置正在占用，不计入长期容量但必须尽快整理。",
            }
        )

    return {
        "schema_version": "P1-29-v1",
        "mode": "erp_business_twin",
        "read_only": True,
        "generated_at": utc_naive_to_api(utc_now_naive()),
        "as_of_date": as_of.isoformat(),
        "days": days,
        "scope": {
            "customer_restricted": visible_customer_ids is not None,
            "notice": (
                "当前账号仅显示授权客户库存；容量、空位和全仓总量已隐藏。"
                if visible_customer_ids is not None
                else "只读投影 ERP 已确认库存、栈板、位置和流水；未确认现场搬动不会自动出现。"
            ),
        },
        "summary": {
            "active_lots": len(current_lots),
            "occupied_pallets": len({row.id for row in visible_pallets}),
            "empty_mapped_locations": (
                sum(row["empty_mapped_locations"] for row in floor_summaries)
                if visible_customer_ids is None
                else None
            ),
            "long_age_lots": len(old_lot_ids),
            "long_age_pallets": len(old_pallet_ids),
            "unlocated_lots": len(unresolved_lots),
            "temporary_occupied_locations": sum(
                1
                for row in location_rows
                if row["is_temporary"] and row["occupancy_status"] == "occupied"
            ),
            "capacity": (
                {
                    "visible": True,
                    "reference_pallet_capacity": capacity_reference_total,
                    "occupied_pallets": capacity_occupied_total,
                    "empty_pallet_slots": max(capacity_reference_total - capacity_occupied_total, 0),
                    "utilization_percent": (
                        round(capacity_occupied_total * 100 / capacity_reference_total, 1)
                        if capacity_reference_total
                        else None
                    ),
                    "tightest_floor_code": tightest_floor["floor_code"] if tightest_floor else None,
                    "tightest_floor_utilization_percent": (
                        tightest_floor["capacity"]["utilization_percent"] if tightest_floor else None
                    ),
                }
                if visible_customer_ids is None
                else {"visible": False}
            ),
            "quantities": _quantity_groups(current_lots),
        },
        "floors": floor_summaries,
        "locations": location_rows,
        "distribution": {
            "floors": floor_distribution,
            "areas": area_distribution,
        },
        "age_distribution": age_distribution,
        "trend": trend,
        "throughput": throughput,
        "alerts": [
            *capacity_alerts,
            *(
                [
                    {
                        "code": "unlocated_inventory",
                        "level": "error",
                        "message": f"有 {len(unresolved_lots)} 个库存批次缺少可高亮的真实位置。",
                    }
                ]
                if unresolved_lots
                else []
            ),
        ],
    }


def build_inventory_code_search_results(
    *,
    lots: list[InventoryLot],
    keyword: str,
    as_of: date,
) -> dict:
    results = []
    floor_counts: dict[str, dict] = {}
    for row in lots:
        payload = _lot_payload(row, as_of)
        location = row.location
        position_status, map_position = (
            _location_position(location) if location is not None else ("unlocated", None)
        )
        floor_code = _floor_key(location.warehouse_floor if location else None)
        result = {
            **payload,
            "floor_code": floor_code,
            "area_code": location.area_code if location else None,
            "location_id": location.id if location else None,
            "location_code": location.location_code if location else None,
            "location_name": location.location_name if location else "待定位",
            "position_status": position_status,
            "map_position": map_position,
            "pallet_id": row.pallet_item.pallet_id if row.pallet_item else None,
            "pallet_code": (
                row.pallet_item.pallet.pallet_code
                if row.pallet_item is not None and row.pallet_item.pallet is not None
                else None
            ),
        }
        results.append(result)
        floor = floor_counts.setdefault(
            floor_code,
            {"floor_code": floor_code, "location_ids": set(), "lot_count": 0, "quantities": {}},
        )
        if result["location_id"] is not None:
            floor["location_ids"].add(result["location_id"])
        floor["lot_count"] += 1
        quantity_key = _quantity_key(row.inventory_type, row.unit)
        floor["quantities"][quantity_key] = floor["quantities"].get(quantity_key, 0) + _physical_quantity(row)
    summaries = []
    for floor in floor_counts.values():
        location_ids = floor.pop("location_ids")
        summaries.append(
            {
                **floor,
                "location_count": len(location_ids),
            }
        )
    summaries.sort(key=lambda row: row["floor_code"])
    results.sort(key=lambda row: (row["floor_code"], row["area_code"] or "", row["location_code"] or ""))
    return {
        "keyword": keyword,
        "generated_at": utc_naive_to_api(utc_now_naive()),
        "result_count": len(results),
        "floor_summaries": summaries,
        "items": results,
        "notice": (
            "命中库存缺少已发布坐标时只显示文字位置，不生成虚假地图点。"
            if any(row["position_status"] != "mapped" for row in results)
            else "全部命中均可定位到已发布地图位置。"
        ),
    }


def inventory_search_matches(row: InventoryLot, keyword: str, as_of: date) -> bool:
    """Match only already-visible inventory facts; this never widens customer scope."""
    needle = str(keyword or "").strip().casefold()
    if not needle:
        return False
    payload = _lot_payload(row, as_of)
    location = row.location
    pallet = row.pallet_item.pallet if row.pallet_item is not None else None
    searchable = " ".join(
        str(value or "")
        for value in (
            payload.get("inventory_code"),
            payload.get("product_name"),
            payload.get("customer_name"),
            payload.get("lot_number"),
            location.location_code if location else None,
            location.location_name if location else None,
            location.area_code if location else None,
            pallet.pallet_code if pallet else None,
        )
    ).casefold()
    return needle in searchable
