from copy import deepcopy
from fastapi.testclient import TestClient
from sqlalchemy import select
from tests.test_n039_composite_bom_requisition import composite_requisition_app, _login
from tests.test_p1_81_receipt_purpose_flow import _p181_published_map_identity
from tests.test_multilevel_bom_receipt_flow import seed_graph
from tests.test_multilevel_bom_order_entry import payload
from app.models.product import Product
from app.models.warehouse_inventory import InventoryLot, InventoryReservation
from app.services.production_workflow import _receipt_auto_finished_ground_target
from app.services.warehouse_inventory import manual_finished_in
from app.core.time_contract import beijing_today
from app.services.multilevel_bom_requirements import read_graph_requirements


def test_multiple_lines_share_stock_once_and_preview_equals_saved_requirements(composite_requisition_app,_p181_published_map_identity):
    app,factory=composite_requisition_app;seed_graph(factory)
    with factory() as db:
        material=db.get(Product,2).material
        material.quote_price=2;material.price_unit='元/㎡';material.purchase_currency='CNY';material.purchase_tax_included=True
        target=_receipt_auto_finished_ground_target(db,claim=True,customer_id=1,product_id=2)
        lot=manual_finished_in(db,customer_id=1,product_id=2,location_id=target.location.id,
            quantity=80,stock_date=beijing_today(),source_type='manual',remarks='多行验证',operator_id=1,
            idempotency_key='multi-line-counted',expected_layout_version=target.layout_version)
        lid=lot.id;db.commit()
    with TestClient(app) as client:
        _login(client)
        response=client.post('/api/orders/inventory-draft-preview',json=dict(customer_id=1,items=[
            dict(client_line_id=str(i),product_id=1,quantity=10) for i in range(3)]))
        assert response.status_code==200,response.text
        previews=response.json()['items']
        assert [next(c for c in row['bom_inventory_components'] if c['product_id']==2)['planned_quantity'] for row in previews]==[30,30,20]
        body=payload(factory,key='multi-line-order');original=body['items'][0]
        body['items']=[dict(deepcopy(original),client_line_id=str(i)) for i in range(3)]
        saved=client.post('/api/orders',json=body)
        assert saved.status_code==201,saved.text
    with factory() as db:
        for item,preview in zip(saved.json()['items'],previews,strict=True):
            expected={r['product_id']:r['requisition_sheet_quantity'] for r in preview['requisition_components']}
            assert {r.product_id:r.purchase_sheets for r in read_graph_requirements(db,item['id']).plan.materials}==expected
        lot=db.get(InventoryLot,lid)
        assert (lot.quantity_available,lot.quantity_reserved)==(0,80)
        assert sum(db.scalars(select(InventoryReservation.reserved_stock_quantity).where(InventoryReservation.inventory_lot_id==lid)))==80
