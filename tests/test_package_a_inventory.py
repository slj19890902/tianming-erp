import json
import subprocess
from pathlib import Path
from fastapi.testclient import TestClient
from sqlalchemy import event, select, func
from test_semi_finished_order_reservation import b1_app, login, order_item, add_finished_lot, finished_plan
from test_processed_sheet_matching import setup, candidates
from test_semi_finished_lot_eligibility import eligibility_db
from app.api.warehouse import SemiProductCandidatePayload, semi_product_candidates, semi_product_inventory_browser
from app.models.order import Order
from app.models.warehouse_inventory import InventoryReservation


def test_real_vue_request_lifecycle():
    result=subprocess.run(['node','tests/package_a_inventory_harness.cjs'],cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,encoding='utf-8')
    assert result.returncode == 0, result.stdout+result.stderr


def test_default_sql_customer_filter_and_explicit_paging(eligibility_db):
    db,data=eligibility_db
    product,lot,profile,facts=setup(db,data,approved=True)
    payload=SemiProductCandidatePayload(customer_id=product.customer_id,board_length_mm=800,board_width_mm=600,material_code='A416D',flute_type='B',component_type='whole',pieces_per_box=1,stock_yield_per_sheet=1,layer_count=3,crease_type='毛片')
    before=(lot.quantity_available,lot.quantity_reserved,lot.version)
    assert semi_product_candidates(product.id,payload,db,data['admin'])['items']
    facts.update(scope='general',customer_ids=[]);profile.data_json=json.dumps(facts);db.flush()
    sql=[]
    def capture(conn,cursor,statement,parameters,context,many): sql.append(statement)
    event.listen(db.bind,'before_cursor_execute',capture)
    try: assert semi_product_candidates(product.id,payload,db,data['admin'])['items'] == []
    finally: event.remove(db.bind,'before_cursor_execute',capture)
    assert any('json_each' in q and 'inventory_lots' in q for q in sql)
    manual=semi_product_inventory_browser(product.id,payload,page=1,page_size=1,db=db,user=data['admin'])
    assert manual['items'] and manual['total'] == 1
    assert not semi_product_inventory_browser(product.id,payload,page=2,page_size=1,db=db,user=data['admin'])['items']
    facts.update(scope='customers',customer_ids=[99999]);profile.data_json=json.dumps(facts);db.flush()
    assert not semi_product_candidates(product.id,payload,db,data['admin'])['items']
    assert before==(lot.quantity_available,lot.quantity_reserved,lot.version)


def test_order_replay_and_mismatched_payload(b1_app):
    app,factory=b1_app
    with TestClient(app) as client:
        login(client,'admin')
        payload={'customer_id':1,'idempotency_key':'package-a-repeat-001','items':[order_item(1,10,{'finished':[],'semi':[]})]}
        first=client.post('/api/orders',json=payload);assert first.status_code==201,first.text
        repeat=client.post('/api/orders',json=payload);assert repeat.status_code==201,repeat.text
        assert repeat.json()['id']==first.json()['id']
        payload['items'][0]['quantity']=11
        assert client.post('/api/orders',json=payload).status_code==409
        with factory() as db:
            assert db.scalar(select(func.count()).select_from(Order))==1
            assert db.scalar(select(func.count()).select_from(InventoryReservation))==0


def test_finished_selection_concurrency_and_replay(b1_app):
    app,factory=b1_app
    lot,version=add_finished_lot(factory,product_id=1,quantity=10,key='package-a-finished')
    with TestClient(app) as client:
        login(client,'admin')
        payload={'customer_id':1,'idempotency_key':'package-a-stock-001','items':[order_item(1,10,{'finished':[finished_plan(lot,version,10)],'semi':[]})]}
        first=client.post('/api/orders',json=payload);assert first.status_code==201,first.text
        assert client.post('/api/orders',json=payload).json()['id']==first.json()['id']
        payload['idempotency_key']='package-a-stock-002'
        assert client.post('/api/orders',json=payload).status_code==409
        with factory() as db:
            assert db.scalar(select(func.count()).select_from(Order))==1
            assert db.scalar(select(func.count()).select_from(InventoryReservation))==1


def test_concurrent_retries_create_once(b1_app):
    from concurrent.futures import ThreadPoolExecutor
    app,factory=b1_app
    with TestClient(app) as client:
        login(client,'admin')
        payload={'customer_id':1,'idempotency_key':'package-a-concurrent','items':[order_item(1,10)]}
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies=list(pool.map(lambda _: client.post('/api/orders',json=payload),range(2)))
        assert all(r.status_code==201 for r in replies), [r.text for r in replies]
        assert replies[0].json()['id']==replies[1].json()['id']
        from app.models.requisition import Requisition
        with factory() as db:
            assert db.scalar(select(func.count()).select_from(Order))==1
            assert db.scalar(select(func.count()).select_from(Requisition))==0


def test_late_failure_rolls_back_order_and_replay_record(b1_app,monkeypatch):
    import app.api.orders as api
    from app.models.audit import OperationLog
    app,factory=b1_app
    original=api._order_response
    def fail(*args,**kwargs): raise RuntimeError('injected response failure')
    with TestClient(app) as client:
        login(client,'admin')
        payload={'customer_id':1,'idempotency_key':'package-a-rollback','items':[order_item(1,10)]}
        monkeypatch.setattr(api,'_order_response',fail)
        assert client.post('/api/orders',json=payload).status_code==500
        with factory() as db:
            assert db.scalar(select(func.count()).select_from(Order))==0
            assert db.scalar(select(func.count()).select_from(OperationLog).where(OperationLog.action=='order_create_replay'))==0
        monkeypatch.setattr(api,'_order_response',original)
        assert client.post('/api/orders',json=payload).status_code==201


def test_other_customer_browse_is_read_only_and_permission_scoped(b1_app):
    from test_semi_finished_order_reservation import add_semi_lot
    app,factory=b1_app
    lot,_=add_semi_lot(factory,quantity=10,key='other-browse',customer_id=2,bind_product=False)
    payload=dict(customer_id=1,board_length_mm=800,board_width_mm=600,material_code='A416D',flute_type='B',component_type='whole',pieces_per_box=1,stock_yield_per_sheet=1)
    with TestClient(app) as client:
        login(client,'admin')
        assert not client.post('/api/warehouse/semi-finished/products/1/candidates',json=payload).json()['items']
        response=client.post('/api/warehouse/semi-finished/products/1/inventory?page=1&page_size=20',json=payload)
        assert response.status_code==200,response.text
        candidate=next(row for row in response.json()['items'] if row['lot_id']==lot)
        assert candidate['selectable'] is False
        assert client.post('/api/warehouse/semi-finished/products/1/candidates',json={**payload,'customer_id':0}).status_code != 200
        from app.models.user import User
        from app.models.access_control import UserCustomerScope
        with factory() as db:
            user=db.scalar(select(User).where(User.username=='sales'))
            user.customer_access_mode='selected'
            db.add(UserCustomerScope(user_id=user.id,customer_id=1));db.commit()
        client.cookies.clear()
        login(client,'sales')
        assert not client.post('/api/warehouse/semi-finished/products/1/inventory?page=1&page_size=20',json=payload).json()['items']
        with factory() as db:
            assert db.scalar(select(func.count()).select_from(InventoryReservation))==0
