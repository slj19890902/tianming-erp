import json
from decimal import Decimal
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select,func
from test_stock_replenishment_flow import stock_replenishment_app,_customer_replenishment_payload,_login
from test_stock_processing_auto import configure_finished_location
from app.models.incoming_receipt import IncomingReceiptItem
from app.models.incoming_actual_spec import IncomingActualSpec
from app.models.stock_preparation import StockPreparationJob as Job
from app.models.stock_replenishment import StockReplenishmentOrderItem
from app.models.warehouse_inventory import InventoryLot
from app.services.sheet_cutting_contract import SheetCuttingContract


def purchase(client,factory,quantity=200):
 _login(client);configure_finished_location(factory)
 created=client.post('/api/requisition/stock-replenishment/orders',json=_customer_replenishment_payload(quantity));assert created.status_code==201,created.text
 item=created.json()['items'][0]
 row=next(r for r in client.get('/api/requisition/pending').json()['items'] if r.get('stock_replenishment_item_id')==item['id'])
 result=client.post('/api/requisition/supplier-orders/from-pending-selection',json={'supplier_groups':[{'supplier_name':row['supplier_name'],'request_key':'actual-test-purchase','stock_sources':[{'stock_replenishment_item_id':item['id'],'source_fingerprint':row['source_fingerprint']}]}]});assert result.status_code==201,result.text
 with factory() as db:
  source=db.get(StockReplenishmentOrderItem,item['id']);source.sheet_cutting_snapshot=json.dumps(SheetCuttingContract('1865','830').to_snapshot());db.commit()
 return item['id']


def actual(client,identity,length=1865,width=2490):
 data=client.get(f'/api/incoming/actual-materials/sr{identity}');assert data.status_code==200,data.text
 material=data.json()['materials'][0]
 request=dict(material_id=material['id'],material_version=material['version'],length_mm=length,width_mm=width)
 preview=client.post(f'/api/incoming/actual-preview/sr{identity}',json=request);assert preview.status_code==200,preview.text
 return dict(request,fingerprint=preview.json()['fingerprint']),preview.json()


@pytest.mark.parametrize('quantity',[200,201])
def test_production_flush_and_full_overreceipt(stock_replenishment_app,quantity):
 app,factory=stock_replenishment_app;factory.configure(autoflush=False)
 with TestClient(app) as client:
  identity=purchase(client,factory)
  body={'received_quantity':quantity,'idempotency_key':'actual-over-receipt'}
  response=client.put(f'/api/incoming/receive/sr{identity}',json=body);assert response.status_code==200,response.text
  assert client.put(f'/api/incoming/receive/sr{identity}',json=body).status_code==200
 with factory() as db:
  assert db.scalar(select(func.sum(Job.actual_output)))==quantity
  assert db.scalar(select(func.count()).select_from(IncomingReceiptItem))==1


def test_actual_size_and_frozen_history_atomic_replay(stock_replenishment_app):
 app,factory=stock_replenishment_app;factory.configure(autoflush=False)
 with TestClient(app) as client:
  identity=purchase(client,factory)
  spec,preview=actual(client,identity)
  assert preview['yield']==3
  body={'received_quantity':67,'actual_spec':spec,'idempotency_key':'actual-larger-sheet'}
  response=client.put(f'/api/incoming/receive/sr{identity}',json=body);assert response.status_code==200,response.text
  assert client.put(f'/api/incoming/receive/sr{identity}',json=body).status_code==200
  assert client.put(f'/api/incoming/receive/sr{identity}',json=dict(body,actual_spec=dict(spec,width_mm=1660))).status_code==409
 with factory() as db:
  assert db.scalar(select(func.sum(Job.actual_output)))==201
  assert db.scalar(select(func.count()).select_from(IncomingActualSpec))==1
  item=db.get(StockReplenishmentOrderItem,identity);assert item.report_width_mm==830 and item.quantity==200
  receipt=db.scalar(select(IncomingReceiptItem));lot=db.get(InventoryLot,receipt.received_inventory_lot_id)
  assert lot.semi_finished_detail.board_width_mm==2490 and lot.quantity_consumed==67

from tests.test_phase11_requisition import requisition_app
from tests.test_p1_81_receipt_purpose_flow import _seed_material_and_staging,_create_frozen_sources,_freeze_receipt_fact,_receive
from tests.test_phase11_requisition import _login as order_login


def test_order_actual_sheet_uses_physical_yield_and_reversal(requisition_app,monkeypatch):
 from tests.test_p1_81_receipt_purpose_flow import _use_p181_published_map_identity
 _use_p181_published_map_identity(monkeypatch)
 from app.models.supplier_requisition_order import SupplierRequisitionOrderItem
 app,factory=requisition_app;_seed_material_and_staging(factory);factory.configure(autoflush=False)
 with TestClient(app) as client:
  order_login(client,'admin')
  source=_create_frozen_sources(client,factory,order_quantity=30,purchase_total=30,order_purpose=30,stock_purpose=0)[0]
  with factory() as db:
   original=db.get(SupplierRequisitionOrderItem,source.supplier_item_id)
   length,width=original.report_length_mm,original.report_width_mm
   original.sheet_cutting_snapshot=SheetCuttingContract(str(length),str(width)).to_snapshot();db.commit()
  frozen=_freeze_receipt_fact(client,source,idempotency_key='actual-order-price');assert frozen.status_code==200,frozen.text
  material=client.get('/api/incoming/actual-materials/'+source.route_key);assert material.status_code==200,material.text
  m=next(m for m in material.json()['materials'] if m['id']==source.material_id)
  request=dict(material_id=m['id'],material_version=m['version'],length_mm=float(length),width_mm=float(width)*3)
  preview=client.post('/api/incoming/actual-preview/'+source.route_key,json=request);assert preview.status_code==200,preview.text
  request['fingerprint']=preview.json()['fingerprint']
  result=_receive(client,source,frozen.json(),quantity=10,idempotency_key='actual-order-receive',overrides={'actual_spec':request})
  assert result.status_code==200,result.text
  assert result.json()['purpose_allocation']['theoretical_finished_cumulative']==30,result.text


def test_mixed_sheet_sizes_use_each_batch_factor(stock_replenishment_app):
 app,factory=stock_replenishment_app;factory.configure(autoflush=False)
 with TestClient(app) as client:
  identity=purchase(client,factory)
  first=client.put(f'/api/incoming/receive/sr{identity}',json={'received_quantity':100,'resolution_action':'await_supplier','idempotency_key':'mixed-first'});assert first.status_code==200,first.text
  spec,_=actual(client,identity)
  second=client.put(f'/api/incoming/receive/sr{identity}',json={'received_quantity':100,'actual_spec':spec,'idempotency_key':'mixed-second'});assert second.status_code==200,second.text
 with factory() as db:
  assert db.scalar(select(func.sum(Job.actual_output)))==400
  assert db.scalar(select(func.sum(Job.input_quantity)))==200


def test_actual_material_cost_identity_and_master_unchanged(stock_replenishment_app):
 from app.models.material import Material
 from app.models.product import Product
 from app.models.supplier_settlement import SupplierReceiptSettlementPriceFact
 app,factory=stock_replenishment_app;factory.configure(autoflush=False)
 with TestClient(app) as client:
  identity=purchase(client,factory)
  with factory() as db:
   m=Material(code='VINCV',supplier_name='苏州佳丰',layer_count=5,flute_type='AB',quote_price=Decimal('3'),price_unit='per_square_meter',purchase_currency='CNY',purchase_tax_included=True,purchase_tax_rate=Decimal('0.13'),is_active=True)
   db.add(m);db.commit();mid=m.id;version=m.version
   product=db.get(Product,1);original_material=product.material_id
  request=dict(material_id=mid,material_version=version,length_mm=1865,width_mm=2490)
  preview=client.post(f'/api/incoming/actual-preview/sr{identity}',json=request);assert preview.status_code==200,preview.text
  body={'received_quantity':67,'actual_spec':dict(request,fingerprint=preview.json()['fingerprint']),'idempotency_key':'actual-material'}
  result=client.put(f'/api/incoming/receive/sr{identity}',json=body);assert result.status_code==200,result.text
  history=client.get('/api/incoming/history',params={'document_keyword':'VINCV','board_width_mm':2490});assert history.status_code==200,history.text
  rows=history.json()['items'];assert rows and rows[0]['actual_material_code']=='VINCV'
  assert 'price_contract' not in rows[0]['actual_spec'] and 'price_contract' not in preview.json()
 with factory() as db:
  fact=db.scalar(select(SupplierReceiptSettlementPriceFact));assert fact.material_id==mid
  job=db.scalar(select(Job));lot=db.get(InventoryLot,job.output_lot_id)
  assert lot.finished_detail.material_code_snapshot=='VINCV'
  assert db.get(Product,1).material_id==original_material
  assert db.get(StockReplenishmentOrderItem,identity).material_code_snapshot=='A416D'


def test_stale_or_invalid_actual_preview_never_posts(stock_replenishment_app):
 app,factory=stock_replenishment_app
 with TestClient(app) as client:
  identity=purchase(client,factory);spec,_=actual(client,identity)
  for change in ({'fingerprint':'0'*64},{'width_mm':1},{'material_version':999}):
   result=client.put(f'/api/incoming/receive/sr{identity}',json={'received_quantity':67,'actual_spec':dict(spec,**change),'idempotency_key':'invalid-actual'})
   assert result.status_code in (409,422),result.text
 with factory() as db:assert db.scalar(select(func.count()).select_from(IncomingReceiptItem))==0


def test_actual_receipt_failure_rolls_back_spec_and_inventory(stock_replenishment_app,monkeypatch):
 from app.services import stock_preparation_processing as processing
 from app.services.warehouse_inventory import WarehouseInventoryError
 app,factory=stock_replenishment_app;factory.configure(autoflush=False)
 with TestClient(app) as client:
  identity=purchase(client,factory);spec,_=actual(client,identity)
  before=None
  with factory() as db:before=db.scalar(select(func.count()).select_from(InventoryLot))
  def fail(*a,**kw):raise WarehouseInventoryError('injected automatic completion failure',409)
  monkeypatch.setattr(processing,'_auto_assemble_frozen_parent',fail)
  response=client.put(f'/api/incoming/receive/sr{identity}',json={'received_quantity':67,'actual_spec':spec,'idempotency_key':'actual-failure'});assert response.status_code==409,response.text
 with factory() as db:
  assert db.scalar(select(func.count()).select_from(IncomingActualSpec))==0
  assert db.scalar(select(func.count()).select_from(IncomingReceiptItem))==0
  assert db.scalar(select(func.count()).select_from(InventoryLot))==before


def test_actual_receipt_can_reverse_without_rewriting_contract(stock_replenishment_app):
 from app.api.stock_preparation import router
 from app.services.stock_preparation_history import rows as history_rows
 app,factory=stock_replenishment_app;factory.configure(autoflush=False);app.include_router(router,prefix='/api/production')
 with TestClient(app) as client:
  identity=purchase(client,factory);spec,_=actual(client,identity)
  response=client.put(f'/api/incoming/receive/sr{identity}',json={'received_quantity':67,'actual_spec':spec,'idempotency_key':'actual-to-reverse'});assert response.status_code==200,response.text
  with factory() as db:receipt=db.scalar(select(IncomingReceiptItem));rid=receipt.id;original=db.get(IncomingActualSpec,rid).actual_json
  with factory() as db:
   history=next(r for r in history_rows(db) if r['origin']=='stock_preparation')
  reverse=client.post('/api/production/stock-preparation/completions/'+history['preparation_key']+'/revert',json={'operation_key':'actual-reverse-completion','confirm_unused':True,'jobs':history['reverse_versions']});assert reverse.status_code==200,reverse.text
  reverted=client.put(f'/api/incoming/receipt-items/{rid}/revert',json={});assert reverted.status_code==200,reverted.text
 with factory() as db:
  assert db.get(IncomingActualSpec,rid).actual_json==original
  assert db.get(IncomingReceiptItem,rid).status=='reversed'
  assert db.scalar(select(func.sum(Job.actual_output)).where(Job.status=='completed')) in (None,0)
