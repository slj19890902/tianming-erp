from __future__ import annotations

from collections import Counter
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo
from typing import Literal
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import and_, case, exists, func, or_, select
from sqlalchemy.orm import Session, aliased, load_only, selectinload, with_loader_criteria

from app.api.deps import (
    PermissionChecker,
    customer_scope_ids,
    get_db,
    get_current_user,
    has_permission,
    has_unrestricted_customer_access,
)
from app.core.time_contract import beijing_today, utc_naive_to_beijing_date
from app.models.delivery import Delivery, DeliveryItem
from app.models.customer import Customer
from app.models.finance import (
    ReturnReceipt,
    ReturnReceiptItem,
    Statement,
    StatementItem,
)
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.production import ProductionTask
from app.models.requisition import RequisitionHold, RequisitionItem
from app.models.stock_replenishment import InventoryStockPolicy
from app.models.user import User
from app.models.warehouse_inventory import InventoryReservation, OrderItemSemiRequirement
from app.services.inventory_insights import build_inventory_insights
from app.services.dashboard_metric_contracts import (
    build_authoritative_snapshot,
    financial_balance_rows,
)
from app.services.order_business_status import build_order_business_statuses
from app.services.stock_replenishment import (
    product_replenishment_defaults,
    product_replenishment_signature,
    stock_policy_dict,
    virtual_composite_replenishment_components,
)
from app.services.external_packaging_purchase import ExternalPurchaseContractError
from app.services.external_packaging_stock_replenishment import external_stock_draft
from app.services.location_candidates import (
    load_warehouse_location_projection_contexts,
)
from app.services.customer_delivery_margin import build_customer_delivery_margin
from app.services.inventory_read_scope import inventory_summary_read_scope


router = APIRouter()
can_read = PermissionChecker("dashboard.view")
RECONCILIATION_REMINDER_START_DAY = 18


class HomeTaskPreferenceRequest(BaseModel):
    task_id: str = Field(min_length=1, max_length=180)
    source_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    scope: Literal["personal", "team"] = "personal"
    state: str = Field(max_length=30)
    reason: str = Field(default="", max_length=40)
    remind_on: date | None = None
    expected_version: int = Field(ge=0)
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_-]{8,120}$")


@router.get("/stock-advice/{policy_id}")
def stock_advice(policy_id: int, db: Session = Depends(get_db), user: User = Depends(can_read)):
    if not all(has_permission(user, p) for p in ("warehouse.view", "requisition.view")):
        raise HTTPException(403, "需要库存及报料查看权限")
    from app.services.home_stock_advice import build_stock_advice
    visible = None if has_unrestricted_customer_access(user, db) else customer_scope_ids(user, db)
    warning = next((w for w in _common_box_low_stock_warnings(db, visible) if w['policy_id'] == policy_id), None)
    if warning is None:
        raise HTTPException(404, "预警已解除或不在当前可见范围")
    return build_stock_advice(db, warning=warning, today=beijing_today())


@router.get("/stock-warnings")
def stock_warnings(db: Session = Depends(get_db), user: User = Depends(can_read)):
    """Refresh the existing stock panel without recomputing unrelated queues."""
    if not all(has_permission(user, p) for p in ("warehouse.view", "requisition.view")):
        raise HTTPException(403, "需要库存及报料查看权限")
    visible = None if has_unrestricted_customer_access(user, db) else customer_scope_ids(user, db)
    from app.services.dashboard_workbench import decorate_stock_warnings
    return {'items': decorate_stock_warnings(db, user=user,
        warnings=_common_box_low_stock_warnings(db, visible), visible_customer_ids=visible)}


@router.post("/task-preferences")
def update_home_task_preference(payload: HomeTaskPreferenceRequest,
    db: Session = Depends(get_db), user: User = Depends(can_read)):
    from app.services.home_task_preferences import replay_mutation, save_preference
    values = payload.model_dump()
    visible = None if has_unrestricted_customer_access(user, db) else customer_scope_ids(user, db)
    try:
        replay = replay_mutation(db, user=user, payload=values, visible_customer_ids=visible)
        if replay is not None:
            return replay
        overview = dashboard_overview(db=db, user=user)
        task = next((t for t in overview["workbench"]["tasks"] if t["id"] == payload.task_id), None)
        if task is None:
            raise HTTPException(404, "事项已完成或不在当前可见范围，请刷新首页")
        result = save_preference(db, user=user, payload=values, task=task, today=beijing_today())
        db.commit()
        return result
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(409, "提醒已被更新，请刷新后重试") from error
    except Exception:
        db.rollback()
        raise


def _customer_delivery_margin_access(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> User:
    required = ("dashboard.view", "finance.view", "cost.view")
    if current_user.role not in {"admin", "boss"} or not all(
        has_permission(current_user, permission) for permission in required
    ):
        raise HTTPException(status_code=403, detail="权限不足")
    return current_user


def _money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.00"))


def _safe_int(value) -> int:
    return int(value or 0)


def _business_date_string(value: date | datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return utc_naive_to_beijing_date(value).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _coalesce_date(value: str | None) -> str:
    return value or date.max.isoformat()


def _delivery_ready_filter():
    task_exists = exists(
        select(ProductionTask.id).where(
            ProductionTask.order_item_id == OrderItem.id,
        )
    )
    task_ready = exists(
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
                        InventoryReservation.credited_requirement_quantity,
                        0,
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
    active_semi_for_requirement = (
        select(
            func.coalesce(
                func.sum(
                    func.coalesce(
                        InventoryReservation.credited_requirement_quantity,
                        0,
                    )
                    - InventoryReservation.released_requirement_quantity
                ),
                0,
            )
        )
        .where(
            InventoryReservation.semi_requirement_id == OrderItemSemiRequirement.id,
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
    return or_(
        task_ready,
        and_(
            ~task_exists,
            or_(
                OrderItem.material_status == "received",
                active_finished_reserved >= OrderItem.quantity,
                semi_fully_covered,
                received_telescoping_components > 0,
            ),
        ),
    )


def _customer_scope_criteria(customer_id_column, visible_customer_ids: set[int] | None):
    if visible_customer_ids is None:
        return ()
    return (customer_id_column.in_(visible_customer_ids),)


def _workflow_projection_rows(
    db: Session,
    *,
    visible_customer_ids: set[int] | None,
    due_on: date | None = None,
    due_through: date | None = None,
    include_delivery: bool = True,
    include_finance: bool = True,
) -> list[dict]:
    """Build dashboard rows from the same projection returned by order APIs."""

    if visible_customer_ids is not None and not visible_customer_ids:
        return []
    query = (
        select(Order)
        .options(
            load_only(
                Order.id,
                Order.order_number,
                Order.customer_id,
                Order.delivery_date,
                Order.created_at,
                Order.status,
            ),
            selectinload(Order.items).load_only(
                OrderItem.id,
                OrderItem.order_id,
                OrderItem.product_id,
                OrderItem.item_sequence,
                OrderItem.quantity,
                OrderItem.delivered_quantity,
                OrderItem.is_force_closed,
                OrderItem.material_status,
                OrderItem.snapshot_product_code,
                OrderItem.supply_mode_snapshot,
                OrderItem.composite_fulfillment_mode_snapshot,
            ).selectinload(OrderItem.product),
        )
        .where(
            Order.status.notin_(["cancelled", "dead", "closed", "archived"]),
        )
    )
    if visible_customer_ids is not None:
        query = query.where(Order.customer_id.in_(visible_customer_ids))
    if due_on is not None:
        query = query.where(Order.delivery_date == due_on)
    if due_through is not None:
        query = query.where(
            or_(Order.delivery_date.is_(None), Order.delivery_date <= due_through)
        )
    orders = list(db.scalars(query).all())
    projections = build_order_business_statuses(
        db,
        orders,
        include_delivery=include_delivery,
        include_finance=include_finance,
    )
    customer_ids = {int(order.customer_id) for order in orders}
    customer_names = (
        {
            int(customer.id): customer.name
            for customer in db.scalars(
                select(Customer).where(Customer.id.in_(customer_ids))
            ).all()
        }
        if customer_ids
        else {}
    )
    rows: list[dict] = []
    for order in orders:
        projection = projections.get(int(order.id), {})
        for item in order.items:
            item_projection = projection.get("items", {}).get(int(item.id), {})
            item_business_status = item_projection.get(
                "business_status", "pending_material"
            )
            # A permission-restricted dashboard must not query delivery facts.
            # When the persisted compatibility snapshot already records a
            # delivery-or-later stage, fail closed instead of falsely exposing
            # the line as an earlier production task. Full order APIs still use
            # the formal delivery projection.
            if (
                not include_delivery
                and order.status
                in {
                    "pending_delivery",
                    "partially_delivered",
                    "delivered",
                    "waiting_receipt",
                    "pending_reconciliation",
                    "pending_invoice",
                    "pending_payment",
                    "completed",
                }
                and item_business_status
                in {
                    "pending_material",
                    "pending_incoming",
                    "pending_production",
                }
            ):
                item_business_status = "pending_delivery"
            rows.append(
                {
                    "order_id": int(order.id),
                    "order_number": order.order_number,
                    "customer_id": int(order.customer_id),
                    "customer_name": customer_names.get(
                        int(order.customer_id), "-"
                    ),
                    "delivery_date": order.delivery_date,
                    "created_at": order.created_at,
                    "item_id": int(item.id),
                    "item_sequence": item.item_sequence,
                    "product_code": item.snapshot_product_code,
                    "business_status": item_business_status,
                }
            )
    return rows


class _CustomerScopedSession:
    def __init__(self, db: Session, visible_customer_ids: set[int] | None) -> None:
        self._db = db
        self._visible_customer_ids = visible_customer_ids

    def _scope(self, statement):
        if self._visible_customer_ids is None:
            return statement
        visible_customer_ids = tuple(self._visible_customer_ids)
        return statement.options(
            with_loader_criteria(
                Order,
                Order.customer_id.in_(visible_customer_ids),
                include_aliases=True,
            ),
            with_loader_criteria(
                Delivery,
                Delivery.customer_id.in_(visible_customer_ids),
                include_aliases=True,
            ),
            with_loader_criteria(
                Statement,
                Statement.customer_id.in_(visible_customer_ids),
                include_aliases=True,
            ),
        )

    def scalar(self, statement):
        return self._db.scalar(self._scope(statement))

    def execute(self, statement):
        return self._db.execute(self._scope(statement))


def _todo_sort_key(todo: dict) -> tuple:
    priority_map = {
        "待对账": 10,
        "待开票": 15,
        "待结款": 20,
        "待回单": 30,
        "待送货": 40,
        "待生产": 45,
        "待入库": 50,
        "待报料": 60,
    }
    return (
        priority_map.get(todo.get("type"), 99),
        _coalesce_date(todo.get("sort_date")),
        -(todo.get("amount") or 0),
        todo.get("customer_name") or "",
    )


def _common_box_low_stock_warnings(
    db: Session,
    visible_customer_ids: set[int] | None,
) -> list[dict]:
    with inventory_summary_read_scope(db):
        return _common_box_low_stock_warnings_in_scope(db, visible_customer_ids)


def _common_box_low_stock_warnings_in_scope(
    db: Session,
    visible_customer_ids: set[int] | None,
) -> list[dict]:
    query = (
        select(InventoryStockPolicy)
        .join(Product, Product.id == InventoryStockPolicy.product_id)
        .options(
            selectinload(InventoryStockPolicy.product).selectinload(
                Product.material
            ),
            selectinload(InventoryStockPolicy.customer),
            selectinload(InventoryStockPolicy.default_location),
        )
        .where(
            InventoryStockPolicy.active.is_(True),
            InventoryStockPolicy.target_inventory_type == "finished",
            Product.is_active.is_(True),
            Product.deleted_at.is_(None),
            Product.customer_id == InventoryStockPolicy.customer_id,
        )
        .order_by(InventoryStockPolicy.id)
    )
    if visible_customer_ids is not None:
        query = query.where(Product.customer_id.in_(visible_customer_ids))
    policies = list(db.scalars(query).all())
    projection_contexts = load_warehouse_location_projection_contexts(
        db,
        [
            policy.default_location
            for policy in policies
            if policy.default_location is not None
        ],
    )
    warnings = []
    for policy in policies:
        item = stock_policy_dict(
            db,
            policy,
            projection_context=(
                projection_contexts.get(int(policy.default_location.id))
                if policy.default_location is not None
                else None
            ),
        )
        if not item["warning_triggered"]:
            continue
        product = policy.product
        if product is None:
            continue
        defaults = product_replenishment_defaults(product)
        composite_defaults = virtual_composite_replenishment_components(
            db,
            product=product,
        )
        if composite_defaults is not None:
            defaults = {
                **defaults,
                "material_supplier_name": (
                    composite_defaults["supplier_name"] or policy.supplier_name
                ),
                "draft_ready": composite_defaults["draft_ready"],
                "missing_fields": composite_defaults["missing_fields"],
            }
        external_defaults: dict = {}
        if product.supply_mode == "external_purchase":
            try:
                external_defaults = external_stock_draft(
                    db,
                    policy=policy,
                    finished_quantity=max(
                        int(item["suggested_replenishment_quantity"] or 0), 1
                    ),
                )
                defaults = {
                    **defaults,
                    "draft_ready": True,
                    "missing_fields": [],
                }
            except ExternalPurchaseContractError as error:
                external_defaults = {
                    "procurement_mode": "external_purchase",
                    "draft_ready": False,
                    "missing_fields": [str(error)],
                }
                defaults = {
                    **defaults,
                    "draft_ready": False,
                    "missing_fields": [str(error)],
                }
        signature = product_replenishment_signature(product)
        warnings.append(
            {
                "policy_id": item["id"],
                "product_id": item["product_id"],
                "customer_id": item["customer_id"],
                "customer_name": item["customer_name"],
                "product_code": item["product_code"],
                "product_name": item["product_name"],
                "unit_label": item.get("unit_label") or "单位待核",
                "available_quantity": item["available_quantity"],
                "physical_unconsumed_quantity": item.get("physical_unconsumed_quantity"),
                "is_virtual_composite_parent": item["is_virtual_composite_parent"],
                "assembled_quantity": item["assembled_quantity"],
                "unassembled_available_set_quantity": item["unassembled_available_set_quantity"],
                "reserved_component_set_quantity": item["reserved_component_set_quantity"],
                "allocatable_available_quantity": item[
                    "allocatable_available_quantity"
                ],
                "reserved_quantity": item["reserved_quantity"],
                "warning_quantity": item["warning_quantity"],
                "target_quantity": item["target_quantity"],
                "suggested_replenishment_quantity": item[
                    "suggested_replenishment_quantity"
                ],
                "processed_component_available_piece_quantity": item[
                    "processed_component_available_piece_quantity"
                ],
                "processed_component_identity_review_piece_quantity": item[
                    "processed_component_identity_review_piece_quantity"
                ],
                "customer_board_preparation_available_sheet_quantity": item[
                    "customer_board_preparation_available_sheet_quantity"
                ],
                "customer_board_preparation_finished_capacity": item[
                    "customer_board_preparation_finished_capacity"
                ],
                "customer_board_preparation_auto_cover_capacity": item[
                    "customer_board_preparation_auto_cover_capacity"
                ],
                "incoming_board_preparation_sheet_quantity": item[
                    "incoming_board_preparation_sheet_quantity"
                ],
                "incoming_board_preparation_finished_capacity": item[
                    "incoming_board_preparation_finished_capacity"
                ],
                "incoming_board_preparation_auto_cover_capacity": item[
                    "incoming_board_preparation_auto_cover_capacity"
                ],
                "suggested_new_requisition_finished_quantity": item[
                    "suggested_new_requisition_finished_quantity"
                ],
                "suggested_new_requisition_sheet_quantity": item[
                    "suggested_new_requisition_sheet_quantity"
                ],
                "replenishment_state": item["replenishment_state"],
                "material_code": defaults["material_code"],
                "supplier_name": defaults["material_supplier_name"],
                "layer_count": defaults["layer_count"],
                "flute_type": defaults["flute_type"],
                "report_length_mm": defaults["report_length_mm"],
                "report_width_mm": defaults["report_width_mm"],
                "crease_type": defaults["crease_type"],
                "cutting_mode": defaults["cutting_mode"],
                "output_per_sheet": defaults["output_per_sheet"],
                "theoretical_requisition_quantity": (
                    item["suggested_new_requisition_sheet_quantity"]
                ),
                "draft_ready": defaults["draft_ready"],
                "missing_fields": defaults["missing_fields"],
                "procurement_mode": external_defaults.get(
                    "procurement_mode", "corrugated_board"
                ),
                "external_purchase_quantity": (
                    external_defaults.get("item", {}).get(
                        "external_purchase_quantity"
                    )
                ),
                "external_purchase_unit": (
                    external_defaults.get("item", {}).get(
                        "external_purchase_unit"
                    )
                ),
                "_replenishment_signature": signature,
            }
        )
    signature_counts: dict[tuple, int] = {}
    for item in warnings:
        signature = item["_replenishment_signature"]
        if signature is not None:
            signature_counts[signature] = signature_counts.get(signature, 0) + 1
    for item in warnings:
        signature = item.pop("_replenishment_signature")
        item["same_spec_warning_count"] = (
            max(signature_counts.get(signature, 0) - 1, 0)
            if signature is not None
            else 0
        )
    return sorted(
        warnings,
        key=lambda item: (
            -(item["warning_quantity"] - item["available_quantity"]),
            item["customer_name"] or "",
            item["product_code"] or "",
        ),
    )


def _dashboard_kpi_payload(
    raw_db: Session,
    *,
    user: User,
    visible_customer_ids: set[int] | None,
    today: date,
    workflow_rows: list[dict] | None = None,
) -> dict:
    """Return KPI values, optionally reusing the overview workflow projection."""

    month = today.strftime("%Y-%m")
    can_view_incoming = has_permission(user, "incoming.view")
    can_view_orders = has_permission(user, "orders.view")
    can_view_deliveries = has_permission(user, "deliveries.view")
    can_view_finance = has_permission(user, "finance.view")
    db = _CustomerScopedSession(raw_db, visible_customer_ids)
    can_view_cost = can_view_finance and has_permission(user, "cost.view")
    result = {"month": month}
    if workflow_rows is None:
        current_workflow_rows = (
            _workflow_projection_rows(
                raw_db,
                visible_customer_ids=visible_customer_ids,
                due_on=today,
                include_delivery=can_view_deliveries or can_view_finance,
                include_finance=can_view_finance,
            )
            if can_view_deliveries or can_view_orders or can_view_incoming
            else []
        )
    else:
        current_workflow_rows = [
            row for row in workflow_rows if row.get("delivery_date") == today
        ]
    if can_view_deliveries:
        result["today_pending_delivery_tasks"] = sum(
            1
            for row in current_workflow_rows
            if row["business_status"] in {"pending_delivery", "partially_delivered"}
        )
    if can_view_orders:
        result["today_pending_production_tasks"] = sum(
            1
            for row in current_workflow_rows
            if row["business_status"] == "pending_production"
        )
    if can_view_incoming:
        result["today_pending_incoming_tasks"] = sum(
            1
            for row in current_workflow_rows
            if row["business_status"] == "pending_incoming"
        )
    if can_view_finance:
        monthly_revenue = db.scalar(
            select(
                func.coalesce(
                    func.sum(
                        ReturnReceiptItem.actual_received_quantity
                        * case(
                            (
                                DeliveryItem.source_type == "unordered_finished",
                                DeliveryItem.unit_price_snapshot,
                            ),
                            else_=OrderItem.unit_price,
                        )
                    ),
                    0,
                )
            )
            .select_from(ReturnReceiptItem)
            .join(
                ReturnReceipt,
                ReturnReceipt.id == ReturnReceiptItem.return_receipt_id,
            )
            .join(DeliveryItem, DeliveryItem.id == ReturnReceiptItem.delivery_item_id)
            .join(Delivery, Delivery.id == DeliveryItem.delivery_id)
            .outerjoin(OrderItem, OrderItem.id == DeliveryItem.order_item_id)
            .where(
                ReturnReceipt.status == "confirmed",
                func.strftime(
                    "%Y-%m",
                    ReturnReceipt.actual_received_date,
                )
                == month,
                *_customer_scope_criteria(Delivery.customer_id, visible_customer_ids),
            )
        )
        outstanding = db.scalar(
            select(
                func.coalesce(
                    func.sum(
                        Statement.total_receivable - Statement.settled_amount
                    ),
                    0,
                )
            ).where(
                Statement.status == "unsettled",
                *_customer_scope_criteria(Statement.customer_id, visible_customer_ids),
            )
        )
        result.update(
            {
                "monthly_revenue": _money(monthly_revenue),
                "outstanding_receivables": _money(outstanding),
            }
        )
    if can_view_cost:
        monthly_profit = db.scalar(
            select(func.coalesce(func.sum(StatementItem.gross_profit_amount), 0))
            .select_from(StatementItem)
            .join(Statement, Statement.id == StatementItem.statement_id)
            .where(
                Statement.statement_month == month,
                *_customer_scope_criteria(Statement.customer_id, visible_customer_ids),
            )
        )
        result["monthly_gross_profit"] = _money(monthly_profit)
    return result


@router.get("/kpi")
def dashboard_kpi(
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    today = beijing_today()
    visible_customer_ids = (
        None
        if has_unrestricted_customer_access(user, db)
        else customer_scope_ids(user, db)
    )
    return _dashboard_kpi_payload(
        db,
        user=user,
        visible_customer_ids=visible_customer_ids,
        today=today,
    )


@router.get("/customer-delivery-margin")
def customer_delivery_margin(
    customer_id: int | None = Query(default=None, gt=0),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    gap_page: int = Query(default=1, ge=1),
    gap_page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(_customer_delivery_margin_access),
) -> dict:
    """Read-only dispatched-line sales/material margin projection."""
    if (date_from is None) != (date_to is None):
        raise HTTPException(status_code=422, detail="date_from 和 date_to 必须同时填写")
    if date_from is None:
        today = beijing_today()
        date_from = today.replace(day=1)
        date_to = today
    if date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from 不能晚于 date_to")
    if date_to == date.max:
        raise HTTPException(status_code=422, detail="date_to 超出可查询范围")
    visible_customer_ids = (
        None
        if has_unrestricted_customer_access(user, db)
        else customer_scope_ids(user, db)
    )
    if customer_id is not None and visible_customer_ids is not None and customer_id not in visible_customer_ids:
        raise HTTPException(status_code=403, detail="无权查看该客户")
    payload = build_customer_delivery_margin(
        db,
        date_from=date_from,
        date_to=date_to,
        customer_id=customer_id,
        visible_customer_ids=visible_customer_ids,
        page=page,
        page_size=page_size,
        gap_page=gap_page,
        gap_page_size=gap_page_size,
    )
    payload["as_of"] = datetime.now(_BEIJING).replace(microsecond=0).isoformat()
    return payload


_BEIJING = ZoneInfo("Asia/Shanghai")


def _dashboard_target_filter(metric_key: str, statement_month: str) -> dict:
    filters = {
        "pending_material": {"tab": "pending"},
        "pending_incoming": {"tab": "pending"},
        "pending_production": {"tab": "pending"},
        "pending_delivery": {"mode": "pending_customers"},
        "pending_receipt": {
            "status": "dispatched",
            "return_status": "waiting_receipt",
        },
        "pending_reconciliation": {
            "mode": "pending_reconciliation",
            "statement_month": statement_month,
        },
        "pending_invoice": {
            "balance_type": "pending_invoice",
            "statement_month": statement_month,
        },
        "pending_payment": {
            "balance_type": "pending_payment",
            "statement_month": "",
            "all_open": True,
        },
    }
    return dict(filters[metric_key])


def _authoritative_dashboard_data(
    db: Session,
    *,
    user: User,
    visible_customer_ids: set[int] | None,
    statement_month: str,
    as_of: str,
    can_view_requisition: bool,
    can_view_incoming: bool,
    can_view_orders: bool,
    can_view_deliveries: bool,
    can_view_finance: bool,
    show_reconciliation_reminder: bool,
) -> dict:
    # Import the shared authoritative projections lazily.  The dashboard keeps
    # the exact page eligibility rules and stable identities without building
    # each page's full, display-heavy payload merely to count pending work.
    from app.api.deliveries import pending_delivery_customer_summaries
    from app.api.finance import pending_statement_customer_summaries, project_customer_months
    from app.api.incoming import dashboard_pending_incoming_rows
    from app.api.requisition import dashboard_pending_requisition_rows
    from app.services.production_workflow import (
        list_production_task_dashboard_rows,
    )

    # An explicitly empty customer scope cannot contain actionable work.
    # Avoid invoking eligibility helpers that inspect global inventory before
    # their final customer filter; None continues to mean unrestricted scope.
    has_customer_scope = visible_customer_ids is None or bool(visible_customer_ids)
    pending_material_rows = (
        dashboard_pending_requisition_rows(db=db, user=user, include_workbench=True)
        if can_view_requisition and has_customer_scope
        else []
    )
    pending_incoming_rows = (
        dashboard_pending_incoming_rows(db=db, user=user, include_workbench=True)
        if can_view_incoming and has_customer_scope
        else []
    )
    pending_production_rows = (
        list_production_task_dashboard_rows(
            db,
            allowed_customer_ids=visible_customer_ids,
            status="pending",
        )
        if can_view_orders and has_customer_scope
        else []
    )
    pending_delivery_rows = (
        pending_delivery_customer_summaries(db=db, user=user, include_workbench=True)
        if can_view_deliveries and has_customer_scope
        else []
    )

    pending_receipt_rows: list[dict] = []
    if can_view_deliveries and has_customer_scope:
        receipt_query = (
            select(
                Delivery.id.label("delivery_id"),
                Delivery.delivery_number,
                Delivery.customer_id,
                Customer.name.label("customer_name"),
                Delivery.delivery_date,
            )
            .join(Customer, Customer.id == Delivery.customer_id)
            .where(
                Delivery.status == "dispatched",
                ~exists(
                    select(ReturnReceipt.id).where(
                        ReturnReceipt.delivery_id == Delivery.id,
                        ReturnReceipt.status == "confirmed",
                    )
                ),
            )
            .order_by(Delivery.delivery_date, Delivery.id)
        )
        if visible_customer_ids is not None:
            receipt_query = receipt_query.where(
                Delivery.customer_id.in_(visible_customer_ids)
            )
        pending_receipt_rows = [
            dict(row) for row in db.execute(receipt_query).mappings().all()
        ]

    pending_reconciliation_rows = (
        pending_statement_customer_summaries(
            db,
            statement_month=statement_month,
            visible_customer_ids=visible_customer_ids,
        )
        if can_view_finance and show_reconciliation_reminder and has_customer_scope
        else []
    )
    statement_rows: list[dict] = []
    if can_view_finance and has_customer_scope:
        statement_query = (
            select(Statement, Customer.name.label("customer_name"))
            .join(Customer, Customer.id == Statement.customer_id)
            .where(Statement.statement_month == statement_month,
                   Statement.confirmation_status == "confirmed")
            .order_by(Statement.id)
        )
        if visible_customer_ids is not None:
            statement_query = statement_query.where(
                Statement.customer_id.in_(visible_customer_ids)
            )
        statement_rows = [
            {
                "statement_id": statement.id,
                "customer_id": statement.customer_id,
                "customer_name": customer_name,
                "statement_month": statement.statement_month,
                "status": statement.status,
                "total_receivable": statement.total_receivable,
                "invoiced_amount": statement.invoiced_amount,
                "settled_amount": statement.settled_amount,
            }
            for statement, customer_name in db.execute(statement_query).all()
        ]

    snapshot = build_authoritative_snapshot(
        pending_material_rows=pending_material_rows,
        pending_incoming_rows=pending_incoming_rows,
        pending_production_rows=pending_production_rows,
        pending_delivery_rows=pending_delivery_rows,
        pending_receipt_rows=pending_receipt_rows,
        pending_reconciliation_rows=pending_reconciliation_rows,
        statement_rows=statement_rows,
        statement_month=statement_month,
        as_of=as_of,
    )
    invoice_rows, payment_rows = financial_balance_rows(statement_rows)
    customer_names = {
        int(row["customer_id"]): str(row.get("customer_name") or "未命名客户")
        for row in statement_rows
    }
    for rows in (invoice_rows, payment_rows):
        for row in rows:
            row["customer_name"] = customer_names.get(
                int(row["customer_id"]), "未命名客户"
            )
            row["statement_month"] = statement_month

    # Use the same eligibility, frozen settlement party and source-customer
    # scope as the collections workbench. No month or pagination truncation.
    payment_rows = []
    if can_view_finance and has_customer_scope:
        finance = project_customer_months(
            db=db, user=user, all_open=True, workspace="collections",
            balance_type="pending_payment", paginate=False,
        )
        for group in finance["items"]:
            identity = (f"entity:{group['settlement_entity_id']}" if group.get("settlement_entity_id")
                        else f"customer:{group['customer_id']}")
            payment_rows.append({
                "customer_id": group["customer_id"], "customer_name": group["customer_name"],
                "_workbench_customer_ids": group["source_customer_ids"],
                "settlement_identity": identity, "statement_month": group["statement_month"],
                "amount": group["pending_payment_action_amount"],
                "source_basis": [(s["id"], str(s.get("pending_payment_action_amount")),
                                  s.get("version"), s.get("ledger_version")) for s in group["statements"]],
            })
    payment_metric = snapshot["metrics"]["pending_payment"]
    payment_metric.update(
        identities=sorted({r["settlement_identity"] for r in payment_rows}),
        count=len({r["settlement_identity"] for r in payment_rows}),
        amount=str(_money(sum((r["amount"] for r in payment_rows), Decimal("0")))),
        authoritative_source="finance.current-customer-months / collections / all_open",
        identity_key="settlement_entity_id or customer_id",
        amount_formula="confirmed: max(min(invoiced_amount,total_receivable)-settled_amount,0)",
    )

    waiting_count = 0
    due_waiting_count = 0
    if can_view_requisition and has_customer_scope:
        business_date = date.fromisoformat(as_of[:10])
        waiting_hold_filters = [
            RequisitionHold.status == "active",
            RequisitionHold.order_item_id.is_not(None),
        ]
        if visible_customer_ids is not None:
            waiting_hold_filters.append(
                Order.customer_id.in_(visible_customer_ids)
            )
        waiting_count = int(
            db.scalar(
                select(func.count(RequisitionHold.id))
                .join(
                    OrderItem,
                    OrderItem.id == RequisitionHold.order_item_id,
                )
                .join(Order, Order.id == OrderItem.order_id)
                .where(*waiting_hold_filters)
            )
            or 0
        )
        previous_item = aliased(OrderItem)
        previous_order = aliased(Order)
        due_waiting_count = int(
            db.scalar(
                select(func.count(RequisitionHold.id))
                .join(
                    OrderItem,
                    OrderItem.id == RequisitionHold.order_item_id,
                )
                .join(Order, Order.id == OrderItem.order_id)
                .outerjoin(
                    previous_item,
                    previous_item.id == RequisitionHold.previous_order_item_id,
                )
                .outerjoin(
                    previous_order,
                    previous_order.id == previous_item.order_id,
                )
                .where(
                    *waiting_hold_filters,
                    or_(
                        and_(
                            RequisitionHold.release_mode == "expected_date",
                            RequisitionHold.expected_requisition_date <= business_date,
                        ),
                        and_(
                            RequisitionHold.release_mode
                            == "previous_batch_completed",
                            previous_item.id.is_not(None),
                            previous_item.is_force_closed.is_(False),
                            previous_order.status.notin_(
                                ["cancelled", "dead", "closed", "archived"]
                            ),
                            previous_item.delivered_quantity
                            >= previous_item.quantity,
                        ),
                    ),
                )
            )
            or 0
        )

    return {
        "snapshot": snapshot,
        "requisition_waiting": {
            "waiting_count": waiting_count,
            "due_waiting_count": due_waiting_count,
        },
        "rows": {
            "pending_material": pending_material_rows,
            "pending_incoming": pending_incoming_rows,
            "pending_production": pending_production_rows,
            "pending_delivery": pending_delivery_rows,
            "pending_receipt": pending_receipt_rows,
            "pending_reconciliation": pending_reconciliation_rows,
            "pending_invoice": invoice_rows,
            "pending_payment": payment_rows,
        },
    }


def _authoritative_dashboard_cards(
    data: dict,
    *,
    permissions: dict[str, bool],
) -> list[dict]:
    descriptions = {
        "pending_material": ("当前可直接进入待报料的业务项", "去报料"),
        "pending_incoming": ("当前可确认收料的来料明细", "去入库"),
        "pending_production": ("当前生产确认页可操作的任务", "去生产确认"),
        "pending_delivery": ("当前至少有一款可送货的客户", "查看待送货客户"),
        "pending_receipt": ("已正式送货但还没有有效回单的客户", "去回单"),
        "pending_reconciliation": ("按客户结转周期归入本月的待对账客户", "去对账"),
        "pending_invoice": ("本月应收减去已登记发票后的余额", "去开票"),
        "pending_payment": ("全部账期已开票、尚未收款的余额", "去收款"),
    }
    result = []
    snapshot = data["snapshot"]
    for key, metric in snapshot["metrics"].items():
        if not permissions.get(key, False):
            continue
        description, button_label = descriptions[key]
        card = {
            "key": key,
            "title": metric["title"],
            "count": metric["count"],
            "count_unit": metric["count_unit"],
            "description": description,
            "button_label": button_label,
            "target": metric["target"],
            "target_filter": _dashboard_target_filter(
                key, snapshot["statement_month"]
            ),
            "identity_key": metric["identity_key"],
            "authoritative_source": metric["authoritative_source"],
            "amount_formula": metric["amount_formula"],
            "as_of": snapshot["as_of"],
            "statement_month": snapshot["statement_month"],
        }
        if metric["amount"] is not None:
            card["amount"] = metric["amount"]
        if key == "pending_material":
            card.update(data["requisition_waiting"])
        result.append(card)
    return result


def _authoritative_dashboard_todos(data: dict) -> list[dict]:
    configs = {
        "pending_material": ("待报料", "个待报料项", "去报料"),
        "pending_incoming": ("待入库", "条待入库明细", "去入库"),
        "pending_production": ("待生产", "个待生产任务", "去生产确认"),
        "pending_delivery": ("待送货", "款可送货明细", "去送货"),
        "pending_receipt": ("待回单", "张待回单送货单", "去回单"),
        "pending_reconciliation": ("待对账", "条待对账明细", "去生成月结对账单"),
        "pending_invoice": ("待开票", "个待开票客户", "去开票"),
        "pending_payment": ("待结款", "个待结款客户", "去收款"),
    }
    target_by_key = {
        key: _dashboard_target_filter(key, data["snapshot"]["statement_month"])
        for key in configs
    }
    todos: list[dict] = []
    for key, rows in data["rows"].items():
        grouped: dict[int, dict] = {}
        for row in rows:
            customer_id = row.get("customer_id")
            if not customer_id:
                continue
            customer_id = int(customer_id)
            group = grouped.setdefault(
                customer_id,
                {
                    "customer_id": customer_id,
                    "customer_name": row.get("customer_name") or "未命名客户",
                    "count": 0,
                    "amount": Decimal("0.00"),
                    "first_order_no": row.get("order_number"),
                    "first_item_no": row.get("product_code"),
                    "sort_date": _business_date_string(
                        row.get("delivery_date") or row.get("created_at")
                    ),
                },
            )
            if key == "pending_reconciliation":
                increment = int(row.get("pending_item_count") or 0)
            elif key == "pending_delivery":
                increment = int(row.get("item_count") or 0)
            else:
                increment = 1
            group["count"] += max(increment, 1)
            group["amount"] = _money(
                group["amount"] + Decimal(str(row.get("amount") or 0))
            )
        label, unit_text, action_text = configs[key]
        for group in grouped.values():
            target_filter = dict(target_by_key[key])
            target_filter["customer_id"] = group["customer_id"]
            message = f"该客户有 {group['count']} {unit_text}。"
            if group["amount"] > 0:
                message = f"该客户{label}金额为 {group['amount']} 元。"
            todos.append(
                {
                    **group,
                    "type": label,
                    "month": data["snapshot"]["statement_month"] if key.startswith("pending_") and key in {"pending_reconciliation", "pending_invoice", "pending_payment"} else None,
                    "message": message,
                    "target": data["snapshot"]["metrics"][key]["target"],
                    "target_filter": target_filter,
                    "action_text": action_text,
                }
            )
    todos.sort(key=_todo_sort_key)
    return todos


_HOME_GROUP_METRICS = {
    "stock": (),
    "production": ("pending_material", "pending_incoming", "pending_production"),
    "delivery": ("pending_delivery", "pending_receipt"),
    "finance": ("pending_reconciliation", "pending_invoice", "pending_payment"),
    "approval": (),
}


def _inventory_dashboard_cards(db, *, as_of, month, deferred=False):
    """Keep stock insight card metadata identical in full and layered reads."""
    risk_count = high_priority_count = None
    if not deferred:
        insights = build_inventory_insights(db)
        items = insights.get("action_items") or []
        risk_count = _safe_int(insights.get("action_item_count", len(items)))
        high_priority_count = _safe_int(insights.get("high_priority_action_item_count", sum(
            1 for item in items if isinstance(item.get("priority"), (int, float)) and item["priority"] <= 1
        )))
    return [
        {"key": key, "title": title, "count": count, "count_unit": "事项",
         "description": description, "button_label": button_label, "target": "warehouse",
         "target_filter": {}, "identity_key": "库存洞察事项 ID", "as_of": as_of,
         "statement_month": month}
        for key, title, count, description, button_label in (
            ("inventory_risk", "库存风险", risk_count, "库存洞察待处理项", "查看库存"),
            ("business_anomaly", "经营异常", high_priority_count, "高优先级库存异常", "查看异常"),
        )
    ]


def _layered_dashboard_payload(db, *, user, group=None):
    """No implicit detail fetch: summary supplies navigation, sections eligibility.

    Every requested section uses the same complete authoritative projection as
    the full response. There is no candidate limit before qualification.
    """
    from app.services.dashboard_workbench import build_workbench
    from app.services.home_task_preferences import annotate_tasks

    now = datetime.now(_BEIJING).replace(microsecond=0)
    today, month, as_of = now.date(), now.strftime("%Y-%m"), now.isoformat()
    visible = None if has_unrestricted_customer_access(user, db) else customer_scope_ids(user, db)
    reconciliation_due = today.day >= RECONCILIATION_REMINDER_START_DAY
    permissions = {
        "pending_material": has_permission(user, "requisition.view"),
        "pending_incoming": has_permission(user, "incoming.view"),
        "pending_production": has_permission(user, "orders.view") and user.role in {"admin", "boss", "workshop"},
        "pending_delivery": has_permission(user, "deliveries.view"),
        "pending_receipt": has_permission(user, "deliveries.view"),
        "pending_reconciliation": has_permission(user, "finance.view") and reconciliation_due,
        "pending_invoice": has_permission(user, "finance.view"),
        "pending_payment": has_permission(user, "finance.view"),
    }
    stock_warnings_allowed = permissions["pending_material"] and has_permission(user, "warehouse.view")
    stock_insights_allowed = user.role == "boss" and has_permission(user, "warehouse.view")
    groups = {
        "stock": stock_warnings_allowed or stock_insights_allowed,
        "production": any(permissions[key] for key in _HOME_GROUP_METRICS["production"]),
        "delivery": permissions["pending_delivery"],
        "finance": permissions["pending_invoice"],
        "approval": user.role in {"admin", "boss"} or has_permission(user, "business_requests.submit"),
    }
    if group is not None and not groups.get(group, False):
        raise HTTPException(status_code=403, detail="无此首页分组的查看权限")
    requested_metrics = _HOME_GROUP_METRICS.get(group, ())
    selected = {key: allowed and key in requested_metrics for key, allowed in permissions.items()}
    data = _authoritative_dashboard_data(
        db, user=user, visible_customer_ids=visible, statement_month=month, as_of=as_of,
        can_view_requisition=selected["pending_material"],
        can_view_incoming=selected["pending_incoming"],
        can_view_orders=selected["pending_production"],
        can_view_deliveries=selected["pending_delivery"],
        can_view_finance=selected["pending_invoice"],
        show_reconciliation_reminder=reconciliation_due,
    )
    cards = _authoritative_dashboard_cards(data, permissions=permissions if group is None else selected)
    if group is None:
        for card in cards:
            for field in ("count", "amount", "waiting_count", "due_waiting_count"):
                if field in card:
                    card[field] = None
    if stock_insights_allowed and group in (None, "stock"):
        cards.extend(_inventory_dashboard_cards(db, as_of=as_of, month=month, deferred=group is None))
    warnings = _common_box_low_stock_warnings(db, visible) if group == "stock" and stock_warnings_allowed else []
    workbench = build_workbench(db, user=user, data=data, warnings=warnings, today=today,
        visible_customer_ids=visible, include_approval_tasks=group == "approval")
    annotate_tasks(db, user=user, workbench=workbench, today=today)
    result = {"as_of": as_of, "month": month, "statement_month": month,
              "timezone": "Asia/Shanghai", "cards": cards, "workbench": workbench}
    if group is None:
        result.update(mode="summary", available_groups=[key for key, allowed in groups.items() if allowed])
    else:
        result["group"] = group
        if group == "stock":
            result["low_stock_warnings"] = warnings
    return result


@router.get("/sections")
def dashboard_section(
    group: Literal["stock", "production", "delivery", "finance", "approval"],
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    return _layered_dashboard_payload(db, user=user, group=group)


@router.get("/overview")
def dashboard_overview(
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
    mode: Literal["full", "summary"] = "full",
) -> dict:
    if mode == "summary":
        return _layered_dashboard_payload(db, user=user)
    raw_db = db
    now = datetime.now(_BEIJING).replace(microsecond=0)
    today = now.date()
    month = now.strftime("%Y-%m")
    as_of = now.isoformat()
    show_reconciliation_reminder = (
        today.day >= RECONCILIATION_REMINDER_START_DAY
    )
    visible_customer_ids = (
        None
        if has_unrestricted_customer_access(user, db)
        else customer_scope_ids(user, db)
    )
    can_view_orders = has_permission(user, "orders.view")
    can_view_requisition = has_permission(user, "requisition.view")
    can_view_incoming = has_permission(user, "incoming.view")
    can_view_warehouse = has_permission(user, "warehouse.view")
    can_view_deliveries = has_permission(user, "deliveries.view")
    can_view_finance = has_permission(user, "finance.view")
    can_view_production = can_view_orders and user.role in {
        "admin",
        "boss",
        "workshop",
    }
    authoritative_data = _authoritative_dashboard_data(
        raw_db,
        user=user,
        visible_customer_ids=visible_customer_ids,
        statement_month=month,
        as_of=as_of,
        can_view_requisition=can_view_requisition,
        can_view_incoming=can_view_incoming,
        can_view_orders=can_view_production,
        can_view_deliveries=can_view_deliveries,
        can_view_finance=can_view_finance,
        show_reconciliation_reminder=show_reconciliation_reminder,
    )
    metric_permissions = {
        "pending_material": can_view_requisition,
        "pending_incoming": can_view_incoming,
        "pending_production": can_view_production,
        "pending_delivery": can_view_deliveries,
        "pending_receipt": can_view_deliveries,
        "pending_reconciliation": (
            can_view_finance and show_reconciliation_reminder
        ),
        "pending_invoice": can_view_finance,
        "pending_payment": can_view_finance,
    }
    db = _CustomerScopedSession(db, visible_customer_ids)
    workflow_rows = (
        _workflow_projection_rows(
            raw_db,
            visible_customer_ids=visible_customer_ids,
            include_delivery=can_view_deliveries or can_view_finance,
            include_finance=can_view_finance,
        )
        if any(
            (
                can_view_orders,
                can_view_requisition,
                can_view_incoming,
                can_view_deliveries,
                can_view_finance,
            )
        )
        else []
    )
    embedded_kpi = _dashboard_kpi_payload(
        raw_db,
        user=user,
        visible_customer_ids=visible_customer_ids,
        today=today,
        workflow_rows=workflow_rows,
    )

    cards = _authoritative_dashboard_cards(
        authoritative_data,
        permissions=metric_permissions,
    )
    if user.role == "boss" and has_permission(user, "warehouse.view"):
        cards.extend(_inventory_dashboard_cards(raw_db, as_of=as_of, month=month))

    todos = _authoritative_dashboard_todos(authoritative_data)
    remaining_todo_count = max(len(todos) - 8, 0)
    todos = todos[:8]

    from app.services.delivery_fulfillment_projection import effective_fulfillment_lines
    physical_today = effective_fulfillment_lines()
    summary = {}
    if can_view_orders:
        summary["today_orders"] = _safe_int(
            db.scalar(select(func.count(Order.id)).where(Order.order_date == today))
        )
    if can_view_deliveries:
        summary.update(
            {
                "today_deliveries": _safe_int(
                    db.scalar(
                        select(func.count(func.distinct(Delivery.id))).join(
                            physical_today, physical_today.c.delivery_id == Delivery.id).where(
                            physical_today.c.actual_date == today,
                            Delivery.status != "voided",
                        )
                    )
                ),
                "today_receipts": _safe_int(
                    db.scalar(
                        select(func.count(ReturnReceipt.id))
                        .select_from(ReturnReceipt)
                        .join(Delivery, Delivery.id == ReturnReceipt.delivery_id)
                        .where(
                            ReturnReceipt.actual_received_date == today,
                            ReturnReceipt.status == "confirmed",
                        )
                    )
                ),
            }
        )
    if can_view_finance:
        pending_payment_amount = Decimal(
            authoritative_data["snapshot"]["metrics"]["pending_payment"][
                "amount"
            ]
            or "0"
        )
        summary.update(
            {
                "month_unsettled_amount": _money(pending_payment_amount),
                "month_settled_amount": _money(
                    db.scalar(
                        select(func.coalesce(func.sum(Statement.settled_amount), 0)).where(
                            Statement.statement_month == month
                        )
                    )
                ),
            }
        )
    visible_business_statuses: set[str] = set()
    if can_view_orders:
        visible_business_statuses.update(
            {"pending_confirmation", "pending_production"}
        )
    if can_view_requisition:
        visible_business_statuses.add("pending_material")
    if can_view_incoming:
        visible_business_statuses.add("pending_incoming")
    if can_view_deliveries:
        visible_business_statuses.update(
            {"pending_delivery", "partially_delivered", "waiting_receipt"}
        )
    if can_view_finance:
        visible_business_statuses.update(
            {
                "pending_invoice",
                "pending_payment",
                "completed",
            }
        )
        if show_reconciliation_reminder:
            visible_business_statuses.add("pending_reconciliation")
    result = {
        "cards": cards,
        "todos": todos,
        "remaining_todo_count": remaining_todo_count,
        "summary": summary,
        "business_status_counts": dict(
            Counter(
                row["business_status"]
                for row in workflow_rows
                if row["business_status"] in visible_business_statuses
            )
        ),
        "month": month,
        "statement_month": month,
        "as_of": as_of,
        "timezone": "Asia/Shanghai",
        "kpi": embedded_kpi,
    }
    if can_view_requisition and can_view_warehouse:
        low_stock_warnings = _common_box_low_stock_warnings(
            raw_db,
            visible_customer_ids,
        )
        if low_stock_warnings:
            result["low_stock_warnings"] = low_stock_warnings
    from app.services.dashboard_workbench import build_workbench
    result["workbench"] = build_workbench(
        raw_db, user=user, data=authoritative_data,
        warnings=result.get("low_stock_warnings", []), today=today,
        visible_customer_ids=visible_customer_ids,
    )
    from app.services.home_task_preferences import annotate_tasks
    annotate_tasks(raw_db, user=user, workbench=result["workbench"], today=today)
    return result
