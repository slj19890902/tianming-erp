"""Legacy and formal route keys must not receive one purchase line twice."""
import pytest
from decimal import Decimal
from sqlalchemy import func, select

from test_chain_receipt_guards import (
    requisition_app,
    _p181_published_map_identity,
    test_batch_cannot_receive_same_order_source_under_signed_numeric_aliases as _receive_aliases,
)


@pytest.mark.parametrize("keys", [("1", "so1"), ("so1", "1")])
def test_batch_rejects_legacy_and_formal_alias_of_same_purchase(requisition_app, keys):
    _receive_aliases(requisition_app, keys)
    from app.models.production import ProductionCompletion
    from app.models.purchase_receipt import IncomingReceiptPurposeAllocation
    from app.models.supplier_settlement import SupplierReceiptSettlementPriceFact
    from app.models.warehouse_inventory import InventoryLot

    _, factory = requisition_app
    with factory() as db:
        allocation = db.scalars(select(IncomingReceiptPurposeAllocation)).one()
        assert allocation.receipt_total_sheet_qty == 40
        assert allocation.order_purpose_cost == Decimal("100")
        assert allocation.total_cost == Decimal("100")
        assert db.scalar(select(func.count(SupplierReceiptSettlementPriceFact.id))) == 1
        completion = db.get(ProductionCompletion, allocation.production_completion_id)
        assert completion.actual_output_quantity == 40
        assert db.scalar(select(func.count(ProductionCompletion.id))) == 1
        lot = db.get(InventoryLot, allocation.finished_inventory_lot_id)
        assert lot.quantity_available + lot.quantity_reserved + lot.quantity_consumed == 40
