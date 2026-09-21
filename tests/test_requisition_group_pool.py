"""One confirmation allocates shared pallets over multiple printed-product orders."""
import json
import pytest
import subprocess
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.models.warehouse_goods import WarehouseGoodsProfile
from app.models.warehouse_inventory import InventoryLot, InventoryReservation
from test_semi_finished_order_reservation import b1_app, login, post_order, order_item, add_semi_lot
from test_requisition_deduction_ui import preview


def test_pool_frontend_behavior():
    subprocess.run(['node','tests/requisition_group_pool_ui.cjs'],check=True,capture_output=True,text=True)


def setup_pool(client, factory, quantities=(224,112), stocks=(334,100)):
    login(client)
    response=post_order(client,[order_item(i+1,q,None,f'POOL-{i}') for i,q in enumerate(quantities)],'POOL-ORDER')
    assert response.status_code==201,response.text
    ids=[r['id'] for r in response.json()['items']]
    lots=[]
    for i,q in enumerate(stocks):
        lot,version=add_semi_lot(factory,quantity=q,key=f'POOL-LOT-{i}',customer_id=None,bind_product=False)
        with factory() as db:
            db.add(WarehouseGoodsProfile(lot_id=lot,data_json=json.dumps({
                'scope':'general','customer_ids':[],'product_ids':[], 'processing':'raw',
                'material_code':'A416D','material_confidence':'confirmed','face_paper':'kraft',
                'mold_tool_id':None,'verified_material_id':None})))
            db.commit()
        lots.append({'lot_id':lot,'expected_version':version})
    return ids, {'allocation_mode':'pooled','idempotency_key':'POOL-CONFIRM','items':[
        {'order_item_id':iid,'component_type':'whole','requested_requirement_quantity':q,
         'board_length_mm':800,'board_width_mm':600,'lots':lots}
        for iid,q in zip(ids,quantities)]}


def test_one_pool_spans_orders_and_pallets_and_replays(b1_app):
    app,factory=b1_app
    with TestClient(app) as client:
        ids,payload=setup_pool(client,factory)
        payload['items'].reverse()  # UI order must not choose which order gets covered first.
        result=client.post('/api/requisition/semi-inventory/reserve-safe-batch',json=payload)
        assert result.status_code==200,result.text
        assert result.json()['allocated_requirement_quantity']==336
        with factory() as db:
            rows=db.scalars(select(InventoryReservation).order_by(InventoryReservation.id)).all()
            assert [(r.order_item_id,r.reserved_stock_quantity) for r in rows]==[(ids[0],224),(ids[1],110),(ids[1],2)]
            assert sum(db.get(InventoryLot,l['lot_id']).quantity_available for l in payload['items'][0]['lots'])==98
        replay=client.post('/api/requisition/semi-inventory/reserve-safe-batch',json=payload)
        assert replay.status_code==200 and replay.json()==result.json()
        payload['items'][0]['requested_requirement_quantity']+=1
        assert client.post('/api/requisition/semi-inventory/reserve-safe-batch',json=payload).status_code==409


def test_pool_shortfall_is_automatic_not_an_error(b1_app):
    app,factory=b1_app
    with TestClient(app) as client:
        ids,payload=setup_pool(client,factory,stocks=(100,200))
        result=client.post('/api/requisition/semi-inventory/reserve-safe-batch',json=payload)
        assert result.status_code==200,result.text
        assert result.json()['allocated_requirement_quantity']==300
        assert result.json()['remaining_requirement_quantity']==36
        assert [row['remaining_requirement_quantity'] for row in result.json()['items']]==[0,36]


def test_pool_stale_later_pallet_rolls_back_every_order(b1_app):
    app,factory=b1_app
    with TestClient(app) as client:
        ids,payload=setup_pool(client,factory)
        with factory() as db:
            db.get(InventoryLot,payload['items'][0]['lots'][1]['lot_id']).version+=1
            db.commit()
        result=client.post('/api/requisition/semi-inventory/reserve-safe-batch',json=payload)
        assert result.status_code==409,result.text
        with factory() as db:
            assert not db.scalars(select(InventoryReservation)).all()


def test_explicit_general_customer_difference_is_pool_safe(b1_app):
    app,factory=b1_app
    with TestClient(app) as client:
        ids,payload=setup_pool(client,factory)
        options=preview(client,ids[0])
        assert all(row['safe_group_eligible'] for row in options['recommended_candidates'])


@pytest.mark.parametrize('change',['flute','dimensions','material','printed','customer','unknown_scope'])
def test_forged_pool_cannot_bypass_physical_or_scope_qualification(b1_app,change):
    app,factory=b1_app
    with TestClient(app) as client:
        ids,payload=setup_pool(client,factory)
        with factory() as db:
            lot=db.get(InventoryLot,payload['items'][0]['lots'][0]['lot_id'])
            profile=db.get(WarehouseGoodsProfile,lot.id)
            data=json.loads(profile.data_json)
            if change=='flute': lot.semi_finished_detail.flute_type='E'
            if change=='dimensions': lot.semi_finished_detail.board_length_mm=1600
            if change=='material':
                lot.semi_finished_detail.material_code_snapshot='A999D'
                lot.semi_finished_detail.normalized_material_code='A999D'
                data['material_code']='A999D'
            if change=='printed': data.update(processing='printed',product_ids=[1])
            if change=='customer': data.update(scope='customers',customer_ids=[2])
            if change=='unknown_scope': db.delete(profile)
            else: profile.data_json=json.dumps(data)
            db.commit()
        result=client.post('/api/requisition/semi-inventory/reserve-safe-batch',json=payload)
        assert result.status_code==409,result.text
        with factory() as db:
            assert not db.scalars(select(InventoryReservation)).all()


def test_pool_audit_failure_rolls_back_all_stock(b1_app,monkeypatch):
    import app.api.requisition as api
    app,factory=b1_app
    with TestClient(app,raise_server_exceptions=False) as client:
        ids,payload=setup_pool(client,factory)
        original=api._audit
        def fail(db,**kw):
            if kw.get('action')=='RESERVE_REQUISITION_SAFE_BATCH': raise RuntimeError('injected audit failure')
            return original(db,**kw)
        monkeypatch.setattr(api,'_audit',fail)
        result=client.post('/api/requisition/semi-inventory/reserve-safe-batch',json=payload)
        assert result.status_code==500
        with factory() as db:
            assert not db.scalars(select(InventoryReservation)).all()
            assert [db.get(InventoryLot,l['lot_id']).quantity_available for l in payload['items'][0]['lots']]==[334,100]


def test_known_blank_die_cut_pool_can_supply_different_product_codes(b1_app):
    app,factory=b1_app
    with TestClient(app) as client:
        ids,payload=setup_pool(client,factory)
        with factory() as db:
            for choice in payload['items'][0]['lots']:
                profile=db.get(WarehouseGoodsProfile,choice['lot_id'])
                data=json.loads(profile.data_json)
                data.update(processing='die_cut',blank_unprinted=True,product_ids=[1,2])
                profile.data_json=json.dumps(data)
            db.commit()
        result=client.post('/api/requisition/semi-inventory/reserve-safe-batch',json=payload)
        assert result.status_code==200,result.text
        assert result.json()['allocated_requirement_quantity']==336


def test_pool_concurrent_retry_has_one_receipt_and_one_reservation_set(b1_app):
    from concurrent.futures import ThreadPoolExecutor
    app,factory=b1_app
    with TestClient(app) as client:
        ids,payload=setup_pool(client,factory)
        cookies=dict(client.cookies)
        def send(_):
            with TestClient(app) as worker:
                worker.cookies.update(cookies)
                return worker.post('/api/requisition/semi-inventory/reserve-safe-batch',json=payload)
        with ThreadPoolExecutor(max_workers=2) as workers:
            results=list(workers.map(send,range(2)))
        assert [r.status_code for r in results]==[200,200],[r.text for r in results]
        assert results[0].json()==results[1].json()
        with factory() as db:
            assert sum(r.reserved_stock_quantity for r in db.scalars(select(InventoryReservation)))==336
