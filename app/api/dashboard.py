from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends
from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from app.api.deps import RoleChecker, get_db
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
can_read = RoleChecker(["admin", "finance", "sales", "workshop"])


def _money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.00"))


def _safe_int(value) -> int:
    return int(value or 0)


@router.get("/kpi")
def dashboard_kpi(
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> dict:
    today = date.today()
    month = today.strftime("%Y-%m")
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
        .join(OrderItem, OrderItem.id == DeliveryItem.order_item_id)
        .where(
            ReturnReceipt.status == "confirmed",
            func.strftime(
                "%Y-%m",
                ReturnReceipt.actual_received_date,
            )
            == month,
        )
    )
    monthly_profit = db.scalar(
        select(func.coalesce(func.sum(StatementItem.gross_profit_amount), 0))
        .select_from(StatementItem)
        .join(Statement, Statement.id == StatementItem.statement_id)
        .where(Statement.statement_month == month)
    )
    outstanding = db.scalar(
        select(
            func.coalesce(
                func.sum(
                    Statement.total_receivable - Statement.settled_amount
                ),
                0,
            )
        ).where(Statement.status == "unsettled")
    )
    pending_delivery = db.scalar(
        select(func.count(OrderItem.id))
        .join(Order, Order.id == OrderItem.order_id)
        .where(
            Order.delivery_date == today,
            OrderItem.material_status == "received",
            OrderItem.delivered_quantity < OrderItem.quantity,
            OrderItem.is_force_closed.is_(False),
        )
    )
    pending_incoming = db.scalar(
        select(func.count(OrderItem.id))
        .join(Order, Order.id == OrderItem.order_id)
        .where(
            Order.delivery_date == today,
            OrderItem.material_status == "pending",
        )
    )
    return {
        "month": month,
        "monthly_revenue": _money(monthly_revenue),
        "monthly_gross_profit": _money(monthly_profit),
        "outstanding_receivables": _money(outstanding),
        "today_pending_delivery_tasks": int(pending_delivery or 0),
        "today_pending_incoming_tasks": int(pending_incoming or 0),
    }


@router.get("/overview")
def dashboard_overview(
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> dict:
    today = date.today()
    month = today.strftime("%Y-%m")

    pending_material_orders = _safe_int(
        db.scalar(
            select(func.count(func.distinct(Order.id)))
            .select_from(Order)
            .join(OrderItem, OrderItem.order_id == Order.id)
            .where(
                Order.status.notin_(["cancelled", "dead"]),
                OrderItem.requisition_status == "未报料",
            )
        )
    )
    pending_incoming_items = _safe_int(
        db.scalar(
            select(func.count(OrderItem.id))
            .select_from(OrderItem)
            .join(Order, Order.id == OrderItem.order_id)
            .where(
                Order.status.notin_(["cancelled", "dead"]),
                OrderItem.requisition_status.in_(["已报料", "供应商已排单"]),
                OrderItem.material_status == "pending",
            )
        )
    )
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
            )
        )
    )
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
    )
    pending_reconciliation_items = _safe_int(
        db.scalar(
            select(func.count(ReturnReceiptItem.id))
            .select_from(ReturnReceiptItem)
            .join(
                ReturnReceipt,
                ReturnReceipt.id == ReturnReceiptItem.return_receipt_id,
            )
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
    )
    unsettled_statements = db.execute(
        select(
            func.count(Statement.id),
            func.coalesce(
                func.sum(Statement.total_receivable - Statement.settled_amount),
                0,
            ),
        ).where(Statement.status == "unsettled")
    ).one()
    unsettled_count = _safe_int(unsettled_statements[0])
    unsettled_amount = _money(unsettled_statements[1])

    cards = [
        {
            "key": "pending_material",
            "title": "待报料订单",
            "count": pending_material_orders,
            "description": "订单已创建，还没有进入报料流程。",
            "button_label": "去报料",
            "target": "requisition",
        },
        {
            "key": "pending_incoming",
            "title": "待入库明细",
            "count": pending_incoming_items,
            "description": "已经报料，但仓库还没有确认来料入库。",
            "button_label": "去入库",
            "target": "incoming",
        },
        {
            "key": "pending_delivery",
            "title": "待送货明细",
            "count": pending_delivery_items,
            "description": "已经入库，可以安排送货，但还没有生成或确认送货。",
            "button_label": "去送货",
            "target": "deliveries",
        },
        {
            "key": "pending_receipt",
            "title": "待回单送货单",
            "count": pending_receipt_deliveries,
            "description": "已经发货，但还没有确认客户回单。",
            "button_label": "去回单",
            "target": "deliveries",
        },
        {
            "key": "pending_reconciliation",
            "title": "待对账明细",
            "count": pending_reconciliation_items,
            "description": "已经确认回单，但还没有进入月结对账。",
            "button_label": "去对账",
            "target": "finance",
        },
        {
            "key": "unsettled_statements",
            "title": "未结清对账单",
            "count": unsettled_count,
            "amount": unsettled_amount,
            "description": "已经生成对账单，但还没有收款结清。",
            "button_label": "去收款/对账",
            "target": "finance",
        },
    ]

    # 只取最紧急的少量待办，避免首页过重。
    pending_material_rows = db.execute(
        select(
            Order.id,
            Order.order_number,
            Order.customer_po,
            Order.delivery_date,
            Order.created_at,
            OrderItem.id.label("item_id"),
            Customer.name.label("customer_name"),
        )
        .join(OrderItem, OrderItem.order_id == Order.id)
        .join(Customer, Customer.id == Order.customer_id)
        .where(
            Order.status.notin_(["cancelled", "dead"]),
            OrderItem.requisition_status == "未报料",
        )
        .order_by(Order.delivery_date.is_(None), Order.delivery_date, Order.created_at)
        .limit(3)
    ).mappings().all()
    pending_incoming_rows = db.execute(
        select(
            OrderItem.id.label("item_id"),
            Order.order_number,
            Order.customer_po,
            Order.delivery_date,
            Order.created_at,
            Customer.name.label("customer_name"),
            OrderItem.snapshot_product_code,
            OrderItem.snapshot_product_name,
            OrderItem.requisition_status,
        )
        .select_from(OrderItem)
        .join(Order, Order.id == OrderItem.order_id)
        .join(Customer, Customer.id == Order.customer_id)
        .where(
            Order.status.notin_(["cancelled", "dead"]),
            OrderItem.requisition_status.in_(["已报料", "供应商已排单"]),
            OrderItem.material_status == "pending",
        )
        .order_by(Order.delivery_date.is_(None), Order.delivery_date, Order.created_at)
        .limit(2)
    ).mappings().all()
    pending_delivery_rows = db.execute(
        select(
            OrderItem.id.label("item_id"),
            Order.order_number,
            Order.customer_po,
            Order.delivery_date,
            Order.created_at,
            Customer.name.label("customer_name"),
            OrderItem.snapshot_product_code,
            OrderItem.snapshot_product_name,
        )
        .select_from(OrderItem)
        .join(Order, Order.id == OrderItem.order_id)
        .join(Customer, Customer.id == Order.customer_id)
        .where(
            Order.status.notin_(["cancelled", "dead"]),
            OrderItem.material_status == "received",
            OrderItem.delivered_quantity < OrderItem.quantity,
            OrderItem.is_force_closed.is_(False),
        )
        .order_by(Order.delivery_date.is_(None), Order.delivery_date, Order.created_at)
        .limit(2)
    ).mappings().all()
    pending_receipt_rows = db.execute(
        select(
            Delivery.id,
            Delivery.delivery_number,
            Delivery.delivery_date,
            Delivery.created_at,
            Customer.name.label("customer_name"),
            Delivery.status,
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
        .order_by(Delivery.delivery_date.is_(None), Delivery.delivery_date, Delivery.created_at)
        .limit(2)
    ).mappings().all()
    recon_month_expr = func.coalesce(
        func.strftime("%Y-%m", ReturnReceipt.actual_received_date),
        "未知月份",
    )
    pending_recon_rows = db.execute(
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
        .limit(6)
    ).mappings().all()
    unsettled_rows = db.execute(
        select(
            Statement.id,
            Statement.statement_number,
            Statement.statement_month,
            Statement.total_receivable,
            Statement.settled_amount,
            Customer.name.label("customer_name"),
            Statement.created_at,
        )
        .select_from(Statement)
        .join(Customer, Customer.id == Statement.customer_id)
        .where(Statement.status == "unsettled")
        .order_by(
            (Statement.total_receivable - Statement.settled_amount).desc(),
            Statement.created_at,
        )
        .limit(2)
    ).mappings().all()

    todos = []
    for row in pending_material_rows:
        todos.append(
            {
                "type": "待报料",
                "customer_name": row["customer_name"],
                "order_no": row["order_number"],
                "item_no": None,
                "current_status": "待报料",
                "message": "这张订单还没有进入报料流程，建议优先处理。",
                "target": "requisition",
            }
        )
    for row in pending_incoming_rows:
        todos.append(
            {
                "type": "待入库",
                "customer_name": row["customer_name"],
                "order_no": row["order_number"],
                "item_no": row["snapshot_product_code"],
                "current_status": row["requisition_status"],
                "message": "这条明细已经报料，还没有确认来料入库。",
                "target": "incoming",
            }
        )
    for row in pending_delivery_rows:
        todos.append(
            {
                "type": "待送货",
                "customer_name": row["customer_name"],
                "order_no": row["order_number"],
                "item_no": row["snapshot_product_code"],
                "current_status": "已入库未送货",
                "message": "这条明细可以送货了，但还没有生成或确认送货。",
                "target": "deliveries",
            }
        )
    for row in pending_receipt_rows:
        todos.append(
            {
                "type": "待回单",
                "customer_name": row["customer_name"],
                "order_no": row["delivery_number"],
                "item_no": None,
                "current_status": "已发货",
                "message": "这张送货单已经发出，还没有确认客户回单。",
                "target": "deliveries",
            }
        )
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
                "item_count": item_count,
                "amount": amount,
                "order_no": f"{month_label} 月结对账单",
                "item_no": f"共 {item_count} 条 / {amount} 元",
                "current_status": "已回单",
                "message": (
                    f"该客户 {month_label} 有 {item_count} 条送货明细待生成月结对账单，"
                    f"合计 {amount} 元。"
                ),
                "target": "finance",
                "action_text": "去生成月结对账单",
            }
        )
    for row in unsettled_rows:
        todos.append(
            {
                "type": "未结清对账单",
                "customer_name": row["customer_name"],
                "order_no": row["statement_number"],
                "item_no": row["statement_month"],
                "current_status": "未结清",
                "message": "这张对账单已经生成，但还没有收款结清。",
                "target": "finance",
            }
        )
    todos = todos[:8]

    return {
        "cards": cards,
        "todos": todos,
        "summary": {
            "today_orders": _safe_int(
                db.scalar(select(func.count(Order.id)).where(Order.order_date == today))
            ),
            "today_deliveries": _safe_int(
                db.scalar(
                    select(func.count(Delivery.id)).where(Delivery.delivery_date == today)
                )
            ),
            "today_receipts": _safe_int(
                db.scalar(
                    select(func.count(ReturnReceipt.id)).where(
                        ReturnReceipt.actual_received_date == today,
                        ReturnReceipt.status == "confirmed",
                    )
                )
            ),
            "month_unsettled_amount": unsettled_amount,
            "month_settled_amount": _money(
                db.scalar(
                    select(
                        func.coalesce(func.sum(Statement.settled_amount), 0)
                    ).where(Statement.status == "settled")
                )
            ),
        },
        "month": month,
    }
