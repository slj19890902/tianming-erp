"""N039 composite BOM execution domain service.

This module is deliberately independent from API handlers.  It only operates
on component snapshots, their append-only adjustments and the new snapshot
foreign keys.  A caller can therefore keep the existing single-product and A3
flows unchanged by simply not calling these functions when an order item has
no BOM snapshots.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from math import ceil, floor
from typing import Iterable

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.models.delivery import DeliveryItem
from app.models.order import OrderItem
from app.models.product import Product
from app.models.product_bom import (
    BomComponentDirectDeliveryAllocation,
    SalesOrderItemBomComponent,
    SalesOrderItemBomDemandAdjustment,
)
from app.models.production import (
    ProductionCompletion,
    ProductionStockTransfer,
    ProductionTask,
)
from app.models.warehouse_inventory import (
    DeliveryInventoryAllocation,
    InventoryLot,
    InventoryReservation,
)
from app.services.production_label_strategy import (
    CURRENT_PRODUCTION_LABEL_TEMPLATE_VERSION,
    ProductionLabelStrategyError,
    build_new_task_production_label_snapshot,
)
from app.services.warehouse_inventory import (
    WarehouseInventoryError,
    _balances,
    _claim_inventory_destination,
    _movement,
    utc_now_naive,
)


DIRECT_DISPOSITION = "direct"  # existing database/API value; means direct kit.
STOCK_DISPOSITION = "stock"
ACTIVE_RESERVATION_STATUSES = ("active", "partial")


class CompositeBomWorkflowError(ValueError):
    """Business-safe failure for the caller to render as a 4xx response."""


@dataclass(frozen=True)
class ComponentDemand:
    snapshot_id: int
    order_item_id: int
    component_product_id: int
    component_code: str
    component_name: str
    specification: str | None
    quantity_per_set: int
    is_required: bool
    effective_sets: int
    required_piece_quantity: int
    # Keep this trailing default for compatibility with older internal callers
    # that construct ComponentDemand positionally.
    show_on_delivery: bool = True


@dataclass(frozen=True)
class ComponentAvailability:
    snapshot_id: int
    component_code: str
    component_name: str
    quantity_per_set: int
    is_required: bool
    stock_quantity: int
    direct_quantity: int
    available_quantity: int
    required_piece_quantity: int


@dataclass(frozen=True)
class ConsumptionPart:
    source: str
    snapshot_id: int
    source_id: int
    quantity: int


@dataclass(frozen=True)
class ComponentConsumption:
    snapshot_id: int
    component_code: str
    component_name: str
    required_quantity: int
    parts: tuple[ConsumptionPart, ...]


def _as_positive_integer(value: object, *, field: str) -> int:
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise CompositeBomWorkflowError(f"{field}必须为正整数") from exc
    if decimal_value <= 0 or decimal_value != decimal_value.to_integral_value():
        raise CompositeBomWorkflowError(f"{field}必须为正整数")
    return int(decimal_value)


def _as_integer(value: object, *, field: str) -> int:
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise CompositeBomWorkflowError(f"{field}必须为整数") from exc
    if decimal_value != decimal_value.to_integral_value():
        raise CompositeBomWorkflowError(f"{field}必须为整数")
    return int(decimal_value)


def _snapshot_rows(db: Session, order_item_id: int) -> list[SalesOrderItemBomComponent]:
    return list(
        db.scalars(
            select(SalesOrderItemBomComponent)
            .where(SalesOrderItemBomComponent.sales_order_item_id == order_item_id)
            .order_by(SalesOrderItemBomComponent.display_order, SalesOrderItemBomComponent.id)
        ).all()
    )


def _remaining_reservation_quantity(reservation: InventoryReservation) -> int:
    return max(
        int(reservation.reserved_stock_quantity or 0)
        - int(reservation.consumed_stock_quantity or 0)
        - int(reservation.released_stock_quantity or 0),
        0,
    )


def _reservation_status(reservation: InventoryReservation) -> str:
    consumed = int(reservation.consumed_stock_quantity or 0)
    released = int(reservation.released_stock_quantity or 0)
    reserved = int(reservation.reserved_stock_quantity or 0)
    if consumed + released >= reserved:
        return "consumed" if consumed else "released"
    return "partial" if consumed or released else "active"


def _adjustment_totals(db: Session, snapshot_id: int) -> tuple[int, int]:
    row = db.execute(
        select(
            func.coalesce(
                func.sum(
                    SalesOrderItemBomDemandAdjustment.delta_order_set_quantity
                ),
                0,
            ),
            func.coalesce(
                func.sum(
                    SalesOrderItemBomDemandAdjustment.delta_required_piece_quantity
                ),
                0,
            ),
        ).where(
            SalesOrderItemBomDemandAdjustment.sales_order_item_bom_component_id
            == snapshot_id
        )
    ).one()
    return int(row[0] or 0), int(row[1] or 0)


def is_composite_order_item(db: Session, order_item_id: int) -> bool:
    """Return whether N039 applies, without changing ordinary/A3 paths."""
    return bool(_snapshot_rows(db, order_item_id))


def effective_component_demands(
    db: Session,
    order_item_id: int,
) -> list[ComponentDemand]:
    """Read immutable snapshots plus append-only set and piece adjustments."""
    rows = _snapshot_rows(db, order_item_id)
    result: list[ComponentDemand] = []
    for snapshot in rows:
        per_set = _as_positive_integer(snapshot.quantity_per_set, field="每套组件数量")
        delta_sets, delta_pieces = _adjustment_totals(db, snapshot.id)
        effective_sets = int(snapshot.order_set_quantity) + delta_sets
        if effective_sets < 0:
            raise CompositeBomWorkflowError("组件调整后的有效套数不能小于0")
        required_piece_quantity = (
            _as_positive_integer(
                snapshot.required_piece_quantity,
                field="组件需求件数",
            )
            + delta_pieces
        )
        if required_piece_quantity <= 0:
            raise CompositeBomWorkflowError("组件调整后的需求件数必须大于0")
        result.append(
            ComponentDemand(
                snapshot_id=snapshot.id,
                order_item_id=snapshot.sales_order_item_id,
                component_product_id=snapshot.component_product_id,
                component_code=snapshot.snapshot_component_product_code,
                component_name=snapshot.snapshot_component_product_name,
                specification=snapshot.snapshot_component_spec,
                quantity_per_set=per_set,
                is_required=bool(snapshot.is_required),
                show_on_delivery=bool(
                    getattr(snapshot, "show_on_delivery", True)
                ),
                effective_sets=effective_sets,
                required_piece_quantity=required_piece_quantity,
            )
        )
    return result


def append_order_quantity_adjustments(
    db: Session,
    *,
    order_item_id: int,
    delta_sets: int,
    reason: str,
    actor_id: int | None,
    idempotency_key: str,
    event_type: str = "order_quantity_adjusted",
    minimum_effective_sets: int = 0,
) -> list[SalesOrderItemBomDemandAdjustment]:
    """Append one immutable adjustment per snapshot, never edit a snapshot."""
    delta = _as_integer(delta_sets, field="订单套数调整")
    if delta == 0:
        raise CompositeBomWorkflowError("订单套数调整不能为0")
    if not reason or not reason.strip():
        raise CompositeBomWorkflowError("订单数量调整必须填写原因")
    if not idempotency_key or not idempotency_key.strip():
        raise CompositeBomWorkflowError("订单数量调整缺少幂等标识")

    demands = effective_component_demands(db, order_item_id)
    if not demands:
        return []
    created: list[SalesOrderItemBomDemandAdjustment] = []
    for demand in demands:
        next_sets = demand.effective_sets + delta
        if next_sets < minimum_effective_sets:
            raise CompositeBomWorkflowError("调整后套数不能小于已处理套数")
        row_key = f"{idempotency_key}:bom:{demand.snapshot_id}"
        existing = db.scalar(
            select(SalesOrderItemBomDemandAdjustment).where(
                SalesOrderItemBomDemandAdjustment.idempotency_key == row_key
            )
        )
        if existing is not None:
            if (
                int(existing.delta_order_set_quantity) != delta
                or int(existing.delta_required_piece_quantity)
                != delta * demand.quantity_per_set
            ):
                raise CompositeBomWorkflowError("订单数量调整幂等标识已被其他变更使用")
            created.append(existing)
            continue
        row = SalesOrderItemBomDemandAdjustment(
            sales_order_item_bom_component_id=demand.snapshot_id,
            event_type=event_type,
            delta_order_set_quantity=delta,
            delta_required_piece_quantity=delta * demand.quantity_per_set,
            reason=reason.strip(),
            actor_id=actor_id,
            idempotency_key=row_key,
        )
        db.add(row)
        created.append(row)
    db.flush()
    return created


def append_component_demand_adjustment(
    db: Session,
    *,
    order_item_id: int,
    snapshot_id: int,
    required_piece_quantity: int,
    expected_required_piece_quantity: int,
    actor_id: int | None,
    idempotency_key: str,
) -> tuple[SalesOrderItemBomDemandAdjustment | None, bool]:
    """Append one order-only component piece-demand fact with CAS and replay safety."""
    target = _as_positive_integer(required_piece_quantity, field="订单专用组件需求件数")
    expected = _as_positive_integer(
        expected_required_piece_quantity,
        field="修改前组件需求件数",
    )
    key = (idempotency_key or "").strip()
    if not key:
        raise CompositeBomWorkflowError("组件需求调整缺少幂等标识")

    snapshot = db.get(SalesOrderItemBomComponent, snapshot_id)
    if snapshot is None or snapshot.sales_order_item_id != order_item_id:
        raise CompositeBomWorkflowError("组件快照不属于当前订单明细")

    existing = db.scalar(
        select(SalesOrderItemBomDemandAdjustment).where(
            SalesOrderItemBomDemandAdjustment.idempotency_key == key
        )
    )
    if existing is not None:
        if (
            existing.sales_order_item_bom_component_id != snapshot_id
            or existing.event_type != "component_demand_adjusted"
            or int(existing.delta_order_set_quantity or 0) != 0
            or int(existing.delta_required_piece_quantity or 0) != target - expected
        ):
            raise CompositeBomWorkflowError("组件需求调整幂等标识已被其他变更使用")
        return existing, False

    current = next(
        (
            demand.required_piece_quantity
            for demand in effective_component_demands(db, order_item_id)
            if demand.snapshot_id == snapshot_id
        ),
        None,
    )
    if current is None:
        raise CompositeBomWorkflowError("组件快照不存在")
    if current != expected:
        raise CompositeBomWorkflowError(
            f"组件需求已由其他操作从{expected}改为{current}，请刷新后重试"
        )
    if target == current:
        return None, False

    row = SalesOrderItemBomDemandAdjustment(
        sales_order_item_bom_component_id=snapshot_id,
        event_type="component_demand_adjusted",
        delta_order_set_quantity=0,
        delta_required_piece_quantity=target - current,
        reason="订单组件需求变更（系统记录）",
        actor_id=actor_id,
        idempotency_key=key,
    )
    db.add(row)
    db.flush()
    return row, True


def ensure_component_production_tasks(
    db: Session,
    order_item_id: int,
) -> list[ProductionTask]:
    """Create/refresh only snapshot-bound tasks; leave the regular task alone."""
    item = db.get(OrderItem, order_item_id)
    if item is None:
        raise CompositeBomWorkflowError("订单明细不存在")
    demands = effective_component_demands(db, order_item_id)
    tasks: list[ProductionTask] = []
    for demand in demands:
        snapshot = db.get(SalesOrderItemBomComponent, demand.snapshot_id)
        if snapshot is None:
            raise CompositeBomWorkflowError("订单组件快照不存在")
        from app.services.production_workflow import (
            _new_task_printing_snapshot,
            _validate_task_status_quantity,
            cutting_output_factor,
        )
        from app.services.production_task_profile import new_task_profile_snapshot

        output_factor = cutting_output_factor(
            snapshot.snapshot_component_default_cutting_mode
        )
        task = db.scalar(
            select(ProductionTask).where(
                ProductionTask.sales_order_item_bom_component_id == demand.snapshot_id
            )
        )
        initial_coverage: int | None = None
        if task is None:
            initial_coverage = component_available_quantity(db, demand.snapshot_id)
            component_product = db.get(Product, snapshot.component_product_id)
            try:
                label_snapshot = build_new_task_production_label_snapshot(
                    component_product,
                    total_quantity=max(
                        demand.required_piece_quantity - initial_coverage,
                        0,
                    ),
                )
                if (
                    getattr(item, "composite_fulfillment_mode_snapshot", None)
                    == "parent_delivery"
                ):
                    label_snapshot = {
                        "production_label_enabled_snapshot": False,
                        "production_label_units_per_label_snapshot": None,
                        "production_label_total_quantity_snapshot": 0,
                        "production_label_count_snapshot": 0,
                        "production_label_template_version_snapshot": (
                            CURRENT_PRODUCTION_LABEL_TEMPLATE_VERSION
                        ),
                        "production_label_product_version_snapshot": (
                            int(component_product.version)
                            if component_product is not None
                            else None
                        ),
                    }
            except ProductionLabelStrategyError as error:
                raise CompositeBomWorkflowError(str(error)) from error
            task = ProductionTask(
                order_item_id=order_item_id,
                sales_order_item_bom_component_id=demand.snapshot_id,
                task_role="component_internal",
                status="waiting_material",
                planned_quantity=0,
                finished_coverage_snapshot=0,
                material_received_quantity=0,
                material_input_quantity=0,
                output_factor=output_factor,
                readiness_basis=None,
                version=1,
                **_new_task_printing_snapshot(db, component_product),
                **new_task_profile_snapshot(
                    db,
                    component_product,
                    item=item,
                    component_snapshot=snapshot,
                ),
                **label_snapshot,
            )
            db.add(task)
            db.flush()

        if task.status == "completed":
            tasks.append(task)
            continue
        ready = item.material_status == "received"
        coverage = (
            initial_coverage
            if initial_coverage is not None
            else component_available_quantity(db, demand.snapshot_id)
        )
        planned_quantity = max(demand.required_piece_quantity - coverage, 0)
        input_quantity = (
            ceil(planned_quantity / max(output_factor, 1)) if ready else 0
        )
        next_planned_quantity = planned_quantity if ready else 0
        next_status = (
            "not_required"
            if coverage >= demand.required_piece_quantity
            else "pending"
            if ready
            else "waiting_material"
        )
        _validate_task_status_quantity(next_status, next_planned_quantity)
        task.planned_quantity = next_planned_quantity
        task.finished_coverage_snapshot = coverage
        task.material_received_quantity = input_quantity
        task.material_input_quantity = input_quantity
        task.output_factor = output_factor
        task.status = next_status
        task.readiness_basis = (
            "component_finished_inventory"
            if next_status == "not_required"
            else "component_material_received"
            if ready
            else None
        )
        task.version = max(int(task.version or 0), 1) + 1
        tasks.append(task)
    db.flush()
    return tasks


def _stock_reservations(
    db: Session,
    snapshot_id: int,
) -> list[InventoryReservation]:
    return list(
        db.scalars(
            select(InventoryReservation)
            .where(
                InventoryReservation.sales_order_item_bom_component_id == snapshot_id,
                InventoryReservation.reservation_type == "finished_order",
                InventoryReservation.status.in_(ACTIVE_RESERVATION_STATUSES),
            )
            .order_by(InventoryReservation.id)
        ).all()
    )


def _direct_completion_rows(db: Session, snapshot_id: int):
    allocated = func.coalesce(
        func.sum(
            BomComponentDirectDeliveryAllocation.consumed_quantity
            - BomComponentDirectDeliveryAllocation.reversed_quantity
        ),
        0,
    )
    return db.execute(
        select(ProductionCompletion, allocated.label("allocated_quantity"))
        .join(ProductionTask, ProductionTask.id == ProductionCompletion.task_id)
        .outerjoin(
            ProductionStockTransfer,
            ProductionStockTransfer.completion_id == ProductionCompletion.id,
        )
        .outerjoin(
            BomComponentDirectDeliveryAllocation,
            (BomComponentDirectDeliveryAllocation.production_completion_id == ProductionCompletion.id)
            & (BomComponentDirectDeliveryAllocation.status.in_(ACTIVE_RESERVATION_STATUSES)),
        )
        .where(
            ProductionTask.sales_order_item_bom_component_id == snapshot_id,
            ProductionCompletion.status == "posted",
            ProductionCompletion.initial_disposition == DIRECT_DISPOSITION,
            ProductionCompletion.inventory_lot_id.is_(None),
            ProductionStockTransfer.id.is_(None),
        )
        .group_by(ProductionCompletion.id)
        .order_by(ProductionCompletion.completed_at, ProductionCompletion.id)
    ).all()


def component_availability(
    db: Session,
    snapshot_id: int,
) -> ComponentAvailability:
    snapshot = db.get(SalesOrderItemBomComponent, snapshot_id)
    if snapshot is None:
        raise CompositeBomWorkflowError("组件快照不存在")
    demand = next(
        demand
        for demand in effective_component_demands(db, snapshot.sales_order_item_id)
        if demand.snapshot_id == snapshot_id
    )
    stock_quantity = sum(_remaining_reservation_quantity(row) for row in _stock_reservations(db, snapshot_id))
    direct_quantity = sum(
        max(int(completion.quantity or 0) - int(allocated or 0), 0)
        for completion, allocated in _direct_completion_rows(db, snapshot_id)
    )
    return ComponentAvailability(
        snapshot_id=snapshot_id,
        component_code=demand.component_code,
        component_name=demand.component_name,
        quantity_per_set=demand.quantity_per_set,
        is_required=demand.is_required,
        stock_quantity=stock_quantity,
        direct_quantity=direct_quantity,
        available_quantity=stock_quantity + direct_quantity,
        required_piece_quantity=demand.required_piece_quantity,
    )


def component_available_quantity(db: Session, snapshot_id: int) -> int:
    return component_availability(db, snapshot_id).available_quantity


def _delivered_component_quantity(db: Session, snapshot_id: int) -> int:
    """Return the active delivered-piece fact for one order BOM snapshot."""
    direct_quantity = db.scalar(
        select(
            func.coalesce(
                func.sum(
                    BomComponentDirectDeliveryAllocation.consumed_quantity
                    - BomComponentDirectDeliveryAllocation.reversed_quantity
                ),
                0,
            )
        ).where(
            BomComponentDirectDeliveryAllocation.sales_order_item_bom_component_id
            == snapshot_id,
            BomComponentDirectDeliveryAllocation.status.in_(
                ACTIVE_RESERVATION_STATUSES
            ),
        )
    )
    stock_quantity = db.scalar(
        select(
            func.coalesce(
                func.sum(
                    DeliveryInventoryAllocation.credited_requirement_quantity
                    - DeliveryInventoryAllocation.reversed_requirement_quantity
                ),
                0,
            )
        )
        .join(
            InventoryReservation,
            InventoryReservation.id == DeliveryInventoryAllocation.reservation_id,
        )
        .where(
            InventoryReservation.sales_order_item_bom_component_id == snapshot_id,
            DeliveryInventoryAllocation.status.in_(ACTIVE_RESERVATION_STATUSES),
        )
    )
    return int(direct_quantity or 0) + int(stock_quantity or 0)


def delivered_component_quantities(
    db: Session,
    order_item_id: int,
) -> dict[int, int]:
    """Return cumulative active delivered pieces for every component snapshot."""
    return {
        demand.snapshot_id: _delivered_component_quantity(db, demand.snapshot_id)
        for demand in effective_component_demands(db, order_item_id)
    }


def delivery_item_component_quantities(
    db: Session,
    delivery_item_id: int,
) -> dict[int, int]:
    """Return actual active component pieces attached to one delivery line."""
    result: dict[int, int] = {}
    direct_rows = db.execute(
        select(
            BomComponentDirectDeliveryAllocation.sales_order_item_bom_component_id,
            func.coalesce(
                func.sum(
                    BomComponentDirectDeliveryAllocation.consumed_quantity
                    - BomComponentDirectDeliveryAllocation.reversed_quantity
                ),
                0,
            ),
        )
        .where(
            BomComponentDirectDeliveryAllocation.delivery_item_id == delivery_item_id,
            BomComponentDirectDeliveryAllocation.status.in_(
                ACTIVE_RESERVATION_STATUSES
            ),
        )
        .group_by(
            BomComponentDirectDeliveryAllocation.sales_order_item_bom_component_id
        )
    ).all()
    for snapshot_id, quantity in direct_rows:
        result[int(snapshot_id)] = result.get(int(snapshot_id), 0) + int(
            quantity or 0
        )
    stock_rows = db.execute(
        select(
            InventoryReservation.sales_order_item_bom_component_id,
            func.coalesce(
                func.sum(
                    DeliveryInventoryAllocation.credited_requirement_quantity
                    - DeliveryInventoryAllocation.reversed_requirement_quantity
                ),
                0,
            ),
        )
        .join(
            InventoryReservation,
            InventoryReservation.id == DeliveryInventoryAllocation.reservation_id,
        )
        .where(
            DeliveryInventoryAllocation.delivery_item_id == delivery_item_id,
            InventoryReservation.sales_order_item_bom_component_id.is_not(None),
            DeliveryInventoryAllocation.status.in_(ACTIVE_RESERVATION_STATUSES),
        )
        .group_by(InventoryReservation.sales_order_item_bom_component_id)
    ).all()
    for snapshot_id, quantity in stock_rows:
        result[int(snapshot_id)] = result.get(int(snapshot_id), 0) + int(
            quantity or 0
        )
    return result


def delivery_component_required_quantities(
    db: Session,
    *,
    order_item_id: int,
    delivery_sets: int,
) -> dict[int, int]:
    """Calculate this dispatch's pieces, capped by each order-specific demand.

    A unified-price composite order is still dispatched as one parent order
    line.  Its component demand may be lower than the parent quantity, so the
    cumulative component consumption must stop at the immutable snapshot plus
    append-only order adjustments instead of blindly multiplying every
    dispatch by the template quantity-per-set.
    """
    sets = _as_integer(delivery_sets, field="送货套数")
    if sets < 0:
        raise CompositeBomWorkflowError("送货套数不能小于0")
    item = db.get(OrderItem, order_item_id)
    delivered_before = max(int(item.delivered_quantity or 0), 0) if item else 0
    delivered_after = delivered_before + sets
    result: dict[int, int] = {}
    for demand in effective_component_demands(db, order_item_id):
        consumed = _delivered_component_quantity(db, demand.snapshot_id)
        target_after_dispatch = min(
            delivered_after * demand.quantity_per_set,
            demand.required_piece_quantity,
        )
        result[demand.snapshot_id] = max(target_after_dispatch - consumed, 0)
    return result


def kit_availability(db: Session, order_item_id: int) -> dict:
    """Return parent delivery capacity while respecting component piece targets."""
    demands = effective_component_demands(db, order_item_id)
    if not demands:
        return {"applicable": False, "available_sets": 0, "missing_components": [], "components": []}
    item = db.get(OrderItem, order_item_id)
    demand_by_snapshot = {demand.snapshot_id: demand for demand in demands}
    component_rows = [component_availability(db, demand.snapshot_id) for demand in demands]
    effective_sets = (
        max(int(item.quantity or 0), 0)
        if item is not None
        else max((d.effective_sets for d in demands), default=0)
    )
    delivered = int(item.delivered_quantity or 0) if item is not None else 0
    remaining_order_sets = max(effective_sets - delivered, 0)
    available_sets = remaining_order_sets
    missing: list[dict] = []
    components: list[dict] = []
    for row in component_rows:
        demand = demand_by_snapshot[row.snapshot_id]
        consumed = _delivered_component_quantity(db, row.snapshot_id)
        remaining_target = max(demand.required_piece_quantity - consumed, 0)
        shortage = max(remaining_target - row.available_quantity, 0)
        component_payload = asdict(row)
        component_payload.update(
            {
                "target_quantity": demand.required_piece_quantity,
                "delivered_quantity": consumed,
                "remaining_quantity": remaining_target,
                "delivered_piece_quantity": consumed,
                "remaining_required_piece_quantity": remaining_target,
            }
        )
        components.append(component_payload)
        if not row.is_required:
            continue
        total_coverable = consumed + row.available_quantity
        if total_coverable < demand.required_piece_quantity:
            component_sets = max(
                total_coverable // row.quantity_per_set - delivered,
                0,
            )
            available_sets = min(available_sets, component_sets)
            missing.append(
                {
                    "snapshot_id": row.snapshot_id,
                    "component_code": row.component_code,
                    "component_name": row.component_name,
                    "required_quantity": remaining_target,
                    "available_quantity": row.available_quantity,
                    "shortage_quantity": shortage,
                }
            )
    return {
        "applicable": True,
        "effective_sets": effective_sets,
        "delivered_sets": delivered,
        "available_sets": available_sets,
        "components": components,
        "missing_components": missing,
    }


def kit_available_sets_by_order_item_ids(
    db: Session,
    order_item_ids: Iterable[int],
) -> dict[int, int]:
    """Return composite-kit delivery capacity with a fixed query count.

    This is the batched count-only counterpart of :func:`kit_availability`.
    It deliberately returns no display payload; callers needing component
    details continue to use the single-item service.  The formulas and active
    reservation/allocation rules are kept identical to that service so a
    dashboard customer summary does not introduce one query group per order
    item.
    """

    item_ids = sorted({int(value) for value in order_item_ids if int(value) > 0})
    if not item_ids:
        return {}

    items = {
        int(item.id): item
        for item in db.scalars(
            select(OrderItem).where(OrderItem.id.in_(item_ids))
        ).all()
    }
    snapshots = list(
        db.scalars(
            select(SalesOrderItemBomComponent)
            .where(SalesOrderItemBomComponent.sales_order_item_id.in_(item_ids))
            .order_by(
                SalesOrderItemBomComponent.sales_order_item_id,
                SalesOrderItemBomComponent.display_order,
                SalesOrderItemBomComponent.id,
            )
        ).all()
    )
    if not snapshots:
        return {}
    snapshot_ids = [int(snapshot.id) for snapshot in snapshots]

    adjustments = {
        int(snapshot_id): (int(delta_sets or 0), int(delta_pieces or 0))
        for snapshot_id, delta_sets, delta_pieces in db.execute(
            select(
                SalesOrderItemBomDemandAdjustment.sales_order_item_bom_component_id,
                func.coalesce(
                    func.sum(
                        SalesOrderItemBomDemandAdjustment.delta_order_set_quantity
                    ),
                    0,
                ),
                func.coalesce(
                    func.sum(
                        SalesOrderItemBomDemandAdjustment.delta_required_piece_quantity
                    ),
                    0,
                ),
            )
            .where(
                SalesOrderItemBomDemandAdjustment.sales_order_item_bom_component_id.in_(
                    snapshot_ids
                )
            )
            .group_by(
                SalesOrderItemBomDemandAdjustment.sales_order_item_bom_component_id
            )
        ).all()
    }

    remaining_stock = (
        InventoryReservation.reserved_stock_quantity
        - InventoryReservation.consumed_stock_quantity
        - InventoryReservation.released_stock_quantity
    )
    stock_by_snapshot = {
        int(snapshot_id): int(quantity or 0)
        for snapshot_id, quantity in db.execute(
            select(
                InventoryReservation.sales_order_item_bom_component_id,
                func.coalesce(
                    func.sum(
                        case((remaining_stock > 0, remaining_stock), else_=0)
                    ),
                    0,
                ),
            )
            .where(
                InventoryReservation.sales_order_item_bom_component_id.in_(
                    snapshot_ids
                ),
                InventoryReservation.reservation_type == "finished_order",
                InventoryReservation.status.in_(ACTIVE_RESERVATION_STATUSES),
            )
            .group_by(InventoryReservation.sales_order_item_bom_component_id)
        ).all()
    }

    allocated_direct = func.coalesce(
        func.sum(
            BomComponentDirectDeliveryAllocation.consumed_quantity
            - BomComponentDirectDeliveryAllocation.reversed_quantity
        ),
        0,
    )
    direct_by_snapshot: dict[int, int] = {}
    for snapshot_id, _completion_id, quantity, allocated in db.execute(
        select(
            ProductionTask.sales_order_item_bom_component_id,
            ProductionCompletion.id,
            ProductionCompletion.quantity,
            allocated_direct.label("allocated_quantity"),
        )
        .join(ProductionTask, ProductionTask.id == ProductionCompletion.task_id)
        .outerjoin(
            ProductionStockTransfer,
            ProductionStockTransfer.completion_id == ProductionCompletion.id,
        )
        .outerjoin(
            BomComponentDirectDeliveryAllocation,
            (
                BomComponentDirectDeliveryAllocation.production_completion_id
                == ProductionCompletion.id
            )
            & (
                BomComponentDirectDeliveryAllocation.status.in_(
                    ACTIVE_RESERVATION_STATUSES
                )
            ),
        )
        .where(
            ProductionTask.sales_order_item_bom_component_id.in_(snapshot_ids),
            ProductionCompletion.status == "posted",
            ProductionCompletion.initial_disposition == DIRECT_DISPOSITION,
            ProductionStockTransfer.id.is_(None),
        )
        .group_by(
            ProductionTask.sales_order_item_bom_component_id,
            ProductionCompletion.id,
            ProductionCompletion.quantity,
        )
    ).all():
        key = int(snapshot_id)
        direct_by_snapshot[key] = direct_by_snapshot.get(key, 0) + max(
            int(quantity or 0) - int(allocated or 0),
            0,
        )

    delivered_direct = {
        int(snapshot_id): int(quantity or 0)
        for snapshot_id, quantity in db.execute(
            select(
                BomComponentDirectDeliveryAllocation.sales_order_item_bom_component_id,
                func.coalesce(
                    func.sum(
                        BomComponentDirectDeliveryAllocation.consumed_quantity
                        - BomComponentDirectDeliveryAllocation.reversed_quantity
                    ),
                    0,
                ),
            )
            .where(
                BomComponentDirectDeliveryAllocation.sales_order_item_bom_component_id.in_(
                    snapshot_ids
                ),
                BomComponentDirectDeliveryAllocation.status.in_(
                    ACTIVE_RESERVATION_STATUSES
                ),
            )
            .group_by(
                BomComponentDirectDeliveryAllocation.sales_order_item_bom_component_id
            )
        ).all()
    }
    delivered_stock = {
        int(snapshot_id): int(quantity or 0)
        for snapshot_id, quantity in db.execute(
            select(
                InventoryReservation.sales_order_item_bom_component_id,
                func.coalesce(
                    func.sum(
                        DeliveryInventoryAllocation.credited_requirement_quantity
                        - DeliveryInventoryAllocation.reversed_requirement_quantity
                    ),
                    0,
                ),
            )
            .join(
                InventoryReservation,
                InventoryReservation.id
                == DeliveryInventoryAllocation.reservation_id,
            )
            .where(
                InventoryReservation.sales_order_item_bom_component_id.in_(
                    snapshot_ids
                ),
                DeliveryInventoryAllocation.status.in_(
                    ACTIVE_RESERVATION_STATUSES
                ),
            )
            .group_by(InventoryReservation.sales_order_item_bom_component_id)
        ).all()
    }

    demands_by_item: dict[int, list[tuple[int, int, bool, int, int]]] = {}
    fallback_sets_by_item: dict[int, int] = {}
    for snapshot in snapshots:
        delta_sets, delta_pieces = adjustments.get(int(snapshot.id), (0, 0))
        per_set = _as_positive_integer(
            snapshot.quantity_per_set,
            field="每套组件数量",
        )
        effective_sets = int(snapshot.order_set_quantity) + delta_sets
        if effective_sets < 0:
            raise CompositeBomWorkflowError("组件调整后的有效套数不能小于0")
        required_pieces = (
            _as_positive_integer(
                snapshot.required_piece_quantity,
                field="组件需求件数",
            )
            + delta_pieces
        )
        if required_pieces <= 0:
            raise CompositeBomWorkflowError("组件调整后的需求件数必须大于0")
        item_id = int(snapshot.sales_order_item_id)
        demands_by_item.setdefault(item_id, []).append(
            (
                int(snapshot.id),
                per_set,
                bool(snapshot.is_required),
                required_pieces,
                effective_sets,
            )
        )
        fallback_sets_by_item[item_id] = max(
            fallback_sets_by_item.get(item_id, 0),
            effective_sets,
        )

    result: dict[int, int] = {}
    for item_id, demands in demands_by_item.items():
        item = items.get(item_id)
        effective_sets = (
            max(int(item.quantity or 0), 0)
            if item is not None
            else fallback_sets_by_item.get(item_id, 0)
        )
        delivered_sets = max(
            int(item.delivered_quantity or 0) if item is not None else 0,
            0,
        )
        available_sets = max(effective_sets - delivered_sets, 0)
        for snapshot_id, per_set, is_required, required_pieces, _ in demands:
            if not is_required:
                continue
            consumed_pieces = (
                delivered_direct.get(snapshot_id, 0)
                + delivered_stock.get(snapshot_id, 0)
            )
            available_pieces = (
                stock_by_snapshot.get(snapshot_id, 0)
                + direct_by_snapshot.get(snapshot_id, 0)
            )
            total_coverable = consumed_pieces + available_pieces
            if total_coverable < required_pieces:
                component_sets = max(
                    total_coverable // per_set - delivered_sets,
                    0,
                )
                available_sets = min(available_sets, component_sets)
        result[item_id] = available_sets
    return result


def build_delivery_component_consumption_plan(
    db: Session,
    *,
    delivery_item_id: int,
    delivery_sets: int,
) -> list[ComponentConsumption]:
    """Preflight every required component.  No writes occur in this function."""
    sets = _as_positive_integer(delivery_sets, field="送货套数")
    delivery_item = db.get(DeliveryItem, delivery_item_id)
    if delivery_item is None:
        raise CompositeBomWorkflowError("送货明细不存在")
    demands = effective_component_demands(db, delivery_item.order_item_id)
    if not demands:
        return []
    required_quantities = delivery_component_required_quantities(
        db,
        order_item_id=delivery_item.order_item_id,
        delivery_sets=sets,
    )

    plan: list[ComponentConsumption] = []
    shortages: list[str] = []
    for demand in demands:
        needed = required_quantities.get(demand.snapshot_id, 0)
        if needed <= 0:
            continue
        parts: list[ConsumptionPart] = []
        remaining = needed
        for reservation in _stock_reservations(db, demand.snapshot_id):
            quantity = min(_remaining_reservation_quantity(reservation), remaining)
            if quantity:
                parts.append(ConsumptionPart("stock", demand.snapshot_id, reservation.id, quantity))
                remaining -= quantity
            if not remaining:
                break
        if remaining:
            for completion, allocated in _direct_completion_rows(db, demand.snapshot_id):
                quantity = min(max(int(completion.quantity or 0) - int(allocated or 0), 0), remaining)
                if quantity:
                    parts.append(ConsumptionPart("direct", demand.snapshot_id, completion.id, quantity))
                    remaining -= quantity
                if not remaining:
                    break
        if remaining and demand.is_required:
            shortages.append(f"{demand.component_name}缺{remaining}件")
        if not demand.is_required and remaining == needed:
            continue
        plan.append(
            ComponentConsumption(
                snapshot_id=demand.snapshot_id,
                component_code=demand.component_code,
                component_name=demand.component_name,
                required_quantity=needed,
                parts=tuple(parts),
            )
        )
    if shortages:
        raise CompositeBomWorkflowError("复合产品无法齐套发货：" + "；".join(shortages))
    return plan


def execute_delivery_component_consumption(
    db: Session,
    *,
    delivery_item_id: int,
    delivery_sets: int,
    operator_id: int | None,
    operation_key: str,
) -> list[ComponentConsumption]:
    """Atomically allocate component stock/direct completions for one delivery item."""
    if not operation_key or not operation_key.strip():
        raise CompositeBomWorkflowError("组件送货扣减缺少操作标识")
    delivery_item = db.get(DeliveryItem, delivery_item_id)
    if delivery_item is None:
        raise CompositeBomWorkflowError("送货明细不存在")
    demands = effective_component_demands(db, delivery_item.order_item_id)
    if not demands:
        return []
    existing_direct = db.scalar(
        select(BomComponentDirectDeliveryAllocation.id).where(
            BomComponentDirectDeliveryAllocation.delivery_item_id == delivery_item_id,
            BomComponentDirectDeliveryAllocation.status.in_(ACTIVE_RESERVATION_STATUSES),
        ).limit(1)
    )
    existing_stock = db.scalar(
        select(DeliveryInventoryAllocation.id)
        .join(
            InventoryReservation,
            InventoryReservation.id == DeliveryInventoryAllocation.reservation_id,
        )
        .where(
            DeliveryInventoryAllocation.delivery_item_id == delivery_item_id,
            DeliveryInventoryAllocation.status.in_(ACTIVE_RESERVATION_STATUSES),
            InventoryReservation.sales_order_item_bom_component_id.is_not(None),
        )
        .limit(1)
    )
    if existing_direct is not None or existing_stock is not None:
        return []  # delivery dispatch is already allocated; caller stays idempotent.

    with db.begin_nested():
        plan = build_delivery_component_consumption_plan(
            db, delivery_item_id=delivery_item_id, delivery_sets=delivery_sets
        )
        for component in plan:
            for part in component.parts:
                if part.source == "direct":
                    existing = db.scalar(
                        select(BomComponentDirectDeliveryAllocation).where(
                            BomComponentDirectDeliveryAllocation.delivery_item_id
                            == delivery_item_id,
                            BomComponentDirectDeliveryAllocation.production_completion_id
                            == part.source_id,
                        )
                    )
                    if existing is None:
                        db.add(
                            BomComponentDirectDeliveryAllocation(
                                delivery_item_id=delivery_item_id,
                                production_completion_id=part.source_id,
                                sales_order_item_bom_component_id=part.snapshot_id,
                                consumed_quantity=part.quantity,
                                reversed_quantity=0,
                                status="active",
                                created_by=operator_id,
                            )
                        )
                    else:
                        if (
                            existing.sales_order_item_bom_component_id
                            != part.snapshot_id
                            or existing.status != "reversed"
                            or int(existing.reversed_quantity or 0)
                            != int(existing.consumed_quantity or 0)
                        ):
                            raise CompositeBomWorkflowError(
                                "组件直接送货分配已变化，请刷新后重试"
                            )
                        existing.consumed_quantity = part.quantity
                        existing.reversed_quantity = 0
                        existing.status = "active"
                        existing.created_by = operator_id
                        existing.reversed_by = None
                        existing.reversed_at = None
                    continue

                reservation = db.get(InventoryReservation, part.source_id)
                if reservation is None or reservation.sales_order_item_bom_component_id != part.snapshot_id:
                    raise CompositeBomWorkflowError("组件成品预占记录已变化，请刷新后重试")
                lot = db.get(InventoryLot, reservation.inventory_lot_id)
                if lot is None or _remaining_reservation_quantity(reservation) < part.quantity:
                    raise CompositeBomWorkflowError("组件成品预占数量不足，请刷新后重试")
                if int(lot.quantity_reserved or 0) < part.quantity:
                    raise CompositeBomWorkflowError("组件库存预占余额异常，请刷新后重试")
                before = _balances(lot)
                lot.quantity_reserved -= part.quantity
                lot.quantity_consumed += part.quantity
                lot.version = int(lot.version or 0) + 1
                reservation.consumed_stock_quantity += part.quantity
                reservation.consumed_requirement_quantity += part.quantity
                reservation.consumed_by = operator_id
                reservation.consumed_at = utc_now_naive()
                reservation.status = _reservation_status(reservation)
                db.flush()
                movement = _movement(
                    db,
                    lot=lot,
                    movement_type="consume",
                    quantity=part.quantity,
                    before=before,
                    operator_id=operator_id,
                    reason="N039复合组件随整套送货消耗",
                    idempotency_key=f"{operation_key}:stock:{reservation.id}",
                    reservation_id=reservation.id,
                    related_order_item_id=delivery_item.order_item_id,
                    related_delivery_id=delivery_item.delivery_id,
                )
                db.flush()
                db.add(
                    DeliveryInventoryAllocation(
                        delivery_item_id=delivery_item_id,
                        reservation_id=reservation.id,
                        consume_movement_id=movement.id,
                        consumed_stock_quantity=part.quantity,
                        credited_requirement_quantity=part.quantity,
                        reversed_stock_quantity=0,
                        reversed_requirement_quantity=0,
                        status="active",
                        created_by=operator_id,
                    )
                )
        db.flush()
    return plan


def reverse_delivery_component_allocations(
    db: Session,
    *,
    delivery_item_id: int,
    operator_id: int | None,
    operation_key: str,
) -> None:
    """Reverse only N039 snapshot-bound allocations for a cancelled delivery item."""
    if not operation_key or not operation_key.strip():
        raise CompositeBomWorkflowError("组件送货撤销缺少操作标识")
    location_ids = db.scalars(
            select(InventoryLot.warehouse_location_id)
            .join(
                InventoryReservation,
                InventoryReservation.inventory_lot_id == InventoryLot.id,
            )
            .join(
                DeliveryInventoryAllocation,
                DeliveryInventoryAllocation.reservation_id
                == InventoryReservation.id,
            )
            .where(
                DeliveryInventoryAllocation.delivery_item_id == delivery_item_id,
                DeliveryInventoryAllocation.status.in_(ACTIVE_RESERVATION_STATUSES),
                InventoryReservation.sales_order_item_bom_component_id.is_not(None),
            )
            .distinct()
        ).all()
    try:
        for location_id in sorted({int(value) for value in location_ids}):
            _claim_inventory_destination(db, location_id)
    except WarehouseInventoryError as error:
        raise CompositeBomWorkflowError(str(error)) from error
    with db.begin_nested():
        direct_rows = db.scalars(
            select(BomComponentDirectDeliveryAllocation).where(
                BomComponentDirectDeliveryAllocation.delivery_item_id == delivery_item_id,
                BomComponentDirectDeliveryAllocation.status.in_(ACTIVE_RESERVATION_STATUSES),
            )
        ).all()
        now = utc_now_naive()
        for row in direct_rows:
            row.reversed_quantity = row.consumed_quantity
            row.status = "reversed"
            row.reversed_by = operator_id
            row.reversed_at = now

        allocations = db.scalars(
            select(DeliveryInventoryAllocation)
            .join(InventoryReservation, InventoryReservation.id == DeliveryInventoryAllocation.reservation_id)
            .where(
                DeliveryInventoryAllocation.delivery_item_id == delivery_item_id,
                DeliveryInventoryAllocation.status.in_(ACTIVE_RESERVATION_STATUSES),
                InventoryReservation.sales_order_item_bom_component_id.is_not(None),
            )
        ).all()
        for allocation in allocations:
            quantity = int(allocation.consumed_stock_quantity) - int(allocation.reversed_stock_quantity or 0)
            if quantity <= 0:
                continue
            reservation = db.get(InventoryReservation, allocation.reservation_id)
            lot = db.get(InventoryLot, reservation.inventory_lot_id) if reservation else None
            if reservation is None or lot is None or int(lot.quantity_consumed or 0) < quantity:
                raise CompositeBomWorkflowError("组件库存分配无法撤销，请先核对库存记录")
            before = _balances(lot)
            lot.quantity_reserved += quantity
            lot.quantity_consumed -= quantity
            lot.version = int(lot.version or 0) + 1
            reservation.consumed_stock_quantity -= quantity
            reservation.consumed_requirement_quantity -= quantity
            reservation.status = _reservation_status(reservation)
            allocation.reversed_stock_quantity += quantity
            allocation.reversed_requirement_quantity += quantity
            allocation.status = "reversed"
            allocation.reversed_by = operator_id
            allocation.reversed_at = now
            db.flush()
            _movement(
                db,
                lot=lot,
                movement_type="reverse_consume",
                quantity=quantity,
                before=before,
                operator_id=operator_id,
                reason="N039复合组件送货撤销",
                idempotency_key=f"{operation_key}:stock:{allocation.id}",
                reservation_id=reservation.id,
                related_order_item_id=reservation.order_item_id,
                reversal_of_movement_id=allocation.consume_movement_id,
            )
        db.flush()


def serialize_kit_availability(db: Session, order_item_id: int) -> dict:
    """Small API-friendly wrapper kept here so APIs do not repeat N039 math."""
    return kit_availability(db, order_item_id)
