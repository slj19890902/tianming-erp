from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from hashlib import sha256
import json
from math import ceil
from typing import Literal, Sequence

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.customer import Customer
from app.models.delivery import Delivery, DeliveryItem
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.production import (
    ProductionCompletion,
    ProductionCompletionBatch,
    ProductionStockTransfer,
    ProductionTask,
)
from app.models.user import User
from app.models.warehouse_inventory import (
    InventoryLot,
    InventoryPallet,
    InventoryPalletItem,
    InventoryReservation,
    OrderItemSemiRequirement,
    WarehouseLocation,
)
from app.services.warehouse_inventory import (
    WarehouseInventoryError,
    active_finished_reserved_qty,
    manual_finished_in,
    reserve_completed_finished_inventory,
)


Disposition = Literal["direct", "stock"]

WAITING_MATERIAL = "waiting_material"
PENDING = "pending"
COMPLETED = "completed"
NOT_REQUIRED = "not_required"
READY_TASK_STATUSES = frozenset({COMPLETED, NOT_REQUIRED})
PRODUCIBLE_ORDER_STATUSES = frozenset(
    {"pending_confirmation", "pending_production", "production"}
)
MUTABLE_ORDER_STATUSES = frozenset(
    {*PRODUCIBLE_ORDER_STATUSES, "pending_delivery"}
)
TEMPORARY_LOCATION_CODES = frozenset(
    [*(f"F12-P{number:02d}" for number in range(1, 9))]
    + [*(f"F34-P{number:02d}" for number in range(1, 4))]
)


class ProductionWorkflowError(ValueError):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class CompletionCommand:
    task_id: int
    expected_version: int
    disposition: Disposition
    location_id: int | None = None
    pallet_id: int | None = None
    pallet_code: str | None = None
    remarks: str | None = None


@dataclass(frozen=True)
class StockTransferCommand:
    location_id: int
    idempotency_key: str
    pallet_id: int | None = None
    pallet_code: str | None = None
    remarks: str | None = None


@dataclass(frozen=True)
class CompletionBatchResult:
    batch: ProductionCompletionBatch
    completions: tuple[ProductionCompletion, ...]
    replayed: bool


@dataclass(frozen=True)
class StockTransferResult:
    transfer: ProductionStockTransfer
    replayed: bool


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _stable_key(*parts: object, max_length: int = 100) -> str:
    raw = ":".join(str(part).strip() for part in parts)
    if len(raw) <= max_length:
        return raw
    digest = sha256(raw.encode("utf-8")).hexdigest()[:24]
    return f"{raw[: max_length - 25]}:{digest}"


def _normalized_text(value: str | None) -> str | None:
    normalized = (value or "").strip()
    return normalized or None


def _canonical_hash(payload: dict) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def completion_batch_request_hash(
    idempotency_key: str,
    commands: Sequence[CompletionCommand],
) -> str:
    items = []
    for command in sorted(commands, key=lambda row: row.task_id):
        item = asdict(command)
        item["remarks"] = _normalized_text(command.remarks)
        item["pallet_code"] = _normalized_text(command.pallet_code)
        items.append(item)
    return _canonical_hash(
        {"idempotency_key": idempotency_key.strip(), "items": items}
    )


def stock_transfer_request_hash(
    completion_id: int,
    command: StockTransferCommand,
) -> str:
    payload = asdict(command)
    payload["idempotency_key"] = command.idempotency_key.strip()
    payload["remarks"] = _normalized_text(command.remarks)
    payload["pallet_code"] = _normalized_text(command.pallet_code)
    return _canonical_hash({"completion_id": completion_id, **payload})


def has_production_completion_facts(
    db: Session,
    order_item_ids: Sequence[int],
) -> bool:
    normalized = sorted({int(value) for value in order_item_ids if int(value) > 0})
    if not normalized:
        return False
    return (
        db.scalar(
            select(ProductionCompletion.id)
            .where(ProductionCompletion.order_item_id.in_(normalized))
            .limit(1)
        )
        is not None
    )


def lock_order_rows_for_production_transition(
    db: Session,
    order_ids: Sequence[int],
) -> dict[int, Order]:
    """Serialize production facts with order termination, deletion, and rollback."""
    normalized = sorted({int(value) for value in order_ids if int(value) > 0})
    if not normalized:
        return {}
    for order_id in normalized:
        claimed = db.execute(
            update(Order)
            .where(Order.id == order_id)
            .values(status=Order.status)
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:
            raise ProductionWorkflowError("订单不存在或已被删除，不能继续生产操作", 409)
    db.flush()
    locked: dict[int, Order] = {}
    for order_id in normalized:
        order = db.get(Order, order_id)
        if order is None:
            raise ProductionWorkflowError("订单不存在或已被删除，不能继续生产操作", 409)
        db.refresh(order)
        locked[order_id] = order
    return locked


def _dispatched_delivery_order_item_ids(
    db: Session,
    order_item_ids: Sequence[int],
) -> set[int]:
    normalized = sorted(
        {int(value) for value in order_item_ids if int(value) > 0}
    )
    if not normalized:
        return set()
    return set(
        db.scalars(
            select(DeliveryItem.order_item_id)
            .join(Delivery, Delivery.id == DeliveryItem.delivery_id)
            .where(
                DeliveryItem.order_item_id.in_(normalized),
                Delivery.status == "dispatched",
            )
            .distinct()
        ).all()
    )


def has_dispatched_delivery_facts(
    db: Session,
    order_item_ids: Sequence[int],
) -> bool:
    return bool(_dispatched_delivery_order_item_ids(db, order_item_ids))


def _semi_inventory_covers(db: Session, order_item_id: int) -> bool:
    # Local import keeps the delivery inventory module free to call the
    # production-facts helper without creating a module import cycle.
    from app.services.semi_finished_inventory import inventory_fully_covers_order_item

    return inventory_fully_covers_order_item(db, order_item_id)


def refresh_production_task(
    db: Session,
    order_item_id: int,
    *,
    create_if_missing: bool = False,
) -> ProductionTask | None:
    item = db.get(OrderItem, order_item_id)
    if item is None:
        raise ProductionWorkflowError("订单明细不存在", 404)
    task = db.scalar(
        select(ProductionTask).where(ProductionTask.order_item_id == item.id)
    )
    if task is None:
        if not create_if_missing:
            return None
        task = ProductionTask(
            order_item_id=item.id,
            status=WAITING_MATERIAL,
            planned_quantity=0,
            finished_coverage_snapshot=0,
            readiness_basis=None,
            ready_at=None,
            version=1,
        )
        db.add(task)
        db.flush()

    if task.status == COMPLETED or has_production_completion_facts(db, [item.id]):
        return task

    order_quantity = int(item.quantity or 0)
    if order_quantity <= 0:
        raise ProductionWorkflowError("订单明细数量必须大于0", 409)
    finished_coverage = min(
        max(active_finished_reserved_qty(db, item.id), 0), order_quantity
    )
    now = utc_now()
    if finished_coverage >= order_quantity:
        next_status = NOT_REQUIRED
        planned_quantity = 0
        readiness_basis = "finished_inventory"
    elif item.material_status == "received":
        next_status = PENDING
        planned_quantity = order_quantity - finished_coverage
        readiness_basis = "material_received"
    elif _semi_inventory_covers(db, item.id):
        next_status = PENDING
        planned_quantity = order_quantity - finished_coverage
        readiness_basis = "semi_finished_inventory"
    else:
        next_status = WAITING_MATERIAL
        planned_quantity = 0
        readiness_basis = None

    state_changed = (
        task.status != next_status
        or int(task.planned_quantity or 0) != planned_quantity
        or int(task.finished_coverage_snapshot or 0) != finished_coverage
        or task.readiness_basis != readiness_basis
    )
    if not state_changed:
        return task

    was_ready = task.status in {PENDING, NOT_REQUIRED}
    is_ready = next_status in {PENDING, NOT_REQUIRED}
    task.status = next_status
    task.planned_quantity = planned_quantity
    task.finished_coverage_snapshot = finished_coverage
    task.readiness_basis = readiness_basis
    task.ready_at = task.ready_at if was_ready and is_ready else (now if is_ready else None)
    task.version = int(task.version or 0) + 1
    db.flush()
    refresh_order_production_status(db, item.order_id)
    return task


def create_or_refresh_production_task(
    db: Session,
    order_item_id: int,
) -> ProductionTask:
    task = refresh_production_task(db, order_item_id, create_if_missing=True)
    assert task is not None
    return task


def refresh_existing_production_task(
    db: Session,
    order_item_id: int | None,
) -> ProductionTask | None:
    if order_item_id is None:
        return None
    return refresh_production_task(db, order_item_id, create_if_missing=False)


def refresh_order_production_status(db: Session, order_id: int) -> Order | None:
    order = db.get(Order, order_id)
    if order is None or order.status not in MUTABLE_ORDER_STATUSES:
        return order
    item_count = int(
        db.scalar(select(func.count(OrderItem.id)).where(OrderItem.order_id == order.id))
        or 0
    )
    tasks = db.scalars(
        select(ProductionTask)
        .join(OrderItem, OrderItem.id == ProductionTask.order_item_id)
        .where(OrderItem.order_id == order.id)
        .order_by(ProductionTask.id)
    ).all()
    if not tasks or len(tasks) != item_count:
        return order
    if all(task.status in READY_TASK_STATUSES for task in tasks):
        next_status = "pending_delivery"
    elif any(task.status == COMPLETED for task in tasks):
        next_status = "production"
    else:
        next_status = "pending_production"
    if order.status != next_status:
        order.status = next_status
        db.flush()
    return order


def production_ready_quantity(db: Session, order_item: OrderItem | int) -> int:
    item = db.get(OrderItem, order_item) if isinstance(order_item, int) else order_item
    if item is None:
        raise ProductionWorkflowError("订单明细不存在", 404)
    finished_coverage = max(active_finished_reserved_qty(db, item.id), 0)
    direct_quantity = int(
        db.scalar(
            select(func.coalesce(func.sum(ProductionCompletion.quantity), 0))
            .outerjoin(
                ProductionStockTransfer,
                ProductionStockTransfer.completion_id == ProductionCompletion.id,
            )
            .where(
                ProductionCompletion.order_item_id == item.id,
                ProductionCompletion.initial_disposition == "direct",
                ProductionStockTransfer.id.is_(None),
            )
        )
        or 0
    )
    return min(int(item.quantity or 0), finished_coverage + direct_quantity)


def _current_pallet(db: Session, location_id: int) -> InventoryPallet | None:
    return db.scalar(
        select(InventoryPallet).where(
            InventoryPallet.location_id == location_id,
            InventoryPallet.is_current.is_(True),
        )
    )


def _temporary_location(
    db: Session,
    location_id: int | None,
    *,
    pallet_id: int | None,
) -> WarehouseLocation:
    if location_id is None:
        raise ProductionWorkflowError("库存完工必须选择三楼临放位", 400)
    location = db.get(WarehouseLocation, location_id)
    if location is None:
        raise ProductionWorkflowError("三楼临放位不存在", 404)
    if (
        location.location_code not in TEMPORARY_LOCATION_CODES
        or location.source_version != "V11"
        or location.warehouse_floor != 3
        or not location.is_temporary
        or not location.is_active
    ):
        raise ProductionWorkflowError(
            "生产完工库存仅允许进入三楼临放位 F12-P01..P08、F34-P01..P03",
            409,
        )
    pallet = _current_pallet(db, location.id)
    if pallet is not None:
        item_exists = db.scalar(
            select(InventoryPalletItem.id)
            .where(InventoryPalletItem.pallet_id == pallet.id)
            .limit(1)
        )
        if pallet_id != pallet.id or item_exists is not None:
            raise ProductionWorkflowError("所选三楼临放位已占用，请选择空位", 409)
    elif pallet_id is not None:
        raise ProductionWorkflowError("指定栈板不在所选三楼临放位", 409)
    return location


def list_temporary_locations(db: Session) -> list[dict]:
    locations = db.scalars(
        select(WarehouseLocation)
        .where(
            WarehouseLocation.location_code.in_(TEMPORARY_LOCATION_CODES),
            WarehouseLocation.source_version == "V11",
            WarehouseLocation.warehouse_floor == 3,
            WarehouseLocation.is_temporary.is_(True),
            WarehouseLocation.is_active.is_(True),
        )
        .order_by(WarehouseLocation.sort_order, WarehouseLocation.location_code)
    ).all()
    result: list[dict] = []
    for location in locations:
        pallet = _current_pallet(db, location.id)
        occupied = False
        if pallet is not None:
            occupied = (
                db.scalar(
                    select(InventoryPalletItem.id)
                    .where(InventoryPalletItem.pallet_id == pallet.id)
                    .limit(1)
                )
                is not None
            )
        result.append(
            {
                "id": location.id,
                "location_code": location.location_code,
                "location_name": location.location_name,
                "pallet_id": pallet.id if pallet is not None else None,
                "pallet_code": pallet.pallet_code if pallet is not None else None,
                "is_empty": not occupied,
            }
        )
    return result


def _consume_completion_semi_reservations(
    db: Session,
    *,
    completion: ProductionCompletion,
    task: ProductionTask,
    item: OrderItem,
    planned_quantity: int,
    operator_id: int | None,
) -> None:
    from app.services.semi_finished_inventory import consume_semi_finished_reservation

    requirements = db.scalars(
        select(OrderItemSemiRequirement)
        .where(OrderItemSemiRequirement.order_item_id == item.id)
        .order_by(OrderItemSemiRequirement.component_type, OrderItemSemiRequirement.id)
    ).all()
    require_full = task.readiness_basis == "semi_finished_inventory"
    product = db.get(Product, item.product_id)
    box_style = str(product.box_style or "") if product is not None else ""
    expected_components = (
        {"cover", "base"}
        if "天地盖" in box_style or "A3" in box_style.upper()
        else {"whole"}
    )
    by_component = {row.component_type: row for row in requirements}
    if require_full and not expected_components.issubset(by_component):
        missing = "、".join(sorted(expected_components - set(by_component)))
        raise ProductionWorkflowError(
            f"半成品齐套任务缺少{missing}组件需求，无法完成生产完工", 409
        )
    for requirement in requirements:
        reservations = db.scalars(
            select(InventoryReservation)
            .join(InventoryLot, InventoryLot.id == InventoryReservation.inventory_lot_id)
            .where(
                InventoryReservation.order_item_id == item.id,
                InventoryReservation.semi_requirement_id == requirement.id,
                InventoryReservation.reservation_type == "semi_order",
                InventoryReservation.status != "cancelled",
                InventoryReservation.reserved_stock_quantity
                > InventoryReservation.consumed_stock_quantity
                + InventoryReservation.released_stock_quantity,
            )
            .order_by(InventoryLot.stock_date, InventoryLot.id, InventoryReservation.id)
        ).all()
        if not reservations:
            if require_full and requirement.component_type in expected_components:
                raise ProductionWorkflowError(
                    f"{requirement.component_type}半成品预占不存在，无法完成生产完工",
                    409,
                )
            continue
        target_pieces = planned_quantity * max(int(requirement.pieces_per_box or 1), 1)
        remaining_pieces = target_pieces
        for reservation in reservations:
            if remaining_pieces <= 0:
                break
            available_credit = max(
                int(reservation.credited_requirement_quantity or 0)
                - int(reservation.consumed_requirement_quantity or 0)
                - int(reservation.released_requirement_quantity or 0),
                0,
            )
            if available_credit <= 0:
                continue
            yield_factor = max(int(reservation.yield_factor or 1), 1)
            available_stock = (
                int(reservation.reserved_stock_quantity or 0)
                - int(reservation.consumed_stock_quantity or 0)
                - int(reservation.released_stock_quantity or 0)
            )
            stock_quantity = min(
                available_stock,
                ceil(min(remaining_pieces, available_credit) / yield_factor),
            )
            if stock_quantity <= 0:
                continue
            lot = db.get(InventoryLot, reservation.inventory_lot_id)
            if lot is None:
                raise ProductionWorkflowError("半成品库存批次不存在", 409)
            before_credit = int(reservation.consumed_requirement_quantity or 0)
            mutation = consume_semi_finished_reservation(
                db,
                reservation_id=reservation.id,
                stock_quantity=stock_quantity,
                expected_version=lot.version,
                operator_id=operator_id,
                idempotency_key=_stable_key(
                    "production-completion", completion.id, "semi", reservation.id
                ),
                delivery_item_id=None,
                reason="生产完工消耗半成品预占",
            )
            consumed_credit = (
                int(mutation.reservation.consumed_requirement_quantity or 0)
                - before_credit
            )
            remaining_pieces -= consumed_credit
        if (
            require_full
            and requirement.component_type in expected_components
            and remaining_pieces > 0
        ):
            raise ProductionWorkflowError(
                f"{requirement.component_type}半成品预占余额不足，无法完成生产完工",
                409,
            )


def _stock_completion_lot(
    db: Session,
    *,
    completion: ProductionCompletion,
    order: Order,
    item: OrderItem,
    command: CompletionCommand | StockTransferCommand,
    operator_id: int | None,
    idempotency_prefix: str,
) -> InventoryLot:
    location = _temporary_location(
        db,
        command.location_id,
        pallet_id=command.pallet_id,
    )
    lot = manual_finished_in(
        db,
        customer_id=order.customer_id,
        product_id=item.product_id,
        location_id=location.id,
        quantity=int(completion.quantity),
        stock_date=date.today(),
        source_type="production_surplus",
        source_ref_type="production_completion",
        source_ref_id=completion.id,
        remarks=_normalized_text(command.remarks),
        operator_id=operator_id,
        idempotency_key=_stable_key(idempotency_prefix, "finished-in"),
        pallet_id=command.pallet_id,
        pallet_code=_normalized_text(command.pallet_code),
        require_empty_pallet=True,
        movement_reason="生产完工入库",
    )
    reserve_completed_finished_inventory(
        db,
        order_item_id=item.id,
        inventory_lot_id=lot.id,
        quantity=int(completion.quantity),
        expected_version=int(lot.version),
        operator_id=operator_id,
        idempotency_key=_stable_key(idempotency_prefix, "finished-reserve"),
    )
    return lot


def _validate_commands(commands: Sequence[CompletionCommand]) -> None:
    if not commands:
        raise ProductionWorkflowError("完工批次至少包含一条生产任务")
    task_ids = [command.task_id for command in commands]
    if any(task_id <= 0 for task_id in task_ids):
        raise ProductionWorkflowError("生产任务编号无效")
    if len(set(task_ids)) != len(task_ids):
        raise ProductionWorkflowError("同一生产任务不能在批次中重复提交")
    for command in commands:
        if command.expected_version <= 0:
            raise ProductionWorkflowError("生产任务版本必须大于0")
        if command.disposition not in {"direct", "stock"}:
            raise ProductionWorkflowError("完工去向必须明确选择 direct 或 stock")
        if command.disposition == "direct" and any(
            value is not None
            for value in (command.location_id, command.pallet_id, command.pallet_code)
        ):
            raise ProductionWorkflowError("直接送货完工不能填写库存货位或栈板")
        if command.disposition == "stock" and command.location_id is None:
            raise ProductionWorkflowError("库存完工必须选择三楼临放位")


def _replay_completion_batch(
    db: Session,
    *,
    batch: ProductionCompletionBatch,
    request_hash: str,
) -> CompletionBatchResult:
    if batch.request_hash != request_hash:
        raise ProductionWorkflowError("同一幂等键对应的完工内容不一致", 409)
    rows = db.scalars(
        select(ProductionCompletion)
        .where(ProductionCompletion.batch_id == batch.id)
        .order_by(ProductionCompletion.id)
    ).all()
    return CompletionBatchResult(batch, tuple(rows), True)


def complete_production_batch(
    db: Session,
    *,
    idempotency_key: str,
    commands: Sequence[CompletionCommand],
    operator_id: int | None,
) -> CompletionBatchResult:
    key = idempotency_key.strip()
    if not key or len(key) > 120:
        raise ProductionWorkflowError("幂等键长度必须为1到120个字符")
    _validate_commands(commands)
    request_hash = completion_batch_request_hash(key, commands)
    existing_batch = db.scalar(
        select(ProductionCompletionBatch).where(
            ProductionCompletionBatch.idempotency_key == key
        )
    )
    if existing_batch is not None:
        return _replay_completion_batch(
            db, batch=existing_batch, request_hash=request_hash
        )

    task_ids = [command.task_id for command in commands]
    initial_rows = db.execute(
        select(ProductionTask, OrderItem, Order)
        .join(OrderItem, OrderItem.id == ProductionTask.order_item_id)
        .join(Order, Order.id == OrderItem.order_id)
        .where(ProductionTask.id.in_(task_ids))
        .order_by(ProductionTask.id)
    ).all()
    if len(initial_rows) != len(task_ids):
        raise ProductionWorkflowError("生产任务不存在或已被删除", 404)
    lock_order_rows_for_production_transition(
        db,
        [order.id for _task, _item, order in initial_rows],
    )
    concurrent_batch = db.scalar(
        select(ProductionCompletionBatch).where(
            ProductionCompletionBatch.idempotency_key == key
        )
    )
    if concurrent_batch is not None:
        return _replay_completion_batch(
            db, batch=concurrent_batch, request_hash=request_hash
        )
    rows = db.execute(
        select(ProductionTask, OrderItem, Order)
        .join(OrderItem, OrderItem.id == ProductionTask.order_item_id)
        .join(Order, Order.id == OrderItem.order_id)
        .where(ProductionTask.id.in_(task_ids))
        .order_by(ProductionTask.id)
        .execution_options(populate_existing=True)
    ).all()
    if len(rows) != len(task_ids):
        raise ProductionWorkflowError("生产任务或关联订单已被删除，不能确认完工", 409)
    by_task = {task.id: (task, item, order) for task, item, order in rows}
    customer_ids = {order.customer_id for _, _, order in rows}
    if len(customer_ids) != 1:
        raise ProductionWorkflowError("一个完工批次只能包含同一客户的生产任务", 409)

    for command in commands:
        task, item, order = by_task[command.task_id]
        if order.status not in PRODUCIBLE_ORDER_STATUSES:
            raise ProductionWorkflowError(
                "订单当前状态不允许继续生产完工，请刷新后重试", 409
            )
        if item.is_force_closed:
            raise ProductionWorkflowError("订单明细已强制关闭，不能继续生产完工", 409)
        if task.status != PENDING:
            raise ProductionWorkflowError("仅待完工生产任务可以确认完工", 409)
        if int(task.version) != command.expected_version:
            raise ProductionWorkflowError("生产任务版本已变化，请刷新后重试", 409)
        if int(task.planned_quantity or 0) <= 0:
            raise ProductionWorkflowError("生产任务冻结计划数量无效", 409)
        if int(item.delivered_quantity or 0) > 0:
            raise ProductionWorkflowError("订单明细已送货，不能再确认生产完工", 409)
        if has_production_completion_facts(db, [item.id]):
            raise ProductionWorkflowError("该订单明细已经存在生产完工事实", 409)
        if command.disposition == "stock":
            _temporary_location(
                db, command.location_id, pallet_id=command.pallet_id
            )

    now = utc_now()
    batch = ProductionCompletionBatch(
        idempotency_key=key,
        request_hash=request_hash,
        item_count=len(commands),
        completed_by=operator_id,
        completed_at=now,
    )
    try:
        # A savepoint keeps a concurrent idempotency-key winner from poisoning
        # the outer API transaction.  PostgreSQL can then see the committed
        # winner on the retry query; SQLite may instead serialize writers.
        with db.begin_nested():
            db.add(batch)
            db.flush()
    except IntegrityError:
        concurrent_batch = db.scalar(
            select(ProductionCompletionBatch).where(
                ProductionCompletionBatch.idempotency_key == key
            )
        )
        if concurrent_batch is None:
            raise
        return _replay_completion_batch(
            db, batch=concurrent_batch, request_hash=request_hash
        )
    completions: list[ProductionCompletion] = []
    affected_order_ids: set[int] = set()
    for command in sorted(commands, key=lambda row: row.task_id):
        task, item, order = by_task[command.task_id]
        completion = ProductionCompletion(
            batch_id=batch.id,
            task_id=task.id,
            order_item_id=item.id,
            expected_version=command.expected_version,
            quantity=int(task.planned_quantity),
            initial_disposition=command.disposition,
            warehouse_location_id=(
                command.location_id if command.disposition == "stock" else None
            ),
            inventory_lot_id=None,
            remarks=_normalized_text(command.remarks),
            completed_by=operator_id,
            completed_at=now,
        )
        db.add(completion)
        db.flush()
        if command.disposition == "stock":
            lot = _stock_completion_lot(
                db,
                completion=completion,
                order=order,
                item=item,
                command=command,
                operator_id=operator_id,
                idempotency_prefix=_stable_key("production-completion", completion.id),
            )
            completion.inventory_lot_id = lot.id
        _consume_completion_semi_reservations(
            db,
            completion=completion,
            task=task,
            item=item,
            planned_quantity=int(task.planned_quantity),
            operator_id=operator_id,
        )
        result = db.execute(
            update(ProductionTask)
            .where(
                ProductionTask.id == task.id,
                ProductionTask.version == command.expected_version,
                ProductionTask.status == PENDING,
            )
            .values(status=COMPLETED, version=ProductionTask.version + 1)
        )
        if result.rowcount != 1:
            raise ProductionWorkflowError("生产任务版本已变化，请刷新后重试", 409)
        affected_order_ids.add(order.id)
        completions.append(completion)
    db.flush()
    for order_id in sorted(affected_order_ids):
        refresh_order_production_status(db, order_id)
    return CompletionBatchResult(batch, tuple(completions), False)


def transfer_direct_completion_to_stock(
    db: Session,
    *,
    completion_id: int,
    command: StockTransferCommand,
    operator_id: int | None,
) -> StockTransferResult:
    key = command.idempotency_key.strip()
    if not key or len(key) > 120:
        raise ProductionWorkflowError("幂等键长度必须为1到120个字符")
    request_hash = stock_transfer_request_hash(completion_id, command)
    repeated = db.scalar(
        select(ProductionStockTransfer).where(
            ProductionStockTransfer.idempotency_key == key
        )
    )
    if repeated is not None:
        if repeated.completion_id != completion_id or repeated.request_hash != request_hash:
            raise ProductionWorkflowError("同一幂等键对应的转库存内容不一致", 409)
        return StockTransferResult(repeated, True)

    completion = db.get(ProductionCompletion, completion_id)
    if completion is None:
        raise ProductionWorkflowError("生产完工记录不存在", 404)
    if completion.initial_disposition != "direct":
        raise ProductionWorkflowError("只有直接送货完工记录可以转库存", 409)
    existing_transfer = db.scalar(
        select(ProductionStockTransfer).where(
            ProductionStockTransfer.completion_id == completion.id
        )
    )
    if existing_transfer is not None:
        raise ProductionWorkflowError("该完工记录已转入库存，不能重复操作", 409)
    item = db.get(OrderItem, completion.order_item_id)
    if item is None:
        raise ProductionWorkflowError("完工记录关联订单明细不存在", 409)
    order = lock_order_rows_for_production_transition(db, [item.order_id])[item.order_id]
    claimed = db.execute(
        update(OrderItem)
        .where(
            OrderItem.id == item.id,
            OrderItem.delivered_quantity == 0,
        )
        .values(delivered_quantity=OrderItem.delivered_quantity)
        .execution_options(synchronize_session=False)
    )
    db.flush()
    db.expire(item)
    item = db.get(OrderItem, completion.order_item_id)
    if item is None:
        raise ProductionWorkflowError("完工记录关联订单明细不存在", 409)
    if order.status not in MUTABLE_ORDER_STATUSES or item.is_force_closed:
        raise ProductionWorkflowError(
            "订单或明细已结案，不能再把直接送货完工转入库存", 409
        )
    if has_dispatched_delivery_facts(db, [item.id]):
        raise ProductionWorkflowError("已经发生真实发货，不能整批转库存", 409)
    if claimed.rowcount != 1 or int(item.delivered_quantity or 0) != 0:
        raise ProductionWorkflowError("订单明细已产生送货数量，不能转入库存", 409)
    existing_transfer = db.scalar(
        select(ProductionStockTransfer).where(
            ProductionStockTransfer.completion_id == completion.id
        )
    )
    if existing_transfer is not None:
        raise ProductionWorkflowError("该完工记录已转入库存，不能重复操作", 409)
    _temporary_location(db, command.location_id, pallet_id=command.pallet_id)
    lot = _stock_completion_lot(
        db,
        completion=completion,
        order=order,
        item=item,
        command=command,
        operator_id=operator_id,
        idempotency_prefix=_stable_key("production-transfer", completion.id, key),
    )
    transfer = ProductionStockTransfer(
        completion_id=completion.id,
        warehouse_location_id=command.location_id,
        inventory_lot_id=lot.id,
        idempotency_key=key,
        request_hash=request_hash,
        transferred_by=operator_id,
        transferred_at=utc_now(),
    )
    db.add(transfer)
    db.flush()
    return StockTransferResult(transfer, False)


def _task_query(db: Session, allowed_customer_ids: set[int] | None):
    query = (
        select(ProductionTask, OrderItem, Order, Customer, Product)
        .join(OrderItem, OrderItem.id == ProductionTask.order_item_id)
        .join(Order, Order.id == OrderItem.order_id)
        .join(Customer, Customer.id == Order.customer_id)
        .join(Product, Product.id == OrderItem.product_id)
    )
    if allowed_customer_ids is not None:
        query = query.where(Order.customer_id.in_(allowed_customer_ids))
    return query


def _item_product_snapshot(item: OrderItem, product: Product) -> dict:
    is_die_cut = product.box_category == "die_cut"
    mold = product.mold_tool if is_die_cut else None
    return {
        "product_id": item.product_id,
        "product_code": item.snapshot_product_code or product.product_code,
        "product_name": item.snapshot_product_name or product.product_name,
        "specification": item.snapshot_spec,
        "material": item.snapshot_material,
        "flute": item.flute_type,
        "special_process": item.special_process,
        "production_notes": item.snapshot_production_notes,
        "mold_name": mold.mold_name if mold is not None else None,
        "mold_location": mold.rack_location if mold is not None else None,
    }


def list_production_tasks(
    db: Session,
    *,
    allowed_customer_ids: set[int] | None,
    status: str | None = None,
) -> list[dict]:
    query = _task_query(db, allowed_customer_ids).where(
        Order.status.in_(PRODUCIBLE_ORDER_STATUSES),
        OrderItem.is_force_closed.is_(False),
    )
    if status:
        query = query.where(ProductionTask.status == status)
    rows = db.execute(query.order_by(Order.delivery_date, Order.id, OrderItem.id)).all()
    return [
        {
            "id": task.id,
            "order_item_id": item.id,
            "order_id": order.id,
            "order_number": order.order_number,
            "item_order_number": item.item_order_number,
            "customer_id": order.customer_id,
            "customer_name": customer.name,
            **_item_product_snapshot(item, product),
            "order_quantity": int(item.quantity),
            "delivered_quantity": int(item.delivered_quantity or 0),
            "material_status": item.material_status,
            "status": task.status,
            "planned_quantity": int(task.planned_quantity),
            "finished_coverage_snapshot": int(task.finished_coverage_snapshot),
            "readiness_basis": task.readiness_basis,
            "ready_at": task.ready_at,
            "version": int(task.version),
            "production_ready_quantity": production_ready_quantity(db, item),
        }
        for task, item, order, customer, product in rows
    ]


def _completion_rows(
    db: Session,
    *,
    allowed_customer_ids: set[int] | None,
    completion_ids: Sequence[int] | None = None,
):
    query = (
        select(
            ProductionCompletion,
            ProductionTask,
            OrderItem,
            Order,
            Customer,
            Product,
            User,
            ProductionStockTransfer,
        )
        .join(ProductionTask, ProductionTask.id == ProductionCompletion.task_id)
        .join(OrderItem, OrderItem.id == ProductionCompletion.order_item_id)
        .join(Order, Order.id == OrderItem.order_id)
        .join(Customer, Customer.id == Order.customer_id)
        .join(Product, Product.id == OrderItem.product_id)
        .outerjoin(User, User.id == ProductionCompletion.completed_by)
        .outerjoin(
            ProductionStockTransfer,
            ProductionStockTransfer.completion_id == ProductionCompletion.id,
        )
    )
    if allowed_customer_ids is not None:
        query = query.where(Order.customer_id.in_(allowed_customer_ids))
    if completion_ids is not None:
        query = query.where(ProductionCompletion.id.in_(completion_ids))
    return db.execute(query.order_by(ProductionCompletion.id.desc())).all()


def list_production_completions(
    db: Session,
    *,
    allowed_customer_ids: set[int] | None,
    completion_ids: Sequence[int] | None = None,
) -> list[dict]:
    rows = _completion_rows(
        db,
        allowed_customer_ids=allowed_customer_ids,
        completion_ids=completion_ids,
    )
    dispatched_item_ids = _dispatched_delivery_order_item_ids(
        db,
        [item.id for _completion, _task, item, *_rest in rows],
    )
    result: list[dict] = []
    for completion, task, item, order, customer, product, user, transfer in rows:
        effective_location_id = (
            transfer.warehouse_location_id
            if transfer is not None
            else completion.warehouse_location_id
        )
        effective_lot_id = (
            transfer.inventory_lot_id if transfer is not None else completion.inventory_lot_id
        )
        location = (
            db.get(WarehouseLocation, effective_location_id)
            if effective_location_id is not None
            else None
        )
        result.append(
            {
                "id": completion.id,
                "batch_id": completion.batch_id,
                "task_id": task.id,
                "order_item_id": item.id,
                "order_id": order.id,
                "order_number": order.order_number,
                "item_order_number": item.item_order_number,
                "customer_id": order.customer_id,
                "customer_name": customer.name,
                **_item_product_snapshot(item, product),
                "quantity": int(completion.quantity),
                "initial_disposition": completion.initial_disposition,
                "warehouse_location_id": effective_location_id,
                "warehouse_location_code": location.location_code if location else None,
                "inventory_lot_id": effective_lot_id,
                "remarks": completion.remarks,
                "completed_by": completion.completed_by,
                "completed_by_name": user.real_name if user is not None else None,
                "completed_at": completion.completed_at,
                "stock_transfer_id": transfer.id if transfer is not None else None,
                "can_transfer_to_stock": (
                    completion.initial_disposition == "direct"
                    and transfer is None
                    and int(item.delivered_quantity or 0) == 0
                    and item.id not in dispatched_item_ids
                    and order.status in MUTABLE_ORDER_STATUSES
                    and not item.is_force_closed
                ),
            }
        )
    return result


def completion_customer_id(db: Session, completion_id: int) -> int | None:
    return db.scalar(
        select(Order.customer_id)
        .join(OrderItem, OrderItem.order_id == Order.id)
        .join(
            ProductionCompletion,
            ProductionCompletion.order_item_id == OrderItem.id,
        )
        .where(ProductionCompletion.id == completion_id)
    )


def batch_customer_ids(
    db: Session,
    completions: Sequence[ProductionCompletion],
) -> set[int]:
    item_ids = [row.order_item_id for row in completions]
    if not item_ids:
        return set()
    return set(
        db.scalars(
            select(Order.customer_id)
            .join(OrderItem, OrderItem.order_id == Order.id)
            .where(OrderItem.id.in_(item_ids))
        ).all()
    )
