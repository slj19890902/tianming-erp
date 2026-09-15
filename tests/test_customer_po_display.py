from types import SimpleNamespace
from app.services.order_number_display import serialize_order_number_fields, display_order_number
from app.services.warehouse_twin_production import build_production_projection

def test_customer_po_projection_preserves_erp_identity():
    row=SimpleNamespace(id=9,order_number='TM-009',customer_po='PO-009')
    assert serialize_order_number_fields(row)==dict(order_number='TM-009',display_order_number='TM-009',customer_po='PO-009')
    assert display_order_number(row)=='TM-009'

def test_missing_po_does_not_invent_customer_number():
    row=SimpleNamespace(id=9,order_number='TM-009',customer_po=None)
    assert serialize_order_number_fields(row)['customer_po'] is None

def test_map_projection_carries_po_without_changing_ids(monkeypatch):
    monkeypatch.setattr('app.services.warehouse_twin_production.list_production_projection_mappings',lambda *args,**kwargs:[])
    result=build_production_projection(floor={'layout_id':'isolation','floor_code':'3F'},tasks=[dict(id=7,order_id=9,
        version=3,order_number='TM-009',customer_po='PO-009',planned_quantity=30)])
    row=result['items'][0]
    assert row['source_task_id']==7 and row['order_id']==9 and row['planned_quantity']==30
    assert row['customer_po']=='PO-009' and row['order_number']=='TM-009'
