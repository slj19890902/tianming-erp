from __future__ import annotations

from app.services.warehouse_storage_usage import effective_inventory_usages

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
import json
from math import ceil
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

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
    InventoryReservation,
    WarehouseArea,
    WarehouseAreaStoragePolicy,
    WarehouseFloor,
    WarehouseGroundOccupancy,
    WarehouseLocation,
    WarehouseLocationDiscrepancy,
    WarehouseUnmatchedInventoryObservation,
)
from app.models.order import Order, OrderItem
from app.models.production import ProductionCompletion
from app.models.product_bom import SalesOrderItemBomComponent
from app.services.warehouse_pallet_standard import standard_pallet_contract
from app.services.warehouse_location_address import (
    employee_area_name,
    employee_location_name,
    location_address_payload,
)
from app.services.location_candidates import (
    current_same_location_pallet,
    load_warehouse_location_projection_contexts,
    warehouse_location_projection,
)
from app.services.product_specification import dimension_specification
from app.services.warehouse_movement_batch import pallet_move_source_issue
from app.services.warehouse_relocation_pending import is_pending_relocation_location


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
DIRECT_DISPATCH_LOCATION_CODE = "F1-DISPATCH-01"
DELAYED_DISPATCH_LEFT_AREA_CODE = "SEMI-008"


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


def _pallet_physical_quantity(row: InventoryPallet) -> int:
    total = 0
    for item in row.items:
        lot = item.inventory_lot
        if lot is not None:
            if lot.status in {"active", "frozen"}:
                total += max(_physical_quantity(lot), 0)
        elif Decimal(str(item.quantity or 0)) > 0:
            total += int(Decimal(str(item.quantity or 0)))
    return total


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
            "product_id": detail.product_id,
            "inventory_code": detail.inventory_code_snapshot,
            "product_name": detail.product_name_snapshot,
            "customer_id": detail.owner_customer_id,
            "customer_name": detail.owner_customer_name_snapshot or "通用库存",
            "customer_short_name": (
                str(detail.customer.chinese_short_name or "").strip()
                if detail.customer is not None
                else None
            ),
            "specification": dimension_specification(
                detail.length_mm,
                detail.width_mm,
                detail.height_mm,
            ),
            "material": detail.material_code_snapshot,
            "flute_type": detail.flute_type_snapshot,
        }
    if row.semi_finished_detail is not None:
        detail = row.semi_finished_detail
        allowed_product_ids = sorted(
            {
                int(binding.product_id)
                for binding in row.allowed_products
                if binding.product_id is not None
            }
        )
        dimensions = (
            f"{detail.board_length_mm}×{detail.board_width_mm}mm"
            if detail.board_length_mm and detail.board_width_mm
            else None
        )
        return {
            "product_id": (
                allowed_product_ids[0]
                if len(allowed_product_ids) == 1
                else None
            ),
            "allowed_product_ids": allowed_product_ids,
            "inventory_code": detail.material_code_snapshot,
            "product_name": (
                detail.internal_name
                or ("客户通用纸板备料"
                if detail.customer_generic_eligible
                else "客户专用纸板备料"
                if detail.owner_customer_id
                else "通用半成品片料")
            ),
            "customer_id": detail.owner_customer_id,
            "customer_name": detail.owner_customer_name_snapshot or "通用库存",
            "customer_short_name": (
                str(detail.customer.chinese_short_name or "").strip()
                if detail.customer is not None
                else None
            ),
            "specification": dimensions,
            "flute_type": detail.flute_type,
            "material": detail.normalized_material_code or detail.material_code_snapshot,
        }
    return {
        "product_id": None,
        "inventory_code": None,
        "product_name": "待补充库存名称",
        "customer_id": None,
        "customer_name": "待确认",
        "customer_short_name": None,
        "specification": None,
        "material": None,
    }


def _parent_delivery_inventory_projections(
    db: Session,
    lots: Iterable[InventoryLot],
) -> dict[int, dict]:
    """Project component ledgers as one physical parent-delivery product.

    This is display metadata only.  The component lots remain authoritative so
    delivery consumption, shortages and traceability keep their exact piece
    quantities.  A projection is calculated independently for every physical
    location/pallet and uses the shortest required component as the set count.
    """

    lot_map = {int(row.id): row for row in lots if int(row.id or 0) > 0}
    if not lot_map:
        return {}
    from app.models.bom_subkit import OrderSubkit
    rows = db.execute(
        select(
            InventoryReservation,
            SalesOrderItemBomComponent,
            OrderItem,
            Order,
        )
        .join(
            SalesOrderItemBomComponent,
            SalesOrderItemBomComponent.id
            == InventoryReservation.sales_order_item_bom_component_id,
        )
        .join(OrderItem, OrderItem.id == InventoryReservation.order_item_id)
        .join(Order, Order.id == OrderItem.order_id)
        .where(
            InventoryReservation.inventory_lot_id.in_(list(lot_map)),
            InventoryReservation.status.in_(("active", "partial")),
            InventoryReservation.reserved_stock_quantity
            > InventoryReservation.consumed_stock_quantity
            + InventoryReservation.released_stock_quantity,
            OrderItem.composite_fulfillment_mode_snapshot == "parent_delivery",
            ~OrderItem.id.in_(select(OrderSubkit.order_item_id)),
        )
        .order_by(InventoryReservation.id)
    ).all()
    if not rows:
        return {}
    item_ids = sorted({int(item.id) for _reservation, _snapshot, item, _order in rows})
    demands_by_item: dict[int, list[SalesOrderItemBomComponent]] = defaultdict(list)
    for snapshot in db.scalars(
        select(SalesOrderItemBomComponent)
        .where(SalesOrderItemBomComponent.sales_order_item_id.in_(item_ids))
        .order_by(
            SalesOrderItemBomComponent.sales_order_item_id,
            SalesOrderItemBomComponent.display_order,
            SalesOrderItemBomComponent.id,
        )
    ).all():
        demands_by_item[int(snapshot.sales_order_item_id)].append(snapshot)

    groups: dict[tuple[int, int, int | None], dict] = {}
    for reservation, snapshot, item, order in rows:
        lot = lot_map.get(int(reservation.inventory_lot_id))
        if lot is None:
            continue
        current_pallet = current_same_location_pallet(lot)
        pallet_id = int(current_pallet.id) if current_pallet is not None else None
        key = (int(item.id), int(lot.warehouse_location_id), pallet_id)
        group = groups.setdefault(
            key,
            {
                "item": item,
                "order": order,
                "lot_ids": set(),
                "pieces_by_snapshot": defaultdict(int),
            },
        )
        remaining = max(
            int(reservation.reserved_stock_quantity or 0)
            - int(reservation.consumed_stock_quantity or 0)
            - int(reservation.released_stock_quantity or 0),
            0,
        )
        # A component completion has one reservation per lot.  Capping by the
        # current reserved balance prevents damaged/reclassified pieces from
        # being presented as ready parent sets.
        remaining = min(remaining, max(int(lot.quantity_reserved or 0), 0))
        group["lot_ids"].add(int(lot.id))
        group["pieces_by_snapshot"][int(snapshot.id)] += remaining

    result: dict[int, dict] = {}
    for (item_id, location_id, pallet_id), group in groups.items():
        item: OrderItem = group["item"]
        order: Order = group["order"]
        component_rows = []
        required_set_counts: list[int] = []
        for snapshot in demands_by_item.get(item_id, []):
            quantity_per_set = max(int(snapshot.quantity_per_set or 0), 1)
            available_pieces = int(
                group["pieces_by_snapshot"].get(int(snapshot.id), 0)
            )
            complete_sets = available_pieces // quantity_per_set
            if snapshot.is_required:
                required_set_counts.append(complete_sets)
            component_rows.append(
                {
                    "snapshot_id": int(snapshot.id),
                    "product_code": snapshot.snapshot_component_product_code,
                    "product_name": snapshot.snapshot_component_product_name,
                    "quantity_per_set": quantity_per_set,
                    "available_piece_quantity": available_pieces,
                    "complete_set_quantity": complete_sets,
                    "is_required": bool(snapshot.is_required),
                }
            )
        remaining_order_sets = max(
            int(item.quantity or 0) - int(item.delivered_quantity or 0),
            0,
        )
        available_sets = min(required_set_counts, default=0)
        available_sets = min(available_sets, remaining_order_sets)
        lot_ids = sorted(int(value) for value in group["lot_ids"])
        group_key = (
            f"parent-delivery:{item_id}:location:{location_id}:"
            f"pallet:{pallet_id or 'loose'}"
        )
        summary = {
            "group_key": group_key,
            "order_item_id": item_id,
            "order_number": order.order_number,
            "product_id": int(item.product_id),
            "inventory_code": item.snapshot_product_code,
            "product_name": item.snapshot_product_name,
            "available_set_quantity": available_sets,
            "remaining_order_set_quantity": remaining_order_sets,
            "unit": "sets",
            "component_lot_count": len(lot_ids),
            "component_lot_ids": lot_ids,
            "components": component_rows,
        }
        representative_lot_id = lot_ids[0]
        for lot_id in lot_ids:
            result[lot_id] = {
                "composite_parent_group_key": group_key,
                "composite_parent_summary": (
                    summary if lot_id == representative_lot_id else None
                ),
            }
    return result


def _lot_payload(
    row: InventoryLot,
    as_of: date,
    *,
    composite_projection: dict | None = None,
    stocktake_decrease_issues: dict[int, str | None] | None = None,
) -> dict:
    from app.services.warehouse_display_units import lot_display_unit
    business = _lot_business_fields(row)
    age_days = _age_days(row, as_of)
    payload = {
        "lot_id": row.id,
        "lot_number": row.lot_number,
        "inventory_type": row.inventory_type,
        "inventory_usage": ("raw_material" if row.semi_finished_detail is not None and row.semi_finished_detail.sheet_type == "raw_board" else row.inventory_type),
        **business,
        "quantity": _physical_quantity(row),
        "available_quantity": int(row.quantity_available),
        "reserved_quantity": int(row.quantity_reserved),
        "damaged_quantity": int(row.quantity_damaged),
        "unit": lot_display_unit(row),
        "age_days": age_days,
        "age_bucket": _age_bucket_key(age_days),
        "stock_date_accuracy": row.stock_date_accuracy,
        "stock_date": row.stock_date.isoformat() if row.stock_date and row.stock_date_accuracy != "unknown" else None,
        "status": row.status,
        "version": row.version,
    }
    if composite_projection:
        payload.update(composite_projection)
    if stocktake_decrease_issues is not None:
        has_projection = int(row.id) in stocktake_decrease_issues
        decrease_issue = stocktake_decrease_issues.get(int(row.id))
        payload.update(
            {
                "stocktake_decrease_eligible": bool(
                    has_projection and decrease_issue is None
                ),
                "stocktake_decrease_block_reason": (
                    decrease_issue
                    if has_projection
                    else "当前投影未计算盘点调减资格"
                ),
            }
        )
    return payload


def _pallet_visible(
    pallet: InventoryPallet,
    visible_customer_ids: set[int] | None,
    *,
    visible_lot_ids: set[int] | None = None,
) -> bool:
    if visible_customer_ids is None:
        return True
    # A pallet is one physical handling unit. Customer-scoped accounts must
    # never receive a partial projection of a mixed or unidentified pallet.
    # Snapshot-only rows are not backed by the scoped lot/product authority,
    # so they are intentionally admin-only in this dashboard projection.
    return bool(pallet.items) and all(
        item.customer_id is not None
        and item.customer_id in visible_customer_ids
        and item.inventory_lot_id is not None
        and visible_lot_ids is not None
        and item.inventory_lot_id in visible_lot_ids
        for item in pallet.items
    )


def _pallet_has_any_physical_goods(
    pallet: InventoryPallet,
    positive_lots_by_id: dict[int, InventoryLot],
) -> bool:
    """Separate a real loaded pallet from an empty current-row residue."""

    return any(
        (
            item.inventory_lot_id is None
            and float(item.quantity or 0) > 0
        )
        or (
            item.inventory_lot_id is not None
            and int(item.inventory_lot_id) in positive_lots_by_id
        )
        for item in pallet.items
    )


def _pallet_has_projectable_physical_goods(
    pallet: InventoryPallet,
    positive_lots_by_id: dict[int, InventoryLot],
) -> bool:
    """Require positive goods and one location identity before map occupancy."""

    for item in pallet.items:
        if item.inventory_lot_id is None:
            if float(item.quantity or 0) > 0:
                return True
            continue
        lot = positive_lots_by_id.get(int(item.inventory_lot_id))
        if lot is None:
            continue
        current_pallet = current_same_location_pallet(lot)
        if current_pallet is not None and int(current_pallet.id) == int(pallet.id):
            return True
    return False


def _delayed_direct_dispatch_projection(
    db: Session,
    *,
    pallets: list[InventoryPallet],
    positive_lots_by_id: dict[int, InventoryLot],
    location_payloads: list[dict],
    as_of: date,
    idle_days: int,
    hide_empty_targets: bool,
) -> dict:
    """Recommend, but never execute, a physical move for delayed direct stock."""

    target_rows = [] if hide_empty_targets else [
        row
        for row in location_payloads
        if row.get("floor_code") == "3F"
        and str(row.get("area_code") or "").upper()
        == DELAYED_DISPATCH_LEFT_AREA_CODE
        and row.get("position_status") == "mapped"
        and row.get("occupancy_status") == "empty"
        and row.get("can_receive_pallet") is True
        and "finished" in (row.get("allowed_inventory_types") or [])
    ]
    targets = [
        {
            "location_id": int(row["location_id"]),
            "location_code": row["location_code"],
            "location_name": row["employee_location_name"],
            "layout_version": int(row["map_position"]["version"]),
        }
        for row in target_rows
        if row.get("map_position") and int(row["map_position"].get("version") or 0) > 0
    ]

    source_locations = {
        int(row["location_id"]): row
        for row in location_payloads
        if row.get("location_id") is not None
    }

    def is_eligible_source_location(pallet: InventoryPallet) -> bool:
        location = pallet.location
        if location is None:
            return False
        # Pre-space-ledger direct stock may remain on the audited F1 staging
        # location.  It has no current-map feature, so retain this narrowly
        # defined legacy compatibility instead of accepting arbitrary unmapped
        # locations.
        if (
            location.location_code == DIRECT_DISPATCH_LOCATION_CODE
            and location.source_version == "P1-25C"
            and location.is_active
            and location.placement_status == "placed"
        ):
            return True
        return (
            source_locations.get(int(location.id), {}).get("position_status")
            == "mapped"
        )

    # New direct completions use a published, physical FIN/3F map slot when
    # the space ledger is available.  F1-DISPATCH-01 remains a valid legacy
    # origin, but it must not be the only way to discover direct stock.
    dispatch_pallets = [
        pallet
        for pallet in pallets
        if pallet.location is not None
        and pallet.is_current
        and pallet.status == "active"
        and is_eligible_source_location(pallet)
        and not (
            pallet.location.warehouse_floor == 3
            and str(pallet.location.area_code or "").upper()
            == DELAYED_DISPATCH_LEFT_AREA_CODE
        )
    ]
    completion_ids = {
        int(lot.source_ref_id)
        for pallet in dispatch_pallets
        for item in pallet.items
        if item.inventory_lot_id is not None
        and (lot := positive_lots_by_id.get(int(item.inventory_lot_id))) is not None
        and lot.source_ref_type == "production_completion"
        and lot.source_ref_id is not None
    }
    completion_facts = {
        int(completion.id): (completion, item, order)
        for completion, item, order in db.execute(
            select(ProductionCompletion, OrderItem, Order)
            .join(OrderItem, OrderItem.id == ProductionCompletion.order_item_id)
            .join(Order, Order.id == OrderItem.order_id)
            .where(ProductionCompletion.id.in_(completion_ids))
        ).all()
    } if completion_ids else {}

    terminal_order_statuses = {
        "delivered", "completed", "archived", "closed", "dead", "cancelled"
    }
    candidates = []
    for pallet in dispatch_pallets:
        pallet_lots: list[InventoryLot] = []
        invalid = not pallet.items
        for item in pallet.items:
            if item.inventory_lot_id is None:
                invalid = invalid or float(item.quantity or 0) > 0
                continue
            lot = positive_lots_by_id.get(int(item.inventory_lot_id))
            if lot is None:
                invalid = True
                continue
            if (
                item.match_status != "matched"
                or lot.inventory_type != "finished"
                or lot.status != "active"
                or int(lot.quantity_damaged or 0) > 0
                or _usable_quantity(lot) <= 0
                or lot.warehouse_location_id != pallet.location_id
                or lot.source_ref_type != "production_completion"
                or lot.source_ref_id is None
            ):
                invalid = True
                continue
            pallet_lots.append(lot)
        source_ids = {int(lot.source_ref_id) for lot in pallet_lots}
        if invalid or not pallet_lots or len(source_ids) != 1:
            continue
        completion_fact = completion_facts.get(next(iter(source_ids)))
        if completion_fact is None:
            continue
        completion, order_item, order = completion_fact
        if (
            completion.status != "posted"
            or completion.initial_disposition != "direct"
            or order.status in terminal_order_statuses
            or any(
                int(lot.source_ref_id or 0) != int(completion.id)
                for lot in pallet_lots
            )
        ):
            continue
        completion_date = utc_naive_to_beijing_date(completion.completed_at)
        age_days = max(0, (as_of - completion_date).days)
        if age_days < idle_days:
            continue
        item_payloads = [_lot_payload(lot, as_of) for lot in pallet_lots]
        candidates.append(
            {
                "pallet_id": int(pallet.id),
                "pallet_code": pallet.pallet_code,
                "version": int(pallet.version),
                "source_location_id": int(pallet.location_id),
                "source_floor_code": f"{int(pallet.location.warehouse_floor)}F",
                "source_location_code": pallet.location.location_code,
                "source_location_name": employee_location_name(pallet.location),
                "completion_id": int(completion.id),
                "completed_date": completion_date.isoformat(),
                "idle_days": age_days,
                "order_id": int(order.id),
                "order_number": order.order_number,
                "order_item_id": int(order_item.id),
                "delivery_date": (
                    order.delivery_date.isoformat() if order.delivery_date else None
                ),
                "quantity": sum(_physical_quantity(lot) for lot in pallet_lots),
                "unit": pallet_lots[0].unit,
                "customer_id": item_payloads[0].get("customer_id"),
                "customer_name": item_payloads[0].get("customer_name"),
                "product_names": sorted(
                    {
                        str(item.get("product_name") or "产品名称待补充")
                        for item in item_payloads
                    }
                ),
                "lot_ids": [int(lot.id) for lot in pallet_lots],
                "recommended_floor_code": "3F",
                "recommended_area_code": DELAYED_DISPATCH_LEFT_AREA_CODE,
                "can_plan_move": bool(targets),
            }
        )
    candidates.sort(key=lambda row: (-int(row["idle_days"]), int(row["pallet_id"])))
    return {
        "policy": {
            "idle_days": idle_days,
            "minimum_idle_days": 1,
            "maximum_idle_days": 30,
            "source_rule": "已发布地图中的直接完工当前栈板",
            "legacy_source_location_code": DIRECT_DISPATCH_LOCATION_CODE,
            "recommended_floor_code": "3F",
            "recommended_area_code": DELAYED_DISPATCH_LEFT_AREA_CODE,
            "writes_inventory": False,
            "notice": (
                "这里只列出可整理的真实待送栈板，不会自动改库存位置；"
                "请先完成现场搬运，再使用现有移货确认提交。"
            ),
        },
        "candidate_count": len(candidates),
        "available_target_count": len(targets),
        "targets": targets,
        "items": candidates,
    }


def _location_position(
    row: WarehouseLocation,
    **projection_context: object,
) -> tuple[str, dict | None]:
    projection = warehouse_location_projection(row, **projection_context)
    return (
        str(projection["position_status"]),
        projection["map_position"],
    )


def _floor_key(value: int | None) -> str:
    return f"{value}F" if value else "UNLOCATED"


def warehouse_capacity_summary(
    floor: WarehouseFloor | None,
    *,
    occupied_pallets: int,
    visible: bool,
) -> dict:
    """Return the single capacity projection used by ledgers and dashboards.

    Planning capacity is a reference only.  Until every enabled area is field
    reviewed, it must never drive utilization, free-slot or threshold alerts.
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

    areas = (
        [
            area
            for area in floor.areas
            if area.construction_status != "archived"
            and not (
                area.storage_policy is not None
                and area.storage_policy.status == "archived"
            )
        ]
        if floor is not None
        else []
    )
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
    reference_capacity = safe_capacity if confirmed else None
    utilization = (
        round(occupied_pallets * 100 / reference_capacity, 1)
        if reference_capacity
        else None
    )
    if not confirmed:
        alert_level, alert_label = "awaiting_confirmation", "现场安全容量待确认"
    elif utilization is None:
        alert_level, alert_label = "unknown", "安全容量资料待补"
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
        "basis": "confirmed" if confirmed else "planning_reference",
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
        "label": "现场安全容量已确认" if confirmed else "规划参考，不参与满载率",
        "thresholds": (
            {
                key: ceil(reference_capacity * ratio)
                for key, ratio in CAPACITY_THRESHOLDS.items()
            }
            if reference_capacity
            else None
        ),
    }


def suppress_capacity_metrics_until_all_confirmed(capacity: dict) -> dict:
    """Keep review progress visible without publishing partial capacity math."""

    if not capacity.get("visible"):
        return capacity
    return {
        **capacity,
        "confirmed": False,
        "safe_pallet_capacity": None,
        "confirmed_area_capacity": None,
        "reference_pallet_capacity": None,
        "empty_pallet_slots": None,
        "utilization_percent": None,
        "alert_level": "awaiting_confirmation",
        "alert_label": "现场安全容量待确认",
        "status": "awaiting_field_confirmation",
        "label": "规划参考，不参与满载率",
        "thresholds": None,
    }


def _location_payload(
    row: WarehouseLocation,
    *,
    lots: list[InventoryLot],
    pallets: list[InventoryPallet],
    as_of: date,
    projection_context: dict | None = None,
    allowed_inventory_types: list[str] | None = None,
    composite_projections: dict[int, dict] | None = None,
    stocktake_decrease_issues: dict[int, str | None] | None = None,
    pallet_move_issues: dict[int, str | None] | None = None,
) -> dict:
    context = projection_context or {}
    floor = context.get("floor")
    area = context.get("area")
    projection = warehouse_location_projection(row, **context)
    position_status = str(projection["position_status"])
    map_position = projection["map_position"]
    address_payload = location_address_payload(
        row,
        area=area,
        floor=floor,
        position_status=position_status,
        area_sequence=(int(context["area_sequence"]) if context.get("area_sequence") else None),
    )
    layout = context.get("layout")
    policy = context.get("policy")
    storage_layout = (
        policy.storage_layout
        if isinstance(policy, WarehouseAreaStoragePolicy)
        else None
    )
    is_functional_loose_area = (
        storage_layout == "functional" or row.address_kind == "functional"
    )
    current_pallet_ids = {
        int(pallet.id)
        for pallet in pallets
        if pallet.is_current
        and str(pallet.status or "").strip().lower() == "active"
        and pallet.location_id is not None
        and int(pallet.location_id) == int(row.id)
    }
    pallet_payloads = []
    for pallet in sorted(pallets, key=lambda item: item.id):
        items = [
            {
                **_lot_payload(
                    lot,
                    as_of,
                    composite_projection=(composite_projections or {}).get(
                        int(lot.id)
                    ),
                    stocktake_decrease_issues=stocktake_decrease_issues,
                ),
                "pallet_projection_status": "current_same_location",
            }
            for lot in lots
            if current_same_location_pallet(lot) is not None
            and int(current_same_location_pallet(lot).id) == int(pallet.id)
        ]
        if not items:
            items = [
                {
                    "lot_id": item.inventory_lot_id,
                    "inventory_code": item.inventory_code,
                    "product_name": item.product_name or "待匹配货物",
                    "item_type": item.item_type,
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
                    "stocktake_decrease_eligible": False,
                    "stocktake_decrease_block_reason": "库存条目尚未匹配正式批次",
                }
                for item in pallet.items
                if item.inventory_lot_id is None
            ]
        pallet_payloads.append(
            {
                "pallet_id": pallet.id,
                "pallet_code": pallet.pallet_code,
                "version": pallet.version,
                "needs_relocation": pallet.needs_relocation,
                "item_count": len(items),
                "items": items,
                "move_eligible": (
                    pallet_move_issues[pallet.id] is None
                    if pallet_move_issues is not None and pallet.id in pallet_move_issues
                    else None
                ),
                "move_block_reason": (pallet_move_issues or {}).get(pallet.id),
            }
        )
    loose_items = [
        {
            **_lot_payload(
                lot,
                as_of,
                composite_projection=(composite_projections or {}).get(int(lot.id)),
                stocktake_decrease_issues=stocktake_decrease_issues,
            ),
            "pallet_projection_status": (
                "functional_loose_inventory"
                if is_functional_loose_area
                else "missing_current_pallet"
            ),
            "pallet_projection_issue": (
                "本区依法存放少量零散库存，无需绑定栈板"
                if is_functional_loose_area
                else "该正数库存批次缺少同库位当前真实栈板"
            ),
        }
        for lot in lots
        if current_same_location_pallet(lot) is None
        or int(current_same_location_pallet(lot).id) not in current_pallet_ids
    ]
    occupied = bool(pallet_payloads) or any(_physical_quantity(lot) > 0 for lot in lots)
    from sqlalchemy.orm import object_session
    from app.services.receipt_putaway import placement_state
    session = object_session(row)
    states = [placement_state(session, lot) for lot in lots] if session else []
    labels = sorted({state["label"] for state in states if state["label"]})
    colors = {state["color"] for state in states if state["color"]}
    receipt_placement = {"label": " / ".join(labels), "color": next(iter(colors)) if len(colors) == 1 else "mixed"}
    return {
        "receipt_placement": receipt_placement,
        "location_id": row.id,
        "location_code": row.location_code,
        "location_name": address_payload["employee_location_name"],
        "location_master_name": row.location_name,
        **address_payload,
        # Rack elevation, desktop map and mobile warehouse views must bind to
        # the same formal rack cell.  Keep these fields explicit in the twin
        # dashboard contract instead of making consumers infer a rack from
        # geometry, display names or array order.
        "map_rack_id": row.map_rack_id,
        "rack_display_name": row.rack_display_name,
        "level_no": row.level_no,
        "slot_no": row.slot_no,
        "address_kind": row.address_kind,
        "address_version": row.address_version,
        "floor_code": _floor_key(row.warehouse_floor),
        "floor_number": row.warehouse_floor,
        "floor_name": floor.floor_name if floor is not None else None,
        "area_code": row.area_code,
        "area_name": employee_area_name(
            area,
            area_code=row.area_code,
            floor_number=(floor.floor_number if floor is not None else row.warehouse_floor),
        ),
        "area_master_name": area.area_name if area is not None else None,
        "map_feature_id": projection["map_feature_id"],
        "published_map_revision": projection["published_map_revision"],
        "map_status": projection["map_status"],
        "map_issue": projection["map_issue"],
        "warehouse_type": row.warehouse_type,
        "allowed_inventory_types": allowed_inventory_types or [],
        "storage_type": row.storage_type,
        "storage_layout": storage_layout,
        "can_receive_pallet": bool(
            not is_functional_loose_area
            and row.address_kind != "functional"
            and storage_layout in {"pallet_ground", "mixed"}
            and layout is not None
            and layout.layout_kind == "physical_pallet"
        ),
        "source_version": row.source_version,
        "is_temporary": row.is_temporary,
        "is_active": row.is_active,
        "position_status": position_status,
        "map_position": map_position,
        "layout_draft_position": (
            {
                "left_pct": _number(layout.left_pct),
                "top_pct": _number(layout.top_pct),
                "width_pct": _number(layout.width_pct),
                "height_pct": _number(layout.height_pct),
                "z_index": layout.z_index,
                "version": layout.version,
                "source_type": layout.source_type,
                "layout_kind": layout.layout_kind,
            }
            if layout is not None and position_status == "unplaced"
            else None
        ),
        "occupancy_status": "occupied" if occupied else "empty",
        # Keep the legacy singular field unambiguous for old consumers. Shared
        # dispatch staging locations must use ``pallets`` to address each ERP
        # system pallet independently.
        "pallet": pallet_payloads[0] if len(pallet_payloads) == 1 else None,
        "pallets": pallet_payloads,
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
        current_pallet = current_same_location_pallet(row)
        if current_pallet is not None:
            pallet_ids[key].add(int(current_pallet.id))
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
    dispatch_idle_days: int = 3,
    stocktake_decrease_issues: dict[int, str | None] | None = None,
) -> dict:
    """Build one read-only projection from formal lots, pallets, locations and movements."""

    visible_lot_ids = {row.id for row in lots} if visible_customer_ids is not None else None
    positive_lots_by_id = {
        int(row.id): row
        for row in lots
        if row.status in {"active", "frozen"} and _physical_quantity(row) > 0
    }
    scoped_current_pallets = [
        row
        for row in pallets
        if row.is_current
        and str(row.status or "").strip().lower() == "active"
        and _pallet_visible(
            row,
            visible_customer_ids,
            visible_lot_ids=visible_lot_ids,
        )
    ]
    visible_pallets = [
        row
        for row in scoped_current_pallets
        if _pallet_has_projectable_physical_goods(row, positive_lots_by_id)
    ]
    all_lots_by_id = {int(row.id): row for row in lots}
    locations_by_id = {int(row.id): row for row in locations}
    pallet_move_issues = {
        int(pallet.id): pallet_move_source_issue(
            pallet,
            locations_by_id.get(pallet.location_id),
            lots_by_id=all_lots_by_id,
        )
        for pallet in visible_pallets
    }
    empty_current_pallet_residues = [
        row
        for row in scoped_current_pallets
        if not _pallet_has_any_physical_goods(row, positive_lots_by_id)
    ]
    visible_pallet_ids = {row.id for row in visible_pallets}
    projection_lots = [
        row
        for row in lots
        if visible_customer_ids is None
        or current_same_location_pallet(row) is None
        or current_same_location_pallet(row).id in visible_pallet_ids
    ]
    current_lots = [
        row
        for row in projection_lots
        if row.status in {"active", "frozen"} and _physical_quantity(row) > 0
    ]
    composite_projections = _parent_delivery_inventory_projections(db, current_lots)
    lots_by_location: dict[int, list[InventoryLot]] = defaultdict(list)
    for row in current_lots:
        lots_by_location[row.warehouse_location_id].append(row)

    pallets_by_location: dict[int, list[InventoryPallet]] = defaultdict(list)
    for row in sorted(visible_pallets, key=lambda item: item.id):
        if row.location_id is not None:
            pallets_by_location[row.location_id].append(row)
    visible_location_ids = set(lots_by_location) | set(pallets_by_location)
    discrepancies_by_location: dict[
        int, list[tuple[WarehouseLocationDiscrepancy, InventoryLot]]
    ] = defaultdict(list)
    for discrepancy in db.scalars(
        select(WarehouseLocationDiscrepancy)
        .where(WarehouseLocationDiscrepancy.status == "open")
        .order_by(
            WarehouseLocationDiscrepancy.reported_at,
            WarehouseLocationDiscrepancy.id,
        )
    ).all():
        lot = positive_lots_by_id.get(int(discrepancy.inventory_lot_id))
        if lot is None:
            continue
        business = _lot_business_fields(lot)
        if (
            visible_customer_ids is not None
            and business.get("customer_id") not in visible_customer_ids
        ):
            continue
        observed_location_id = int(discrepancy.observed_location_id)
        discrepancies_by_location[observed_location_id].append((discrepancy, lot))
        visible_location_ids.add(observed_location_id)
    floor_records = {row.floor_number: row for row in floors}
    policy_types_by_area: dict[tuple[int, str], list[str]] = {}
    policy_rows = db.execute(
        select(
            WarehouseFloor.floor_number,
            WarehouseArea.area_code,
            WarehouseAreaStoragePolicy.allowed_inventory_types_json,
            WarehouseAreaStoragePolicy.status,
            WarehouseAreaStoragePolicy.published_map_revision,
            WarehouseAreaStoragePolicy.map_feature_id,
        )
        .join(WarehouseArea, WarehouseArea.floor_id == WarehouseFloor.id)
        .join(
            WarehouseAreaStoragePolicy,
            WarehouseAreaStoragePolicy.area_id == WarehouseArea.id,
        )
    ).all()
    for (
        floor_number,
        area_code,
        raw_types,
        status,
        published_map_revision,
        map_feature_id,
    ) in policy_rows:
        key = (int(floor_number), str(area_code).upper())
        if status != "published":
            continue
        try:
            values = json.loads(raw_types)
        except (TypeError, ValueError, json.JSONDecodeError):
            values = []
        if isinstance(values, list):
            policy_types_by_area[key] = effective_inventory_usages([
                str(value) for value in values if isinstance(value, str) and value
            ])
    projection_contexts = load_warehouse_location_projection_contexts(db, locations)
    unmatched_by_location: dict[
        int, list[WarehouseUnmatchedInventoryObservation]
    ] = defaultdict(list)
    if visible_customer_ids is None:
        for observation in db.scalars(
            select(WarehouseUnmatchedInventoryObservation)
            .where(WarehouseUnmatchedInventoryObservation.status == "open")
            .order_by(
                WarehouseUnmatchedInventoryObservation.reported_at,
                WarehouseUnmatchedInventoryObservation.id,
            )
        ).all():
            unmatched_by_location[int(observation.observed_location_id)].append(
                observation
            )
    location_rows = []
    for location in locations:
        if visible_customer_ids is not None and location.id not in visible_location_ids:
            continue
        location_key = (
            int(location.warehouse_floor or 0),
            str(location.area_code or "").upper(),
        )
        projection_context = projection_contexts.get(int(location.id), {})
        payload = _location_payload(
            location,
            lots=lots_by_location.get(location.id, []),
            pallets=pallets_by_location.get(location.id, []),
            as_of=as_of,
            projection_context=projection_context,
            allowed_inventory_types=policy_types_by_area.get(location_key, []),
            composite_projections=composite_projections,
            stocktake_decrease_issues=stocktake_decrease_issues,
            pallet_move_issues=pallet_move_issues,
        )
        open_observations = unmatched_by_location.get(int(location.id), [])
        payload["has_unmatched_inventory_observation"] = bool(open_observations)
        payload["unmatched_inventory_observation_count"] = len(open_observations)
        payload["unmatched_inventory_observations"] = [
            {
                "id": int(observation.id),
                "version": int(observation.version),
                "customer_keyword": observation.customer_keyword,
                "inventory_keyword": observation.inventory_keyword,
                "reported_quantity": observation.reported_quantity,
                "reported_unit": observation.reported_unit,
                "reason": observation.reason,
                "reported_at": utc_naive_to_api(observation.reported_at),
            }
            for observation in open_observations
        ]
        open_discrepancies = discrepancies_by_location.get(int(location.id), [])
        payload["has_location_discrepancy"] = bool(open_discrepancies)
        payload["location_discrepancy_count"] = len(open_discrepancies)
        payload["location_discrepancies"] = [
            {
                "id": int(discrepancy.id),
                "version": int(discrepancy.version),
                "reported_quantity": int(discrepancy.reported_quantity),
                "reason": discrepancy.reason,
                "reported_at": utc_naive_to_api(discrepancy.reported_at),
                "registered_location_id": int(discrepancy.registered_location_id),
                "lot": _lot_payload(
                    lot,
                    as_of,
                    composite_projection=composite_projections.get(int(lot.id)),
                    stocktake_decrease_issues=stocktake_decrease_issues,
                ),
            }
            for discrepancy, lot in open_discrepancies
        ]
        location_rows.append(payload)

    floor_summaries = []
    dashboard_floor_numbers = [1, 3]
    if 4 in floor_records or any(row["floor_number"] == 4 for row in location_rows):
        dashboard_floor_numbers.append(4)
    for floor_number in dashboard_floor_numbers:
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
                pallet["pallet_id"]
                for row in occupied
                for pallet in row["pallets"]
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
        all_lots=projection_lots,
        current_lots=current_lots,
        days=days,
        as_of=as_of,
    )
    age_distribution = _age_distribution(current_lots, as_of)
    old_lot_ids = {
        row.id for row in current_lots if (_age_days(row, as_of) or 0) > 90
    }
    old_pallet_ids = {
        int(current_pallet.id)
        for row in current_lots
        if row.id in old_lot_ids
        and (current_pallet := current_same_location_pallet(row)) is not None
    }
    location_payload_by_id = {
        int(row["location_id"]): row
        for row in location_rows
        if row.get("location_id") is not None
    }
    finished_current_lots = [
        row
        for row in current_lots
        if row.inventory_type == "finished" and _physical_quantity(row) > 0
    ]
    unresolved_lots = [
        row
        for row in finished_current_lots
        if row.warehouse_location_id is None
        or location_payload_by_id.get(int(row.warehouse_location_id), {}).get(
            "position_status"
        )
        != "mapped"
    ]
    unexpectedly_unresolved_lots = [
        row for row in unresolved_lots
        if not is_pending_relocation_location(locations_by_id.get(row.warehouse_location_id))
    ]
    finished_lots_on_current_pallet = [
        row
        for row in finished_current_lots
        if current_same_location_pallet(row) is not None
    ]
    finished_lots_without_current_pallet = [
        row
        for row in finished_current_lots
        if current_same_location_pallet(row) is None
    ]
    mapped_lots_without_current_pallet = [
        row
        for row in finished_lots_without_current_pallet
        if row.warehouse_location_id is not None
        and location_payload_by_id.get(int(row.warehouse_location_id), {}).get(
            "position_status"
        )
        == "mapped"
        and location_payload_by_id.get(int(row.warehouse_location_id), {}).get(
            "storage_layout"
        )
        != "functional"
        and row.location is not None
        and row.location.address_kind != "functional"
    ]
    twin_ground_lots_on_current_pallet = [
        row
        for row in finished_lots_on_current_pallet
        if row.location is not None
        and str(row.location.source_version or "").strip().upper()
        in {"TWIN_V1", "CURRENT_MAP"}
        and row.location.address_kind != "functional"
        and location_payload_by_id.get(int(row.warehouse_location_id), {}).get(
            "storage_layout"
        )
        != "functional"
        and str(row.location.storage_type or "").strip().lower()
        in {"ground", "temporary_aisle"}
        and location_payload_by_id.get(int(row.warehouse_location_id), {}).get(
            "position_status"
        )
        == "mapped"
    ]
    twin_ground_pallet_ids = {
        int(current_same_location_pallet(row).id)
        for row in twin_ground_lots_on_current_pallet
        if current_same_location_pallet(row) is not None
    }
    active_occupancies_by_pallet: dict[int, list[WarehouseGroundOccupancy]] = (
        defaultdict(list)
    )
    if twin_ground_pallet_ids:
        for occupancy in db.scalars(
            select(WarehouseGroundOccupancy)
            .where(
                WarehouseGroundOccupancy.pallet_id.in_(twin_ground_pallet_ids),
                WarehouseGroundOccupancy.status == "active",
            )
            .options(selectinload(WarehouseGroundOccupancy.slots))
        ).all():
            active_occupancies_by_pallet[int(occupancy.pallet_id)].append(occupancy)
    twin_ground_without_active_occupancy = []
    twin_ground_occupancy_location_mismatch = []
    twin_ground_occupancy_capacity_insufficient = []
    twin_ground_valid_occupancy = []
    for row in twin_ground_lots_on_current_pallet:
        current_pallet = current_same_location_pallet(row)
        assert current_pallet is not None
        occupancies = active_occupancies_by_pallet.get(int(current_pallet.id), [])
        if not occupancies:
            twin_ground_without_active_occupancy.append(row)
            continue
        if len(occupancies) != 1:
            twin_ground_occupancy_location_mismatch.append(row)
            continue
        occupancy = occupancies[0]
        active_location_ids = {
            int(slot.location_id)
            for slot in occupancy.slots
            if slot.status == "active"
        }
        if (
            int(occupancy.primary_location_id) != int(row.warehouse_location_id)
            or int(row.warehouse_location_id) not in active_location_ids
        ):
            twin_ground_occupancy_location_mismatch.append(row)
            continue
        pallet_physical_quantity = _pallet_physical_quantity(
            current_pallet
        )
        if int(occupancy.capacity_quantity or 0) < pallet_physical_quantity:
            twin_ground_occupancy_capacity_insufficient.append(row)
            continue
        twin_ground_valid_occupancy.append(row)
    twin_ground_occupancy_issues = {
        int(row.id): row
        for row in [
            *twin_ground_without_active_occupancy,
            *twin_ground_occupancy_location_mismatch,
            *twin_ground_occupancy_capacity_insufficient,
        ]
    }
    unlocated_inventory = []
    unlocated_reason = {
        "disabled": "正式位置已停用",
        "unplaced": "正式位置尚未完成布局发布",
        "area_only": "只有区域台账，缺少已发布实测格位",
        "unlocated": "尚未绑定正式地图位置",
    }
    for row in unresolved_lots:
        location_payload = (
            location_payload_by_id.get(int(row.warehouse_location_id))
            if row.warehouse_location_id is not None
            else None
        )
        position_status = (
            location_payload.get("position_status")
            if location_payload is not None
            else "unlocated"
        )
        unlocated_inventory.append(
            {
                **_lot_payload(
                    row,
                    as_of,
                    composite_projection=composite_projections.get(int(row.id)),
                    stocktake_decrease_issues=stocktake_decrease_issues,
                ),
                "floor_code": (
                    location_payload.get("floor_code")
                    if location_payload is not None
                    else "UNLOCATED"
                ),
                "area_code": (
                    location_payload.get("area_code")
                    if location_payload is not None
                    else None
                ),
                "location_id": (
                    location_payload.get("location_id")
                    if location_payload is not None
                    else None
                ),
                "location_code": (
                    location_payload.get("location_code")
                    if location_payload is not None
                    else None
                ),
                "location_name": (
                    location_payload.get("employee_location_name")
                    if location_payload is not None
                    else "尚未绑定正式位置"
                ),
                "employee_location_name": (
                    location_payload.get("employee_location_name")
                    if location_payload is not None
                    else "尚未绑定正式位置"
                ),
                "position_status": position_status,
                "pending_relocation": is_pending_relocation_location(locations_by_id.get(row.warehouse_location_id)),
                "unlocated_reason": (
                    location_payload.get("map_issue")
                    if location_payload is not None
                    and location_payload.get("map_issue")
                    else unlocated_reason.get(
                        position_status,
                        "缺少已发布实测格位",
                    )
                ),
                "map_position": None,
            }
        )
    unresolved_lot_ids = {int(row.id) for row in unresolved_lots}
    finished_map_coverage = {
        "total_lots": len(finished_current_lots),
        "mapped_lots": len(finished_current_lots) - len(unresolved_lots),
        "unlocated_lots": len(unresolved_lots),
        "total_quantity": sum(_usable_quantity(row) for row in finished_current_lots),
        "mapped_quantity": sum(
            _usable_quantity(row)
            for row in finished_current_lots
            if int(row.id) not in unresolved_lot_ids
        ),
        "unlocated_quantity": sum(_usable_quantity(row) for row in unresolved_lots),
        "total_physical_quantity": sum(
            _physical_quantity(row) for row in finished_current_lots
        ),
        "mapped_physical_quantity": sum(
            _physical_quantity(row)
            for row in finished_current_lots
            if int(row.id) not in unresolved_lot_ids
        ),
        "unlocated_physical_quantity": sum(
            _physical_quantity(row) for row in unresolved_lots
        ),
        "all_located": not unresolved_lots,
    }
    finished_pallet_coverage = {
        "total_lots": len(finished_current_lots),
        "current_same_location_pallet_lots": len(
            finished_lots_on_current_pallet
        ),
        "without_current_same_location_pallet_lots": len(
            finished_lots_without_current_pallet
        ),
        "total_quantity": sum(
            _usable_quantity(row) for row in finished_current_lots
        ),
        "current_same_location_pallet_quantity": sum(
            _usable_quantity(row) for row in finished_lots_on_current_pallet
        ),
        "without_current_same_location_pallet_quantity": sum(
            _usable_quantity(row) for row in finished_lots_without_current_pallet
        ),
        "total_physical_quantity": sum(
            _physical_quantity(row) for row in finished_current_lots
        ),
        "current_same_location_pallet_physical_quantity": sum(
            _physical_quantity(row) for row in finished_lots_on_current_pallet
        ),
        "without_current_same_location_pallet_physical_quantity": sum(
            _physical_quantity(row) for row in finished_lots_without_current_pallet
        ),
        "all_on_current_same_location_pallet": not (
            finished_lots_without_current_pallet
        ),
        "conservation": {
            "lot_count": len(finished_current_lots)
            == len(finished_lots_on_current_pallet)
            + len(finished_lots_without_current_pallet),
            "quantity": sum(
                _usable_quantity(row) for row in finished_current_lots
            )
            == sum(
                _usable_quantity(row) for row in finished_lots_on_current_pallet
            )
            + sum(
                _usable_quantity(row)
                for row in finished_lots_without_current_pallet
            ),
            "physical_quantity": sum(
                _physical_quantity(row) for row in finished_current_lots
            )
            == sum(
                _physical_quantity(row)
                for row in finished_lots_on_current_pallet
            )
            + sum(
                _physical_quantity(row)
                for row in finished_lots_without_current_pallet
            ),
        },
    }
    mapped_location_without_current_pallet = {
        "lot_count": len(mapped_lots_without_current_pallet),
        "quantity": sum(
            _usable_quantity(row) for row in mapped_lots_without_current_pallet
        ),
        "physical_quantity": sum(
            _physical_quantity(row) for row in mapped_lots_without_current_pallet
        ),
    }
    twin_ground_occupancy_coverage = {
        "required_lots": len(twin_ground_lots_on_current_pallet),
        "valid_lots": len(twin_ground_valid_occupancy),
        "invalid_lots": len(twin_ground_occupancy_issues),
        "without_active_occupancy_lots": len(
            twin_ground_without_active_occupancy
        ),
        "location_or_pallet_mismatch_lots": len(
            twin_ground_occupancy_location_mismatch
        ),
        "capacity_insufficient_lots": len(
            twin_ground_occupancy_capacity_insufficient
        ),
        "required_physical_quantity": sum(
            _physical_quantity(row) for row in twin_ground_lots_on_current_pallet
        ),
        "valid_physical_quantity": sum(
            _physical_quantity(row) for row in twin_ground_valid_occupancy
        ),
        "invalid_physical_quantity": sum(
            _physical_quantity(row) for row in twin_ground_occupancy_issues.values()
        ),
        "all_valid": not twin_ground_occupancy_issues,
        "conservation": {
            "lot_count": len(twin_ground_lots_on_current_pallet)
            == len(twin_ground_valid_occupancy) + len(twin_ground_occupancy_issues),
            "physical_quantity": sum(
                _physical_quantity(row) for row in twin_ground_lots_on_current_pallet
            )
            == sum(_physical_quantity(row) for row in twin_ground_valid_occupancy)
            + sum(
                _physical_quantity(row)
                for row in twin_ground_occupancy_issues.values()
            ),
        },
    }
    visible_capacities = [
        floor["capacity"]
        for floor in floor_summaries
        if floor["capacity"]["visible"]
    ]
    capacities_confirmed = bool(visible_capacities) and all(
        row["confirmed"] for row in visible_capacities
    )
    if visible_customer_ids is None and not capacities_confirmed:
        for floor in floor_summaries:
            floor["capacity"] = suppress_capacity_metrics_until_all_confirmed(
                floor["capacity"]
            )
        visible_capacities = [floor["capacity"] for floor in floor_summaries]
    capacity_reference_total = sum(
        int(row["reference_pallet_capacity"] or 0) for row in visible_capacities
    ) if capacities_confirmed else 0
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
        pending_confirmation_floors = [
            floor for floor in floor_summaries if not floor["capacity"]["confirmed"]
        ]
        if pending_confirmation_floors:
            capacity_alerts.append(
                {
                    "code": "capacity_field_confirmation_pending",
                    "level": "warning",
                    "message": "现场安全容量尚未全部确认；规划值仅供整理参考，不显示满载率、空位或阈值告警。",
                }
            )
        for floor in floor_summaries:
            capacity = floor["capacity"]
            alert_level = capacity["alert_level"]
            if alert_level in {"unknown", "awaiting_confirmation"}:
                if alert_level == "awaiting_confirmation":
                    continue
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

    delayed_dispatch = _delayed_direct_dispatch_projection(
        db,
        pallets=visible_pallets,
        positive_lots_by_id=positive_lots_by_id,
        location_payloads=location_rows,
        as_of=as_of,
        idle_days=dispatch_idle_days,
        hide_empty_targets=visible_customer_ids is not None,
    )

    return {
        "schema_version": "P1-29-v1",
        "mode": "erp_business_twin",
        "read_only": True,
        "standard_pallet": standard_pallet_contract(),
        "generated_at": utc_naive_to_api(utc_now_naive()),
        "as_of_date": as_of.isoformat(),
        "days": days,
        "delayed_dispatch_relocation": delayed_dispatch,
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
            "finished_map_coverage": finished_map_coverage,
            "finished_pallet_coverage": finished_pallet_coverage,
            "mapped_location_without_current_pallet": (
                mapped_location_without_current_pallet
            ),
            "empty_current_pallet_residue_count": (
                len(empty_current_pallet_residues)
                if visible_customer_ids is None
                else None
            ),
            "twin_ground_occupancy_coverage": twin_ground_occupancy_coverage,
            "finished_projection_complete": bool(
                not unresolved_lots
                and not mapped_lots_without_current_pallet
                and not empty_current_pallet_residues
                and not twin_ground_occupancy_issues
            ),
            "temporary_occupied_locations": sum(
                1
                for row in location_rows
                if row["is_temporary"] and row["occupancy_status"] == "occupied"
            ),
            "capacity": (
                {
                    "visible": True,
                    "confirmed": capacities_confirmed,
                    "reference_pallet_capacity": capacity_reference_total or None,
                    "occupied_pallets": capacity_occupied_total,
                    "planned_pallet_capacity": sum(
                        int(floor["capacity"].get("planned_pallet_capacity") or 0)
                        for floor in floor_summaries if floor["capacity"]["visible"]
                    ),
                    "empty_pallet_slots": (
                        max(capacity_reference_total - capacity_occupied_total, 0)
                        if capacities_confirmed
                        else None
                    ),
                    "utilization_percent": (
                        round(capacity_occupied_total * 100 / capacity_reference_total, 1)
                        if capacities_confirmed and capacity_reference_total
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
        "unlocated_inventory": unlocated_inventory,
        "distribution": {
            "floors": floor_distribution,
            "areas": area_distribution,
        },
        "age_distribution": age_distribution,
        "trend": trend,
        "throughput": throughput,
        "alerts": [
            *(
                [
                    {
                        "code": "unmatched_physical_inventory_observation",
                        "level": "error",
                        "message": (
                            f"有 {sum(len(rows) for rows in unmatched_by_location.values())} 条现场有货但全仓实物库存未匹配的标记；"
                            "相关货位已标红，须由管理员核对后通过正式盘点或入库流程处理。"
                        ),
                    }
                ]
                if unmatched_by_location
                else []
            ),
            *capacity_alerts,
            *(
                [
                    {
                        "code": "twin_ground_without_valid_active_occupancy",
                        "level": "error",
                        "message": (
                            f"有 {len(twin_ground_occupancy_issues)} 个正数成品批次"
                            "位于当前 TWIN 地堆位置且已有真实栈板，但活动地堆占用"
                            "缺失、异位或容量不足；地图仍显示库存，同时阻断完整闭环。"
                        ),
                    }
                ]
                if twin_ground_occupancy_issues
                and visible_customer_ids is None
                else []
            ),
            *(
                [
                    {
                        "code": "mapped_location_without_current_pallet",
                        "level": "error",
                        "message": (
                            f"有 {len(mapped_lots_without_current_pallet)} 个正数成品批次"
                            "已有实测地图位置但缺少同库位当前真实栈板；"
                            "库存仍显示在原位置的 loose_items，须补齐栈板闭环。"
                        ),
                    }
                ]
                if mapped_lots_without_current_pallet
                and visible_customer_ids is None
                else []
            ),
            *(
                [
                    {
                        "code": "empty_current_pallet_residue",
                        "level": "error",
                        "message": (
                            f"有 {len(empty_current_pallet_residues)} 个 current 栈板"
                            "不含任何正数实物库存；已从产品占用中剔除，须核对送完释放。"
                        ),
                    }
                ]
                if empty_current_pallet_residues
                and visible_customer_ids is None
                else []
            ),
            *(
                [
                    {
                        "code": "unlocated_inventory",
                        "level": "error",
                        "message": f"有 {len(unexpectedly_unresolved_lots)} 个成品库存批次缺少已发布实测格位；已列入待定位清单，不会借用其他区域坐标。",
                    }
                ]
                if unexpectedly_unresolved_lots
                else []
            ),
        ],
    }


def build_inventory_code_search_results(
    *,
    lots: list[InventoryLot],
    keyword: str,
    as_of: date,
    location_projection_contexts: dict[int, dict] | None = None,
) -> dict:
    results = []
    floor_counts: dict[str, dict] = {}
    for row in lots:
        payload = _lot_payload(row, as_of)
        location = row.location
        location_context = (
            (location_projection_contexts or {}).get(int(location.id), {})
            if location is not None
            else {}
        )
        position_status, map_position = (
            _location_position(location, **location_context)
            if location is not None
            else ("unlocated", None)
        )
        address_payload = (
            location_address_payload(
                location,
                area=location_context.get("area"),
                floor=location_context.get("floor"),
                position_status=position_status,
                area_sequence=location_context.get("area_sequence"),
            )
            if location is not None
            else None
        )
        current_pallet = current_same_location_pallet(row)
        floor_code = _floor_key(location.warehouse_floor if location else None)
        result = {
            **payload,
            "floor_code": floor_code,
            "area_code": location.area_code if location else None,
            "location_id": location.id if location else None,
            "location_code": location.location_code if location else None,
            "location_name": (
                address_payload["employee_location_name"]
                if address_payload is not None
                else "尚未绑定正式位置"
            ),
            "employee_location_name": (
                address_payload["employee_location_name"]
                if address_payload is not None
                else "尚未绑定正式位置"
            ),
            "current_address_code": (
                address_payload["current_address_code"]
                if address_payload is not None
                else None
            ),
            "position_status": position_status,
            "map_position": map_position,
            "pallet_id": current_pallet.id if current_pallet is not None else None,
            "pallet_code": (
                current_pallet.pallet_code if current_pallet is not None else None
            ),
            "pallet_projection_status": (
                "current_same_location"
                if current_pallet is not None
                else "missing_current_pallet"
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
    def normalized(value: object) -> str:
        return (
            "".join(str(value or "").split())
            .replace("X", "×")
            .replace("x", "×")
            .replace("*", "×")
            .replace("毫米", "")
            .replace("mm", "")
            .casefold()
        )

    needle = normalized(keyword)
    if not needle:
        return False
    payload = _lot_payload(row, as_of)
    location = row.location
    pallet = current_same_location_pallet(row)
    finished_customer_code = (
        row.finished_detail.customer.customer_code
        if row.finished_detail is not None and row.finished_detail.customer is not None
        else None
    )
    semi_finished_customer_code = (
        row.semi_finished_detail.customer.customer_code
        if row.semi_finished_detail is not None
        and row.semi_finished_detail.customer is not None
        else None
    )
    searchable = " ".join(
        str(value or "")
        for value in (
            payload.get("inventory_code"),
            payload.get("product_name"),
            payload.get("customer_name"),
            payload.get("customer_short_name"),
            *(f"{binding.product.product_code} {binding.product.product_name} {binding.product.customer_material_code or ''}"
              for binding in row.allowed_products if binding.product is not None),
            finished_customer_code,
            semi_finished_customer_code,
            payload.get("specification"),
            payload.get("material"),
            payload.get("lot_number"),
            location.location_code if location else None,
            location.location_name if location else None,
            employee_location_name(location) if location else None,
            *(
                alias.alias_text
                for alias in (location.address_aliases if location else [])
            ),
            location.area_code if location else None,
            pallet.pallet_code if pallet else None,
        )
    )
    return needle in normalized(searchable)
