"""Shared read-only name/date matching for invoice tasks and real invoices."""
from datetime import date
import json
from fastapi import HTTPException
from sqlalchemy import select
from app.models.customer import Customer
from app.models.finance import StatementItem


def validate_range(start, end, basis):
    if basis not in ('statement', 'invoice'):
        raise HTTPException(422, '请选择账期或实际开票月份')
    for value in (start, end):
        if value:
            try:
                if date.fromisoformat(value + '-01').strftime('%Y-%m') != value:
                    raise ValueError()
            except ValueError:
                raise HTTPException(422, '月份格式应为 YYYY-MM')
    if start and end and start > end:
        raise HTTPException(422, '起始月份不能晚于结束月份')


def matches(db, statements, *, customer_id, keyword, start, end, basis, invoice_date):
    identities = set()
    names = []
    for statement in statements:
        identities.add(statement.customer_id)
        names.append(statement.settlement_name_snapshot or '')
        try:
            identities.update(int(i) for i in json.loads(statement.settlement_customer_ids_snapshot_json or '[]'))
        except (ValueError, TypeError):
            pass
        identities.update(db.scalars(select(StatementItem.source_customer_id).where(
            StatementItem.statement_id == statement.id, StatementItem.source_customer_id.is_not(None))))
    names.extend(db.scalars(select(Customer.name).where(Customer.id.in_(identities))))
    if customer_id is not None and customer_id not in identities:
        return False
    if keyword and not any(keyword.casefold() in (name or '').casefold() for name in names):
        return False
    months = [s.statement_month for s in statements] if basis == 'statement' else ([invoice_date.strftime('%Y-%m')] if invoice_date else [])
    return any((not start or m >= start) and (not end or m <= end) for m in months)
