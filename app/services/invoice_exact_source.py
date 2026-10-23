"""Prove exact statement coverage before applying the invoice's cent contract.

Rounding a tax document must never change the underlying sale facts or accept a
changed source merely because both values happen to round to the same cent.
"""
from decimal import Decimal, ROUND_HALF_UP
from sqlalchemy import select
from app.models.finance import StatementItem
from app.models.invoice_task import FinanceInvoiceTaskItem


def invoice_money(value):
    return Decimal(value).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def exact_full_statement(db, task, statement):
    source = db.scalars(select(StatementItem).where(StatementItem.statement_id == statement.id)).all()
    frozen = db.scalars(select(FinanceInvoiceTaskItem).where(
        FinanceInvoiceTaskItem.task_id == task.id,
        FinanceInvoiceTaskItem.statement_item_id.in_([row.id for row in source]))).all()
    if not source or len(frozen) != len(source):
        return False
    by_id = {row.statement_item_id: row for row in frozen}
    if len(by_id) != len(source):
        return False
    for row in source:
        item = by_id.get(row.id)
        if item is None:
            return False
        gross = item.amount + item.tax_amount if task.price_tax_mode == 'tax_exclusive' else item.amount
        quantity = row.charge_quantity_snapshot if row.customer_charge_id else row.actual_received_quantity
        if gross != row.receivable_amount or item.quantity != quantity:
            return False
    return sum((row.receivable_amount for row in source), Decimal(0)) == statement.total_receivable
