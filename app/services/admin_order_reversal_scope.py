"""Internal, transaction-scoped authority; never accepted from a request flag."""
from contextlib import contextmanager
from contextvars import ContextVar

_items = ContextVar('admin_order_reversal_items', default=frozenset())


@contextmanager
def current_location_reversal(item_ids):
    token = _items.set(frozenset(item_ids))
    try:
        yield
    finally:
        _items.reset(token)


def may_reverse_at_current_location(order_item_id):
    return order_item_id in _items.get()
