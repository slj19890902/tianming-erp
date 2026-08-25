from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models.delivery import Delivery
from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem
from app.models.order import Order, OrderItem
from app.models.production import ProductionTask
from app.models.supplier_requisition_order import (
    SupplierRequisitionOrder,
    SupplierRequisitionOrderItem,
)
from app.models.warehouse_capacity import WarehouseCapacityForecastPlan
from app.models.warehouse_inventory import (
    InventoryPallet,
    WarehouseFloor,
    WarehouseLocation,
)
from app.services.warehouse_twin_dashboard import warehouse_capacity_summary
from app.services.receipt_managed_production import receipt_managed_order_item_ids
from app.services.location_candidates import pallet_has_physical_goods_condition


SOURCE_LABELS = {
    "supplier_requisition": "供应商报料单",
    "production_task": "生产任务",
    "delivery": "送货单",
}
ALLOWED_EFFECTS = {
    "supplier_requisition": {"inflow", "no_storage"},
    "production_task": {"inflow", "no_storage"},
    "delivery": {"outflow"},
}


def _as_date(value: date | datetime | None) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    return value


def _posted_supplier_receipts_by_item(
    db: Session, supplier_item_ids: list[int]
) -> dict[int, tuple[int, bool]]:
    normalized_ids = sorted({int(item_id) for item_id in supplier_item_ids})
    if not normalized_ids:
        return {}
    balances: dict[int, tuple[int, bool]] = {}
    for offset in range(0, len(normalized_ids), 500):
        item_id_chunk = normalized_ids[offset : offset + 500]
        balances.update(
            {
                int(item_id): (int(received_quantity or 0), bool(accepted_short))
                for item_id, received_quantity, accepted_short in db.execute(
                    select(
                        IncomingReceiptItem.supplier_order_item_id,
                        func.coalesce(
                            func.sum(IncomingReceiptItem.received_quantity), 0
                        ),
                        func.max(
                            case(
                                (
                                    IncomingReceiptItem.resolution_action
                                    == "accept_short",
                                    1,
                                ),
                                else_=0,
                            )
                        ),
                    )
                    .join(
                        IncomingReceipt,
                        IncomingReceipt.id == IncomingReceiptItem.receipt_id,
                    )
                    .where(
                        IncomingReceiptItem.supplier_order_item_id.in_(
                            item_id_chunk
                        ),
                        IncomingReceiptItem.status == "posted",
                        IncomingReceipt.status == "posted",
                    )
                    .group_by(IncomingReceiptItem.supplier_order_item_id)
                )
                if item_id is not None
            }
        )
    return balances


def _legacy_completed_supplier_item_ids(
    db: Session,
    supplier_item_ids: list[int],
    received_by_item: dict[int, tuple[int, bool]],
) -> set[int]:
    """Return old-workflow receipt completions without inventing receipt facts.

    The modern incoming receipt ledger is authoritative as soon as it contains
    any posted quantity for a supplier line.  Only lines with zero modern posted
    quantity may fall back to the three matching legacy completion fields on the
    linked order item.
    """

    candidate_ids = sorted(
        {
            int(item_id)
            for item_id in supplier_item_ids
            if max(int(received_by_item.get(int(item_id), (0, False))[0]), 0) == 0
        }
    )
    completed: set[int] = set()
    for offset in range(0, len(candidate_ids), 500):
        item_id_chunk = candidate_ids[offset : offset + 500]
        completed.update(
            int(item_id)
            for item_id in db.scalars(
                select(SupplierRequisitionOrderItem.id)
                .join(
                    OrderItem,
                    OrderItem.id == SupplierRequisitionOrderItem.order_item_id,
                )
                .where(
                    SupplierRequisitionOrderItem.id.in_(item_id_chunk),
                    OrderItem.material_status == "received",
                    OrderItem.requisition_status == "已入库",
                    OrderItem.material_received_at.is_not(None),
                )
            ).all()
        )
    return completed


def _supplier_source(
    row: SupplierRequisitionOrder,
    received_by_item: dict[int, tuple[int, bool]] | None = None,
    legacy_completed_item_ids: set[int] | None = None,
) -> dict[str, Any]:
    received_by_item = received_by_item or {}
    legacy_completed_item_ids = legacy_completed_item_ids or set()
    item_balances = [
        (
            item,
            # Only the quantity actually placed with the supplier can arrive.
            # `quantity` is the pre-stock-deduction production demand.
            max(int(item.requisition_qty or 0), 0),
            max(int(received_by_item.get(int(item.id), (0, False))[0]), 0),
            bool(received_by_item.get(int(item.id), (0, False))[1]),
        )
        for item in row.items
        if item.id is not None
    ]
    if item_balances:
        planned_quantity = sum(
            planned for _item, planned, _received, _accepted_short in item_balances
        )
        posted_received_quantity = sum(
            received
            for _item, _planned, received, _accepted_short in item_balances
        )
        received_quantity = sum(
            min(planned, received)
            for _item, planned, received, _accepted_short in item_balances
        )
        legacy_completed_quantity = sum(
            planned
            for item, planned, _received, _accepted_short in item_balances
            if int(item.id) in legacy_completed_item_ids
        )
        remaining_quantity = sum(
            (
                0
                if accepted_short or int(item.id) in legacy_completed_item_ids
                else max(planned - received, 0)
            )
            for item, planned, received, accepted_short in item_balances
        )
        dates = [
            item.delivery_date
            for item, planned, received, accepted_short in item_balances
            if not accepted_short
            and int(item.id) not in legacy_completed_item_ids
            and planned > received
            and item.delivery_date
        ]
    else:
        # Historical/header-only rows have no line that a receipt fact can safely
        # target. Use the header's actual supplier quantity without guessing a
        # receipt-to-line match.
        planned_quantity = max(int(row.requisition_qty or 0), 0)
        posted_received_quantity = 0
        received_quantity = 0
        legacy_completed_quantity = 0
        remaining_quantity = planned_quantity
        dates = []
    reference_date = min(dates) if dates else None
    legacy_completion_label = (
        f"旧流程已入库 {legacy_completed_quantity} 张"
        if legacy_completed_quantity > 0
        else None
    )
    source_label = f"{row.supplier_name or '供应商待补'} · {remaining_quantity} 张"
    if legacy_completion_label:
        source_label = f"{source_label} · {legacy_completion_label}"
    return {
        "source_type": "supplier_requisition",
        "source_id": row.id,
        "source_number": row.order_number,
        "source_label": source_label,
        "reference_date": reference_date,
        "reference_label": "客户交期参考" if reference_date else "没有可靠到料日期",
        "created_date": _as_date(row.created_at),
        "suggested_effect": "inflow",
        "planned_quantity": planned_quantity,
        "posted_received_quantity": posted_received_quantity,
        "received_quantity": received_quantity,
        "legacy_completed": legacy_completed_quantity > 0,
        "legacy_completed_quantity": legacy_completed_quantity,
        "legacy_completion_label": legacy_completion_label,
        "remaining_quantity": remaining_quantity,
        "valid": row.status == "confirmed" and remaining_quantity > 0,
    }


def _production_source(
    row: ProductionTask,
    order: Order | None,
    *,
    receipt_purpose_managed: bool = False,
) -> dict[str, Any]:
    reference_date = order.delivery_date if order is not None else None
    order_number = order.order_number if order is not None else f"任务{row.id}"
    return {
        "source_type": "production_task",
        "source_id": row.id,
        "source_number": order_number,
        "source_label": f"生产 {row.planned_quantity} 个 · 任务#{row.id}",
        "reference_date": reference_date,
        "reference_label": "客户交期参考" if reference_date else "没有可靠完工日期",
        "created_date": _as_date(row.created_at),
        "suggested_effect": "inflow",
        "valid": (
            not receipt_purpose_managed
            and row.status == "pending"
            and int(row.planned_quantity or 0) > 0
        ),
        "invalid_reason": (
            "receipt_auto_managed" if receipt_purpose_managed else None
        ),
    }


def _delivery_source(row: Delivery) -> dict[str, Any]:
    return {
        "source_type": "delivery",
        "source_id": row.id,
        "source_number": row.delivery_number,
        "source_label": f"待发货 {row.total_quantity} 个",
        "reference_date": row.delivery_date,
        "reference_label": "送货日期",
        "created_date": _as_date(row.created_at),
        "suggested_effect": "outflow",
        "valid": row.status == "pending" and int(row.total_quantity or 0) > 0,
    }


def resolve_capacity_forecast_source(
    db: Session, source_type: str, source_id: int
) -> dict[str, Any] | None:
    if source_type == "supplier_requisition":
        row = db.scalar(
            select(SupplierRequisitionOrder)
            .options(selectinload(SupplierRequisitionOrder.items))
            .where(SupplierRequisitionOrder.id == source_id)
        )
        if row is None:
            return None
        supplier_item_ids = [
            int(item.id) for item in row.items if item.id is not None
        ]
        received_by_item = _posted_supplier_receipts_by_item(
            db, supplier_item_ids
        )
        legacy_completed_item_ids = _legacy_completed_supplier_item_ids(
            db, supplier_item_ids, received_by_item
        )
        return _supplier_source(
            row,
            received_by_item,
            legacy_completed_item_ids,
        )
    if source_type == "production_task":
        result = db.execute(
            select(ProductionTask, Order)
            .join(OrderItem, OrderItem.id == ProductionTask.order_item_id)
            .join(Order, Order.id == OrderItem.order_id)
            .where(ProductionTask.id == source_id)
        ).first()
        if result is None:
            return None
        task, order = result
        receipt_purpose_managed = bool(
            receipt_managed_order_item_ids(db, [int(task.order_item_id)])
        )
        return _production_source(
            task,
            order,
            receipt_purpose_managed=receipt_purpose_managed,
        )
    if source_type == "delivery":
        row = db.get(Delivery, source_id)
        return _delivery_source(row) if row is not None else None
    return None


def _candidate_sources(db: Session, *, as_of: date, horizon: int) -> list[dict[str, Any]]:
    cutoff = as_of + timedelta(days=horizon)
    recent = as_of - timedelta(days=30)
    sources: list[dict[str, Any]] = []

    supplier_rows = list(
        db.scalars(
            select(SupplierRequisitionOrder)
            .options(selectinload(SupplierRequisitionOrder.items))
            .where(
                SupplierRequisitionOrder.status == "confirmed",
                or_(
                    SupplierRequisitionOrder.created_at.is_(None),
                    SupplierRequisitionOrder.created_at
                    >= datetime.combine(recent, datetime.min.time()),
                    SupplierRequisitionOrder.items.any(
                        and_(
                            SupplierRequisitionOrderItem.delivery_date >= recent,
                            SupplierRequisitionOrderItem.delivery_date <= cutoff,
                        )
                    ),
                ),
            )
            .order_by(SupplierRequisitionOrder.id.desc())
        ).all()
    )
    supplier_item_ids = [
        int(item.id)
        for row in supplier_rows
        for item in row.items
        if item.id is not None
    ]
    received_by_item = _posted_supplier_receipts_by_item(db, supplier_item_ids)
    legacy_completed_item_ids = _legacy_completed_supplier_item_ids(
        db, supplier_item_ids, received_by_item
    )
    sources.extend(
        _supplier_source(row, received_by_item, legacy_completed_item_ids)
        for row in supplier_rows
    )

    production_rows = db.execute(
        select(ProductionTask, Order)
        .join(OrderItem, OrderItem.id == ProductionTask.order_item_id)
        .join(Order, Order.id == OrderItem.order_id)
        .where(ProductionTask.status == "pending", ProductionTask.planned_quantity > 0)
        .order_by(ProductionTask.id.desc())
        .limit(100)
    ).all()
    managed_order_item_ids = receipt_managed_order_item_ids(
        db,
        [int(task.order_item_id) for task, _order in production_rows],
    )
    sources.extend(
        _production_source(
            task,
            order,
            receipt_purpose_managed=int(task.order_item_id)
            in managed_order_item_ids,
        )
        for task, order in production_rows
    )

    delivery_rows = db.scalars(
        select(Delivery)
        .where(Delivery.status == "pending", Delivery.total_quantity > 0)
        .order_by(Delivery.id.desc())
        .limit(100)
    ).all()
    sources.extend(_delivery_source(row) for row in delivery_rows)

    return [
        source
        for source in sources
        if source["valid"]
        and (
            source["reference_date"] is None
            and (source["created_date"] is None or source["created_date"] >= recent)
            or source["reference_date"] is not None
            and recent <= source["reference_date"] <= cutoff
        )
    ]


def _occupied_by_floor(db: Session) -> dict[int, int]:
    formal_location = or_(
        WarehouseLocation.source_version.is_(None),
        WarehouseLocation.source_version != "V11",
        and_(
            WarehouseLocation.source_version == "V11",
            WarehouseLocation.warehouse_floor == 3,
        ),
    )
    return {
        int(floor_number): int(count or 0)
        for floor_number, count in db.execute(
            select(WarehouseLocation.warehouse_floor, func.count(InventoryPallet.id))
            .join(InventoryPallet, InventoryPallet.location_id == WarehouseLocation.id)
            .where(
                InventoryPallet.is_current.is_(True),
                InventoryPallet.status == "active",
                pallet_has_physical_goods_condition(InventoryPallet.id),
                formal_location,
                WarehouseLocation.warehouse_floor.in_((1, 3)),
            )
            .group_by(WarehouseLocation.warehouse_floor)
        ).all()
        if floor_number is not None
    }


def serialize_capacity_forecast_plan(
    plan: WarehouseCapacityForecastPlan,
    *,
    floor: WarehouseFloor | None = None,
    source_valid: bool | None = None,
    source_snapshot_current: bool | None = None,
    stale_reason: str | None = None,
) -> dict[str, Any]:
    return {
        "id": plan.id,
        "source_type": plan.source_type,
        "source_type_label": SOURCE_LABELS.get(plan.source_type, plan.source_type),
        "source_id": plan.source_id,
        "source_number": plan.source_number_snapshot,
        "source_label": plan.source_label_snapshot,
        "effect": plan.effect,
        "floor_id": plan.floor_id,
        "floor_code": floor.floor_code if floor is not None else None,
        "floor_name": floor.floor_name if floor is not None else None,
        "planned_date": plan.planned_date.isoformat(),
        "pallet_slots": plan.pallet_slots,
        "status": plan.status,
        "version": plan.version,
        "source_valid": source_valid,
        "source_snapshot_current": source_snapshot_current,
        "stale_reason": stale_reason,
        "confidence": "operator_confirmed",
        "updated_at": plan.updated_at.isoformat() if plan.updated_at else None,
    }


def build_warehouse_capacity_forecast(
    db: Session,
    *,
    horizon: int = 7,
    as_of: date,
) -> dict[str, Any]:
    if horizon not in {7, 14, 30}:
        raise ValueError("容量预测仅支持 7、14、30 天")
    cutoff = as_of + timedelta(days=horizon)
    floors = list(
        db.scalars(
            select(WarehouseFloor)
            .options(selectinload(WarehouseFloor.areas))
            .where(WarehouseFloor.floor_number.in_((1, 3)))
            .order_by(WarehouseFloor.floor_number)
        ).all()
    )
    floor_by_id = {floor.id: floor for floor in floors}
    occupied = _occupied_by_floor(db)
    capacity_rows = {
        floor.id: warehouse_capacity_summary(
            floor,
            occupied_pallets=occupied.get(floor.floor_number, 0),
            visible=True,
        )
        for floor in floors
    }
    plans = list(
        db.scalars(
            select(WarehouseCapacityForecastPlan)
            .where(
                WarehouseCapacityForecastPlan.status == "active",
                WarehouseCapacityForecastPlan.planned_date <= cutoff,
            )
            .order_by(
                WarehouseCapacityForecastPlan.planned_date,
                WarehouseCapacityForecastPlan.id,
            )
        ).all()
    )
    candidates = _candidate_sources(db, as_of=as_of, horizon=horizon)
    source_cache = {
        (source["source_type"], source["source_id"]): source for source in candidates
    }
    missing_production_source_ids = {
        int(plan.source_id)
        for plan in plans
        if plan.source_type == "production_task"
        and (plan.source_type, int(plan.source_id)) not in source_cache
    }
    if missing_production_source_ids:
        saved_production_rows = db.execute(
            select(ProductionTask, Order)
            .join(OrderItem, OrderItem.id == ProductionTask.order_item_id)
            .join(Order, Order.id == OrderItem.order_id)
            .where(ProductionTask.id.in_(missing_production_source_ids))
        ).all()
        saved_managed_order_item_ids = receipt_managed_order_item_ids(
            db,
            [int(task.order_item_id) for task, _order in saved_production_rows],
        )
        source_cache.update(
            {
                ("production_task", int(task.id)): _production_source(
                    task,
                    order,
                    receipt_purpose_managed=(
                        int(task.order_item_id) in saved_managed_order_item_ids
                    ),
                )
                for task, order in saved_production_rows
            }
        )
    plan_keys: set[tuple[str, int]] = set()
    valid_plans: list[WarehouseCapacityForecastPlan] = []
    stale_plans: list[dict[str, Any]] = []
    serialized_plans: list[dict[str, Any]] = []
    for plan in plans:
        key = (plan.source_type, plan.source_id)
        source = source_cache.get(key) or resolve_capacity_forecast_source(
            db, plan.source_type, plan.source_id
        )
        source_valid = bool(source and source["valid"])
        source_snapshot_current = bool(
            source_valid
            and (
                plan.source_type != "supplier_requisition"
                or plan.source_label_snapshot == source["source_label"]
            )
        )
        stale_reason = (
            None
            if source_snapshot_current
            else (
                "supplier_remaining_quantity_changed"
                if source_valid and plan.source_type == "supplier_requisition"
                else "source_invalid"
            )
        )
        serialized_plans.append(
            serialize_capacity_forecast_plan(
                plan,
                floor=floor_by_id.get(plan.floor_id),
                source_valid=source_valid,
                source_snapshot_current=source_snapshot_current,
                stale_reason=stale_reason,
            )
        )
        if not source_snapshot_current:
            stale_plans.append(serialized_plans[-1])
            continue
        plan_keys.add(key)
        valid_plans.append(plan)

    missing_sources = [
        {
            **source,
            "source_type_label": SOURCE_LABELS.get(
                source["source_type"], source["source_type"]
            ),
            "confidence": "missing_pallet_conversion",
            "reference_date": (
                source["reference_date"].isoformat()
                if source["reference_date"] is not None
                else None
            ),
            "created_date": (
                source["created_date"].isoformat()
                if source["created_date"] is not None
                else None
            ),
        }
        for source in candidates
        if (source["source_type"], source["source_id"]) not in plan_keys
    ]

    daily_delta: dict[int, dict[date, int]] = defaultdict(lambda: defaultdict(int))
    for plan in valid_plans:
        if plan.floor_id is None or plan.effect == "no_storage":
            continue
        change = plan.pallet_slots if plan.effect == "inflow" else -plan.pallet_slots
        # An overdue but still-valid plan is shown as due today.  Ignoring it
        # would make the projection look artificially complete and low.
        effective_date = max(plan.planned_date, as_of)
        daily_delta[plan.floor_id][effective_date] += change

    floor_items = []
    threshold_actions = []
    negative_projection = False
    for floor in floors:
        capacity = capacity_rows[floor.id]
        current = int(capacity["occupied_pallets"] or 0)
        running = current
        daily = []
        peak = current
        first_crossing: dict[int, str] = {}
        for offset in range(0, horizon + 1):
            day = as_of + timedelta(days=offset)
            running += daily_delta[floor.id].get(day, 0)
            if running < 0:
                negative_projection = True
            visible_occupied = max(running, 0)
            peak = max(peak, visible_occupied)
            reference = capacity["reference_pallet_capacity"]
            utilization = (
                round(visible_occupied * 100 / reference, 1) if reference else None
            )
            if utilization is not None:
                for threshold in (80, 90, 95):
                    if utilization >= threshold and threshold not in first_crossing:
                        first_crossing[threshold] = day.isoformat()
            daily.append(
                {
                    "date": day.isoformat(),
                    "occupied_pallets": visible_occupied,
                    "net_change": daily_delta[floor.id].get(day, 0),
                    "utilization_percent": utilization,
                }
            )
        reference = capacity["reference_pallet_capacity"]
        peak_utilization = round(peak * 100 / reference, 1) if reference else None
        floor_items.append(
            {
                "floor_id": floor.id,
                "floor_code": floor.floor_code,
                "floor_name": floor.floor_name,
                "current_occupied": current,
                "reference_capacity": reference,
                "capacity_basis": capacity["basis"],
                "capacity_confirmed": capacity["confirmed"],
                "peak_occupied": peak,
                "peak_utilization_percent": peak_utilization,
                "threshold_crossings": first_crossing,
                "daily": daily,
            }
        )
        if first_crossing:
            highest = max(first_crossing)
            threshold_actions.append(
                {
                    "code": "capacity_threshold_crossing",
                    "level": "error" if highest >= 95 else "warning",
                    "message": (
                        f"{floor.floor_code} 预计 {first_crossing[highest]} 达到 "
                        f"{highest}% 容量，请先安排送货、合并零散货或调整楼层。"
                    ),
                }
            )

    planning_basis = any(not item["capacity_confirmed"] for item in floor_items)
    complete = not (planning_basis or missing_sources or stale_plans or negative_projection)
    actions = []
    if missing_sources:
        actions.append(
            {
                "code": "missing_forecast_source",
                "level": "warning",
                "message": f"有 {len(missing_sources)} 张近期单据缺少预计占用栈板位，请补齐后再看预测。",
            }
        )
    if stale_plans:
        actions.append(
            {
                "code": "stale_forecast_plan",
                "level": "warning",
                "message": (
                    f"有 {len(stale_plans)} 条预测对应单据已失效或待收数量已变化，"
                    "已停止计入，请按当前剩余数量重新确认。"
                ),
            }
        )
    if planning_basis:
        actions.append(
            {
                "code": "planning_capacity_basis",
                "level": "warning",
                "message": "现场安全容量尚未全部复核，当前峰值仍按规划容量显示。",
            }
        )
    if negative_projection:
        actions.append(
            {
                "code": "negative_projection",
                "level": "warning",
                "message": "部分出库计划大于预测占用，相关楼层最低按 0 显示，请核对栈板位。",
            }
        )
    actions.extend(threshold_actions)
    return {
        "visible": True,
        "as_of": as_of.isoformat(),
        "horizon_days": horizon,
        "forecast_complete": complete,
        "forecast_status_label": "预测完整" if complete else "预测不完整",
        "notice": (
            "只按已确认的栈板位计划预测，不会把产品数量或纸板张数猜成栈板数，"
            "也不会自动移动、入库或发货。"
        ),
        "floors": floor_items,
        "plans": serialized_plans,
        "missing_sources": missing_sources,
        "stale_plans": stale_plans,
        "actions": actions,
    }
