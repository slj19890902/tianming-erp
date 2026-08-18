from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.core.time_contract import (
    beijing_now_naive,
    utc_naive_to_api,
    utc_naive_to_beijing_date,
    utc_now_naive,
)
from app.models.customer import Customer
from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem
from app.models.order import Order, OrderItem
from app.models.product_bom import (
    RequisitionItemBomSource,
    SalesOrderItemBomComponent,
)
from app.models.requisition import Requisition, RequisitionItem
from app.models.stock_replenishment import (
    StockReplenishmentOrder,
    StockReplenishmentOrderItem,
)
from app.models.supplier_requisition_order import (
    SupplierRequisitionOrder,
    SupplierRequisitionOrderItem,
)
from app.models.user import User
from app.models.warehouse_inventory import InventoryLot, InventoryMovement
from app.services.production_workflow import (
    ProductionWorkflowError,
    has_production_completion_facts,
    is_production_task_status_quantity_conflict,
    lock_order_rows_for_production_transition,
    refresh_order_production_status,
    refresh_production_task,
)
from app.services.requisition_quantities import purchase_sheet_quantity
from app.services.stock_replenishment import (
    StockReplenishmentError,
    receive_replenishment_item,
)
from app.services.semi_finished_inventory import (
    active_semi_reserved_piece_qty,
)
from app.services.warehouse_inventory import (
    WarehouseInventoryError,
    active_finished_reserved_qty,
    component_effective_required_piece_qty,
    component_inventory_coverage,
    manual_semi_finished_in,
    mutate_lot,
)
from app.services.audit_log import append_audit_event


class IncomingReceiptError(ValueError):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class IncomingTarget:
    item_key: str
    order: Order
    order_item: OrderItem
    requisition_item: RequisitionItem | None
    planned_quantity: int
    component_type: str
    bom_snapshot: SalesOrderItemBomComponent | None = None
    supplier_order: SupplierRequisitionOrder | None = None
    supplier_order_item: SupplierRequisitionOrderItem | None = None


def _stock_item_id(item_key: int | str) -> int | None:
    text = str(item_key)
    return int(text[2:]) if text.startswith("sr") and text[2:].isdigit() else None


def supplier_order_item_key(item_id: int) -> str:
    return f"so{int(item_id)}"


def _supplier_order_item_id(item_key: int | str) -> int | None:
    text = str(item_key)
    return int(text[2:]) if text.startswith("so") and text[2:].isdigit() else None


def supplier_order_item_component_type(
    item: SupplierRequisitionOrderItem,
) -> str:
    source_tail = str(item.source_key or "").strip().lower().rsplit(":", 1)[-1]
    if source_tail in {"whole", "cover", "base"}:
        return source_tail
    return _component_kind(item.product_name)


def current_supplier_order_items(
    db: Session,
    order_item_ids: list[int],
) -> list[SupplierRequisitionOrderItem]:
    """Return physical lines from each order item's current confirmed SRO.

    The sales order item stores the immutable supplier order number selected by
    the requisition workflow.  Matching by that number prevents an older or
    voided supplier order from silently becoming receivable again.
    """

    if not order_item_ids:
        return []
    return list(
        db.scalars(
            select(SupplierRequisitionOrderItem)
            .join(
                SupplierRequisitionOrder,
                SupplierRequisitionOrder.id
                == SupplierRequisitionOrderItem.supplier_order_id,
            )
            .join(
                OrderItem,
                (OrderItem.id == SupplierRequisitionOrderItem.order_item_id)
                & (
                    OrderItem.supplier_order_number
                    == SupplierRequisitionOrder.order_number
                ),
            )
            .where(
                OrderItem.id.in_(order_item_ids),
                SupplierRequisitionOrder.status == "confirmed",
            )
            .order_by(
                SupplierRequisitionOrderItem.order_item_id,
                SupplierRequisitionOrderItem.id,
            )
        ).all()
    )


def _stock_target(
    db: Session,
    item_key: int | str,
    *,
    allow_closed: bool = False,
    claim_for_receipt: bool = False,
) -> tuple[StockReplenishmentOrder, StockReplenishmentOrderItem]:
    item_id = _stock_item_id(item_key)
    if item_id is None:
        raise IncomingReceiptError("补库来料明细ID无效")
    row = db.execute(
        select(StockReplenishmentOrderItem, StockReplenishmentOrder)
        .join(
            StockReplenishmentOrder,
            StockReplenishmentOrder.id
            == StockReplenishmentOrderItem.replenishment_order_id,
        )
        .where(StockReplenishmentOrderItem.id == item_id)
    ).one_or_none()
    if row is None:
        raise IncomingReceiptError("补库来料明细不存在", 404)
    item, order = row
    if not allow_closed and order.status not in {"confirmed", "partially_stocked"}:
        raise IncomingReceiptError(
            "该补库明细当前不可收货，可能已入库或已作废", 409
        )
    if not allow_closed and int(item.stocked_quantity or 0) >= int(item.quantity or 0):
        raise IncomingReceiptError("该补库明细已经全部入库", 409)
    if claim_for_receipt:
        claim = db.execute(
            update(StockReplenishmentOrder)
            .where(
                StockReplenishmentOrder.id == order.id,
                StockReplenishmentOrder.status.in_(
                    ("confirmed", "partially_stocked")
                ),
            )
            .values(status=StockReplenishmentOrder.status)
            .execution_options(synchronize_session=False)
        )
        if claim.rowcount != 1:
            db.expire_all()
            current = db.get(StockReplenishmentOrder, order.id)
            if current is None:
                raise IncomingReceiptError("补库来料单不存在", 404)
            raise IncomingReceiptError(
                "该补库明细当前不可收货，可能已入库或已作废", 409
            )
        # 竞争事务可能刚完成分批收货；锁定状态后必须重新读取最新累计数量。
        db.expire_all()
        refreshed = db.execute(
            select(StockReplenishmentOrderItem, StockReplenishmentOrder)
            .join(
                StockReplenishmentOrder,
                StockReplenishmentOrder.id
                == StockReplenishmentOrderItem.replenishment_order_id,
            )
            .where(StockReplenishmentOrderItem.id == item_id)
            .execution_options(populate_existing=True)
        ).one_or_none()
        if refreshed is None:
            raise IncomingReceiptError("补库来料明细不存在", 404)
        item, order = refreshed
        if order.status not in {"confirmed", "partially_stocked"}:
            raise IncomingReceiptError(
                "该补库明细当前不可收货，可能已入库或已作废", 409
            )
        if int(item.stocked_quantity or 0) >= int(item.quantity or 0):
            raise IncomingReceiptError("该补库明细已经全部入库", 409)
    return order, item


def _stock_source_filter(item_id: int):
    return IncomingReceiptItem.stock_replenishment_item_id == item_id


def stock_source_summary(
    db: Session,
    item_key: int | str,
    *,
    allow_closed: bool = True,
) -> dict:
    _order, item = _stock_target(db, item_key, allow_closed=allow_closed)
    received = int(
        db.scalar(
            select(
                func.coalesce(func.sum(IncomingReceiptItem.received_quantity), 0)
            ).where(
                _stock_source_filter(item.id),
                IncomingReceiptItem.status == "posted",
            )
        )
        or 0
    )
    latest = db.scalar(
        select(IncomingReceiptItem)
        .where(
            _stock_source_filter(item.id),
            IncomingReceiptItem.status == "posted",
        )
        .order_by(IncomingReceiptItem.id.desc())
    )
    planned = int(item.quantity or 0)
    variance = received - planned
    return {
        "planned_quantity": planned,
        "cumulative_received_quantity": received,
        "remaining_quantity": max(planned - received, 0),
        "variance_quantity": variance,
        "variance_type": (
            "matched" if variance == 0 else "short" if variance < 0 else "over"
        ),
        "resolution_status": latest.resolution_status if latest else "not_required",
        "resolution_action": latest.resolution_action if latest else None,
        "pending_receipt_item_id": (
            latest.id
            if latest
            and latest.resolution_status == "pending"
            and latest.resolution_action == "await_supplier"
            else None
        ),
        "latest_receipt_item_id": latest.id if latest else None,
        "latest_receipt_id": latest.receipt_id if latest else None,
        "received_inventory_lot_id": (
            latest.received_inventory_lot_id if latest else None
        ),
        "surplus_inventory_lot_id": None,
    }


def _number(prefix: str) -> str:
    return f"{prefix}-{beijing_now_naive():%Y%m%d}-{uuid4().hex[:10].upper()}"


def _component_kind(name: str | None) -> str:
    value = str(name or "")
    if value.endswith("-底"):
        return "base"
    if value.endswith("-盖"):
        return "cover"
    return "whole"


def _is_telescoping_lid_box(box_style: str | None) -> bool:
    value = (box_style or "").strip().upper()
    return bool(value) and ("天地盖" in value or "A3" in value)


def _snapshot_component_types(
    snapshot: SalesOrderItemBomComponent,
) -> tuple[str, ...]:
    if _is_telescoping_lid_box(snapshot.snapshot_component_box_style):
        return ("cover", "base")
    return ("whole",)


def _snapshot_physical_pieces(
    snapshot: SalesOrderItemBomComponent,
    component_type: str,
) -> int:
    if component_type in {"cover", "base"}:
        return 1
    frozen = int(snapshot.snapshot_component_pieces_per_box or 0)
    if frozen > 0:
        return frozen
    return (
        2
        if (snapshot.snapshot_component_splice_mode or "").strip().lower()
        == "double"
        else 1
    )


def _is_set_only_a3_surround_bom(
    snapshots: list[SalesOrderItemBomComponent],
) -> bool:
    if len(snapshots) != 2:
        return False
    if any(
        Decimal(row.quantity_per_set or 0) != Decimal("1")
        for row in snapshots
    ):
        return False
    a3 = [
        row
        for row in snapshots
        if _is_telescoping_lid_box(row.snapshot_component_box_style)
    ]
    surrounds = [
        row
        for row in snapshots
        if (
            (row.snapshot_component_box_style or "").strip()
            in {"围板", "围套"}
            or "围板" in (row.snapshot_component_product_name or "")
        )
    ]
    return (
        len(a3) == 1
        and len(surrounds) == 1
        and a3[0].id != surrounds[0].id
    )


def _parent_inventory_fully_covers(
    db: Session,
    item: OrderItem,
) -> bool:
    pieces_per_box = int(item.snapshot_pieces_per_box or 0)
    if pieces_per_box <= 0:
        pieces_per_box = (
            2
            if (item.snapshot_splice_mode or "").strip().lower() == "double"
            else 1
        )
    finished = active_finished_reserved_qty(db, item.id)
    required = max(int(item.quantity or 0) - finished, 0) * pieces_per_box
    semi = active_semi_reserved_piece_qty(
        db,
        order_item_id=item.id,
        component_type="whole",
    )
    return required <= semi


def _all_expected_bom_sources_received(
    db: Session,
    item: OrderItem,
) -> bool | None:
    """Return None for non-BOM items, otherwise exact physical-source closure."""
    snapshots = db.scalars(
        select(SalesOrderItemBomComponent)
        .where(SalesOrderItemBomComponent.sales_order_item_id == item.id)
        .order_by(
            SalesOrderItemBomComponent.display_order,
            SalesOrderItemBomComponent.id,
        )
    ).all()
    if not snapshots:
        return None

    expected: set[tuple[int, str]] = set()
    for snapshot in snapshots:
        for component_type in _snapshot_component_types(snapshot):
            required = (
                component_effective_required_piece_qty(db, snapshot)
                * _snapshot_physical_pieces(snapshot, component_type)
            )
            coverage = component_inventory_coverage(
                db,
                snapshot.id,
                component_type=component_type,
            )
            if int(coverage["total_piece_quantity"]) < required:
                expected.add((snapshot.id, component_type))

    received_rows = db.execute(
        select(
            RequisitionItemBomSource.sales_order_item_bom_component_id,
            RequisitionItemBomSource.component_type,
        )
        .join(
            RequisitionItem,
            RequisitionItem.id
            == RequisitionItemBomSource.requisition_item_id,
        )
        .where(
            RequisitionItem.order_item_id == item.id,
            RequisitionItem.status == "已入库",
        )
    ).all()
    received = {
        (int(snapshot_id), component_type)
        for snapshot_id, component_type in received_rows
    }
    for snapshot_id, component_type in expected:
        if (snapshot_id, component_type) in received:
            continue
        # Historical A3 facts used one `whole` source before physical split.
        if (
            component_type in {"cover", "base"}
            and (snapshot_id, "whole") in received
        ):
            continue
        return False

    parent_required = (
        not _is_set_only_a3_surround_bom(snapshots)
        and not _parent_inventory_fully_covers(db, item)
    )
    if parent_required:
        linked_source = (
            select(RequisitionItemBomSource.id)
            .where(
                RequisitionItemBomSource.requisition_item_id
                == RequisitionItem.id
            )
            .exists()
        )
        parent_received = db.scalar(
            select(RequisitionItem.id)
            .where(
                RequisitionItem.order_item_id == item.id,
                RequisitionItem.status == "已入库",
                ~linked_source,
            )
            .limit(1)
        )
        if parent_received is None:
            return False
    return True


def _uses_confirmed_supplier_order(db: Session, order_item: OrderItem) -> bool:
    if not order_item.supplier_order_number:
        return False
    return db.scalar(
        select(SupplierRequisitionOrderItem.id)
        .join(
            SupplierRequisitionOrder,
            SupplierRequisitionOrder.id
            == SupplierRequisitionOrderItem.supplier_order_id,
        )
        .where(
            SupplierRequisitionOrder.order_number
            == order_item.supplier_order_number,
            SupplierRequisitionOrder.status == "confirmed",
            SupplierRequisitionOrderItem.order_item_id == order_item.id,
        )
        .limit(1)
    ) is not None


def _latest_supplier_requisition_id(
    db: Session, order_item_id: int
) -> int | None:
    return db.scalar(
        select(func.max(RequisitionItem.requisition_id))
        .join(Requisition, Requisition.id == RequisitionItem.requisition_id)
        .where(
            RequisitionItem.order_item_id == order_item_id,
            Requisition.status == "supplier_requisition_created",
        )
    )


def _requisition_can_receive(
    db: Session,
    requisition_item: RequisitionItem,
    order_item: OrderItem,
) -> bool:
    if _uses_confirmed_supplier_order(db, order_item):
        return (
            requisition_item.status == "supplier_requisition_created"
            and requisition_item.requisition_id
            == _latest_supplier_requisition_id(db, order_item.id)
        )
    return requisition_item.status == "有效"


def _open_requisition_status(db: Session, row: RequisitionItem) -> str:
    requisition_status = db.scalar(
        select(Requisition.status).where(Requisition.id == row.requisition_id)
    )
    return (
        "supplier_requisition_created"
        if requisition_status == "supplier_requisition_created"
        else "有效"
    )


def _target(
    db: Session, item_key: int | str, *, allow_closed: bool = False
) -> IncomingTarget:
    text = str(item_key)
    supplier_item_id = _supplier_order_item_id(text)
    if supplier_item_id is not None:
        row = db.execute(
            select(
                SupplierRequisitionOrderItem,
                SupplierRequisitionOrder,
                OrderItem,
                Order,
            )
            .join(
                SupplierRequisitionOrder,
                SupplierRequisitionOrder.id
                == SupplierRequisitionOrderItem.supplier_order_id,
            )
            .join(
                OrderItem,
                OrderItem.id == SupplierRequisitionOrderItem.order_item_id,
            )
            .join(Order, Order.id == OrderItem.order_id)
            .where(SupplierRequisitionOrderItem.id == supplier_item_id)
        ).one_or_none()
        if row is None:
            raise IncomingReceiptError("供应商报料明细不存在", 404)
        supplier_item, supplier_order, order_item, order = row
        if (
            supplier_order.status != "confirmed"
            or order_item.supplier_order_number != supplier_order.order_number
        ):
            raise IncomingReceiptError("该供应商报料明细不是当前有效报料单", 409)
        if not allow_closed and (
            order_item.material_status != "pending"
            or order_item.requisition_status not in {"已报料", "供应商已排单"}
        ):
            raise IncomingReceiptError(
                "该明细当前不可入库，可能已入库、已作废或状态已变化", 409
            )
        planned = int(supplier_item.requisition_qty or 0)
        return IncomingTarget(
            item_key=text,
            order=order,
            order_item=order_item,
            requisition_item=None,
            planned_quantity=planned,
            component_type=supplier_order_item_component_type(supplier_item),
            supplier_order=supplier_order,
            supplier_order_item=supplier_item,
        )

    if text.startswith("r") and text[1:].isdigit():
        requisition_item_id = int(text[1:])
        row = db.execute(
            select(RequisitionItem, OrderItem, Order)
            .join(OrderItem, OrderItem.id == RequisitionItem.order_item_id)
            .join(Order, Order.id == OrderItem.order_id)
            .where(RequisitionItem.id == requisition_item_id)
        ).one_or_none()
        if row is None:
            raise IncomingReceiptError("报料明细不存在", 404)
        requisition_item, order_item, order = row
        can_receive = _requisition_can_receive(db, requisition_item, order_item)
        if not can_receive and not (
            allow_closed and requisition_item.status == "已入库"
        ):
            raise IncomingReceiptError("该报料明细当前不可入库", 409)
        if not allow_closed and order_item.material_status != "pending":
            raise IncomingReceiptError(
                "该明细当前不可入库，可能已入库、已作废或状态已变化", 409
            )
        source = db.scalar(
            select(RequisitionItemBomSource).where(
                RequisitionItemBomSource.requisition_item_id
                == requisition_item.id
            )
        )
        bom_snapshot = (
            db.get(
                SalesOrderItemBomComponent,
                source.sales_order_item_bom_component_id,
            )
            if source is not None
            else None
        )
        source_component = str(
            source.component_type if source is not None else ""
        ).strip().lower()
        component_type = (
            source_component
            if source_component in {"whole", "cover", "base"}
            else _component_kind(requisition_item.product_name_snapshot)
        )
        planned = int(requisition_item.requisition_qty or 0)
        return IncomingTarget(
            item_key=text,
            order=order,
            order_item=order_item,
            requisition_item=requisition_item,
            planned_quantity=planned,
            component_type=component_type,
            bom_snapshot=bom_snapshot,
        )

    try:
        order_item_id = int(text)
    except ValueError as error:
        raise IncomingReceiptError("入库明细ID无效") from error
    row = db.execute(
        select(OrderItem, Order)
        .join(Order, Order.id == OrderItem.order_id)
        .where(OrderItem.id == order_item_id)
    ).one_or_none()
    if row is None:
        raise IncomingReceiptError("订单明细不存在", 404)
    order_item, order = row
    if not allow_closed and (
        order_item.material_status != "pending"
        or order_item.requisition_status not in {"已报料", "供应商已排单"}
    ):
        raise IncomingReceiptError(
            "该明细当前不可入库，可能已入库、已作废或状态已变化", 409
        )
    component_rows = db.scalars(
        select(RequisitionItem).where(
            RequisitionItem.order_item_id == order_item.id,
        )
    ).all()
    source_rows = db.execute(
        select(
            RequisitionItemBomSource.requisition_item_id,
            RequisitionItemBomSource.component_type,
        ).where(
            RequisitionItemBomSource.requisition_item_id.in_(
                [row.id for row in component_rows]
            )
        )
    ).all()
    sourced_item_ids = {int(item_id) for item_id, _kind in source_rows}
    source_types = {
        str(kind or "").strip().lower() for _item_id, kind in source_rows
    }
    if (
        source_types.intersection({"cover", "base"})
        or any(
            _component_kind(row.product_name_snapshot) in {"cover", "base"}
            for row in component_rows
            if row.id not in sourced_item_ids
        )
    ):
        raise IncomingReceiptError("天地盖来料必须分别按盖片和底片确认实收", 409)
    current_supplier_lines = current_supplier_order_items(db, [order_item.id])
    if current_supplier_lines:
        if (
            len(current_supplier_lines) == 1
            and supplier_order_item_component_type(current_supplier_lines[0])
            == "whole"
        ):
            supplier_item = current_supplier_lines[0]
            supplier_order = db.get(
                SupplierRequisitionOrder,
                supplier_item.supplier_order_id,
            )
            if supplier_order is not None:
                return IncomingTarget(
                    item_key=text,
                    order=order,
                    order_item=order_item,
                    requisition_item=None,
                    planned_quantity=int(supplier_item.requisition_qty or 0),
                    component_type="whole",
                    supplier_order=supplier_order,
                    supplier_order_item=supplier_item,
                )
        raise IncomingReceiptError(
            "该订单已有正式供应商报料明细，请刷新后按每条报料明细分别收料",
            409,
        )
    planned = int(order_item.requisition_qty or order_item.quantity or 0)
    return IncomingTarget(
        item_key=text,
        order=order,
        order_item=order_item,
        requisition_item=None,
        planned_quantity=planned,
        component_type="whole",
    )


def _source_filter(target: IncomingTarget):
    if target.supplier_order_item is not None:
        return (
            (
                IncomingReceiptItem.supplier_order_item_id
                == target.supplier_order_item.id
            )
            & IncomingReceiptItem.requisition_item_id.is_(None)
        )
    if target.requisition_item is not None:
        return IncomingReceiptItem.requisition_item_id == target.requisition_item.id
    return (
        (IncomingReceiptItem.order_item_id == target.order_item.id)
        & IncomingReceiptItem.requisition_item_id.is_(None)
        & IncomingReceiptItem.supplier_order_item_id.is_(None)
    )


def cumulative_received(db: Session, target: IncomingTarget) -> int:
    return int(
        db.scalar(
            select(func.coalesce(func.sum(IncomingReceiptItem.received_quantity), 0)).where(
                _source_filter(target), IncomingReceiptItem.status == "posted"
            )
        )
        or 0
    )


def source_summary(db: Session, target: IncomingTarget) -> dict:
    received = cumulative_received(db, target)
    planned = target.planned_quantity
    latest = db.scalar(
        select(IncomingReceiptItem)
        .where(_source_filter(target), IncomingReceiptItem.status == "posted")
        .order_by(IncomingReceiptItem.id.desc())
    )
    variance = received - planned
    variance_type = "matched" if variance == 0 else "short" if variance < 0 else "over"
    return {
        "planned_quantity": planned,
        "cumulative_received_quantity": received,
        "remaining_quantity": max(planned - received, 0),
        "variance_quantity": variance,
        "variance_type": variance_type,
        "resolution_status": latest.resolution_status if latest else "not_required",
        "resolution_action": latest.resolution_action if latest else None,
        "pending_receipt_item_id": (
            latest.id
            if latest
            and latest.resolution_status == "pending"
            and latest.resolution_action == "await_supplier"
            else None
        ),
        "latest_receipt_item_id": latest.id if latest else None,
        "latest_receipt_id": latest.receipt_id if latest else None,
        "surplus_inventory_lot_id": latest.surplus_inventory_lot_id if latest else None,
    }


def source_summary_for_item(
    db: Session, item_key: int | str
) -> dict | None:
    if _stock_item_id(item_key) is not None:
        try:
            summary = stock_source_summary(db, item_key)
        except IncomingReceiptError:
            return None
        return summary if summary["latest_receipt_item_id"] is not None else None
    try:
        target = _target(db, item_key, allow_closed=True)
    except IncomingReceiptError:
        return None
    summary = source_summary(db, target)
    return summary if summary["latest_receipt_item_id"] is not None else None


def receipt_history(
    db: Session,
    *,
    received_since: datetime | None = None,
    include_reversed: bool = False,
) -> list[IncomingReceiptItem]:
    query = (
        select(IncomingReceiptItem)
        .options(selectinload(IncomingReceiptItem.receipt))
        .join(IncomingReceipt, IncomingReceipt.id == IncomingReceiptItem.receipt_id)
    )
    if not include_reversed:
        query = query.where(IncomingReceiptItem.status == "posted")
    if received_since is not None:
        query = query.where(IncomingReceipt.received_at >= received_since)
    return list(
        db.scalars(
            query.order_by(IncomingReceipt.received_at.desc(), IncomingReceiptItem.id.desc())
        ).all()
    )


def _validate_decision(
    *,
    planned: int,
    cumulative: int,
    action: str | None,
    reason: str | None,
    surplus_location_id: int | None,
) -> tuple[str, str | None]:
    if planned <= 0:
        raise IncomingReceiptError("原采购张数必须大于0")
    clean_action = (action or "").strip() or None
    clean_reason = (reason or "").strip() or None
    if cumulative == planned:
        if clean_action:
            raise IncomingReceiptError("等量收货不需要选择差异处理方式")
        return "not_required", None
    if cumulative < planned:
        if clean_action not in {"await_supplier", "accept_short"}:
            raise IncomingReceiptError("短收时请选择继续等待补货或按当前数量结单")
        return ("pending" if clean_action == "await_supplier" else "resolved"), clean_action
    if clean_action not in {"all_to_production", "transfer_to_semi_inventory"}:
        raise IncomingReceiptError("超收时请选择全部投入生产或余量转半成品库存")
    if clean_action == "transfer_to_semi_inventory" and not surplus_location_id:
        raise IncomingReceiptError("余量转半成品库存时必须选择库位")
    return "resolved", clean_action


def _supplier_link(
    db: Session, order_item_id: int
) -> tuple[int | None, int | None]:
    order_item = db.get(OrderItem, order_item_id)
    row = db.execute(
        select(SupplierRequisitionOrderItem, SupplierRequisitionOrder)
        .join(
            SupplierRequisitionOrder,
            SupplierRequisitionOrder.id == SupplierRequisitionOrderItem.supplier_order_id,
        )
        .where(
            SupplierRequisitionOrderItem.order_item_id == order_item_id,
            SupplierRequisitionOrder.status == "confirmed",
        )
        .order_by(
            (
                SupplierRequisitionOrder.order_number
                == (order_item.supplier_order_number if order_item else None)
            ).desc(),
            SupplierRequisitionOrderItem.id.desc(),
        )
    ).first()
    if row is None:
        return None, None
    supplier_item, supplier_order = row
    return supplier_order.id, supplier_item.id


def _all_current_supplier_order_items_closed(
    db: Session,
    order_item: OrderItem,
) -> bool:
    supplier_items = current_supplier_order_items(db, [order_item.id])
    if not supplier_items:
        return False
    for supplier_item in supplier_items:
        facts = list(
            db.scalars(
                select(IncomingReceiptItem)
                .where(
                    IncomingReceiptItem.supplier_order_item_id == supplier_item.id,
                    IncomingReceiptItem.requisition_item_id.is_(None),
                    IncomingReceiptItem.status == "posted",
                )
                .order_by(IncomingReceiptItem.id)
            ).all()
        )
        if not facts:
            return False
        received = sum(int(fact.received_quantity or 0) for fact in facts)
        latest = facts[-1]
        if received < int(supplier_item.requisition_qty or 0) and (
            latest.resolution_action != "accept_short"
        ):
            return False
    return True


def _all_required_supplier_demand_requisitioned(
    db: Session,
    order_item: OrderItem,
) -> bool:
    """Return whether every frozen physical source has been fully reported.

    A supplier order can intentionally cover only part of an order item's
    demand.  Receiving every sheet on that one supplier order therefore must
    not close the sales-order item while another report is still required.
    Each partial supplier line keeps the full required-piece snapshot, so the
    active reported quantity can be compared with the maximum frozen target
    for the same physical source without changing any receipt facts.
    """

    rows = list(
        db.scalars(
            select(SupplierRequisitionOrderItem)
            .join(
                SupplierRequisitionOrder,
                SupplierRequisitionOrder.id
                == SupplierRequisitionOrderItem.supplier_order_id,
            )
            .where(
                SupplierRequisitionOrderItem.order_item_id == order_item.id,
                SupplierRequisitionOrder.status != "voided",
            )
            .order_by(SupplierRequisitionOrderItem.id)
        ).all()
    )
    if not rows:
        return False

    source_totals: dict[str, dict[str, int]] = {}
    for row in rows:
        component_type = supplier_order_item_component_type(row)
        source_key = str(row.source_key or "").strip().lower()
        identity = source_key or f"legacy:{component_type}"
        source = source_totals.setdefault(
            identity,
            {"reported": 0, "required": 0, "has_snapshot": 0},
        )
        source["reported"] += max(int(row.requisition_qty or 0), 0)

        required_piece_qty = max(int(row.required_piece_qty or 0), 0)
        if required_piece_qty <= 0:
            continue
        semi_reserved_qty = max(
            int(
                active_semi_reserved_piece_qty(
                    db,
                    order_item_id=order_item.id,
                    component_type=component_type,
                )
                or 0
            ),
            0,
        )
        target = purchase_sheet_quantity(
            max(required_piece_qty - semi_reserved_qty, 0),
            0,
            row.cutting_mode,
        )
        source["required"] = max(source["required"], int(target))
        source["has_snapshot"] = 1

    authoritative_sources = [
        source for source in source_totals.values() if source["has_snapshot"]
    ]
    if not authoritative_sources:
        # Historical supplier lines may not carry a demand snapshot.  Preserve
        # their established closure behaviour instead of inventing a target.
        return True
    return all(
        source["reported"] >= source["required"]
        for source in authoritative_sources
    )


def _mark_order_progress(db: Session, target: IncomingTarget, *, closed: bool, user_id: int) -> None:
    item = target.order_item
    now = utc_now_naive()
    if target.requisition_item is not None:
        target.requisition_item.status = (
            "已入库"
            if closed
            else _open_requisition_status(db, target.requisition_item)
        )
        bom_sources_received = _all_expected_bom_sources_received(db, item)
        if bom_sources_received is None:
            component_rows = db.scalars(
                select(RequisitionItem).where(
                    RequisitionItem.order_item_id == item.id,
                )
            ).all()
            remaining_components = sum(
                _requisition_can_receive(db, component, item)
                for component in component_rows
            )
            closed = closed and remaining_components == 0
        else:
            closed = closed and bom_sources_received
    if target.supplier_order_item is not None:
        closed = (
            closed
            and _all_current_supplier_order_items_closed(db, item)
            and _all_required_supplier_demand_requisitioned(db, item)
        )
    if closed:
        item.material_status = "received"
        item.requisition_status = "已入库"
        item.material_received_at = now
        item.material_received_by = user_id
    else:
        item.material_status = "pending"
        if item.requisition_status == "已入库":
            item.requisition_status = "已报料"
        item.material_received_at = None
        item.material_received_by = None
    db.flush()
    try:
        production_task = refresh_production_task(db, item.id)
    except ProductionWorkflowError as error:
        raise IncomingReceiptError(str(error), error.status_code) from error
    except IntegrityError as error:
        if not is_production_task_status_quantity_conflict(error):
            raise
        raise IncomingReceiptError(
            "实收纸板与生产计划换算结果冲突，本次实收未保存；"
            "请刷新后核对实收数量、开料方式和单双拼设置",
            409,
        ) from error
    if production_task is not None:
        refresh_order_production_status(db, item.order_id)
        return
    remaining_items = int(
        db.scalar(
            select(func.count(OrderItem.id)).where(
                OrderItem.order_id == item.order_id,
                OrderItem.material_status != "received",
            )
        )
        or 0
    )
    if remaining_items == 0 and target.order.status in {"pending_production", "production"}:
        target.order.status = "pending_delivery"
    elif remaining_items > 0 and target.order.status == "pending_delivery":
        target.order.status = "pending_production"


def _surplus_dimensions(target: IncomingTarget) -> tuple[int, int]:
    supplier_item = target.supplier_order_item
    req = target.requisition_item
    item = target.order_item
    length = (
        supplier_item.report_length_mm
        if supplier_item is not None
        else req.cardboard_len
        if req is not None
        else item.cardboard_len
    )
    width = (
        supplier_item.report_width_mm
        if supplier_item is not None
        else req.cardboard_width
        if req is not None
        else item.cardboard_width
    )
    if length is None or width is None:
        raise IncomingReceiptError("缺少纸板报料长宽，不能把超收余量转库存")
    return int(round(Decimal(length))), int(round(Decimal(width)))


def _surplus_crease(
    target: IncomingTarget,
) -> tuple[str | None, str, int | None, int | None, int | None]:
    item = target.order_item
    snapshot = getattr(target, "bom_snapshot", None)
    if snapshot is not None and target.component_type == "base":
        crease_type = snapshot.snapshot_component_base_crease_type
        values = (
            snapshot.snapshot_component_base_crease_left_mm,
            snapshot.snapshot_component_base_crease_middle_mm,
            snapshot.snapshot_component_base_crease_right_mm,
        )
    elif snapshot is not None:
        crease_type = snapshot.snapshot_component_crease_type
        values = (
            snapshot.snapshot_component_crease_left_mm,
            snapshot.snapshot_component_crease_middle_mm,
            snapshot.snapshot_component_crease_right_mm,
        )
    elif target.component_type == "base":
        crease_type = item.snapshot_base_crease_type
        values = (
            item.snapshot_base_crease_left_mm,
            item.snapshot_base_crease_middle_mm,
            item.snapshot_base_crease_right_mm,
        )
    else:
        crease_type = item.snapshot_crease_type
        values = (
            item.snapshot_crease_left_mm,
            item.snapshot_crease_middle_mm,
            item.snapshot_crease_right_mm,
        )
    normalized = (crease_type or "").strip()
    sheet_type = (
        "creased_sheet"
        if normalized == "压线"
        else "net_sheet"
        if normalized in {"净", "净料"}
        else "raw_board"
    )
    return crease_type, sheet_type, *values


def _create_surplus_lot(
    db: Session,
    *,
    target: IncomingTarget,
    receipt_item: IncomingReceiptItem,
    surplus: int,
    location_id: int,
    user_id: int,
    reason: str | None,
    expected_layout_version: int | None = None,
) -> InventoryLot:
    item = target.order_item
    snapshot = getattr(target, "bom_snapshot", None)
    supplier_item = target.supplier_order_item
    length, width = _surplus_dimensions(target)
    crease_type, sheet_type, crease_left, crease_middle, crease_right = (
        _surplus_crease(target)
    )
    material_code = (
        supplier_item.material_code_snapshot
        if supplier_item is not None
        else snapshot.snapshot_component_material
        if snapshot is not None
        else item.snapshot_material
    )
    layer_count = (
        supplier_item.layer_count_snapshot
        if supplier_item is not None
        else snapshot.snapshot_component_layer_count
        if snapshot is not None
        else item.layer_count
    )
    flute_type = (
        supplier_item.flute_type_snapshot
        if supplier_item is not None
        else snapshot.snapshot_component_flute_type
        if snapshot is not None
        else item.flute_type
    )
    supplier_name = (
        supplier_item.supplier_name_snapshot
        if supplier_item is not None
        else snapshot.snapshot_component_supplier_name
        if snapshot is not None
        else item.snapshot_supplier_name
    )
    material_id = (
        supplier_item.material_id
        if supplier_item is not None
        else snapshot.snapshot_component_material_id
        if snapshot is not None
        else item.material_id
    )
    material_code = (material_code or "").strip()
    if not material_code or not layer_count or not flute_type:
        raise IncomingReceiptError("缺少材质、层数或楞型，不能把超收余量转库存")
    try:
        return manual_semi_finished_in(
            db,
            location_id=location_id,
            quantity=surplus,
            stock_date=utc_naive_to_beijing_date(receipt_item.receipt.received_at),
            source_type="purchase_surplus",
            material_code=material_code,
            layer_count=int(layer_count),
            flute_type=flute_type,
            board_length_mm=length,
            board_width_mm=width,
            sheet_type=sheet_type,
            component_type=target.component_type,
            pieces_per_box=int(
                (supplier_item.pieces_per_box if supplier_item is not None else None)
                or (target.requisition_item.pieces_per_box if target.requisition_item else None)
                or item.snapshot_pieces_per_box
                or 1
            ),
            stock_yield_per_sheet=1,
            supplier_name=supplier_name,
            customer_id=target.order.customer_id,
            crease_type=crease_type,
            crease_left_mm=crease_left,
            crease_middle_mm=crease_middle,
            crease_right_mm=crease_right,
            cutting_note="供应商来料超收余量",
            remarks=reason or f"来料实收超出原采购 {surplus} 张",
            operator_id=user_id,
            idempotency_key=f"incoming-surplus:{receipt_item.id}",
            source_ref_type="incoming_receipt_item",
            source_ref_id=receipt_item.id,
            material_id=material_id,
            expected_layout_version=expected_layout_version,
        )
    except WarehouseInventoryError as error:
        raise IncomingReceiptError(str(error), error.status_code) from error


def _audit(
    db: Session,
    *,
    user: User,
    action: str,
    target: IncomingTarget,
    details: dict,
    audit_context: dict[str, object] | None = None,
) -> None:
    context = audit_context or {}
    customer = db.get(Customer, target.order.customer_id)
    receipt_item_id = details.get("incoming_receipt_item_id")
    action_codes = {
        "RECEIVE_MATERIAL": "incoming.receive",
        "RESOLVE_INCOMING_VARIANCE": "incoming.accept_short",
        "REVERT_MATERIAL": "incoming.revert",
    }
    descriptions = {
        "RECEIVE_MATERIAL": "来料实收登记",
        "RESOLVE_INCOMING_VARIANCE": "来料短收结单",
        "REVERT_MATERIAL": "撤回来料入库",
    }
    append_audit_event(
        db,
        request=context.get("request"),
        actor=user,
        event_category="business",
        result="success",
        source=str(context.get("source") or "web"),
        module_code="incoming",
        action_code=action_codes.get(action, f"incoming.{action.lower()}"),
        legacy_action=action,
        resource="IncomingReceiptItem",
        entity_type="incoming_receipt_item",
        entity_id=(
            int(receipt_item_id)
            if isinstance(receipt_item_id, int)
            else target.order_item.id
        ),
        object_ref=(
            f"incoming:{receipt_item_id}"
            if isinstance(receipt_item_id, int)
            else (
                target.order_item.item_order_number
                or f"order_item:{target.order_item.id}"
            )
        ),
        customer_id=target.order.customer_id,
        customer_name=customer.name if customer is not None else None,
        batch_id=(
            str(context["batch_id"])
            if context.get("batch_id") is not None
            else None
        ),
        description=descriptions.get(action, action),
        details=details,
    )


def _idempotent_receipt_item(
    db: Session,
    *,
    receipt: IncomingReceipt,
    item_key: int | str,
    received_quantity: int | None,
    resolution_action: str | None,
    resolution_reason: str | None,
    surplus_location_id: int | None,
) -> IncomingReceiptItem:
    if len(receipt.items) != 1:
        raise IncomingReceiptError("幂等键已用于其他入库操作", 409)
    row = receipt.items[0]
    text = str(item_key)
    requested_stock_item_id = _stock_item_id(text)
    requested_supplier_item_id = _supplier_order_item_id(text)
    requested_requisition_id = (
        int(text[1:])
        if requested_stock_item_id is None
        and requested_supplier_item_id is None
        and text.startswith("r")
        and text[1:].isdigit()
        else None
    )
    try:
        requested_order_item_id = (
            None
            if requested_stock_item_id is not None
            or requested_supplier_item_id is not None
            or requested_requisition_id
            else int(text)
        )
    except ValueError as error:
        raise IncomingReceiptError("入库明细ID无效") from error
    if requested_stock_item_id is not None:
        same_target = (
            row.stock_replenishment_item_id == requested_stock_item_id
            and row.order_item_id is None
        )
    elif requested_supplier_item_id is not None:
        same_target = (
            row.supplier_order_item_id == requested_supplier_item_id
            and row.requisition_item_id is None
            and row.stock_replenishment_item_id is None
        )
    elif requested_requisition_id is not None:
        same_target = row.requisition_item_id == requested_requisition_id
    else:
        compatible_supplier_item_id: int | None = None
        if row.supplier_order_item_id is not None:
            try:
                compatible_target = _target(db, text, allow_closed=True)
            except IncomingReceiptError:
                compatible_target = None
            if compatible_target is not None and (
                compatible_target.supplier_order_item is not None
            ):
                compatible_supplier_item_id = (
                    compatible_target.supplier_order_item.id
                )
        same_target = (
            row.requisition_item_id is None
            and row.stock_replenishment_item_id is None
            and row.order_item_id == requested_order_item_id
            and (
                row.supplier_order_item_id is None
                or row.supplier_order_item_id == compatible_supplier_item_id
            )
        )
    normalized_action = (resolution_action or "").strip() or None
    normalized_reason = (resolution_reason or "").strip() or None
    expected_quantity = (
        row.planned_quantity if received_quantity is None else int(received_quantity)
    )
    same_location = True
    if requested_stock_item_id is not None:
        lot = (
            db.get(InventoryLot, row.received_inventory_lot_id)
            if row.received_inventory_lot_id
            else None
        )
        same_location = lot is not None
    elif normalized_action == "transfer_to_semi_inventory":
        lot = (
            db.get(InventoryLot, row.surplus_inventory_lot_id)
            if row.surplus_inventory_lot_id
            else None
        )
        same_location = bool(
            lot
            and surplus_location_id
            and lot.warehouse_location_id == surplus_location_id
        )
    if not (
        same_target
        and row.received_quantity == expected_quantity
        and row.resolution_action == normalized_action
        and row.resolution_reason == normalized_reason
        and same_location
    ):
        raise IncomingReceiptError("幂等键已用于其他入库操作", 409)
    return row


def _receive_stock_replenishment_one(
    db: Session,
    *,
    user: User,
    item_key: int | str,
    received_quantity: int | None,
    resolution_action: str | None,
    resolution_reason: str | None,
    surplus_location_id: int | None,
    idempotency_key: str,
    audit_context: dict[str, object] | None = None,
) -> IncomingReceiptItem:
    order, item = _stock_target(db, item_key, claim_for_receipt=True)
    planned = int(item.quantity or 0)
    before = int(
        db.scalar(
            select(
                func.coalesce(func.sum(IncomingReceiptItem.received_quantity), 0)
            ).where(
                _stock_source_filter(item.id),
                IncomingReceiptItem.status == "posted",
            )
        )
        or 0
    )
    quantity = planned - before if received_quantity is None else int(received_quantity)
    if quantity <= 0:
        raise IncomingReceiptError("入库数量必须大于0")
    cumulative = before + quantity
    if cumulative > planned:
        raise IncomingReceiptError(
            "补库来料实收不能超过原报料数量；请核对后另建补库单处理超收。",
            409,
        )
    action = (resolution_action or "").strip() or None
    reason = (resolution_reason or "").strip() or None
    if cumulative < planned:
        if action != "await_supplier":
            raise IncomingReceiptError(
                "补库来料短收时请选择“继续等待供应商补货”。"
            )
        resolution_status = "pending"
        variance_type = "short"
    else:
        if action:
            raise IncomingReceiptError("补库来料等量收货不需要选择差异处理方式")
        resolution_status = "not_required"
        variance_type = "matched"
    if surplus_location_id is not None:
        raise IncomingReceiptError("补库片料由系统自动进入原料暂存区，无需另选余量库位")

    now = utc_now_naive()
    receipt = IncomingReceipt(
        receipt_number=_number("IR"),
        status="posted",
        received_at=now,
        received_by=user.id,
        idempotency_key=idempotency_key,
        remarks=reason,
    )
    db.add(receipt)
    db.flush()
    receipt_item = IncomingReceiptItem(
        receipt_id=receipt.id,
        order_id=None,
        order_item_id=None,
        stock_replenishment_item_id=item.id,
        planned_quantity=planned,
        received_quantity=quantity,
        cumulative_received_quantity=cumulative,
        variance_quantity=cumulative - planned,
        variance_type=variance_type,
        resolution_status=resolution_status,
        resolution_action=action,
        resolution_reason=reason,
        status="posted",
    )
    receipt.items.append(receipt_item)
    db.flush()
    try:
        lot = receive_replenishment_item(
            db,
            order=order,
            item=item,
            quantity=quantity,
            operator_id=user.id,
            receipt_item_id=receipt_item.id,
        )
    except StockReplenishmentError as error:
        raise IncomingReceiptError(str(error), error.status_code) from error
    receipt_item.received_inventory_lot_id = lot.id
    context = audit_context or {}
    customer = db.get(Customer, item.customer_id) if item.customer_id is not None else None
    append_audit_event(
        db,
        request=context.get("request"),
        actor=user,
        event_category="business",
        result="success",
        source=str(context.get("source") or "web"),
        module_code="incoming",
        action_code="incoming.stock_replenishment.receive",
        legacy_action="RECEIVE_STOCK_REPLENISHMENT",
        resource="IncomingReceiptItem",
        entity_type="stock_replenishment_item",
        entity_id=item.id,
        object_ref=f"stock_replenishment_item:{item.id}",
        customer_id=item.customer_id,
        customer_name=customer.name if customer is not None else None,
        batch_id=(
            str(context["batch_id"])
            if context.get("batch_id") is not None
            else None
        ),
        description="补库来料实际收货",
        details={
            "incoming_receipt_id": receipt.id,
            "incoming_receipt_item_id": receipt_item.id,
            "stock_replenishment_order_id": order.id,
            "stock_replenishment_item_id": item.id,
            "planned_quantity": planned,
            "received_quantity": quantity,
            "cumulative_received_quantity": cumulative,
            "received_inventory_lot_id": lot.id,
            "receipt_location_id": lot.warehouse_location_id,
        },
    )
    db.flush()
    return receipt_item


def receive_one(
    db: Session,
    *,
    user: User,
    item_key: int | str,
    received_quantity: int | None,
    resolution_action: str | None,
    resolution_reason: str | None,
    surplus_location_id: int | None,
    idempotency_key: str | None,
    expected_surplus_layout_version: int | None = None,
    audit_context: dict[str, object] | None = None,
) -> IncomingReceiptItem:
    key = (idempotency_key or "").strip() or uuid4().hex
    existing_receipt = db.scalar(
        select(IncomingReceipt).where(IncomingReceipt.idempotency_key == key)
    )
    if existing_receipt is not None:
        return _idempotent_receipt_item(
            db,
            receipt=existing_receipt,
            item_key=item_key,
            received_quantity=received_quantity,
            resolution_action=resolution_action,
            resolution_reason=resolution_reason,
            surplus_location_id=surplus_location_id,
        )

    if _stock_item_id(item_key) is not None:
        return _receive_stock_replenishment_one(
            db,
            user=user,
            item_key=item_key,
            received_quantity=received_quantity,
            resolution_action=resolution_action,
            resolution_reason=resolution_reason,
            surplus_location_id=surplus_location_id,
            idempotency_key=key,
            audit_context=audit_context,
        )

    target = _target(db, item_key)
    quantity = int(
        target.planned_quantity if received_quantity is None else received_quantity
    )
    if quantity <= 0:
        raise IncomingReceiptError("入库数量必须大于0")
    before = cumulative_received(db, target)
    cumulative = before + quantity
    resolution_status, action = _validate_decision(
        planned=target.planned_quantity,
        cumulative=cumulative,
        action=resolution_action,
        reason=resolution_reason,
        surplus_location_id=surplus_location_id,
    )
    variance = cumulative - target.planned_quantity
    variance_type = "matched" if variance == 0 else "short" if variance < 0 else "over"
    now = utc_now_naive()
    receipt = IncomingReceipt(
        receipt_number=_number("IR"),
        status="posted",
        received_at=now,
        received_by=user.id,
        idempotency_key=key,
        remarks=(resolution_reason or "").strip() or None,
    )
    db.add(receipt)
    db.flush()
    if target.supplier_order_item is not None and target.supplier_order is not None:
        supplier_order_id = target.supplier_order.id
        supplier_order_item_id = target.supplier_order_item.id
    else:
        supplier_order_id, supplier_order_item_id = _supplier_link(
            db, target.order_item.id
        )
    receipt_item = IncomingReceiptItem(
        receipt_id=receipt.id,
        order_id=target.order.id,
        order_item_id=target.order_item.id,
        requisition_id=(target.requisition_item.requisition_id if target.requisition_item else None),
        requisition_item_id=(target.requisition_item.id if target.requisition_item else None),
        supplier_order_id=supplier_order_id,
        supplier_order_item_id=supplier_order_item_id,
        planned_quantity=target.planned_quantity,
        received_quantity=quantity,
        cumulative_received_quantity=cumulative,
        variance_quantity=variance,
        variance_type=variance_type,
        resolution_status=resolution_status,
        resolution_action=action,
        resolution_reason=(resolution_reason or "").strip() or None,
        resolved_by=user.id if resolution_status == "resolved" else None,
        resolved_at=now if resolution_status == "resolved" else None,
        status="posted",
    )
    receipt.items.append(receipt_item)
    db.flush()
    if action == "transfer_to_semi_inventory":
        surplus = cumulative - target.planned_quantity
        lot = _create_surplus_lot(
            db,
            target=target,
            receipt_item=receipt_item,
            surplus=surplus,
            location_id=int(surplus_location_id or 0),
            user_id=user.id,
            reason=resolution_reason,
            expected_layout_version=expected_surplus_layout_version,
        )
        receipt_item.surplus_inventory_lot_id = lot.id

    closed = cumulative >= target.planned_quantity or action == "accept_short"
    _mark_order_progress(db, target, closed=closed, user_id=user.id)
    if closed:
        pending_items = db.scalars(
            select(IncomingReceiptItem).where(
                _source_filter(target),
                IncomingReceiptItem.status == "posted",
                IncomingReceiptItem.resolution_status == "pending",
            )
        ).all()
        for pending in pending_items:
            pending.resolution_status = "resolved"
            pending.resolved_by = user.id
            pending.resolved_at = now
    _audit(
        db,
        user=user,
        action="RECEIVE_MATERIAL",
        target=target,
        details={
            "incoming_receipt_id": receipt.id,
            "incoming_receipt_item_id": receipt_item.id,
            "item_key": target.item_key,
            "planned_quantity": target.planned_quantity,
            "received_quantity": quantity,
            "cumulative_received_quantity": cumulative,
            "variance_quantity": variance,
            "resolution_action": action,
            "surplus_inventory_lot_id": receipt_item.surplus_inventory_lot_id,
        },
        audit_context=audit_context,
    )
    db.flush()
    return receipt_item


def accept_short(
    db: Session,
    *,
    user: User,
    receipt_item_id: int,
    reason: str | None,
    audit_context: dict[str, object] | None = None,
) -> IncomingReceiptItem:
    clean_reason = (reason or "").strip() or None
    row = db.get(IncomingReceiptItem, receipt_item_id)
    if row is None or row.status != "posted":
        raise IncomingReceiptError("来料实收记录不存在或已撤销", 404)
    if row.stock_replenishment_item_id is not None:
        raise IncomingReceiptError(
            "补库来料短收请继续等待供应商补货，暂不支持按短收数量直接结单。",
            409,
        )
    target = _target(
        db,
        supplier_order_item_key(row.supplier_order_item_id)
        if row.supplier_order_item_id is not None
        else f"r{row.requisition_item_id}"
        if row.requisition_item_id
        else row.order_item_id,
    )
    latest = db.scalar(
        select(IncomingReceiptItem)
        .where(_source_filter(target), IncomingReceiptItem.status == "posted")
        .order_by(IncomingReceiptItem.id.desc())
    )
    if latest is None or latest.id != row.id:
        raise IncomingReceiptError(
            "同一来料来源存在更晚的实收记录，请在最新一笔上处理短收结单",
            409,
        )
    cumulative = cumulative_received(db, target)
    if cumulative <= 0 or cumulative >= target.planned_quantity:
        raise IncomingReceiptError("当前记录不是可结单的短收状态", 409)
    now = utc_now_naive()
    pending_items = db.scalars(
        select(IncomingReceiptItem).where(
            _source_filter(target),
            IncomingReceiptItem.status == "posted",
            IncomingReceiptItem.resolution_status == "pending",
        )
    ).all()
    for pending in pending_items:
        pending.resolution_status = "resolved"
        pending.resolved_by = user.id
        pending.resolved_at = now
    row.resolution_action = "accept_short"
    row.resolution_reason = clean_reason
    _mark_order_progress(db, target, closed=True, user_id=user.id)
    _audit(
        db,
        user=user,
        action="RESOLVE_INCOMING_VARIANCE",
        target=target,
        details={
            "incoming_receipt_item_id": row.id,
            "planned_quantity": target.planned_quantity,
            "cumulative_received_quantity": cumulative,
            "resolution_action": "accept_short",
            "resolution_reason": clean_reason,
        },
        audit_context=audit_context,
    )
    db.flush()
    return row


def _reverse_surplus_lot(
    db: Session,
    *,
    receipt_item: IncomingReceiptItem,
    user: User,
    reason: str | None,
) -> None:
    if not receipt_item.surplus_inventory_lot_id:
        return
    lot = db.get(InventoryLot, receipt_item.surplus_inventory_lot_id)
    if lot is None:
        raise IncomingReceiptError("关联的超收库存批次不存在，禁止撤销", 409)
    if (
        lot.status != "active"
        or lot.quantity_reserved
        or lot.quantity_consumed
        or lot.quantity_damaged
        or lot.quantity_scrapped
    ):
        raise IncomingReceiptError("超收库存已被预占、消耗、冻结或处理，禁止撤销", 409)
    initial = db.scalar(
        select(InventoryMovement)
        .where(InventoryMovement.inventory_lot_id == lot.id)
        .order_by(InventoryMovement.id)
    )
    if initial is None or lot.quantity_available != initial.quantity:
        raise IncomingReceiptError("超收库存数量已经变化，禁止撤销", 409)
    try:
        mutate_lot(
            db,
            lot_id=lot.id,
            operation="adjust",
            expected_version=lot.version,
            operator_id=user.id,
            quantity=-lot.quantity_available,
            reason=f"撤销来料实收：{reason}",
            idempotency_key=f"incoming-surplus-revert:{receipt_item.id}",
        )
    except WarehouseInventoryError as error:
        raise IncomingReceiptError(str(error), error.status_code) from error
    lot.status = "closed"


def revert_receipt_item(
    db: Session,
    *,
    user: User,
    receipt_item_id: int,
    reason: str,
    audit_context: dict[str, object] | None = None,
) -> IncomingReceiptItem:
    clean_reason = (reason or "").strip() or "撤回来料实收（系统记录）"
    receipt_item = db.get(IncomingReceiptItem, receipt_item_id)
    if receipt_item is None:
        raise IncomingReceiptError("来料实收记录不存在", 404)
    if receipt_item.stock_replenishment_item_id is not None:
        raise IncomingReceiptError(
            "补库来料已经形成客户专用纸板备料，不能用普通撤销；"
            "请走后续受控库存调整流程。",
            409,
        )
    try:
        locked_orders = lock_order_rows_for_production_transition(
            db, [receipt_item.order_id]
        )
    except ProductionWorkflowError as error:
        raise IncomingReceiptError(str(error), error.status_code) from error
    db.expire(receipt_item)
    receipt_item = db.get(IncomingReceiptItem, receipt_item_id)
    if receipt_item is None:
        raise IncomingReceiptError("来料实收记录不存在", 409)
    if receipt_item.status != "posted":
        raise IncomingReceiptError("该来料实收记录已经撤销", 409)
    order = locked_orders.get(receipt_item.order_id)
    if order is None:
        raise IncomingReceiptError("关联订单不存在", 409)
    if order.status in {"partially_delivered", "delivered"}:
        raise IncomingReceiptError("订单已发货，禁止撤回来料", 409)
    target = _target(
        db,
        supplier_order_item_key(receipt_item.supplier_order_item_id)
        if receipt_item.supplier_order_item_id is not None
        else f"r{receipt_item.requisition_item_id}"
        if receipt_item.requisition_item_id
        else receipt_item.order_item_id,
        allow_closed=True,
    )
    if has_production_completion_facts(db, [target.order_item.id]):
        raise IncomingReceiptError("订单明细已有生产完工事实，不能撤销来料实收", 409)
    latest = db.scalar(
        select(IncomingReceiptItem)
        .where(_source_filter(target), IncomingReceiptItem.status == "posted")
        .order_by(IncomingReceiptItem.id.desc())
    )
    if latest is None or latest.id != receipt_item.id:
        raise IncomingReceiptError(
            "同一来料来源存在更晚的实收记录，请先撤销最新一笔",
            409,
        )
    _reverse_surplus_lot(
        db, receipt_item=receipt_item, user=user, reason=clean_reason
    )
    now = utc_now_naive()
    receipt_item.status = "reversed"
    receipt_item.reversal_reason = clean_reason
    receipt_item.reversed_by = user.id
    receipt_item.reversed_at = now
    # Production sessions disable autoflush. Persist the reversed fact before
    # recalculating cumulative receipts, otherwise the query still sees it as posted.
    db.flush()
    receipt = receipt_item.receipt
    if all(item.status == "reversed" for item in receipt.items):
        receipt.status = "reversed"
        receipt.reversal_reason = clean_reason
        receipt.reversed_by = user.id
        receipt.reversed_at = now
    remaining = cumulative_received(db, target)
    latest = db.scalar(
        select(IncomingReceiptItem)
        .where(_source_filter(target), IncomingReceiptItem.status == "posted")
        .order_by(IncomingReceiptItem.id.desc())
    )
    closed = bool(
        latest
        and (
            remaining >= target.planned_quantity
            or latest.resolution_action == "accept_short"
        )
    )
    if (
        not closed
        and latest is not None
        and latest.resolution_action == "await_supplier"
    ):
        latest.resolution_status = "pending"
        latest.resolved_by = None
        latest.resolved_at = None
    _mark_order_progress(db, target, closed=closed, user_id=user.id)
    _audit(
        db,
        user=user,
        action="REVERT_MATERIAL",
        target=target,
        details={
            "incoming_receipt_id": receipt.id,
            "incoming_receipt_item_id": receipt_item.id,
            "reason": clean_reason,
            "before_status": "posted",
            "after_status": receipt_item.status,
            "remaining_cumulative_received_quantity": remaining,
        },
        audit_context=audit_context,
    )
    db.flush()
    return receipt_item


def receipt_item_dict(row: IncomingReceiptItem) -> dict:
    return {
        "receipt_id": row.receipt_id,
        "receipt_item_id": row.id,
        "receipt_number": row.receipt.receipt_number,
        "receipt_status": row.receipt.status,
        "received_at": (
            utc_naive_to_api(row.receipt.received_at)
            if row.receipt.received_at
            else None
        ),
        "received_by": row.receipt.received_by,
        "order_id": row.order_id,
        "order_item_id": row.order_item_id,
        "stock_replenishment_item_id": row.stock_replenishment_item_id,
        "requisition_id": row.requisition_id,
        "requisition_item_id": row.requisition_item_id,
        "supplier_order_id": row.supplier_order_id,
        "supplier_order_item_id": row.supplier_order_item_id,
        "planned_quantity": row.planned_quantity,
        "received_quantity_this_time": row.received_quantity,
        "cumulative_received_quantity": row.cumulative_received_quantity,
        "remaining_quantity": max(
            row.planned_quantity - row.cumulative_received_quantity, 0
        ),
        "variance_quantity": row.variance_quantity,
        "variance_type": row.variance_type,
        "resolution_status": row.resolution_status,
        "resolution_action": row.resolution_action,
        "resolution_reason": row.resolution_reason,
        "surplus_inventory_lot_id": row.surplus_inventory_lot_id,
        "received_inventory_lot_id": row.received_inventory_lot_id,
        "status": row.status,
    }
