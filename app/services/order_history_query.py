from __future__ import annotations

from datetime import datetime

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ColumnElement

from app.models.delivery import Delivery, DeliveryItem
from app.models.finance import (
    Invoice,
    ReturnReceipt,
    ReturnReceiptItem,
    SettlementRecord,
    Statement,
    StatementItem,
)
from app.models.order import OrderItem


def order_completion_activity_since_predicate(
    order_id: ColumnElement[int],
    cutoff: datetime,
) -> ColumnElement[bool]:
    """Return whether a completion-relevant finance fact changed since cutoff.

    An order becomes business-completed only after both invoicing and settlement.
    Those facts do not update ``sales_orders.updated_at``, so the recent/history
    window must follow the related statement facts rather than the order row.
    ``Statement.created_at`` keeps legacy cached-amount completions searchable
    even when they predate the explicit invoice/settlement ledgers.
    """

    statement = aliased(Statement)
    statement_item = aliased(StatementItem)
    receipt_item = aliased(ReturnReceiptItem)
    delivery_item = aliased(DeliveryItem)
    order_item = aliased(OrderItem)
    invoice = aliased(Invoice)
    settlement = aliased(SettlementRecord)
    has_recent_invoice = exists(
        select(invoice.id).where(
            invoice.statement_id == statement.id,
            invoice.created_at >= cutoff,
        )
    ).correlate(statement)
    has_recent_settlement = exists(
        select(settlement.id).where(
            settlement.statement_id == statement.id,
            settlement.created_at >= cutoff,
        )
    ).correlate(statement)
    return exists(
        select(statement.id)
        .join(statement_item, statement_item.statement_id == statement.id)
        .join(
            receipt_item,
            receipt_item.id == statement_item.return_receipt_item_id,
        )
        .join(delivery_item, delivery_item.id == receipt_item.delivery_item_id)
        .join(order_item, order_item.id == delivery_item.order_item_id)
        .where(
            order_item.order_id == order_id,
            or_(
                statement.created_at >= cutoff,
                has_recent_invoice,
                has_recent_settlement,
            ),
        )
    )


def completed_order_predicate(order_id: ColumnElement[int]) -> ColumnElement[bool]:
    """Return the SQL equivalent of the projection's order-level completed test.

    The list endpoint uses this read-only predicate to apply the active/completed
    scope before LIMIT/OFFSET.  It intentionally mirrors
    ``build_order_business_statuses`` instead of trusting the persisted order
    status, which stops at delivery and is not the finance completion fact.
    """

    item = aliased(OrderItem)

    dispatched_item = aliased(DeliveryItem)
    dispatched_delivery = aliased(Delivery)
    has_dispatched = exists(
        select(dispatched_item.id)
        .join(
            dispatched_delivery,
            dispatched_delivery.id == dispatched_item.delivery_id,
        )
        .where(
            dispatched_item.order_item_id == item.id,
            dispatched_delivery.status == "dispatched",
        )
        .correlate(item)
    )

    unchecked_delivery_item = aliased(DeliveryItem)
    unchecked_delivery = aliased(Delivery)
    checked_receipt_item = aliased(ReturnReceiptItem)
    checked_receipt = aliased(ReturnReceipt)
    has_confirmed_receipt = exists(
        select(checked_receipt_item.id)
        .join(
            checked_receipt,
            checked_receipt.id == checked_receipt_item.return_receipt_id,
        )
        .where(
            checked_receipt_item.delivery_item_id == unchecked_delivery_item.id,
            checked_receipt.status == "confirmed",
        )
        .correlate(unchecked_delivery_item)
    )
    has_unconfirmed_dispatch = exists(
        select(unchecked_delivery_item.id)
        .join(
            unchecked_delivery,
            unchecked_delivery.id == unchecked_delivery_item.delivery_id,
        )
        .where(
            unchecked_delivery_item.order_item_id == item.id,
            unchecked_delivery.status == "dispatched",
            ~has_confirmed_receipt,
        )
        .correlate(item)
    )

    quantity_receipt_item = aliased(ReturnReceiptItem)
    quantity_receipt = aliased(ReturnReceipt)
    quantity_delivery_item = aliased(DeliveryItem)
    quantity_delivery = aliased(Delivery)
    confirmed_quantity = (
        select(func.coalesce(func.sum(quantity_receipt_item.actual_received_quantity), 0))
        .select_from(quantity_receipt_item)
        .join(
            quantity_receipt,
            quantity_receipt.id == quantity_receipt_item.return_receipt_id,
        )
        .join(
            quantity_delivery_item,
            quantity_delivery_item.id == quantity_receipt_item.delivery_item_id,
        )
        .join(
            quantity_delivery,
            quantity_delivery.id == quantity_delivery_item.delivery_id,
        )
        .where(
            quantity_delivery_item.order_item_id == item.id,
            quantity_delivery.status == "dispatched",
            quantity_receipt.status == "confirmed",
        )
        .correlate(item)
        .scalar_subquery()
    )

    short_receipt_item = aliased(ReturnReceiptItem)
    short_receipt = aliased(ReturnReceipt)
    short_delivery_item = aliased(DeliveryItem)
    short_delivery = aliased(Delivery)
    has_accepted_short = exists(
        select(short_receipt_item.id)
        .join(
            short_receipt,
            short_receipt.id == short_receipt_item.return_receipt_id,
        )
        .join(
            short_delivery_item,
            short_delivery_item.id == short_receipt_item.delivery_item_id,
        )
        .join(
            short_delivery,
            short_delivery.id == short_delivery_item.delivery_id,
        )
        .where(
            short_delivery_item.order_item_id == item.id,
            short_delivery.status == "dispatched",
            short_receipt.status == "confirmed",
            short_receipt_item.resolution_action == "accept_short",
        )
        .correlate(item)
    )

    unstated_receipt_item = aliased(ReturnReceiptItem)
    unstated_receipt = aliased(ReturnReceipt)
    unstated_delivery_item = aliased(DeliveryItem)
    unstated_delivery = aliased(Delivery)
    linked_statement_item = aliased(StatementItem)
    has_statement = exists(
        select(linked_statement_item.id)
        .where(
            linked_statement_item.return_receipt_item_id
            == unstated_receipt_item.id
        )
        .correlate(unstated_receipt_item)
    )
    has_unstated_receipt = exists(
        select(unstated_receipt_item.id)
        .join(
            unstated_receipt,
            unstated_receipt.id == unstated_receipt_item.return_receipt_id,
        )
        .join(
            unstated_delivery_item,
            unstated_delivery_item.id == unstated_receipt_item.delivery_item_id,
        )
        .join(
            unstated_delivery,
            unstated_delivery.id == unstated_delivery_item.delivery_id,
        )
        .where(
            unstated_delivery_item.order_item_id == item.id,
            unstated_delivery.status == "dispatched",
            unstated_receipt.status == "confirmed",
            ~has_statement,
        )
        .correlate(item)
    )

    statement = aliased(Statement)
    statement_link = aliased(StatementItem)
    statement_receipt_item = aliased(ReturnReceiptItem)
    statement_receipt = aliased(ReturnReceipt)
    statement_delivery_item = aliased(DeliveryItem)
    statement_delivery = aliased(Delivery)
    statement_belongs_to_item = exists(
        select(statement_link.id)
        .join(
            statement_receipt_item,
            statement_receipt_item.id == statement_link.return_receipt_item_id,
        )
        .join(
            statement_receipt,
            statement_receipt.id == statement_receipt_item.return_receipt_id,
        )
        .join(
            statement_delivery_item,
            statement_delivery_item.id == statement_receipt_item.delivery_item_id,
        )
        .join(
            statement_delivery,
            statement_delivery.id == statement_delivery_item.delivery_id,
        )
        .where(
            statement_link.statement_id == statement.id,
            statement_delivery_item.order_item_id == item.id,
            statement_delivery.status == "dispatched",
            statement_receipt.status == "confirmed",
        )
        .correlate(statement, item)
    )
    invoice = aliased(Invoice)
    invoice_total = (
        select(func.coalesce(func.sum(invoice.invoice_amount), 0))
        .where(invoice.statement_id == statement.id)
        .correlate(statement)
        .scalar_subquery()
    )
    settlement = aliased(SettlementRecord)
    settlement_total = (
        select(func.coalesce(func.sum(settlement.settled_amount), 0))
        .where(settlement.statement_id == statement.id)
        .correlate(statement)
        .scalar_subquery()
    )
    has_unfinished_statement = exists(
        select(statement.id)
        .where(
            statement_belongs_to_item,
            statement.total_receivable > 0,
            or_(
                and_(
                    statement.invoiced_amount < statement.total_receivable,
                    invoice_total < statement.total_receivable,
                ),
                and_(
                    statement.settled_amount < statement.total_receivable,
                    settlement_total < statement.total_receivable,
                ),
            ),
        )
        .correlate(item)
    )

    item_is_completed = and_(
        has_dispatched,
        ~has_unconfirmed_dispatch,
        or_(
            confirmed_quantity >= item.quantity,
            item.is_force_closed.is_(True),
            has_accepted_short,
        ),
        ~has_unstated_receipt,
        ~has_unfinished_statement,
    )
    has_item = exists(
        select(item.id).where(item.order_id == order_id)
    )
    has_incomplete_item = exists(
        select(item.id).where(
            item.order_id == order_id,
            ~item_is_completed,
        )
    )
    return and_(has_item, ~has_incomplete_item)
