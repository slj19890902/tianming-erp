"""Read projections only: one physical invoice, exact frozen statement shares."""
from types import SimpleNamespace

from sqlalchemy import exists, or_, select, union_all
from sqlalchemy.orm import registry

from app.models.finance import Invoice
from app.models.invoice_task import FinanceInvoiceTask, FinanceInvoiceTaskStatement


def task_statement_filter(statement_id):
    return or_(FinanceInvoiceTask.statement_id == statement_id, exists().where(
        FinanceInvoiceTaskStatement.task_id == FinanceInvoiceTask.id,
        FinanceInvoiceTaskStatement.statement_id == statement_id))


def task_shares(db, task):
    shares = db.scalars(select(FinanceInvoiceTaskStatement).where(
        FinanceInvoiceTaskStatement.task_id == task.id).order_by(FinanceInvoiceTaskStatement.statement_id)).all()
    return shares or [SimpleNamespace(statement_id=task.statement_id,
        statement_version=task.statement_version, total_amount=task.total_amount,
        net_amount=task.net_amount, tax_amount=task.tax_amount)]


_i = Invoice.__table__
_s = FinanceInvoiceTaskStatement.__table__
_overrides = {"statement_id": _s.c.statement_id, "invoice_amount": _s.c.total_amount,
              "net_amount": _s.c.net_amount, "tax_amount": _s.c.tax_amount, "total_amount": _s.c.total_amount}
_projection = union_all(
    select(*_i.c).where(~exists().where(_s.c.task_id == _i.c.invoice_task_id)),
    select(*[_overrides.get(c.name, c).label(c.name) for c in _i.c]).select_from(
        _i.join(_s, _s.c.task_id == _i.c.invoice_task_id)),
).subquery("statement_invoices")


class StatementInvoice:
    """Read-only mapping; composite identity prevents collapsing two bill shares."""


_read_registry = registry()
_read_registry.map_imperatively(StatementInvoice, _projection,
    primary_key=[_projection.c.id, _projection.c.statement_id])

_t = FinanceInvoiceTask.__table__
task_statement_rows = union_all(
    select(_t.c.statement_id, _t.c.statement_version, _t.c.status, _t.c.id.label('task_id')).where(
        ~exists().where(_s.c.task_id == _t.c.id)),
    select(_s.c.statement_id, _s.c.statement_version, _t.c.status, _t.c.id.label('task_id')).select_from(
        _t.join(_s, _s.c.task_id == _t.c.id)),
).subquery("task_statement_rows")

UNISSUED_TASK_STATUSES = frozenset({'draft', 'ready', 'exported', 'failed'})


def active_statement_tasks(db, statement):
    """Read task identities only after the caller has admitted statement scope."""
    return [{'id': task_id, 'status': status} for task_id, status in db.execute(
        select(task_statement_rows.c.task_id, task_statement_rows.c.status).where(
            task_statement_rows.c.statement_id == statement.id,
            task_statement_rows.c.statement_version == statement.version,
            task_statement_rows.c.status.in_(UNISSUED_TASK_STATUSES),
        ).order_by(task_statement_rows.c.task_id)).all()]


def cent_invoice_completed(statement):
    """Require a live full invoice and exact frozen line coverage, never tolerance.

    The predicate is shared by display and the collection ceiling, so an issued
    task flag or a matching aggregate alone cannot authorize a rounding close.
    """
    from sqlalchemy import case, func
    from sqlalchemy.orm import aliased
    from app.models.finance import StatementItem
    from app.models.invoice_task import FinanceInvoiceTaskItem
    source = aliased(StatementItem)
    frozen = aliased(FinanceInvoiceTaskItem)
    gross = case((_t.c.price_tax_mode == 'tax_exclusive',
                  func.tm_decimal_add(func.tm_decimal_legacy(frozen.amount, 10),
                                      func.tm_decimal_legacy(frozen.tax_amount, 10))),
                 else_=func.tm_decimal_legacy(frozen.amount, 10))
    source_quantity = case((source.customer_charge_id.is_not(None), source.charge_quantity_snapshot),
                           else_=source.actual_received_quantity)
    matching_line = exists(select(frozen.id).where(
        frozen.task_id == _t.c.id, frozen.statement_item_id == source.id,
        func.tm_decimal_compare(gross, source.receivable_amount) == 0,
        func.tm_decimal_compare(frozen.quantity, source_quantity) == 0,
    ).correlate(_t, source))
    incomplete_source = exists(select(source.id).where(
        source.statement_id == statement.id, ~matching_line,
    ).correlate(statement, _t))
    source_total = select(func.sum(source.receivable_amount)).where(
        source.statement_id == statement.id).correlate(statement).scalar_subquery()
    full_live_invoice = exists(select(_i.c.id).where(
        _i.c.invoice_task_id == _t.c.id, _i.c.invoice_status == 'issued',
        func.tm_decimal_compare(_i.c.invoice_amount, _t.c.total_amount) == 0,
    ).correlate(_t))
    full_share = exists(select(_projection.c.id).where(
        _projection.c.invoice_task_id == _t.c.id,
        _projection.c.statement_id == statement.id,
        _projection.c.invoice_status == 'issued',
        func.tm_decimal_compare(_projection.c.invoice_amount, statement.invoiced_amount) == 0,
    ).correlate(_t, statement))
    return exists(select(task_statement_rows.c.statement_id).join(
        _t, _t.c.id == task_statement_rows.c.task_id).where(
        task_statement_rows.c.statement_id == statement.id,
        task_statement_rows.c.statement_version == statement.version,
        _t.c.status == 'issued',
        func.tm_decimal_compare(statement.invoiced_amount,
            func.tm_decimal_invoice_money(statement.total_receivable)) == 0,
        func.tm_decimal_compare(source_total, statement.total_receivable) == 0,
        full_live_invoice, full_share, ~incomplete_source,
    ).correlate(statement))


def collectible_receivable(statement):
    """Invoice cents close payment only with the existing complete-source proof."""
    from sqlalchemy import case, type_coerce
    from app.core.exact_money import ExactDecimalText
    return type_coerce(case((cent_invoice_completed(statement), statement.invoiced_amount),
                           else_=statement.total_receivable), ExactDecimalText())
