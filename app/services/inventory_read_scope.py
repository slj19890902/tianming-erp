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
