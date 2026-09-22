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
    select(_t.c.statement_id, _t.c.statement_version, _t.c.status).where(
        ~exists().where(_s.c.task_id == _t.c.id)),
    select(_s.c.statement_id, _s.c.statement_version, _t.c.status).select_from(
        _t.join(_s, _s.c.task_id == _t.c.id)),
).subquery("task_statement_rows")
