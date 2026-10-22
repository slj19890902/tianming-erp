"""Short-lived memoization for read-only inventory qualification summaries.

Opt-in only: never enabled for reservations or other inventory writes, never
shared across requests, and always discarded when the summary finishes.
"""
from contextlib import contextmanager
from contextvars import ContextVar

_scope = ContextVar("inventory_summary_read_scope", default=None)


@contextmanager
def inventory_summary_read_scope(db):
    token = _scope.set((db, {}))
    try:
        yield
    finally:
        _scope.reset(token)


def summary_read(db, key, read):
    scope = _scope.get()
    if scope is None or scope[0] is not db:
        return read()
    cache = scope[1]
    if key not in cache:
        cache[key] = read()
    return cache[key]


def summary_get(db, model, identity):
    return summary_read(db, (model, identity), lambda: db.get(model, identity))


def summary_scope_active(db):
    scope = _scope.get()
    return scope is not None and scope[0] is db


def summary_prefetch(db, model, identities):
    """Prime single-key lookups only in this Session's explicit read scope.

    Missing rows are cached as missing, just like summary_get. Nothing is
    retained beyond the scope; ordinary qualification/writes keep fresh gets.
    """
    scope = _scope.get()
    if scope is None or scope[0] is not db:
        return
    cache = scope[1]
    missing = {identity for identity in identities
               if identity is not None and (model, identity) not in cache}
    if not missing:
        return
    from sqlalchemy import inspect, select
    primary_key = inspect(model).primary_key
    if len(primary_key) != 1:
        return
    identities = sorted(missing)
    for start in range(0, len(identities), 500):
        batch = identities[start:start + 500]
        rows = db.scalars(select(model).where(primary_key[0].in_(batch))).all()
        found = {inspect(row).identity[0]: row for row in rows}
        cache.update({(model, identity): found.get(identity) for identity in batch})
