from types import SimpleNamespace as NS
from datetime import datetime
import pytest
from app.models.incoming_receipt import IncomingReceipt
from app.models.order import OrderItem
from app.models.supplier_requisition_order import SupplierRequisitionOrder, SupplierRequisitionOrderItem
from app.services.supplier_receipt_price_facts import _receipt_source_context, SupplierReceiptPriceFactError


def test_only_authorized_historical_context_can_use_matching_frozen_order():
    receipt=NS(status="posted",received_at=datetime(2026,7,20),receipt_number="IR-X")
    source=NS(supplier_order_id=4,order_item_id=5,report_length_mm=None,report_width_mm=None,material_id=6,material_code_snapshot="A7A",supplier_name_snapshot="供应商")
    order=NS(snapshot_report_length_mm=800,snapshot_report_width_mm=600,cardboard_len=800,cardboard_width=600)
    records={IncomingReceipt:receipt,SupplierRequisitionOrderItem:source,SupplierRequisitionOrder:NS(order_number="SRO-X"),OrderItem:order}
    db=NS(get=lambda kind,id:records[kind]);item=NS(receipt_id=1,status="posted",supplier_order_item_id=2,id=3,received_quantity=20)
    with pytest.raises(SupplierReceiptPriceFactError):_receipt_source_context(db,item)
    value=_receipt_source_context(db,item,allow_historical_dimensions=True)
    assert value.report_length_mm==800 and value.report_width_mm==600
    assert source.report_length_mm is None
    order.cardboard_len=900
    with pytest.raises(SupplierReceiptPriceFactError):_receipt_source_context(db,item,allow_historical_dimensions=True)
    order.cardboard_len=800;source.report_width_mm=700
    with pytest.raises(SupplierReceiptPriceFactError):_receipt_source_context(db,item,allow_historical_dimensions=True)
