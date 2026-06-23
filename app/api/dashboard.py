from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import RoleChecker, get_db
from app.models.delivery import Delivery, DeliveryItem
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
