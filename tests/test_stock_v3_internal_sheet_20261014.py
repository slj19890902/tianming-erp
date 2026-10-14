from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from tests.test_n039_composite_bom_requisition import composite_requisition_app, _login
from tests.test_manual_sheet_deduction_20261013 import ordinary_seed, payload
from app.models.order import OrderItem
from app.models.requisition import Requisition, RequisitionItem
from app.models.warehouse_inventory import InventoryLot, InventoryReservation

BASE = '/api/warehouse/semi-finished/order-items/1'


def internal_seed(sessions, *, qty=100):
    lot_id = ordinary_seed(sessions)
    with sessions() as db:
        item = db.get(OrderItem, 1)
        item.quantity = 50
        item.requisition_status = '已报料'
        item.requisition_qty = qty
        req = Requisition(requisition_number='BL-20261014-001', requisition_date=date(2026,10,14),
            supplier_name='N039 供应商', status='已报料')
        db.add(req); db.flush()
        row = RequisitionItem(requisition_id=req.id, order_item_id=1, requisition_qty=qty,
            cardboard_len=1000, cardboard_width=700, pieces_per_box=2, required_piece_qty=100,
            special_process='一开一', material_snapshot='K616K', product_name_snapshot='内部报料',
            remark='保留现场备注', status='有效')
        db.add(row); db.flush(); row_id=row.id; db.commit()
    return lot_id, row_id


@pytest.mark.parametrize('adopt,remaining', [(60,40), (100,0)])
def test_existing_internal_requisition_revises_remaining_atomically(composite_requisition_app, adopt, remaining):
    app, sessions = composite_requisition_app
    lot_id, req_id = internal_seed(sessions)
    with TestClient(app) as client:
        _login(client)
        read=client.get(BASE+'/manual-inventory')
        assert read.status_code==200,read.text
        data=read.json()
        assert data['remaining_required_piece_qty']==100
        request=payload(data,adopt,'existing-internal-'+str(adopt))
        result=client.post(BASE+'/manual-reserve',json=request)
        assert result.status_code==200,result.text
        assert result.json()['remaining_required_piece_qty']==remaining
        replay=client.post(BASE+'/manual-reserve',json=request)
        assert replay.status_code==200 and replay.json()['replayed']
    with sessions() as db:
        row=db.get(RequisitionItem,req_id)
        assert row.requisition_qty==remaining
        assert row.version==2 and row.remark=='保留现场备注'
        assert row.status==('有效' if remaining else 'voided')
        lot=db.get(InventoryLot,lot_id)
        assert (lot.quantity_available,lot.quantity_reserved,lot.quantity_consumed)==(204-adopt,adopt,0)
        assert db.scalar(select(func.count(InventoryReservation.id)))==1
        from app.models.supplier_requisition_order import SupplierRequisitionOrder
        assert db.scalar(select(func.count(SupplierRequisitionOrder.id)))==0


def test_internal_changed_since_preview_rejects_old_review(composite_requisition_app):
    app,sessions=composite_requisition_app;lot_id,req_id=internal_seed(sessions)
    with TestClient(app) as client:
        _login(client);read=client.get(BASE+'/manual-inventory');assert read.status_code==200,read.text
        with sessions() as db:
            db.get(RequisitionItem,req_id).version+=1;db.commit()
        assert client.post(BASE+'/manual-reserve',json=payload(read.json(),60)).status_code==409
    with sessions() as db:
        assert db.get(InventoryLot,lot_id).quantity_available==204


def supplier_seed(sessions, qty=20):
    from app.models.supplier_requisition_order import SupplierRequisitionOrder, SupplierRequisitionOrderItem
    with sessions() as db:
        order=SupplierRequisitionOrder(order_number='PURCHASE-PARTIAL',supplier_name='N039 供应商',
            status='confirmed',total_quantity=qty,requisition_qty=qty)
        db.add(order);db.flush()
        row=SupplierRequisitionOrderItem(supplier_order_id=order.id,order_item_id=1,
            source_key='order_item:1:whole',quantity=50,requisition_qty=qty,cutting_mode='一开一')
        db.add(row);db.flush();row_id=row.id;db.commit()
    return row_id


def test_partial_supplier_commitment_keeps_unmerged_remainder_selectable(composite_requisition_app):
    from app.models.supplier_requisition_order import SupplierRequisitionOrderItem
    app,sessions=composite_requisition_app;lot_id,req_id=internal_seed(sessions,qty=80)
    supplier_id=supplier_seed(sessions)
    with TestClient(app) as client:
        _login(client);data=client.get(BASE+'/manual-inventory').json()
        assert data['remaining_required_piece_qty']==80 and data['committed_piece_qty']==20
        assert data['items'][0]['max_stock_quantity']==80
        result=client.post(BASE+'/manual-reserve',json=payload(data,60,'partial-sixty'))
        assert result.status_code==200,result.text
        assert result.json()['remaining_required_piece_qty']==20
    with sessions() as db:
        assert db.get(RequisitionItem,req_id).requisition_qty==20
        assert db.get(SupplierRequisitionOrderItem,supplier_id).requisition_qty==20
        assert db.get(SupplierRequisitionOrderItem,supplier_id).version==1
        assert db.get(InventoryLot,lot_id).quantity_reserved==60


def test_supplier_commitment_cannot_be_silently_replaced(composite_requisition_app):
    app,sessions=composite_requisition_app;lot_id,_=internal_seed(sessions)
    supplier_seed(sessions,100)
    with TestClient(app) as client:
        _login(client);data=client.get(BASE+'/manual-inventory').json()
        assert data['remaining_required_piece_qty']==0
        assert client.post(BASE+'/manual-reserve',json=payload(data,1)).status_code==409
    with sessions() as db:assert db.get(InventoryLot,lot_id).quantity_available==204


def test_internal_draft_keeps_notes_and_extras_and_rejects_stale_review(composite_requisition_app):
    app,sessions=composite_requisition_app;_,req_id=internal_seed(sessions,qty=107)
    with sessions() as db:
        row=db.get(RequisitionItem,req_id);row.status='merged_pending';row.requisition.status='merged_pending';db.commit()
    with TestClient(app) as client:
        _login(client);data=client.get(BASE+'/manual-inventory').json();old=payload(data,60)
        result=client.post(BASE+'/manual-reserve',json=old)
        assert result.status_code==200,result.text
        assert client.post(BASE+'/manual-reserve',json={**old,'idempotency_key':'draft-stale-new-key'}).status_code==409
    with sessions() as db:
        row=db.get(RequisitionItem,req_id)
        assert row.requisition_qty==47 and row.status=='merged_pending'
        assert row.remark=='保留现场备注'


def test_internal_revision_audit_failure_rolls_back_everything(composite_requisition_app,monkeypatch):
    import app.api.warehouse as api
    app,sessions=composite_requisition_app;lot_id,req_id=internal_seed(sessions)
    def fail(*args,**kwargs):raise RuntimeError('revision audit failure')
    with TestClient(app,raise_server_exceptions=False) as client:
        _login(client);data=client.get(BASE+'/manual-inventory').json()
        monkeypatch.setattr(api,'append_audit_event',fail)
        assert client.post(BASE+'/manual-reserve',json=payload(data,60)).status_code==500
    with sessions() as db:
        assert db.get(RequisitionItem,req_id).requisition_qty==100
        assert db.get(RequisitionItem,req_id).version==1
        assert db.get(InventoryLot,lot_id).quantity_available==204
        assert db.scalar(select(func.count(InventoryReservation.id)))==0


def test_bom_internal_demand_revises_only_selected_component(composite_requisition_app):
    from decimal import Decimal
    from app.models.product_bom import RequisitionItemBomSource, SalesOrderItemBomComponent
    from tests.test_manual_sheet_deduction_20261013 import seed
    app,sessions=composite_requisition_app;lot_id=seed(sessions,double=True)
    with sessions() as db:
        header=Requisition(requisition_number='BOM-INTERNAL',requisition_date=date.today(),status='已报料')
        db.add(header);db.flush()
        row_ids=[]
        for snapshot_id,qty in [(1,40),(2,30)]:
            snap=db.get(SalesOrderItemBomComponent,snapshot_id)
            row=RequisitionItem(requisition_id=header.id,order_item_id=1,requisition_qty=qty,
                cardboard_len=1000,cardboard_width=700,special_process='一开一',
                product_name_snapshot='内部组件',required_piece_qty=qty,status='有效')
            db.add(row);db.flush();row_ids.append(row.id)
            db.add(RequisitionItemBomSource(requisition_item_id=row.id,
                sales_order_item_bom_component_id=snapshot_id,component_type='whole',
                order_set_quantity=snap.order_set_quantity,quantity_per_set=snap.quantity_per_set,
                required_piece_quantity=snap.required_piece_quantity,spare_sheet_quantity=0,
                calculated_purchase_quantity=qty))
        db.commit()
    base='/api/warehouse/semi-finished/bom-components/1'
    with TestClient(app) as client:
        _login(client);read=client.get(base+'/manual-inventory');assert read.status_code==200,read.text
        assert read.json()['remaining_required_piece_qty']==40
        result=client.post(base+'/manual-reserve',json=payload(read.json(),7,'bom-internal-seven'))
        assert result.status_code==200,result.text
        assert result.json()['remaining_required_piece_qty']==33
    with sessions() as db:
        assert db.get(RequisitionItem,row_ids[0]).requisition_qty==33
        assert db.get(RequisitionItem,row_ids[1]).requisition_qty==30
        assert db.get(RequisitionItem,row_ids[1]).version==1


def test_two_orders_competing_for_same_batch_cannot_overreserve(composite_requisition_app):
    from concurrent.futures import ThreadPoolExecutor
    app,sessions=composite_requisition_app;lot_id,_=internal_seed(sessions)
    with sessions() as db:
        first=db.get(OrderItem,1)
        values={col.name:getattr(first,col.name) for col in OrderItem.__table__.columns
                if col.name not in {'id','created_at','item_order_number'}}
        second=OrderItem(**values,item_order_number='SECOND-ORDER-LINE')
        db.add(second);db.flush();second_id=second.id
        db.get(InventoryLot,lot_id).quantity_available=150;db.commit()
    with TestClient(app) as first_client, TestClient(app) as second_client:
        _login(first_client);_login(second_client)
        first_data=first_client.get(BASE+'/manual-inventory').json()
        second_base='/api/warehouse/semi-finished/order-items/'+str(second_id)
        second_data=second_client.get(second_base+'/manual-inventory').json()
        def submit(client,base,data,key):
            return client.post(base+'/manual-reserve',json=payload(data,100,key))
        with ThreadPoolExecutor(max_workers=2) as pool:
            a=pool.submit(submit,first_client,BASE,first_data,'competition-first')
            b=pool.submit(submit,second_client,second_base,second_data,'competition-second')
            results=[a.result(),b.result()]
        assert sorted(r.status_code for r in results)==[200,409],[(r.status_code,r.text) for r in results]
    with sessions() as db:
        lot=db.get(InventoryLot,lot_id)
        assert (lot.quantity_available,lot.quantity_reserved)==(50,100)
        assert db.scalar(select(func.count(InventoryReservation.id)))==1


def test_posted_receipt_is_not_revised_as_internal_demand(composite_requisition_app):
    from datetime import datetime
    from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem
    app,sessions=composite_requisition_app;lot_id,req_id=internal_seed(sessions)
    with sessions() as db:
        header=IncomingReceipt(receipt_number='RECEIVED-EXISTING',received_at=datetime.now(),
            idempotency_key='received-existing-internal')
        db.add(header);db.flush()
        req=db.get(RequisitionItem,req_id)
        db.add(IncomingReceiptItem(receipt_id=header.id,order_id=1,order_item_id=1,
            requisition_id=req.requisition_id,requisition_item_id=req.id,planned_quantity=100,
            received_quantity=100,cumulative_received_quantity=100,variance_quantity=0,
            variance_type='matched',resolution_status='not_required',status='posted'))
        db.commit()
    with TestClient(app) as client:
        _login(client);data=client.get(BASE+'/manual-inventory').json()
        assert data['remaining_required_piece_qty']==0 and data['committed_piece_qty']==100
        assert client.post(BASE+'/manual-reserve',json=payload(data,1)).status_code==409
    with sessions() as db:
        assert db.get(RequisitionItem,req_id).requisition_qty==100
        assert db.get(RequisitionItem,req_id).version==1
        assert db.get(InventoryLot,lot_id).quantity_available==204


def test_full_stock_cover_does_not_offer_a_zero_supplier_purchase(composite_requisition_app):
    app,sessions=composite_requisition_app;internal_seed(sessions)
    with TestClient(app) as client:
        _login(client);data=client.get(BASE+'/manual-inventory').json()
        result=client.post(BASE+'/manual-reserve',json=payload(data,100,'zero-supply-stock-cover'))
        assert result.status_code==200,result.text
        pending=client.get('/api/requisition/pending');assert pending.status_code==200,pending.text
        assert not any(row.get('order_item_id',row.get('id'))==1 for row in pending.json()['items'])
        current=client.get(BASE+'/manual-inventory').json()
        assert current['remaining_required_piece_qty']==0
        assert all(row['max_stock_quantity']==0 for row in current['items'])


def test_multiple_internal_rows_preserve_separate_extra_stock(composite_requisition_app):
    app,sessions=composite_requisition_app;_,req_id=internal_seed(sessions,qty=107)
    with sessions() as db:
        first=db.get(RequisitionItem,req_id);first.requisition_qty=60
        second=RequisitionItem(requisition_id=first.requisition_id,order_item_id=1,requisition_qty=47,
            cardboard_len=1000,cardboard_width=700,pieces_per_box=2,required_piece_qty=100,
            special_process='一开一',material_snapshot='K616K',product_name_snapshot='同用途第二行',status='有效')
        db.add(second);db.flush();second_id=second.id;db.commit()
    with TestClient(app) as client:
        _login(client);data=client.get(BASE+'/manual-inventory').json()
        result=client.post(BASE+'/manual-reserve',json=payload(data,60,'multiple-extra-sixty'))
        assert result.status_code==200,result.text
    with sessions() as db:
        assert db.get(RequisitionItem,req_id).requisition_qty==40
        assert db.get(RequisitionItem,second_id).requisition_qty==7
        assert db.get(OrderItem,1).requisition_qty==47


def test_concurrent_identical_submissions_return_original_receipt_once(composite_requisition_app):
    from concurrent.futures import ThreadPoolExecutor
    app,sessions=composite_requisition_app;lot_id,req_id=internal_seed(sessions)
    with TestClient(app) as a, TestClient(app) as b:
        _login(a);_login(b)
        data=a.get(BASE+'/manual-inventory').json()
        request=payload(data,60,'identical-concurrent-sixty')
        with ThreadPoolExecutor(max_workers=2) as pool:
            first=pool.submit(a.post,BASE+'/manual-reserve',json=request)
            second=pool.submit(b.post,BASE+'/manual-reserve',json=request)
            results=[first.result(),second.result()]
        assert [r.status_code for r in results]==[200,200],[(r.status_code,r.text) for r in results]
        assert sum(bool(r.json().get('replayed')) for r in results)==1
    with sessions() as db:
        assert db.get(InventoryLot,lot_id).quantity_reserved==60
        assert db.get(RequisitionItem,req_id).requisition_qty==40
        assert db.get(RequisitionItem,req_id).version==2
        assert db.scalar(select(func.count(InventoryReservation.id)))==1
