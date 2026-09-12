from datetime import date
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_stock_replenishment_flow import stock_replenishment_app
from test_stock_preparation_groups import prepare,plan,action_body
from app.api.stock_preparation import router
from app.services.stock_preparation import list_rows
from app.services.stock_preparation_groups import workspace_rows
from app.models.stock_replenishment import StockReplenishmentOrder,StockReplenishmentOrderItem
from app.models.warehouse_inventory import InventoryLot
from app.services.warehouse_inventory import manual_finished_in

def test_legacy_stock_without_receipt_tracks_transferred_lots_not_waiting(stock_replenishment_app):
 app,factory=stock_replenishment_app
 with factory() as db:
  lot=manual_finished_in(db,customer_id=1,product_id=1,location_id=7,quantity=20,stock_date=date(2026,9,12),source_type="manual",remarks=None,idempotency_key="legacy-origin-test",operator_id=1,expected_layout_version=1,source_ref_type='legacy-test',source_ref_id=9)
  split=manual_finished_in(db,customer_id=1,product_id=1,location_id=7,quantity=7,stock_date=date(2026,9,12),source_type="manual",remarks=None,idempotency_key="legacy-split-test",operator_id=1,expected_layout_version=1,source_ref_type='legacy-test',source_ref_id=9)
  lot.quantity_available=0;lot.status='closed'
  order=StockReplenishmentOrder(order_number='OLD-STOCKED',supplier_name='测试',customer_id=1,source_type='stock_warning',status='stocked',created_by=1)
  order.items=[StockReplenishmentOrderItem(target_inventory_type='finished',product_id=1,customer_id=1,product_code_snapshot='OLD',product_name_snapshot='旧成品',quantity=20,stocked_quantity=20,inventory_lot_id=lot.id,location_id=7)]
  db.add(order);db.commit()
  row=next(r for r in list_rows(db) if r['code']=='OLD')
  assert row['source_kind']=='legacy_stock' and row['status']=='stock'
  assert row['quantity']==20 and row['available']==7 and row['physical']==7
  assert not row['can_plan'] and row['receipt_item_id'] is None
  split.quantity_available=0;split.status='closed';db.commit()
  assert next(r for r in list_rows(db) if r['code']=='OLD')['status']=='history'

def test_stock_projection_keeps_group_sets_and_separate_material_remainder(stock_replenishment_app):
 app,factory=stock_replenishment_app;app.include_router(router,prefix='/api/production')
 with TestClient(app) as client:
  pid=prepare(app,factory,client)
  response,_=plan(client,pid,sets=5);assert response.status_code==200
  body=action_body(client,pid,'group-flow-complete','complete')
  assert client.post('/api/production/stock-preparation/group-actions',json=body).status_code==200
  items=client.get('/api/production/stock-preparation',params={'workspace':True,'state':'stock'}).json()['items']
  groups=[r for r in items if r['entry_type']=='group_stock'];assert len(groups)==1 and groups[0]['task']['remaining_sets']==5
  assert all(r['grouped_output_hidden'] for r in items if r['entry_type']=='receipt')
  # Exhaust one remaining input. A zero-set kit must not masquerade as an actionable group.
  with factory() as db:
   from app.models.incoming_receipt import IncomingReceiptItem
   receipt=db.scalars(select(IncomingReceiptItem).where(IncomingReceiptItem.stock_replenishment_item_id.is_not(None))).first()
   db.get(InventoryLot,receipt.received_inventory_lot_id).quantity_available=0;db.commit()
   assert not any(r['entry_type']=='kit' and r['plan']['available_sets']==0 for r in workspace_rows(db,list_rows(db),''))
