from datetime import date,timedelta
from decimal import Decimal
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select,func
from tests.test_phase11_requisition import requisition_app,_login
from tests.test_p1_81_receipt_purpose_flow import (_p181_published_map_identity,_seed_material_and_staging,_create_frozen_sources,_freeze_receipt_fact,_receive)


def setup(app,factory):
    from app.api.deliveries import router as deliveries
    from app.api.tianhua_pre_delivery import router,mobile_router
    app.include_router(deliveries,prefix='/api/deliveries');app.include_router(router,prefix='/api/deliveries');app.include_router(mobile_router,prefix='/api/mobile')
    _seed_material_and_staging(factory)


def notice(factory,*,item_id=1,quantity=300,due=None,po='OLD-PO'):
    from app.models.tianhua_pre_delivery import TianhuaPreDeliveryImportBatch as Batch,TianhuaPreDeliveryImportItem as Notice
    from app.models.order import OrderItem,Order
    from app.models.customer import Customer
    import uuid
    with factory() as db:
        item=db.get(OrderItem,item_id);order=db.get(Order,item.order_id);customer=db.get(Customer,order.customer_id)
        batch=Batch(batch_number='R09-'+uuid.uuid4().hex,filename='isolated.png',customer_id=customer.id,customer_name=customer.name,pre_delivery_date=due or date.today(),total_rows=1,created_by=1)
        db.add(batch);db.flush()
        row=Notice(batch_id=batch.id,row_no=1,raw_text='isolated notice',stock_code=item.snapshot_product_code,image_qty=quantity,
            product_id=item.product_id,product_name=item.snapshot_product_name,order_item_id=item.id,order_id=item.order_id,order_number=order.order_number,
            customer_order_no=po,status='ok',selected=True,final_delivery_qty=quantity,suggested_qty=quantity)
        db.add(row);db.commit();return row.id,batch.id


def stock(client,factory):
    source=_create_frozen_sources(client,factory,order_quantity=300,purchase_total=300,order_purpose=300,stock_purpose=0)[0]
    price=_freeze_receipt_fact(client,source,idempotency_key='r09-price').json()
    return source,price


def rows(client,**params):
    result=client.get('/api/deliveries/tianhua-backlogs/list',params=params);assert result.status_code==200,result.text
    return result.json()['items']


def test_wait_300_draft_no_debit_dispatch_100_cancel_restore(requisition_app,monkeypatch):
    from app.models.delivery_backlog import DeliveryBacklog,DeliveryBacklogSource,DeliveryBacklogFulfillment
    from app.models.order import OrderItem
    app,factory=requisition_app;setup(app,factory)
    with TestClient(app) as client:
        _login(client,'admin');source,price=stock(client,factory);nid,_=notice(factory)
        saved=client.post(f'/api/deliveries/tianhua-backlogs/defer/{nid}',json=dict(quantity=300,reason='未到料'))
        assert saved.status_code==200,saved.text
        assert saved.json()['remaining_quantity']==300 and saved.json()['ready_quantity']==0
        assert client.post(f'/api/deliveries/tianhua-backlogs/defer/{nid}',json=dict(quantity=300,reason='未到料')).json()['id']==saved.json()['id']
        assert client.post(f'/api/deliveries/tianhua-backlogs/defer/{nid}',json=dict(quantity=200,reason='改数量')).status_code==409
        nid2,_=notice(factory)
        again=client.post(f'/api/deliveries/tianhua-backlogs/defer/{nid2}',json=dict(quantity=300,reason='重复通知仍未到料'))
        assert again.status_code==200 and again.json()['target_quantity']==300,again.text
        receipt=_receive(client,source,price,quantity=300,idempotency_key='r09-stock');assert receipt.status_code==200,receipt.text
        assert rows(client)[0]['ready_quantity']==300
        created=client.post('/api/deliveries',json=dict(customer_id=1,delivery_date=date.today().isoformat(),items=[dict(order_item_id=1,delivered_quantity=100)]))
        assert created.status_code==201,created.text
        assert rows(client)[0]['remaining_quantity']==300
        did=created.json()['id']
        import app.services.delivery_backlogs as svc
        original=svc.audit
        def fail_audit(*a,**kw):raise RuntimeError('r09-audit-failure')
        monkeypatch.setattr(svc,'audit',fail_audit)
        with pytest.raises(RuntimeError,match='r09-audit-failure'):client.put(f'/api/deliveries/{did}/dispatch')
        monkeypatch.setattr(svc,'audit',original)
        assert rows(client)[0]['remaining_quantity']==300
        with factory() as db:assert db.get(OrderItem,1).delivered_quantity==0
        sent=client.put(f'/api/deliveries/{did}/dispatch');assert sent.status_code==200,sent.text
        assert rows(client)[0]['remaining_quantity']==200 and rows(client)[0]['fulfilled_quantity']==100
        client.put(f'/api/deliveries/{did}/dispatch')
        with factory() as db:
            assert db.scalar(select(func.count()).select_from(DeliveryBacklog))==1
            assert db.scalar(select(func.count()).select_from(DeliveryBacklogSource))==2
            assert db.scalar(select(func.count()).select_from(DeliveryBacklogFulfillment))==1
        cancelled=client.put(f'/api/deliveries/{did}/cancel');assert cancelled.status_code==200,cancelled.text
        assert rows(client)[0]['remaining_quantity']==300
        client.put(f'/api/deliveries/{did}/cancel')
        assert rows(client)[0]['remaining_quantity']==300


def test_mobile_partial_only_future_delivery_reduces_missing_200(requisition_app):
    from app.core.security import create_tianhua_pick_token
    from app.models.tianhua_pre_delivery import TianhuaPreDeliveryDraftItem
    app,factory=requisition_app;setup(app,factory)
    with TestClient(app) as client:
        _login(client,'admin');source,price=stock(client,factory)
        receipt=_receive(client,source,price,quantity=300,idempotency_key='r09-mobile-stock');assert receipt.status_code==200,receipt.text
        nid,bid=notice(factory)
        draft=client.post(f'/api/deliveries/tianhua-preimport/{bid}/create-draft',json=dict(items=[dict(item_id=nid,row_no=1,selected=True,final_delivery_qty=300)]))
        assert draft.status_code==201,draft.text
        with factory() as db:line=db.scalar(select(TianhuaPreDeliveryDraftItem));lineid=line.id
        token,_=create_tianhua_pick_token(bid,draft.json()['draft_id'])
        picked=client.put(f'/api/mobile/tianhua-pick/items/{lineid}',json=dict(token=token,mobile_pick_status='partial',mobile_picked_qty=100,wait_next=True,mobile_pick_note='只拿到100，其余待补'))
        assert picked.status_code==200,picked.text
        assert rows(client)[0]['remaining_quantity']==200
        replay=client.put(f'/api/mobile/tianhua-pick/items/{lineid}',json=dict(token=token,mobile_pick_status='partial',mobile_picked_qty=100,wait_next=True,mobile_pick_note='只拿到100，其余待补'))
        assert replay.status_code==200 and replay.json()==picked.json(),replay.text
        sent=client.put(f"/api/deliveries/{draft.json()['delivery_id']}/dispatch");assert sent.status_code==200,sent.text
        assert rows(client)[0]['remaining_quantity']==200 and rows(client)[0]['fulfilled_quantity']==0


def test_revision_cancel_and_replay_preserve_backlog_facts(requisition_app):
    app,factory=requisition_app;setup(app,factory)
    with TestClient(app) as client:
        _login(client,'admin');source,price=stock(client,factory);nid,_=notice(factory)
        assert client.post(f'/api/deliveries/tianhua-backlogs/defer/{nid}',json=dict(quantity=300,reason='下次补送')).status_code==200
        assert _receive(client,source,price,quantity=300,idempotency_key='r09-revision-in').status_code==200
        created=client.post('/api/deliveries',json=dict(customer_id=1,delivery_date=date.today().isoformat(),items=[dict(order_item_id=1,delivered_quantity=100)]))
        did=created.json()['id'];sent=client.put(f'/api/deliveries/{did}/dispatch')
        payload=dict(source_mode='order',expected_version=sent.json()['version'],idempotency_key='r09-revise',items=[dict(order_item_id=1,delivered_quantity=50)])
        revised=client.put(f'/api/deliveries/{did}/revision',json=payload);assert revised.status_code==200,revised.text
        assert rows(client)[0]['remaining_quantity']==250
        repeated=client.put(f'/api/deliveries/{did}/revision',json=payload);assert repeated.status_code==200,repeated.text
        assert rows(client)[0]['fulfilled_quantity']==50
        assert client.put(f'/api/deliveries/{did}/cancel').status_code==200
        assert rows(client)[0]['remaining_quantity']==300
        # Retrying the original deferral after dispatch does not erase delivery history.
        replay=client.post(f'/api/deliveries/tianhua-backlogs/defer/{nid}',json=dict(quantity=300,reason='下次补送'))
        assert replay.status_code==200,replay.text
        assert len(replay.json()['deliveries'])==2


def test_mobile_zero_removes_current_delivery_but_keeps_backlog(requisition_app):
    from app.core.security import create_tianhua_pick_token
    from app.models.tianhua_pre_delivery import TianhuaPreDeliveryDraftItem,TianhuaPreDeliveryDraft
    from app.models.delivery import Delivery
    app,factory=requisition_app;setup(app,factory)
    with TestClient(app) as client:
        _login(client,'admin');source,price=stock(client,factory)
        assert _receive(client,source,price,quantity=300,idempotency_key='r09-zero-in').status_code==200
        nid,bid=notice(factory)
        draft=client.post(f'/api/deliveries/tianhua-preimport/{bid}/create-draft',json=dict(items=[dict(item_id=nid,row_no=1,selected=True,final_delivery_qty=300)]));assert draft.status_code==201,draft.text
        with factory() as db:lineid=db.scalar(select(TianhuaPreDeliveryDraftItem.id))
        token,_=create_tianhua_pick_token(bid,draft.json()['draft_id'])
        payload=dict(token=token,mobile_pick_status='no_stock',mobile_picked_qty=0,wait_next=True,mobile_pick_note='未完工')
        picked=client.put(f'/api/mobile/tianhua-pick/items/{lineid}',json=payload);assert picked.status_code==200,picked.text
        assert client.put(f'/api/mobile/tianhua-pick/items/{lineid}',json=payload).status_code==200
        assert rows(client)[0]['remaining_quantity']==300
        with factory() as db:
            assert db.get(TianhuaPreDeliveryDraft,draft.json()['draft_id']).delivery_id is None
            assert db.get(Delivery,draft.json()['delivery_id']) is None


def test_ready_requires_own_active_finished_stock_and_admin_cancel(requisition_app):
    from app.models.warehouse_inventory import InventoryLot,InventoryReservation
    from app.models.order import OrderItem
    app,factory=requisition_app;setup(app,factory)
    with TestClient(app) as client:
        _login(client,'admin');source,price=stock(client,factory);nid,_=notice(factory)
        saved=client.post(f'/api/deliveries/tianhua-backlogs/defer/{nid}',json=dict(quantity=300,reason='等成品'));assert saved.status_code==200,saved.text
        assert rows(client)[0]['ready_quantity']==0
        assert _receive(client,source,price,quantity=300,idempotency_key='r09-ready').status_code==200
        assert rows(client)[0]['ready_quantity']==300
        with factory() as db:
            lot=db.scalar(select(InventoryLot).where(InventoryLot.inventory_type=='finished'));lot.status='frozen';lotid=lot.id;db.commit()
        assert rows(client)[0]['ready_quantity']==0
        with factory() as db:
            db.get(InventoryLot,lotid).status='active';reservation=db.scalar(select(InventoryReservation).where(InventoryReservation.order_item_id==1));reservation.status='cancelled';db.commit()
        assert rows(client)[0]['ready_quantity']==0
        bid=saved.json()['id']
        _login(client,'sales')
        assert client.post(f'/api/deliveries/tianhua-backlogs/{bid}/cancel',json=dict(expected_version=1,reason='不能由业务取消')).status_code==403
        _login(client,'admin')
        assert client.post(f'/api/deliveries/tianhua-backlogs/{bid}/cancel',json=dict(expected_version=999,reason='客户取消')).status_code==409
        cancelled=client.post(f'/api/deliveries/tianhua-backlogs/{bid}/cancel',json=dict(expected_version=1,reason='客户取消'));assert cancelled.status_code==200,cancelled.text
        assert rows(client)==[] and rows(client,history=True)[0]['status']=='cancelled'
        with factory() as db:assert db.get(OrderItem,1).quantity==300 and db.get(OrderItem,1).delivered_quantity==0


def test_fifo_po_priority_customer_scope_and_no_new_order_auto_close(requisition_app,monkeypatch):
    from app.models.order import Order,OrderItem
    from app.models.customer import Customer
    from app.services import delivery_backlogs as svc
    app,factory=requisition_app;setup(app,factory)
    with factory() as db:
        db.add(Customer(id=2,name='隔离客户',customer_code='OTHER'))
        db.get(OrderItem,1).quantity=300
        for ident,customer,due in [(2,1,date(2026,7,1)),(3,1,date(2026,8,1)),(4,2,date(2026,1,1))]:
            db.add(Order(id=ident,order_number=f'R09-{ident}',customer_id=customer,order_date=date(2026,1,1),delivery_date=due,status='pending_production',payment_status='unpaid',total_amount=0))
            db.flush();db.add(OrderItem(id=ident,order_id=ident,product_id=1,quantity=300,unit_price=0,subtotal=0,material_status='pending',snapshot_product_name='同款'))
        db.commit()
    with TestClient(app) as client:
        _login(client,'admin')
        for ident,po in [(1,'FIRST'),(2,'MATCH'),(4,'OTHER')]:
            nid,_=notice(factory,item_id=ident,po=po)
            result=client.post(f'/api/deliveries/tianhua-backlogs/defer/{nid}',json=dict(quantity=300,reason='等待'));assert result.status_code==200,result.text
        assert len(rows(client,customer_id=1))==2 # the new order 3 is not a fulfillment
        # Allocation ordering is tested independently of inventory qualification,
        # whose real receipt/frozen/reservation behavior is covered above.
        monkeypatch.setattr(svc,'physical_ready',lambda db,item:(300,0))
        request=dict(customer_id=1,product_id=1,quantity=350)
        first=client.post('/api/deliveries/tianhua-backlogs/suggest',json=request);assert first.status_code==200,first.text
        assert [(r['order_item_id'],r['suggested_quantity']) for r in first.json()['items']]==[(1,300),(2,50)]
        matched=client.post('/api/deliveries/tianhua-backlogs/suggest',json=dict(request,customer_order_no='MATCH'))
        assert [(r['order_item_id'],r['suggested_quantity']) for r in matched.json()['items']]==[(2,300),(1,50)]
        assert all(r['customer_id']==1 for r in matched.json()['items'])
        assert all(r['fulfilled_quantity']==0 for r in rows(client))


def test_backlog_migration_immutable_and_nonempty_downgrade_refused(requisition_app):
    import importlib.util
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError
    from app.models import Base
    app,factory=requisition_app;setup(app,factory);engine=factory.kw['bind']
    spec=importlib.util.spec_from_file_location('r09_migration','alembic/versions/db0919_delivery_backlogs.py')
    migration=importlib.util.module_from_spec(spec);spec.loader.exec_module(migration)
    with engine.begin() as conn:
        for name in ('delivery_backlog_fulfillments','delivery_backlog_sources','delivery_backlogs'):Base.metadata.tables[name].drop(conn)
        migration.op=Operations(MigrationContext.configure(conn));migration.upgrade();migration.downgrade();migration.upgrade()
    with TestClient(app) as client:
        _login(client,'admin');nid,_=notice(factory,quantity=100)
        created=client.post(f'/api/deliveries/tianhua-backlogs/defer/{nid}',json=dict(quantity=100,reason='实际未送'));assert created.status_code==200,created.text
    for sql in ("UPDATE delivery_backlogs SET original_quantity=90","DELETE FROM delivery_backlogs","UPDATE delivery_backlog_sources SET snapshot_json='{}'"):
        with engine.begin() as conn:
            with pytest.raises(IntegrityError):conn.execute(text(sql))
    with engine.begin() as conn:
        migration.op=Operations(MigrationContext.configure(conn))
        with pytest.raises(RuntimeError,match='禁止删除历史'):migration.downgrade()
        assert conn.execute(text('SELECT count(*) FROM delivery_backlogs')).scalar_one()==1


def test_customer_allowlist_applies_to_list_suggest_and_defer(requisition_app):
    from app.models.user import User
    from app.models.access_control import UserPermissionOverride
    app,factory=requisition_app;setup(app,factory)
    with TestClient(app) as client:
        _login(client,'admin');nid,_=notice(factory,quantity=100)
        saved=client.post(f'/api/deliveries/tianhua-backlogs/defer/{nid}',json=dict(quantity=100,reason='等待'));assert saved.status_code==200,saved.text
        with factory() as db:
            sales=db.scalar(select(User).where(User.username=='sales'));sales.customer_access_mode='selected'
            db.add_all([UserPermissionOverride(user_id=sales.id,permission_code=code,is_allowed=True) for code in ('deliveries.view','deliveries.execute')]);db.commit()
        _login(client,'sales')
        result=client.get('/api/deliveries/tianhua-backlogs/list');assert result.status_code==200,result.text
        assert result.json()['items']==[]
        denied=client.post('/api/deliveries/tianhua-backlogs/suggest',json=dict(customer_id=1,product_id=1,quantity=100));assert denied.status_code==403,denied.text
        denied=client.post(f'/api/deliveries/tianhua-backlogs/defer/{nid}',json=dict(quantity=100,reason='越权'));assert denied.status_code==403,denied.text
