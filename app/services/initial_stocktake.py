"""Read the existing ledger before adding previously unrecorded physical goods."""
from __future__ import annotations

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.warehouse_inventory import InventoryLot, FinishedGoodsInventoryDetail
from app.services.stocktake import get_location_detail


def initial_stock_context(db: Session, *, location_id: int, product_id: int) -> dict:
    location = get_location_detail(db, location_id)
    lots = list(db.scalars(select(InventoryLot).join(FinishedGoodsInventoryDetail).where(
        FinishedGoodsInventoryDetail.product_id == product_id,
        InventoryLot.quantity_available + InventoryLot.quantity_reserved
        + InventoryLot.quantity_damaged > 0,
    ).order_by(InventoryLot.id)))
    facts = [(lot.id, lot.version, lot.warehouse_location_id, lot.quantity_available,
              lot.quantity_reserved, lot.quantity_damaged, lot.status) for lot in lots]
    snapshot = hashlib.sha256(json.dumps({
        "location": location, "product_id": product_id, "lots": facts,
    }, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()
    pending = bool(location.get("pending_stocktake"))
    return {
        "snapshot": snapshot,
        "existing_quantity": sum(lot.quantity_available + lot.quantity_reserved
                                 + lot.quantity_damaged for lot in lots),
        "existing_location_count": len({lot.warehouse_location_id for lot in lots}),
        "can_add": not pending,
        "block_reason": "当前货位有待审核盘点，请处理后再入库。" if pending else None,
    }
