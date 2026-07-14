from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends
from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session, with_loader_criteria

from app.api.deps import (
    PermissionChecker,
    customer_scope_ids,
    get_db,
    has_permission,
    has_unrestricted_customer_access,
)
from app.api.incoming import _rows as incoming_rows
from app.models.delivery import Delivery, DeliveryItem
from app.models.customer import Customer
from app.models.finance import (
    ReturnReceipt,
    ReturnReceiptItem,
    Statement,
    StatementItem,
)
from app.models.order import Order, OrderItem
from app.models.user import User


router = APIRouter()
can_read = PermissionChecker("dashboard.view")


def _money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.00"))


def _safe_int(value) -> int:
    return int(value or 0)


def _coalesce_date(value):
    return value or date.max


def _customer_scope_criteria(customer_id_column, visible_customer_ids: set[int] | None):
    if visible_customer_ids is None:
        return ()
    return (customer_id_column.in_(visible_customer_ids),)


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
        "未结清对账单": 20,
        "待回单": 30,
        "待送货": 40,
        "待入库": 50,
        "待报料": 60,
    }
    return (
        priority_map.get(todo.get("type"), 99),
        _coalesce_date(todo.get("sort_date")),
        -(todo.get("amount") or 0),
        todo.get("customer_name") or "",
    )


@router.get("/kpi")
def dashboard_kpi(
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    today = date.today()
    month = today.strftime("%Y-%m")
    visible_customer_ids = (
        None
        if has_unrestricted_customer_access(user, db)
        else customer_scope_ids(user, db)
    )
    can_view_incoming = has_permission(user, "incoming.view")
    can_view_deliveries = has_permission(user, "deliveries.view")
    can_view_finance = has_permission(user, "finance.view")
    db = _CustomerScopedSession(db, visible_customer_ids)
    can_view_cost = can_view_finance and has_permission(user, "cost.view")
    result = {"month": month}
    if can_view_deliveries:
        pending_delivery = db.scalar(
            select(func.count(OrderItem.id))
            .join(Order, Order.id == OrderItem.order_id)
            .where(
                Order.delivery_date == today,
                OrderItem.material_status == "received",
                OrderItem.delivered_quantity < OrderItem.quantity,
                OrderItem.is_force_closed.is_(False),
                *_customer_scope_criteria(Order.customer_id, visible_customer_ids),
            )
        )
        result["today_pending_delivery_tasks"] = int(pending_delivery or 0)
    if can_view_incoming:
        pending_incoming = db.scalar(
            select(func.count(OrderItem.id))
            .join(Order, Order.id == OrderItem.order_id)
            .where(
                Order.delivery_date == today,
                OrderItem.material_status == "pending",
                *_customer_scope_criteria(Order.customer_id, visible_customer_ids),
            )
        )
        result["today_pending_incoming_tasks"] = int(pending_incoming or 0)
    if can_view_finance:
        monthly_revenue = db.scalar(
            select(
                func.coalesce(
                    func.sum(
                        ReturnReceiptItem.actual_received_quantity
                        * OrderItem.unit_price
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
            .join(OrderItem, OrderItem.id == DeliveryItem.order_item_id)
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


@router.get("/overview")
def dashboard_overview(
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    raw_db = db
    today = date.today()
    month = today.strftime("%Y-%m")
    visible_customer_ids = (
        None
        if has_unrestricted_customer_access(user, db)
        else customer_scope_ids(user, db)
    )
    can_view_orders = has_permission(user, "orders.view")
    can_view_requisition = has_permission(user, "requisition.view")
    can_view_incoming = has_permission(user, "incoming.view")
    can_view_deliveries = has_permission(user, "deliveries.view")
    can_view_finance = has_permission(user, "finance.view")
    db = _CustomerScopedSession(db, visible_customer_ids)

    pending_material_orders = _safe_int(
        db.scalar(
            select(func.count(func.distinct(Order.id)))
            .select_from(Order)
            .join(OrderItem, OrderItem.order_id == Order.id)
            .where(
                Order.status.notin_(["cancelled", "dead"]),
                OrderItem.requisition_status == "未报料",
                or_(Order.delivery_date.is_(None), Order.delivery_date <= today),
                *_customer_scope_criteria(Order.customer_id, visible_customer_ids),
            )
        )
    ) if can_view_requisition else 0
    pending_incoming_rows = incoming_rows(raw_db, user=user) if can_view_incoming else []
    pending_incoming_items = len(pending_incoming_rows)
    pending_delivery_items = _safe_int(
        db.scalar(
            select(func.count(OrderItem.id))
            .select_from(OrderItem)
            .join(Order, Order.id == OrderItem.order_id)
            .where(
                Order.status.notin_(["cancelled", "dead"]),
                OrderItem.material_status == "received",
                OrderItem.delivered_quantity < OrderItem.quantity,
                OrderItem.is_force_closed.is_(False),
                or_(Order.delivery_date.is_(None), Order.delivery_date <= today),
            )
        )
    ) if can_view_deliveries else 0
    pending_receipt_deliveries = _safe_int(
        db.scalar(
            select(func.count(func.distinct(Delivery.id)))
            .select_from(Delivery)
            .where(
                Delivery.status == "dispatched",
                ~exists(
                    select(ReturnReceipt.id).where(
                        ReturnReceipt.delivery_id == Delivery.id,
                        ReturnReceipt.status == "confirmed",
                    )
                ),
            )
        )
    ) if can_view_deliveries else 0
    pending_reconciliation_items = _safe_int(
        db.scalar(
            select(func.count(ReturnReceiptItem.id))
            .select_from(ReturnReceiptItem)
            .join(
                ReturnReceipt,
                ReturnReceipt.id == ReturnReceiptItem.return_receipt_id,
            )
            .join(Delivery, Delivery.id == ReturnReceipt.delivery_id)
            .where(
                ReturnReceipt.status == "confirmed",
                ~exists(
                    select(StatementItem.id).where(
                        StatementItem.return_receipt_item_id
                        == ReturnReceiptItem.id,
                    )
                ),
            )
        )
    ) if can_view_finance else 0
    unsettled_statements = (
        db.execute(
            select(
                func.count(Statement.id),
                func.coalesce(
                    func.sum(Statement.total_receivable - Statement.settled_amount),
                    0,
                ),
            ).where(Statement.status == "unsettled")
        ).one()
        if can_view_finance
        else (0, 0)
    )
    unsettled_count = _safe_int(unsettled_statements[0])
    unsettled_amount = _money(unsettled_statements[1])

    cards = []
    if can_view_requisition:
        cards.append(
            {
                "key": "pending_material",
                "title": "待报料订单",
                "count": pending_material_orders,
                "description": "订单还没进入报料",
                "button_label": "去报料",
                "target": "requisition",
            }
        )
    if can_view_incoming:
        cards.append(
            {
                "key": "pending_incoming",
                "title": "待入库明细",
                "count": pending_incoming_items,
                "description": "已报料但还没入库",
                "button_label": "去入库",
                "target": "incoming",
            }
        )
    if can_view_deliveries:
        cards.extend(
            [
                {
                    "key": "pending_delivery",
                    "title": "待送货明细",
                    "count": pending_delivery_items,
                    "description": "已入库但还没送货",
                    "button_label": "去送货",
                    "target": "deliveries",
                },
                {
                    "key": "pending_receipt",
                    "title": "待回单送货单",
                    "count": pending_receipt_deliveries,
                    "description": "已发货但还没回单",
                    "button_label": "去回单",
                    "target": "deliveries",
                },
            ]
        )
    if can_view_finance:
        cards.extend(
            [
                {
                    "key": "pending_reconciliation",
                    "title": "待对账明细",
                    "count": pending_reconciliation_items,
                    "description": "已回单但还没对账",
                    "button_label": "去对账",
                    "target": "finance",
                },
                {
                    "key": "unsettled_statements",
                    "title": "未结清对账单",
                    "count": unsettled_count,
                    "amount": unsettled_amount,
                    "description": "还没收款结清",
                    "button_label": "去收款/对账",
                    "target": "finance",
                },
            ]
        )

    pending_material_rows = (
        db.execute(
            select(
            Customer.id.label("customer_id"),
            Customer.name.label("customer_name"),
            Order.order_number,
            Order.delivery_date,
            Order.created_at,
            )
            .select_from(Order)
            .join(Customer, Customer.id == Order.customer_id)
            .join(OrderItem, OrderItem.order_id == Order.id)
            .where(
                Order.status.notin_(["cancelled", "dead"]),
                OrderItem.requisition_status == "未报料",
                or_(Order.delivery_date.is_(None), Order.delivery_date <= today),
            )
            .order_by(
                Order.delivery_date.is_(None),
                Order.delivery_date,
                Order.created_at,
                Order.id,
            )
        ).mappings().all()
        if can_view_requisition
        else []
    )
    pending_delivery_rows = (
        db.execute(
            select(
            Customer.id.label("customer_id"),
            Customer.name.label("customer_name"),
            Order.order_number,
            Order.delivery_date,
            Order.created_at,
            OrderItem.snapshot_product_code,
            )
            .select_from(OrderItem)
            .join(Order, Order.id == OrderItem.order_id)
            .join(Customer, Customer.id == Order.customer_id)
            .where(
                Order.status.notin_(["cancelled", "dead"]),
                OrderItem.material_status == "received",
                OrderItem.delivered_quantity < OrderItem.quantity,
                OrderItem.is_force_closed.is_(False),
                or_(Order.delivery_date.is_(None), Order.delivery_date <= today),
            )
            .order_by(
                Order.delivery_date.is_(None),
                Order.delivery_date,
                Order.created_at,
                OrderItem.id,
            )
        ).mappings().all()
        if can_view_deliveries
        else []
    )
    pending_receipt_rows = (
        db.execute(
            select(
            Customer.id.label("customer_id"),
            Customer.name.label("customer_name"),
            Delivery.delivery_number,
            Delivery.delivery_date,
            Delivery.created_at,
            )
            .select_from(Delivery)
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
            .order_by(
                Delivery.delivery_date.is_(None),
                Delivery.delivery_date,
                Delivery.created_at,
                Delivery.id,
            )
        ).mappings().all()
        if can_view_deliveries
        else []
    )
    recon_month_expr = func.coalesce(
        func.strftime("%Y-%m", ReturnReceipt.actual_received_date),
        "未知月份",
    )
    pending_recon_rows = (
        db.execute(
        select(
            Customer.id.label("customer_id"),
            Customer.name.label("customer_name"),
            recon_month_expr.label("month"),
            func.count(ReturnReceiptItem.id).label("item_count"),
            func.coalesce(
                func.sum(
                    ReturnReceiptItem.actual_received_quantity * OrderItem.unit_price
                ),
                0,
            ).label("amount"),
            func.min(ReturnReceipt.actual_received_date).label("first_received_date"),
            func.min(ReturnReceipt.created_at).label("first_created_at"),
        )
        .select_from(ReturnReceiptItem)
        .join(ReturnReceipt, ReturnReceipt.id == ReturnReceiptItem.return_receipt_id)
        .join(DeliveryItem, DeliveryItem.id == ReturnReceiptItem.delivery_item_id)
        .join(Delivery, Delivery.id == DeliveryItem.delivery_id)
        .join(OrderItem, OrderItem.id == DeliveryItem.order_item_id)
        .join(Order, Order.id == OrderItem.order_id)
        .join(Customer, Customer.id == Order.customer_id)
        .where(
            ReturnReceipt.status == "confirmed",
            ~exists(
                select(StatementItem.id).where(
                    StatementItem.return_receipt_item_id
                    == ReturnReceiptItem.id,
                )
            ),
        )
        .group_by(Customer.id, Customer.name, recon_month_expr)
        .order_by(
            recon_month_expr,
            func.count(ReturnReceiptItem.id).desc(),
            func.coalesce(
                func.sum(
                    ReturnReceiptItem.actual_received_quantity * OrderItem.unit_price
                ),
                0,
            ).desc(),
            func.min(ReturnReceipt.actual_received_date),
            func.min(ReturnReceipt.created_at),
        )
        ).mappings().all()
        if can_view_finance
        else []
    )
    unsettled_rows = (
        db.execute(
        select(
            Customer.id.label("customer_id"),
            Customer.name.label("customer_name"),
            Statement.statement_month,
            Statement.total_receivable,
            Statement.settled_amount,
            Statement.created_at,
        )
        .select_from(Statement)
        .join(Customer, Customer.id == Statement.customer_id)
        .where(Statement.status == "unsettled")
        .order_by(
            (Statement.total_receivable - Statement.settled_amount).desc(),
            Statement.created_at,
        )
        ).mappings().all()
        if can_view_finance
        else []
    )

    pending_material_groups: dict[int, dict] = {}
    for row in pending_material_rows:
        group = pending_material_groups.setdefault(
            row["customer_id"],
            {
                "type": "待报料",
                "customer_name": row["customer_name"],
                "count": 0,
                "first_order_no": row["order_number"],
                "first_item_no": None,
                "sort_date": row["delivery_date"] or row["created_at"],
                "message": "",
                "target": "requisition",
                "action_text": "去报料",
            },
        )
        group["count"] += 1

    pending_incoming_groups: dict[int, dict] = {}
    for row in pending_incoming_rows:
        group = pending_incoming_groups.setdefault(
            row["customer_id"],
            {
                "type": "待入库",
                "customer_name": row["customer_name"],
                "count": 0,
                "first_order_no": row["order_number"],
                "first_item_no": row.get("product_code"),
                "sort_date": row["delivery_date"] or row["created_at"],
                "message": "",
                "target": "incoming",
                "action_text": "去入库",
            },
        )
        group["count"] += 1

    pending_delivery_groups: dict[int, dict] = {}
    for row in pending_delivery_rows:
        group = pending_delivery_groups.setdefault(
            row["customer_id"],
            {
                "type": "待送货",
                "customer_name": row["customer_name"],
                "count": 0,
                "first_order_no": row["order_number"],
                "first_item_no": row["snapshot_product_code"],
                "sort_date": row["delivery_date"] or row["created_at"],
                "message": "",
                "target": "deliveries",
                "action_text": "去送货",
            },
        )
        group["count"] += 1

    pending_receipt_groups: dict[int, dict] = {}
    for row in pending_receipt_rows:
        group = pending_receipt_groups.setdefault(
            row["customer_id"],
            {
                "type": "待回单",
                "customer_name": row["customer_name"],
                "count": 0,
                "first_order_no": row["delivery_number"],
                "first_item_no": None,
                "sort_date": row["delivery_date"] or row["created_at"],
                "message": "",
                "target": "deliveries",
                "action_text": "去回单",
            },
        )
        group["count"] += 1

    todos = []
    for group in pending_material_groups.values():
        group["message"] = (
            f"该客户有 {group['count']} 张订单还没有进入报料流程，建议优先处理。"
        )
        todos.append(group)
    for group in pending_incoming_groups.values():
        group["message"] = (
            f"该客户有 {group['count']} 条明细已报料但还没有确认来料入库。"
        )
        todos.append(group)
    for group in pending_delivery_groups.values():
        group["message"] = (
            f"该客户有 {group['count']} 条明细已可送货，建议尽快安排。"
        )
        todos.append(group)
    for group in pending_receipt_groups.values():
        group["message"] = (
            f"该客户有 {group['count']} 张送货单已发货但还没确认回单。"
        )
        todos.append(group)
    for row in pending_recon_rows:
        month_label = row["month"] or "未知月份"
        item_count = int(row["item_count"] or 0)
        amount = _money(row["amount"])
        todos.append(
            {
                "type": "待对账",
                "customer_id": row["customer_id"],
                "customer_name": row["customer_name"],
                "month": month_label,
                "count": item_count,
                "item_count": item_count,
                "amount": amount,
                "first_order_no": None,
                "first_item_no": None,
                "sort_date": row["first_received_date"] or row["first_created_at"],
                "message": (
                    f"该客户 {month_label} 有 {item_count} 条送货明细待生成月结对账单，"
                    f"合计 {amount} 元。"
                ),
                "target": "finance",
                "action_text": "去生成月结对账单",
            }
        )
    unsettled_groups: dict[int, dict] = {}
    for row in unsettled_rows:
        balance = _money((row["total_receivable"] or 0) - (row["settled_amount"] or 0))
        group = unsettled_groups.setdefault(
            row["customer_id"],
            {
                "type": "未结清对账单",
                "customer_name": row["customer_name"],
                "count": 0,
                "amount": Decimal("0.00"),
                "month": row["statement_month"],
                "first_order_no": None,
                "first_item_no": None,
                "sort_date": row["created_at"],
                "message": "",
                "target": "finance",
                "action_text": "去收款/对账",
            },
        )
        group["count"] += 1
        group["amount"] = _money(group["amount"] + balance)
    for group in unsettled_groups.values():
        group["message"] = (
            f"该客户还有 {group['count']} 张对账单未结清，合计 {group['amount']} 元。"
        )
        todos.append(group)

    todos.sort(key=_todo_sort_key)
    remaining_todo_count = max(len(todos) - 8, 0)
    todos = todos[:8]

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
                        select(func.count(Delivery.id)).where(
                            Delivery.delivery_date == today
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
        summary.update(
            {
                "month_unsettled_amount": unsettled_amount,
                "month_settled_amount": _money(
                    db.scalar(
                        select(func.coalesce(func.sum(Statement.settled_amount), 0)).where(
                            Statement.status == "settled"
                        )
                    )
                ),
            }
        )
    return {
        "cards": cards,
        "todos": todos,
        "remaining_todo_count": remaining_todo_count,
        "summary": summary,
        "month": month,
    }
