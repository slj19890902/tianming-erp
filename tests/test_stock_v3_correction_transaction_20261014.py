"""Later correction failure cannot leave procurement or stock partly committed."""
import pytest
from sqlalchemy import select, func
from tests.test_stock_v3_internal_sheet_20261014 import composite_requisition_app, internal_seed, supplier_seed
from tests.test_manual_sheet_deduction_20261013 import payload
from app.models.user import User
from app.models.requisition import RequisitionItem
from app.models.supplier_requisition_order import SupplierRequisitionOrder, SupplierRequisitionOrderItem
from app.models.warehouse_inventory import InventoryLot, InventoryReservation
from app.services.bom_transactions import atomic_bom
from app.api.requisition import _void_supplier_order
from app.api.warehouse import _order_manual_sheet_state, _reserve_manual_sheet_inventory, ManualSheetReservationPayload


def test_internal_reserve_remains_owned_by_correction_transaction(composite_requisition_app):
    _, sessions = composite_requisition_app
    lot_id, req_id = internal_seed(sessions)
    with sessions() as db:
        actor = db.get(User, 1)
        with pytest.raises(RuntimeError, match='later correction failure'):
            with atomic_bom(db):
                preview = _order_manual_sheet_state(db, 1, 'whole', actor)[3]
                result = _reserve_manual_sheet_inventory(1, ManualSheetReservationPayload(**payload(preview,60)),
                    db,actor,is_bom=False,commit=False)
                assert result['remaining_required_piece_qty'] == 40
                raise RuntimeError('later correction failure')
        db.rollback()
    with sessions() as db:
        assert db.get(RequisitionItem,req_id).requisition_qty == 100
        assert db.get(InventoryLot,lot_id).quantity_available == 204
        assert db.scalar(select(func.count(InventoryReservation.id))) == 0


def test_supplier_void_remains_owned_by_correction_transaction(composite_requisition_app):
    _, sessions = composite_requisition_app
    internal_seed(sessions)
    supplier_item_id = supplier_seed(sessions,100)
    with sessions() as db:
        supplier_id = db.get(SupplierRequisitionOrderItem,supplier_item_id).supplier_order_id
        with pytest.raises(RuntimeError, match='later correction failure'):
            with atomic_bom(db):
                _void_supplier_order(supplier_id,db=db,user=db.get(User,1),commit=False)
                assert db.get(SupplierRequisitionOrder,supplier_id).status == 'voided'
                raise RuntimeError('later correction failure')
        db.rollback()
    with sessions() as db:
        assert db.get(SupplierRequisitionOrder,supplier_id).status == 'confirmed'
        assert db.scalar(select(RequisitionItem)).status == '有效'
