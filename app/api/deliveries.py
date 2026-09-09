from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import String, and_, case, cast, delete, exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.api.deps import (
    PermissionChecker,
    customer_scope_ids,
    get_db,
    get_current_user,
    has_permission,
    has_unrestricted_customer_access,
    require_customer_access,
)
from app.core.time_contract import beijing_today, utc_naive_to_api, utc_now_naive
from app.models.audit import OperationLog
from app.models.company_config import CompanyConfig
from app.models.customer import Customer
from app.models.delivery import (
    Delivery,
    DeliveryItem,
    DeliveryPickTask,
    DeliveryPickTaskItem,
)
from app.models.finance import (
    FinanceIdempotencyRecord,
    ReturnReceipt,
    ReturnReceiptItem,
    Statement,
    StatementItem,
)
from app.models.external_packaging_purchase import (
    ExternalPackagingPurchaseCancellation,
    ExternalPackagingPurchaseItem,
    ExternalPackagingPurchaseOrder,
    ExternalPackagingReceiptItem,
)
from app.models.order import Order, OrderItem
from app.models.order_external_packaging import SalesOrderItemExternalComponent
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
from app.models.production_label_print import ProductionPackagingLabelPrintJob
from app.models.requisition import RequisitionItem
from app.models.tianhua_pre_delivery import (
    TianhuaPreDeliveryDraft,
    TianhuaPreDeliveryDraftItem,
)
from app.models.user import User
from app.models.warehouse_inventory import (
    DeliveryInventoryAllocation,
    FinishedGoodsInventoryDetail,
    InventoryLot,
    InventoryMovement,
    InventoryPalletItem,
    InventoryReservation,
    OrderItemSemiRequirement,
    UnorderedFinishedDeliveryAllocation,
    WarehouseArea,
    WarehouseFloor,
    WarehouseLocation,
)
from app.services.order_number_display import display_order_number
from app.services.audit_log import append_audit_event
from app.services.fulfillment_reminders import list_delivery_reminders
from app.services.location_candidates import (
    current_same_location_pallet,
    has_space_ledger,
    load_warehouse_location_projection_contexts,
    operational_location_issue,
    warehouse_location_projection,
)
from app.services.warehouse_floor1_candidate_planner import (
    overlay_formal_area_bindings,
)
from app.services.warehouse_twin_layout import (
    WarehouseTwinLayoutNotFoundError,
    load_warehouse_twin_floor,
)
from app.services.delivery_numbering import (
    DeliveryNumberingError,
    next_delivery_number,
)
from app.services.delivery_snapshots import (
    build_order_delivery_snapshot,
    ensure_order_delivery_snapshot,
)
from app.services.delivery_goods_projection import (
    actual_goods_lines as project_actual_goods_lines,
    customer_document_fulfillment_mode as resolve_customer_document_fulfillment_mode,
    delivery_component_lines as project_delivery_component_lines,
)
from app.services.production_packaging_label import (
    ProductionPackagingLabelError,
    build_delivery_packaging_label_package,
)
from app.services.production_packaging_label_layout import (
    ProductionPackagingLabelLayoutError,
)
from app.services.production_label_operations import (
    ProductionLabelOperationError,
    latest_printed_delivery_job_metadata,
    packaging_label_job_response,
    prepare_delivery_packaging_label_job,
    production_label_write_guard,
)
from app.services.product_specification import (
    product_dimension_specification,
    resolved_product_specification,
)
from app.services.order_status_policy import (
    DELIVERY_CANDIDATE_ORDER_STATUSES,
    order_item_forward_block_message,
    order_item_forward_block_reason,
)
from app.services.production_workflow import (
    ProductionWorkflowError,
    lock_order_rows_for_production_transition,
    normalized_completion_output,
    production_ready_quantity,
    receipt_auto_deliverable_quantity_by_item_ids,
    remaining_finished_order_credit_by_item_ids,
    remaining_finished_order_credit_expression,
)
from app.services.composite_bom_workflow import (
    _delivery_graph_root_snapshot,
    _delivery_reservation_condition,
    _snapshot_reservation_condition,
    ACTIVE_RESERVATION_STATUSES,
    DIRECT_DISPOSITION,
    ComponentDemand,
    CompositeBomWorkflowError,
    component_availability,
    delivery_component_required_quantities,
    delivery_component_demands,
    execute_delivery_component_consumption,
    is_composite_order_item,
    kit_availability,
    kit_available_sets_by_order_item_ids,
    reverse_delivery_component_allocations,
)
from app.services.semi_finished_inventory import (
    active_semi_requirement_credited_quantity,
    consume_delivery_item_inventory,
    finished_order_source_coverage,
    inventory_fully_covers_order_item,
    reverse_delivery_item_inventory,
)
from app.services.warehouse_inventory import (
    WarehouseInventoryError,
    inventory_fifo_order_columns,
    inventory_fifo_sort_key,
    release_empty_pallets_after_delivery,
    restore_auto_released_pallets_after_delivery_cancel,
)
from app.services.warehouse_location_address import (
    employee_area_name,
    employee_location_name,
)
from app.services.unordered_finished_delivery import (
    cancel_unordered_finished_dispatch,
    dispatch_unordered_finished_inventory,
)


router = APIRouter()
order_actions_router = APIRouter()
pick_router = APIRouter()
can_read = PermissionChecker("deliveries.view")
can_operate = PermissionChecker("deliveries.execute")
can_pick = PermissionChecker("deliveries.pick")
PICK_TASK_STATUSES = {"pushed", "driver_confirmed", "exception", "applied", "dispatched"}


def _utc_now() -> datetime:
    return utc_now_naive()


def _require_historical_delivery_permissions(user: User) -> None:
    required = (
        "deliveries.execute",
        "finance.return_receipt.period.adjust",
    )
    missing = [code for code in required if not has_permission(user, code)]
    if missing:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "historical_delivery_permission_denied",
                "message": "历史送货补录需要同时具备送货操作和对账月份调整权限",
                "missing_permissions": missing,
            },
        )


def _delivery_request_hash(action: str, value: dict) -> str:
    normalized = json.dumps(
        {"action": action, "payload": value},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _delivery_idempotency_replay(
    db: Session,
    *,
    idempotency_key: str | None,
    request_hash: str,
    action: str,
    actor: User,
) -> tuple[dict | None, FinanceIdempotencyRecord | None]:
    if not idempotency_key:
        return None, None
    record = db.scalar(
        select(FinanceIdempotencyRecord).where(
            FinanceIdempotencyRecord.idempotency_key == idempotency_key
        )
    )
    if record is None:
        return None, None
    if (
        record.actor_user_id != actor.id
        or record.action != action
        or record.request_hash != request_hash
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "delivery_idempotency_conflict",
                "message": "该幂等键已用于不同操作者或不同内容，请刷新后重试",
            },
        )
    return json.loads(record.response_json), record


def _record_delivery_idempotency(
    db: Session,
    *,
    idempotency_key: str | None,
    request_hash: str,
    action: str,
    actor: User,
    delivery_id: int,
    response: dict,
) -> None:
    if not idempotency_key:
        return
    db.add(
        FinanceIdempotencyRecord(
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            action=action,
            actor_user_id=actor.id,
            resource_type="delivery",
            resource_id=delivery_id,
            response_json=json.dumps(
                jsonable_encoder(response),
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
    )


def _historical_delivery_date_lower_bound(
    db: Session,
    order_item_ids: list[int],
) -> date:
    unique_ids = {int(value) for value in order_item_ids if value}
    if not unique_ids:
        raise HTTPException(
            status_code=400,
            detail="历史送货补录仅支持可追溯到正式订单的送货明细",
        )
    rows = db.execute(
        select(OrderItem.id, Order.order_date)
        .join(Order, Order.id == OrderItem.order_id)
        .where(OrderItem.id.in_(unique_ids))
    ).all()
    found_ids = {int(row.id) for row in rows}
    if found_ids != unique_ids or any(row.order_date is None for row in rows):
        raise HTTPException(
            status_code=409,
            detail="关联订单日期缺失或来源不完整，不能补录历史送货",
        )
    return max(row.order_date for row in rows)


def _validate_historical_delivery_date(
    db: Session,
    *,
    actual_delivery_date: date,
    order_item_ids: list[int],
) -> date:
    today = beijing_today()
    if actual_delivery_date > today:
        raise HTTPException(status_code=400, detail="实际送货日期不能晚于服务器今天")
    lower_bound = _historical_delivery_date_lower_bound(db, order_item_ids)
    if actual_delivery_date < lower_bound:
        raise HTTPException(
            status_code=400,
            detail=(
                f"实际送货日期不能早于关联订单中最晚下单日期 {lower_bound}"
            ),
        )
    return lower_bound


def _suggested_reconciliation_month(delivery_date: date, cycle_day: int) -> str:
    normalized_day = int(cycle_day or 1)
    if normalized_day == 1 or delivery_date.day < normalized_day:
        return delivery_date.strftime("%Y-%m")
    month_index = delivery_date.year * 12 + delivery_date.month
    year, month_zero = divmod(month_index, 12)
    return f"{year:04d}-{month_zero + 1:02d}"


def _delivery_finance_chain_block_reason(
    db: Session,
    delivery_id: int,
) -> str | None:
    row = db.execute(
        select(Statement.statement_number, Statement.confirmation_status)
        .join(StatementItem, StatementItem.statement_id == Statement.id)
        .join(
            ReturnReceiptItem,
            ReturnReceiptItem.id == StatementItem.return_receipt_item_id,
        )
        .join(
            DeliveryItem,
            DeliveryItem.id == ReturnReceiptItem.delivery_item_id,
        )
        .where(DeliveryItem.delivery_id == delivery_id)
        .limit(1)
    ).first()
    if row is None:
        return None
    statement_number, confirmation_status = row
    stage = "已确认对账" if confirmation_status == "confirmed" else "对账草稿"
    return (
        f"送货单已进入{stage} {statement_number} 或其开票/收款下游，"
        "不能直接更正实际送货日期"
    )


def _print_product_code(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return re.split(r"\s*/\s*|\s+", text, maxsplit=1)[0]


def _tianhua_internal_remarks_by_delivery_item(
    db: Session,
    delivery_item_ids: set[int] | list[int],
) -> dict[int, set[str]]:
    ids = {int(value) for value in delivery_item_ids if value}
    if not ids:
        return {}
    rows = db.execute(
        select(
            TianhuaPreDeliveryDraftItem.delivery_item_id,
            TianhuaPreDeliveryDraft.draft_number,
        )
        .join(
            TianhuaPreDeliveryDraft,
            TianhuaPreDeliveryDraft.id
            == TianhuaPreDeliveryDraftItem.draft_id,
        )
        .where(TianhuaPreDeliveryDraftItem.delivery_item_id.in_(ids))
    ).all()
    internal_remarks: dict[int, set[str]] = {}
    for delivery_item_id, draft_number in rows:
        if delivery_item_id is None:
            continue
        internal_remarks.setdefault(int(delivery_item_id), set()).add(
            f"来源：天华预送货草稿 {draft_number}"
        )
    return internal_remarks


def _customer_visible_delivery_remark(
    delivery_item_id: int,
    remark: str | None,
    internal_remarks: dict[int, set[str]],
) -> str | None:
    value = str(remark or "").strip()
    if not value:
        return None
    if value in internal_remarks.get(int(delivery_item_id), set()):
        return None
    return value


def _component_kind(name: str | None) -> str:
    value = str(name or "")
    if value.endswith("-底"):
        return "base"
    if value.endswith("-盖"):
        return "cover"
    return ""


def _received_telescoping_capacity(db: Session, order_item_id: int) -> int | None:
    rows = db.execute(
        select(
            RequisitionItem.product_name_snapshot,
            RequisitionItem.requisition_qty,
            RequisitionItem.status,
        ).where(RequisitionItem.order_item_id == order_item_id)
    ).all()
    base_qty = 0
    cover_qty = 0
    has_telescoping_component = False
    for product_name, quantity, item_status in rows:
        component = _component_kind(product_name)
        if component:
            has_telescoping_component = True
        if item_status != "已入库":
            continue
        if component == "base":
            base_qty += int(quantity or 0)
        elif component == "cover":
            cover_qty += int(quantity or 0)
    if has_telescoping_component:
        return min(base_qty, cover_qty)
    return None


def _external_packaging_received(db: Session, order_item_id: int) -> bool:
    required_component_ids = {
        int(value)
        for value in db.scalars(
            select(SalesOrderItemExternalComponent.id).where(
                SalesOrderItemExternalComponent.sales_order_item_id
                == order_item_id,
                SalesOrderItemExternalComponent.is_required.is_(True),
            )
        ).all()
    }
    if not required_component_ids:
        return False
    received_total = (
        select(
            func.coalesce(
                func.sum(ExternalPackagingReceiptItem.received_quantity), 0
            )
        )
        .where(
            ExternalPackagingReceiptItem.purchase_item_id
            == ExternalPackagingPurchaseItem.id
        )
        .correlate(ExternalPackagingPurchaseItem)
        .scalar_subquery()
    )
    rows = db.execute(
        select(
            ExternalPackagingPurchaseItem.order_component_id,
            ExternalPackagingPurchaseItem.purchase_quantity,
            received_total.label("received_quantity"),
        )
        .join(
            ExternalPackagingPurchaseOrder,
            ExternalPackagingPurchaseOrder.id
            == ExternalPackagingPurchaseItem.purchase_order_id,
        )
        .outerjoin(
            ExternalPackagingPurchaseCancellation,
            ExternalPackagingPurchaseCancellation.purchase_order_id
            == ExternalPackagingPurchaseOrder.id,
        )
        .where(
            ExternalPackagingPurchaseItem.sales_order_item_id == order_item_id,
            ExternalPackagingPurchaseOrder.status == "confirmed",
            ExternalPackagingPurchaseCancellation.id.is_(None),
            ExternalPackagingPurchaseItem.order_component_id.in_(
                required_component_ids
            ),
        )
    ).all()
    fully_received_component_ids = {
        int(component_id)
        for component_id, purchase_quantity, received_quantity in rows
        if Decimal(str(received_quantity or 0)) >= Decimal(purchase_quantity)
    }
    return required_component_ids.issubset(fully_received_component_ids)


def _delivery_remaining_quantity(db: Session, order_item: OrderItem) -> int:
    has_external_components = bool(
        db.scalar(
            select(SalesOrderItemExternalComponent.id)
            .where(
                SalesOrderItemExternalComponent.sales_order_item_id
                == order_item.id,
                SalesOrderItemExternalComponent.is_required.is_(True),
            )
            .limit(1)
        )
    )
    if has_external_components and not _external_packaging_received(
        db, order_item.id
    ):
        return 0
    has_receipt_auto_finished = _has_receipt_auto_finished_fact(db, order_item.id)
    if _uses_composite_inventory(db, order_item.id):
        return int(kit_availability(db, order_item.id)["available_sets"])
    task_query = select(ProductionTask).where(
        ProductionTask.order_item_id == order_item.id,
    )
    if has_receipt_auto_finished:
        task_query = task_query.where(
            ProductionTask.sales_order_item_bom_component_id.is_(None),
            ProductionTask.task_role == "order_main",
        )
    task = db.scalar(task_query.order_by(ProductionTask.id))
    if order_item.supply_mode_snapshot == "external_purchase":
        return max(
            int(order_item.quantity or 0) - int(order_item.delivered_quantity or 0),
            0,
        )
    if has_receipt_auto_finished:
        return _receipt_auto_delivery_ready_quantity(db, order_item.id)
    if task is not None:
        if task.status not in {"completed", "not_required"}:
            return 0
        max_deliverable = max(production_ready_quantity(db, order_item), 0)
        return max(max_deliverable - int(order_item.delivered_quantity or 0), 0)

    max_deliverable = int(order_item.quantity or 0)
    inventory_covered = inventory_fully_covers_order_item(db, order_item.id)
    component_capacity = _received_telescoping_capacity(db, order_item.id)
    if component_capacity is not None:
        max_deliverable = min(max_deliverable, component_capacity)
    elif order_item.material_status != "received" and not inventory_covered:
        return 0
    return max(max_deliverable - int(order_item.delivered_quantity or 0), 0)


def _uses_composite_inventory(db: Session, order_item_id: int, composite_hint=None) -> bool:
    composite = is_composite_order_item(db, order_item_id) if composite_hint is None else composite_hint
    if not composite:
        return False
    from app.models.multilevel_bom import OrderBomGraph
    graph_exists = select(OrderBomGraph.order_item_id).where(
        OrderBomGraph.order_item_id == order_item_id).exists()
    receipt_exists = select(ProductionCompletion.id).where(
        ProductionCompletion.order_item_id == order_item_id,
        ProductionCompletion.status == "posted",
        ProductionCompletion.origin == "receipt_auto").exists()
    return bool(db.scalar(select(or_(graph_exists, ~receipt_exists))))


def _has_receipt_auto_finished_fact(db: Session, order_item_id: int) -> bool:
    """Choose the parent finished ledger for P1-81 composite receipts.

    Historical composite orders have no receipt-auto completion and continue
    to consume their component inventory exactly as before.
    """

    return db.scalar(
        select(ProductionCompletion.id)
        .where(
            ProductionCompletion.order_item_id == int(order_item_id),
            ProductionCompletion.status == "posted",
            ProductionCompletion.origin == "receipt_auto",
        )
        .limit(1)
    ) is not None


def _receipt_auto_ready_quantity_from_facts(
    *,
    remaining_finished_reserved: int,
) -> int:
    """Return current order-backed receipt-managed finished goods.

    Completion counters are historical facts.  Only unconsumed and unreleased
    ``finished_order`` reservation credit is safe for the order delivery path;
    any surplus continues through the separate stock delivery workflow.
    """

    return max(int(remaining_finished_reserved or 0), 0)


def _receipt_auto_delivery_ready_quantity(db: Session, order_item_id: int) -> int:
    return receipt_auto_deliverable_quantity_by_item_ids(
        db, [int(order_item_id)]
    ).get(int(order_item_id), 0)


def _delivery_quantity_facts(db: Session, order_item: OrderItem) -> dict[str, int]:
    ordered = max(int(order_item.quantity or 0), 0)
    delivered = max(int(order_item.delivered_quantity or 0), 0)
    order_remaining = max(ordered - delivered, 0)
    deliverable = _delivery_remaining_quantity(db, order_item)
    return {
        "ordered_quantity": ordered,
        "delivered_quantity": delivered,
        "order_remaining_quantity": order_remaining,
        "deliverable_quantity": deliverable,
        "over_delivery_quantity": max(deliverable - order_remaining, 0),
        "surplus_finished_quantity": max(deliverable - order_remaining, 0),
    }


def _has_production_task(db: Session, order_item_id: int) -> bool:
    return db.scalar(
        select(ProductionTask.id)
        .where(ProductionTask.order_item_id == order_item_id)
        .limit(1)
    ) is not None


def _has_active_production_stock_reservation(
    db: Session,
    order_item_id: int,
) -> bool:
    return db.scalar(
        select(InventoryReservation.id)
        .join(InventoryLot, InventoryLot.id == InventoryReservation.inventory_lot_id)
        .where(
            InventoryReservation.order_item_id == order_item_id,
            InventoryReservation.reservation_type == "finished_order",
            InventoryReservation.status.in_(("active", "partial")),
            InventoryReservation.reserved_stock_quantity
            > InventoryReservation.consumed_stock_quantity
            + InventoryReservation.released_stock_quantity,
            InventoryLot.source_ref_type == "production_completion",
        )
        .limit(1)
    ) is not None


_DELIVERY_ROUTE_AREAS = (
    ("苏州工业园区", ("苏州工业园区", "工业园区")),
    ("相城区", ("相城区",)),
    ("吴中区", ("吴中区",)),
    ("吴江区", ("吴江区",)),
    ("虎丘区/高新区", ("虎丘区", "高新区", "苏州新区")),
    ("姑苏区", ("姑苏区",)),
    ("昆山市", ("昆山市", "昆山")),
    ("常熟市", ("常熟市", "常熟")),
    ("太仓市", ("太仓市", "太仓")),
    ("张家港市", ("张家港市", "张家港")),
)


def _delivery_route_area(address: str | None) -> tuple[str, str | None]:
    text = re.sub(r"\s+", "", str(address or "").strip())
    if not text:
        return "地址未登记", None
    area = "其他区域"
    for label, aliases in _DELIVERY_ROUTE_AREAS:
        if any(alias in text for alias in aliases):
            area = label
            break
    if area == "其他区域":
        match = re.search(r"([\u4e00-\u9fff]{2,8}(?:区|县|市))", text)
        if match:
            area = match.group(1)
    subarea_match = re.search(
        r"([\u4e00-\u9fff]{2,12}(?:镇|街道|开发区|产业园|工业园))",
        text,
    )
    return area, subarea_match.group(1) if subarea_match else None


def _baidu_delivery_direction_url(
    origin: str | None,
    destination: str | None,
) -> str | None:
    if not origin or not destination:
        return None
    return "https://api.map.baidu.com/direction?" + urlencode(
        {
            "origin": origin,
            "destination": destination,
            "mode": "driving",
            "region": "苏州",
            "output": "html",
            "coord_type": "bd09ll",
            "src": "webapp.tianming.erp",
        }
    )


def _normalized_delivery_address(address: str | None) -> str:
    return re.sub(r"[\s,，/]+", "", str(address or "").strip()).lower()


class UnorderedFinishedAllocationCreate(BaseModel):
    inventory_lot_id: int
    quantity: int


class DeliveryLineCreate(BaseModel):
    customer_po: str | None = Field(default=None, max_length=200)
    source_type: str = "order"
    order_item_id: int | None = None
    product_id: int | None = None
    delivered_quantity: int
    unit_price: Decimal | None = None
    allocations: list[UnorderedFinishedAllocationCreate] = Field(default_factory=list)
    over_delivery_confirmed: bool = False
    over_delivery_reason: str | None = None
    remarks: str | None = None


class DeliveryCreate(BaseModel):
    customer_id: int
    delivery_date: date | None = None
    historical_backfill: bool = False
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=120)
    vehicle_number: str | None = None
    source_mode: str = "order"
    items: list[DeliveryLineCreate] = Field(default_factory=list)
    lines: list[DeliveryLineCreate] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_delivery_lines(self):
        if self.items and self.lines:
            raise ValueError("送货明细只能使用 items 或 lines 其中一种格式")
        selected = self.items or self.lines
        if not selected:
            raise ValueError("送货单至少需要一条明细")
        self.items = selected
        self.lines = []
        key = (self.idempotency_key or "").strip()
        if self.historical_backfill and len(key) < 8:
            raise ValueError("补录历史送货必须提供幂等键")
        if not self.historical_backfill and key:
            raise ValueError("普通送货不要提交历史补录幂等键")
        self.idempotency_key = key or None
        if self.source_mode == "unordered_finished":
            for line in selected:
                if line.source_type == "finished_stock":
                    line.source_type = "unordered_finished"
        return self


class DeliveryUpdate(BaseModel):
    delivery_date: date | None = None
    historical_backfill: bool | None = None
    expected_version: int | None = Field(default=None, gt=0)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=120)
    vehicle_number: str | None = None
    source_mode: str = "order"
    items: list[DeliveryLineCreate] = Field(default_factory=list)
    lines: list[DeliveryLineCreate] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_delivery_lines(self):
        if self.items and self.lines:
            raise ValueError("送货明细只能使用 items 或 lines 其中一种格式")
        selected = self.items or self.lines
        if not selected:
            raise ValueError("送货单至少需要一条明细")
        self.items = selected
        self.lines = []
        self.idempotency_key = (self.idempotency_key or "").strip() or None
        if self.source_mode == "unordered_finished":
            for line in selected:
                if line.source_type == "finished_stock":
                    line.source_type = "unordered_finished"
        return self


class DeliveryRevisionUpdate(DeliveryUpdate):
    expected_version: int = Field(gt=0)
    idempotency_key: str = Field(min_length=8, max_length=120)

    @model_validator(mode="after")
    def validate_revision_request(self):
        if self.historical_backfill:
            raise ValueError("已发货送货单受控编辑不能切换为历史补录")
        self.idempotency_key = self.idempotency_key.strip()
        return self


class DeliveryCustomerPoLine(BaseModel):
    delivery_item_id: int = Field(gt=0)
    customer_po: str = Field(max_length=200)

class DeliveryCustomerPoUpdate(BaseModel):
    expected_version: int = Field(gt=0)
    idempotency_key: str = Field(min_length=8, max_length=120)
    items: list[DeliveryCustomerPoLine] = Field(min_length=1)

class DeliveryDateCorrection(BaseModel):
    actual_delivery_date: date
    expected_version: int = Field(gt=0)
    idempotency_key: str = Field(min_length=8, max_length=120)

    @field_validator("idempotency_key")
    @classmethod
    def normalize_idempotency_key(cls, value: str) -> str:
        return value.strip()


class DeliveryPackagingLabelJobItemRequest(BaseModel):
    model_config = {"extra": "forbid"}

    selection_key: str = Field(min_length=1, max_length=160)
    print_label_count: int = Field(ge=0)

    @field_validator("selection_key")
    @classmethod
    def validate_selection_key(cls, value: str) -> str:
        normalized = value.strip()
        if not re.fullmatch(
            r"delivery:\d+:item:\d+:(?:parent|product|component:\d+)",
            normalized,
        ):
            raise ValueError("送货标签明细身份无效")
        return normalized

    @field_validator("print_label_count", mode="before")
    @classmethod
    def reject_boolean_count(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("本次打印标签张数必须为整数")
        return value


class DeliveryPackagingLabelJobRequest(BaseModel):
    model_config = {"extra": "forbid"}

    idempotency_key: str = Field(min_length=8, max_length=120)
    plan_fingerprint: str = Field(min_length=64, max_length=64)
    confirmed: Literal[True]
    items: list[DeliveryPackagingLabelJobItemRequest] = Field(
        min_length=1,
        max_length=500,
    )

    @field_validator("idempotency_key")
    @classmethod
    def validate_idempotency_key(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) < 8:
            raise ValueError("送货标签幂等键过短")
        return normalized

    @field_validator("plan_fingerprint")
    @classmethod
    def validate_plan_fingerprint(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", normalized):
            raise ValueError("送货标签计划指纹无效")
        return normalized

    @model_validator(mode="after")
    def validate_unique_items(self):
        keys = [item.selection_key for item in self.items]
        if len(keys) != len(set(keys)):
            raise ValueError("送货标签明细不能重复")
        return self


def _validate_delivery_source_contract(
    source_mode: str,
    lines: list[DeliveryLineCreate],
) -> None:
    if source_mode not in {"order", "unordered_finished", "mixed"}:
        raise ValueError("送货来源必须是订单待送、无订单成品库存或两者混合")
    seen_order_items: set[int] = set()
    seen_products: set[int] = set()
    seen_lots: set[int] = set()
    source_types: set[str] = set()
    for index, line in enumerate(lines, start=1):
        normalized = (line.source_type or "order").strip()
        if normalized == "finished_stock":
            normalized = "unordered_finished"
            line.source_type = normalized
        if normalized not in {"order", "unordered_finished"}:
            raise ValueError(f"第 {index} 条送货明细来源无效")
        source_types.add(normalized)
        if source_mode != "mixed" and normalized != source_mode:
            raise ValueError(f"第 {index} 条送货明细来源不一致，禁止混合送货")
        if normalized == "order":
            if line.order_item_id is None or line.product_id is not None:
                raise ValueError(f"第 {index} 条订单待送明细缺少订单关联")
            if line.order_item_id in seen_order_items:
                raise ValueError("同一订单明细不能重复选择")
            seen_order_items.add(line.order_item_id)
            if line.allocations:
                raise ValueError("订单待送不能提交无订单库存批次")
            continue
        if line.order_item_id is not None or line.product_id is None:
            raise ValueError(f"第 {index} 条无订单库存明细缺少产品")
        if line.product_id in seen_products:
            raise ValueError("同一产品在一张无订单送货单中只能出现一行")
        seen_products.add(line.product_id)
        if line.unit_price is not None and line.unit_price < 0:
            raise ValueError(f"第 {index} 条无订单库存明细单价不能小于零")
        if not line.allocations:
            raise ValueError(f"第 {index} 条无订单库存明细必须选择库存批次")
        allocated = 0
        for allocation in line.allocations:
            if allocation.quantity <= 0:
                raise ValueError(f"第 {index} 条库存批次数量必须大于零")
            if allocation.inventory_lot_id in seen_lots:
                raise ValueError("同一库存批次不能在一张送货单中重复使用")
            seen_lots.add(allocation.inventory_lot_id)
            allocated += allocation.quantity
        if allocated != line.delivered_quantity:
            raise ValueError(f"第 {index} 条送货数量必须等于批次分配数量")
    if source_mode == "mixed" and source_types != {"order", "unordered_finished"}:
        raise ValueError("混合送货必须同时包含订单待送和无订单成品库存")


class PickStagingTarget(BaseModel):
    location_id: int = Field(gt=0)
    address_version: int = Field(gt=0)
    layout_version: int | None = Field(default=None, gt=0)


class PickStagingBatch(BaseModel):
    print_version: str | None = None
    targets: dict[int, PickStagingTarget] = Field(default_factory=dict)


class DeliveryPickItemUpdate(BaseModel):
    pick_status: str
    picked_quantity: int | None = None
    print_version: str | None = None
    staging_target: PickStagingTarget | None = None

    @field_validator("pick_status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"picked", "partial", "no_stock"}:
            raise ValueError("拿货状态必须是 picked、partial 或 no_stock")
        return normalized


class DeliveryPickAssignmentUpdate(BaseModel):
    picker_user_id: int | None = Field(default=None, gt=0)


def _pick_task_read_context(
    db: Session,
    items: list[DeliveryPickTaskItem],
) -> dict:
    """Preload task, inventory and canonical map facts in bounded batches."""
    delivery_item_ids = {
        item.delivery_item_id for item in items if item.delivery_item_id is not None
    }
    order_item_ids = {
        item.order_item_id for item in items if item.order_item_id is not None
    }
    delivery_items = {
        row.id: row
        for row in db.scalars(
            select(DeliveryItem).where(DeliveryItem.id.in_(delivery_item_ids))
        ).all()
    } if delivery_item_ids else {}
    order_items = {
        row.id: row
        for row in db.scalars(
            select(OrderItem).where(OrderItem.id.in_(order_item_ids))
        ).all()
    } if order_item_ids else {}
    composite_order_item_ids = set(
        db.scalars(
            select(SalesOrderItemBomComponent.sales_order_item_id)
            .where(
                SalesOrderItemBomComponent.sales_order_item_id.in_(order_item_ids)
            )
            .distinct()
        ).all()
    ) if order_item_ids else set()
    inventory_source_order_item_ids = set(
        db.scalars(
            select(InventoryReservation.order_item_id)
            .where(
                InventoryReservation.order_item_id.in_(order_item_ids),
                InventoryReservation.status != "cancelled",
                func.coalesce(
                    InventoryReservation.credited_requirement_quantity,
                    0,
                )
                > InventoryReservation.released_requirement_quantity,
            )
            .distinct()
        ).all()
    ) if order_item_ids else set()
    reservation_lot_ids = set(
        db.scalars(
            select(InventoryReservation.inventory_lot_id)
            .where(
                InventoryReservation.order_item_id.in_(order_item_ids),
                InventoryReservation.status != "cancelled",
            )
            .distinct()
        ).all()
    ) if order_item_ids else set()
    unordered_lot_ids = set(
        db.scalars(
            select(UnorderedFinishedDeliveryAllocation.inventory_lot_id)
            .where(
                UnorderedFinishedDeliveryAllocation.delivery_item_id.in_(
                    delivery_item_ids
                ),
                UnorderedFinishedDeliveryAllocation.status == "planned",
            )
            .distinct()
        ).all()
    ) if delivery_item_ids else set()
    lot_ids = {
        int(lot_id)
        for lot_id in reservation_lot_ids | unordered_lot_ids
        if lot_id is not None
    }
    lots = {
        int(row.id): row
        for row in db.scalars(
            select(InventoryLot)
            .options(
                selectinload(InventoryLot.pallet_item).selectinload(
                    InventoryPalletItem.pallet
                )
            )
            .where(InventoryLot.id.in_(lot_ids))
        ).all()
    } if lot_ids else {}
    location_ids = {
        int(row.warehouse_location_id)
        for row in lots.values()
        if row.warehouse_location_id is not None
    }
    locations = {
        int(row.id): row
        for row in db.scalars(
            select(WarehouseLocation).where(WarehouseLocation.id.in_(location_ids))
        ).all()
    } if location_ids else {}
    location_projection_contexts = load_warehouse_location_projection_contexts(
        db,
        locations.values(),
    )
    return {
        "delivery_items": delivery_items,
        "order_items": order_items,
        "composite_order_item_ids": composite_order_item_ids,
        "inventory_source_order_item_ids": inventory_source_order_item_ids,
        "lots": lots,
        "locations": locations,
        "location_projection_contexts": location_projection_contexts,
        "space_ledger_enabled": has_space_ledger(db),
    }


def _pick_item_component_lines(
    db: Session,
    item: DeliveryPickTaskItem,
    *,
    read_context: dict | None = None,
) -> list[dict]:
    """Return read-only parent-priced component goods for one pick row.

    Pick tasks persist exactly one operable row per delivery item.  Components
    deliberately remain derived display data: the driver still confirms only
    the parent delivery quantity, while the later dispatch transaction applies
    the existing component inventory gate and allocation facts.
    """
    delivery_item = (
        read_context["delivery_items"].get(item.delivery_item_id)
        if read_context is not None
        else (
            db.get(DeliveryItem, item.delivery_item_id)
            if item.delivery_item_id is not None
            else None
        )
    )
    if (
        delivery_item is None
        or item.order_item_id is None
        or delivery_item.order_item_id != item.order_item_id
    ):
        return []
    order_item = (
        read_context["order_items"].get(item.order_item_id)
        if read_context is not None
        else db.get(OrderItem, item.order_item_id)
    )
    is_composite = (
        order_item is not None
        and (
            order_item.id in read_context["composite_order_item_ids"]
            if read_context is not None
            else is_composite_order_item(db, order_item.id)
        )
    )
    if order_item is None or not is_composite:
        return []
    return [
        component
        for component in _delivery_component_lines(
            db,
            order_item=order_item,
            planned_delivery_quantity=int(item.original_quantity or 0),
            delivery_item_id=delivery_item.id,
            dispatched=False,
        )
        if int(component.get("planned_delivery_quantity") or 0) > 0
    ]


def _pick_unordered_location_plan(
    db: Session,
    *,
    item: DeliveryPickTaskItem,
    delivery_item: DeliveryItem,
    read_context: dict | None = None,
) -> tuple[list[dict], bool]:
    allocations = db.scalars(
        select(UnorderedFinishedDeliveryAllocation)
        .where(
            UnorderedFinishedDeliveryAllocation.delivery_item_id
            == delivery_item.id,
            UnorderedFinishedDeliveryAllocation.status == "planned",
        )
        .order_by(UnorderedFinishedDeliveryAllocation.id)
    ).all()
    lines: list[dict] = []
    for allocation in allocations:
        quantity = int(allocation.planned_quantity or 0)
        if quantity <= 0:
            continue
        location = _pick_source_location(
            db,
            source={"lot_id": allocation.inventory_lot_id},
            read_context=read_context,
        )
        lines.append(
            {
                "pick_item_id": item.id,
                "order_item_id": None,
                "source_type": "finished_inventory",
                "reservation_id": None,
                "lot_id": allocation.inventory_lot_id,
                "lot_number": allocation.lot_number_snapshot,
                "component_snapshot_id": None,
                "product_code": item.product_code_snapshot,
                "product_name": item.product_name_snapshot,
                "specification": item.specification_snapshot,
                "pick_quantity": quantity,
                "requirement_quantity": quantity,
                "unit": "个",
                "location_id": location["location_id"],
                "location_code": allocation.warehouse_location_code_snapshot
                or location["location_code"],
                "location_name": location["location_name"],
                "warehouse_floor": location["warehouse_floor"],
                "area_code": location["area_code"],
                "location_sort_order": location["location_sort_order"],
                "placement_status": location["placement_status"],
                "position_status": location["position_status"],
                "map_feature_id": location["map_feature_id"],
                "published_map_revision": location["published_map_revision"],
                "map_point": location["map_point"],
                "pallet_id": location["pallet_id"],
                "pallet_code": allocation.pallet_code_snapshot
                or location["pallet_code"],
                "location_operational": location["location_operational"],
                "needs_relocation": location["needs_relocation"],
                "requires_attention": bool(
                    location["location_id"] is not None
                    and not location["location_operational"]
                ),
            }
        )
    complete_quantity = sum(int(line["pick_quantity"]) for line in lines)
    complete = complete_quantity == int(item.original_quantity or 0) and not any(
        line["requires_attention"] for line in lines
    )
    return lines, complete


def _pick_source_location(
    db: Session,
    *,
    source: dict,
    read_context: dict | None = None,
) -> dict:
    lot_id = int(source["lot_id"]) if source.get("lot_id") is not None else None
    lot = (
        (read_context.get("lots") or {}).get(lot_id)
        if read_context is not None and lot_id is not None
        else (db.get(InventoryLot, lot_id) if lot_id is not None else None)
    )
    if lot is None and lot_id is not None:
        lot = db.get(InventoryLot, lot_id)
        if lot is not None and read_context is not None:
            read_context.setdefault("lots", {})[lot_id] = lot
    location_id = (
        int(lot.warehouse_location_id)
        if lot is not None and lot.warehouse_location_id is not None
        else None
    )
    location = (
        (read_context.get("locations") or {}).get(location_id)
        if read_context is not None and location_id is not None
        else (
            db.get(WarehouseLocation, location_id)
            if location_id is not None
            else None
        )
    )
    if location is None and location_id is not None:
        location = db.get(WarehouseLocation, location_id)
        if location is not None and read_context is not None:
            read_context.setdefault("locations", {})[location_id] = location
    projection_context = (
        (read_context.get("location_projection_contexts") or {}).get(
            int(location.id)
        )
        if read_context is not None and location is not None
        else None
    )
    if location is not None and projection_context is None:
        projection_context = load_warehouse_location_projection_contexts(
            db,
            [location],
        ).get(int(location.id), {})
        if read_context is not None:
            read_context.setdefault("locations", {})[int(location.id)] = location
            read_context.setdefault("location_projection_contexts", {})[
                int(location.id)
            ] = projection_context
    projection = (
        warehouse_location_projection(location, **(projection_context or {}))
        if location is not None
        else {}
    )
    space_ledger_enabled = (
        bool(read_context.get("space_ledger_enabled"))
        if read_context is not None
        else has_space_ledger(db)
    )
    operational_projection_context = (
        projection_context if space_ledger_enabled else None
    )
    pallet = current_same_location_pallet(lot) if lot is not None else None
    location_operational = bool(
        location is not None
        and operational_location_issue(
            db,
            location,
            warehouse_types={"finished", "shared"},
            projection_context=operational_projection_context,
        )
        is None
    )
    needs_relocation = bool(
        (pallet is not None and pallet.needs_relocation)
        or (location is not None and location.placement_status == "unplaced")
        or (location is not None and not location_operational)
    )
    return {
        "location_id": location.id if location else None,
        "location_code": location.location_code if location else None,
        "location_name": (
            employee_location_name(
                location,
                area=(projection_context or {}).get("area"),
                floor=(projection_context or {}).get("floor"),
            )
            if location
            else None
        ),
        "warehouse_floor": location.warehouse_floor if location else None,
        "area_code": location.area_code if location else None,
        "location_sort_order": int(location.sort_order or 0) if location else None,
        "placement_status": location.placement_status if location else None,
        "position_status": projection.get("position_status", "unlocated"),
        "map_feature_id": projection.get("map_feature_id"),
        "published_map_revision": projection.get("published_map_revision"),
        "map_point": projection.get("map_position"),
        "pallet_id": pallet.id if pallet else None,
        "pallet_code": pallet.pallet_code if pallet else None,
        "location_operational": location_operational,
        "needs_relocation": needs_relocation,
    }


def _pick_parent_finished_sources(
    db: Session,
    *,
    order_item: OrderItem,
    planned_quantity: int,
    read_context: dict | None = None,
) -> list[dict]:
    """Select unconsumed parent-product reservations for a composite delivery."""

    remaining = max(int(planned_quantity or 0), 0)
    if remaining <= 0:
        return []
    reservations = db.scalars(
        select(InventoryReservation)
        .join(InventoryLot, InventoryLot.id == InventoryReservation.inventory_lot_id)
        .where(
            InventoryReservation.order_item_id == order_item.id,
            InventoryReservation.reservation_type == "finished_order",
            InventoryReservation.sales_order_item_bom_component_id.is_(None),
            InventoryReservation.status != "cancelled",
            InventoryReservation.reserved_stock_quantity
            > InventoryReservation.consumed_stock_quantity
            + InventoryReservation.released_stock_quantity,
        )
        .order_by(*inventory_fifo_order_columns(), InventoryReservation.id)
    ).all()
    sources: list[dict] = []
    for reservation in reservations:
        available = max(
            int(reservation.reserved_stock_quantity or 0)
            - int(reservation.consumed_stock_quantity or 0)
            - int(reservation.released_stock_quantity or 0),
            0,
        )
        picked = min(available, remaining)
        if picked <= 0:
            continue
        lot = (
            (read_context.get("lots") or {}).get(
                int(reservation.inventory_lot_id)
            )
            if read_context is not None
            else db.get(InventoryLot, reservation.inventory_lot_id)
        )
        sources.append(
            {
                "source_type": "finished",
                "reservation_id": reservation.id,
                "lot_id": lot.id if lot else None,
                "lot_number": lot.lot_number if lot else None,
                "component_snapshot_id": None,
                "quantity_to_pick_stock": picked,
                "quantity_to_pick_requirement": picked,
            }
        )
        remaining -= picked
        if remaining <= 0:
            break
    return sources


def _pick_item_location_plan(
    db: Session,
    *,
    item: DeliveryPickTaskItem,
    component_lines: list[dict],
    read_context: dict | None = None,
) -> tuple[list[dict], bool]:
    """Build a read-only loading plan from current inventory and production facts."""

    delivery_item = (
        read_context["delivery_items"].get(item.delivery_item_id)
        if read_context is not None
        else (
            db.get(DeliveryItem, item.delivery_item_id)
            if item.delivery_item_id is not None
            else None
        )
    )
    if delivery_item is not None and delivery_item.source_type == "unordered_finished":
        return _pick_unordered_location_plan(
            db,
            item=item,
            delivery_item=delivery_item,
            read_context=read_context,
        )
    order_item = (
        read_context["order_items"].get(item.order_item_id)
        if read_context is not None
        else db.get(OrderItem, item.order_item_id)
    )
    if order_item is None:
        return [], False
    fulfillment_mode = (
        getattr(order_item, "composite_fulfillment_mode_snapshot", None)
        or "component_delivery"
    )
    composite_hint = (
        order_item.id in read_context["composite_order_item_ids"]
        if read_context is not None
        else None
    )
    if (
        read_context is not None
        and not composite_hint
        and order_item.id not in read_context["inventory_source_order_item_ids"]
    ):
        return [], False
    planned_quantity = max(int(item.original_quantity or 0), 0)
    raw_sources = _inventory_sources_for_order_item(
        db,
        order_item=order_item,
        planned_delivery_quantity=planned_quantity,
        delivery_item_id=item.delivery_item_id,
        dispatched=False,
        composite_hint=composite_hint,
        read_context=read_context,
    )
    if component_lines and fulfillment_mode == "parent_delivery":
        raw_sources = [
            *_pick_parent_finished_sources(
                db,
                order_item=order_item,
                planned_quantity=planned_quantity,
                read_context=read_context,
            ),
            *raw_sources,
        ]
    component_by_snapshot = {
        int(row["component_snapshot_id"]): row
        for row in component_lines
        if row.get("component_snapshot_id") is not None
    }
    lines: list[dict] = []
    covered_by_component: dict[int, int] = {}
    finished_covered = 0

    for source in raw_sources:
        source_type = str(source.get("source_type") or "")
        # Semi-finished reservations are production inputs, not finished goods
        # that a driver should load.  Their eventual finished output is listed
        # below as a production-area direct pick.
        if source_type == "semi_finished":
            continue
        stock_quantity = max(int(source.get("quantity_to_pick_stock") or 0), 0)
        requirement_quantity = max(
            int(source.get("quantity_to_pick_requirement") or 0),
            0,
        )
        display_quantity = stock_quantity or requirement_quantity
        if display_quantity <= 0:
            continue
        component_snapshot_id = source.get("component_snapshot_id")
        component = (
            component_by_snapshot.get(int(component_snapshot_id))
            if component_snapshot_id is not None
            else None
        )
        if component_snapshot_id is not None:
            snapshot_id = int(component_snapshot_id)
            covered_by_component[snapshot_id] = (
                covered_by_component.get(snapshot_id, 0) + requirement_quantity
            )
        elif source_type == "finished":
            finished_covered += requirement_quantity
        location = _pick_source_location(
            db,
            source=source,
            read_context=read_context,
        )
        is_direct = source_type == "component_direct"
        lines.append(
            {
                "pick_item_id": item.id,
                "order_item_id": item.order_item_id,
                "source_type": (
                    "production_direct" if is_direct else "finished_inventory"
                ),
                "reservation_id": source.get("reservation_id"),
                "lot_id": source.get("lot_id"),
                "lot_number": source.get("lot_number"),
                "component_snapshot_id": component_snapshot_id,
                "product_code": (
                    (component or {}).get("product_code")
                    or source.get("component_code")
                    or item.product_code_snapshot
                ),
                "product_name": (
                    (component or {}).get("product_name")
                    or source.get("component_name")
                    or item.product_name_snapshot
                ),
                "specification": (
                    (component or {}).get("specification")
                    or item.specification_snapshot
                ),
                "pick_quantity": display_quantity,
                "requirement_quantity": requirement_quantity,
                "unit": "个",
                "location_id": None if is_direct else location["location_id"],
                "location_code": None if is_direct else location["location_code"],
                "location_name": None if is_direct else location["location_name"],
                "warehouse_floor": (
                    None if is_direct else location["warehouse_floor"]
                ),
                "area_code": None if is_direct else location["area_code"],
                "location_sort_order": (
                    None if is_direct else location["location_sort_order"]
                ),
                "placement_status": (
                    None if is_direct else location["placement_status"]
                ),
                "position_status": (
                    None if is_direct else location["position_status"]
                ),
                "map_feature_id": (
                    None if is_direct else location["map_feature_id"]
                ),
                "published_map_revision": (
                    None if is_direct else location["published_map_revision"]
                ),
                "map_point": None if is_direct else location["map_point"],
                "pallet_id": None if is_direct else location["pallet_id"],
                "pallet_code": None if is_direct else location["pallet_code"],
                "location_operational": (
                    True if is_direct else location["location_operational"]
                ),
                "needs_relocation": (
                    False if is_direct else location["needs_relocation"]
                ),
                "requires_attention": bool(
                    not is_direct
                    and location["location_id"] is not None
                    and not location["location_operational"]
                ),
            }
        )

    if component_lines:
        parent_direct_quantity = (
            max(planned_quantity - finished_covered, 0)
            if fulfillment_mode == "parent_delivery"
            else 0
        )
        if parent_direct_quantity > 0:
            lines.append(
                {
                    "pick_item_id": item.id,
                    "order_item_id": item.order_item_id,
                    "source_type": "production_direct",
                    "reservation_id": None,
                    "lot_id": None,
                    "lot_number": None,
                    "component_snapshot_id": None,
                    "product_code": item.product_code_snapshot,
                    "product_name": item.product_name_snapshot,
                    "specification": item.specification_snapshot,
                    "pick_quantity": parent_direct_quantity,
                    "requirement_quantity": parent_direct_quantity,
                    "unit": "个",
                    "location_id": None,
                    "location_code": None,
                    "location_name": None,
                    "warehouse_floor": None,
                    "area_code": None,
                    "location_sort_order": None,
                    "placement_status": None,
                    "pallet_id": None,
                    "pallet_code": None,
                    "location_operational": True,
                    "needs_relocation": False,
                    "requires_attention": False,
                }
            )
        for component in component_lines:
            snapshot_id = int(component["component_snapshot_id"])
            expected = max(
                int(component.get("planned_delivery_quantity") or 0),
                0,
            )
            missing = max(expected - covered_by_component.get(snapshot_id, 0), 0)
            if missing <= 0:
                continue
            lines.append(
                {
                    "pick_item_id": item.id,
                    "order_item_id": item.order_item_id,
                    "source_type": "unassigned",
                    "reservation_id": None,
                    "lot_id": None,
                    "lot_number": None,
                    "component_snapshot_id": snapshot_id,
                    "product_code": component.get("product_code"),
                    "product_name": component.get("product_name"),
                    "specification": component.get("specification"),
                    "pick_quantity": missing,
                    "requirement_quantity": missing,
                    "unit": "个",
                    "location_id": None,
                    "location_code": None,
                    "location_name": None,
                    "warehouse_floor": None,
                    "area_code": None,
                    "location_sort_order": None,
                    "placement_status": None,
                    "pallet_id": None,
                    "pallet_code": None,
                    "location_operational": False,
                    "needs_relocation": False,
                    "requires_attention": True,
                }
            )
    else:
        direct_quantity = max(planned_quantity - finished_covered, 0)
        if direct_quantity > 0:
            lines.append(
                {
                    "pick_item_id": item.id,
                    "order_item_id": item.order_item_id,
                    "source_type": "production_direct",
                    "reservation_id": None,
                    "lot_id": None,
                    "lot_number": None,
                    "component_snapshot_id": None,
                    "product_code": item.product_code_snapshot,
                    "product_name": item.product_name_snapshot,
                    "specification": item.specification_snapshot,
                    "pick_quantity": direct_quantity,
                    "requirement_quantity": direct_quantity,
                    "unit": "个",
                    "location_id": None,
                    "location_code": None,
                    "location_name": None,
                    "warehouse_floor": None,
                    "area_code": None,
                    "location_sort_order": None,
                    "placement_status": None,
                    "pallet_id": None,
                    "pallet_code": None,
                    "location_operational": True,
                    "needs_relocation": False,
                    "requires_attention": False,
                }
            )
    return lines, not any(line["requires_attention"] for line in lines)


def _pick_location_projection_batch(
    db: Session,
    location_ids: set[int],
    *,
    read_context: dict | None = None,
) -> tuple[dict[int, WarehouseLocation], dict[int, dict]]:
    locations = dict((read_context or {}).get("locations") or {})
    missing_location_ids = location_ids - set(locations)
    if missing_location_ids:
        locations.update(
            {
                int(row.id): row
                for row in db.scalars(
                    select(WarehouseLocation).where(
                        WarehouseLocation.id.in_(missing_location_ids)
                    )
                ).all()
            }
        )
    projection_contexts = dict(
        (read_context or {}).get("location_projection_contexts") or {}
    )
    missing_context_locations = [
        locations[location_id]
        for location_id in location_ids
        if location_id in locations and location_id not in projection_contexts
    ]
    if missing_context_locations:
        projection_contexts.update(
            load_warehouse_location_projection_contexts(
                db,
                missing_context_locations,
            )
        )
    if read_context is not None:
        read_context["locations"] = locations
        read_context["location_projection_contexts"] = projection_contexts
    return locations, projection_contexts


def _pick_location_groups(
    db: Session,
    item_responses: list[dict],
    *,
    read_context: dict | None = None,
) -> list[dict]:
    groups: dict[tuple, dict] = {}
    for item in item_responses:
        for line in item.get("location_lines") or []:
            source_type = str(line.get("source_type") or "")
            if source_type == "production_direct":
                priority = 1
                group_key = ("production_direct",)
                label = "生产区直接拿货"
            elif source_type == "unassigned":
                priority = 3
                group_key = ("unassigned",)
                label = "未分配拿货位置（请核对）"
            else:
                priority = 2 if line.get("needs_relocation") else 0
                group_key = (
                    "finished_inventory",
                    line.get("location_id"),
                    line.get("pallet_id"),
                    bool(line.get("needs_relocation")),
                )
                label = line.get("location_name") or "位置名称待完善"
                if line.get("needs_relocation"):
                    label += "（待归位）"
            group = groups.setdefault(
                group_key,
                {
                    "key": "|".join(str(part) for part in group_key),
                    "priority": priority,
                    "label": label,
                    "source_type": source_type,
                    "warehouse_floor": line.get("warehouse_floor"),
                    "area_code": line.get("area_code"),
                    "location_id": line.get("location_id"),
                    "location_code": line.get("location_code"),
                    "location_name": line.get("location_name"),
                    "location_sort_order": line.get("location_sort_order"),
                    "pallet_id": line.get("pallet_id"),
                    "pallet_code": line.get("pallet_code"),
                    "needs_relocation": bool(line.get("needs_relocation")),
                    "requires_attention": bool(line.get("requires_attention")),
                    "total_pick_quantity": 0,
                    "lines": [],
                },
            )
            group["total_pick_quantity"] += int(line.get("pick_quantity") or 0)
            group["requires_attention"] = bool(
                group["requires_attention"] or line.get("requires_attention")
            )
            group["lines"].append(line)

    def sort_key(group: dict) -> tuple:
        return (
            int(group["priority"]),
            int(group["warehouse_floor"] or 999),
            str(group["area_code"] or ""),
            int(group["location_sort_order"] or 0),
            str(group["location_code"] or ""),
            str(group["pallet_code"] or ""),
        )

    ordered = sorted(groups.values(), key=sort_key)
    location_ids = {
        int(group["location_id"])
        for group in ordered
        if group.get("location_id") is not None
    }
    locations, projection_contexts = _pick_location_projection_batch(
        db,
        location_ids,
        read_context=read_context,
    )
    for sequence, group in enumerate(ordered, start=1):
        group["recommended_sequence"] = sequence
        location_id = (
            int(group["location_id"])
            if group.get("location_id") is not None
            else None
        )
        location = locations.get(location_id) if location_id is not None else None
        projection_context = (
            projection_contexts.get(location_id, {})
            if location_id is not None
            else {}
        )
        projection = (
            warehouse_location_projection(location, **projection_context)
            if location is not None
            else {}
        )
        if location is not None:
            location_name = employee_location_name(
                location,
                area=projection_context.get("area"),
                floor=projection_context.get("floor"),
            )
            group.update(
                {
                    "warehouse_floor": location.warehouse_floor,
                    "area_code": location.area_code,
                    "location_code": location.location_code,
                    "location_name": location_name,
                    "location_sort_order": int(location.sort_order or 0),
                    "position_status": projection.get("position_status"),
                    "map_feature_id": projection.get("map_feature_id"),
                    "published_map_revision": projection.get(
                        "published_map_revision"
                    ),
                }
            )
            group["label"] = location_name + (
                "（待归位）" if group.get("needs_relocation") else ""
            )
            for line in group["lines"]:
                line.update(
                    {
                        "warehouse_floor": location.warehouse_floor,
                        "area_code": location.area_code,
                        "location_code": location.location_code,
                        "location_name": location_name,
                        "placement_status": location.placement_status,
                        "position_status": projection.get("position_status"),
                        "map_feature_id": projection.get("map_feature_id"),
                        "published_map_revision": projection.get(
                            "published_map_revision"
                        ),
                        "map_point": projection.get("map_position"),
                    }
                )
        map_point = projection.get("map_position")
        if projection.get("position_status") == "mapped" and map_point:
            group["map_status"] = "mapped"
            group["map_point"] = {
                "left_pct": float(map_point["left_pct"]),
                "top_pct": float(map_point["top_pct"]),
                "width_pct": float(map_point["width_pct"]),
                "height_pct": float(map_point["height_pct"]),
                "z_index": int(map_point.get("z_index") or 0),
            }
        else:
            group["map_status"] = (
                "text_only"
                if group.get("source_type") == "production_direct"
                or group.get("needs_relocation")
                or group.get("requires_attention")
                or group.get("location_id") is None
                else "unmapped"
            )
            group["map_point"] = None
    return ordered


def _pick_item_response(
    db: Session,
    item: DeliveryPickTaskItem,
    *,
    include_location_plan: bool = True,
    read_context: dict | None = None,
) -> dict:
    component_lines = _pick_item_component_lines(
        db,
        item,
        read_context=read_context,
    )
    location_lines, location_plan_complete = (
        _pick_item_location_plan(
            db,
            item=item,
            component_lines=component_lines,
            read_context=read_context,
        )
        if include_location_plan
        else ([], True)
    )
    return {
        "id": item.id,
        "delivery_item_id": item.delivery_item_id,
        "order_item_id": item.order_item_id,
        "original_quantity": item.original_quantity,
        "planned_quantity": item.original_quantity,
        "picked_quantity": item.picked_quantity,
        "status": item.status,
        "pick_status": item.status,
        "product_code": item.product_code_snapshot,
        "product_name": item.product_name_snapshot,
        "specification": item.specification_snapshot,
        "is_composite_bom": bool(component_lines),
        "component_lines": component_lines,
        "location_lines": location_lines,
        "location_plan_complete": location_plan_complete,
        "updated_at": utc_naive_to_api(item.updated_at) if item.updated_at else None,
    }


def _pick_task_response(
    db: Session,
    task: DeliveryPickTask,
    *,
    include_location_plan: bool = True,
    read_context: dict | None = None,
) -> dict:
    task_items = list(task.items)
    read_context = read_context or _pick_task_read_context(db, task_items)
    item_responses = [
        _pick_item_response(
            db,
            item,
            include_location_plan=include_location_plan,
            read_context=read_context,
        )
        for item in task_items
    ]
    location_groups = (
        _pick_location_groups(
            db,
            item_responses,
            read_context=read_context,
        )
        if include_location_plan
        else []
    )
    from app.services.fixed_shelf import enrich_pick_groups, display_specification
    enrich_pick_groups(db, location_groups)
    item_product_ids = {}
    for item in item_responses:
        order_item = read_context['order_items'].get(item.get('order_item_id'))
        delivery_item = read_context['delivery_items'].get(item.get('delivery_item_id'))
        item_product_ids[item['id']] = order_item.product_id if order_item else delivery_item.product_id if delivery_item else None
    customer_codes = dict(db.execute(select(Product.id, Product.customer_material_code).where(Product.id.in_([pid for pid in item_product_ids.values() if pid]))).all())
    for item in item_responses:
        item['specification_display'] = display_specification(item.get('specification'))
        item['customer_inventory_code'] = customer_codes.get(item_product_ids.get(item['id']))
    from app.services.fixed_shelf_staging import enrich_staging
    if include_location_plan:
        enrich_staging(db, item_responses, item_product_ids)
    print_version_payload = {
        "task_id": task.id,
        "snapshot_version": task.snapshot_version,
        "items": [
            {
                "id": item.get("id"),
                "planned_quantity": item.get("planned_quantity"),
                "picked_quantity": item.get("picked_quantity"),
                "pick_status": item.get("pick_status"),
                "product_code": item.get("product_code"),
                "customer_inventory_code": item.get("customer_inventory_code"),
                "specification_display": item.get("specification_display"),
                "staged_quantity": item.get("staged_quantity"),
                "staging_candidates": item.get("staging_candidates"),
            }
            for item in item_responses
        ],
        "location_groups": [
            {
                "key": group.get("key"),
                "sequence": group.get("recommended_sequence"),
                "location_id": group.get("location_id"),
                "location_code": group.get("location_code"),
                "lines": [
                    {
                        "pick_item_id": line.get("pick_item_id"),
                        "source_type": line.get("source_type"),
                        "location_id": line.get("location_id"),
                        "pallet_id": line.get("pallet_id"),
                        "pick_quantity": line.get("pick_quantity"),
                        "lot_id": line.get("lot_id"),
                        "units_per_bundle": line.get("units_per_bundle"),
                        "putaway_pending": line.get("putaway_pending"),
                        "specification_display": line.get("specification_display"),
                        "customer_inventory_code": line.get("customer_inventory_code"),
                    }
                    for line in group.get("lines") or []
                ],
            }
            for group in location_groups
        ],
    }
    print_version_digest = hashlib.sha256(
        json.dumps(
            print_version_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:10]
    exception_items = [
        {
            **item_response,
            "customer_name": task.customer.name if task.customer else None,
        }
        for item, item_response in zip(task_items, item_responses, strict=True)
        if item.status in {"partial", "no_stock"}
        or int(item.picked_quantity) > int(item.original_quantity)
    ]
    assigned_user = db.get(User, task.assigned_to) if task.assigned_to else None
    return {
        "id": task.id,
        "delivery_id": task.delivery_id,
        "customer_id": task.customer_id,
        "customer_name": task.customer.name if task.customer else None,
        "delivery_number": task.delivery.delivery_number if task.delivery else None,
        "planned_delivery_date": (
            task.delivery.delivery_date.isoformat()
            if task.delivery and task.delivery.delivery_date
            else None
        ),
        "status": task.status,
        "has_exception": bool(exception_items) or task.status == "exception",
        "exceptions": exception_items,
        "snapshot_version": task.snapshot_version,
        "print_version": f"{task.snapshot_version}-{print_version_digest}",
        "assigned_to": task.assigned_to,
        "assigned_to_name": (
            assigned_user.display_name or assigned_user.real_name or assigned_user.username
            if assigned_user is not None
            else None
        ),
        "assignment_required": task.assigned_to is None,
        "created_at": utc_naive_to_api(task.created_at) if task.created_at else None,
        "submitted_at": utc_naive_to_api(task.submitted_at) if task.submitted_at else None,
        "applied_at": utc_naive_to_api(task.applied_at) if task.applied_at else None,
        "dispatched_at": utc_naive_to_api(task.dispatched_at) if task.dispatched_at else None,
        "items": item_responses,
        "location_groups": location_groups,
        "location_plan_complete": (
            all(bool(item.get("location_plan_complete")) for item in item_responses)
            if include_location_plan
            else None
        ),
        "as_of": utc_naive_to_api(_utc_now()),
    }


def _delivery_pick_task(db: Session, delivery_id: int) -> DeliveryPickTask | None:
    return db.scalar(
        select(DeliveryPickTask)
        .where(DeliveryPickTask.delivery_id == delivery_id)
        .order_by(DeliveryPickTask.id.desc())
    )


def _discard_delivery_pick_task(
    db: Session,
    *,
    delivery_id: int,
    user: User,
    reason: str,
) -> None:
    task = _delivery_pick_task(db, delivery_id)
    if task is None:
        return
    _write_audit(
        db,
        user=user,
        action="PICK_TASK_INVALIDATED",
        resource="DeliveryPickTask",
        entity_id=task.id,
        details={"delivery_id": delivery_id, "reason": reason},
        description="送货草稿变更，已作废旧拿货快照",
    )
    db.delete(task)
    db.flush()


class ForceCloseRequest(BaseModel):
    reason: str | None = None

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


def _pending_query(
    *,
    order_item_id: int | None = None,
    customer_id: int | None = None,
    customer_ids: set[int] | None = None,
    inventory_keyword: str | None = None,
    customer_po_keyword: str | None = None,
    product_name_keyword: str | None = None,
    general_keyword: str | None = None,
):
    production_task_exists = exists(
        select(ProductionTask.id).where(
            ProductionTask.order_item_id == OrderItem.id,
        )
    )
    production_task_ready = exists(
        select(ProductionTask.id).where(
            ProductionTask.order_item_id == OrderItem.id,
            ProductionTask.status.in_(["completed", "not_required"]),
        )
    )
    active_finished_reserved = (
        select(
            func.coalesce(
                func.sum(
                    func.coalesce(
                        InventoryReservation.credited_requirement_quantity, 0
                    )
                    - InventoryReservation.released_requirement_quantity
                ),
                0,
            )
        )
        .where(
            InventoryReservation.order_item_id == OrderItem.id,
            InventoryReservation.reservation_type == "finished_order",
            InventoryReservation.status != "cancelled",
        )
        .correlate(OrderItem)
        .scalar_subquery()
    )
    receipt_auto_finished = exists(
        select(ProductionCompletion.id).where(
            ProductionCompletion.order_item_id == OrderItem.id,
            ProductionCompletion.status == "posted",
            ProductionCompletion.origin == "receipt_auto",
        )
    )
    receipt_auto_finished_reserved = (
        select(
            func.coalesce(
                func.sum(remaining_finished_order_credit_expression()),
                0,
            )
        )
        .where(
            InventoryReservation.order_item_id == OrderItem.id,
            InventoryReservation.reservation_type == "finished_order",
            InventoryReservation.sales_order_item_bom_component_id.is_(None),
            InventoryReservation.status != "cancelled",
        )
        .correlate(OrderItem)
        .scalar_subquery()
    )
    receipt_auto_delivery_ready = and_(
        receipt_auto_finished,
        receipt_auto_finished_reserved > 0,
        OrderItem.quantity > OrderItem.delivered_quantity,
    )
    active_semi_for_requirement = (
        select(
            func.coalesce(
                func.sum(
                    func.coalesce(
                        InventoryReservation.credited_requirement_quantity, 0
                    )
                    - InventoryReservation.released_requirement_quantity
                ),
                0,
            )
        )
        .where(
            InventoryReservation.semi_requirement_id
            == OrderItemSemiRequirement.id,
            InventoryReservation.reservation_type == "semi_order",
            InventoryReservation.status != "cancelled",
        )
        .correlate(OrderItemSemiRequirement)
        .scalar_subquery()
    )
    semi_requirement_count = (
        select(func.count(OrderItemSemiRequirement.id))
        .where(OrderItemSemiRequirement.order_item_id == OrderItem.id)
        .correlate(OrderItem)
        .scalar_subquery()
    )
    uncovered_semi_requirement_count = (
        select(func.count(OrderItemSemiRequirement.id))
        .where(
            OrderItemSemiRequirement.order_item_id == OrderItem.id,
            active_semi_for_requirement
            < OrderItemSemiRequirement.required_piece_quantity,
        )
        .correlate(OrderItem)
        .scalar_subquery()
    )
    semi_fully_covered = and_(
        semi_requirement_count > 0,
        uncovered_semi_requirement_count == 0,
    )
    received_telescoping_components = (
        select(func.count(RequisitionItem.id))
        .where(
            RequisitionItem.order_item_id == OrderItem.id,
            RequisitionItem.status == "已入库",
            or_(
                RequisitionItem.product_name_snapshot.like("%-盖"),
                RequisitionItem.product_name_snapshot.like("%-底"),
            ),
        )
        .correlate(OrderItem)
        .scalar_subquery()
    )
    required_external_components = (
        select(func.count(SalesOrderItemExternalComponent.id))
        .where(
            SalesOrderItemExternalComponent.sales_order_item_id == OrderItem.id,
            SalesOrderItemExternalComponent.is_required.is_(True),
        )
        .correlate(OrderItem)
        .scalar_subquery()
    )
    received_required_external_components = (
        select(func.count(ExternalPackagingPurchaseItem.id))
        .join(
            ExternalPackagingPurchaseOrder,
            ExternalPackagingPurchaseOrder.id
            == ExternalPackagingPurchaseItem.purchase_order_id,
        )
        .outerjoin(
            ExternalPackagingPurchaseCancellation,
            ExternalPackagingPurchaseCancellation.purchase_order_id
            == ExternalPackagingPurchaseOrder.id,
        )
        .join(
            SalesOrderItemExternalComponent,
            SalesOrderItemExternalComponent.id
            == ExternalPackagingPurchaseItem.order_component_id,
        )
        .where(
            ExternalPackagingPurchaseItem.sales_order_item_id == OrderItem.id,
            ExternalPackagingPurchaseOrder.status == "confirmed",
            ExternalPackagingPurchaseCancellation.id.is_(None),
            SalesOrderItemExternalComponent.is_required.is_(True),
            ExternalPackagingPurchaseItem.purchase_quantity
            <= select(
                func.coalesce(
                    func.sum(ExternalPackagingReceiptItem.received_quantity), 0
                )
            )
            .where(
                ExternalPackagingReceiptItem.purchase_item_id
                == ExternalPackagingPurchaseItem.id
            )
            .correlate(ExternalPackagingPurchaseItem)
            .scalar_subquery(),
        )
        .correlate(OrderItem)
        .scalar_subquery()
    )
    external_packaging_received = and_(
        required_external_components > 0,
        received_required_external_components == required_external_components,
    )
    external_packaging_gate = or_(
        required_external_components == 0,
        external_packaging_received,
    )
    query = (
        select(
            OrderItem.id.label("item_id"),
            OrderItem.id.label("order_item_id"),
            Order.id.label("order_id"),
            Order.order_number,
            Order.customer_po,
            Order.customer_id,
            Customer.name.label("customer_name"),
            Product.id.label("product_id"),
            Product.product_code,
            OrderItem.snapshot_product_name.label("product_name"),
            OrderItem.snapshot_spec.label("specification"),
            Product.length_mm.label("product_length_mm"),
            Product.width_mm.label("product_width_mm"),
            Product.height_mm.label("product_height_mm"),
            OrderItem.snapshot_material.label("material"),
            OrderItem.flute_type,
            OrderItem.snapshot_production_notes.label("production_notes"),
            OrderItem.quantity,
            OrderItem.delivered_quantity,
            (
                OrderItem.quantity - OrderItem.delivered_quantity
            ).label("remaining_quantity"),
            Order.delivery_date,
        )
        .join(Order, Order.id == OrderItem.order_id)
        .join(Customer, Customer.id == Order.customer_id)
        .join(Product, Product.id == OrderItem.product_id)
        .where(
            external_packaging_gate,
            or_(
                production_task_ready,
                receipt_auto_delivery_ready,
                and_(
                    ~production_task_exists,
                    or_(
                        OrderItem.material_status == "received",
                        active_finished_reserved >= OrderItem.quantity,
                        semi_fully_covered,
                        received_telescoping_components > 0,
                        external_packaging_received,
                    ),
                ),
            ),
            OrderItem.is_force_closed.is_(False),
            OrderItem.delivered_quantity < OrderItem.quantity,
            Order.status.in_(DELIVERY_CANDIDATE_ORDER_STATUSES),
        )
    )
    if order_item_id is not None:
        query = query.where(OrderItem.id == int(order_item_id))
    if customer_id is not None:
        query = query.where(Order.customer_id == customer_id)
    if customer_ids is not None:
        query = query.where(Order.customer_id.in_(customer_ids))
    inventory_keyword = (inventory_keyword or "").strip()
    customer_po_keyword = (customer_po_keyword or "").strip()
    product_name_keyword = (product_name_keyword or "").strip()
    general_keyword = (general_keyword or "").strip()

    if inventory_keyword:
        lowered = inventory_keyword.lower()
        fuzzy = f"%{inventory_keyword}%"
        query = query.where(
            or_(
                func.lower(Product.product_code) == lowered,
                Product.product_code.like(fuzzy),
            )
        )
    if customer_po_keyword:
        lowered = customer_po_keyword.lower()
        fuzzy = f"%{customer_po_keyword}%"
        query = query.where(
            or_(
                func.lower(Order.customer_po) == lowered,
                Order.customer_po.like(fuzzy),
            )
        )
    if product_name_keyword:
        query = query.where(
            OrderItem.snapshot_product_name.like(f"%{product_name_keyword}%")
        )

    rank_ordering = []
    if general_keyword:
        lowered = general_keyword.lower()
        prefix = f"{general_keyword}%"
        fuzzy = f"%{general_keyword}%"
        query = query.where(
            or_(
                func.lower(Product.product_code) == lowered,
                Product.product_code.like(prefix),
                Product.product_code.like(fuzzy),
                func.lower(Order.customer_po) == lowered,
                Order.customer_po.like(prefix),
                Order.customer_po.like(fuzzy),
                OrderItem.snapshot_product_name.like(fuzzy),
            )
        )
        rank_ordering.append(
            case(
                (func.lower(Product.product_code) == lowered, 0),
                (func.lower(Order.customer_po) == lowered, 1),
                (Product.product_code.like(prefix), 2),
                (Order.customer_po.like(prefix), 3),
                (Product.product_code.like(fuzzy), 4),
                (Order.customer_po.like(fuzzy), 5),
                else_=6,
            )
        )
    elif inventory_keyword:
        lowered = inventory_keyword.lower()
        prefix = f"{inventory_keyword}%"
        fuzzy = f"%{inventory_keyword}%"
        rank_ordering.append(
            case(
                (func.lower(Product.product_code) == lowered, 0),
                (Product.product_code.like(prefix), 1),
                (Product.product_code.like(fuzzy), 2),
                else_=3,
            )
        )
    elif customer_po_keyword:
        lowered = customer_po_keyword.lower()
        prefix = f"{customer_po_keyword}%"
        fuzzy = f"%{customer_po_keyword}%"
        rank_ordering.append(
            case(
                (func.lower(Order.customer_po) == lowered, 0),
                (Order.customer_po.like(prefix), 1),
                (Order.customer_po.like(fuzzy), 2),
                else_=3,
            )
        )

    query = query.order_by(
        *rank_ordering,
        OrderItem.material_received_at.desc(),
        OrderItem.created_at.desc(),
        OrderItem.id.desc(),
    )
    return query


def _delivery_or_404(db: Session, delivery_id: int) -> Delivery:
    delivery = db.get(Delivery, delivery_id)
    if delivery is None:
        raise HTTPException(status_code=404, detail="送货单不存在")
    return delivery


def _visible_customer_ids(user: User, db: Session) -> set[int] | None:
    if has_unrestricted_customer_access(user, db):
        return None
    return customer_scope_ids(user, db)


def _delivery_for_user(
    db: Session,
    delivery_id: int,
    user: User,
) -> Delivery:
    delivery = _delivery_or_404(db, delivery_id)
    require_customer_access(delivery.customer_id, user, db)
    return delivery


def _delivery_for_label_user(
    db: Session,
    delivery_id: int,
    user: User,
) -> Delivery:
    if not has_permission(user, "orders.view"):
        raise HTTPException(status_code=403, detail="无产品标签查看权限")
    return _delivery_for_user(db, delivery_id, user)


def _require_order_item_customer_access(
    db: Session,
    order_item_id: int,
    user: User,
) -> None:
    customer_id = db.scalar(
        select(Order.customer_id)
        .join(OrderItem, OrderItem.order_id == Order.id)
        .where(OrderItem.id == order_item_id)
    )
    if customer_id is not None:
        require_customer_access(customer_id, user, db)


def _delivery_location_metadata(
    db: Session,
    location: WarehouseLocation | None,
    *,
    finished: bool,
    projection_context: dict | None = None,
    space_ledger_enabled: bool | None = None,
) -> dict:
    warehouse_types = (
        {"finished", "shared"}
        if finished
        else {"semi_finished", "shared"}
    )
    resolved_projection_context = projection_context
    if location is not None and resolved_projection_context is None:
        resolved_projection_context = load_warehouse_location_projection_contexts(
            db,
            [location],
        ).get(int(location.id), {})
    projection = (
        warehouse_location_projection(
            location,
            **(resolved_projection_context or {}),
        )
        if location is not None
        else {}
    )
    resolved_space_ledger_enabled = (
        has_space_ledger(db)
        if space_ledger_enabled is None
        else bool(space_ledger_enabled)
    )
    operational_projection_context = (
        resolved_projection_context if resolved_space_ledger_enabled else None
    )
    return {
        "warehouse_floor": location.warehouse_floor if location else None,
        "area_code": location.area_code if location else None,
        "position_status": projection.get("position_status", "unlocated"),
        "map_issue": projection.get("map_issue"),
        "location_operational": bool(
            location is not None
            and operational_location_issue(
                db,
                location,
                warehouse_types=warehouse_types,
                projection_context=operational_projection_context,
            )
            is None
        ),
    }


def _composite_inventory_sources_for_order_item(
    db: Session,
    *,
    order_item: OrderItem,
    planned_delivery_quantity: int,
    delivery_item_id: int | None,
    dispatched: bool,
    read_context: dict | None = None,
) -> list[dict]:
    """Expose N039 component pick sources without treating pieces as parent sets."""
    demands = delivery_component_demands(db, order_item.id)
    demand_by_snapshot = {row.snapshot_id: row for row in demands}
    if not demand_by_snapshot:
        return []

    def source_payload(
        *,
        demand,
        source_type: str,
        quantity: int,
        reservation: InventoryReservation | None = None,
    ) -> dict:
        lot = (
            (
                (read_context.get("lots") or {}).get(
                    int(reservation.inventory_lot_id)
                )
                if read_context is not None
                else db.get(InventoryLot, reservation.inventory_lot_id)
            )
            if reservation
            else None
        )
        location = (
            (
                (read_context.get("locations") or {}).get(
                    int(lot.warehouse_location_id)
                )
                if read_context is not None
                else db.get(WarehouseLocation, lot.warehouse_location_id)
            )
            if lot is not None
            and lot.warehouse_location_id is not None
            else None
        )
        projection_context = (
            (read_context.get("location_projection_contexts") or {}).get(
                int(location.id)
            )
            if read_context is not None and location is not None
            else None
        )
        return {
            "source_type": source_type,
            "reservation_id": reservation.id if reservation else None,
            "lot_id": lot.id if lot else None,
            "lot_number": lot.lot_number if lot else None,
            "location_id": location.id if location else None,
            "location_code": location.location_code if location else None,
            "location_name": (
                employee_location_name(
                    location,
                    area=(projection_context or {}).get("area"),
                    floor=(projection_context or {}).get("floor"),
                )
                if location
                else None
            ),
            **_delivery_location_metadata(
                db,
                location,
                finished=source_type == "component_stock",
                projection_context=projection_context,
                space_ledger_enabled=(
                    bool(read_context.get("space_ledger_enabled"))
                    if read_context is not None
                    else None
                ),
            ),
            "component_type": "bom_component",
            "component_snapshot_id": demand.snapshot_id,
            "component_code": demand.component_code,
            "component_name": demand.component_name,
            "quantity_per_set": demand.quantity_per_set,
            "quantity_to_pick_stock": quantity if reservation else 0,
            "quantity_to_pick_requirement": quantity,
        }

    items: list[dict] = []
    if dispatched and delivery_item_id is not None:
        root_snapshot_id = _delivery_graph_root_snapshot(db, delivery_item_id)
        stock_allocations = db.scalars(
            select(DeliveryInventoryAllocation)
            .join(
                InventoryReservation,
                InventoryReservation.id == DeliveryInventoryAllocation.reservation_id,
            )
            .where(
                DeliveryInventoryAllocation.delivery_item_id == delivery_item_id,
                _delivery_reservation_condition(db, delivery_item_id),
            )
            .order_by(DeliveryInventoryAllocation.id)
        ).all()
        for allocation in stock_allocations:
            reservation = db.get(InventoryReservation, allocation.reservation_id)
            if reservation is None:
                continue
            demand = demand_by_snapshot.get(
                reservation.sales_order_item_bom_component_id or root_snapshot_id
            )
            quantity = max(
                int(allocation.consumed_stock_quantity or 0)
                - int(allocation.reversed_stock_quantity or 0),
                0,
            )
            if demand is not None and quantity > 0:
                items.append(
                    source_payload(
                        demand=demand,
                        source_type="component_stock",
                        quantity=quantity,
                        reservation=reservation,
                    )
                )
        direct_allocations = db.scalars(
            select(BomComponentDirectDeliveryAllocation)
            .where(
                BomComponentDirectDeliveryAllocation.delivery_item_id
                == delivery_item_id
            )
            .order_by(BomComponentDirectDeliveryAllocation.id)
        ).all()
        for allocation in direct_allocations:
            demand = demand_by_snapshot.get(
                allocation.sales_order_item_bom_component_id
            )
            quantity = max(
                int(allocation.consumed_quantity or 0)
                - int(allocation.reversed_quantity or 0),
                0,
            )
            if demand is not None and quantity > 0:
                items.append(
                    source_payload(
                        demand=demand,
                        source_type="component_direct",
                        quantity=quantity,
                    )
                )
        return items

    delivery_sets = max(int(planned_delivery_quantity or 0), 0)
    required_quantities = delivery_component_required_quantities(
        db,
        order_item_id=order_item.id,
        delivery_sets=delivery_sets,
    )
    for demand in demands:
        required_pieces = required_quantities.get(demand.snapshot_id, 0)
        if required_pieces <= 0:
            continue
        remaining = required_pieces
        reservations = db.scalars(
            select(InventoryReservation)
            .join(InventoryLot, InventoryLot.id == InventoryReservation.inventory_lot_id)
            .where(
                InventoryReservation.order_item_id == order_item.id,
                _snapshot_reservation_condition(db, demand.snapshot_id),
                InventoryReservation.status != "cancelled",
                InventoryReservation.reserved_stock_quantity
                > InventoryReservation.consumed_stock_quantity
                + InventoryReservation.released_stock_quantity,
            )
            .order_by(
                *inventory_fifo_order_columns(),
                InventoryReservation.id,
            )
        ).all()
        for reservation in reservations:
            available = max(
                int(reservation.reserved_stock_quantity or 0)
                - int(reservation.consumed_stock_quantity or 0)
                - int(reservation.released_stock_quantity or 0),
                0,
            )
            picked = min(available, remaining)
            if picked > 0:
                items.append(
                    source_payload(
                        demand=demand,
                        source_type="component_stock",
                        quantity=picked,
                        reservation=reservation,
                    )
                )
                remaining -= picked
            if remaining <= 0:
                break
        direct_quantity = min(
            max(component_availability(db, demand.snapshot_id).direct_quantity, 0),
            remaining,
        )
        if direct_quantity > 0:
            items.append(
                source_payload(
                    demand=demand,
                    source_type="component_direct",
                    quantity=direct_quantity,
                )
            )
    return items


def _delivery_component_lines(
    db: Session,
    *,
    order_item: OrderItem,
    planned_delivery_quantity: int,
    delivery_item_id: int | None = None,
    dispatched: bool = False,
) -> list[dict]:
    return project_delivery_component_lines(
        db,
        order_item=order_item,
        planned_delivery_quantity=planned_delivery_quantity,
        delivery_item_id=delivery_item_id,
        dispatched=dispatched,
    )


def _actual_goods_lines(
    *,
    order_item_id: int,
    product_code: str | None,
    product_name: str | None,
    specification: str | None,
    parent_quantity: int,
    component_lines: list[dict],
    fulfillment_mode: str = "component_delivery",
) -> list[dict]:
    return project_actual_goods_lines(
        order_item_id=order_item_id,
        product_code=product_code,
        product_name=product_name,
        specification=specification,
        parent_quantity=parent_quantity,
        component_lines=component_lines,
        fulfillment_mode=fulfillment_mode,
    )


def _customer_document_fulfillment_mode(
    *,
    frozen_order_mode: str | None,
    current_product_mode: str | None,
) -> str:
    """Resolve the customer-facing delivery projection without changing stock facts.

    An order explicitly frozen as parent delivery must stay consolidated.  A later
    explicit product-master switch to parent delivery may also consolidate an older
    component-delivery order on customer documents, which is the safe one-way
    compatibility path for already-created delivery notes.  Switching the master
    back to component delivery never expands an order that was frozen as parent.
    """

    return resolve_customer_document_fulfillment_mode(
        frozen_order_mode=frozen_order_mode,
        current_product_mode=current_product_mode,
    )


def _delivery_document_goods_lines(
    *,
    order_item_id: int,
    product_code: str | None,
    product_name: str | None,
    specification: str | None,
    parent_quantity: int,
    kit_metadata: dict,
) -> list[dict]:
    """Project one saved delivery item into customer-visible goods lines."""

    if kit_metadata.get("is_composite_bom"):
        fulfillment_mode = (
            kit_metadata.get("composite_fulfillment_mode")
            or "component_delivery"
        )
    else:
        fulfillment_mode = "parent_delivery"
    return _actual_goods_lines(
        order_item_id=order_item_id,
        product_code=product_code,
        product_name=product_name,
        specification=specification,
        parent_quantity=parent_quantity,
        component_lines=kit_metadata.get("component_lines") or [],
        fulfillment_mode=fulfillment_mode,
    )


def _delivery_kit_metadata(
    db: Session,
    order_item: OrderItem | None,
    *,
    planned_delivery_quantity: int | None = None,
    delivery_item_id: int | None = None,
    dispatched: bool = False,
    current_product_fulfillment_mode: str | None = None,
) -> dict:
    if order_item is None or not is_composite_order_item(db, order_item.id):
        return {
            "is_composite_bom": False,
            "composite_fulfillment_mode": None,
            "kit_availability": None,
            "available_sets": None,
            "missing_components": [],
            "component_lines": [],
        }
    receipt_auto_finished = _has_receipt_auto_finished_fact(db, order_item.id)
    availability = kit_availability(db, order_item.id)
    if receipt_auto_finished and not _uses_composite_inventory(db, order_item.id):
        available_finished = _delivery_remaining_quantity(db, order_item)
        availability = {
            **availability,
            "available_sets": available_finished,
            "missing_components": [],
            "fulfillment_basis": "receipt_auto_finished",
        }
    planned_quantity = (
        int(availability.get("available_sets") or 0)
        if planned_delivery_quantity is None
        else max(int(planned_delivery_quantity or 0), 0)
    )
    component_lines = _delivery_component_lines(
        db,
        order_item=order_item,
        planned_delivery_quantity=planned_quantity,
        delivery_item_id=delivery_item_id,
        dispatched=dispatched,
    )
    if (
        current_product_fulfillment_mode is None
        and order_item.product_id is not None
    ):
        product = db.get(Product, order_item.product_id)
        current_product_fulfillment_mode = (
            product.composite_fulfillment_mode if product is not None else None
        )
    return {
        "is_composite_bom": True,
        "composite_fulfillment_mode": _customer_document_fulfillment_mode(
            frozen_order_mode=getattr(
                order_item,
                "composite_fulfillment_mode_snapshot",
                None,
            ),
            current_product_mode=current_product_fulfillment_mode,
        ),
        "kit_availability": availability,
        "available_sets": int(availability.get("available_sets") or 0),
        "missing_components": availability.get("missing_components") or [],
        "component_lines": component_lines,
    }


def _inventory_sources_for_order_item(
    db: Session,
    *,
    order_item: OrderItem,
    planned_delivery_quantity: int,
    delivery_item_id: int | None = None,
    dispatched: bool = False,
    composite_hint: bool | None = None,
    read_context: dict | None = None,
) -> list[dict]:
    composite_source_mode = _uses_composite_inventory(db, order_item.id, composite_hint)
    if composite_source_mode:
        return _composite_inventory_sources_for_order_item(
            db,
            order_item=order_item,
            planned_delivery_quantity=planned_delivery_quantity,
            delivery_item_id=delivery_item_id,
            dispatched=dispatched,
            read_context=read_context,
        )
    reservations = db.scalars(
        select(InventoryReservation)
        .join(InventoryLot, InventoryLot.id == InventoryReservation.inventory_lot_id)
        .where(
            InventoryReservation.order_item_id == order_item.id,
            InventoryReservation.reservation_type.in_(
                ("finished_order", "finished_surplus_delivery", "semi_order")
            ),
            or_(
                InventoryReservation.reservation_type != "finished_order",
                InventoryReservation.sales_order_item_bom_component_id.is_(None),
            ),
            InventoryReservation.status != "cancelled",
            func.coalesce(InventoryReservation.credited_requirement_quantity, 0)
            > InventoryReservation.released_requirement_quantity,
        )
        .order_by(
            case(
                (InventoryReservation.reservation_type == "finished_order", 0),
                else_=1,
            ),
            *inventory_fifo_order_columns(),
            InventoryReservation.id,
        )
    ).all()
    from app.services.fixed_shelf_staging import prioritize_staged
    reservations = prioritize_staged(db, reservations, delivery_item_id)
    requirements = {
        row.id: row
        for row in db.scalars(
            select(OrderItemSemiRequirement).where(
                OrderItemSemiRequirement.order_item_id == order_item.id
            )
        ).all()
    }
    allocated_by_reservation: dict[int, tuple[int, int]] = {}
    if delivery_item_id is not None:
        allocations = db.scalars(
            select(DeliveryInventoryAllocation).where(
                DeliveryInventoryAllocation.delivery_item_id == delivery_item_id
            )
        ).all()
        for allocation in allocations:
            stock = (
                int(allocation.consumed_stock_quantity)
                - int(allocation.reversed_stock_quantity or 0)
            )
            credit = (
                int(allocation.credited_requirement_quantity)
                - int(allocation.reversed_requirement_quantity or 0)
            )
            previous_stock, previous_credit = allocated_by_reservation.get(
                allocation.reservation_id, (0, 0)
            )
            allocated_by_reservation[allocation.reservation_id] = (
                previous_stock + stock,
                previous_credit + credit,
            )

    planned_stock: dict[int, tuple[int, int]] = {}
    remaining_unreserved_surplus = 0
    if not dispatched:
        target_delivered = (
            int(order_item.delivered_quantity or 0)
            + max(int(planned_delivery_quantity), 0)
        )
        target_order_delivered = min(
            target_delivered, int(order_item.quantity or 0)
        )
        finished_coverage = finished_order_source_coverage(reservations)
        finished_target = min(target_order_delivered, finished_coverage)
        finished_current = sum(
            int(row.consumed_stock_quantity or 0)
            for row in reservations
            if row.reservation_type == "finished_order"
        )
        finished_need = max(finished_target - finished_current, 0)
        for reservation in reservations:
            available_stock = (
                int(reservation.reserved_stock_quantity)
                - int(reservation.consumed_stock_quantity or 0)
                - int(reservation.released_stock_quantity or 0)
            )
            if reservation.reservation_type == "finished_order":
                stock = min(available_stock, finished_need)
                planned_stock[reservation.id] = (stock, stock)
                finished_need -= stock

        target_surplus = max(
            target_delivered - int(order_item.quantity or 0), 0
        )
        consumed_surplus = sum(
            int(row.consumed_stock_quantity or 0)
            for row in reservations
            if row.reservation_type == "finished_surplus_delivery"
        )
        remaining_unreserved_surplus = max(
            target_surplus - consumed_surplus, 0
        )
        for reservation in reservations:
            if reservation.reservation_type != "finished_surplus_delivery":
                continue
            available_stock = (
                int(reservation.reserved_stock_quantity)
                - int(reservation.consumed_stock_quantity or 0)
                - int(reservation.released_stock_quantity or 0)
            )
            stock = min(available_stock, remaining_unreserved_surplus)
            if stock <= 0:
                continue
            planned_stock[reservation.id] = (stock, stock)
            remaining_unreserved_surplus -= stock

        semi_boxes = max(target_order_delivered - finished_coverage, 0)
        for requirement in requirements.values():
            coverage = active_semi_requirement_credited_quantity(db, requirement.id)
            target_pieces = min(
                semi_boxes * max(int(requirement.pieces_per_box or 1), 1),
                coverage,
            )
            current_pieces = sum(
                int(row.consumed_requirement_quantity or 0)
                for row in reservations
                if row.semi_requirement_id == requirement.id
            )
            for reservation in reservations:
                if reservation.semi_requirement_id != requirement.id:
                    continue
                available_stock = (
                    int(reservation.reserved_stock_quantity)
                    - int(reservation.consumed_stock_quantity or 0)
                    - int(reservation.released_stock_quantity or 0)
                )
                yield_factor = max(int(reservation.yield_factor or 1), 1)
                needed_pieces = max(target_pieces - current_pieces, 0)
                stock = min(
                    available_stock,
                    (needed_pieces + yield_factor - 1) // yield_factor,
                )
                credit = min(
                    max(
                        int(reservation.credited_requirement_quantity or 0)
                        - int(reservation.consumed_requirement_quantity or 0)
                        - int(reservation.released_requirement_quantity or 0),
                        0,
                    ),
                    stock * yield_factor,
                )
                planned_stock[reservation.id] = (stock, credit)
                current_pieces += credit

    items: list[dict] = []
    for reservation in reservations:
        lot = (
            (read_context.get("lots") or {}).get(
                int(reservation.inventory_lot_id)
            )
            if read_context is not None
            else db.get(InventoryLot, reservation.inventory_lot_id)
        )
        if lot is None:
            continue
        location = (
            (read_context.get("locations") or {}).get(
                int(lot.warehouse_location_id)
            )
            if read_context is not None and lot.warehouse_location_id is not None
            else (
                db.get(WarehouseLocation, lot.warehouse_location_id)
                if lot.warehouse_location_id is not None
                else None
            )
        )
        projection_context = (
            (read_context.get("location_projection_contexts") or {}).get(
                int(location.id)
            )
            if read_context is not None and location is not None
            else None
        )
        requirement = requirements.get(reservation.semi_requirement_id)
        pick_stock, pick_credit = (
            allocated_by_reservation.get(reservation.id, (0, 0))
            if dispatched
            else planned_stock.get(reservation.id, (0, 0))
        )
        if pick_stock <= 0 and pick_credit <= 0:
            continue
        items.append(
            {
                "source_type": (
                    "finished"
                    if reservation.reservation_type
                    in {"finished_order", "finished_surplus_delivery"}
                    else "semi_finished"
                ),
                "reservation_id": reservation.id,
                "lot_id": lot.id,
                "lot_number": lot.lot_number,
                "location_id": location.id if location else None,
                "location_code": location.location_code if location else None,
                "location_name": (
                    employee_location_name(
                        location,
                        area=(projection_context or {}).get("area"),
                        floor=(projection_context or {}).get("floor"),
                    )
                    if location
                    else None
                ),
                **_delivery_location_metadata(
                    db,
                    location,
                    finished=reservation.reservation_type
                    in {"finished_order", "finished_surplus_delivery"},
                    projection_context=projection_context,
                    space_ledger_enabled=(
                        bool(read_context.get("space_ledger_enabled"))
                        if read_context is not None
                        else None
                    ),
                ),
                "component_type": (
                    requirement.component_type if requirement else "whole"
                ),
                "yield_factor": max(int(reservation.yield_factor or 1), 1),
                "reserved_stock_quantity": int(
                    reservation.reserved_stock_quantity or 0
                ),
                "remaining_reserved_stock_quantity": max(
                    int(reservation.reserved_stock_quantity or 0)
                    - int(reservation.consumed_stock_quantity or 0)
                    - int(reservation.released_stock_quantity or 0),
                    0,
                ),
                "covered_requirement_quantity": max(
                    int(reservation.credited_requirement_quantity or 0)
                    - int(reservation.released_requirement_quantity or 0),
                    0,
                ),
                "quantity_to_pick_stock": pick_stock,
                "quantity_to_pick_requirement": pick_credit,
            }
        )
    if not dispatched and remaining_unreserved_surplus > 0:
        order = db.get(Order, order_item.order_id)
        if order is not None:
            surplus_lots = list(
                db.scalars(
                    select(InventoryLot)
                    .join(
                        FinishedGoodsInventoryDetail,
                        FinishedGoodsInventoryDetail.inventory_lot_id
                        == InventoryLot.id,
                    )
                    .where(
                        InventoryLot.inventory_type == "finished",
                        InventoryLot.status == "active",
                        InventoryLot.quantity_available > 0,
                        InventoryLot.source_type.in_(
                            (
                                "production_surplus",
                                "production_completion",
                                "transfer",
                            )
                        ),
                        FinishedGoodsInventoryDetail.product_id
                        == order_item.product_id,
                        FinishedGoodsInventoryDetail.is_general.is_(False),
                        FinishedGoodsInventoryDetail.owner_customer_id
                        == order.customer_id,
                    )
                    .order_by(
                        case(
                            (
                                (InventoryLot.source_ref_type == "production_completion")
                                & (
                                    InventoryLot.source_ref_id.in_(
                                        select(ProductionCompletion.id).where(
                                            ProductionCompletion.order_item_id
                                            == order_item.id,
                                            ProductionCompletion.status == "posted",
                                        )
                                    )
                                ),
                                0,
                            ),
                            else_=1,
                        ),
                        *inventory_fifo_order_columns(),
                    )
                ).all()
            )
            for lot in surplus_lots:
                if remaining_unreserved_surplus <= 0:
                    break
                take = min(
                    int(lot.quantity_available or 0),
                    remaining_unreserved_surplus,
                )
                if take <= 0:
                    continue
                if read_context is not None:
                    read_context.setdefault("lots", {})[int(lot.id)] = lot
                items.append(
                    {
                        "source_type": "finished",
                        "reservation_id": None,
                        "lot_id": int(lot.id),
                        "lot_number": lot.lot_number,
                        "location_id": lot.warehouse_location_id,
                        "location_code": None,
                        "location_name": None,
                        "component_type": "whole",
                        "yield_factor": 1,
                        "reserved_stock_quantity": 0,
                        "remaining_reserved_stock_quantity": 0,
                        "covered_requirement_quantity": take,
                        "quantity_to_pick_stock": take,
                        "quantity_to_pick_requirement": take,
                    }
                )
                remaining_unreserved_surplus -= take
    return items


def _delivery_item_rows(db: Session, delivery_ids: list[int]) -> list[dict]:
    if not delivery_ids:
        return []
    rows = db.execute(
        select(
                DeliveryItem.id,
                DeliveryItem.delivery_id,
                DeliveryItem.source_type,
                DeliveryItem.order_item_id,
                DeliveryItem.product_id,
                DeliveryItem.delivered_quantity,
                DeliveryItem.unit_price_snapshot.label("unit_price"),
                DeliveryItem.price_source,
                DeliveryItem.ordered_quantity_snapshot,
                DeliveryItem.order_remaining_snapshot,
                DeliveryItem.over_delivery_quantity,
                DeliveryItem.over_delivery_confirmed_by,
                DeliveryItem.over_delivery_reason,
                DeliveryItem.remarks,
                Order.id.label("order_id"),
                Order.order_number,
                func.coalesce(DeliveryItem.customer_po_snapshot, Order.customer_po).label("customer_po"),
                func.coalesce(
                    func.nullif(DeliveryItem.product_code_snapshot, ""),
                    func.nullif(OrderItem.snapshot_product_code, ""),
                ).label("product_code"),
                func.coalesce(
                    func.nullif(DeliveryItem.product_name_snapshot, ""),
                    func.nullif(OrderItem.snapshot_product_name, ""),
                ).label("product_name"),
                func.coalesce(
                    func.nullif(DeliveryItem.product_code_snapshot, ""),
                    func.nullif(OrderItem.snapshot_product_code, ""),
                    Product.product_code,
                ).label("search_product_code"),
                func.coalesce(
                    func.nullif(DeliveryItem.product_name_snapshot, ""),
                    func.nullif(OrderItem.snapshot_product_name, ""),
                    Product.product_name,
                ).label("search_product_name"),
                DeliveryItem.specification_snapshot.label(
                    "delivery_specification_snapshot"
                ),
                OrderItem.snapshot_spec.label("order_specification_snapshot"),
                Product.length_mm.label("product_length_mm"),
                Product.width_mm.label("product_width_mm"),
                Product.height_mm.label("product_height_mm"),
                Product.composite_fulfillment_mode.label(
                    "current_product_fulfillment_mode"
                ),
                DeliveryItem.unit_snapshot,
                OrderItem.snapshot_production_notes.label("production_notes"),
        )
        .outerjoin(OrderItem, OrderItem.id == DeliveryItem.order_item_id)
        .outerjoin(Order, Order.id == OrderItem.order_id)
        .outerjoin(
            Product,
            Product.id
            == func.coalesce(DeliveryItem.product_id, OrderItem.product_id),
        )
        .where(
            DeliveryItem.delivery_id.in_(delivery_ids),
            DeliveryItem.is_current.is_(True),
        )
        .order_by(DeliveryItem.delivery_id, DeliveryItem.id)
    ).all()
    result: list[dict] = []
    for row in rows:
        mapping = dict(row._mapping)
        mapping["specification"] = resolved_product_specification(
            mapping.pop("delivery_specification_snapshot", None),
            fallback_snapshots=(
                mapping.pop("order_specification_snapshot", None),
            ),
            length_mm=mapping.pop("product_length_mm", None),
            customer_name_snapshots=(mapping.get("product_name"),),
            width_mm=mapping.pop("product_width_mm", None),
            height_mm=mapping.pop("product_height_mm", None),
        )
        result.append(mapping)
    return result


_DELIVERY_SEARCH_FIELDS = ("customer_po", "product_code", "product_name")


def _empty_delivery_search_matches(active_fields: list[str] | None = None) -> dict:
    return {
        "active_fields": list(active_fields or []),
        "matched_fields": [],
        "matched_values": {},
        "items": [],
    }


def _delivery_search_matches(
    db: Session,
    delivery_ids: list[int],
    *,
    customer_po: str | None,
    product_code: str | None,
    product_name: str | None,
) -> dict[int, dict]:
    """Explain detailed list matches without exposing out-of-scope rows."""

    filters = {
        "customer_po": str(customer_po or "").strip(),
        "product_code": str(product_code or "").strip(),
        "product_name": str(product_name or "").strip(),
    }
    active_fields = [field for field in _DELIVERY_SEARCH_FIELDS if filters[field]]
    if not delivery_ids or not active_fields:
        return {}

    payloads = {
        int(delivery_id): _empty_delivery_search_matches(active_fields)
        for delivery_id in delivery_ids
    }
    matched_values: dict[int, dict[str, list[str]]] = {
        int(delivery_id): {field: [] for field in active_fields}
        for delivery_id in delivery_ids
    }
    for item in _delivery_item_rows(db, delivery_ids):
        delivery_id = int(item["delivery_id"])
        if delivery_id not in payloads:
            continue
        item_matches: list[str] = []
        for field in active_fields:
            value_key = (
                f"search_{field}"
                if field in {"product_code", "product_name"}
                else field
            )
            value = str(item.get(value_key) or "").strip()
            if not value or filters[field].casefold() not in value.casefold():
                continue
            item_matches.append(field)
            if value not in matched_values[delivery_id][field]:
                matched_values[delivery_id][field].append(value)
        if item_matches:
            payloads[delivery_id]["items"].append(
                {
                    "delivery_item_id": int(item["id"]),
                    "matched_fields": item_matches,
                }
            )

    for delivery_id, payload in payloads.items():
        payload["matched_fields"] = [
            field for field in active_fields if matched_values[delivery_id][field]
        ]
        payload["matched_values"] = {
            field: matched_values[delivery_id][field]
            for field in payload["matched_fields"]
        }
    return payloads


def _delivery_pick_task_summary(
    db: Session,
    task: DeliveryPickTask,
    *,
    customer_name: str | None,
    delivery_number: str | None,
    items: list[DeliveryPickTaskItem],
    assigned_user: User | None = None,
    assignee_preloaded: bool = False,
) -> dict:
    """List-view summary; full location planning remains on the detail routes."""

    exception_items = [
        {
            "id": item.id,
            "delivery_item_id": item.delivery_item_id,
            "order_item_id": item.order_item_id,
            "planned_quantity": item.original_quantity,
            "picked_quantity": item.picked_quantity,
            "status": item.status,
            "pick_status": item.status,
            "product_code": item.product_code_snapshot,
            "product_name": item.product_name_snapshot,
            "specification": item.specification_snapshot,
            "customer_name": customer_name,
        }
        for item in items
        if item.status in {"partial", "no_stock"}
        or int(item.picked_quantity) > int(item.original_quantity)
    ]
    if not assignee_preloaded and task.assigned_to:
        assigned_user = db.get(User, task.assigned_to)
    return {
        "id": task.id,
        "delivery_id": task.delivery_id,
        "customer_id": task.customer_id,
        "customer_name": customer_name,
        "delivery_number": delivery_number,
        "status": task.status,
        "has_exception": bool(exception_items) or task.status == "exception",
        "exceptions": exception_items,
        "snapshot_version": task.snapshot_version,
        "assigned_to": task.assigned_to,
        "assigned_to_name": (
            assigned_user.display_name or assigned_user.real_name or assigned_user.username
            if assigned_user is not None
            else None
        ),
        "assignment_required": task.assigned_to is None,
        "created_at": utc_naive_to_api(task.created_at) if task.created_at else None,
        "submitted_at": utc_naive_to_api(task.submitted_at) if task.submitted_at else None,
        "applied_at": utc_naive_to_api(task.applied_at) if task.applied_at else None,
        "dispatched_at": utc_naive_to_api(task.dispatched_at) if task.dispatched_at else None,
        "items": [],
        "location_groups": [],
        "location_plan_complete": None,
    }


def _delivery_pick_task_list_summaries(
    db: Session,
    tasks: list[DeliveryPickTask],
) -> list[dict]:
    """Build mobile list summaries with a fixed number of batch queries."""
    if not tasks:
        return []
    task_ids = [task.id for task in tasks]
    customer_ids = {task.customer_id for task in tasks}
    delivery_ids = {task.delivery_id for task in tasks}
    assigned_ids = {task.assigned_to for task in tasks if task.assigned_to is not None}
    customers = {
        row.id: row
        for row in db.scalars(select(Customer).where(Customer.id.in_(customer_ids))).all()
    }
    deliveries = {
        row.id: row
        for row in db.scalars(select(Delivery).where(Delivery.id.in_(delivery_ids))).all()
    }
    assignees = {
        row.id: row
        for row in db.scalars(select(User).where(User.id.in_(assigned_ids))).all()
    } if assigned_ids else {}
    items_by_task: dict[int, list[DeliveryPickTaskItem]] = {}
    for item in db.scalars(
        select(DeliveryPickTaskItem)
        .where(DeliveryPickTaskItem.task_id.in_(task_ids))
        .order_by(DeliveryPickTaskItem.task_id, DeliveryPickTaskItem.id)
    ).all():
        items_by_task.setdefault(int(item.task_id), []).append(item)
    return [
        _delivery_pick_task_summary(
            db,
            task,
            customer_name=(
                customers[task.customer_id].name
                if task.customer_id in customers
                else None
            ),
            delivery_number=(
                deliveries[task.delivery_id].delivery_number
                if task.delivery_id in deliveries
                else None
            ),
            items=items_by_task.get(task.id, []),
            assigned_user=assignees.get(task.assigned_to),
            assignee_preloaded=True,
        )
        for task in tasks
    ]


def _empty_delivery_kit_metadata() -> dict:
    return {
        "is_composite_bom": False,
        "composite_fulfillment_mode": None,
        "kit_availability": None,
        "available_sets": None,
        "missing_components": [],
        "component_lines": [],
    }


def _delivery_list_positive_integer(value: object, *, field: str) -> int:
    try:
        decimal_value = Decimal(str(value))
    except (ArithmeticError, TypeError, ValueError) as error:
        raise CompositeBomWorkflowError(f"{field}必须为正整数") from error
    if decimal_value <= 0 or decimal_value != decimal_value.to_integral_value():
        raise CompositeBomWorkflowError(f"{field}必须为正整数")
    return int(decimal_value)


def _delivery_list_component_demands(
    snapshots: list[SalesOrderItemBomComponent],
    adjustments: dict[int, tuple[int, int]],
) -> dict[int, list[ComponentDemand]]:
    result: dict[int, list[ComponentDemand]] = {}
    for snapshot in snapshots:
        delta_sets, delta_pieces = adjustments.get(int(snapshot.id), (0, 0))
        quantity_per_set = _delivery_list_positive_integer(
            snapshot.quantity_per_set,
            field="每套组件数量",
        )
        effective_sets = int(snapshot.order_set_quantity) + delta_sets
        if effective_sets < 0:
            raise CompositeBomWorkflowError("组件调整后的有效套数不能小于0")
        required_piece_quantity = (
            _delivery_list_positive_integer(
                snapshot.required_piece_quantity,
                field="组件需求件数",
            )
            + delta_pieces
        )
        if required_piece_quantity <= 0:
            raise CompositeBomWorkflowError("组件调整后的需求件数必须大于0")
        demand = ComponentDemand(
            snapshot_id=int(snapshot.id),
            order_item_id=int(snapshot.sales_order_item_id),
            component_product_id=int(snapshot.component_product_id),
            component_code=snapshot.snapshot_component_product_code,
            component_name=snapshot.snapshot_component_product_name,
            specification=snapshot.snapshot_component_spec,
            quantity_per_set=quantity_per_set,
            is_required=bool(snapshot.is_required),
            effective_sets=effective_sets,
            required_piece_quantity=required_piece_quantity,
            show_on_delivery=bool(
                getattr(snapshot, "show_on_delivery", True)
            ),
        )
        result.setdefault(demand.order_item_id, []).append(demand)
    return result


def _delivery_list_location_operational(
    context: dict,
    location: WarehouseLocation | None,
    *,
    finished: bool,
) -> bool:
    if location is None or not location.is_active:
        return False
    if (location.placement_status or "placed") != "placed":
        return False
    accepted_types = {"finished", "shared"} if finished else {"semi_finished", "shared"}
    if location.warehouse_type not in accepted_types:
        return False
    if not context["has_space_ledger"]:
        return True
    if location.warehouse_floor is None or not str(location.area_code or "").strip():
        return False
    floor = context["floors_by_number"].get(int(location.warehouse_floor))
    if floor is None or floor.construction_status != "enabled":
        return False
    area = context["areas_by_floor_code"].get(
        (int(floor.id), str(location.area_code).strip().upper())
    )
    return bool(area is not None and area.construction_status == "enabled")


def _delivery_list_location_payload(
    context: dict,
    reservation: InventoryReservation | None,
    *,
    finished: bool,
) -> tuple[InventoryLot | None, WarehouseLocation | None, dict]:
    lot = (
        context["lots"].get(int(reservation.inventory_lot_id))
        if reservation is not None
        else None
    )
    location = (
        context["locations"].get(int(lot.warehouse_location_id))
        if lot is not None
        else None
    )
    projection_context = (
        context.get("location_projection_contexts", {}).get(int(location.id), {})
        if location is not None
        else {}
    )
    projection = (
        warehouse_location_projection(location, **projection_context)
        if location is not None
        else {}
    )
    return lot, location, {
        "location_name": (
            employee_location_name(
                location,
                area=projection_context.get("area"),
                floor=projection_context.get("floor"),
            )
            if location is not None
            else None
        ),
        "warehouse_floor": location.warehouse_floor if location else None,
        "area_code": location.area_code if location else None,
        "position_status": projection.get("position_status", "unlocated"),
        "map_issue": projection.get("map_issue"),
        "location_operational": _delivery_list_location_operational(
            context,
            location,
            finished=finished,
        ),
    }


def _delivery_list_component_required_quantities(
    context: dict,
    *,
    order_item: OrderItem,
    delivery_sets: int,
) -> dict[int, int]:
    sets = int(delivery_sets)
    if sets < 0:
        raise CompositeBomWorkflowError("送货套数不能小于0")
    delivered_after = max(int(order_item.delivered_quantity or 0), 0) + sets
    result: dict[int, int] = {}
    for demand in context["component_demands_by_order_item"].get(order_item.id, []):
        consumed = context["component_delivered_by_snapshot"].get(demand.snapshot_id, 0)
        target_after_dispatch = min(
            delivered_after * demand.quantity_per_set,
            demand.required_piece_quantity,
        )
        result[demand.snapshot_id] = max(target_after_dispatch - consumed, 0)
    return result


def _delivery_list_kit_metadata(
    context: dict,
    *,
    order_item: OrderItem,
    planned_delivery_quantity: int,
    delivery_item_id: int,
    dispatched: bool,
    current_product_fulfillment_mode: str | None = None,
) -> dict:
    demands = context["component_demands_by_order_item"].get(order_item.id, [])
    if not demands:
        return _empty_delivery_kit_metadata()

    delivered_sets = int(order_item.delivered_quantity or 0)
    effective_sets = max(int(order_item.quantity or 0), 0)
    available_sets = max(effective_sets - delivered_sets, 0)
    missing_components: list[dict] = []
    components: list[dict] = []
    for demand in demands:
        stock_quantity = sum(
            max(
                int(row.reserved_stock_quantity or 0)
                - int(row.consumed_stock_quantity or 0)
                - int(row.released_stock_quantity or 0),
                0,
            )
            for row in context["reservations_by_snapshot"].get(demand.snapshot_id, [])
            if row.reservation_type == "finished_order"
            and row.status in ACTIVE_RESERVATION_STATUSES
        )
        direct_quantity = context["component_direct_available_by_snapshot"].get(
            demand.snapshot_id,
            0,
        )
        component_available = stock_quantity + direct_quantity
        consumed = context["component_delivered_by_snapshot"].get(demand.snapshot_id, 0)
        remaining_target = max(demand.required_piece_quantity - consumed, 0)
        shortage = max(remaining_target - component_available, 0)
        components.append(
            {
                "snapshot_id": demand.snapshot_id,
                "component_code": demand.component_code,
                "component_name": demand.component_name,
                "quantity_per_set": demand.quantity_per_set,
                "is_required": demand.is_required,
                "stock_quantity": stock_quantity,
                "direct_quantity": direct_quantity,
                "available_quantity": component_available,
                "required_piece_quantity": demand.required_piece_quantity,
                "target_quantity": demand.required_piece_quantity,
                "delivered_quantity": consumed,
                "remaining_quantity": remaining_target,
                "delivered_piece_quantity": consumed,
                "remaining_required_piece_quantity": remaining_target,
            }
        )
        if not demand.is_required:
            continue
        total_coverable = consumed + component_available
        if total_coverable < demand.required_piece_quantity:
            component_sets = max(
                total_coverable // demand.quantity_per_set - delivered_sets,
                0,
            )
            available_sets = min(available_sets, component_sets)
            missing_components.append(
                {
                    "snapshot_id": demand.snapshot_id,
                    "component_code": demand.component_code,
                    "component_name": demand.component_name,
                    "required_quantity": remaining_target,
                    "available_quantity": component_available,
                    "shortage_quantity": shortage,
                }
            )
    availability = {
        "applicable": True,
        "effective_sets": effective_sets,
        "delivered_sets": delivered_sets,
        "available_sets": available_sets,
        "components": components,
        "missing_components": missing_components,
    }

    document_quantities = (
        context["component_quantities_by_delivery_item"].get(delivery_item_id, {})
        if dispatched
        else _delivery_list_component_required_quantities(
            context,
            order_item=order_item,
            delivery_sets=max(int(planned_delivery_quantity or 0), 0),
        )
    )
    component_lines = [
        {
            "line_type": "component",
            "component_snapshot_id": demand.snapshot_id,
            "component_product_id": demand.component_product_id,
            "product_code": demand.component_code,
            "product_name": demand.component_name,
            "specification": demand.specification,
            "unit": "PCS",
            "quantity_per_set": demand.quantity_per_set,
            "target_quantity": demand.required_piece_quantity,
            "delivered_quantity": context["component_delivered_by_snapshot"].get(
                demand.snapshot_id,
                0,
            ),
            "remaining_quantity": max(
                demand.required_piece_quantity
                - context["component_delivered_by_snapshot"].get(demand.snapshot_id, 0),
                0,
            ),
            "planned_delivery_quantity": document_quantities.get(demand.snapshot_id, 0),
            "pricing_included": False,
            "show_on_delivery": bool(demand.show_on_delivery),
            "pricing_note": "套内组件，不单独计价",
            "independent_return_receipt": False,
            "independent_statement": False,
        }
        for demand in demands
    ]
    return {
        "is_composite_bom": True,
        "composite_fulfillment_mode": _customer_document_fulfillment_mode(
            frozen_order_mode=getattr(
                order_item,
                "composite_fulfillment_mode_snapshot",
                None,
            ),
            current_product_mode=current_product_fulfillment_mode,
        ),
        "kit_availability": availability,
        "available_sets": available_sets,
        "missing_components": missing_components,
        "component_lines": component_lines,
    }


def _delivery_list_composite_inventory_sources(
    context: dict,
    *,
    order_item: OrderItem,
    planned_delivery_quantity: int,
    delivery_item_id: int,
    dispatched: bool,
) -> list[dict]:
    demands = context["component_demands_by_order_item"].get(order_item.id, [])
    demand_by_snapshot = {row.snapshot_id: row for row in demands}
    if not demands:
        return []

    def source_payload(
        *,
        demand: ComponentDemand,
        source_type: str,
        quantity: int,
        reservation: InventoryReservation | None = None,
    ) -> dict:
        lot, location, location_metadata = _delivery_list_location_payload(
            context,
            reservation,
            finished=source_type == "component_stock",
        )
        return {
            "source_type": source_type,
            "reservation_id": reservation.id if reservation else None,
            "lot_id": lot.id if lot else None,
            "lot_number": lot.lot_number if lot else None,
            "location_id": location.id if location else None,
            "location_code": location.location_code if location else None,
            **location_metadata,
            "component_type": "bom_component",
            "component_snapshot_id": demand.snapshot_id,
            "component_code": demand.component_code,
            "component_name": demand.component_name,
            "quantity_per_set": demand.quantity_per_set,
            "quantity_to_pick_stock": quantity if reservation else 0,
            "quantity_to_pick_requirement": quantity,
        }

    items: list[dict] = []
    if dispatched:
        for allocation in context["delivery_allocations_by_delivery_item"].get(
            delivery_item_id,
            [],
        ):
            reservation = context["reservations"].get(int(allocation.reservation_id))
            if reservation is None:
                continue
            demand = demand_by_snapshot.get(reservation.sales_order_item_bom_component_id)
            quantity = max(
                int(allocation.consumed_stock_quantity or 0)
                - int(allocation.reversed_stock_quantity or 0),
                0,
            )
            if demand is not None and quantity > 0:
                items.append(
                    source_payload(
                        demand=demand,
                        source_type="component_stock",
                        quantity=quantity,
                        reservation=reservation,
                    )
                )
        for allocation in context["direct_allocations_by_delivery_item"].get(
            delivery_item_id,
            [],
        ):
            demand = demand_by_snapshot.get(
                int(allocation.sales_order_item_bom_component_id)
            )
            quantity = max(
                int(allocation.consumed_quantity or 0)
                - int(allocation.reversed_quantity or 0),
                0,
            )
            if demand is not None and quantity > 0:
                items.append(
                    source_payload(
                        demand=demand,
                        source_type="component_direct",
                        quantity=quantity,
                    )
                )
        return items

    required_quantities = _delivery_list_component_required_quantities(
        context,
        order_item=order_item,
        delivery_sets=max(int(planned_delivery_quantity or 0), 0),
    )
    for demand in demands:
        remaining = required_quantities.get(demand.snapshot_id, 0)
        if remaining <= 0:
            continue
        reservations = [
            row
            for row in context["reservations_by_snapshot"].get(demand.snapshot_id, [])
            if row.order_item_id == order_item.id
            and row.status != "cancelled"
            and int(row.reserved_stock_quantity or 0)
            > int(row.consumed_stock_quantity or 0)
            + int(row.released_stock_quantity or 0)
            and int(row.inventory_lot_id) in context["lots"]
        ]
        reservations.sort(
            key=lambda row: (
                *inventory_fifo_sort_key(context["lots"][int(row.inventory_lot_id)]),
                int(row.id),
            )
        )
        for reservation in reservations:
            available = max(
                int(reservation.reserved_stock_quantity or 0)
                - int(reservation.consumed_stock_quantity or 0)
                - int(reservation.released_stock_quantity or 0),
                0,
            )
            picked = min(available, remaining)
            if picked > 0:
                items.append(
                    source_payload(
                        demand=demand,
                        source_type="component_stock",
                        quantity=picked,
                        reservation=reservation,
                    )
                )
                remaining -= picked
            if remaining <= 0:
                break
        direct_quantity = min(
            max(
                context["component_direct_available_by_snapshot"].get(
                    demand.snapshot_id,
                    0,
                ),
                0,
            ),
            remaining,
        )
        if direct_quantity > 0:
            items.append(
                source_payload(
                    demand=demand,
                    source_type="component_direct",
                    quantity=direct_quantity,
                )
            )
    return items


def _delivery_list_standard_inventory_sources(
    context: dict,
    *,
    order_item: OrderItem,
    planned_delivery_quantity: int,
    delivery_item_id: int,
    dispatched: bool,
) -> list[dict]:
    reservations = [
        row
        for row in context["reservations_by_order_item"].get(order_item.id, [])
        if row.reservation_type in {"finished_order", "semi_order"}
        and (
            row.reservation_type != "finished_order"
            or row.sales_order_item_bom_component_id is None
        )
        and row.status != "cancelled"
        and int(row.credited_requirement_quantity or 0)
        > int(row.released_requirement_quantity or 0)
        and int(row.inventory_lot_id) in context["lots"]
    ]
    reservations.sort(
        key=lambda row: (
            0 if row.reservation_type == "finished_order" else 1,
            *inventory_fifo_sort_key(context["lots"][int(row.inventory_lot_id)]),
            int(row.id),
        )
    )
    requirements = {
        int(row.id): row
        for row in context["requirements_by_order_item"].get(order_item.id, [])
    }
    allocated_by_reservation: dict[int, tuple[int, int]] = {}
    for allocation in context["delivery_allocations_by_delivery_item"].get(
        delivery_item_id,
        [],
    ):
        stock = int(allocation.consumed_stock_quantity or 0) - int(
            allocation.reversed_stock_quantity or 0
        )
        credit = int(allocation.credited_requirement_quantity or 0) - int(
            allocation.reversed_requirement_quantity or 0
        )
        previous_stock, previous_credit = allocated_by_reservation.get(
            int(allocation.reservation_id),
            (0, 0),
        )
        allocated_by_reservation[int(allocation.reservation_id)] = (
            previous_stock + stock,
            previous_credit + credit,
        )

    planned_stock: dict[int, tuple[int, int]] = {}
    if not dispatched:
        target_delivered = min(
            int(order_item.delivered_quantity or 0)
            + max(int(planned_delivery_quantity or 0), 0),
            int(order_item.quantity or 0),
        )
        finished_coverage = context["active_finished_by_order_item"].get(
            order_item.id,
            0,
        )
        finished_target = min(target_delivered, finished_coverage)
        finished_current = sum(
            int(row.consumed_stock_quantity or 0)
            for row in reservations
            if row.reservation_type == "finished_order"
        )
        finished_need = max(finished_target - finished_current, 0)
        for reservation in reservations:
            available_stock = (
                int(reservation.reserved_stock_quantity or 0)
                - int(reservation.consumed_stock_quantity or 0)
                - int(reservation.released_stock_quantity or 0)
            )
            if reservation.reservation_type == "finished_order":
                stock = min(available_stock, finished_need)
                planned_stock[int(reservation.id)] = (stock, stock)
                finished_need -= stock

        semi_boxes = max(target_delivered - finished_coverage, 0)
        for requirement in requirements.values():
            coverage = context["active_semi_by_requirement"].get(requirement.id, 0)
            target_pieces = min(
                semi_boxes * max(int(requirement.pieces_per_box or 1), 1),
                coverage,
            )
            current_pieces = sum(
                int(row.consumed_requirement_quantity or 0)
                for row in reservations
                if row.semi_requirement_id == requirement.id
            )
            for reservation in reservations:
                if reservation.semi_requirement_id != requirement.id:
                    continue
                available_stock = (
                    int(reservation.reserved_stock_quantity or 0)
                    - int(reservation.consumed_stock_quantity or 0)
                    - int(reservation.released_stock_quantity or 0)
                )
                yield_factor = max(int(reservation.yield_factor or 1), 1)
                needed_pieces = max(target_pieces - current_pieces, 0)
                stock = min(
                    available_stock,
                    (needed_pieces + yield_factor - 1) // yield_factor,
                )
                credit = min(
                    max(
                        int(reservation.credited_requirement_quantity or 0)
                        - int(reservation.consumed_requirement_quantity or 0)
                        - int(reservation.released_requirement_quantity or 0),
                        0,
                    ),
                    stock * yield_factor,
                )
                planned_stock[int(reservation.id)] = (stock, credit)
                current_pieces += credit

    items: list[dict] = []
    for reservation in reservations:
        lot, location, location_metadata = _delivery_list_location_payload(
            context,
            reservation,
            finished=reservation.reservation_type == "finished_order",
        )
        if lot is None:
            continue
        requirement = requirements.get(reservation.semi_requirement_id)
        pick_stock, pick_credit = (
            allocated_by_reservation.get(int(reservation.id), (0, 0))
            if dispatched
            else planned_stock.get(int(reservation.id), (0, 0))
        )
        if pick_stock <= 0 and pick_credit <= 0:
            continue
        items.append(
            {
                "source_type": (
                    "finished"
                    if reservation.reservation_type == "finished_order"
                    else "semi_finished"
                ),
                "reservation_id": reservation.id,
                "lot_id": lot.id,
                "lot_number": lot.lot_number,
                "location_id": location.id if location else None,
                "location_code": location.location_code if location else None,
                **location_metadata,
                "component_type": requirement.component_type if requirement else "whole",
                "yield_factor": max(int(reservation.yield_factor or 1), 1),
                "reserved_stock_quantity": int(
                    reservation.reserved_stock_quantity or 0
                ),
                "remaining_reserved_stock_quantity": max(
                    int(reservation.reserved_stock_quantity or 0)
                    - int(reservation.consumed_stock_quantity or 0)
                    - int(reservation.released_stock_quantity or 0),
                    0,
                ),
                "covered_requirement_quantity": max(
                    int(reservation.credited_requirement_quantity or 0)
                    - int(reservation.released_requirement_quantity or 0),
                    0,
                ),
                "quantity_to_pick_stock": pick_stock,
                "quantity_to_pick_requirement": pick_credit,
            }
        )
    return items


def _unordered_finished_allocation_payload(row: UnorderedFinishedDeliveryAllocation) -> dict:
    return {
        "id": row.id,
        "inventory_lot_id": row.inventory_lot_id,
        "lot_number": row.lot_number_snapshot,
        "location_id": row.warehouse_location_id_snapshot,
        "location_code": row.warehouse_location_code_snapshot,
        "pallet_code": row.pallet_code_snapshot,
        "quantity": int(row.planned_quantity or 0),
        "planned_quantity": int(row.planned_quantity or 0),
        "consumed_quantity": int(row.consumed_quantity or 0),
        "restored_quantity": int(row.restored_quantity or 0),
        "status": row.status,
    }


def _delivery_list_page_context(db: Session, delivery_ids: list[int]) -> dict:
    """Batch data used only by the desktop delivery list page."""

    deliveries = {
        delivery.id: delivery
        for delivery in db.scalars(
            select(Delivery).where(Delivery.id.in_(delivery_ids))
        ).all()
    } if delivery_ids else {}
    customer_ids = {delivery.customer_id for delivery in deliveries.values()}
    customers = {
        customer.id: customer
        for customer in db.scalars(select(Customer).where(Customer.id.in_(customer_ids))).all()
    } if customer_ids else {}
    receipts: dict[int, ReturnReceipt] = {}
    receipt_item_quantities_by_delivery_item: dict[int, int] = {}
    if delivery_ids:
        for receipt, receipt_item in db.execute(
            select(ReturnReceipt, ReturnReceiptItem)
            .outerjoin(
                ReturnReceiptItem,
                ReturnReceiptItem.return_receipt_id == ReturnReceipt.id,
            )
            .where(ReturnReceipt.delivery_id.in_(delivery_ids))
            .order_by(ReturnReceipt.delivery_id, ReturnReceiptItem.id)
        ).all():
            receipts[int(receipt.delivery_id)] = receipt
            if receipt.status == "confirmed" and receipt_item is not None:
                receipt_item_quantities_by_delivery_item[
                    int(receipt_item.delivery_item_id)
                ] = int(receipt_item.actual_received_quantity or 0)
    item_rows = _delivery_item_rows(db, delivery_ids)
    rows_by_delivery: dict[int, list[dict]] = {}
    for row in item_rows:
        rows_by_delivery.setdefault(int(row["delivery_id"]), []).append(row)
    order_ids = {
        int(row["order_id"])
        for row in item_rows
        if row.get("order_id") is not None
    }
    orders = {
        order.id: order
        for order in db.scalars(select(Order).where(Order.id.in_(order_ids))).all()
    } if order_ids else {}
    order_item_ids = {
        int(row["order_item_id"])
        for row in item_rows
        if row.get("order_item_id") is not None
    }
    order_items = {
        item.id: item
        for item in db.scalars(select(OrderItem).where(OrderItem.id.in_(order_item_ids))).all()
    } if order_item_ids else {}
    snapshots = list(
        db.scalars(
            select(SalesOrderItemBomComponent)
            .where(SalesOrderItemBomComponent.sales_order_item_id.in_(order_item_ids))
            .order_by(
                SalesOrderItemBomComponent.sales_order_item_id,
                SalesOrderItemBomComponent.display_order,
                SalesOrderItemBomComponent.id,
            )
        ).all()
    ) if order_item_ids else []
    composite_item_ids = {
        int(row.sales_order_item_id) for row in snapshots
    }
    snapshot_ids = [int(row.id) for row in snapshots]
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
    } if snapshot_ids else {}
    component_demands_by_order_item = _delivery_list_component_demands(
        snapshots,
        adjustments,
    )
    requirements = list(
        db.scalars(
            select(OrderItemSemiRequirement)
            .where(OrderItemSemiRequirement.order_item_id.in_(order_item_ids))
            .order_by(OrderItemSemiRequirement.order_item_id, OrderItemSemiRequirement.id)
        ).all()
    ) if order_item_ids else []
    requirements_by_order_item: dict[int, list[OrderItemSemiRequirement]] = {}
    for requirement in requirements:
        requirements_by_order_item.setdefault(int(requirement.order_item_id), []).append(
            requirement
        )
    requirement_ids = [int(row.id) for row in requirements]

    delivery_item_ids = [int(row["id"]) for row in item_rows]
    delivery_allocations = list(
        db.scalars(
            select(DeliveryInventoryAllocation)
            .where(DeliveryInventoryAllocation.delivery_item_id.in_(delivery_item_ids))
            .order_by(DeliveryInventoryAllocation.id)
        ).all()
    ) if delivery_item_ids else []
    delivery_allocations_by_delivery_item: dict[int, list[DeliveryInventoryAllocation]] = {}
    for allocation in delivery_allocations:
        delivery_allocations_by_delivery_item.setdefault(
            int(allocation.delivery_item_id),
            [],
        ).append(allocation)
    allocation_reservation_ids = {
        int(row.reservation_id) for row in delivery_allocations
    }
    direct_allocations = list(
        db.scalars(
            select(BomComponentDirectDeliveryAllocation)
            .where(
                BomComponentDirectDeliveryAllocation.delivery_item_id.in_(
                    delivery_item_ids
                )
            )
            .order_by(BomComponentDirectDeliveryAllocation.id)
        ).all()
    ) if delivery_item_ids and snapshot_ids else []
    direct_allocations_by_delivery_item: dict[
        int, list[BomComponentDirectDeliveryAllocation]
    ] = {}
    for allocation in direct_allocations:
        direct_allocations_by_delivery_item.setdefault(
            int(allocation.delivery_item_id),
            [],
        ).append(allocation)
    unordered_allocations = list(
        db.scalars(
            select(UnorderedFinishedDeliveryAllocation)
            .where(
                UnorderedFinishedDeliveryAllocation.delivery_item_id.in_(
                    delivery_item_ids
                )
            )
            .order_by(
                UnorderedFinishedDeliveryAllocation.delivery_item_id,
                UnorderedFinishedDeliveryAllocation.id,
            )
        ).all()
    ) if delivery_item_ids else []
    unordered_allocations_by_delivery_item: dict[
        int, list[UnorderedFinishedDeliveryAllocation]
    ] = {}
    for allocation in unordered_allocations:
        unordered_allocations_by_delivery_item.setdefault(
            int(allocation.delivery_item_id),
            [],
        ).append(allocation)

    reservation_conditions = []
    if order_item_ids:
        reservation_conditions.append(
            InventoryReservation.order_item_id.in_(order_item_ids)
        )
    if requirement_ids:
        reservation_conditions.append(
            InventoryReservation.semi_requirement_id.in_(requirement_ids)
        )
    if snapshot_ids:
        reservation_conditions.append(
            InventoryReservation.sales_order_item_bom_component_id.in_(snapshot_ids)
        )
    if allocation_reservation_ids:
        reservation_conditions.append(
            InventoryReservation.id.in_(allocation_reservation_ids)
        )
    reservation_rows = list(
        db.scalars(
            select(InventoryReservation)
            .where(or_(*reservation_conditions))
            .order_by(InventoryReservation.id)
        ).all()
    ) if reservation_conditions else []
    reservations = {int(row.id): row for row in reservation_rows}
    reservations_by_order_item: dict[int, list[InventoryReservation]] = {}
    reservations_by_requirement: dict[int, list[InventoryReservation]] = {}
    reservations_by_snapshot: dict[int, list[InventoryReservation]] = {}
    for reservation in reservation_rows:
        if reservation.order_item_id is not None:
            reservations_by_order_item.setdefault(
                int(reservation.order_item_id),
                [],
            ).append(reservation)
        if reservation.semi_requirement_id is not None:
            reservations_by_requirement.setdefault(
                int(reservation.semi_requirement_id),
                [],
            ).append(reservation)
        if reservation.sales_order_item_bom_component_id is not None:
            reservations_by_snapshot.setdefault(
                int(reservation.sales_order_item_bom_component_id),
                [],
            ).append(reservation)
    reserved_item_ids = {
        int(row.order_item_id)
        for row in reservation_rows
        if row.order_item_id is not None and row.status != "cancelled"
    }
    semi_requirement_item_ids = set(requirements_by_order_item)
    safe_empty_inventory_order_item_ids = (
        order_item_ids - composite_item_ids - reserved_item_ids - semi_requirement_item_ids
    )

    lot_ids = {int(row.inventory_lot_id) for row in reservation_rows}
    lots = {
        int(row.id): row
        for row in db.scalars(
            select(InventoryLot).where(InventoryLot.id.in_(lot_ids))
        ).all()
    } if lot_ids else {}
    location_ids = {int(row.warehouse_location_id) for row in lots.values()}
    locations = {
        int(row.id): row
        for row in db.scalars(
            select(WarehouseLocation).where(WarehouseLocation.id.in_(location_ids))
        ).all()
    } if location_ids else {}
    location_projection_contexts = load_warehouse_location_projection_contexts(
        db,
        locations.values(),
    )
    floors = list(db.scalars(select(WarehouseFloor)).all()) if location_ids else []
    floors_by_number = {int(row.floor_number): row for row in floors}
    floor_ids = [int(row.id) for row in floors]
    areas = list(
        db.scalars(
            select(WarehouseArea).where(WarehouseArea.floor_id.in_(floor_ids))
        ).all()
    ) if floor_ids else []
    areas_by_floor_code = {
        (int(row.floor_id), str(row.area_code).strip().upper()): row
        for row in areas
    }

    finished_reservations_by_order_item: dict[int, list[InventoryReservation]] = {}
    active_semi_by_requirement: dict[int, int] = {}
    for reservation in reservation_rows:
        if reservation.order_item_id is not None:
            finished_reservations_by_order_item.setdefault(
                int(reservation.order_item_id), []
            ).append(reservation)
        if (
            reservation.semi_requirement_id is not None
            and reservation.reservation_type == "semi_order"
            and reservation.status != "cancelled"
        ):
            requirement_id = int(reservation.semi_requirement_id)
            active_semi_by_requirement[requirement_id] = (
                active_semi_by_requirement.get(requirement_id, 0)
                + max(
                    int(reservation.credited_requirement_quantity or 0)
                    - int(reservation.released_requirement_quantity or 0),
                    0,
                )
            )
    active_finished_by_order_item = {
        item_id: finished_order_source_coverage(item_reservations)
        for item_id, item_reservations in finished_reservations_by_order_item.items()
    }

    component_direct_available_by_snapshot: dict[int, int] = {}
    component_delivered_direct = {}
    component_delivered_stock = {}
    if snapshot_ids:
        allocated_direct = func.coalesce(
            func.sum(
                BomComponentDirectDeliveryAllocation.consumed_quantity
                - BomComponentDirectDeliveryAllocation.reversed_quantity
            ),
            0,
        )
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
                ProductionCompletion.inventory_lot_id.is_(None),
                ProductionStockTransfer.id.is_(None),
            )
            .group_by(
                ProductionTask.sales_order_item_bom_component_id,
                ProductionCompletion.id,
                ProductionCompletion.quantity,
            )
        ).all():
            key = int(snapshot_id)
            component_direct_available_by_snapshot[key] = (
                component_direct_available_by_snapshot.get(key, 0)
                + max(int(quantity or 0) - int(allocated or 0), 0)
            )
        component_delivered_direct = {
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
        component_delivered_stock = {
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
    component_delivered_by_snapshot = {
        snapshot_id: component_delivered_direct.get(snapshot_id, 0)
        + component_delivered_stock.get(snapshot_id, 0)
        for snapshot_id in snapshot_ids
    }
    component_quantities_by_delivery_item: dict[int, dict[int, int]] = {}
    for allocation in direct_allocations:
        if allocation.status not in ACTIVE_RESERVATION_STATUSES:
            continue
        quantities = component_quantities_by_delivery_item.setdefault(
            int(allocation.delivery_item_id),
            {},
        )
        snapshot_id = int(allocation.sales_order_item_bom_component_id)
        quantities[snapshot_id] = quantities.get(snapshot_id, 0) + int(
            allocation.consumed_quantity or 0
        ) - int(allocation.reversed_quantity or 0)
    for allocation in delivery_allocations:
        if allocation.status not in ACTIVE_RESERVATION_STATUSES:
            continue
        reservation = reservations.get(int(allocation.reservation_id))
        if reservation is None or reservation.sales_order_item_bom_component_id is None:
            continue
        quantities = component_quantities_by_delivery_item.setdefault(
            int(allocation.delivery_item_id),
            {},
        )
        snapshot_id = int(reservation.sales_order_item_bom_component_id)
        quantities[snapshot_id] = quantities.get(snapshot_id, 0) + int(
            allocation.credited_requirement_quantity or 0
        ) - int(allocation.reversed_requirement_quantity or 0)
    pick_task_rows = db.scalars(
        select(DeliveryPickTask)
        .where(DeliveryPickTask.delivery_id.in_(delivery_ids))
        .order_by(DeliveryPickTask.delivery_id, DeliveryPickTask.id.desc())
    ).all() if delivery_ids else []
    pick_tasks: dict[int, DeliveryPickTask] = {}
    for task in pick_task_rows:
        pick_tasks.setdefault(int(task.delivery_id), task)
    pick_task_ids = [task.id for task in pick_tasks.values()]
    pick_items_by_task: dict[int, list[DeliveryPickTaskItem]] = {}
    if pick_task_ids:
        for item in db.scalars(
            select(DeliveryPickTaskItem)
            .where(DeliveryPickTaskItem.task_id.in_(pick_task_ids))
            .order_by(DeliveryPickTaskItem.id)
        ).all():
            pick_items_by_task.setdefault(int(item.task_id), []).append(item)
    assigned_user_ids = {
        int(task.assigned_to)
        for task in pick_tasks.values()
        if task.assigned_to is not None
    }
    assigned_users = {
        int(row.id): row
        for row in db.scalars(
            select(User).where(User.id.in_(assigned_user_ids))
        ).all()
    } if assigned_user_ids else {}
    internal_remarks = _tianhua_internal_remarks_by_delivery_item(
        db, [int(row["id"]) for row in item_rows]
    )
    inventory_backed_delivery_item_ids = {
        int(allocation.delivery_item_id)
        for allocation in delivery_allocations
        if (
            (reservation := reservations.get(int(allocation.reservation_id)))
            is not None
            and reservation.sales_order_item_bom_component_id is None
        )
    }
    context = {
        "deliveries": deliveries,
        "customers": customers,
        "receipts": receipts,
        "receipt_item_quantities_by_delivery_item": (
            receipt_item_quantities_by_delivery_item
        ),
        "rows_by_delivery": rows_by_delivery,
        "orders": orders,
        "order_items": order_items,
        "safe_empty_inventory_order_item_ids": safe_empty_inventory_order_item_ids,
        "component_demands_by_order_item": component_demands_by_order_item,
        "component_direct_available_by_snapshot": component_direct_available_by_snapshot,
        "component_delivered_by_snapshot": component_delivered_by_snapshot,
        "component_quantities_by_delivery_item": component_quantities_by_delivery_item,
        "requirements_by_order_item": requirements_by_order_item,
        "reservations": reservations,
        "reservations_by_order_item": reservations_by_order_item,
        "reservations_by_requirement": reservations_by_requirement,
        "reservations_by_snapshot": reservations_by_snapshot,
        "delivery_allocations_by_delivery_item": delivery_allocations_by_delivery_item,
        "direct_allocations_by_delivery_item": direct_allocations_by_delivery_item,
        "unordered_allocations_by_delivery_item": unordered_allocations_by_delivery_item,
        "lots": lots,
        "locations": locations,
        "location_projection_contexts": location_projection_contexts,
        "has_space_ledger": bool(floors),
        "floors_by_number": floors_by_number,
        "areas_by_floor_code": areas_by_floor_code,
        "active_finished_by_order_item": active_finished_by_order_item,
        "active_semi_by_requirement": active_semi_by_requirement,
        "pick_tasks": pick_tasks,
        "pick_items_by_task": pick_items_by_task,
        "assigned_users": assigned_users,
        "internal_remarks": internal_remarks,
        "inventory_backed_delivery_item_ids": inventory_backed_delivery_item_ids,
    }
    item_contexts: dict[int, dict] = {}
    for row in item_rows:
        delivery_item_id = int(row["id"])
        if row.get("source_type") == "unordered_finished":
            unordered_payloads = [
                _unordered_finished_allocation_payload(allocation)
                for allocation in unordered_allocations_by_delivery_item.get(
                    delivery_item_id,
                    [],
                )
            ]
            item_contexts[delivery_item_id] = {
                "kit_metadata": _empty_delivery_kit_metadata(),
                "inventory_sources": unordered_payloads,
                "unordered_allocations": unordered_payloads,
            }
            continue
        order_item_id = row.get("order_item_id")
        order_item = order_items.get(order_item_id) if order_item_id is not None else None
        if order_item is None:
            item_contexts[delivery_item_id] = {
                "kit_metadata": _empty_delivery_kit_metadata(),
                "inventory_sources": [],
                "unordered_allocations": [],
            }
            continue
        delivery = deliveries.get(int(row["delivery_id"]))
        dispatched = bool(delivery and delivery.status in {"dispatched", "voided"})
        if order_item.id in composite_item_ids:
            kit_metadata = _delivery_list_kit_metadata(
                context,
                order_item=order_item,
                planned_delivery_quantity=int(row["delivered_quantity"] or 0),
                delivery_item_id=delivery_item_id,
                dispatched=dispatched,
                current_product_fulfillment_mode=row.get(
                    "current_product_fulfillment_mode"
                ),
            )
            inventory_sources = _delivery_list_composite_inventory_sources(
                context,
                order_item=order_item,
                planned_delivery_quantity=int(row["delivered_quantity"] or 0),
                delivery_item_id=delivery_item_id,
                dispatched=dispatched,
            )
        elif order_item.id in safe_empty_inventory_order_item_ids:
            kit_metadata = _empty_delivery_kit_metadata()
            inventory_sources = []
        else:
            kit_metadata = _empty_delivery_kit_metadata()
            inventory_sources = _delivery_list_standard_inventory_sources(
                context,
                order_item=order_item,
                planned_delivery_quantity=int(row["delivered_quantity"] or 0),
                delivery_item_id=delivery_item_id,
                dispatched=dispatched,
            )
        item_contexts[delivery_item_id] = {
            "kit_metadata": kit_metadata,
            "inventory_sources": inventory_sources,
            "unordered_allocations": [],
        }
    context["item_contexts"] = item_contexts
    # Every order rendered by this page is already loaded above.  Reuse that
    # page-local snapshot instead of scanning the complete order table again.
    context["registry"] = {
        int(order_id): str(order.order_number)
        for order_id, order in orders.items()
    }
    return context


def _delivery_list_summary_context(db: Session, delivery_ids: list[int]) -> dict:
    """Build the collapsed desktop list without expanding delivery details.

    The regular response intentionally remains available to existing callers.
    This context is only used by ``view=summary`` and keeps its query families
    fixed for ordinary deliveries, regardless of the number of documents or
    detail rows on the current page.
    """

    if not delivery_ids:
        return {
            "deliveries": {},
            "customers": {},
            "receipts": {},
            "item_counts": {},
            "delivered_quantities": {},
            "receipt_actual_quantities": {},
            "receipt_actual_item_counts": {},
            "actual_goods_quantities": {},
            "pick_tasks": {},
        }

    deliveries = {
        delivery.id: delivery
        for delivery in db.scalars(
            select(Delivery).where(Delivery.id.in_(delivery_ids))
        ).all()
    }
    customer_ids = {delivery.customer_id for delivery in deliveries.values()}
    customers = {
        customer.id: customer
        for customer in db.scalars(
            select(Customer).where(Customer.id.in_(customer_ids))
        ).all()
    } if customer_ids else {}

    receipts: dict[int, ReturnReceipt] = {}
    receipt_actual_quantities: dict[int, int] = {}
    receipt_actual_item_counts: dict[int, int] = {}
    for receipt, receipt_item in db.execute(
        select(ReturnReceipt, ReturnReceiptItem)
        .outerjoin(
            ReturnReceiptItem,
            ReturnReceiptItem.return_receipt_id == ReturnReceipt.id,
        )
        .where(ReturnReceipt.delivery_id.in_(delivery_ids))
        .order_by(ReturnReceipt.delivery_id, ReturnReceiptItem.id)
    ).all():
        delivery_id = int(receipt.delivery_id)
        receipts[delivery_id] = receipt
        if receipt.status == "confirmed":
            receipt_actual_quantities.setdefault(delivery_id, 0)
            receipt_actual_item_counts.setdefault(delivery_id, 0)
            if receipt_item is None:
                continue
            receipt_actual_quantities[delivery_id] += int(
                receipt_item.actual_received_quantity or 0
            )
            receipt_actual_item_counts[delivery_id] += 1

    item_counts: dict[int, int] = {}
    delivered_quantities: dict[int, int] = {}
    actual_goods_quantities: dict[int, int] = {}
    for delivery_id, item_count, delivered_quantity in db.execute(
        select(
            DeliveryItem.delivery_id,
            func.count(DeliveryItem.id),
            func.coalesce(func.sum(DeliveryItem.delivered_quantity), 0),
        )
        .where(
            DeliveryItem.delivery_id.in_(delivery_ids),
            DeliveryItem.is_current.is_(True),
        )
        .group_by(DeliveryItem.delivery_id)
    ).all():
        normalized_id = int(delivery_id)
        item_counts[normalized_id] = int(item_count or 0)
        delivered_quantities[normalized_id] = int(delivered_quantity or 0)
        actual_goods_quantities[normalized_id] = int(delivered_quantity or 0)

    # Ordinary deliveries need no further work.  Composite orders are rare but
    # their collapsed quantity must still include the physical component lines,
    # so calculate only those exceptional rows using the established workflow.
    composite_rows = db.execute(
        select(
            DeliveryItem.id,
            DeliveryItem.delivery_id,
            DeliveryItem.order_item_id,
            DeliveryItem.delivered_quantity,
            Delivery.status,
            OrderItem.delivered_quantity.label("order_delivered_quantity"),
            OrderItem.composite_fulfillment_mode_snapshot.label(
                "composite_fulfillment_mode"
            ),
            Product.composite_fulfillment_mode.label(
                "current_product_fulfillment_mode"
            ),
        )
        .join(Delivery, Delivery.id == DeliveryItem.delivery_id)
        .join(OrderItem, OrderItem.id == DeliveryItem.order_item_id)
        .outerjoin(Product, Product.id == OrderItem.product_id)
        .where(
            DeliveryItem.delivery_id.in_(delivery_ids),
            DeliveryItem.is_current.is_(True),
            DeliveryItem.order_item_id.in_(
                select(SalesOrderItemBomComponent.sales_order_item_id).distinct()
            ),
        )
        .order_by(DeliveryItem.delivery_id, DeliveryItem.id)
    ).all()
    component_delivery_rows = [
        row
        for row in composite_rows
        if _customer_document_fulfillment_mode(
            frozen_order_mode=row.composite_fulfillment_mode,
            current_product_mode=row.current_product_fulfillment_mode,
        )
        == "component_delivery"
    ]
    for row in component_delivery_rows:
        delivery_id = int(row.delivery_id)
        actual_goods_quantities[delivery_id] = max(
            actual_goods_quantities.get(delivery_id, 0)
            - int(row.delivered_quantity or 0),
            0,
        )
    component_quantities = _delivery_summary_component_quantities(
        db, component_delivery_rows
    )
    for delivery_id, component_quantity in component_quantities.items():
        actual_goods_quantities[delivery_id] = (
            actual_goods_quantities.get(delivery_id, 0) + component_quantity
        )

    pick_task_rows = db.scalars(
        select(DeliveryPickTask)
        .where(DeliveryPickTask.delivery_id.in_(delivery_ids))
        .order_by(DeliveryPickTask.delivery_id, DeliveryPickTask.id.desc())
    ).all()
    latest_pick_tasks: dict[int, DeliveryPickTask] = {}
    for task in pick_task_rows:
        latest_pick_tasks.setdefault(int(task.delivery_id), task)
    pick_tasks = {
        int(summary["delivery_id"]): summary
        for summary in _delivery_pick_task_list_summaries(
            db, list(latest_pick_tasks.values())
        )
    }
    return {
        "deliveries": deliveries,
        "customers": customers,
        "receipts": receipts,
        "item_counts": item_counts,
        "delivered_quantities": delivered_quantities,
        "receipt_actual_quantities": receipt_actual_quantities,
        "receipt_actual_item_counts": receipt_actual_item_counts,
        "actual_goods_quantities": actual_goods_quantities,
        "pick_tasks": pick_tasks,
    }


def _delivery_summary_component_quantities(
    db: Session,
    composite_rows: list,
) -> dict[int, int]:
    """Return exact component pieces with a fixed set of aggregate queries."""

    if not composite_rows:
        return {}
    pending_rows = [
        row for row in composite_rows if row.status not in {"dispatched", "voided"}
    ]
    historical_rows = [
        row for row in composite_rows if row.status in {"dispatched", "voided"}
    ]
    result: dict[int, int] = {}

    if historical_rows:
        historical_item_ids = [int(row.id) for row in historical_rows]
        historical_by_item: dict[int, int] = {}
        for delivery_item_id, quantity in db.execute(
            select(
                BomComponentDirectDeliveryAllocation.delivery_item_id,
                func.coalesce(
                    func.sum(
                        BomComponentDirectDeliveryAllocation.consumed_quantity
                        - BomComponentDirectDeliveryAllocation.reversed_quantity
                    ),
                    0,
                ),
            )
            .where(
                BomComponentDirectDeliveryAllocation.delivery_item_id.in_(
                    historical_item_ids
                ),
                BomComponentDirectDeliveryAllocation.status.in_(
                    ACTIVE_RESERVATION_STATUSES
                ),
            )
            .group_by(BomComponentDirectDeliveryAllocation.delivery_item_id)
        ).all():
            historical_by_item[int(delivery_item_id)] = int(quantity or 0)
        for delivery_item_id, quantity in db.execute(
            select(
                DeliveryInventoryAllocation.delivery_item_id,
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
                DeliveryInventoryAllocation.delivery_item_id.in_(
                    historical_item_ids
                ),
                InventoryReservation.sales_order_item_bom_component_id.is_not(None),
                DeliveryInventoryAllocation.status.in_(
                    ACTIVE_RESERVATION_STATUSES
                ),
            )
            .group_by(DeliveryInventoryAllocation.delivery_item_id)
        ).all():
            normalized_id = int(delivery_item_id)
            historical_by_item[normalized_id] = (
                historical_by_item.get(normalized_id, 0) + int(quantity or 0)
            )
        for row in historical_rows:
            delivery_id = int(row.delivery_id)
            result[delivery_id] = (
                result.get(delivery_id, 0)
                + historical_by_item.get(int(row.id), 0)
            )

    if pending_rows:
        order_item_ids = {int(row.order_item_id) for row in pending_rows}
        snapshots = db.scalars(
            select(SalesOrderItemBomComponent)
            .where(
                SalesOrderItemBomComponent.sales_order_item_id.in_(
                    order_item_ids
                )
            )
            .order_by(
                SalesOrderItemBomComponent.sales_order_item_id,
                SalesOrderItemBomComponent.display_order,
                SalesOrderItemBomComponent.id,
            )
        ).all()
        snapshot_ids = [int(snapshot.id) for snapshot in snapshots]
        snapshots_by_order_item: dict[int, list[SalesOrderItemBomComponent]] = {}
        for snapshot in snapshots:
            snapshots_by_order_item.setdefault(
                int(snapshot.sales_order_item_id), []
            ).append(snapshot)

        adjustment_totals = {
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
        } if snapshot_ids else {}
        consumed_quantities: dict[int, int] = {}
        if snapshot_ids:
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
            ).all():
                consumed_quantities[int(snapshot_id)] = int(quantity or 0)
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
                .group_by(
                    InventoryReservation.sales_order_item_bom_component_id
                )
            ).all():
                normalized_id = int(snapshot_id)
                consumed_quantities[normalized_id] = (
                    consumed_quantities.get(normalized_id, 0) + int(quantity or 0)
                )

        for row in pending_rows:
            component_quantity = 0
            delivered_after = max(int(row.order_delivered_quantity or 0), 0) + max(
                int(row.delivered_quantity or 0), 0
            )
            for snapshot in snapshots_by_order_item.get(
                int(row.order_item_id), []
            ):
                snapshot_id = int(snapshot.id)
                delta_sets, delta_pieces = adjustment_totals.get(
                    snapshot_id, (0, 0)
                )
                if int(snapshot.order_set_quantity or 0) + delta_sets < 0:
                    raise CompositeBomWorkflowError(
                        "组件调整后的有效套数不能小于0"
                    )
                target = int(snapshot.required_piece_quantity or 0) + (
                    delta_pieces
                )
                if target <= 0:
                    raise CompositeBomWorkflowError(
                        "组件调整后的需求件数必须大于0"
                    )
                target_after = min(
                    delivered_after * int(snapshot.quantity_per_set or 0),
                    target,
                )
                component_quantity += max(
                    target_after - consumed_quantities.get(snapshot_id, 0), 0
                )
            delivery_id = int(row.delivery_id)
            result[delivery_id] = result.get(delivery_id, 0) + component_quantity
    return result


def _delivery_summary_response(delivery_id: int, *, context: dict) -> dict:
    """Serialize fields needed before a delivery row is expanded."""

    delivery = context["deliveries"].get(delivery_id)
    if delivery is None:
        raise HTTPException(status_code=404, detail="送货单不存在")
    customer = context["customers"].get(delivery.customer_id)
    return_receipt = context["receipts"].get(delivery_id)
    original_delivered_quantity = context["delivered_quantities"].get(
        delivery_id, 0
    )
    has_confirmed_receipt_quantity = bool(
        return_receipt
        and return_receipt.status == "confirmed"
        and delivery_id in context["receipt_actual_quantities"]
        and context["receipt_actual_item_counts"].get(delivery_id, 0)
        == context["item_counts"].get(delivery_id, 0)
    )
    display_quantity = (
        context["receipt_actual_quantities"][delivery_id]
        if has_confirmed_receipt_quantity
        else original_delivered_quantity
    )
    return {
        "id": delivery.id,
        "delivery_number": delivery.delivery_number,
        "customer_id": delivery.customer_id,
        "customer_name": customer.name if customer else None,
        "delivery_date": delivery.delivery_date,
        "created_at": utc_naive_to_api(delivery.created_at),
        "is_historical_backfill": bool(delivery.is_historical_backfill),
        "backfilled_by": delivery.backfilled_by,
        "backfilled_at": (
            utc_naive_to_api(delivery.backfilled_at)
            if delivery.backfilled_at
            else None
        ),
        "version": int(delivery.version or 1),
        "suggested_reconciliation_month": _suggested_reconciliation_month(
            delivery.delivery_date,
            customer.statement_cycle_start_day if customer else 1,
        ),
        "vehicle_number": delivery.vehicle_number,
        "source_mode": delivery.source_mode,
        "status": delivery.status,
        "total_quantity": delivery.total_quantity,
        "total_actual_goods_quantity": context["actual_goods_quantities"].get(
            delivery_id, 0
        ),
        "original_delivered_quantity": original_delivered_quantity,
        "display_quantity": display_quantity,
        "quantity_difference": display_quantity - original_delivered_quantity,
        "quantity_source": (
            "actual_received" if has_confirmed_receipt_quantity else "delivered"
        ),
        "item_count": context["item_counts"].get(delivery_id, 0),
        "dispatched_at": (
            utc_naive_to_api(delivery.dispatched_at)
            if delivery.dispatched_at
            else None
        ),
        "ever_dispatched_at": (
            utc_naive_to_api(delivery.ever_dispatched_at)
            if delivery.ever_dispatched_at
            else None
        ),
        "voided_at": (
            utc_naive_to_api(delivery.voided_at)
            if delivery.voided_at
            else None
        ),
        "voided_by": delivery.voided_by,
        "is_printed": delivery.printed_at is not None,
        "printed_at": (
            utc_naive_to_api(delivery.printed_at) if delivery.printed_at else None
        ),
        "printed_by": delivery.printed_by,
        "return_receipt_id": return_receipt.id if return_receipt else None,
        "return_receipt_status": (
            return_receipt.status if return_receipt else None
        ),
        "pick_task": context["pick_tasks"].get(delivery_id),
        "search_matches": context.get("search_matches", {}).get(
            delivery_id, _empty_delivery_search_matches()
        ),
    }


def _unordered_finished_allocation_response(
    db: Session,
    delivery_item_id: int,
) -> list[dict]:
    rows = db.scalars(
        select(UnorderedFinishedDeliveryAllocation)
        .where(
            UnorderedFinishedDeliveryAllocation.delivery_item_id
            == delivery_item_id
        )
        .order_by(UnorderedFinishedDeliveryAllocation.id)
    ).all()
    return [
        {
            "id": row.id,
            "inventory_lot_id": row.inventory_lot_id,
            "lot_number": row.lot_number_snapshot,
            "location_id": row.warehouse_location_id_snapshot,
            "location_code": row.warehouse_location_code_snapshot,
            "pallet_code": row.pallet_code_snapshot,
            "quantity": int(row.planned_quantity or 0),
            "planned_quantity": int(row.planned_quantity or 0),
            "consumed_quantity": int(row.consumed_quantity or 0),
            "restored_quantity": int(row.restored_quantity or 0),
            "status": row.status,
        }
        for row in rows
    ]


def _delivery_response(
    db: Session,
    delivery_id: int,
    *,
    list_context: dict | None = None,
) -> dict:
    from app.models.finance import ReturnReceipt

    delivery = (
        list_context["deliveries"].get(delivery_id)
        if list_context is not None
        else _delivery_or_404(db, delivery_id)
    )
    if delivery is None:
        raise HTTPException(status_code=404, detail="送货单不存在")
    customer = (
        list_context["customers"].get(delivery.customer_id)
        if list_context is not None
        else db.get(Customer, delivery.customer_id)
    )
    return_receipt = (
        list_context["receipts"].get(delivery_id)
        if list_context is not None
        else db.scalar(select(ReturnReceipt).where(ReturnReceipt.delivery_id == delivery_id))
    )
    receipt_item_quantities_by_delivery_item = (
        list_context["receipt_item_quantities_by_delivery_item"]
        if list_context is not None
        else {
            int(row.delivery_item_id): int(row.actual_received_quantity or 0)
            for row in db.scalars(
                select(ReturnReceiptItem).where(
                    ReturnReceiptItem.return_receipt_id == return_receipt.id
                )
            ).all()
        }
        if return_receipt and return_receipt.status == "confirmed"
        else {}
    )
    items = (
        list_context["rows_by_delivery"].get(delivery_id, [])
        if list_context is not None
        else _delivery_item_rows(db, [delivery_id])
    )
    order_ids = {
        row["order_id"]
        for row in items
        if row.get("order_id") is not None
    }
    orders = (
        {order_id: list_context["orders"][order_id] for order_id in order_ids if order_id in list_context["orders"]}
        if list_context is not None
        else {
            order.id: order for order in db.scalars(select(Order).where(Order.id.in_(order_ids))).all()
        } if order_ids else {}
    )
    registry = (
        list_context["registry"]
        if list_context is not None
        else {
            int(order_id): str(order.order_number)
            for order_id, order in orders.items()
        }
    )
    pick_task = (
        list_context["pick_tasks"].get(delivery_id)
        if list_context is not None
        else _delivery_pick_task(db, delivery_id)
    )
    has_dispatch_history = delivery.status in {"dispatched", "voided"}
    pick_by_delivery_item = {
        item.delivery_item_id: _pick_item_response(db, item)
        for item in (pick_task.items if pick_task and list_context is None else [])
    }
    internal_remarks = (
        list_context["internal_remarks"]
        if list_context is not None
        else _tianhua_internal_remarks_by_delivery_item(
            db,
            [
                row["id"]
                for row in items
                if str(row["remarks"] or "").strip().startswith(
                "来源：天华预送货草稿 "
                )
            ],
        )
    )
    delivery_item_ids = [int(row["id"]) for row in items]
    has_complete_confirmed_receipt = bool(
        return_receipt
        and return_receipt.status == "confirmed"
        and all(
            delivery_item_id in receipt_item_quantities_by_delivery_item
            for delivery_item_id in delivery_item_ids
        )
    )
    inventory_backed_delivery_item_ids = (
        set(list_context["inventory_backed_delivery_item_ids"])
        if list_context is not None
        else set(
            db.scalars(
                select(DeliveryInventoryAllocation.delivery_item_id)
                .join(
                    InventoryReservation,
                    InventoryReservation.id
                    == DeliveryInventoryAllocation.reservation_id,
                )
                .where(
                    DeliveryInventoryAllocation.delivery_item_id.in_(
                        delivery_item_ids
                    ),
                    InventoryReservation.sales_order_item_bom_component_id.is_(None),
                )
                .distinct()
            ).all()
        )
        if delivery_item_ids
        else set()
    )
    response_items: list[dict] = []
    total_actual_goods_quantity = 0
    original_delivered_quantity = 0
    display_quantity = 0
    for row in items:
        mapping = dict(row)
        mapping.pop("search_product_code", None)
        mapping.pop("search_product_name", None)
        current_product_fulfillment_mode = mapping.pop(
            "current_product_fulfillment_mode",
            None,
        )
        mapping["product_code"] = (
            str(mapping.get("product_code") or "").strip()
            or "存货编码未登记"
        )
        mapping["product_name"] = (
            str(mapping.get("product_name") or "").strip()
            or "产品名称未登记"
        )
        mapping["specification"] = (
            str(mapping.get("specification") or "").strip()
        )
        is_unordered = mapping.get("source_type") == "unordered_finished"
        order_item = (
            list_context["order_items"].get(mapping["order_item_id"])
            if list_context is not None
            else (
                db.get(OrderItem, mapping["order_item_id"])
                if mapping.get("order_item_id") is not None
                else None
            )
        )
        skip_inventory_lookup = bool(
            list_context is not None
            and order_item is not None
            and int(order_item.id)
            in list_context["safe_empty_inventory_order_item_ids"]
        )
        item_context = (
            list_context["item_contexts"].get(int(mapping["id"]))
            if list_context is not None
            else None
        )
        kit_metadata = (
            item_context["kit_metadata"]
            if item_context is not None
            else (
                _empty_delivery_kit_metadata()
                if skip_inventory_lookup
                else _delivery_kit_metadata(
                    db,
                    order_item,
                    planned_delivery_quantity=mapping["delivered_quantity"],
                    delivery_item_id=mapping["id"],
                    dispatched=has_dispatch_history,
                    current_product_fulfillment_mode=(
                        current_product_fulfillment_mode
                    ),
                )
            )
        )
        actual_goods_lines = (
            [
                {
                    "line_type": "parent",
                    "order_item_id": None,
                    "component_snapshot_id": None,
                    "product_code": mapping["product_code"],
                    "product_name": mapping["product_name"],
                    "specification": mapping["specification"],
                    "unit": mapping.get("unit_snapshot") or "PCS",
                    "quantity": int(mapping["delivered_quantity"] or 0),
                    "pricing_included": True,
                    "independent_return_receipt": True,
                    "independent_statement": True,
                }
            ]
            if is_unordered
            else _delivery_document_goods_lines(
                order_item_id=mapping["order_item_id"],
                product_code=mapping["product_code"],
                product_name=mapping["product_name"],
                specification=mapping["specification"],
                parent_quantity=mapping["delivered_quantity"],
                kit_metadata=kit_metadata,
            )
        )
        actual_goods_quantity = sum(
            int(line["quantity"] or 0) for line in actual_goods_lines
        )
        total_actual_goods_quantity += actual_goods_quantity
        item_original_quantity = int(mapping["delivered_quantity"] or 0)
        item_has_confirmed_receipt_quantity = bool(
            has_complete_confirmed_receipt
            and int(mapping["id"])
            in receipt_item_quantities_by_delivery_item
        )
        item_display_quantity = (
            receipt_item_quantities_by_delivery_item[int(mapping["id"])]
            if item_has_confirmed_receipt_quantity
            else item_original_quantity
        )
        original_delivered_quantity += item_original_quantity
        display_quantity += item_display_quantity
        response_items.append(
            {
                **dict(mapping),
                "remarks": _customer_visible_delivery_remark(
                    mapping["id"],
                    mapping["remarks"],
                    internal_remarks,
                ),
                "actual_delivery_quantity": mapping["delivered_quantity"],
                "original_delivered_quantity": item_original_quantity,
                "display_quantity": item_display_quantity,
                "quantity_difference": (
                    item_display_quantity - item_original_quantity
                ),
                "quantity_source": (
                    "actual_received"
                    if item_has_confirmed_receipt_quantity
                    else "delivered"
                ),
                "order_number": (
                    "无订单库存"
                    if is_unordered
                    else display_order_number(
                        orders.get(mapping["order_id"]),
                        registry,
                    )
                ),
                "display_order_number": (
                    "无订单库存"
                    if is_unordered
                    else display_order_number(
                        orders.get(mapping["order_id"]),
                        registry,
                    )
                ),
                "customer_po": (
                    mapping.get("customer_po") if mapping.get("customer_po") is not None else ("无订单库存" if is_unordered else None)
                ),
                **kit_metadata,
                "actual_goods_lines": actual_goods_lines,
                "actual_goods_quantity": actual_goods_quantity,
                "requires_return_location": bool(
                    not is_unordered
                    and has_dispatch_history
                    and int(mapping["id"])
                    in inventory_backed_delivery_item_ids
                ),
                "allocations": (
                    item_context["unordered_allocations"]
                    if item_context is not None
                    else _unordered_finished_allocation_response(db, mapping["id"])
                    if is_unordered
                    else []
                ),
                "inventory_sources": (
                    item_context["inventory_sources"]
                    if item_context is not None
                    else _unordered_finished_allocation_response(db, mapping["id"])
                    if is_unordered
                    else []
                    if skip_inventory_lookup
                    else _inventory_sources_for_order_item(
                        db,
                        order_item=order_item,
                        planned_delivery_quantity=mapping["delivered_quantity"],
                        delivery_item_id=mapping["id"],
                        dispatched=has_dispatch_history,
                    )
                ),
                "pick_result": pick_by_delivery_item.get(mapping["id"]),
            }
        )
    return {
        "id": delivery.id,
        "delivery_number": delivery.delivery_number,
        "customer_id": delivery.customer_id,
        "customer_name": customer.name if customer else None,
        "delivery_date": delivery.delivery_date,
        "created_at": utc_naive_to_api(delivery.created_at),
        "is_historical_backfill": bool(delivery.is_historical_backfill),
        "backfilled_by": delivery.backfilled_by,
        "backfilled_at": (
            utc_naive_to_api(delivery.backfilled_at)
            if delivery.backfilled_at
            else None
        ),
        "version": int(delivery.version or 1),
        "suggested_reconciliation_month": _suggested_reconciliation_month(
            delivery.delivery_date,
            customer.statement_cycle_start_day if customer else 1,
        ),
        "vehicle_number": delivery.vehicle_number,
        "source_mode": delivery.source_mode,
        "status": delivery.status,
        "total_quantity": delivery.total_quantity,
        "total_actual_goods_quantity": total_actual_goods_quantity,
        "original_delivered_quantity": original_delivered_quantity,
        "display_quantity": display_quantity,
        "quantity_difference": display_quantity - original_delivered_quantity,
        "quantity_source": (
            "actual_received"
            if has_complete_confirmed_receipt
            else "delivered"
        ),
        "dispatched_at": (
            utc_naive_to_api(delivery.dispatched_at)
            if delivery.dispatched_at
            else None
        ),
        "ever_dispatched_at": (
            utc_naive_to_api(delivery.ever_dispatched_at)
            if delivery.ever_dispatched_at
            else None
        ),
        "voided_at": (
            utc_naive_to_api(delivery.voided_at)
            if delivery.voided_at
            else None
        ),
        "voided_by": delivery.voided_by,
        "is_printed": delivery.printed_at is not None,
        "printed_at": (
            utc_naive_to_api(delivery.printed_at) if delivery.printed_at else None
        ),
        "printed_by": delivery.printed_by,
        "return_receipt_id": return_receipt.id if return_receipt else None,
        "return_receipt_status": (
            return_receipt.status if return_receipt else None
        ),
        "pick_task": (
            _delivery_pick_task_summary(
                db,
                pick_task,
                customer_name=customer.name if customer else None,
                delivery_number=delivery.delivery_number,
                items=list_context["pick_items_by_task"].get(pick_task.id, []),
                assigned_user=list_context["assigned_users"].get(
                    pick_task.assigned_to
                ),
                assignee_preloaded=True,
            )
            if pick_task and list_context is not None
            else (_pick_task_response(db, pick_task) if pick_task else None)
        ),
        "items": response_items,
    }


def _write_audit(
    db: Session,
    *,
    user: User,
    action: str,
    resource: str,
    entity_id: int,
    details: dict,
    description: str,
) -> None:
    db.add(
        OperationLog(
            user_id=user.id,
            action=action,
            resource=resource,
            details=json.dumps(details, ensure_ascii=False, default=str),
            username=user.username,
            role=user.role,
            entity_type=resource.lower(),
            entity_id=entity_id,
            description=description,
        )
    )


def _current_delivery_lifecycle_dispatch_at(
    db: Session,
    delivery: Delivery,
) -> datetime | None:
    """Return a dispatch timestamp without confusing a reused SQLite row id."""

    create_logs = db.scalars(
        select(OperationLog)
        .where(
            OperationLog.resource == "Delivery",
            OperationLog.entity_id == delivery.id,
            OperationLog.action == "CREATE",
        )
        .order_by(OperationLog.id.desc())
    ).all()
    current_create_log: OperationLog | None = None
    for log in create_logs:
        try:
            details = json.loads(log.details or "{}")
        except (TypeError, ValueError):
            continue
        if details.get("delivery_number") == delivery.delivery_number:
            current_create_log = log
            break
    if current_create_log is None:
        # Legacy/imported rows may not have a CREATE audit.  A timestamp-bound
        # lookup is conservative: a same-second id reuse may cause a harmless
        # soft void, but it cannot cause historical facts to be hard-deleted.
        return db.scalar(
            select(func.min(OperationLog.created_at)).where(
                OperationLog.resource == "Delivery",
                OperationLog.entity_id == delivery.id,
                OperationLog.created_at >= delivery.created_at,
                OperationLog.action.in_(("DISPATCH", "CANCEL_DISPATCH")),
            )
        )
    return db.scalar(
        select(func.min(OperationLog.created_at)).where(
            OperationLog.resource == "Delivery",
            OperationLog.entity_id == delivery.id,
            OperationLog.id > current_create_log.id,
            OperationLog.action.in_(("DISPATCH", "CANCEL_DISPATCH")),
        )
    )


def _delivery_deletion_facts(
    db: Session,
    delivery: Delivery,
) -> dict:
    from app.models.finance import (
        ReturnReceipt,
        ReturnReceiptItem,
        StatementItem,
    )

    item_ids = list(
        db.scalars(
            # Deletion safety must include superseded revisions because their
            # immutable receipt/inventory facts still belong to this document.
            select(DeliveryItem.id).where(DeliveryItem.delivery_id == delivery.id)
        ).all()
    )
    inventory_allocations = (
        db.scalars(
            select(DeliveryInventoryAllocation).where(
                DeliveryInventoryAllocation.delivery_item_id.in_(item_ids)
            )
        ).all()
        if item_ids
        else []
    )
    component_allocations = (
        db.scalars(
            select(BomComponentDirectDeliveryAllocation).where(
                BomComponentDirectDeliveryAllocation.delivery_item_id.in_(
                    item_ids
                )
            )
        ).all()
        if item_ids
        else []
    )
    unordered_allocations = (
        db.scalars(
            select(UnorderedFinishedDeliveryAllocation).where(
                UnorderedFinishedDeliveryAllocation.delivery_item_id.in_(
                    item_ids
                )
            )
        ).all()
        if item_ids
        else []
    )
    receipt = db.scalar(
        select(ReturnReceipt).where(ReturnReceipt.delivery_id == delivery.id)
    )
    statement_item_id = db.scalar(
        select(StatementItem.id)
        .join(
            ReturnReceiptItem,
            ReturnReceiptItem.id == StatementItem.return_receipt_item_id,
        )
        .join(
            ReturnReceipt,
            ReturnReceipt.id == ReturnReceiptItem.return_receipt_id,
        )
        .where(ReturnReceipt.delivery_id == delivery.id)
        .limit(1)
    )
    movement_at = db.scalar(
        select(func.min(InventoryMovement.created_at)).where(
            InventoryMovement.related_delivery_id == delivery.id
        )
    )
    lifecycle_dispatch_at = _current_delivery_lifecycle_dispatch_at(
        db,
        delivery,
    )
    label_job_at = db.scalar(
        select(func.min(ProductionPackagingLabelPrintJob.created_at)).where(
            ProductionPackagingLabelPrintJob.delivery_id == delivery.id
        )
    )
    history_times = [
        delivery.ever_dispatched_at,
        movement_at,
        lifecycle_dispatch_at,
        receipt.created_at if receipt is not None else None,
        *(row.created_at for row in inventory_allocations),
        *(row.created_at for row in component_allocations),
        *(
            row.dispatched_at or row.created_at
            for row in unordered_allocations
            if int(row.consumed_quantity or 0) > 0
        ),
    ]
    history_times = [value for value in history_times if value is not None]
    return {
        "inventory_allocations": inventory_allocations,
        "component_allocations": component_allocations,
        "unordered_allocations": unordered_allocations,
        "receipt": receipt,
        "statement_item_id": statement_item_id,
        "history_at": min(history_times) if history_times else None,
        "has_history": bool(history_times) or label_job_at is not None,
        "label_job_at": label_job_at,
    }


def _build_pick_task(
    db: Session,
    *,
    delivery: Delivery,
    user: User,
) -> DeliveryPickTask:
    if delivery.status != "pending":
        raise HTTPException(status_code=409, detail="已发货送货单不能创建拿货任务")
    previous = _delivery_pick_task(db, delivery.id)
    if previous is not None:
        # The normal button is idempotent.  Editing the delivery explicitly
        # invalidates the task; a repeated click must never erase driver input.
        if previous.assigned_to is None and previous.status == "pushed":
            candidates = _eligible_delivery_pickers(
                db,
                customer_id=previous.customer_id,
            )
            if len(candidates) == 1:
                previous.assigned_to = candidates[0].id
        return previous
    lines = db.scalars(
        select(DeliveryItem)
        .where(
            DeliveryItem.delivery_id == delivery.id,
            DeliveryItem.is_current.is_(True),
        )
        .order_by(DeliveryItem.id)
    ).all()
    if not lines:
        raise HTTPException(status_code=409, detail="送货单没有可拿货明细")
    from app.services.fixed_shelf import guard_pick_task_overlap, ShelfError
    try:
        guard_pick_task_overlap(db, [line.order_item_id for line in lines if line.order_item_id], delivery.id)
        from app.services.fixed_shelf_staging import guard_unordered_pick_overlap
        guard_unordered_pick_overlap(db, delivery.id, [line.id for line in lines])
    except ShelfError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    task = DeliveryPickTask(
        delivery_id=delivery.id,
        customer_id=delivery.customer_id,
        status="pushed",
        snapshot_version=1,
        created_by=user.id,
    )
    candidates = _eligible_delivery_pickers(db, customer_id=delivery.customer_id)
    if len(candidates) == 1:
        task.assigned_to = candidates[0].id
    db.add(task)
    db.flush()
    for line in lines:
        order_item = (
            db.get(OrderItem, line.order_item_id)
            if line.order_item_id is not None
            else None
        )
        product_id = order_item.product_id if order_item else line.product_id
        product = db.get(Product, product_id) if product_id is not None else None
        db.add(
            DeliveryPickTaskItem(
                task_id=task.id,
                delivery_item_id=line.id,
                order_item_id=line.order_item_id,
                original_quantity=int(line.delivered_quantity),
                picked_quantity=0,
                status="pending",
                product_code_snapshot=(
                    product.product_code if product else line.product_code_snapshot
                ),
                product_name_snapshot=(
                    order_item.snapshot_product_name
                    if order_item
                    else line.product_name_snapshot
                ),
                specification_snapshot=(
                    resolved_product_specification(
                        order_item.snapshot_spec,
                        product,
                        customer_name_snapshots=(order_item.snapshot_product_name,),
                    )
                    if order_item
                    else resolved_product_specification(
                        line.specification_snapshot,
                        product,
                        customer_name_snapshots=(line.product_name_snapshot,),
                    )
                ),
            )
        )
    db.flush()
    _write_audit(
        db,
        user=user,
        action="CREATE_PICK_TASK",
        resource="DeliveryPickTask",
        entity_id=task.id,
        details={
            "delivery_id": delivery.id,
            "delivery_number": delivery.delivery_number,
            "snapshot_version": task.snapshot_version,
            "item_count": len(lines),
            "assigned_to": task.assigned_to,
        },
        description="创建送货拿货任务快照",
    )
    return task


def _picker_can_access_customer(
    db: Session,
    picker: User,
    customer_id: int,
) -> bool:
    return has_unrestricted_customer_access(picker, db) or customer_id in customer_scope_ids(
        picker,
        db,
    )


def _eligible_delivery_pickers(
    db: Session,
    *,
    customer_id: int | None = None,
) -> list[User]:
    candidates = db.scalars(
        select(User)
        .where(User.is_active.is_(True), User.role == "delivery_picker")
        .order_by(User.real_name, User.username)
    ).all()
    return [
        picker
        for picker in candidates
        if has_permission(picker, "deliveries.pick")
        and (
            customer_id is None
            or _picker_can_access_customer(db, picker, customer_id)
        )
    ]


def _can_view_pick_tasks(
    user: User = Depends(get_current_user),
) -> User:
    if not (
        has_permission(user, "deliveries.pick")
        or has_permission(user, "deliveries.execute")
    ):
        raise HTTPException(status_code=403, detail="权限不足")
    return user


def _pick_task_for_user(
    db: Session,
    task_id: int,
    user: User,
) -> DeliveryPickTask:
    task = db.get(DeliveryPickTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="拿货任务不存在")
    require_customer_access(task.customer_id, user, db)
    if not has_permission(user, "deliveries.execute") and task.assigned_to != user.id:
        # Do not reveal whether another employee has this task.
        raise HTTPException(status_code=404, detail="拿货任务不存在或未分配给当前账号")
    return task


def _delivery_pick_measured_map_context(
    db: Session,
    *,
    task: DeliveryPickTask,
) -> tuple[dict, list[dict], dict[int, object], dict[int, object]]:
    """Return the current pick plan plus its formal warehouse-space metadata.

    The delivery pick plan remains the authority for which locations belong to
    this task.  The map only supplies measured floor geometry for those exact
    locations; it must never expand the task into a general inventory view.
    """

    read_context = _pick_task_read_context(db, list(task.items))
    task_payload = _pick_task_response(
        db,
        task,
        read_context=read_context,
    )
    location_groups = [
        group
        for group in task_payload.get("location_groups") or []
        if group.get("location_id") is not None
        and group.get("warehouse_floor") is not None
        and str(group.get("area_code") or "").strip()
    ]
    location_ids = {
        int(group["location_id"])
        for group in location_groups
        if group.get("location_id") is not None
    }
    _locations, projection_contexts = _pick_location_projection_batch(
        db,
        location_ids,
        read_context=read_context,
    )
    floors_by_number: dict[int, object] = {}
    areas_by_key: dict[tuple[int, str], object] = {}
    for location_id in location_ids:
        context = projection_contexts.get(location_id) or {}
        floor = context.get("floor")
        area = context.get("area")
        if floor is not None:
            floors_by_number[int(floor.floor_number)] = floor
        if floor is not None and area is not None:
            areas_by_key[
                (
                    int(floor.id),
                    str(area.area_code or "").strip().upper(),
                )
            ] = area
    return task_payload, location_groups, floors_by_number, areas_by_key


def _delivery_pick_floor_code(
    floor_number: int,
    floors_by_number: dict[int, object],
) -> str:
    floor = floors_by_number.get(int(floor_number))
    return (
        str(floor.floor_code).strip().upper()
        if floor is not None and str(floor.floor_code or "").strip()
        else f"{int(floor_number)}F"
    )


def _delivery_pick_map_floor(
    db: Session,
    *,
    floor_code: str,
) -> dict | None:
    try:
        return overlay_formal_area_bindings(
            db,
            floor_code=floor_code,
            floor_layout=load_warehouse_twin_floor(floor_code),
        )
    except (WarehouseTwinLayoutNotFoundError, ValueError):
        return None


def _delivery_pick_map_features(
    map_floor: dict | None,
    *,
    area_code: str,
) -> list[dict]:
    if map_floor is None:
        return []
    normalized_area = area_code.strip().upper()
    features: list[dict] = []
    for raw in map_floor.get("features") or []:
        kind = str(raw.get("feature_kind") or "")
        bound_area = str(raw.get("erp_area_code") or "").strip().upper()
        if kind == "aisle" or (kind == "zone" and bound_area == normalized_area):
            features.append(
                {
                    "id": raw.get("id"),
                    "feature_kind": kind,
                    "name": raw.get("name"),
                    "points": raw.get("points") or [],
                }
            )
    return features


def _delivery_pick_group_updated_at(task_payload: dict, group: dict) -> str | None:
    updated_by_item = {
        int(item["id"]): item.get("updated_at")
        for item in task_payload.get("items") or []
        if item.get("id") is not None and item.get("updated_at")
    }
    timestamps = [
        updated_by_item.get(int(line["pick_item_id"]))
        for line in group.get("lines") or []
        if line.get("pick_item_id") is not None
        and updated_by_item.get(int(line["pick_item_id"]))
    ]
    return max(timestamps) if timestamps else task_payload.get("as_of")


@router.post("/{delivery_id}/pick-task", status_code=status.HTTP_201_CREATED)
def create_or_rebuild_delivery_pick_task(
    delivery_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    delivery = _delivery_for_user(db, delivery_id, user)
    try:
        task = _build_pick_task(db, delivery=delivery, user=user)
        db.commit()
        return _pick_task_response(db, task)
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise


@pick_router.get("")
def list_delivery_pick_tasks(
    status_filter: str | None = Query(default=None, alias="status"),
    response_mode: Literal["full", "summary"] = Query(default="full"),
    include_dispatched: bool = Query(default=True),
    page: int | None = Query(default=None, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(_can_view_pick_tasks),
) -> dict:
    query = select(DeliveryPickTask).order_by(DeliveryPickTask.id.desc())
    if not has_permission(user, "deliveries.execute"):
        query = query.where(DeliveryPickTask.assigned_to == user.id)
    visible = _visible_customer_ids(user, db)
    if visible is not None:
        query = query.where(DeliveryPickTask.customer_id.in_(visible))
    if status_filter:
        normalized_status = status_filter.strip().lower()
        if normalized_status not in PICK_TASK_STATUSES:
            raise HTTPException(status_code=400, detail="拿货任务状态筛选值无效")
        query = query.where(DeliveryPickTask.status == normalized_status)
    if not include_dispatched:
        query = query.where(DeliveryPickTask.status != "dispatched")
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    if page is not None:
        query = query.offset((page - 1) * page_size).limit(page_size)
    tasks = db.scalars(query).all()
    items = (
        _delivery_pick_task_list_summaries(db, tasks)
        if response_mode == "summary"
        else [
            _pick_task_response(db, task, include_location_plan=False)
            for task in tasks
        ]
    )
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size if page is not None else total,
        "total_pages": (
            (total + page_size - 1) // page_size
            if page is not None and total
            else (1 if total else 0)
        ),
        "as_of": utc_naive_to_api(_utc_now()),
    }


@pick_router.get("/assignees")
def list_delivery_pick_assignees(
    customer_id: int | None = Query(default=None, gt=0),
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    if customer_id is not None:
        require_customer_access(customer_id, user, db)
    return {
        "items": [
            {
                "id": picker.id,
                "username": picker.username,
                "name": picker.display_name or picker.real_name or picker.username,
            }
            for picker in _eligible_delivery_pickers(db, customer_id=customer_id)
        ]
    }


@pick_router.put("/{task_id}/assignment")
def assign_delivery_pick_task(
    task_id: int,
    payload: DeliveryPickAssignmentUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    task = _pick_task_for_user(db, task_id, user)
    if task.status != "pushed" or any(
        item.status != "pending" or int(item.picked_quantity or 0) != 0
        for item in task.items
    ):
        raise HTTPException(status_code=409, detail="拿货已开始，不能改派送货员")
    picker = None
    if payload.picker_user_id is not None:
        picker = db.get(User, payload.picker_user_id)
        if (
            picker is None
            or picker not in _eligible_delivery_pickers(
                db,
                customer_id=task.customer_id,
            )
        ):
            raise HTTPException(status_code=400, detail="请选择可执行本客户任务的送货拿货员")
    previous = task.assigned_to
    task.assigned_to = picker.id if picker is not None else None
    _write_audit(
        db,
        user=user,
        action="ASSIGN_PICK_TASK",
        resource="DeliveryPickTask",
        entity_id=task.id,
        details={"previous_assigned_to": previous, "assigned_to": task.assigned_to},
        description="分配送货拿货任务",
    )
    db.commit()
    return _pick_task_response(db, task)


@pick_router.get("/{task_id}")
def get_delivery_pick_task(
    task_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(_can_view_pick_tasks),
) -> dict:
    return _pick_task_response(db, _pick_task_for_user(db, task_id, user))


@pick_router.get("/{task_id}/measured-map/floors")
def get_delivery_pick_measured_map_floors(
    task_id: int,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(_can_view_pick_tasks),
) -> dict:
    response.headers["Cache-Control"] = "private, no-store"
    task = _pick_task_for_user(db, task_id, user)
    task_payload, groups, floors_by_number, areas_by_key = (
        _delivery_pick_measured_map_context(db, task=task)
    )
    grouped: dict[str, dict] = {}
    for group in groups:
        floor_number = int(group["warehouse_floor"])
        floor_code = _delivery_pick_floor_code(floor_number, floors_by_number)
        floor_row = floors_by_number.get(floor_number)
        area_code = str(group.get("area_code") or "").strip().upper()
        area_row = (
            areas_by_key.get((int(floor_row.id), area_code))
            if floor_row is not None
            else None
        )
        floor = grouped.setdefault(
            floor_code,
            {
                "floor_code": floor_code,
                "floor_name": (
                    floor_row.floor_name if floor_row is not None else f"{floor_number}楼"
                ),
                "floor_number": floor_number,
                "areas": {},
            },
        )
        projected_area_name = employee_area_name(
            area_row,
            area_code=area_code,
            floor_number=floor_number,
        )
        area = floor["areas"].setdefault(
            area_code,
            {
                "area_code": area_code,
                "area_name": projected_area_name,
                "task_location_count": 0,
                "mapped_location_count": 0,
            },
        )
        area["task_location_count"] += 1
        if group.get("map_status") == "mapped" and group.get("map_point"):
            area["mapped_location_count"] += 1

    floors: list[dict] = []
    for floor in sorted(
        grouped.values(),
        key=lambda item: (item["floor_number"], item["floor_code"]),
    ):
        map_floor = _delivery_pick_map_floor(db, floor_code=floor["floor_code"])
        areas = []
        for area in sorted(
            floor.pop("areas").values(), key=lambda item: item["area_code"]
        ):
            measured = bool(map_floor is not None and area["mapped_location_count"])
            area["map_status"] = "ready" if measured else "unmeasured"
            area["map_status_text"] = (
                "实测地图已建立" if measured else "未建立实测地图"
            )
            areas.append(area)
        floor["areas"] = areas
        floors.append(floor)
    return {
        "task_id": int(task.id),
        "snapshot_version": int(task.snapshot_version),
        "floors": floors,
        "text_only_groups": [
            {
                "key": group.get("key"),
                "label": group.get("label"),
                "map_status": group.get("map_status"),
                "reason": "未建立实测地图，只能按文字位置核对。",
            }
            for group in task_payload.get("location_groups") or []
            if group.get("map_status") != "mapped" or not group.get("map_point")
        ],
        "as_of": task_payload.get("as_of"),
        "read_only": True,
    }


@pick_router.get("/{task_id}/measured-map/floors/{floor_code}")
def get_delivery_pick_measured_map_area(
    task_id: int,
    floor_code: str,
    response: Response,
    area_code: str = Query(min_length=1, max_length=30),
    db: Session = Depends(get_db),
    user: User = Depends(_can_view_pick_tasks),
) -> dict:
    response.headers["Cache-Control"] = "private, no-store"
    task = _pick_task_for_user(db, task_id, user)
    task_payload, groups, floors_by_number, areas_by_key = (
        _delivery_pick_measured_map_context(db, task=task)
    )
    normalized_floor = floor_code.strip().upper()
    normalized_area = area_code.strip().upper()
    selected_groups = [
        group
        for group in groups
        if _delivery_pick_floor_code(
            int(group["warehouse_floor"]), floors_by_number
        )
        == normalized_floor
        and str(group.get("area_code") or "").strip().upper() == normalized_area
    ]
    if not selected_groups:
        raise HTTPException(
            status_code=404,
            detail="当前拿货任务不包含这个楼层和区域",
        )
    floor_number = int(selected_groups[0]["warehouse_floor"])
    floor_row = floors_by_number.get(floor_number)
    area_row = (
        areas_by_key.get((int(floor_row.id), normalized_area))
        if floor_row is not None
        else None
    )
    map_floor = _delivery_pick_map_floor(db, floor_code=normalized_floor)
    measured = bool(
        map_floor is not None
        and any(
            group.get("map_status") == "mapped" and group.get("map_point")
            for group in selected_groups
        )
    )
    group_payloads = []
    for group in selected_groups:
        group_payloads.append(
            {
                "key": group.get("key"),
                "recommended_sequence": group.get("recommended_sequence"),
                "label": group.get("label"),
                "source_type": group.get("source_type"),
                "location_id": group.get("location_id"),
                "location_code": group.get("location_code"),
                "location_name": group.get("location_name"),
                "pallet_code": group.get("pallet_code"),
                "total_pick_quantity": group.get("total_pick_quantity"),
                "geometry": group.get("map_point") if measured else None,
                "map_status": "ready" if measured and group.get("map_point") else "unmeasured",
                "map_status_text": (
                    "实测地图已建立"
                    if measured and group.get("map_point")
                    else "未建立实测地图"
                ),
                "updated_at": _delivery_pick_group_updated_at(task_payload, group),
                "lines": [
                    {
                        "pick_item_id": line.get("pick_item_id"),
                        "product_code": line.get("product_code"),
                        "product_name": line.get("product_name"),
                        "specification": line.get("specification"),
                        "lot_number": line.get("lot_number"),
                        "pick_quantity": line.get("pick_quantity"),
                        "unit": line.get("unit"),
                    }
                    for line in group.get("lines") or []
                ],
            }
        )
    return {
        "task_id": int(task.id),
        "snapshot_version": int(task.snapshot_version),
        "floor_code": normalized_floor,
        "floor_name": (
            floor_row.floor_name if floor_row is not None else f"{floor_number}楼"
        ),
        "area_code": normalized_area,
        "area_name": employee_area_name(
            area_row,
            area_code=normalized_area,
            floor_number=floor_number,
        ),
        "map_status": "ready" if measured else "unmeasured",
        "map_status_text": "实测地图已建立" if measured else "未建立实测地图",
        "guidance": (
            "橙色高亮为当前任务位置；到现场后核对库位、产品、批次和更新时间。"
            if measured
            else "未建立实测地图，只能按文字位置核对；系统不会生成假坐标或编号格子。"
        ),
        "bounds_mm": map_floor.get("bounds_mm") if map_floor else None,
        "features": _delivery_pick_map_features(
            map_floor,
            area_code=normalized_area,
        ),
        "groups": group_payloads,
        "as_of": task_payload.get("as_of"),
        "read_only": True,
    }


def _confirm_shelf_staging(db, task, confirmations, print_version, user):
    from app.models.fixed_shelf import ShelfProfile
    from app.services.fixed_shelf import ShelfError
    from app.services.fixed_shelf_staging import item_product, stage_pick_item, staged_lots
    required = []
    for item, quantity, target in confirmations:
        product = item_product(db, item)
        if product and db.get(ShelfProfile, product.id) and (
            target is not None or quantity < sum(
                int(lot.quantity_available or 0) + int(lot.quantity_reserved or 0) + int(lot.quantity_damaged or 0)
                for lot in staged_lots(db, item.delivery_item_id))
        ):
            required.append((item, quantity, target))
    if not required:
        return
    try:
        claimed = db.execute(update(Delivery).where(Delivery.id == task.delivery_id,
            Delivery.status == 'pending').values(version=Delivery.version))
        if claimed.rowcount != 1:
            raise ShelfError('送货单状态已变化，请刷新')
        task_id = task.id
        db.expire_all()
        if db.get(DeliveryPickTask, task_id, populate_existing=True) is None:
            raise ShelfError('拿货任务已变更，请重新打开')
        fresh = _pick_task_response(db, task)
        by_id = {row['id']: row for row in fresh['items']}
        if any(quantity != by_id[item.id].get('staged_quantity', 0) for item, quantity, _target in required) and print_version != fresh['print_version']:
            raise ShelfError('拿货位置或数量已变化，请刷新手机或A4明细后再确认集货')
        for item, quantity, target in required:
            stage_pick_item(db, item, quantity=quantity, target=target.model_dump() if target else None, operator_id=user.id)
    except (ShelfError, WarehouseInventoryError) as error:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(error)) from error


def _prepare_shelf_draft_replacement(db, delivery_id, delivery_item_id=None):
    from app.services.fixed_shelf import ShelfError
    from app.services.fixed_shelf_staging import prepare_draft_replacement
    try:
        prepare_draft_replacement(db, delivery_id, delivery_item_id)
    except ShelfError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@pick_router.post("/{task_id}/complete-planned")
def complete_delivery_pick_task_as_planned(
    task_id: int,
    payload: PickStagingBatch | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(can_pick),
) -> dict:
    """Confirm the normal path in one action without dispatching the delivery."""

    task = _pick_task_for_user(db, task_id, user)
    response = _pick_task_response(db, task)
    if task.status == "driver_confirmed" and all(
        item.status == "picked"
        and int(item.picked_quantity or 0) == int(item.original_quantity or 0)
        for item in task.items
    ) and all(not row.get('staging_required') or row.get('staged_quantity', 0) >= row['original_quantity'] for row in response['items']):
        return response
    if task.status not in {"pushed", "driver_confirmed"}:
        raise HTTPException(status_code=409, detail="当前拿货任务不能一键按计划拿齐")
    if not response["location_plan_complete"]:
        raise HTTPException(
            status_code=409,
            detail="存在未分配拿货来源，请按实际情况登记部分拿货或没货",
        )
    if not task.items:
        raise HTTPException(status_code=409, detail="拿货任务没有可确认明细")
    _confirm_shelf_staging(db, task,
        [(item, int(item.original_quantity), payload.targets.get(item.id) if payload else None) for item in task.items],
        payload.print_version if payload else None, user)
    for item in task.items:
        item.status = "picked"
        item.picked_quantity = int(item.original_quantity or 0)
    task.status = "driver_confirmed"
    task.submitted_by = user.id
    task.submitted_at = _utc_now()
    _write_audit(
        db,
        user=user,
        action="COMPLETE_PICK_TASK_PLANNED",
        resource="DeliveryPickTask",
        entity_id=task.id,
        details={
            "delivery_id": task.delivery_id,
            "item_count": len(task.items),
            "location_group_count": len(response["location_groups"]),
            "status": task.status,
        },
        description="本单全部按库位计划拿齐，进入可发货打印",
    )
    db.commit()
    return _pick_task_response(db, task)


@pick_router.put("/{task_id}/items/{item_id}")
def update_delivery_pick_task_item(
    task_id: int,
    item_id: int,
    payload: DeliveryPickItemUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(can_pick),
) -> dict:
    task = _pick_task_for_user(db, task_id, user)
    if task.status not in {"pushed", "driver_confirmed", "exception"}:
        raise HTTPException(status_code=409, detail="当前拿货任务不能再更新")
    item = db.scalar(
        select(DeliveryPickTaskItem).where(
            DeliveryPickTaskItem.id == item_id,
            DeliveryPickTaskItem.task_id == task.id,
        )
    )
    if item is None:
        raise HTTPException(status_code=404, detail="拿货任务明细不存在")
    original = int(item.original_quantity)
    requested = payload.picked_quantity
    if payload.pick_status == "picked":
        quantity = original if requested is None else int(requested)
        if quantity < original:
            raise HTTPException(status_code=400, detail="完整拿货数量不能小于原送货数量")
    elif payload.pick_status == "partial":
        if requested is None or not 0 < int(requested) < original:
            raise HTTPException(status_code=400, detail="部分拿货数量必须大于0且小于原送货数量")
        quantity = int(requested)
    else:
        if requested not in {None, 0}:
            raise HTTPException(status_code=400, detail="无货状态的拿货数量必须为0")
        quantity = 0
    _confirm_shelf_staging(db, task, [(item, quantity, payload.staging_target)], payload.print_version, user)
    item.status = payload.pick_status
    item.picked_quantity = quantity
    if task.status in {"driver_confirmed", "exception"}:
        task.status = "pushed"
        task.submitted_at = None
        task.submitted_by = None
    _write_audit(
        db,
        user=user,
        action="UPDATE_PICK_ITEM",
        resource="DeliveryPickTaskItem",
        entity_id=item.id,
        details={"task_id": task.id, "status": item.status, "picked_quantity": quantity},
        description="更新送货拿货结果",
    )
    db.commit()
    return {
        "task": _pick_task_response(db, task),
        "item": _pick_item_response(db, item),
    }


@pick_router.post("/{task_id}/submit")
def submit_delivery_pick_task(
    task_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_pick),
) -> dict:
    task = _pick_task_for_user(db, task_id, user)
    if task.status not in {"pushed", "driver_confirmed", "exception"}:
        raise HTTPException(status_code=409, detail="当前拿货任务不能提交")
    if any(item.status == "pending" for item in task.items):
        raise HTTPException(status_code=400, detail="仍有未处理的拿货明细")
    has_exception = any(
        item.status in {"partial", "no_stock"}
        or int(item.picked_quantity) > int(item.original_quantity)
        for item in task.items
    )
    task.status = "exception" if has_exception else "driver_confirmed"
    task.submitted_by = user.id
    task.submitted_at = _utc_now()
    _write_audit(
        db,
        user=user,
        action="SUBMIT_PICK_TASK",
        resource="DeliveryPickTask",
        entity_id=task.id,
        details={"delivery_id": task.delivery_id, "status": task.status},
        description="司机提交送货拿货结果",
    )
    db.commit()
    return _pick_task_response(db, task)


@pick_router.post("/{task_id}/apply")
def apply_delivery_pick_task(
    task_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    task = _pick_task_for_user(db, task_id, user)
    if task.status not in {"driver_confirmed", "exception"}:
        raise HTTPException(status_code=409, detail="请先由司机提交拿货结果")
    delivery = _delivery_for_user(db, task.delivery_id, user)
    if delivery.status != "pending":
        raise HTTPException(status_code=409, detail="已发货送货单不能应用拿货结果")
    try:
        # Acquire the same delivery-row write claim used by dispatch without
        # changing business state.  This serializes apply vs dispatch: if
        # dispatch wins first, rowcount is zero; if apply wins, dispatch waits
        # and then reads the adjusted draft quantities.
        claimed = db.execute(
            update(Delivery)
            .where(Delivery.id == delivery.id, Delivery.status == "pending")
            .values(total_quantity=Delivery.total_quantity)
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:
            raise HTTPException(status_code=409, detail="送货单状态已变化，请刷新后重试")
        applied_changes: list[dict] = []
        for item in list(task.items):
            delivery_item = db.get(DeliveryItem, item.delivery_item_id)
            if (
                delivery_item is None
                or delivery_item.delivery_id != delivery.id
                or delivery_item.order_item_id != item.order_item_id
                or int(delivery_item.delivered_quantity) != int(item.original_quantity)
            ):
                raise HTTPException(status_code=409, detail="送货草稿已变更，请重新创建拿货任务")
            applied_changes.append(
                {
                    "task_item_id": item.id,
                    "delivery_item_id": item.delivery_item_id,
                    "order_item_id": item.order_item_id,
                    "product_code": item.product_code_snapshot,
                    "product_name": item.product_name_snapshot,
                    "original_quantity": int(item.original_quantity),
                    "picked_quantity": int(item.picked_quantity),
                    "pick_status": item.status,
                }
            )
            if int(item.picked_quantity) <= 0:
                _prepare_shelf_draft_replacement(db, delivery.id, delivery_item.id)
                db.delete(delivery_item)
            else:
                if delivery_item.source_type == "unordered_finished":
                    allocations = db.scalars(
                        select(UnorderedFinishedDeliveryAllocation)
                        .where(
                            UnorderedFinishedDeliveryAllocation.delivery_item_id
                            == delivery_item.id,
                            UnorderedFinishedDeliveryAllocation.status == "planned",
                        )
                        .order_by(UnorderedFinishedDeliveryAllocation.id)
                    ).all()
                    from app.services.fixed_shelf_staging import prioritize_staged
                    allocations = prioritize_staged(db, allocations, delivery_item.id)
                    planned_total = sum(
                        int(allocation.planned_quantity or 0)
                        for allocation in allocations
                    )
                    picked_quantity = int(item.picked_quantity)
                    if picked_quantity > planned_total:
                        raise HTTPException(
                            status_code=409,
                            detail="无订单成品库存拿货数量不能超过草稿已选批次数量",
                        )
                    remaining = picked_quantity
                    for allocation in allocations:
                        planned = int(allocation.planned_quantity or 0)
                        if remaining <= 0:
                            db.delete(allocation)
                            continue
                        kept = min(planned, remaining)
                        allocation.planned_quantity = kept
                        remaining -= kept
                delivery_item.delivered_quantity = int(item.picked_quantity)
        db.flush()
        delivery.total_quantity = int(
            db.scalar(
                select(func.coalesce(func.sum(DeliveryItem.delivered_quantity), 0)).where(
                    DeliveryItem.delivery_id == delivery.id,
                    DeliveryItem.is_current.is_(True),
                )
            )
            or 0
        )
        task.status = "applied"
        task.applied_by = user.id
        task.applied_at = _utc_now()
        _write_audit(
            db,
            user=user,
            action="APPLY_PICK_TASK",
            resource="DeliveryPickTask",
            entity_id=task.id,
            details={
                "delivery_id": delivery.id,
                "total_quantity": delivery.total_quantity,
                "item_count": len(task.items),
                "items": applied_changes,
            },
            description="应用送货拿货结果到本次送货明细",
        )
        db.commit()
        return _delivery_response(db, delivery.id)
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise


def _refresh_order_status(db: Session, order_id: int) -> None:
    items = db.scalars(
        select(OrderItem).where(OrderItem.order_id == order_id)
    ).all()
    if items and all(
        item.is_force_closed or item.delivered_quantity >= item.quantity
        for item in items
    ):
        new_status = "delivered"
    elif any(item.delivered_quantity > 0 for item in items):
        new_status = "partially_delivered"
    else:
        new_status = "pending_delivery"
    db.execute(
        update(Order).where(Order.id == order_id).values(status=new_status)
    )


def _collect_delivery_lines(
    db: Session,
    *,
    customer_id: int,
    lines: list[DeliveryLineCreate],
    user: User,
) -> tuple[list[tuple[OrderItem, DeliveryLineCreate]], int, list[dict]]:
    """校验送货明细并返回 (订单明细, 行) 列表、总数量、超送警告。

    不写入任何数据，供创建与编辑共用。已送数量在此阶段不变动，
    因此剩余可送量按订单明细当前 delivered_quantity 计算。
    """
    built: list[tuple[OrderItem, DeliveryLineCreate]] = []
    seen: set[int] = set()
    total_quantity = 0
    warnings: list[dict] = []
    for index, line in enumerate(lines, start=1):
        if line.order_item_id in seen:
            raise HTTPException(
                status_code=400,
                detail=f"第{index}条订单明细重复选择",
            )
        seen.add(line.order_item_id)
        if line.delivered_quantity <= 0:
            raise HTTPException(
                status_code=400,
                detail=f"第{index}条送货数量必须大于0",
            )
        row = db.execute(
            select(OrderItem, Order)
            .join(Order, Order.id == OrderItem.order_id)
            .where(OrderItem.id == line.order_item_id)
        ).one_or_none()
        if row is None:
            raise HTTPException(
                status_code=400,
                detail=f"第{index}条订单明细不存在",
            )
        order_item, order = row
        forward_block = order_item_forward_block_reason(
            order_status=order.status,
            ordered_quantity=order_item.quantity,
            delivered_quantity=order_item.delivered_quantity,
            is_force_closed=order_item.is_force_closed,
        )
        if forward_block is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"第{index}条"
                    + order_item_forward_block_message(
                        forward_block,
                        action="发货",
                        order_status=order.status,
                    )
                ),
            )
        production_managed = _has_production_task(db, order_item.id)
        has_external_components = bool(
            db.scalar(
                select(SalesOrderItemExternalComponent.id)
                .where(
                    SalesOrderItemExternalComponent.sales_order_item_id
                    == order_item.id,
                    SalesOrderItemExternalComponent.is_required.is_(True),
                )
                .limit(1)
            )
        )
        if has_external_components and not _external_packaging_received(
            db, order_item.id
        ):
            raise HTTPException(
                status_code=409,
                detail=f"第{index}条订单明细外购包材尚未收齐，当前不可发货",
            )
        quantity_facts = _delivery_quantity_facts(db, order_item)
        remaining = quantity_facts["deliverable_quantity"]
        order_remaining = quantity_facts["order_remaining_quantity"]
        component_capacity = (
            None
            if production_managed
            else _received_telescoping_capacity(db, order_item.id)
        )
        component_remaining = (
            max(component_capacity - int(order_item.delivered_quantity or 0), 0)
            if component_capacity is not None
            else None
        )
        if order.customer_id != customer_id:
            raise HTTPException(
                status_code=400,
                detail=f"第{index}条订单明细不属于当前客户",
            )
        full_inventory_coverage = (
            False
            if production_managed
            else inventory_fully_covers_order_item(db, order_item.id)
        )
        if (
            component_remaining is not None
            and line.delivered_quantity > component_remaining
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"第{index}条天地盖盖/底成套可送数量不足，"
                    f"当前物理可送数量为 {component_remaining}"
                ),
            )
        if production_managed and (
            order_item.is_force_closed
            or remaining <= 0
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"第{index}条订单明细生产可送数量不足，"
                    f"当前可送数量为 {remaining}"
                ),
            )
        if (
            not production_managed
            and (
            (
                order_item.material_status != "received"
                and not (
                    order_item.supply_mode_snapshot == "external_purchase"
                    and _external_packaging_received(db, order_item.id)
                )
                and not full_inventory_coverage
                and _received_telescoping_capacity(db, order_item.id) is None
            )
            or order_item.is_force_closed
            )
        ):
            raise HTTPException(
                status_code=400,
                detail=f"第{index}条订单明细当前不可发货",
            )
        if line.delivered_quantity > remaining:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"第{index}条实际可送成品不足，当前最多可送 {remaining}"
                ),
            )
        over_delivery = max(line.delivered_quantity - order_remaining, 0)
        if over_delivery > 0:
            if not has_permission(user, "deliveries.over_delivery"):
                raise HTTPException(
                    status_code=403,
                    detail=f"第{index}条超订单送货需要超量送货权限",
                )
            if not line.over_delivery_confirmed:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"第{index}条本次送货超过订单剩余数量 {over_delivery}，"
                        "请确认使用真实可用成品余货后再保存。"
                    ),
                )
            confirmed_reason = (line.over_delivery_reason or "").strip()
            if not confirmed_reason:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"第{index}条超订单送货必须填写确认原因，"
                        "说明为什么要把实际余货一并送出。"
                    ),
                )
            line.over_delivery_reason = confirmed_reason
            warnings.append(
                {
                    "code": "OVER_DELIVERY",
                    "order_item_id": order_item.id,
                    "remaining_quantity": order_remaining,
                    "deliverable_quantity": remaining,
                    "delivered_quantity": line.delivered_quantity,
                    "excess_quantity": over_delivery,
                    "message": f"本次送货超过订单数量 {over_delivery}，已用黄色提醒",
                }
            )
        built.append((order_item, line))
        total_quantity += line.delivered_quantity
    return built, total_quantity, warnings


def _collect_unordered_finished_lines(
    db: Session,
    *,
    customer_id: int,
    lines: list[DeliveryLineCreate],
) -> tuple[list[dict], int]:
    built: list[dict] = []
    total_quantity = 0
    for index, line in enumerate(lines, start=1):
        product = db.get(Product, line.product_id)
        if (
            product is None
            or product.deleted_at is not None
            or not product.is_active
        ):
            raise HTTPException(
                status_code=409,
                detail=f"第 {index} 条产品不存在或已停用",
            )
        if product.customer_id != customer_id:
            raise HTTPException(
                status_code=409,
                detail=f"第 {index} 条产品不属于当前客户",
            )
        submitted_unit_price = (
            Decimal(str(line.unit_price)).quantize(Decimal("0.0001"))
            if line.unit_price is not None
            else None
        )
        if submitted_unit_price is not None and submitted_unit_price < 0:
            raise HTTPException(
                status_code=400,
                detail=f"第 {index} 条无订单库存明细单价不能小于零",
            )
        # 无订单库存可能在客户临时要货时尚未定价。显式 0 与空值都只代表
        # “待补价”，不得把 0 冻结成正式成交价或进入财务金额。
        unit_price = (
            submitted_unit_price
            if submitted_unit_price is not None and submitted_unit_price > 0
            else None
        )
        allocation_rows: list[dict] = []
        for planned in line.allocations:
            row = db.execute(
                select(
                    InventoryLot,
                    FinishedGoodsInventoryDetail,
                    WarehouseLocation,
                )
                .join(
                    FinishedGoodsInventoryDetail,
                    FinishedGoodsInventoryDetail.inventory_lot_id
                    == InventoryLot.id,
                )
                .join(
                    WarehouseLocation,
                    WarehouseLocation.id
                    == InventoryLot.warehouse_location_id,
                )
                .where(InventoryLot.id == planned.inventory_lot_id)
            ).one_or_none()
            if row is None:
                raise HTTPException(
                    status_code=409,
                    detail=f"第 {index} 条所选成品库存批次不存在",
                )
            lot, detail, location = row
            if (
                lot.inventory_type != "finished"
                or lot.status != "active"
                or int(lot.quantity_reserved or 0) != 0
                or int(lot.quantity_available or 0) < planned.quantity
                or detail.is_general
                or detail.owner_customer_id != customer_id
                or detail.product_id != product.id
            ):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"批次 {lot.lot_number} 已不可用于当前客户和产品，"
                        "请刷新库存后重试"
                    ),
                )
            pallet = lot.pallet_item.pallet if lot.pallet_item else None
            allocation_rows.append(
                {
                    "lot": lot,
                    "quantity": int(planned.quantity),
                    "location": location,
                    "pallet_code": pallet.pallet_code if pallet else None,
                }
            )
        built.append(
            {
                "product": product,
                "line": line,
                "unit_price": unit_price,
                "price_source": (
                    "pending"
                    if unit_price is None
                    else (
                        "product_default"
                        if product.sale_unit_price is not None
                        and Decimal(str(product.sale_unit_price)).quantize(
                            Decimal("0.0001")
                        )
                        == unit_price
                        else "manual"
                    )
                ),
                "allocations": allocation_rows,
            }
        )
        total_quantity += int(line.delivered_quantity)
    return built, total_quantity


def _pending_order_quantities_by_product_code(
    db: Session,
    *,
    customer_id: int,
) -> dict[str, int]:
    """Return authoritative currently deliverable order quantity by stock code."""

    rows = list(db.execute(_pending_query(customer_id=customer_id)))
    if not rows:
        return {}
    context = _PendingDeliveryReadContext(db, rows)
    totals: dict[str, int] = {}
    for row in rows:
        mapping = row._mapping
        order_item = context.order_item(mapping["order_item_id"])
        if order_item is None:
            continue
        quantity = context.remaining_quantity(db, order_item)
        code = str(mapping["product_code"] or "").strip().casefold()
        if quantity > 0 and code:
            totals[code] = totals.get(code, 0) + int(quantity)
    return totals


def _enforce_unordered_finished_order_priority(
    db: Session,
    *,
    customer_id: int,
    order_lines: list[tuple[OrderItem, DeliveryLineCreate]],
    unordered_lines: list[dict],
) -> None:
    """An unordered lot may supplement a stock code only after all order work."""

    if not unordered_lines:
        return
    product_ids = {int(item.product_id) for item, _line in order_lines}
    products = {
        int(product.id): product
        for product in db.scalars(select(Product).where(Product.id.in_(product_ids))).all()
    } if product_ids else {}
    selected_by_code: dict[str, int] = {}
    for order_item, line in order_lines:
        product = products.get(int(order_item.product_id))
        code = str(product.product_code if product is not None else "").strip().casefold()
        if code:
            selected_by_code[code] = selected_by_code.get(code, 0) + int(
                line.delivered_quantity or 0
            )
    pending_by_code = _pending_order_quantities_by_product_code(
        db,
        customer_id=customer_id,
    )
    for entry in unordered_lines:
        product: Product = entry["product"]
        code = str(product.product_code or "").strip().casefold()
        pending = int(pending_by_code.get(code, 0))
        selected = int(selected_by_code.get(code, 0))
        if pending > selected:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"存货编码 {product.product_code} 仍有订单待送 {pending}，"
                    f"本单只选择 {selected}；请先把订单待送数量全部加入，"
                    "不足部分才能使用无订单成品库存补量"
                ),
            )


def _store_unordered_finished_items(
    db: Session,
    *,
    delivery: Delivery,
    built: list[dict],
    user: User,
) -> None:
    for entry in built:
        product: Product = entry["product"]
        line: DeliveryLineCreate = entry["line"]
        delivery_item = DeliveryItem(
            delivery_id=delivery.id,
            source_type="unordered_finished",
            order_item_id=None,
            product_id=product.id,
            customer_po_snapshot=line.customer_po,
            product_code_snapshot=product.product_code,
            product_name_snapshot=product.product_name,
            specification_snapshot=_product_specification(product),
            unit_snapshot=product.unit or "只",
            unit_price_snapshot=entry["unit_price"],
            price_source=entry["price_source"],
            delivered_quantity=int(line.delivered_quantity),
            ordered_quantity_snapshot=0,
            order_remaining_snapshot=0,
            over_delivery_quantity=0,
            remarks=(line.remarks or "").strip() or None,
        )
        db.add(delivery_item)
        db.flush()
        for planned in entry["allocations"]:
            lot: InventoryLot = planned["lot"]
            location: WarehouseLocation = planned["location"]
            db.add(
                UnorderedFinishedDeliveryAllocation(
                    delivery_item_id=delivery_item.id,
                    inventory_lot_id=lot.id,
                    planned_quantity=planned["quantity"],
                    consumed_quantity=0,
                    restored_quantity=0,
                    status="planned",
                    lot_number_snapshot=lot.lot_number,
                    warehouse_location_id_snapshot=location.id,
                    warehouse_location_code_snapshot=location.location_code,
                    pallet_code_snapshot=planned["pallet_code"],
                    created_by=user.id,
                )
            )


def _product_specification(product: Product) -> str | None:
    return product_dimension_specification(product)


def _unordered_finished_customer_summaries(
    db: Session,
    *,
    user: User,
) -> list[dict]:
    """Return customers that own at least one currently selectable free lot.

    Keep this qualification aligned with ``unordered_finished_candidates`` so
    the delivery customer selector never advertises a customer whose inventory
    picker would immediately be empty.
    """

    active_reservation = exists(
        select(1).where(
            InventoryReservation.inventory_lot_id == InventoryLot.id,
            InventoryReservation.status != "cancelled",
            InventoryReservation.reserved_stock_quantity
            > (
                InventoryReservation.consumed_stock_quantity
                + InventoryReservation.released_stock_quantity
            ),
        )
    )
    query = (
        select(
            Customer.id.label("customer_id"),
            Customer.name.label("customer_name"),
            func.count(InventoryLot.id).label("lot_count"),
            func.coalesce(func.sum(InventoryLot.quantity_available), 0).label(
                "available_quantity"
            ),
        )
        .select_from(InventoryLot)
        .join(
            FinishedGoodsInventoryDetail,
            FinishedGoodsInventoryDetail.inventory_lot_id == InventoryLot.id,
        )
        .join(Product, Product.id == FinishedGoodsInventoryDetail.product_id)
        .join(
            WarehouseLocation,
            WarehouseLocation.id == InventoryLot.warehouse_location_id,
        )
        .join(Customer, Customer.id == FinishedGoodsInventoryDetail.owner_customer_id)
        .where(
            Customer.is_active.is_(True),
            InventoryLot.inventory_type == "finished",
            InventoryLot.status == "active",
            InventoryLot.quantity_available > 0,
            InventoryLot.quantity_reserved == 0,
            FinishedGoodsInventoryDetail.is_general.is_(False),
            Product.customer_id == Customer.id,
            Product.is_active.is_(True),
            Product.deleted_at.is_(None),
            ~active_reservation,
        )
        .group_by(Customer.id, Customer.name)
        .order_by(Customer.name, Customer.id)
    )
    visible_customer_ids = _visible_customer_ids(user, db)
    if visible_customer_ids is not None:
        query = query.where(Customer.id.in_(visible_customer_ids))
    return [
        {
            "customer_id": int(row.customer_id),
            "customer_name": row.customer_name,
            "lot_count": int(row.lot_count or 0),
            "available_quantity": int(row.available_quantity or 0),
        }
        for row in db.execute(query).all()
    ]


def _delivery_customer_candidates_from_pending_items(
    db: Session,
    *,
    user: User,
    pending_items: list[dict],
) -> list[dict]:
    """Merge order and free-stock sources without repeating the pending query."""

    grouped: dict[int, dict] = {}
    for item in pending_items:
        customer_id = int(item["customer_id"])
        candidate = grouped.setdefault(
            customer_id,
            {
                "customer_id": customer_id,
                "customer_name": item["customer_name"],
                "has_pending_orders": True,
                "pending_item_count": 0,
                "pending_quantity": 0,
                "has_unordered_finished": False,
                "unordered_lot_count": 0,
                "unordered_available_quantity": 0,
            },
        )
        candidate["pending_item_count"] += 1
        candidate["pending_quantity"] += int(
            item.get("deliverable_quantity")
            or item.get("remaining_quantity")
            or 0
        )

    for summary in _unordered_finished_customer_summaries(db, user=user):
        customer_id = int(summary["customer_id"])
        candidate = grouped.setdefault(
            customer_id,
            {
                "customer_id": customer_id,
                "customer_name": summary["customer_name"],
                "has_pending_orders": False,
                "pending_item_count": 0,
                "pending_quantity": 0,
                "has_unordered_finished": False,
                "unordered_lot_count": 0,
                "unordered_available_quantity": 0,
            },
        )
        candidate["has_unordered_finished"] = True
        candidate["unordered_lot_count"] = int(summary["lot_count"])
        candidate["unordered_available_quantity"] = int(
            summary["available_quantity"]
        )

    if grouped:
        active_customer_ids = set(
            db.scalars(
                select(Customer.id).where(
                    Customer.id.in_(grouped),
                    Customer.is_active.is_(True),
                )
            ).all()
        )
        grouped = {
            customer_id: candidate
            for customer_id, candidate in grouped.items()
            if customer_id in active_customer_ids
        }

    return sorted(
        grouped.values(),
        key=lambda row: (row["customer_name"], row["customer_id"]),
    )


def _delivery_customer_candidates_from_summaries(
    db: Session,
    *,
    user: User,
    pending_summaries: list[dict],
) -> list[dict]:
    """Build the delivery customer selector without expanding every order item."""

    grouped: dict[int, dict] = {}
    for summary in pending_summaries:
        customer_id = int(summary["customer_id"])
        grouped[customer_id] = {
            "customer_id": customer_id,
            "customer_name": summary["customer_name"],
            "has_pending_orders": True,
            "pending_item_count": int(summary.get("item_count") or 0),
            "pending_quantity": int(summary.get("pending_quantity") or 0),
            "has_unordered_finished": False,
            "unordered_lot_count": 0,
            "unordered_available_quantity": 0,
        }

    for summary in _unordered_finished_customer_summaries(db, user=user):
        customer_id = int(summary["customer_id"])
        candidate = grouped.setdefault(
            customer_id,
            {
                "customer_id": customer_id,
                "customer_name": summary["customer_name"],
                "has_pending_orders": False,
                "pending_item_count": 0,
                "pending_quantity": 0,
                "has_unordered_finished": False,
                "unordered_lot_count": 0,
                "unordered_available_quantity": 0,
            },
        )
        candidate["has_unordered_finished"] = True
        candidate["unordered_lot_count"] = int(summary["lot_count"])
        candidate["unordered_available_quantity"] = int(
            summary["available_quantity"]
        )

    return sorted(
        grouped.values(),
        key=lambda row: (row["customer_name"], row["customer_id"]),
    )


@router.get("/unordered-finished-candidates")
def unordered_finished_candidates(
    customer_id: int = Query(gt=0),
    q: str = Query(default="", max_length=150),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=12, ge=1, le=50),
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    require_customer_access(customer_id, user, db)
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="客户不存在")
    active_reservation = exists(
        select(1).where(
            InventoryReservation.inventory_lot_id == InventoryLot.id,
            InventoryReservation.status != "cancelled",
            InventoryReservation.reserved_stock_quantity
            > (
                InventoryReservation.consumed_stock_quantity
                + InventoryReservation.released_stock_quantity
            ),
        )
    )
    query = (
        select(
            InventoryLot,
            FinishedGoodsInventoryDetail,
            Product,
            WarehouseLocation,
        )
        .join(
            FinishedGoodsInventoryDetail,
            FinishedGoodsInventoryDetail.inventory_lot_id == InventoryLot.id,
        )
        .join(Product, Product.id == FinishedGoodsInventoryDetail.product_id)
        .join(
            WarehouseLocation,
            WarehouseLocation.id == InventoryLot.warehouse_location_id,
        )
        .where(
            InventoryLot.inventory_type == "finished",
            InventoryLot.status == "active",
            InventoryLot.quantity_available > 0,
            InventoryLot.quantity_reserved == 0,
            FinishedGoodsInventoryDetail.owner_customer_id == customer_id,
            FinishedGoodsInventoryDetail.is_general.is_(False),
            Product.customer_id == customer_id,
            Product.is_active.is_(True),
            Product.deleted_at.is_(None),
            ~active_reservation,
        )
    )
    keyword = q.strip()
    if keyword:
        fuzzy = f"%{keyword}%"
        query = query.where(
            or_(
                Product.product_code.like(fuzzy),
                Product.product_name.like(fuzzy),
                cast(Product.length_mm, String).like(fuzzy),
                cast(Product.width_mm, String).like(fuzzy),
                cast(Product.height_mm, String).like(fuzzy),
            )
        )
    query = query.order_by(Product.product_code, *inventory_fifo_order_columns())
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = db.execute(
        query.offset((page - 1) * page_size).limit(page_size)
    ).all()
    projection_contexts = load_warehouse_location_projection_contexts(
        db,
        [location for _lot, _detail, _product, location in rows],
    )
    pending_by_code = _pending_order_quantities_by_product_code(
        db,
        customer_id=customer_id,
    )
    items: list[dict] = []
    for lot, detail, product, location in rows:
        pallet = lot.pallet_item.pallet if lot.pallet_item else None
        items.append(
            {
                "inventory_lot_id": lot.id,
                "inventory_lot_number": lot.lot_number,
                "lot_number": lot.lot_number,
                "inventory_version": lot.version,
                "product_id": product.id,
                "product_code": detail.inventory_code_snapshot
                or product.product_code,
                "product_name": detail.product_name_snapshot
                or product.product_name,
                "specification": _product_specification(product),
                "unit": product.unit,
                "owner_customer_id": customer_id,
                "customer_id": customer_id,
                "is_general": False,
                "available_quantity": int(lot.quantity_available or 0),
                "unit_price": (
                    str(product.sale_unit_price)
                    if product.sale_unit_price is not None
                    and product.sale_unit_price > 0
                    else None
                ),
                "price_required": not bool(
                    product.sale_unit_price is not None
                    and product.sale_unit_price > 0
                ),
                "location_id": location.id,
                "location_code": location.location_code,
                "location_name": employee_location_name(
                    location,
                    area=projection_contexts.get(int(location.id), {}).get("area"),
                    floor=projection_contexts.get(int(location.id), {}).get("floor"),
                ),
                "pallet_code": pallet.pallet_code if pallet else None,
                "order_pending_quantity": int(
                    pending_by_code.get(str(product.product_code or "").strip().casefold(), 0)
                ),
            }
        )
    return {
        "customer_id": customer_id,
        "items": items,
        "total": int(total),
        "page": page,
        "page_size": page_size,
        "total_pages": max((int(total) + page_size - 1) // page_size, 1),
    }


@router.get("/route-suggestions")
def delivery_route_suggestions(
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    raw_rows = list(
        db.execute(
            _pending_query(customer_ids=_visible_customer_ids(user, db))
        )
    )
    customer_ids = {
        int(row._mapping["customer_id"])
        for row in raw_rows
        if row._mapping["customer_id"] is not None
    }
    customers = {
        row.id: row
        for row in db.scalars(
            select(Customer).where(Customer.id.in_(customer_ids))
        ).all()
    }
    aggregate: dict[int, dict] = {}
    for row in raw_rows:
        mapping = row._mapping
        order_item = db.get(OrderItem, mapping["order_item_id"])
        remaining = _delivery_remaining_quantity(db, order_item) if order_item else 0
        if remaining <= 0:
            continue
        customer_id = int(mapping["customer_id"])
        customer = customers.get(customer_id)
        if customer is None:
            continue
        entry = aggregate.setdefault(
            customer_id,
            {
                "customer_id": customer_id,
                "customer_name": customer.name,
                "address": (customer.address or "").strip() or None,
                "contact_person": customer.contact_person,
                "phone": customer.phone,
                "pending_item_count": 0,
                "pending_quantity": 0,
                "earliest_delivery_date": None,
                "product_codes": set(),
            },
        )
        entry["pending_item_count"] += 1
        entry["pending_quantity"] += int(remaining)
        if mapping["product_code"]:
            entry["product_codes"].add(str(mapping["product_code"]))
        delivery_date = mapping["delivery_date"]
        if delivery_date and (
            entry["earliest_delivery_date"] is None
            or delivery_date < entry["earliest_delivery_date"]
        ):
            entry["earliest_delivery_date"] = delivery_date

    company = db.scalar(select(CompanyConfig).where(CompanyConfig.id == 1))
    origin_address = (company.address or "").strip() if company else ""
    origin_area, _origin_subarea = _delivery_route_area(origin_address)
    grouped: dict[str, list[dict]] = {}
    for entry in aggregate.values():
        area, subarea = _delivery_route_area(entry["address"])
        entry["area"] = area
        entry["subarea"] = subarea
        entry["product_codes"] = sorted(entry["product_codes"])
        grouped.setdefault(area, []).append(entry)

    groups = []
    for area, entries in grouped.items():
        entries.sort(
            key=lambda row: (
                row["subarea"] or "",
                row["earliest_delivery_date"] or date.max,
                row["customer_name"],
            )
        )
        previous_address = origin_address or None
        for sequence, entry in enumerate(entries, start=1):
            entry["sequence"] = sequence
            entry["navigation_from"] = previous_address
            is_same_address = bool(
                _normalized_delivery_address(previous_address)
                and _normalized_delivery_address(previous_address)
                == _normalized_delivery_address(entry["address"])
            )
            entry["same_as_previous_address"] = is_same_address
            if is_same_address:
                entry["navigation_url"] = None
                entry["navigation_note"] = "与上一站同地址，可同站处理"
            else:
                entry["navigation_url"] = _baidu_delivery_direction_url(
                    previous_address, entry["address"]
                )
                entry["navigation_note"] = (
                    None if entry["navigation_url"] else "缺少起点或地址"
                )
            if entry["address"]:
                previous_address = entry["address"]
        groups.append(
            {
                "area": area,
                "same_as_origin_area": bool(origin_address and area == origin_area),
                "customer_count": len(entries),
                "pending_item_count": sum(
                    row["pending_item_count"] for row in entries
                ),
                "pending_quantity": sum(row["pending_quantity"] for row in entries),
                "customers": entries,
            }
        )
    groups.sort(
        key=lambda group: (
            not group["same_as_origin_area"],
            min(
                (
                    row["earliest_delivery_date"] or date.max
                    for row in group["customers"]
                ),
                default=date.max,
            ),
            group["area"],
        )
    )
    return {
        "origin_address": origin_address or None,
        "origin_area": origin_area if origin_address else None,
        "customer_count": len(aggregate),
        "groups": groups,
        "planning_mode": "regional_grouping_with_segment_navigation",
        "disclaimer": (
            "这是按客户地址区域和交期生成的同车辅助建议，不是实时路况最优解；"
            "请在百度地图打开每一段后由司机确认最终顺序。"
        ),
    }


class _PendingDeliveryReadContext:
    """Request-scoped lookup for uncomplicated pending rows.

    Complex production, inventory and BOM rows continue through the existing
    workflow helpers.  A received-material row with none of those related
    facts has an exact, stable delivery formula: ordered minus delivered.
    Prefetching only the negative facts prevents a query-per-row cascade
    without using a stale process-wide cache for inventory availability.
    """

    def __init__(
        self,
        db: Session,
        rows: list[object],
        *,
        include_inventory_sources: bool = True,
    ):
        item_ids = {
            int(row._mapping["order_item_id"])
            for row in rows
            if row._mapping["order_item_id"] is not None
        }
        order_ids = {
            int(row._mapping["order_id"])
            for row in rows
            if row._mapping["order_id"] is not None
        }
        self.order_items = {
            item.id: item
            for item in db.scalars(
                select(OrderItem).where(OrderItem.id.in_(item_ids))
            ).all()
        } if item_ids else {}
        self.orders = {
            order.id: order
            for order in db.scalars(select(Order).where(Order.id.in_(order_ids))).all()
        } if order_ids else {}
        reservation_lot_ids = set(
            db.scalars(
                select(InventoryReservation.inventory_lot_id)
                .where(
                    InventoryReservation.order_item_id.in_(item_ids),
                    InventoryReservation.status != "cancelled",
                )
                .distinct()
            ).all()
        ) if include_inventory_sources and item_ids else set()
        lot_ids = {
            int(lot_id) for lot_id in reservation_lot_ids if lot_id is not None
        }
        lots = {
            int(lot.id): lot
            for lot in db.scalars(
                select(InventoryLot)
                .options(
                    selectinload(InventoryLot.pallet_item).selectinload(
                        InventoryPalletItem.pallet
                    )
                )
                .where(InventoryLot.id.in_(lot_ids))
            ).all()
        } if lot_ids else {}
        location_ids = {
            int(lot.warehouse_location_id)
            for lot in lots.values()
            if lot.warehouse_location_id is not None
        }
        locations = {
            int(location.id): location
            for location in db.scalars(
                select(WarehouseLocation).where(
                    WarehouseLocation.id.in_(location_ids)
                )
            ).all()
        } if location_ids else {}
        self.inventory_read_context = {
            "lots": lots,
            "locations": locations,
            "location_projection_contexts": (
                load_warehouse_location_projection_contexts(db, locations.values())
            ),
            "space_ledger_enabled": (
                has_space_ledger(db) if include_inventory_sources else False
            ),
        }
        if not item_ids:
            self.fast_item_ids: set[int] = set()
            self.receipt_auto_item_ids: set[int] = set()
            self.graph_item_ids: set[int] = set()
            return

        composite_rows = db.execute(
                select(SalesOrderItemBomComponent.sales_order_item_id,
                       SalesOrderItemBomComponent.snapshot_schema_version)
                .where(SalesOrderItemBomComponent.sales_order_item_id.in_(item_ids))
                .distinct()
            ).all()
        self.composite_ids = {int(row[0]) for row in composite_rows}
        self.graph_item_ids = {int(row[0]) for row in composite_rows if row[1] == 5}
        self.composite_available_sets = kit_available_sets_by_order_item_ids(
            db,
            self.composite_ids,
        )
        tasks = list(
            db.scalars(
                select(ProductionTask).where(ProductionTask.order_item_id.in_(item_ids))
            ).all()
        )
        task_item_ids = {int(task.order_item_id) for task in tasks}
        regular_tasks = {
            int(task.order_item_id): task
            for task in tasks
            if task.sales_order_item_bom_component_id is None
        }
        reservation_item_ids = set(
            db.scalars(
                select(InventoryReservation.order_item_id)
                .where(InventoryReservation.order_item_id.in_(item_ids))
                .distinct()
            ).all()
        )
        semi_requirements = list(
            db.scalars(
                select(OrderItemSemiRequirement).where(
                    OrderItemSemiRequirement.order_item_id.in_(item_ids)
                )
            ).all()
        )
        semi_requirement_item_ids = {
            int(requirement.order_item_id) for requirement in semi_requirements
        }
        telescoping_rows = db.execute(
            select(
                RequisitionItem.order_item_id,
                RequisitionItem.product_name_snapshot,
                RequisitionItem.requisition_qty,
                RequisitionItem.status,
            ).where(RequisitionItem.order_item_id.in_(item_ids))
        ).all()
        telescoping: dict[int, dict[str, int | bool]] = {}
        for item_id, product_name, quantity, item_status in telescoping_rows:
            component = _component_kind(product_name)
            if not component:
                continue
            state = telescoping.setdefault(
                int(item_id), {"has": True, "base": 0, "cover": 0}
            )
            if item_status == "已入库":
                state[component] = int(state[component]) + int(quantity or 0)
        telescoping_item_ids = set(telescoping)
        excluded_ids = (
            self.composite_ids
            | task_item_ids
            | reservation_item_ids
            | semi_requirement_item_ids
            | telescoping_item_ids
        )
        required_external_item_ids = {
            int(value)
            for value in db.scalars(
                select(SalesOrderItemExternalComponent.sales_order_item_id)
                .where(
                    SalesOrderItemExternalComponent.sales_order_item_id.in_(
                        item_ids
                    ),
                    SalesOrderItemExternalComponent.is_required.is_(True),
                )
                .distinct()
            ).all()
        }
        external_received_ids = {
            int(item_id)
            for item_id, item in self.order_items.items()
            if item_id in required_external_item_ids
            and _external_packaging_received(db, int(item_id))
        }
        self.fast_item_ids = {
            item_id
            for item_id, item in self.order_items.items()
            if (
                item.material_status == "received"
                or item_id in external_received_ids
            )
            and item_id not in excluded_ids
        }
        completions_by_item: dict[int, list[ProductionCompletion]] = {}
        self.receipt_auto_item_ids: set[int] = set()
        for completion in db.scalars(
            select(ProductionCompletion).where(
                ProductionCompletion.order_item_id.in_(item_ids),
                ProductionCompletion.status == "posted",
            )
        ).all():
            completions_by_item.setdefault(
                int(completion.order_item_id), []
            ).append(completion)
            if completion.origin == "receipt_auto":
                self.receipt_auto_item_ids.add(int(completion.order_item_id))
        remaining_finished_reserved_by_item = (
            remaining_finished_order_credit_by_item_ids(db, sorted(item_ids))
        )
        customer_product_pairs = {
            (int(self.orders[item.order_id].customer_id), int(item.product_id))
            for item in self.order_items.values()
            if item.order_id in self.orders
        }
        available_surplus_by_customer_product: dict[tuple[int, int], int] = {}
        if customer_product_pairs:
            customer_ids = {pair[0] for pair in customer_product_pairs}
            product_ids = {pair[1] for pair in customer_product_pairs}
            for owner_customer_id, product_id, quantity in db.execute(
                select(
                    FinishedGoodsInventoryDetail.owner_customer_id,
                    FinishedGoodsInventoryDetail.product_id,
                    func.coalesce(func.sum(InventoryLot.quantity_available), 0),
                )
                .join(
                    FinishedGoodsInventoryDetail,
                    FinishedGoodsInventoryDetail.inventory_lot_id
                    == InventoryLot.id,
                )
                .where(
                    InventoryLot.inventory_type == "finished",
                    InventoryLot.status == "active",
                    InventoryLot.quantity_available > 0,
                    InventoryLot.source_type.in_(
                        ("production_surplus", "production_completion", "transfer")
                    ),
                    FinishedGoodsInventoryDetail.is_general.is_(False),
                    FinishedGoodsInventoryDetail.owner_customer_id.in_(customer_ids),
                    FinishedGoodsInventoryDetail.product_id.in_(product_ids),
                )
                .group_by(
                    FinishedGoodsInventoryDetail.owner_customer_id,
                    FinishedGoodsInventoryDetail.product_id,
                )
            ).all():
                pair = (int(owner_customer_id), int(product_id))
                if pair in customer_product_pairs:
                    available_surplus_by_customer_product[pair] = int(quantity or 0)
        reserved_by_item = {
            item_id: max(int(item.delivered_quantity or 0), 0)
            + int(remaining_finished_reserved_by_item.get(item_id, 0))
            for item_id, item in self.order_items.items()
        }
        semi_credited_by_requirement: dict[int, int] = {}
        semi_requirement_ids = [requirement.id for requirement in semi_requirements]
        if semi_requirement_ids:
            for reservation in db.scalars(
                select(InventoryReservation).where(
                    InventoryReservation.semi_requirement_id.in_(
                        semi_requirement_ids
                    ),
                    InventoryReservation.reservation_type == "semi_order",
                    InventoryReservation.status != "cancelled",
                )
            ).all():
                requirement_id = int(reservation.semi_requirement_id)
                credited = max(
                    int(reservation.credited_requirement_quantity or 0)
                    - int(reservation.released_requirement_quantity or 0),
                    0,
                )
                semi_credited_by_requirement[requirement_id] = (
                    semi_credited_by_requirement.get(requirement_id, 0)
                    + credited
                )
        product_styles = {
            int(product_id): str(box_style or "")
            for product_id, box_style in db.execute(
                select(Product.id, Product.box_style).where(
                    Product.id.in_(
                        {
                            int(item.product_id)
                            for item in self.order_items.values()
                        }
                    )
                )
            ).all()
        }
        requirements_by_item: dict[int, dict[str, OrderItemSemiRequirement]] = {}
        for requirement in semi_requirements:
            requirements_by_item.setdefault(
                int(requirement.order_item_id), {}
            )[requirement.component_type] = requirement
        semi_fully_covered_ids: set[int] = set()
        for item_id, requirements in requirements_by_item.items():
            item = self.order_items.get(item_id)
            if item is None:
                continue
            box_style = product_styles.get(int(item.product_id), "")
            expected_components = (
                {"cover", "base"}
                if "天地盖" in box_style or "A3" in box_style.upper()
                else {"whole"}
            )
            if not expected_components.issubset(requirements):
                continue
            production_boxes = max(
                int(item.quantity or 0)
                - int(reserved_by_item.get(item_id, 0)),
                0,
            )
            if all(
                semi_credited_by_requirement.get(requirements[component].id, 0)
                >= production_boxes
                * max(int(requirements[component].pieces_per_box or 1), 1)
                for component in expected_components
            ):
                semi_fully_covered_ids.add(item_id)
        self.remaining_by_item: dict[int, int] = {}
        for item_id, item in self.order_items.items():
            delivered = max(int(item.delivered_quantity or 0), 0)
            if item_id in self.receipt_auto_item_ids:
                order = self.orders.get(int(item.order_id))
                available_surplus = (
                    available_surplus_by_customer_product.get(
                        (int(order.customer_id), int(item.product_id)),
                        0,
                    )
                    if order is not None
                    else 0
                )
                ready_quantity = _receipt_auto_ready_quantity_from_facts(
                    remaining_finished_reserved=int(
                        remaining_finished_reserved_by_item.get(item_id, 0)
                    ),
                ) + max(int(available_surplus), 0)
                self.remaining_by_item[item_id] = max(ready_quantity, 0)
                continue
            if item_id in self.composite_ids:
                continue
            task = regular_tasks.get(item_id)
            if task is not None:
                if (
                    item_id in required_external_item_ids
                    and item_id not in external_received_ids
                ):
                    self.remaining_by_item[item_id] = 0
                    continue
                if task.status not in {"completed", "not_required"}:
                    self.remaining_by_item[item_id] = 0
                    continue
                completions = completions_by_item.get(item_id, [])
                if completions:
                    ready_quantity = max(
                        int(task.finished_coverage_snapshot or 0)
                        + sum(
                            normalized_completion_output(item, completion)
                            for completion in completions
                        ),
                        0,
                    )
                else:
                    ready_quantity = max(int(reserved_by_item.get(item_id, 0)), 0)
                self.remaining_by_item[item_id] = max(
                    ready_quantity - delivered, 0
                )
                continue

            max_deliverable = max(int(item.quantity or 0), 0)
            if item.supply_mode_snapshot == "external_purchase":
                self.remaining_by_item[item_id] = (
                    max(max_deliverable - delivered, 0)
                    if item_id in external_received_ids
                    else 0
                )
                continue
            component_state = telescoping.get(item_id)
            if component_state is not None:
                max_deliverable = min(
                    max_deliverable,
                    int(component_state["base"]),
                    int(component_state["cover"]),
                )
            elif (
                item.material_status != "received"
                and int(reserved_by_item.get(item_id, 0)) < max_deliverable
                and item_id not in semi_fully_covered_ids
            ):
                self.remaining_by_item[item_id] = 0
                continue
            self.remaining_by_item[item_id] = max(
                max_deliverable - delivered, 0
            )

    def order(self, order_id: int) -> Order | None:
        return self.orders.get(int(order_id))

    def order_item(self, order_item_id: int) -> OrderItem | None:
        return self.order_items.get(int(order_item_id))

    def is_fast(self, order_item: OrderItem | None) -> bool:
        return bool(order_item and order_item.id in self.fast_item_ids)

    def remaining_quantity(self, db: Session, order_item: OrderItem) -> int:
        if order_item.id in self.graph_item_ids:
            return max(int(self.composite_available_sets.get(order_item.id, 0)), 0)
        if order_item.id in self.receipt_auto_item_ids:
            from app.services.bom_subkit_delivery import limit_by_subkit_stock
            return limit_by_subkit_stock(db, {order_item.id: max(int(self.remaining_by_item.get(order_item.id, 0)), 0)})[order_item.id]
        if order_item.id in self.composite_ids:
            return max(
                int(self.composite_available_sets.get(order_item.id, 0)),
                0,
            )
        return max(int(self.remaining_by_item.get(order_item.id, 0)), 0)


def _pending_delivery_item_payload(
    db: Session,
    *,
    row,
    registry,
    context: _PendingDeliveryReadContext,
    include_material_display: bool = False,
) -> dict | None:
    """Build the legacy response, avoiding duplicate SQL for the safe path."""

    mapping = row._mapping
    order = context.order(mapping["order_id"])
    order_item = context.order_item(mapping["order_item_id"])
    if order_item is None:
        return None
    display = display_order_number(order, registry) if order is not None else mapping["order_number"]
    if context.is_fast(order_item):
        ordered = max(int(order_item.quantity or 0), 0)
        delivered = max(int(order_item.delivered_quantity or 0), 0)
        remaining_quantity = max(ordered - delivered, 0)
        if remaining_quantity <= 0:
            return None
        quantity_facts = {
            "ordered_quantity": ordered,
            "delivered_quantity": delivered,
            "order_remaining_quantity": remaining_quantity,
            "deliverable_quantity": remaining_quantity,
            "over_delivery_quantity": 0,
            "surplus_finished_quantity": 0,
        }
        kit_metadata = {
            "is_composite_bom": False,
            "kit_availability": None,
            "available_sets": None,
            "missing_components": [],
            "component_lines": [],
        }
        inventory_sources: list[dict] = []
    else:
        remaining_quantity = context.remaining_quantity(db, order_item)
        if remaining_quantity <= 0:
            return None
        ordered = max(int(order_item.quantity or 0), 0)
        delivered = max(int(order_item.delivered_quantity or 0), 0)
        order_remaining = max(ordered - delivered, 0)
        quantity_facts = {
            "ordered_quantity": ordered,
            "delivered_quantity": delivered,
            "order_remaining_quantity": order_remaining,
            "deliverable_quantity": remaining_quantity,
            "over_delivery_quantity": max(
                remaining_quantity - order_remaining, 0
            ),
            "surplus_finished_quantity": max(
                remaining_quantity - order_remaining, 0
            ),
        }
        kit_metadata = (
            _delivery_kit_metadata(db, order_item)
            if order_item.id in context.composite_ids
            else {
                "is_composite_bom": False,
                "kit_availability": None,
                "available_sets": None,
                "missing_components": [],
                "component_lines": [],
            }
        )
        inventory_sources = _inventory_sources_for_order_item(
            db,
            order_item=order_item,
            planned_delivery_quantity=remaining_quantity,
            read_context=context.inventory_read_context,
        )

    base_payload = dict(mapping)
    base_payload["specification"] = resolved_product_specification(
        base_payload.get("specification"),
        length_mm=base_payload.pop("product_length_mm", None),
        customer_name_snapshots=(base_payload.get("product_name"),),
        width_mm=base_payload.pop("product_width_mm", None),
        height_mm=base_payload.pop("product_height_mm", None),
    )
    payload = {
        **base_payload,
        "remaining_quantity": remaining_quantity,
        **quantity_facts,
        "order_number": display,
        "display_order_number": display,
        **kit_metadata,
        "inventory_sources": inventory_sources,
    }
    if include_material_display:
        material = (mapping["material"] or "").strip()
        flute_type = (mapping["flute_type"] or "").strip()
        payload["material_display"] = (
            f"{material} / {flute_type}" if material and flute_type else material
        )
    return payload


@router.get("/pending_items")
def pending_delivery_items(
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    rows = list(
        db.execute(_pending_query(customer_ids=_visible_customer_ids(user, db)))
    )
    context = _PendingDeliveryReadContext(db, rows)
    registry = {
        int(order_id): str(order.order_number)
        for order_id, order in context.orders.items()
    }
    items = []
    for row in rows:
        payload = _pending_delivery_item_payload(
            db, row=row, registry=registry, context=context
        )
        if payload is not None:
            items.append(payload)
    return {
        "items": items,
        "customer_candidates": _delivery_customer_candidates_from_pending_items(
            db,
            user=user,
            pending_items=items,
        ),
    }


def pending_delivery_customer_summaries(
    db: Session,
    *,
    user: User,
) -> list[dict]:
    """Return the exact pending-delivery customer set without item payload N+1.

    Qualification reuses ``_pending_query`` and the same request-scoped
    remaining-quantity context as ``pending_delivery_items``.  The dashboard
    needs customer identities and counts only, so inventory source and BOM
    display payloads are deliberately not expanded here.
    """

    rows = list(db.execute(_pending_query(customer_ids=_visible_customer_ids(user, db))))
    context = _PendingDeliveryReadContext(
        db,
        rows,
        include_inventory_sources=False,
    )
    grouped: dict[int, dict] = {}
    for row in rows:
        mapping = row._mapping
        order_item = context.order_item(mapping["order_item_id"])
        if order_item is None:
            continue
        remaining_quantity = context.remaining_quantity(db, order_item)
        if remaining_quantity <= 0:
            continue
        customer_id = int(mapping["customer_id"])
        group = grouped.setdefault(
            customer_id,
            {
                "customer_id": customer_id,
                "customer_name": mapping["customer_name"],
                "item_count": 0,
                "pending_quantity": 0,
                "order_item_ids": [],
                "delivery_date": mapping["delivery_date"],
            },
        )
        group["item_count"] += 1
        group["pending_quantity"] += int(remaining_quantity)
        group["order_item_ids"].append(int(mapping["order_item_id"]))
        candidate_date = mapping["delivery_date"]
        if candidate_date is not None and (
            group["delivery_date"] is None
            or candidate_date < group["delivery_date"]
        ):
            group["delivery_date"] = candidate_date
    return sorted(
        grouped.values(),
        key=lambda row: (
            row["delivery_date"] is None,
            row["delivery_date"] or date.max,
            row["customer_name"],
            row["customer_id"],
        ),
    )


@router.get("/pending-customer-options")
def pending_delivery_customer_options(
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    """Return only customers that currently have a real selectable source."""

    summaries = pending_delivery_customer_summaries(db, user=user)
    return {
        "items": _delivery_customer_candidates_from_summaries(
            db,
            user=user,
            pending_summaries=summaries,
        )
    }


@router.get("/fulfillment-reminders")
def get_delivery_fulfillment_reminders(
    customer_id: int = Query(gt=0),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    """Return the current customer's internal delivery reminders in one page."""

    require_customer_access(customer_id, user, db)
    return list_delivery_reminders(
        db,
        customer_id=customer_id,
        page=page,
        page_size=page_size,
    )


@router.get("/pending-items/search")
def search_pending_delivery_items(
    customer_id: int = Query(gt=0),
    order_item_id: int | None = Query(default=None, gt=0),
    inventory_code: str = Query(default="", max_length=150),
    q: str = Query(default="", max_length=150),
    customer_po: str = Query(default="", max_length=150),
    product_name: str = Query(default="", max_length=150),
    search_type: str = Query(default="", max_length=50),
    list_all: bool = False,
    limit: int | None = Query(default=None, ge=1, le=200),
    page: int = Query(default=1, ge=1),
    page_size: int | None = Query(default=None, ge=1, le=50),
    db: Session = Depends(get_db),
    _user: User = Depends(can_operate),
) -> dict:
    require_customer_access(customer_id, _user, db)
    inventory_keyword = inventory_code.strip()
    general_keyword = q.strip()
    customer_po_keyword = customer_po.strip()
    product_name_keyword = product_name.strip()
    search_type = search_type.strip().lower()

    if search_type:
        allowed_search_types = {
            "inventory_code",
            "customer_po",
            "product_name",
        }
        if search_type not in allowed_search_types:
            raise HTTPException(status_code=400, detail="search_type 参数无效")
        if general_keyword:
            if search_type == "inventory_code":
                inventory_keyword = inventory_keyword or general_keyword
            elif search_type == "customer_po":
                customer_po_keyword = customer_po_keyword or general_keyword
            else:
                product_name_keyword = product_name_keyword or general_keyword
            general_keyword = ""

    if not list_all and order_item_id is None and not any(
        [
            inventory_keyword,
            general_keyword,
            customer_po_keyword,
            product_name_keyword,
        ]
    ):
        return {"items": [], "total": 0, "page": page, "page_size": page_size or 20, "total_pages": 1}
    if db.get(Customer, customer_id) is None:
        raise HTTPException(status_code=400, detail="客户不存在")
    load_all = list_all and page_size is None and limit is None
    effective_limit = page_size or limit or (100 if list_all else 20)
    base_query = _pending_query(
        order_item_id=order_item_id,
        customer_id=customer_id,
        inventory_keyword=inventory_keyword,
        customer_po_keyword=customer_po_keyword,
        product_name_keyword=product_name_keyword,
        general_keyword=general_keyword,
    )
    total = db.scalar(select(func.count()).select_from(base_query.subquery())) or 0
    if load_all:
        rows = list(db.execute(base_query))
        effective_limit = max(int(total), 1)
        page = 1
    else:
        query_limit = min(effective_limit * 3, 200)
        offset = 0 if list_all and page_size is None else (page - 1) * effective_limit
        rows = list(db.execute(base_query.offset(offset).limit(query_limit)))
    context = _PendingDeliveryReadContext(db, rows)
    registry = {
        int(order_id): str(order.order_number)
        for order_id, order in context.orders.items()
    }
    items = []
    for row in rows:
        payload = _pending_delivery_item_payload(
            db,
            row=row,
            registry=registry,
            context=context,
            include_material_display=True,
        )
        if payload is not None:
            items.append(payload)
        if len(items) >= effective_limit:
            break
    return {
        "items": items,
        "total": int(total),
        "page": page,
        "page_size": effective_limit,
        "total_pages": (
            1
            if load_all
            else max((int(total) + effective_limit - 1) // effective_limit, 1)
        ),
    }


@router.get("")
def list_deliveries(
    customer_id: int | None = None,
    customer_ids: list[int] | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    statuses: list[str] | None = Query(default=None),
    keyword: str | None = Query(default=None, max_length=200),
    delivery_no: str | None = None,
    order_no: str | None = None,
    customer_po: str | None = Query(default=None, max_length=200),
    product_code: str | None = Query(default=None, max_length=200),
    product_name: str | None = Query(default=None, max_length=200),
    spec: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    return_status: list[str] | None = Query(default=None),
    view: Literal["full", "summary"] = Query(default="full"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    # New delivery drafts must stay at the top. Printing or dispatching an old
    # delivery must not move it ahead of a delivery that was just created.
    query = select(Delivery.id)

    # Apply the customer's visibility boundary before any document or product
    # filter.  A direct filter request for an out-of-scope customer keeps the
    # existing endpoint's explicit 403 behaviour instead of silently revealing
    # whether that customer has deliveries.
    requested_customer_ids = {
        int(value) for value in (customer_ids or [])
    }
    if customer_id is not None:
        requested_customer_ids.add(customer_id)
    if requested_customer_ids:
        for requested_customer_id in requested_customer_ids:
            require_customer_access(requested_customer_id, user, db)
        query = query.where(Delivery.customer_id.in_(requested_customer_ids))
    else:
        visible_customer_ids = _visible_customer_ids(user, db)
        if visible_customer_ids is not None:
            query = query.where(Delivery.customer_id.in_(visible_customer_ids))

    normalized_statuses = [
        value.strip() for value in (statuses or []) if value and value.strip()
    ]
    if normalized_statuses:
        query = query.where(Delivery.status.in_(normalized_statuses))
    elif status_filter:
        query = query.where(Delivery.status == status_filter)
    else:
        query = query.where(Delivery.status != "voided")

    def _contains(column, value: str | None):
        normalized = (value or "").strip().lower()
        return func.lower(column).like(f"%{normalized}%") if normalized else None

    delivery_number_filter = _contains(Delivery.delivery_number, delivery_no)
    if delivery_number_filter is not None:
        query = query.where(delivery_number_filter)
    normalized_keyword = (keyword or "").strip().lower()
    if normalized_keyword:
        pattern = f"%{normalized_keyword}%"
        customer_keyword_match = exists(
            select(1).where(
                Customer.id == Delivery.customer_id,
                or_(
                    func.lower(Customer.name).like(pattern),
                    func.lower(Customer.customer_code).like(pattern),
                ),
            )
        )
        item_keyword_match = exists(
            select(1)
            .select_from(DeliveryItem)
            .outerjoin(OrderItem, OrderItem.id == DeliveryItem.order_item_id)
            .outerjoin(Order, Order.id == OrderItem.order_id)
            .outerjoin(
                Product,
                Product.id
                == func.coalesce(DeliveryItem.product_id, OrderItem.product_id),
            )
            .where(
                DeliveryItem.delivery_id == Delivery.id,
                DeliveryItem.is_current.is_(True),
                or_(
                    func.lower(Order.order_number).like(pattern),
                    func.lower(func.coalesce(DeliveryItem.customer_po_snapshot, Order.customer_po)).like(pattern),
                    func.lower(
                        func.coalesce(
                            func.nullif(DeliveryItem.product_code_snapshot, ""),
                            func.nullif(OrderItem.snapshot_product_code, ""),
                            Product.product_code,
                        )
                    ).like(pattern),
                    func.lower(
                        func.coalesce(
                            func.nullif(DeliveryItem.product_name_snapshot, ""),
                            func.nullif(OrderItem.snapshot_product_name, ""),
                            Product.product_name,
                        )
                    ).like(pattern),
                    func.lower(
                        func.coalesce(
                            func.nullif(DeliveryItem.specification_snapshot, ""),
                            OrderItem.snapshot_spec,
                        )
                    ).like(pattern),
                ),
            )
        )
        query = query.where(
            or_(
                func.lower(Delivery.delivery_number).like(pattern),
                customer_keyword_match,
                item_keyword_match,
            )
        )
    if date_from is not None:
        query = query.where(Delivery.delivery_date >= date_from)
    if date_to is not None:
        query = query.where(Delivery.delivery_date <= date_to)

    item_product_code = func.coalesce(
        func.nullif(DeliveryItem.product_code_snapshot, ""),
        func.nullif(OrderItem.snapshot_product_code, ""),
        Product.product_code,
    )
    item_product_name = func.coalesce(
        func.nullif(DeliveryItem.product_name_snapshot, ""),
        func.nullif(OrderItem.snapshot_product_name, ""),
        Product.product_name,
    )
    item_filters = [
        condition
        for condition in (
            _contains(Order.order_number, order_no),
            _contains(func.coalesce(DeliveryItem.customer_po_snapshot, Order.customer_po), customer_po),
            _contains(item_product_code, product_code),
            _contains(item_product_name, product_name),
            _contains(OrderItem.snapshot_spec, spec),
        )
        if condition is not None
    ]
    if item_filters:
        query = query.where(
            exists(
                select(1)
                .select_from(DeliveryItem)
                .outerjoin(OrderItem, OrderItem.id == DeliveryItem.order_item_id)
                .outerjoin(Order, Order.id == OrderItem.order_id)
                .outerjoin(
                    Product,
                    Product.id
                    == func.coalesce(DeliveryItem.product_id, OrderItem.product_id),
                )
                .where(
                    DeliveryItem.delivery_id == Delivery.id,
                    DeliveryItem.is_current.is_(True),
                    *item_filters,
                )
            )
        )

    normalized_return_statuses = {
        value.strip() for value in (return_status or []) if value and value.strip()
    }
    if normalized_return_statuses:
        return_conditions = []
        confirmed_receipt_exists = exists(
            select(1).where(
                ReturnReceipt.delivery_id == Delivery.id,
                ReturnReceipt.status == "confirmed",
            )
        )
        for receipt_status in normalized_return_statuses:
            if receipt_status in {"waiting", "waiting_receipt", "pending"}:
                # A cancelled receipt is not an effective receipt.  The delivery
                # therefore remains actionable in the same waiting set used by
                # the dashboard until a confirmed receipt exists.
                return_conditions.append(~confirmed_receipt_exists)
            else:
                return_conditions.append(
                    exists(
                        select(1).where(
                            ReturnReceipt.delivery_id == Delivery.id,
                            ReturnReceipt.status == receipt_status,
                        )
                    )
                )
        query = query.where(or_(*return_conditions))

    query = query.order_by(
        Delivery.created_at.desc(),
        Delivery.id.desc(),
    )
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    delivery_ids = db.scalars(
        query.offset((page - 1) * page_size).limit(page_size)
    ).all()
    search_matches = _delivery_search_matches(
        db,
        list(delivery_ids),
        customer_po=customer_po,
        product_code=product_code,
        product_name=product_name,
    )
    if view == "summary":
        summary_context = _delivery_list_summary_context(db, list(delivery_ids))
        summary_context["search_matches"] = search_matches
        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "view": "summary",
            "items": [
                _delivery_summary_response(delivery_id, context=summary_context)
                for delivery_id in delivery_ids
            ],
        }
    list_context = _delivery_list_page_context(db, list(delivery_ids))
    response_items = []
    for delivery_id in delivery_ids:
        response = _delivery_response(db, delivery_id, list_context=list_context)
        response["search_matches"] = search_matches.get(
            int(delivery_id), _empty_delivery_search_matches()
        )
        response_items.append(response)
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": response_items,
    }


@router.post("", status_code=status.HTTP_201_CREATED)
def create_delivery(
    payload: DeliveryCreate,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    require_customer_access(payload.customer_id, user, db)
    try:
        _validate_delivery_source_contract(payload.source_mode, payload.items)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    customer = db.get(Customer, payload.customer_id)
    if customer is None:
        raise HTTPException(status_code=400, detail="客户不存在")
    today = beijing_today()
    if payload.historical_backfill:
        _require_historical_delivery_permissions(user)
        if payload.delivery_date is None:
            raise HTTPException(status_code=400, detail="补录历史送货必须填写实际送货日期")
        if any(
            line.source_type != "order" or line.order_item_id is None
            for line in payload.items
        ):
            raise HTTPException(
                status_code=400,
                detail="历史送货补录仅支持可追溯到正式订单的送货明细",
            )
        _validate_historical_delivery_date(
            db,
            actual_delivery_date=payload.delivery_date,
            order_item_ids=[int(line.order_item_id) for line in payload.items],
        )
        delivery_date = payload.delivery_date
    else:
        if payload.delivery_date is not None and payload.delivery_date != today:
            raise HTTPException(
                status_code=400,
                detail="非当天实际送货必须主动选择“补录历史送货”",
            )
        delivery_date = today
    request_hash = _delivery_request_hash(
        "historical_delivery_create",
        payload.model_dump(exclude={"idempotency_key"}),
    )
    replay, replay_record = _delivery_idempotency_replay(
        db,
        idempotency_key=payload.idempotency_key,
        request_hash=request_hash,
        action="historical_delivery_create",
        actor=user,
    )
    if replay is not None:
        assert replay_record is not None
        _delivery_for_user(db, replay_record.resource_id, user)
        return replay
    try:
        delivery = Delivery(
            delivery_number=next_delivery_number(
                db,
                customer=customer,
                delivery_date=delivery_date,
            ),
            customer_id=payload.customer_id,
            delivery_date=delivery_date,
            is_historical_backfill=payload.historical_backfill,
            backfilled_by=(user.id if payload.historical_backfill else None),
            backfilled_at=(_utc_now() if payload.historical_backfill else None),
            version=1,
            vehicle_number=(payload.vehicle_number or "").strip() or None,
            source_mode=payload.source_mode,
            status="pending",
            total_quantity=0,
            created_by=user.id,
        )
        db.add(delivery)
        db.flush()
        warnings: list[dict] = []
        order_payload_lines = [
            line for line in payload.items if line.source_type == "order"
        ]
        unordered_payload_lines = [
            line for line in payload.items if line.source_type == "unordered_finished"
        ]
        built: list[tuple[OrderItem, DeliveryLineCreate]] = []
        built_unordered: list[dict] = []
        order_quantity = 0
        unordered_quantity = 0
        if order_payload_lines:
            built, order_quantity, warnings = _collect_delivery_lines(
                db,
                customer_id=payload.customer_id,
                lines=order_payload_lines,
                user=user,
            )
        if unordered_payload_lines:
            built_unordered, total_quantity = _collect_unordered_finished_lines(
                db,
                customer_id=payload.customer_id,
                lines=unordered_payload_lines,
            )
            unordered_quantity = total_quantity
            _enforce_unordered_finished_order_priority(
                db,
                customer_id=payload.customer_id,
                order_lines=built,
                unordered_lines=built_unordered,
            )
            _store_unordered_finished_items(
                db,
                delivery=delivery,
                built=built_unordered,
                user=user,
            )
        for order_item, line in built:
            order_remaining = max(
                int(order_item.quantity or 0)
                - int(order_item.delivered_quantity or 0),
                0,
            )
            over_delivery = max(line.delivered_quantity - order_remaining, 0)
            db.add(
                DeliveryItem(
                    delivery_id=delivery.id,
                    source_type="order",
                    order_item_id=order_item.id,
                    **build_order_delivery_snapshot(db, order_item),
                    customer_po_snapshot=line.customer_po,
                    delivered_quantity=line.delivered_quantity,
                    ordered_quantity_snapshot=int(order_item.quantity or 0),
                    order_remaining_snapshot=order_remaining,
                    over_delivery_quantity=over_delivery,
                    over_delivery_confirmed_by=(
                        user.id if over_delivery > 0 else None
                    ),
                    over_delivery_reason=(
                        (line.over_delivery_reason or "").strip() or None
                    ),
                    remarks=(line.remarks or "").strip() or None,
                )
            )
        total_quantity = order_quantity + unordered_quantity
        delivery.total_quantity = total_quantity
        _write_audit(
            db,
            user=user,
            action="CREATE",
            resource="Delivery",
            entity_id=delivery.id,
            details={
                "delivery_number": delivery.delivery_number,
                "source_mode": delivery.source_mode,
                "item_count": len(payload.items),
                "total_quantity": total_quantity,
                "is_historical_backfill": payload.historical_backfill,
                "actual_delivery_date": delivery_date,
                "erp_created_at_preserved": True,
            },
            description=(
                "补录历史待发货送货单"
                if payload.historical_backfill
                else "创建待发货送货单"
            ),
        )
        response = _delivery_response(db, delivery.id)
        response["warnings"] = warnings
        _record_delivery_idempotency(
            db,
            idempotency_key=payload.idempotency_key,
            request_hash=request_hash,
            action="historical_delivery_create",
            actor=user,
            delivery_id=delivery.id,
            response=response,
        )
        db.commit()
        return response
    except DeliveryNumberingError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(error)) from error
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError as error:
        db.rollback()
        replay, replay_record = _delivery_idempotency_replay(
            db,
            idempotency_key=payload.idempotency_key,
            request_hash=request_hash,
            action="historical_delivery_create",
            actor=user,
        )
        if replay is not None:
            assert replay_record is not None
            _delivery_for_user(db, replay_record.resource_id, user)
            return replay
        raise HTTPException(status_code=409, detail="送货单数据冲突") from error
    except Exception:
        db.rollback()
        raise


def _dispatch_delivery(
    delivery_id: int,
    *,
    db: Session,
    user: User,
    commit: bool = True,
    write_audit: bool = True,
) -> dict:
    delivery = _delivery_for_user(db, delivery_id, user)
    dispatched_at = _utc_now()
    try:
        pick_task = _delivery_pick_task(db, delivery_id)
        if pick_task and pick_task.status == "exception":
            raise HTTPException(
                status_code=409,
                detail="拿货任务存在异常，请先在电脑端应用拿货结果后再发货",
            )
        order_ids = (
            []
            if delivery.source_mode == "unordered_finished"
            else list(
                db.scalars(
                    select(OrderItem.order_id)
                    .join(DeliveryItem, DeliveryItem.order_item_id == OrderItem.id)
                    .where(
                        DeliveryItem.delivery_id == delivery_id,
                        DeliveryItem.is_current.is_(True),
                    )
                    .distinct()
                    .order_by(OrderItem.order_id)
                ).all()
            )
        )
        locked_orders: dict[int, Order] = {}
        if order_ids:
            try:
                # Global transition order: Order -> Delivery -> OrderItem.  Workflow
                # rollback also starts from Order before touching Delivery rows.
                locked_orders = lock_order_rows_for_production_transition(
                    db, order_ids
                )
            except ProductionWorkflowError as error:
                raise HTTPException(
                    status_code=error.status_code,
                    detail=str(error),
                ) from error
        claimed = db.execute(
            update(Delivery)
            .where(
                Delivery.id == delivery_id,
                Delivery.status == "pending",
            )
            .values(
                status="dispatched",
                dispatched_by=user.id,
                dispatched_at=dispatched_at,
                ever_dispatched_at=func.coalesce(
                    Delivery.ever_dispatched_at,
                    dispatched_at,
                ),
            )
        )
        if claimed.rowcount != 1:
            exists = db.scalar(
                select(Delivery.id).where(Delivery.id == delivery_id)
            )
            if exists is None:
                raise HTTPException(status_code=404, detail="送货单不存在")
            existing_status = db.scalar(
                select(Delivery.status).where(Delivery.id == delivery_id)
            )
            if existing_status == "voided":
                raise HTTPException(
                    status_code=409,
                    detail="送货单已作废，不能再次确认发货",
                )
            raise HTTPException(status_code=409, detail="送货单已确认发货")

        lines = db.scalars(
            select(DeliveryItem)
            .where(
                DeliveryItem.delivery_id == delivery_id,
                DeliveryItem.is_current.is_(True),
            )
            .order_by(DeliveryItem.id)
        ).all()
        if not lines:
            raise HTTPException(
                status_code=409,
                detail="本次送货单已无可发货明细，请删除送货草稿或重新编辑",
            )
        from app.services.fixed_shelf_staging import require_staged_dispatch
        require_staged_dispatch(db, lines)
        order_lines = [line for line in lines if line.source_type == "order"]
        unordered_lines = [
            line for line in lines if line.source_type == "unordered_finished"
        ]
        if delivery.source_mode == "mixed" and (
            not order_lines or not unordered_lines
        ):
            raise HTTPException(
                status_code=409,
                detail="混合送货单来源不完整，请重新编辑后再发货",
            )
        if unordered_lines:
            priority_order_lines: list[tuple[OrderItem, DeliveryItem]] = []
            for line in order_lines:
                order_item = db.get(OrderItem, line.order_item_id)
                if order_item is None:
                    raise HTTPException(
                        status_code=409,
                        detail=f"订单明细{line.order_item_id}不存在",
                    )
                priority_order_lines.append((order_item, line))
            priority_unordered_lines: list[dict] = []
            for line in unordered_lines:
                product = db.get(Product, line.product_id)
                if product is None:
                    raise HTTPException(
                        status_code=409,
                        detail="无订单库存送货产品不存在，请刷新后重试",
                    )
                priority_unordered_lines.append({"product": product, "line": line})
            _enforce_unordered_finished_order_priority(
                db,
                customer_id=delivery.customer_id,
                order_lines=priority_order_lines,
                unordered_lines=priority_unordered_lines,
            )
        if delivery.source_mode == "unordered_finished":
            if any(
                line.source_type != "unordered_finished"
                or line.order_item_id is not None
                for line in lines
            ):
                raise HTTPException(
                    status_code=409,
                    detail="无订单成品库存送货单来源异常，禁止发货",
                )
            dispatch_unordered_finished_inventory(
                db,
                delivery=delivery,
                delivery_items=lines,
                operator_id=user.id,
                dispatched_at=dispatched_at,
            )
            released_pallet_ids = release_empty_pallets_after_delivery(
                db,
                delivery_id=delivery_id,
                operator_id=user.id,
            )
            if pick_task is not None:
                pick_task.status = "dispatched"
                pick_task.dispatched_at = dispatched_at
            if write_audit:
                _write_audit(
                    db,
                    user=user,
                    action="DISPATCH",
                    resource="Delivery",
                    entity_id=delivery_id,
                    details={
                        "source_mode": "unordered_finished",
                        "dispatched_at": dispatched_at,
                        "item_count": len(lines),
                        "total_quantity": sum(
                            int(line.delivered_quantity or 0) for line in lines
                        ),
                        "released_pallet_ids": released_pallet_ids,
                    },
                    description="确认无订单客户专用成品正式发货",
                )
            db.commit() if commit else db.flush()
            return _delivery_response(db, delivery_id)
        current_order_ids = set(
            db.scalars(
                select(OrderItem.order_id)
                .where(OrderItem.id.in_([line.order_item_id for line in lines]))
                .distinct()
            ).all()
        )
        if not current_order_ids.issubset(set(order_ids)):
            raise HTTPException(
                status_code=409,
                detail="送货单明细已变化，请刷新后重新确认发货",
            )
        affected_order_ids: set[int] = set()
        for line in order_lines:
            order_item = db.get(OrderItem, line.order_item_id)
            if order_item is None:
                raise HTTPException(
                    status_code=409,
                    detail=f"订单明细{line.order_item_id}不存在",
                )
            order = locked_orders.get(int(order_item.order_id))
            if order is None:
                raise HTTPException(
                    status_code=409,
                    detail=f"订单明细{line.order_item_id}关联订单不存在",
                )
            forward_block = order_item_forward_block_reason(
                order_status=order.status,
                ordered_quantity=order_item.quantity,
                delivered_quantity=order_item.delivered_quantity,
                is_force_closed=order_item.is_force_closed,
            )
            if forward_block is not None:
                raise HTTPException(
                    status_code=409,
                    detail=order_item_forward_block_message(
                        forward_block,
                        action="发货",
                        order_status=order.status,
                    ),
                )
            production_managed = _has_production_task(db, order_item.id)
            if production_managed:
                delivered_before = int(order_item.delivered_quantity or 0)
                locked = db.execute(
                    update(OrderItem)
                    .where(
                        OrderItem.id == order_item.id,
                        OrderItem.delivered_quantity == delivered_before,
                    )
                    .values(delivered_quantity=OrderItem.delivered_quantity)
                    .execution_options(synchronize_session=False)
                )
                if locked.rowcount != 1:
                    raise HTTPException(
                        status_code=409,
                        detail=f"订单明细{line.order_item_id}状态已变化，请刷新后重试",
                    )
                db.flush()
                db.expire(order_item)
                order_item = db.get(OrderItem, line.order_item_id)
                if order_item is None:
                    raise HTTPException(
                        status_code=409,
                        detail=f"订单明细{line.order_item_id}不存在",
                    )
                production_managed = _has_production_task(db, order_item.id)
            remaining = _delivery_remaining_quantity(db, order_item)
            if production_managed and (
                order_item.is_force_closed
                or remaining <= 0
            ):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"订单明细{line.order_item_id}生产可送数量不足，"
                        f"当前可送数量为 {remaining}"
                    ),
                )
            if line.delivered_quantity > remaining:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"订单明细{line.order_item_id}实际可送成品不足，"
                        f"当前最多可送 {remaining}"
                    ),
                )
            order_remaining_before = max(
                int(order_item.quantity or 0)
                - int(order_item.delivered_quantity or 0),
                0,
            )
            over_delivery = max(
                int(line.delivered_quantity) - order_remaining_before,
                0,
            )
            if over_delivery > 0:
                if not has_permission(user, "deliveries.over_delivery"):
                    raise HTTPException(
                        status_code=403,
                        detail="当前账号没有超量送货权限",
                    )
                confirmed_over_delivery = max(
                    int(line.over_delivery_quantity or 0), 0
                )
                confirmed_reason = str(line.over_delivery_reason or "").strip()
                if (
                    line.over_delivery_confirmed_by is None
                    or not confirmed_reason
                    or over_delivery > confirmed_over_delivery
                ):
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "订单剩余数量在送货单创建后发生变化，本单现在形成了"
                            f" {over_delivery} 个超量；请刷新送货单并重新确认原因。"
                        ),
                    )
            line.ordered_quantity_snapshot = int(order_item.quantity or 0)
            line.order_remaining_snapshot = order_remaining_before
            line.over_delivery_quantity = over_delivery
            ensure_order_delivery_snapshot(db, line, order_item)
            component_capacity = (
                None
                if production_managed
                else _received_telescoping_capacity(db, order_item.id)
            )
            component_remaining = (
                max(component_capacity - int(order_item.delivered_quantity or 0), 0)
                if component_capacity is not None
                else None
            )
            if (
                component_remaining is not None
                and line.delivered_quantity > component_remaining
            ):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"订单明细{line.order_item_id}天地盖盖/底成套可送数量不足，"
                        f"当前物理可送数量为 {component_remaining}"
                    ),
                )
            order_id = order_item.order_id
            delivered_before = int(order_item.delivered_quantity or 0)
            operation_key = (
                f"d{delivery_id}-{dispatched_at:%Y%m%d%H%M%S%f}-i{line.id}"
            )
            from app.services.bom_subkit_delivery import consume_delivery_subkits
            from app.services.bom_subkits import SubkitError
            try:
                consume_delivery_subkits(db, delivery_item_id=line.id, operator_id=user.id, operation_key=operation_key)
            except SubkitError as error:
                raise HTTPException(status_code=error.status_code, detail=str(error)) from error
            if _uses_composite_inventory(db, order_item.id):
                execute_delivery_component_consumption(
                    db,
                    delivery_item_id=line.id,
                    delivery_sets=line.delivered_quantity,
                    operator_id=user.id,
                    operation_key=operation_key,
                )
            else:
                consume_delivery_item_inventory(
                    db,
                    delivery_item_id=line.id,
                    delivered_quantity_after_dispatch=(
                        delivered_before + line.delivered_quantity
                    ),
                    operator_id=user.id,
                    operation_key=operation_key,
                )
            result = db.execute(
                update(OrderItem)
                .where(
                    OrderItem.id == line.order_item_id,
                    OrderItem.is_force_closed.is_(False),
                    OrderItem.delivered_quantity == delivered_before,
                )
                .values(
                    delivered_quantity=(
                        OrderItem.delivered_quantity + line.delivered_quantity
                    )
                )
            )
            if result.rowcount != 1:
                raise HTTPException(
                    status_code=409,
                    detail=f"订单明细{line.order_item_id}状态或可发数量已变化",
                )
            if order_id is not None:
                affected_order_ids.add(order_id)
        if unordered_lines:
            dispatch_unordered_finished_inventory(
                db,
                delivery=delivery,
                delivery_items=unordered_lines,
                operator_id=user.id,
                dispatched_at=dispatched_at,
            )
        released_pallet_ids = release_empty_pallets_after_delivery(
            db,
            delivery_id=delivery_id,
            operator_id=user.id,
        )
        for order_id in affected_order_ids:
            _refresh_order_status(db, order_id)
        if pick_task is not None:
            pick_task.status = "dispatched"
            pick_task.dispatched_at = dispatched_at
        if write_audit:
            _write_audit(
                db,
                user=user,
                action="DISPATCH",
                resource="Delivery",
                entity_id=delivery_id,
                details={
                    "dispatched_at": dispatched_at,
                    "item_count": len(lines),
                    "source_mode": delivery.source_mode,
                    "unordered_item_count": len(unordered_lines),
                    "released_pallet_ids": released_pallet_ids,
                    "over_delivery_quantity": sum(
                        int(line.over_delivery_quantity or 0) for line in lines
                    ),
                    "over_delivery_items": [
                        {
                            "delivery_item_id": line.id,
                            "order_item_id": line.order_item_id,
                            "ordered_quantity_snapshot": line.ordered_quantity_snapshot,
                            "actual_delivery_quantity": line.delivered_quantity,
                            "over_delivery_quantity": line.over_delivery_quantity,
                            "confirmed_by": line.over_delivery_confirmed_by,
                            "reason": line.over_delivery_reason,
                        }
                        for line in lines
                        if int(line.over_delivery_quantity or 0) > 0
                    ],
                },
                description="确认送货单发货",
            )
        db.commit() if commit else db.flush()
        return _delivery_response(db, delivery_id)
    except HTTPException:
        db.rollback()
        raise
    except WarehouseInventoryError as error:
        db.rollback()
        raise HTTPException(
            status_code=error.status_code,
            detail=str(error),
        ) from error
    except CompositeBomWorkflowError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(error)) from error
    except Exception:
        db.rollback()
        raise


@router.put("/{delivery_id}/dispatch")
def dispatch_delivery(
    delivery_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    return _dispatch_delivery(delivery_id, db=db, user=user)


def _update_delivery(
    delivery_id: int,
    payload: DeliveryUpdate,
    *,
    db: Session,
    user: User,
    commit: bool = True,
    revision_mode: bool = False,
) -> dict:
    existing_delivery = _delivery_for_user(db, delivery_id, user)
    previous_po = {
        (row.source_type, row.order_item_id if row.source_type == "order" else row.product_id): row.customer_po_snapshot
        for row in db.scalars(select(DeliveryItem).where(
            DeliveryItem.delivery_id == delivery_id, DeliveryItem.is_current.is_(True)
        ))
    }
    for line in payload.items:
        if "customer_po" not in line.model_fields_set:
            line.customer_po = previous_po.get((line.source_type, line.order_item_id if line.source_type == "order" else line.product_id))
    historical_backfill = bool(existing_delivery.is_historical_backfill)
    if (
        payload.historical_backfill is not None
        and bool(payload.historical_backfill) != historical_backfill
    ):
        raise HTTPException(
            status_code=409,
            detail="送货单保存后不能切换普通送货与历史补录模式",
        )
    if historical_backfill:
        _require_historical_delivery_permissions(user)
        if payload.expected_version is None or payload.idempotency_key is None:
            raise HTTPException(
                status_code=400,
                detail="编辑历史补录送货单必须提交版本和幂等键",
            )
        if any(
            line.source_type != "order" or line.order_item_id is None
            for line in payload.items
        ):
            raise HTTPException(
                status_code=400,
                detail="历史送货补录仅支持可追溯到正式订单的送货明细",
            )
        target_delivery_date = payload.delivery_date or existing_delivery.delivery_date
        _validate_historical_delivery_date(
            db,
            actual_delivery_date=target_delivery_date,
            order_item_ids=[int(line.order_item_id) for line in payload.items],
        )
    elif revision_mode:
        target_delivery_date = payload.delivery_date or existing_delivery.delivery_date
        if target_delivery_date != existing_delivery.delivery_date:
            _require_historical_delivery_permissions(user)
            order_item_ids = [
                int(line.order_item_id)
                for line in payload.items
                if line.source_type == "order" and line.order_item_id is not None
            ]
            if len(order_item_ids) != len(payload.items):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "送货来源包含无订单库存，无法证明订单日期下限，"
                        "禁止在本次编辑中改写实际送货日期"
                    ),
                )
            _validate_historical_delivery_date(
                db,
                actual_delivery_date=target_delivery_date,
                order_item_ids=order_item_ids,
            )
    else:
        target_delivery_date = existing_delivery.delivery_date
        if (
            payload.delivery_date is not None
            and payload.delivery_date != existing_delivery.delivery_date
        ):
            raise HTTPException(
                status_code=409,
                detail="普通送货单的实际日期不能在编辑明细时改写，请使用受控日期更正",
            )
        if payload.idempotency_key is not None:
            raise HTTPException(status_code=400, detail="普通送货编辑不要提交补录幂等键")
    request_hash = _delivery_request_hash(
        "historical_delivery_update",
        {"delivery_id": delivery_id, **payload.model_dump(exclude={"idempotency_key"})},
    )
    if not revision_mode:
        replay, replay_record = _delivery_idempotency_replay(
            db,
            idempotency_key=payload.idempotency_key,
            request_hash=request_hash,
            action="historical_delivery_update",
            actor=user,
        )
        if replay is not None:
            assert replay_record is not None
            _delivery_for_user(db, replay_record.resource_id, user)
            return replay
    try:
        _validate_delivery_source_contract(payload.source_mode, payload.items)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    try:
        # 先以条件写取得 SQLite 写锁。这样编辑与发货并发时，
        # 只有先取得 pending 状态的一方继续，避免按过期状态替换明细。
        claimed = db.execute(
            update(Delivery)
            .where(
                Delivery.id == delivery_id,
                Delivery.status == "pending",
            )
            .values(status="pending")
        )
        if claimed.rowcount != 1:
            existing_status = db.scalar(
                select(Delivery.status).where(Delivery.id == delivery_id)
            )
            if existing_status is None:
                raise HTTPException(status_code=404, detail="送货单不存在")
            if existing_status == "voided":
                raise HTTPException(
                    status_code=409,
                    detail="送货单已作废，不能编辑",
                )
            raise HTTPException(
                status_code=409,
                detail="送货单已确认发货，不能编辑，请先取消发货",
            )
        delivery = _delivery_or_404(db, delivery_id)
        current_version = int(delivery.version or 1)
        if (
            payload.expected_version is not None
            and payload.expected_version != current_version
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "delivery_version_conflict",
                    "message": "送货单版本已变化，请刷新后重试",
                },
            )
        if payload.source_mode != delivery.source_mode and not revision_mode:
            raise HTTPException(
                status_code=409,
                detail="送货单保存后不能切换订单待送与无订单库存来源",
            )
        _prepare_shelf_draft_replacement(db, delivery_id)
        _discard_delivery_pick_task(
            db,
            delivery_id=delivery_id,
            user=user,
            reason="delivery_draft_updated",
        )
        warnings: list[dict] = []
        order_payload_lines = [
            line for line in payload.items if line.source_type == "order"
        ]
        unordered_payload_lines = [
            line for line in payload.items if line.source_type == "unordered_finished"
        ]
        built: list[tuple[OrderItem, DeliveryLineCreate]] = []
        built_unordered: list[dict] = []
        order_quantity = 0
        unordered_quantity = 0
        if order_payload_lines:
            built, order_quantity, warnings = _collect_delivery_lines(
                db,
                customer_id=delivery.customer_id,
                lines=order_payload_lines,
                user=user,
            )
        if unordered_payload_lines:
            built_unordered, total_quantity = _collect_unordered_finished_lines(
                db,
                customer_id=delivery.customer_id,
                lines=unordered_payload_lines,
            )
            unordered_quantity = total_quantity
            _enforce_unordered_finished_order_priority(
                db,
                customer_id=delivery.customer_id,
                order_lines=built,
                unordered_lines=built_unordered,
            )
        if delivery.source_mode in {"unordered_finished", "mixed"}:
            delivery_item_ids = select(DeliveryItem.id).where(
                DeliveryItem.delivery_id == delivery_id,
                DeliveryItem.is_current.is_(True),
            )
            db.execute(
                delete(UnorderedFinishedDeliveryAllocation).where(
                    UnorderedFinishedDeliveryAllocation.delivery_item_id.in_(
                        delivery_item_ids
                    )
                )
            )
        db.execute(
            delete(DeliveryItem).where(
                DeliveryItem.delivery_id == delivery_id,
                DeliveryItem.is_current.is_(True),
            )
        )
        db.flush()
        delivery.source_mode = payload.source_mode
        if built_unordered:
            _store_unordered_finished_items(
                db,
                delivery=delivery,
                built=built_unordered,
                user=user,
            )
        for order_item, line in built:
            order_remaining = max(
                int(order_item.quantity or 0)
                - int(order_item.delivered_quantity or 0),
                0,
            )
            over_delivery = max(line.delivered_quantity - order_remaining, 0)
            db.add(
                DeliveryItem(
                    delivery_id=delivery.id,
                    source_type="order",
                    order_item_id=order_item.id,
                    **build_order_delivery_snapshot(db, order_item),
                    customer_po_snapshot=line.customer_po,
                    delivered_quantity=line.delivered_quantity,
                    ordered_quantity_snapshot=int(order_item.quantity or 0),
                    order_remaining_snapshot=order_remaining,
                    over_delivery_quantity=over_delivery,
                    over_delivery_confirmed_by=(
                        user.id if over_delivery > 0 else None
                    ),
                    over_delivery_reason=(
                        (line.over_delivery_reason or "").strip() or None
                    ),
                    remarks=(line.remarks or "").strip() or None,
                )
            )
        total_quantity = order_quantity + unordered_quantity
        delivery.delivery_date = target_delivery_date
        if payload.vehicle_number is not None:
            delivery.vehicle_number = payload.vehicle_number.strip() or None
        delivery.total_quantity = total_quantity
        delivery.version = current_version + 1
        if not revision_mode:
            _write_audit(
                db,
                user=user,
                action="UPDATE",
                resource="Delivery",
                entity_id=delivery.id,
                details={
                    "delivery_number": delivery.delivery_number,
                    "source_mode": delivery.source_mode,
                    "item_count": len(payload.items),
                    "total_quantity": total_quantity,
                    "is_historical_backfill": historical_backfill,
                    "actual_delivery_date": delivery.delivery_date,
                    "before_version": current_version,
                    "after_version": delivery.version,
                },
                description="编辑待发货送货单",
            )
        response = _delivery_response(db, delivery.id)
        response["warnings"] = warnings
        if not revision_mode:
            _record_delivery_idempotency(
                db,
                idempotency_key=payload.idempotency_key,
                request_hash=request_hash,
                action="historical_delivery_update",
                actor=user,
                delivery_id=delivery.id,
                response=response,
            )
        db.commit() if commit else db.flush()
        return response
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError as error:
        db.rollback()
        if not revision_mode:
            replay, replay_record = _delivery_idempotency_replay(
                db,
                idempotency_key=payload.idempotency_key,
                request_hash=request_hash,
                action="historical_delivery_update",
                actor=user,
            )
            if replay is not None:
                assert replay_record is not None
                _delivery_for_user(db, replay_record.resource_id, user)
                return replay
        raise HTTPException(status_code=409, detail="送货单数据冲突") from error
    except Exception:
        db.rollback()
        raise


@router.put("/{delivery_id}")
def update_delivery(
    delivery_id: int,
    payload: DeliveryUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    return _update_delivery(delivery_id, payload, db=db, user=user)


@router.put("/{delivery_id}/revision")
def revise_dispatched_delivery(
    delivery_id: int,
    payload: DeliveryRevisionUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    """Atomically replace the active revision without rewriting dispatch history."""

    request_hash = _delivery_request_hash(
        "delivery_revision_update",
        {
            "delivery_id": delivery_id,
            **payload.model_dump(exclude={"idempotency_key"}),
        },
    )
    replay, replay_record = _delivery_idempotency_replay(
        db,
        idempotency_key=payload.idempotency_key,
        request_hash=request_hash,
        action="delivery_revision_update",
        actor=user,
    )
    if replay is not None:
        assert replay_record is not None
        _delivery_for_user(db, replay_record.resource_id, user)
        return replay

    delivery = _delivery_for_user(db, delivery_id, user)
    try:
        claimed = db.execute(
            update(Delivery)
            .where(
                Delivery.id == delivery_id,
                Delivery.status == "dispatched",
                Delivery.version == payload.expected_version,
            )
            .values(version=Delivery.version)
        )
        if claimed.rowcount != 1:
            current = db.execute(
                select(Delivery.status, Delivery.version).where(
                    Delivery.id == delivery_id
                )
            ).one_or_none()
            if current is None:
                raise HTTPException(status_code=404, detail="送货单不存在")
            if current.status == "voided":
                raise HTTPException(status_code=409, detail="送货单已作废，不能编辑")
            if current.status != "dispatched":
                raise HTTPException(
                    status_code=409,
                    detail="只有已发货且待回单的送货单可以受控编辑",
                )
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "delivery_version_conflict",
                    "message": "送货单版本已变化，请刷新后重试",
                },
            )

        receipt = db.scalar(
            select(ReturnReceipt).where(ReturnReceipt.delivery_id == delivery_id)
        )
        if receipt is not None and receipt.status != "cancelled":
            raise HTTPException(
                status_code=409,
                detail="送货单已有有效回单或待确认回单，不能直接编辑",
            )
        block_reason = _delivery_finance_chain_block_reason(db, delivery_id)
        if block_reason:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "delivery_revision_finance_locked",
                    "message": block_reason,
                },
            )
        try:
            _validate_delivery_source_contract(payload.source_mode, payload.items)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

        old_lines = db.scalars(
            select(DeliveryItem)
            .where(
                DeliveryItem.delivery_id == delivery_id,
                DeliveryItem.is_current.is_(True),
            )
            .order_by(DeliveryItem.id)
        ).all()
        if not old_lines:
            raise HTTPException(status_code=409, detail="送货单当前明细为空，不能受控编辑")
        old_revision = max(int(line.revision_number or 1) for line in old_lines)
        before = {
            "version": int(delivery.version or 1),
            "source_mode": delivery.source_mode,
            "delivery_date": delivery.delivery_date,
            "vehicle_number": delivery.vehicle_number,
            "total_quantity": int(delivery.total_quantity or 0),
            "dispatched_at": delivery.dispatched_at,
            "dispatched_by": delivery.dispatched_by,
            "printed_at": delivery.printed_at,
            "items": [
                {
                    "delivery_item_id": line.id,
                    "source_type": line.source_type,
                    "order_item_id": line.order_item_id,
                    "product_id": line.product_id,
                    "quantity": int(line.delivered_quantity or 0),
                    "revision_number": int(line.revision_number or 1),
                }
                for line in old_lines
            ],
        }
        original_dispatched_at = delivery.dispatched_at
        original_dispatched_by = delivery.dispatched_by

        _cancel_delivery(
            delivery_id,
            db=db,
            user=user,
            commit=False,
            revision_mode=True,
        )
        prior_po = {(line.source_type, line.order_item_id if line.source_type == "order" else line.product_id): line.customer_po_snapshot for line in old_lines}
        for item in payload.items:
            if "customer_po" not in item.model_fields_set:
                item.customer_po = prior_po.get((item.source_type, item.order_item_id if item.source_type == "order" else item.product_id))
        for line in old_lines:
            line.is_current = False
        db.flush()

        _update_delivery(
            delivery_id,
            payload,
            db=db,
            user=user,
            commit=False,
            revision_mode=True,
        )
        next_revision = old_revision + 1
        new_lines = db.scalars(
            select(DeliveryItem)
            .where(
                DeliveryItem.delivery_id == delivery_id,
                DeliveryItem.is_current.is_(True),
            )
            .order_by(DeliveryItem.id)
        ).all()
        for line in new_lines:
            line.revision_number = next_revision
        db.flush()

        _dispatch_delivery(
            delivery_id,
            db=db,
            user=user,
            commit=False,
            write_audit=False,
        )
        delivery = _delivery_or_404(db, delivery_id)
        delivery.dispatched_at = original_dispatched_at
        delivery.dispatched_by = original_dispatched_by
        delivery.printed_at = None
        delivery.printed_by = None
        db.flush()

        after = {
            "version": int(delivery.version or 1),
            "source_mode": delivery.source_mode,
            "delivery_date": delivery.delivery_date,
            "vehicle_number": delivery.vehicle_number,
            "total_quantity": int(delivery.total_quantity or 0),
            "items": [
                {
                    "delivery_item_id": line.id,
                    "source_type": line.source_type,
                    "order_item_id": line.order_item_id,
                    "product_id": line.product_id,
                    "quantity": int(line.delivered_quantity or 0),
                    "revision_number": int(line.revision_number or 1),
                }
                for line in new_lines
            ],
        }
        _write_audit(
            db,
            user=user,
            action="REVISE_DISPATCHED_DELIVERY",
            resource="Delivery",
            entity_id=delivery_id,
            details={
                "delivery_number": delivery.delivery_number,
                "before": before,
                "after": after,
                "reprint_required": True,
            },
            description="受控编辑已发货待回单送货单并保留原明细修订历史",
        )
        response = _delivery_response(db, delivery_id)
        response["warnings"] = [
            {
                "code": "delivery_reprint_required",
                "message": "送货单内容已修改，请补打最新版",
            }
        ]
        response["reprint_required"] = True
        _record_delivery_idempotency(
            db,
            idempotency_key=payload.idempotency_key,
            request_hash=request_hash,
            action="delivery_revision_update",
            actor=user,
            delivery_id=delivery_id,
            response=response,
        )
        db.commit()
        return response
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError as error:
        db.rollback()
        replay, replay_record = _delivery_idempotency_replay(
            db,
            idempotency_key=payload.idempotency_key,
            request_hash=request_hash,
            action="delivery_revision_update",
            actor=user,
        )
        if replay is not None:
            assert replay_record is not None
            _delivery_for_user(db, replay_record.resource_id, user)
            return replay
        raise HTTPException(status_code=409, detail="送货单修订数据冲突") from error
    except Exception:
        db.rollback()
        raise


@router.put("/{delivery_id}/actual-date")
def correct_delivery_actual_date(
    delivery_id: int,
    payload: DeliveryDateCorrection,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    delivery = _delivery_for_user(db, delivery_id, user)
    _require_historical_delivery_permissions(user)
    if delivery.status == "voided":
        raise HTTPException(status_code=409, detail="已作废送货单不能更正实际日期")
    order_item_ids = list(
        db.scalars(
            select(DeliveryItem.order_item_id)
            .where(
                DeliveryItem.delivery_id == delivery.id,
                DeliveryItem.is_current.is_(True),
            )
            .order_by(DeliveryItem.id)
        ).all()
    )
    if not order_item_ids or any(value is None for value in order_item_ids):
        raise HTTPException(
            status_code=409,
            detail="送货来源包含无订单库存，无法证明订单日期下限，禁止直接更正",
        )
    lower_bound = _validate_historical_delivery_date(
        db,
        actual_delivery_date=payload.actual_delivery_date,
        order_item_ids=[int(value) for value in order_item_ids],
    )
    request_hash = _delivery_request_hash(
        "delivery_actual_date_update",
        {"delivery_id": delivery_id, **payload.model_dump(exclude={"idempotency_key"})},
    )
    replay, replay_record = _delivery_idempotency_replay(
        db,
        idempotency_key=payload.idempotency_key,
        request_hash=request_hash,
        action="delivery_actual_date_update",
        actor=user,
    )
    if replay is not None:
        assert replay_record is not None
        _delivery_for_user(db, replay_record.resource_id, user)
        return replay
    block_reason = _delivery_finance_chain_block_reason(db, delivery.id)
    if block_reason:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "delivery_actual_date_locked",
                "message": block_reason,
            },
        )
    if int(delivery.version or 1) != payload.expected_version:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "delivery_version_conflict",
                "message": "送货单版本已变化，请刷新后重试",
            },
        )
    before = {
        "delivery_date": delivery.delivery_date,
        "is_historical_backfill": bool(delivery.is_historical_backfill),
        "version": int(delivery.version or 1),
        "created_at": delivery.created_at,
    }
    if delivery.delivery_date == payload.actual_delivery_date:
        response = _delivery_response(db, delivery.id)
        _record_delivery_idempotency(
            db,
            idempotency_key=payload.idempotency_key,
            request_hash=request_hash,
            action="delivery_actual_date_update",
            actor=user,
            delivery_id=delivery.id,
            response=response,
        )
        db.commit()
        return response
    new_version = payload.expected_version + 1
    try:
        claimed = db.execute(
            update(Delivery)
            .where(
                Delivery.id == delivery.id,
                Delivery.version == payload.expected_version,
                Delivery.status != "voided",
            )
            .values(
                delivery_date=payload.actual_delivery_date,
                is_historical_backfill=True,
                backfilled_by=func.coalesce(Delivery.backfilled_by, user.id),
                backfilled_at=func.coalesce(Delivery.backfilled_at, _utc_now()),
                version=new_version,
            )
            .execution_options(synchronize_session=False)
        )
    except Exception:
        db.rollback()
        raise
    if claimed.rowcount != 1:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "delivery_version_conflict",
                "message": "送货单日期已被其他操作修改，请刷新后重试",
            },
        )
    db.expire(delivery)
    _write_audit(
        db,
        user=user,
        action="CORRECT_ACTUAL_DELIVERY_DATE",
        resource="Delivery",
        entity_id=delivery.id,
        details={
            "before": before,
            "after": {
                "delivery_date": payload.actual_delivery_date,
                "is_historical_backfill": True,
                "version": new_version,
                "created_at": before["created_at"],
            },
            "order_date_lower_bound": lower_bound,
        },
        description="更正送货单实际送货日期",
    )
    response = _delivery_response(db, delivery.id)
    _record_delivery_idempotency(
        db,
        idempotency_key=payload.idempotency_key,
        request_hash=request_hash,
        action="delivery_actual_date_update",
        actor=user,
        delivery_id=delivery.id,
        response=response,
    )
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        replay, replay_record = _delivery_idempotency_replay(
            db,
            idempotency_key=payload.idempotency_key,
            request_hash=request_hash,
            action="delivery_actual_date_update",
            actor=user,
        )
        if replay is not None:
            assert replay_record is not None
            _delivery_for_user(db, replay_record.resource_id, user)
            return replay
        raise HTTPException(
            status_code=409,
            detail={
                "code": "delivery_idempotency_conflict",
                "message": "实际送货日期已被其他操作修改，请刷新后重试",
            },
        ) from error
    return response


@router.get("/{delivery_id}/production-packaging-label-package")
def get_delivery_production_packaging_label_package(
    delivery_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    delivery = _delivery_for_label_user(db, delivery_id, user)
    try:
        package = build_delivery_packaging_label_package(db, delivery)
    except (
        ProductionPackagingLabelError,
        ProductionPackagingLabelLayoutError,
    ) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    if package["review_required"]:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "delivery_label_review_required",
                "message": "送货产品标签计划需要核对",
                "reasons": package["review_messages"],
            },
        )
    if not package["label_count"]:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "delivery_label_not_enabled",
                "message": "该送货单没有在常用箱中勾选打印标签的产品",
                "excluded_items": package.get("excluded_items") or [],
            },
        )
    package["latest_printed_job"] = latest_printed_delivery_job_metadata(
        db,
        delivery.id,
    )
    return package


@router.get("/{delivery_id}/production-packaging-label-latest-job")
def get_delivery_latest_production_packaging_label_job(
    delivery_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    delivery = _delivery_for_label_user(db, delivery_id, user)
    latest = latest_printed_delivery_job_metadata(db, delivery.id)
    if latest is None:
        raise HTTPException(
            status_code=404,
            detail="该送货单没有已登记打印的产品标签作业",
        )
    return {"delivery_id": int(delivery.id), "latest_printed_job": latest}


@router.post("/{delivery_id}/production-packaging-label-jobs")
def post_delivery_production_packaging_label_job(
    delivery_id: int,
    payload: DeliveryPackagingLabelJobRequest,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
    _write_guard: None = Depends(production_label_write_guard),
) -> dict:
    delivery = _delivery_for_label_user(db, delivery_id, user)
    is_reprint = (
        latest_printed_delivery_job_metadata(db, delivery.id) is not None
    )
    try:
        result = prepare_delivery_packaging_label_job(
            db,
            delivery=delivery,
            idempotency_key=payload.idempotency_key,
            expected_plan_fingerprint=payload.plan_fingerprint,
            requested_print_counts={
                item.selection_key: item.print_label_count
                for item in payload.items
            },
            operator_id=user.id,
        )
        if not result.replayed:
            append_audit_event(
                db,
                event_category="business",
                result="success",
                source="web",
                module_code="delivery",
                action_code="delivery.packaging_label_job.prepared",
                legacy_action="PREPARE_DELIVERY_LABEL_JOB",
                resource="ProductionPackagingLabelPrintJob",
                actor=user,
                entity_type="production_packaging_label_print_job",
                entity_id=result.job.id,
                object_ref=(
                    f"production_packaging_label_print_job:{result.job.id}"
                ),
                batch_id=result.job.idempotency_key,
                description="冻结送货产品标签打印作业",
                details={
                    "delivery_id": delivery.id,
                    "delivery_number": delivery.delivery_number,
                    "template_version": result.job.template_version,
                    "label_policy_source": result.package.get(
                        "label_policy_source"
                    ),
                    "is_reprint": is_reprint,
                    "plan_fingerprint": result.job.plan_fingerprint,
                    "payload_hash": result.job.payload_hash,
                    "layout_version": (
                        (result.package.get("label_layout") or {}).get("version")
                    ),
                    "layout_hash": (
                        (result.package.get("label_layout") or {}).get(
                            "layout_hash"
                        )
                    ),
                    "label_count": result.package.get("label_count"),
                    "system_label_count": result.package.get(
                        "system_label_count"
                    ),
                    "print_selection": result.package.get("print_selection"),
                    "fulfillment_modes": sorted(
                        {
                            str(plan.get("fulfillment_mode") or "")
                            for plan in result.package.get("plans") or []
                        }
                    ),
                    "lines": [
                        {
                            "selection_key": plan.get("selection_key"),
                            "delivery_item_id": plan.get("delivery_item_id"),
                            "order_item_id": plan.get("order_item_id"),
                            "component_snapshot_id": plan.get(
                                "component_snapshot_id"
                            ),
                            "product_id": plan.get("product_id"),
                            "product_code": plan.get("product_code"),
                            "product_name": plan.get("product_name"),
                            "total_quantity": plan.get("total_quantity"),
                            "units_per_label": plan.get("units_per_label"),
                            "print_label_count": plan.get(
                                "print_label_count",
                                plan.get("label_count"),
                            ),
                            "fulfillment_mode": plan.get("fulfillment_mode"),
                        }
                        for plan in result.package.get("plans") or []
                        if isinstance(plan, dict)
                    ],
                },
            )
        db.commit()
        return packaging_label_job_response(
            result.job,
            result.package,
            replayed=result.replayed,
        )
    except (
        ProductionPackagingLabelError,
        ProductionPackagingLabelLayoutError,
    ) as error:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ProductionLabelOperationError as error:
        db.rollback()
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    except IntegrityError as error:
        db.rollback()
        repeated = db.scalar(
            select(ProductionPackagingLabelPrintJob).where(
                ProductionPackagingLabelPrintJob.idempotency_key
                == payload.idempotency_key
            )
        )
        if repeated is not None:
            try:
                replay = prepare_delivery_packaging_label_job(
                    db,
                    delivery=_delivery_for_label_user(db, delivery_id, user),
                    idempotency_key=payload.idempotency_key,
                    expected_plan_fingerprint=payload.plan_fingerprint,
                    requested_print_counts={
                        item.selection_key: item.print_label_count
                        for item in payload.items
                    },
                    operator_id=user.id,
                )
                db.commit()
                return packaging_label_job_response(
                    replay.job,
                    replay.package,
                    replayed=True,
                )
            except ProductionLabelOperationError as replay_error:
                db.rollback()
                raise HTTPException(
                    status_code=replay_error.status_code,
                    detail=str(replay_error),
                ) from replay_error
        raise HTTPException(
            status_code=409,
            detail="送货标签打印作业已被其他请求创建，请刷新后重试",
        ) from error
    except Exception:
        db.rollback()
        raise


@router.delete("/{delivery_id}")
def delete_delivery(
    delivery_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    delivery = db.get(Delivery, delivery_id)
    if delivery is None:
        return {
            "deleted": True,
            "voided": False,
            "disposition": "deleted",
            "id": delivery_id,
        }
    require_customer_access(delivery.customer_id, user, db)
    try:
        if delivery.status == "voided":
            return {
                "deleted": False,
                "voided": True,
                "disposition": "voided",
                "id": delivery_id,
            }
        # 与确认发货竞争时先锁定 pending 状态，防止发货累计数量后
        # 送货单又被按旧状态删除。
        claimed = db.execute(
            update(Delivery)
            .where(
                Delivery.id == delivery_id,
                Delivery.status == "pending",
            )
            .values(status="pending")
        )
        if claimed.rowcount != 1:
            existing_status = db.scalar(
                select(Delivery.status).where(Delivery.id == delivery_id)
            )
            if existing_status is None:
                db.rollback()
                return {
                    "deleted": True,
                    "voided": False,
                    "disposition": "deleted",
                    "id": delivery_id,
                }
            if existing_status == "voided":
                return {
                    "deleted": False,
                    "voided": True,
                    "disposition": "voided",
                    "id": delivery_id,
                }
            raise HTTPException(
                status_code=409,
                detail="已确认发货的送货单不能删除，请改用取消发货",
            )
        delivery = _delivery_or_404(db, delivery_id)
        _prepare_shelf_draft_replacement(db, delivery_id)
        facts = _delivery_deletion_facts(db, delivery)
        if facts["statement_item_id"] is not None:
            raise HTTPException(
                status_code=409,
                detail="送货单已进入对账，不能删除或作废；请先反审核对账",
            )
        receipt = facts["receipt"]
        if receipt is not None and receipt.status == "confirmed":
            raise HTTPException(
                status_code=409,
                detail="送货单已有有效回单，不能删除或作废；请先撤销回单",
            )
        if any(
            row.status != "reversed"
            or int(row.reversed_stock_quantity or 0)
            != int(row.consumed_stock_quantity or 0)
            or int(row.reversed_requirement_quantity or 0)
            != int(row.credited_requirement_quantity or 0)
            for row in facts["inventory_allocations"]
        ):
            raise HTTPException(
                status_code=409,
                detail="送货单库存抵扣尚未全部冲回，不能作废；请先取消发货并刷新后重试",
            )
        if any(
            row.status != "reversed"
            or int(row.reversed_quantity or 0)
            != int(row.consumed_quantity or 0)
            for row in facts["component_allocations"]
        ):
            raise HTTPException(
                status_code=409,
                detail="送货单组件成品抵扣尚未全部冲回，不能作废；请先取消发货并刷新后重试",
            )
        if any(
            int(row.restored_quantity or 0)
            != int(row.consumed_quantity or 0)
            for row in facts["unordered_allocations"]
        ):
            raise HTTPException(
                status_code=409,
                detail="无订单成品送货库存尚未全部退回原批次，不能作废",
            )
        has_delivery_history = facts["history_at"] is not None
        has_label_only_history = (
            not has_delivery_history and facts["label_job_at"] is not None
        )
        _discard_delivery_pick_task(
            db,
            delivery_id=delivery_id,
            user=user,
            reason=(
                "delivery_voided_after_cancel"
                if has_delivery_history
                else "delivery_voided_with_label_history"
                if has_label_only_history
                else "送货草稿被删除"
            ),
        )
        if facts["has_history"]:
            voided_at = _utc_now()
            voided = db.execute(
                update(Delivery)
                .where(
                    Delivery.id == delivery_id,
                    Delivery.status == "pending",
                )
                .values(
                    status="voided",
                    voided_by=user.id,
                    voided_at=voided_at,
                    ever_dispatched_at=func.coalesce(
                        Delivery.ever_dispatched_at,
                        facts["history_at"],
                    ),
                    printed_by=None,
                    printed_at=None,
                )
            )
            if voided.rowcount != 1:
                current_status = db.scalar(
                    select(Delivery.status).where(Delivery.id == delivery_id)
                )
                if current_status == "voided":
                    db.rollback()
                    return {
                        "deleted": False,
                        "voided": True,
                        "disposition": "voided",
                        "id": delivery_id,
                    }
                raise HTTPException(
                    status_code=409,
                    detail="送货单状态已变化，请刷新后重试",
                )
            _write_audit(
                db,
                user=user,
                action=(
                    "VOID_AFTER_CANCEL"
                    if has_delivery_history
                    else "VOID_WITH_LABEL_HISTORY"
                ),
                resource="Delivery",
                entity_id=delivery.id,
                details={
                    "delivery_number": delivery.delivery_number,
                    "total_quantity": delivery.total_quantity,
                    "inventory_allocation_count": len(
                        facts["inventory_allocations"]
                    ),
                    "component_allocation_count": len(
                        facts["component_allocations"]
                    ),
                    "unordered_allocation_count": len(
                        facts["unordered_allocations"]
                    ),
                    "return_receipt_status": (
                        receipt.status if receipt is not None else None
                    ),
                    "label_job_at": (
                        utc_naive_to_api(facts["label_job_at"])
                        if facts["label_job_at"] is not None
                        else None
                    ),
                },
                description=(
                    "作废已取消发货的送货单并保留库存及审计记录"
                    if has_delivery_history
                    else "作废已有产品标签历史的待发货送货单并保留审计记录"
                ),
            )
            db.commit()
            return {
                "deleted": False,
                "voided": True,
                "disposition": "voided",
                "id": delivery_id,
            }
        _write_audit(
            db,
            user=user,
            action="DELETE",
            resource="Delivery",
            entity_id=delivery.id,
            details={
                "delivery_number": delivery.delivery_number,
                "total_quantity": delivery.total_quantity,
            },
            description="删除待发货送货单",
        )
        if delivery.source_mode in {"unordered_finished", "mixed"}:
            delivery_item_ids = select(DeliveryItem.id).where(
                DeliveryItem.delivery_id == delivery.id,
                DeliveryItem.is_current.is_(True),
            )
            db.execute(
                delete(UnorderedFinishedDeliveryAllocation).where(
                    UnorderedFinishedDeliveryAllocation.delivery_item_id.in_(
                        delivery_item_ids
                    )
                )
            )
        db.delete(delivery)
        db.commit()
        return {
            "deleted": True,
            "voided": False,
            "disposition": "deleted",
            "id": delivery_id,
        }
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=(
                "送货单仍有关联业务记录，不能物理删除；"
                "请刷新后确认回单、对账、库存抵扣或组件抵扣状态"
            ),
        ) from error
    except Exception:
        db.rollback()
        raise


def _cancel_delivery(
    delivery_id: int,
    *,
    db: Session,
    user: User,
    commit: bool = True,
    revision_mode: bool = False,
) -> dict:
    from app.models.finance import ReturnReceipt, ReturnReceiptItem, StatementItem

    _delivery_for_user(db, delivery_id, user)
    cancelled_at = _utc_now()
    try:
        # 先原子抢占 dispatched -> pending 并取得写锁，再检查回单/对账。
        # 若门禁不通过，整个事务 rollback，状态仍保持 dispatched。
        claimed = db.execute(
            update(Delivery)
            .where(
                Delivery.id == delivery_id,
                Delivery.status == "dispatched",
            )
            .values(
                status="pending",
                dispatched_by=None,
                dispatched_at=None,
                printed_by=None,
                printed_at=None,
            )
        )
        if claimed.rowcount != 1:
            existing_status = db.scalar(
                select(Delivery.status).where(Delivery.id == delivery_id)
            )
            if existing_status is None:
                raise HTTPException(status_code=404, detail="送货单不存在")
            if existing_status == "voided":
                raise HTTPException(
                    status_code=409,
                    detail="送货单已作废，不能取消发货",
                )
            raise HTTPException(
                status_code=409,
                detail="送货单未确认发货，无需取消",
            )
        delivery = _delivery_or_404(db, delivery_id)
        # 门禁 1（优先）：已进入对账 -> 必须先反审核对账，本阶段不开放
        statement_linked = db.scalar(
            select(StatementItem.id)
            .join(
                ReturnReceiptItem,
                ReturnReceiptItem.id == StatementItem.return_receipt_item_id,
            )
            .join(
                ReturnReceipt,
                ReturnReceipt.id == ReturnReceiptItem.return_receipt_id,
            )
            .where(ReturnReceipt.delivery_id == delivery_id)
            .limit(1)
        )
        if statement_linked is not None:
            raise HTTPException(
                status_code=409,
                detail="送货单已进入对账，必须先反审核对账，本阶段不支持取消发货",
            )
        # 门禁 2：已有有效回单 -> 不能取消发货
        receipt = db.scalar(
            select(ReturnReceipt).where(ReturnReceipt.delivery_id == delivery_id)
        )
        if receipt is not None and receipt.status == "confirmed":
            raise HTTPException(
                status_code=409,
                detail="送货单已有回单，请先撤销回单后再取消发货",
            )
        lines = db.scalars(
            select(DeliveryItem)
            .where(
                DeliveryItem.delivery_id == delivery_id,
                DeliveryItem.is_current.is_(True),
            )
            .order_by(DeliveryItem.id)
        ).all()
        order_lines = [line for line in lines if line.source_type == "order"]
        unordered_lines = [
            line for line in lines if line.source_type == "unordered_finished"
        ]
        if unordered_lines:
            cancel_unordered_finished_dispatch(
                db,
                delivery=delivery,
                delivery_items=unordered_lines,
                operator_id=user.id,
            )
        if delivery.source_mode == "unordered_finished":
            restored_pallet_ids = restore_auto_released_pallets_after_delivery_cancel(
                db,
                delivery_id=delivery_id,
                operator_id=user.id,
            )
            # Once an unordered-stock dispatch has been reversed, its immutable
            # allocation/reversal audit must remain attached to the original
            # document.  Archive it immediately instead of presenting a
            # misleading editable pending draft that cannot safely reuse those
            # allocations.
            if not revision_mode:
                delivery.status = "voided"
                delivery.voided_by = user.id
                delivery.voided_at = cancelled_at
            _discard_delivery_pick_task(
                db,
                delivery_id=delivery_id,
                user=user,
                reason="unordered_finished_delivery_dispatch_cancelled",
            )
            if not revision_mode:
                _write_audit(
                    db,
                    user=user,
                    action="CANCEL_DISPATCH",
                    resource="Delivery",
                    entity_id=delivery_id,
                    details={
                        "source_mode": "unordered_finished",
                        "delivery_number": delivery.delivery_number,
                        "item_count": len(lines),
                        "restored_quantity": delivery.total_quantity,
                        "disposition": "voided_after_dispatch_cancel",
                        "restored_pallet_ids": restored_pallet_ids,
                    },
                    description="取消无订单成品送货、退回原库存批次并归档",
                )
            db.commit() if commit else db.flush()
            return _delivery_response(db, delivery_id)
        affected_order_ids: set[int] = set()
        for line in order_lines:
            order_item = db.get(OrderItem, line.order_item_id)
            if order_item is None:
                raise HTTPException(
                    status_code=409,
                    detail=f"订单明细{line.order_item_id}不存在，无法回滚",
                )
            order_id = order_item.order_id
            delivered_before = int(order_item.delivered_quantity or 0)
            if delivered_before < line.delivered_quantity:
                raise HTTPException(
                    status_code=409,
                    detail=f"订单明细{line.order_item_id}已送数量异常，无法回滚",
                )
            delivered_after = delivered_before - line.delivered_quantity
            operation_key = (
                f"c{delivery_id}-{cancelled_at:%Y%m%d%H%M%S%f}-i{line.id}"
            )
            from app.services.bom_subkit_delivery import reverse_delivery_subkits
            from app.services.bom_subkits import SubkitError
            try:
                reverse_delivery_subkits(db, delivery_item_id=line.id, operator_id=user.id, operation_key=operation_key)
            except SubkitError as error:
                raise HTTPException(status_code=error.status_code, detail=str(error)) from error
            if _uses_composite_inventory(db, order_item.id):
                reverse_delivery_component_allocations(
                    db,
                    delivery_item_id=line.id,
                    operator_id=user.id,
                    operation_key=operation_key,
                )
            else:
                reverse_delivery_item_inventory(
                    db,
                    delivery_item_id=line.id,
                    delivered_quantity_after_cancel=delivered_after,
                    operator_id=user.id,
                    operation_key=operation_key,
                )
            result = db.execute(
                update(OrderItem)
                .where(
                    OrderItem.id == line.order_item_id,
                    OrderItem.delivered_quantity == delivered_before,
                )
                .values(delivered_quantity=delivered_after)
            )
            if result.rowcount != 1:
                raise HTTPException(
                    status_code=409,
                    detail=f"订单明细{line.order_item_id}已送数量异常，无法回滚",
                )
            if order_id is not None:
                affected_order_ids.add(order_id)
        for order_id in affected_order_ids:
            _refresh_order_status(db, order_id)
        restored_pallet_ids = restore_auto_released_pallets_after_delivery_cancel(
            db,
            delivery_id=delivery_id,
            operator_id=user.id,
        )
        _discard_delivery_pick_task(
            db,
            delivery_id=delivery_id,
            user=user,
            reason="delivery_dispatch_cancelled",
        )
        if unordered_lines and not revision_mode:
            # Mixed documents contain immutable unordered-lot reversal history.
            # Archive the whole document after both source branches are reversed.
            delivery.status = "voided"
            delivery.voided_by = user.id
            delivery.voided_at = cancelled_at
        if not revision_mode:
            _write_audit(
                db,
                user=user,
                action="CANCEL_DISPATCH",
                resource="Delivery",
                entity_id=delivery_id,
                details={
                    "delivery_number": delivery.delivery_number,
                    "item_count": len(lines),
                    "restored_quantity": delivery.total_quantity,
                    "source_mode": delivery.source_mode,
                    "restored_pallet_ids": restored_pallet_ids,
                    "disposition": (
                        "voided_after_dispatch_cancel" if unordered_lines else "pending"
                    ),
                },
                description=(
                    "取消混合送货、回滚订单已送数量、退回原库存批次并归档"
                    if unordered_lines
                    else "取消送货单发货并回滚已送数量"
                ),
            )
        db.commit() if commit else db.flush()
        return _delivery_response(db, delivery_id)
    except HTTPException:
        db.rollback()
        raise
    except WarehouseInventoryError as error:
        db.rollback()
        raise HTTPException(
            status_code=error.status_code,
            detail=str(error),
        ) from error
    except CompositeBomWorkflowError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(error)) from error
    except Exception:
        db.rollback()
        raise


@router.put("/{delivery_id}/cancel")
def cancel_delivery(
    delivery_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    return _cancel_delivery(delivery_id, db=db, user=user)


@router.put("/{delivery_id}/printed")
def mark_delivery_printed(
    delivery_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    _delivery_for_user(db, delivery_id, user)
    printed_at = _utc_now()
    try:
        updated = db.execute(
            update(Delivery)
            .where(
                Delivery.id == delivery_id,
                Delivery.status == "dispatched",
            )
            .values(
                printed_at=printed_at,
                printed_by=user.id,
            )
        )
        if updated.rowcount != 1:
            existing_status = db.scalar(
                select(Delivery.status).where(Delivery.id == delivery_id)
            )
            if existing_status is None:
                raise HTTPException(status_code=404, detail="送货单不存在")
            if existing_status == "voided":
                raise HTTPException(
                    status_code=409,
                    detail="送货单已作废，不能打印",
                )
            raise HTTPException(status_code=409, detail="送货单尚未确认发货")
        _write_audit(
            db,
            user=user,
            action="PRINT_DELIVERY",
            resource="Delivery",
            entity_id=delivery_id,
            details={"printed_at": printed_at},
            description="打印送货单",
        )
        db.commit()
        return _delivery_response(db, delivery_id)
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise


@order_actions_router.put("/items/{item_id}/force_close")
def force_close_order_item(
    item_id: int,
    payload: ForceCloseRequest,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    reason = payload.reason or "订单未送尾数强制结案（系统记录）"
    item = db.get(OrderItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="订单明细不存在")
    _require_order_item_customer_access(db, item.id, user)
    try:
        lock_order_rows_for_production_transition(db, [item.order_id])
    except ProductionWorkflowError as error:
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    item = db.scalar(
        select(OrderItem)
        .where(OrderItem.id == item_id)
        .execution_options(populate_existing=True)
    )
    if item is None:
        raise HTTPException(status_code=409, detail="订单明细已被删除，请刷新后重试")
    if item.delivered_quantity >= item.quantity:
        raise HTTPException(status_code=409, detail="订单明细已全部发货")
    if _has_active_production_stock_reservation(db, item.id):
        raise HTTPException(
            status_code=409,
            detail="该明细仍有生产完工成品库存预占，请先完成送货或库存处理后再结案",
        )
    try:
        result = db.execute(
            update(OrderItem)
            .where(
                OrderItem.id == item_id,
                OrderItem.is_force_closed.is_(False),
                OrderItem.delivered_quantity < OrderItem.quantity,
            )
            .values(is_force_closed=True)
        )
        if result.rowcount != 1:
            raise HTTPException(status_code=409, detail="订单明细已结案")
        _refresh_order_status(db, item.order_id)
        _write_audit(
            db,
            user=user,
            action="FORCE_CLOSE_ORDER_ITEM",
            resource="OrderItem",
            entity_id=item_id,
            details={
                "reason": reason,
                "ordered_quantity": item.quantity,
                "delivered_quantity": item.delivered_quantity,
            },
            description="缺货尾数强制结案",
        )
        db.commit()
        return {
            "item_id": item_id,
            "is_force_closed": True,
            "reason": reason,
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise


@router.get("/{delivery_id}")
def get_delivery(
    delivery_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    _delivery_for_user(db, delivery_id, user)
    return _delivery_response(db, delivery_id)


@router.put("/{delivery_id}/customer-po")
def update_delivery_customer_po(
    delivery_id: int,
    payload: DeliveryCustomerPoUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    """Change document references only, without cancelling/re-dispatching stock."""
    delivery = _delivery_for_user(db, delivery_id, user)
    action = "delivery_customer_po_update"
    request_hash = _delivery_request_hash(action, {
        "delivery_id": delivery_id,
        **payload.model_dump(exclude={"idempotency_key"}),
    })
    replay, _ = _delivery_idempotency_replay(
        db, idempotency_key=payload.idempotency_key, request_hash=request_hash,
        action=action, actor=user,
    )
    if replay is not None:
        return replay
    try:
        claimed = db.execute(update(Delivery).where(
            Delivery.id == delivery_id,
            Delivery.status.in_(["pending", "dispatched"]),
            Delivery.version == payload.expected_version,
        ).values(version=Delivery.version + 1))
        if claimed.rowcount != 1:
            raise HTTPException(status_code=409, detail="送货单已作废或版本已变化，请刷新后重试")
        rows = {row.id: row for row in db.scalars(select(DeliveryItem).where(
            DeliveryItem.delivery_id == delivery_id, DeliveryItem.is_current.is_(True)
        ))}
        ids = [line.delivery_item_id for line in payload.items]
        if len(set(ids)) != len(ids) or any(item_id not in rows for item_id in ids):
            raise HTTPException(status_code=409, detail="送货明细不属于当前送货单或已变化")
        changes = []
        for line in payload.items:
            row = rows[line.delivery_item_id]
            value = line.customer_po.strip()
            original_po = db.scalar(select(Order.customer_po).join(
                OrderItem, OrderItem.order_id == Order.id
            ).where(OrderItem.id == row.order_item_id)) if row.order_item_id else None
            changes.append({"delivery_item_id": row.id,
                            "before": row.customer_po_snapshot if row.customer_po_snapshot is not None else original_po,
                            "after": value})
            row.customer_po_snapshot = value
        _write_audit(db, user=user, action="UPDATE_CUSTOMER_PO", resource="Delivery",
                     entity_id=delivery_id, details={"changes": changes,
                     "before_version": payload.expected_version,
                     "after_version": payload.expected_version + 1},
                     description="修改送货单客户单号，不回写上游订单")
        db.flush()
        db.expire(delivery)
        response = _delivery_response(db, delivery_id)
        _record_delivery_idempotency(db, idempotency_key=payload.idempotency_key,
            request_hash=request_hash, action=action, actor=user,
            delivery_id=delivery_id, response=response)
        db.commit()
        return response
    except Exception:
        db.rollback()
        raise


@router.get("/{delivery_id}/print")
def get_delivery_print_data(
    delivery_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    delivery = _delivery_for_user(db, delivery_id, user)
    if delivery.status == "voided":
        raise HTTPException(
            status_code=409,
            detail="送货单已作废，不能打印",
        )
    customer = db.get(Customer, delivery.customer_id)
    company = db.scalar(select(CompanyConfig).where(CompanyConfig.id == 1))
    rows = db.execute(
        select(
            DeliveryItem.id.label("delivery_item_id"),
            DeliveryItem.source_type,
            DeliveryItem.order_item_id,
            func.coalesce(DeliveryItem.customer_po_snapshot, case(
                (DeliveryItem.source_type == "unordered_finished", "无订单库存"),
                else_=Order.customer_po,
            )).label("customer_po"),
            func.coalesce(
                DeliveryItem.product_code_snapshot,
                OrderItem.snapshot_product_code,
            ).label("product_code"),
            func.coalesce(
                DeliveryItem.product_name_snapshot,
                OrderItem.snapshot_product_name,
            ).label("product_name"),
            DeliveryItem.specification_snapshot.label(
                "delivery_specification_snapshot"
            ),
            OrderItem.snapshot_spec.label("order_specification_snapshot"),
            Product.length_mm.label("product_length_mm"),
            Product.width_mm.label("product_width_mm"),
            Product.height_mm.label("product_height_mm"),
            Product.composite_fulfillment_mode.label(
                "current_product_fulfillment_mode"
            ),
            DeliveryItem.unit_snapshot,
            DeliveryItem.delivered_quantity.label("quantity"),
            DeliveryItem.ordered_quantity_snapshot,
            DeliveryItem.over_delivery_quantity,
            DeliveryItem.remarks,
        )
        .outerjoin(OrderItem, OrderItem.id == DeliveryItem.order_item_id)
        .outerjoin(Order, Order.id == OrderItem.order_id)
        .outerjoin(
            Product,
            Product.id
            == func.coalesce(DeliveryItem.product_id, OrderItem.product_id),
        )
        .where(
            DeliveryItem.delivery_id == delivery_id,
            DeliveryItem.is_current.is_(True),
        )
        .order_by(DeliveryItem.id)
    ).all()
    internal_remarks = _tianhua_internal_remarks_by_delivery_item(
        db,
        [
            row.delivery_item_id
            for row in rows
            if str(row.remarks or "").strip().startswith(
                "来源：天华预送货草稿 "
            )
        ],
    )
    print_items: list[dict] = []
    actual_goods_items: list[dict] = []
    for row in rows:
        product_code = (
            str(row.product_code or "").strip() or "存货编码未登记"
        )
        product_name = (
            str(row.product_name or "").strip() or "产品名称未登记"
        )
        specification = resolved_product_specification(
            row.delivery_specification_snapshot,
            fallback_snapshots=(row.order_specification_snapshot,),
            customer_name_snapshots=(product_name,),
            length_mm=row.product_length_mm,
            width_mm=row.product_width_mm,
            height_mm=row.product_height_mm,
        ) or ""
        is_unordered = row.source_type == "unordered_finished"
        order_item = (
            db.get(OrderItem, row.order_item_id)
            if row.order_item_id is not None
            else None
        )
        kit_metadata = _delivery_kit_metadata(
            db,
            order_item,
            planned_delivery_quantity=row.quantity,
            delivery_item_id=row.delivery_item_id,
            dispatched=delivery.status == "dispatched",
            current_product_fulfillment_mode=row.current_product_fulfillment_mode,
        )
        actual_goods_lines = (
            [
                {
                    "line_type": "parent",
                    "order_item_id": None,
                    "component_snapshot_id": None,
                    "product_code": product_code,
                    "product_name": product_name,
                    "specification": specification,
                    "unit": row.unit_snapshot or "PCS",
                    "quantity": int(row.quantity or 0),
                    "pricing_included": True,
                    "independent_return_receipt": True,
                    "independent_statement": True,
                }
            ]
            if is_unordered
            else _delivery_document_goods_lines(
                order_item_id=row.order_item_id,
                product_code=_print_product_code(product_code),
                product_name=product_name,
                specification=specification,
                parent_quantity=row.quantity,
                kit_metadata=kit_metadata,
            )
        )
        document_goods_lines = [
            {
                **line,
                "delivery_item_id": row.delivery_item_id,
                "customer_po": row.customer_po,
                "product_code": _print_product_code(line["product_code"]),
            }
            for line in actual_goods_lines
        ]
        actual_goods_items.extend(document_goods_lines)
        print_items.append(
            {
                "delivery_item_id": row.delivery_item_id,
                "order_item_id": row.order_item_id,
                "customer_po": row.customer_po,
                "product_code": _print_product_code(product_code),
                "product_name": product_name,
                "specification": specification,
                "unit": row.unit_snapshot or "PCS",
                "quantity": row.quantity,
                "ordered_quantity": row.ordered_quantity_snapshot,
                "over_delivery_quantity": row.over_delivery_quantity,
                "remarks": _customer_visible_delivery_remark(
                    row.delivery_item_id,
                    row.remarks,
                    internal_remarks,
                ),
                "component_lines": kit_metadata["component_lines"],
                "actual_goods_lines": document_goods_lines,
                "actual_goods_quantity": sum(
                    int(line["quantity"] or 0) for line in document_goods_lines
                ),
            }
        )
    return {
        "id": delivery.id,
        "delivery_number": delivery.delivery_number,
        "delivery_date": delivery.delivery_date,
        "vehicle_number": delivery.vehicle_number,
        "status": delivery.status,
        "total_quantity": delivery.total_quantity,
        "total_actual_goods_quantity": sum(
            int(line["quantity"] or 0) for line in actual_goods_items
        ),
        "actual_goods_items": actual_goods_items,
        "created_at": utc_naive_to_api(delivery.created_at),
        "customer": {
            "name": customer.name if customer else "",
            "contact_person": customer.contact_person if customer else None,
            "phone": customer.phone if customer else None,
            "address": customer.address if customer else None,
        },
        "sender": {
            "company_name": company.company_name if company else "",
            "address": company.address if company else None,
            "phone": company.phone if company else None,
            "fax": company.fax if company else None,
            "tax_number": company.tax_number if company else None,
            "bank_name": company.bank_name if company else None,
            "bank_account": company.bank_account if company else None,
            "contact_person": company.contact_person if company else None,
            "contact_phone": company.contact_phone if company else None,
        },
        "items": print_items,
    }
