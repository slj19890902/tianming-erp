"""Shared customer-master identity search for business list queries."""

from __future__ import annotations

from sqlalchemy import or_
from sqlalchemy.sql.elements import ColumnElement

from app.models.customer import Customer


def customer_identity_search_clause(
    keyword: str | None,
) -> ColumnElement[bool] | None:
    """Match only authoritative customer identity fields.

    ``customer_code`` is the maintained customer abbreviation.  Keeping this
    predicate in one place prevents order pages from inventing aliases or
    applying a different customer identity contract after pagination.
    """

    normalized = str(keyword or "").strip()
    if not normalized:
        return None
    pattern = f"%{normalized}%"
    return or_(
        Customer.name.ilike(pattern),
        Customer.chinese_short_name.ilike(pattern),
        Customer.customer_code.ilike(pattern),
    )
