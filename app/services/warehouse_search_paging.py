"""Bounded, read-only keyset search over an already customer-scoped lot query.

The established Python matcher handles aliases and normalized dimensions. Never
truncate candidates before that matcher: a late lot can be the only match.
"""
from datetime import date

from sqlalchemy.orm import Session

from app.models.warehouse_inventory import InventoryLot
from app.services.warehouse_twin_dashboard import inventory_search_matches

SEARCH_BATCH_SIZE = 200


def search_lot_page(db: Session, query, *, keyword: str, as_of: date,
                    page_size: int = 100, after_lot_id: int | None = None):
    if not 1 <= page_size <= 500 or (after_lot_id is not None and after_lot_id < 1):
        raise ValueError("Invalid inventory search page")
    cursor = after_lot_id or 0
    matches = []
    previous_batch_had_match = True
    # Keep the incoming scope/status filters. Only the ordering is replaced.
    while len(matches) <= page_size:
        # Do not materialize 200 rows when only a few more matches are needed.
        # Sparse matching still advances in bounded batches without dropping
        # late matches; one additional match decides the next-page cursor.
        batch_size = (min(SEARCH_BATCH_SIZE, page_size + 1 - len(matches))
                      if previous_batch_had_match else SEARCH_BATCH_SIZE)
        batch = db.scalars(query.where(InventoryLot.id > cursor)
            .order_by(None).order_by(InventoryLot.id).limit(batch_size)).unique().all()
        if not batch:
            break
        matched_before = len(matches)
        for row in batch:
            cursor = row.id
            if not keyword or inventory_search_matches(row, keyword, as_of):
                matches.append(row)
                if len(matches) > page_size:
                    break
        previous_batch_had_match = len(matches) > matched_before
        if len(batch) < batch_size:
            break
    has_more = len(matches) > page_size
    rows = matches[:page_size]
    return rows, {
        "page_size": page_size,
        "has_more": has_more,
        # The look-ahead match belongs to the next page, not this cursor.
        "next_after_lot_id": rows[-1].id if has_more else None,
        "counts_scope": "page",
    }
