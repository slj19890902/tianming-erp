"""Batch projection keeps the reserve-only finished-location fallback."""
from sqlalchemy import select

from test_p1_81_receipt_purpose_flow import (
    requisition_app,
    _p181_published_map_identity,
    test_frozen_500_600_receipts_split_450_580_600_and_block_duplicate_overreceipt as _split_receipts,
)


def test_batch_projection_preserves_explicit_and_reserve_only_finished_locations(requisition_app):
    _split_receipts(requisition_app)
    from app.models.purchase_receipt import IncomingReceiptPurposeAllocation
    from app.services.receipt_purpose_distribution import (
        serialize_receipt_purpose_allocation,
        serialize_receipt_purpose_allocations,
    )

    _, factory = requisition_app
    with factory() as db:
        rows = list(db.scalars(select(IncomingReceiptPurposeAllocation).order_by(
            IncomingReceiptPurposeAllocation.id)))
        assert len(rows) == 3
        assert rows[0].finished_inventory_lot_id is not None
        assert rows[-1].finished_inventory_lot_id is None
        for selected in (rows, rows[-1:]):
            batch = serialize_receipt_purpose_allocations(db, selected)
            for row in selected:
                single = serialize_receipt_purpose_allocation(db, row)
                assert batch[row.id] == {key: single[key] for key in batch[row.id]}
            assert batch[rows[-1].id]["finished_location_name"] == "一楼 匿名真实成品待送区-02"
            assert batch[rows[-1].id]["reserve_location_name"] == "三楼 匿名三楼左区原料备料区-01"
