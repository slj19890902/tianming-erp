from __future__ import annotations
from app.services.external_receipt_state import active_receipt_item

from collections import Counter, OrderedDict, defaultdict
from dataclasses import dataclass
from decimal import Decimal
from threading import RLock
from typing import Iterable, Sequence

from sqlalchemy import Row, event, func, or_, select
from sqlalchemy.orm import Session, load_only, selectinload

from app.models.delivery import Delivery, DeliveryItem
from app.models.external_packaging_purchase import (
    ExternalPackagingPurchaseBatch,
    ExternalPackagingPurchaseCancellation,
    ExternalPackagingPurchaseItem,
    ExternalPackagingPurchaseOrder,
    ExternalPackagingReceiptItem,
)
from app.models.finance import (
    Invoice,
    ReturnReceipt,
    ReturnReceiptItem,
    SettlementRecord,
    Statement,
    StatementItem,
)
from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.order_external_packaging import SalesOrderItemExternalComponent
from app.services.multilevel_bom_external_identity import current_external_component_predicate
from app.services.multilevel_bom_execution_boundary import current_snapshot_predicate
from app.models.product_bom import SalesOrderItemBomComponent, RequisitionItemBomSource
from app.models.multilevel_bom import OrderBomGraph
from app.models.requisition import Requisition, RequisitionItem
from app.models.production import ProductionCompletion, ProductionTask
from app.models.supplier_requisition_order import (
    SupplierRequisitionOrder,
    SupplierRequisitionOrderItem,
)
from app.models.warehouse_inventory import (
    InventoryReservation,
    OrderItemSemiRequirement,
)
from app.services.order_status_policy import (
    MANAGEMENT_ORDER_STATUSES,
    MANAGEMENT_TERMINAL_ORDER_STATUSES,
    PERSISTED_ORDER_STATUS_LABELS,
)


BUSINESS_STATUS_LABELS = {
    **PERSISTED_ORDER_STATUS_LABELS,
    "pending_material": "待报料",
    "pending_incoming": "待收料",
    "waiting_receipt": "待回单",
}

BUSINESS_STATUS_ORDER = (
    "pending_confirmation",
    "pending_material",
    "pending_incoming",
    "pending_production",
    "pending_delivery",
    "partially_delivered",
    "waiting_receipt",
    "pending_reconciliation",
    "pending_invoice",
    "pending_payment",
    "completed",
)

DERIVED_BUSINESS_STATUSES = frozenset(BUSINESS_STATUS_ORDER[1:])
EARLY_BLOCKING_STATUSES = frozenset(
    {"pending_material", "pending_incoming", "pending_production"}
)
READY_PRODUCTION_STATUSES = frozenset({"completed", "not_required"})

_STATUS_RANK = {
    status: index for index, status in enumerate(BUSINESS_STATUS_ORDER)
}


@dataclass(frozen=True)
class ActiveOrderStatusBadgeSnapshot:
    """A read-only active-order projection, scoped to one database and role."""

    order_ids: tuple[int, ...]
    projections: dict[int, dict]


_ACTIVE_STATUS_BADGE_CACHE: OrderedDict[
    tuple[str, tuple[int, ...] | None, bool], ActiveOrderStatusBadgeSnapshot
] = OrderedDict()
_ACTIVE_STATUS_BADGE_CACHE_LOCK = RLock()
_ACTIVE_STATUS_BADGE_CACHE_LIMIT = 8


def _clear_active_status_badge_cache(*_args) -> None:
    """A committed mutation makes every process-local badge projection stale."""

    with _ACTIVE_STATUS_BADGE_CACHE_LOCK:
        _ACTIVE_STATUS_BADGE_CACHE.clear()


event.listen(Session, "after_commit", _clear_active_status_badge_cache)


def order_status_projection_load_options(*, include_order_summary: bool = False) -> list:
    """Keep bulk state reads independent of wide transaction detail snapshots.

    Summary rendering consumes the order header and the same quantity fields.
    Full detail requests load their page separately; no facts are discarded.
    """
    options = [
        selectinload(Order.items).load_only(
            OrderItem.id,
            OrderItem.order_id,
            OrderItem.product_id,
            OrderItem.item_sequence,
            OrderItem.quantity,
            OrderItem.delivered_quantity,
            OrderItem.material_status,
            OrderItem.requisition_status,
            OrderItem.supply_mode_snapshot,
            OrderItem.is_force_closed,
            OrderItem.composite_fulfillment_mode_snapshot,
        ),
    ]
    if not include_order_summary:
        options.insert(0, load_only(Order.id, Order.status))
    return options


@dataclass(frozen=True, slots=True)
class _OrderStatusReadRow:
    """A disposable input to the existing status rules, never a write model."""

    id: int
    status: str
    items: tuple[Row, ...]


def _status_read_rows(db: Session, headers: Sequence[Row]) -> list[_OrderStatusReadRow]:
    items_by_order: dict[int, list[Row]] = defaultdict(list)
    for chunk in _chunks([int(row.id) for row in headers]):
        for item in db.execute(
            select(
                OrderItem.id, OrderItem.order_id, OrderItem.product_id,
                OrderItem.item_sequence, OrderItem.quantity, OrderItem.delivered_quantity,
                OrderItem.material_status, OrderItem.requisition_status,
                OrderItem.supply_mode_snapshot, OrderItem.is_force_closed,
                OrderItem.composite_fulfillment_mode_snapshot,
            ).where(OrderItem.order_id.in_(chunk)).order_by(OrderItem.id)
        ):
            items_by_order[int(item.order_id)].append(item)
    return [
        _OrderStatusReadRow(int(row.id), row.status, tuple(items_by_order[int(row.id)]))
        for row in headers
    ]


def active_order_status_badge_snapshot(
    db: Session,
    *,
    scoped_customer_ids: set[int] | None,
    include_finance: bool,
) -> tuple[ActiveOrderStatusBadgeSnapshot, dict[int, Order], bool]:
    """Return the exact active-order badge projection with commit invalidation.

    The projection remains a presentation cache only.  It is keyed by database,
    customer scope and finance visibility, is never used by a write path, and
    is cleared after every SQLAlchemy commit before the next reader can reuse
    it. The third return value marks a cache hit. Bulk inputs are read-only
    SQL rows; the caller loads ORM detail for its visible page separately.
    """

    database_key = str(db.get_bind().url)
    scope_key = (
        tuple(sorted(int(customer_id) for customer_id in scoped_customer_ids))
        if scoped_customer_ids is not None
        else None
    )
    key = (database_key, scope_key, bool(include_finance))
    with _ACTIVE_STATUS_BADGE_CACHE_LOCK:
        cached = _ACTIVE_STATUS_BADGE_CACHE.get(key)
        if cached is not None:
            _ACTIVE_STATUS_BADGE_CACHE.move_to_end(key)
            return cached, {}, True

        query = select(Order.id, Order.status).where(
            Order.status.notin_(MANAGEMENT_TERMINAL_ORDER_STATUSES),
            Order.items.any(),
        )
        if scoped_customer_ids is not None:
            query = query.where(Order.customer_id.in_(scoped_customer_ids))
        headers = db.execute(query).all()
        order_ids = tuple(int(row.id) for row in headers)
        orders = _status_read_rows(db, headers)
        snapshot = ActiveOrderStatusBadgeSnapshot(
            order_ids=order_ids,
            projections=build_order_business_statuses(
                db,
                orders,
                include_finance=include_finance,
            ),
        )
        _ACTIVE_STATUS_BADGE_CACHE[key] = snapshot
        _ACTIVE_STATUS_BADGE_CACHE.move_to_end(key)
        while len(_ACTIVE_STATUS_BADGE_CACHE) > _ACTIVE_STATUS_BADGE_CACHE_LIMIT:
            _ACTIVE_STATUS_BADGE_CACHE.popitem(last=False)
        return snapshot, {}, False


def status_label(status: str) -> str:
    return BUSINESS_STATUS_LABELS.get(status, status)


def _chunks(values: Sequence[int], size: int = 800) -> Iterable[list[int]]:
    for offset in range(0, len(values), size):
        yield list(values[offset : offset + size])


def _minimum_status(statuses: Iterable[str]) -> str:
    return min(statuses, key=lambda value: _STATUS_RANK.get(value, 10_000))


def aggregate_business_status(
    item_statuses: Sequence[str],
    *,
    delivered_quantity: int,
    remaining_quantity: int,
) -> str:
    """Aggregate independent line states without letting a later line hide a blocker."""

    if not item_statuses:
        return "pending_material"
    blockers = [status for status in item_statuses if status in EARLY_BLOCKING_STATUSES]
    if blockers:
        return _minimum_status(blockers)
    if delivered_quantity > 0 and remaining_quantity > 0:
        return "partially_delivered"
    return _minimum_status(item_statuses)


def _evidence(basis: str, label: str, **extra) -> dict:
    return {"basis": basis, "label": label, **extra}


def build_order_business_statuses(
    db: Session,
    orders: Sequence[Order | _OrderStatusReadRow],
    *,
    include_delivery: bool = True,
    include_finance: bool = True,
) -> dict[int, dict]:
    """Return one batch-derived projection for order lists, detail and dashboards.

    The function never writes persisted order status.  It deliberately reads
    formal facts in batches so list rendering does not perform one query per
    order or per line.
    """

    if not orders:
        return {}
    items = [item for order in orders for item in order.items]
    item_ids = [int(item.id) for item in items]
    if not item_ids:
        return {
            int(order.id): {
                "business_status": (
                    order.status
                    if order.status in MANAGEMENT_ORDER_STATUSES
                    else "pending_material"
                ),
                "business_status_label": status_label(
                    order.status
                    if order.status in MANAGEMENT_ORDER_STATUSES
                    else "pending_material"
                ),
                "business_status_evidence": _evidence(
                    "no_order_items", "订单没有有效明细"
                ),
                "business_delivery_progress": None,
                "business_item_status_counts": {},
                "items": {},
            }
            for order in orders
        }

    product_ids = {int(item.product_id) for item in items if item.product_id is not None}
    products_by_id = (
        {
            int(product.id): product
            for product in db.scalars(
                select(Product)
                .options(load_only(Product.id, Product.box_style, Product.box_category))
                .where(Product.id.in_(product_ids))
            ).all()
        }
        # Small detail pages already retain their few products in the session.
        # The explicit batch matters only for large list projections, where it
        # prevents the direct-liner check from reloading the same products.
        if len(item_ids) > 100 and product_ids
        else {}
    )

    confirmed_supplier_item_ids: set[int] = set()
    closed_incoming_item_ids: set[int] = set()
    confirmed_external_component_ids_by_item: dict[int, set[int]] = defaultdict(set)
    received_external_component_ids_by_item: dict[int, set[int]] = defaultdict(set)
    posted_completion_item_ids: set[int] = set()
    required_component_ids_by_item: dict[int, set[int]] = defaultdict(set)
    required_external_component_ids_by_item: dict[int, set[int]] = defaultdict(set)
    task_statuses_by_item: dict[int, dict[int | None, str]] = defaultdict(dict)
    finished_coverage_by_item: dict[int, int] = defaultdict(int)
    semi_requirement_quantity_by_id: dict[int, tuple[int, int]] = {}
    semi_coverage_by_requirement_id: dict[int, int] = defaultdict(int)
    dispatched_rows_by_item: dict[int, list[tuple[int, int, int]]] = defaultdict(list)

    for item_id_chunk in _chunks(item_ids):
        confirmed_supplier_item_ids.update(
            int(item_id)
            for item_id in db.scalars(
                select(SupplierRequisitionOrderItem.order_item_id)
                .join(
                    SupplierRequisitionOrder,
                    SupplierRequisitionOrder.id
                    == SupplierRequisitionOrderItem.supplier_order_id,
                )
                .where(
                    SupplierRequisitionOrderItem.order_item_id.in_(item_id_chunk),
                    SupplierRequisitionOrder.status == "confirmed",
                    SupplierRequisitionOrderItem.status == "active",
                )
                .distinct()
            ).all()
            if item_id is not None
        )
        # The explicit BOM batch API is also a formal paper report. It freezes
        # purchase-purpose sources directly; it need not manufacture a second
        # supplier-order row merely to make the status projection advance.
        confirmed_supplier_item_ids.update(db.scalars(select(RequisitionItem.order_item_id)
            .join(Requisition, Requisition.id == RequisitionItem.requisition_id)
            .join(RequisitionItemBomSource, RequisitionItemBomSource.requisition_item_id == RequisitionItem.id)
            .join(SalesOrderItemBomComponent,
                SalesOrderItemBomComponent.id == RequisitionItemBomSource.sales_order_item_bom_component_id)
            .join(OrderBomGraph, OrderBomGraph.order_item_id == RequisitionItem.order_item_id)
            .where(RequisitionItem.order_item_id.in_(item_id_chunk),
                SalesOrderItemBomComponent.sales_order_item_id == RequisitionItem.order_item_id,
                current_snapshot_predicate(), Requisition.status == "已报料",
                Requisition.request_key.is_not(None), Requisition.request_actor_id.is_not(None),
                RequisitionItem.status.in_(("已报料", "部分入库", "已入库"))).distinct()))
        external_purchase_rows = db.execute(
            select(
                ExternalPackagingPurchaseItem.sales_order_item_id,
                ExternalPackagingPurchaseItem.order_component_id,
                ExternalPackagingPurchaseItem.id,
                ExternalPackagingPurchaseItem.purchase_quantity,
            )
            .join(
                ExternalPackagingPurchaseOrder,
                ExternalPackagingPurchaseOrder.id
                == ExternalPackagingPurchaseItem.purchase_order_id,
            )
            .join(
                ExternalPackagingPurchaseBatch,
                ExternalPackagingPurchaseBatch.id
                == ExternalPackagingPurchaseOrder.batch_id,
            )
            .outerjoin(
                ExternalPackagingPurchaseCancellation,
                ExternalPackagingPurchaseCancellation.purchase_order_id
                == ExternalPackagingPurchaseOrder.id,
            )
            .where(
                ExternalPackagingPurchaseItem.sales_order_item_id.in_(
                    item_id_chunk
                ),
                ExternalPackagingPurchaseOrder.status == "confirmed",
                ExternalPackagingPurchaseCancellation.id.is_(None),
            )
        ).all()
        external_purchase_item_ids = [int(row[2]) for row in external_purchase_rows]
        received_external_totals = {
            int(purchase_item_id): Decimal(str(quantity or 0))
            for purchase_item_id, quantity in db.execute(
                select(
                    ExternalPackagingReceiptItem.purchase_item_id,
                    func.sum(ExternalPackagingReceiptItem.received_quantity),
                )
                .where(
                    active_receipt_item(),
                    ExternalPackagingReceiptItem.purchase_item_id.in_(
                        external_purchase_item_ids
                    )
                )
                .group_by(ExternalPackagingReceiptItem.purchase_item_id)
            ).all()
        } if external_purchase_item_ids else {}
        for (
            sales_order_item_id,
            order_component_id,
            purchase_item_id,
            purchase_quantity,
        ) in external_purchase_rows:
            order_item_id = int(sales_order_item_id)
            component_id = int(order_component_id)
            confirmed_external_component_ids_by_item[order_item_id].add(
                component_id
            )
            if received_external_totals.get(
                int(purchase_item_id), Decimal("0")
            ) >= Decimal(purchase_quantity):
                received_external_component_ids_by_item[order_item_id].add(
                    component_id
                )
        closed_incoming_item_ids.update(
            int(item_id)
            for item_id in db.scalars(
                select(IncomingReceiptItem.order_item_id)
                .join(
                    IncomingReceipt,
                    IncomingReceipt.id == IncomingReceiptItem.receipt_id,
                )
                .where(
                    IncomingReceiptItem.order_item_id.in_(item_id_chunk),
                    IncomingReceiptItem.status == "posted",
                    IncomingReceipt.status == "posted",
                    or_(
                        IncomingReceiptItem.cumulative_received_quantity
                        >= IncomingReceiptItem.planned_quantity,
                        IncomingReceiptItem.resolution_action == "accept_short",
                    ),
                )
                .distinct()
            ).all()
        )
        posted_completion_item_ids.update(
            int(item_id)
            for item_id in db.scalars(
                select(ProductionCompletion.order_item_id)
                .where(
                    ProductionCompletion.order_item_id.in_(item_id_chunk),
                    ProductionCompletion.status == "posted",
                )
                .distinct()
            ).all()
        )
        for order_item_id, component_id in db.execute(
            select(
                SalesOrderItemBomComponent.sales_order_item_id,
                SalesOrderItemBomComponent.id,
            ).where(
                SalesOrderItemBomComponent.sales_order_item_id.in_(item_id_chunk),
                SalesOrderItemBomComponent.is_required.is_(True),
                current_snapshot_predicate(),
            )
        ):
            required_component_ids_by_item[int(order_item_id)].add(int(component_id))
        for order_item_id, component_id in db.execute(
            select(
                SalesOrderItemExternalComponent.sales_order_item_id,
                SalesOrderItemExternalComponent.id,
            ).where(
                SalesOrderItemExternalComponent.sales_order_item_id.in_(
                    item_id_chunk
                ),
                SalesOrderItemExternalComponent.is_required.is_(True),
                current_external_component_predicate(),
            )
        ):
            required_external_component_ids_by_item[int(order_item_id)].add(
                int(component_id)
            )
        for order_item_id, component_id, task_status in db.execute(
            select(
                ProductionTask.order_item_id,
                ProductionTask.sales_order_item_bom_component_id,
                ProductionTask.status,
            ).where(ProductionTask.order_item_id.in_(item_id_chunk))
        ):
            task_statuses_by_item[int(order_item_id)][
                int(component_id) if component_id is not None else None
            ] = str(task_status)
        for order_item_id, credited_quantity in db.execute(
            select(
                InventoryReservation.order_item_id,
                func.coalesce(
                    func.sum(
                        func.coalesce(
                            InventoryReservation.credited_requirement_quantity,
                            0,
                        )
                        - InventoryReservation.released_requirement_quantity
                    ),
                    0,
                ),
            )
            .where(
                InventoryReservation.order_item_id.in_(item_id_chunk),
                InventoryReservation.reservation_type == "finished_order",
                InventoryReservation.sales_order_item_bom_component_id.is_(None),
                InventoryReservation.status != "cancelled",
            )
            .group_by(InventoryReservation.order_item_id)
        ):
            if order_item_id is not None:
                finished_coverage_by_item[int(order_item_id)] = max(
                    int(credited_quantity or 0), 0
                )
        semi_requirement_rows = db.execute(
            select(
                OrderItemSemiRequirement.id,
                OrderItemSemiRequirement.order_item_id,
                OrderItemSemiRequirement.required_piece_quantity,
            ).where(OrderItemSemiRequirement.order_item_id.in_(item_id_chunk))
        ).all()
        requirement_ids = [int(requirement_id) for requirement_id, *_rest in semi_requirement_rows]
        for requirement_id, order_item_id, required_piece_quantity in semi_requirement_rows:
            semi_requirement_quantity_by_id[int(requirement_id)] = (
                int(order_item_id),
                max(int(required_piece_quantity or 0), 0),
            )
        if requirement_ids:
            for requirement_id, credited_quantity in db.execute(
                select(
                    InventoryReservation.semi_requirement_id,
                    func.coalesce(
                        func.sum(
                            func.coalesce(
                                InventoryReservation.credited_requirement_quantity,
                                0,
                            )
                            - InventoryReservation.consumed_requirement_quantity
                            - InventoryReservation.released_requirement_quantity
                        ),
                        0,
                    ),
                )
                .where(
                    InventoryReservation.semi_requirement_id.in_(requirement_ids),
                    InventoryReservation.reservation_type == "semi_order",
                    InventoryReservation.status.in_(("active", "partial")),
                )
                .group_by(InventoryReservation.semi_requirement_id)
            ).all():
                if requirement_id is not None:
                    semi_coverage_by_requirement_id[int(requirement_id)] = max(
                        int(credited_quantity or 0), 0
                    )
        if include_delivery:
            for delivery_item_id, order_item_id, delivery_id, quantity in db.execute(
                select(
                    DeliveryItem.id,
                    DeliveryItem.order_item_id,
                    Delivery.id,
                    DeliveryItem.delivered_quantity,
                )
                .join(Delivery, Delivery.id == DeliveryItem.delivery_id)
                .where(
                    DeliveryItem.order_item_id.in_(item_id_chunk),
                    DeliveryItem.is_current.is_(True),
                    Delivery.status == "dispatched",
                )
            ):
                dispatched_rows_by_item[int(order_item_id)].append(
                    (int(delivery_item_id), int(delivery_id), int(quantity or 0))
                )

    dispatched_delivery_item_ids = sorted(
        {
            delivery_item_id
            for rows in dispatched_rows_by_item.values()
            for delivery_item_id, _delivery_id, _quantity in rows
        }
    )
    confirmed_receipt_by_delivery_item: dict[int, dict] = {}
    for delivery_item_chunk in _chunks(dispatched_delivery_item_ids):
        for (
            delivery_item_id,
            receipt_item_id,
            actual_received_quantity,
            resolution_action,
        ) in db.execute(
            select(
                ReturnReceiptItem.delivery_item_id,
                ReturnReceiptItem.id,
                ReturnReceiptItem.actual_received_quantity,
                ReturnReceiptItem.resolution_action,
            )
            .join(
                ReturnReceipt,
                ReturnReceipt.id == ReturnReceiptItem.return_receipt_id,
            )
            .where(
                ReturnReceiptItem.delivery_item_id.in_(delivery_item_chunk),
                ReturnReceipt.status == "confirmed",
            )
        ):
            confirmed_receipt_by_delivery_item[int(delivery_item_id)] = {
                "receipt_item_id": int(receipt_item_id),
                "actual_received_quantity": max(
                    int(actual_received_quantity or 0), 0
                ),
                "resolution_action": str(resolution_action or "") or None,
            }

    confirmed_receipt_item_ids = (
        sorted(
            {
                int(row["receipt_item_id"])
                for row in confirmed_receipt_by_delivery_item.values()
            }
        )
        if include_finance
        else []
    )
    statement_id_by_receipt_item: dict[int, int] = {}
    for receipt_item_chunk in _chunks(confirmed_receipt_item_ids):
        for receipt_item_id, statement_id in db.execute(
            select(
                StatementItem.return_receipt_item_id,
                StatementItem.statement_id,
            ).where(StatementItem.return_receipt_item_id.in_(receipt_item_chunk))
        ):
            statement_id_by_receipt_item[int(receipt_item_id)] = int(statement_id)

    statement_ids = sorted(set(statement_id_by_receipt_item.values()))
    statement_totals: dict[int, Decimal] = {}
    cached_invoice_totals: dict[int, Decimal] = {}
    cached_settlement_totals: dict[int, Decimal] = {}
    invoice_totals: dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
    settlement_totals: dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
    for statement_chunk in _chunks(statement_ids):
        for (
            statement_id,
            total_receivable,
            invoiced_amount,
            settled_amount,
        ) in db.execute(
            select(
                Statement.id,
                Statement.total_receivable,
                Statement.invoiced_amount,
                Statement.settled_amount,
            ).where(Statement.id.in_(statement_chunk))
        ):
            statement_totals[int(statement_id)] = Decimal(str(total_receivable or 0))
            cached_invoice_totals[int(statement_id)] = Decimal(
                str(invoiced_amount or 0)
            )
            cached_settlement_totals[int(statement_id)] = Decimal(
                str(settled_amount or 0)
            )
        for statement_id, amount in db.execute(
            select(
                Invoice.statement_id,
                func.coalesce(func.sum(Invoice.invoice_amount), 0),
            )
            .where(Invoice.statement_id.in_(statement_chunk))
            .group_by(Invoice.statement_id)
        ):
            invoice_totals[int(statement_id)] = Decimal(str(amount or 0))
        for statement_id, amount in db.execute(
            select(
                SettlementRecord.statement_id,
                func.coalesce(func.sum(SettlementRecord.settled_amount), 0),
            )
            .where(SettlementRecord.statement_id.in_(statement_chunk))
            .group_by(SettlementRecord.statement_id)
        ):
            settlement_totals[int(statement_id)] = Decimal(str(amount or 0))

    semi_requirement_ids_by_item: dict[int, list[int]] = defaultdict(list)
    for requirement_id, (order_item_id, _required_piece_quantity) in (
        semi_requirement_quantity_by_id.items()
    ):
        semi_requirement_ids_by_item[order_item_id].append(requirement_id)
    fully_reserved_semi_item_ids = {
        order_item_id
        for order_item_id, requirement_ids in semi_requirement_ids_by_item.items()
        if requirement_ids
        and all(
            semi_coverage_by_requirement_id.get(requirement_id, 0)
            >= semi_requirement_quantity_by_id[requirement_id][1]
            for requirement_id in requirement_ids
        )
    }

    item_projection: dict[int, dict] = {}
    for item in items:
        item_id = int(item.id)
        quantity = max(int(item.quantity or 0), 0)
        dispatched_rows = dispatched_rows_by_item.get(item_id, [])
        delivered_quantity = sum(
            int(
                confirmed_receipt_by_delivery_item.get(
                    delivery_item_id,
                    {"actual_received_quantity": delivered_row_quantity},
                )["actual_received_quantity"]
            )
            for (
                delivery_item_id,
                _delivery_id,
                delivered_row_quantity,
            ) in dispatched_rows
        )
        accepted_short = any(
            confirmed_receipt_by_delivery_item.get(
                delivery_item_id, {}
            ).get("resolution_action")
            == "accept_short"
            for delivery_item_id, _delivery_id, _quantity in dispatched_rows
        )
        delivery_obligation_closed = bool(
            dispatched_rows and (bool(item.is_force_closed) or accepted_short)
        )
        remaining_quantity = (
            0
            if delivery_obligation_closed
            else max(quantity - delivered_quantity, 0)
        )
        required_components = required_component_ids_by_item.get(item_id, set())
        tasks = task_statuses_by_item.get(item_id, {})
        if required_components:
            production_ready = all(
                tasks.get(component_id) in READY_PRODUCTION_STATUSES
                for component_id in required_components
            )
        else:
            production_ready = tasks.get(None) in READY_PRODUCTION_STATUSES
        finished_coverage = finished_coverage_by_item.get(item_id, 0)
        from app.services.liner_direct_delivery import liner_direct_coverage
        direct_liner = liner_direct_coverage(
            db,
            item,
            product=products_by_id.get(int(item.product_id))
            if item.product_id is not None
            else None,
        )
        production_ready = production_ready or direct_liner > 0
        legacy_taskless_delivery_ready = bool(
            not required_components
            and not tasks
            and item.supply_mode_snapshot == "corrugated_production"
            and item.material_status == "received"
        )
        production_ready = (
            production_ready
            or (
                not required_components
                and item_id in posted_completion_item_ids
            )
            or (quantity > 0 and finished_coverage >= quantity)
            # Delivery intentionally keeps this bridge for received lines
            # created before persistent production tasks existed.  Describe
            # the same eligibility here so those rows do not claim to need a
            # production task that can never exist.
            or legacy_taskless_delivery_ready
        )
        has_actual_incoming = (
            item.material_status == "received"
            or item_id in closed_incoming_item_ids
        )
        has_formal_requisition = item_id in confirmed_supplier_item_ids
        fully_reserved_semi = item_id in fully_reserved_semi_item_ids
        required_external_components = required_external_component_ids_by_item.get(
            item_id, set()
        )
        purchased_external_components = confirmed_external_component_ids_by_item.get(
            item_id, set()
        )
        received_external_components = received_external_component_ids_by_item.get(
            item_id, set()
        )
        has_external_requirement = bool(required_external_components)
        external_purchase_complete = bool(
            has_external_requirement
            and required_external_components.issubset(purchased_external_components)
        )
        external_receipt_complete = bool(
            external_purchase_complete
            and required_external_components.issubset(received_external_components)
        )

        if delivered_quantity > 0 and remaining_quantity > 0:
            status = "partially_delivered"
            evidence = _evidence(
                "confirmed_delivery_partial",
                "已有正式送货，仍有待送数量",
                dispatched_document_count=len(
                    {delivery_id for _line_id, delivery_id, _qty in dispatched_rows}
                ),
            )
        elif (
            delivered_quantity > 0 and remaining_quantity == 0
        ) or delivery_obligation_closed:
            delivery_item_ids = {row[0] for row in dispatched_rows}
            confirmed_delivery_item_ids = set(
                confirmed_receipt_by_delivery_item
            ).intersection(delivery_item_ids)
            if not delivery_item_ids or confirmed_delivery_item_ids != delivery_item_ids:
                status = "waiting_receipt"
                evidence = _evidence(
                    "confirmed_delivery",
                    "已正式送货，等待客户回单",
                    dispatched_document_count=len(
                        {
                            delivery_id
                            for _line_id, delivery_id, _qty in dispatched_rows
                        }
                    ),
                )
            else:
                receipt_item_ids = {
                    int(
                        confirmed_receipt_by_delivery_item[delivery_item_id][
                            "receipt_item_id"
                        ]
                    )
                    for delivery_item_id in delivery_item_ids
                }
                statement_receipt_item_ids = set(
                    statement_id_by_receipt_item
                ).intersection(receipt_item_ids)
                if statement_receipt_item_ids != receipt_item_ids:
                    status = "pending_reconciliation"
                    evidence = _evidence(
                        "confirmed_receipt",
                        "客户回单已确认，等待对账",
                        receipt_item_count=len(receipt_item_ids),
                    )
                else:
                    related_statement_ids = {
                        statement_id_by_receipt_item[receipt_item_id]
                        for receipt_item_id in receipt_item_ids
                    }
                    pending_invoice = False
                    pending_payment = False
                    for statement_id in related_statement_ids:
                        receivable = statement_totals.get(
                            statement_id, Decimal("0")
                        )
                        invoiced = max(
                            invoice_totals.get(statement_id, Decimal("0")),
                            cached_invoice_totals.get(
                                statement_id, Decimal("0")
                            ),
                        )
                        settled = max(
                            settlement_totals.get(statement_id, Decimal("0")),
                            cached_settlement_totals.get(
                                statement_id, Decimal("0")
                            ),
                        )
                        if receivable > 0 and invoiced < receivable:
                            pending_invoice = True
                        elif receivable > 0 and settled < receivable:
                            pending_payment = True
                    if pending_invoice:
                        status = "pending_invoice"
                        evidence = _evidence(
                            "statement_not_fully_invoiced",
                            "已对账，等待开票",
                            statement_count=len(related_statement_ids),
                        )
                    elif pending_payment:
                        status = "pending_payment"
                        evidence = _evidence(
                            "statement_invoiced_not_settled",
                            "已开票，等待结款",
                            statement_count=len(related_statement_ids),
                        )
                    else:
                        status = "completed"
                        evidence = _evidence(
                            "statement_settled",
                            "对应对账范围已完成结款",
                            statement_count=len(related_statement_ids),
                        )
        elif item.supply_mode_snapshot == "external_purchase":
            if external_receipt_complete:
                status = "pending_delivery"
                evidence = _evidence(
                    "external_packaging_received",
                    "纯外购包材已收齐，等待送货",
                    external_component_count=len(required_external_components),
                )
            elif external_purchase_complete:
                status = "pending_incoming"
                evidence = _evidence(
                    "confirmed_external_packaging_purchase",
                    "正式外购包材采购已保存，等待实收",
                    external_component_count=len(required_external_components),
                    purchased_external_component_count=len(
                        purchased_external_components
                    ),
                    received_external_component_count=len(
                        received_external_components
                    ),
                )
            else:
                status = "pending_material"
                evidence = _evidence(
                    "no_confirmed_external_packaging_purchase",
                    "尚无有效正式外购包材采购",
                )
        elif has_external_requirement:
            if not has_formal_requisition or not external_purchase_complete:
                status = "pending_material"
                evidence = _evidence(
                    "mixed_supply_not_fully_requisitioned",
                    "纸板报料与外购包材采购尚未全部完成",
                    has_formal_supplier_requisition=has_formal_requisition,
                    external_purchase_complete=external_purchase_complete,
                )
            elif not has_actual_incoming or not external_receipt_complete:
                status = "pending_incoming"
                evidence = _evidence(
                    "mixed_supply_waiting_incoming",
                    "纸板与外购包材尚未全部实收",
                    has_actual_supplier_incoming=has_actual_incoming,
                    external_receipt_complete=external_receipt_complete,
                )
            elif production_ready:
                status = "pending_delivery"
                if legacy_taskless_delivery_ready:
                    evidence = _evidence(
                        "legacy_taskless_delivery_compatibility",
                        "历史订单无持久生产任务，沿用现有送货兼容资格",
                        legacy_taskless_delivery_ready=True,
                    )
                else:
                    evidence = _evidence(
                        "production_or_finished_inventory_ready",
                        "生产已完成或成品库存已足额覆盖",
                        finished_inventory_coverage=finished_coverage,
                        legacy_taskless_delivery_ready=False,
                    )
            else:
                status = "pending_production"
                evidence = _evidence(
                    "mixed_supply_received",
                    "纸板与外购包材均已实收，等待生产确认",
                )
        elif production_ready:
            status = "pending_delivery"
            if legacy_taskless_delivery_ready:
                evidence = _evidence(
                    "legacy_taskless_delivery_compatibility",
                    "历史订单无持久生产任务，沿用现有送货兼容资格",
                    legacy_taskless_delivery_ready=True,
                )
            else:
                evidence = _evidence(
                    "production_or_finished_inventory_ready",
                    "生产已完成或成品库存已足额覆盖",
                    finished_inventory_coverage=finished_coverage,
                    legacy_taskless_delivery_ready=False,
                )
        elif fully_reserved_semi:
            status = "pending_production"
            evidence = _evidence(
                "fully_reserved_semi_finished_inventory",
                "半成品库存已全额抵扣，等待生产确认",
                semi_requirement_count=len(
                    semi_requirement_ids_by_item.get(item_id, [])
                ),
            )
        elif has_actual_incoming:
            status = "pending_production"
            evidence = _evidence(
                "actual_incoming_received",
                "来料已实收，等待生产确认",
            )
        elif has_formal_requisition:
            status = "pending_incoming"
            evidence = _evidence(
                "confirmed_supplier_requisition",
                "正式供应商报料已保存，等待实收",
            )
        else:
            status = "pending_material"
            evidence = _evidence(
                "no_confirmed_supplier_requisition",
                "尚无有效正式供应商报料",
            )
        persisted_delivered_quantity = max(
            int(item.delivered_quantity or 0), 0
        )
        if persisted_delivered_quantity != delivered_quantity:
            evidence = {
                **evidence,
                "delivery_counter_mismatch": {
                    "persisted_quantity": persisted_delivered_quantity,
                    "formal_effective_quantity": delivered_quantity,
                },
            }

        item_projection[item_id] = {
            "business_status": status,
            "business_status_label": status_label(status),
            "business_status_evidence": evidence,
            "business_delivered_quantity": delivered_quantity,
            "business_remaining_quantity": remaining_quantity,
        }

    result: dict[int, dict] = {}
    for order in orders:
        order_items = list(order.items)
        item_rows = [item_projection[int(item.id)] for item in order_items]
        total_quantity = sum(max(int(item.quantity or 0), 0) for item in order_items)
        total_delivered = sum(
            item_projection[int(item.id)]["business_delivered_quantity"]
            for item in order_items
        )
        total_remaining = sum(
            item_projection[int(item.id)]["business_remaining_quantity"]
            for item in order_items
        )
        if order.status in MANAGEMENT_ORDER_STATUSES:
            order_status = order.status
            order_evidence = _evidence(
                "explicit_management_status",
                "受控管理状态",
            )
            for item in order_items:
                item_projection[int(item.id)] = {
                    **item_projection[int(item.id)],
                    "business_status": order_status,
                    "business_status_label": status_label(order_status),
                    "business_status_evidence": order_evidence,
                }
            item_rows = [item_projection[int(item.id)] for item in order_items]
        else:
            order_status = aggregate_business_status(
                [row["business_status"] for row in item_rows],
                delivered_quantity=total_delivered,
                remaining_quantity=total_remaining,
            )
            order_evidence = _evidence(
                "item_status_aggregate",
                "按各明细最早阻塞阶段聚合",
            )
        status_counts = Counter(row["business_status"] for row in item_rows)
        delivered_sequences = sorted(
            {
                int(item.item_sequence)
                for item in order_items
                if item.item_sequence is not None
                and item_projection[int(item.id)]["business_delivered_quantity"] > 0
            }
        )
        result[int(order.id)] = {
            "business_status": order_status,
            "business_status_label": status_label(order_status),
            "business_status_evidence": {
                **order_evidence,
                "item_status_counts": dict(status_counts),
            },
            "business_delivery_progress": (
                {
                    "delivered_quantity": total_delivered,
                    "total_quantity": total_quantity,
                    "item_sequences": delivered_sequences,
                }
                if total_delivered > 0
                else None
            ),
            "business_item_status_counts": dict(status_counts),
            "items": {
                int(item.id): item_projection[int(item.id)] for item in order_items
            },
        }
    return result
