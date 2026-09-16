from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from app.api import mobile_stock_use
from app.models.warehouse_inventory import InventoryLot, InventoryMovement, SemiFinishedInventoryDetail, WarehouseLocation
from app.models.audit import OperationLog
from tests.test_n035_stocktake_api import stocktake_api, _login

BASE = '/api/mobile/erp/warehouse/dimension-stock'


@pytest.fixture
def stock_api(stocktake_api):
    app, factory, ids = stocktake_api
    app.include_router(mobile_stock_use.router, prefix='/api/mobile/erp')
    with factory() as db:
        lot = db.get(InventoryLot, ids['semi_lot'])
        lot.semi_finished_detail = SemiFinishedInventoryDetail(
            owner_customer_id=db.get(InventoryLot, ids['lot1']).finished_detail.owner_customer_id,
            internal_name='原板', material_code_snapshot='H', normalized_material_code='H',
            layer_count=1, flute_type='NONE', board_length_mm=900, board_width_mm=600,
            sheet_type='raw_board', pieces_per_box=1, stock_yield_per_sheet=1)
        db.commit()
    return app, factory, ids


def body(factory, ids, **changes):
    with factory() as db:
        lot = db.get(InventoryLot, ids['lot1'])
        result = dict(quantity=3, purpose='sample', expected_version=lot.version,
                      location_id=lot.warehouse_location_id, address_version=lot.location.address_version,
                      idempotency_key='mobile_take_test_123456')
        result.update(changes)
        return result


def test_search_all_dimensions_flute_scope_pagination(stock_api):
    app, factory, ids = stock_api
    with factory() as db:
        lot = db.get(InventoryLot, ids['other_lot'])
        lot.finished_detail.length_mm = 510
        db.commit()
    with TestClient(app) as client:
        assert client.get(BASE+'?length=500').status_code == 401
        _login(client, 'n035-admin')
        data = client.get(BASE+'?kind=box&length=500&width=300&height=200&limit=2').json()
        assert data['total'] == 3
        assert [row['id'] for row in data['items']] == [ids['lot1'],ids['lot2']]
        assert data['items'][0]['physical'] == 12
        assert data['items'][0]['unit'] == '只'
        assert data['items'][1]['can_take'] is False
        assert client.get(BASE+'?kind=box&length=500&offset=2&limit=2').json()['items'][0]['id'] == ids['other_lot']
        assert client.get(BASE+'?kind=box&length=500&length_op=eq').json()['total']==2
        assert client.get(BASE+'?kind=box&length=509&length_op=le').json()['total']==2
        assert client.get(BASE+'?kind=box&length=300&width=500&length_op=eq&width_op=eq').json()['total']==0
        board=client.get(BASE+'?kind=board&length=900&flute=NONE').json()['items'][0]
        assert board['id']==ids['semi_lot'] and board['unit']=='张'
        assert client.get(BASE+'?kind=board&flute=B').json()['total']==0
        _login(client, 'n035-restricted')
        assert client.get(BASE+'?kind=box&length=1').json()['total']==0
        assert client.get(BASE+f"/{ids['lot1']}/history").status_code==403


@pytest.mark.parametrize('query', ['length=100000','length=-1','length=1.2','length=1e3','length=abc','length=0','height=2','length=2&length_op=bad',''])
def test_invalid_dimensions(stock_api, query):
    app, _, _ = stock_api
    with TestClient(app) as client:
        _login(client, 'n035-admin')
        assert client.get(BASE+'?'+query).status_code==422


@pytest.mark.parametrize('kind', ['finished','board'])
def test_take_conserves_identity_reservations_replay_history(stock_api, kind):
    app, factory, ids = stock_api
    lot_id=ids['lot1'] if kind=='finished' else ids['semi_lot']
    with factory() as db:
        lot=db.get(InventoryLot,lot_id)
        before=(lot.quantity_available,lot.quantity_reserved,lot.quantity_consumed,lot.quantity_damaged,lot.quantity_scrapped)
        version=lot.version
    payload=body(factory,ids,expected_version=version)
    with TestClient(app) as client:
        _login(client,'n035-admin')
        url=BASE+f'/{lot_id}/take'
        first=client.post(url,json=payload)
        assert first.status_code==200,first.text
        repeat=client.post(url,json=payload)
        assert repeat.status_code==200 and repeat.json()['replayed']
        assert repeat.json()['movement_id']==first.json()['movement_id']
        assert client.post(url,json={**payload,'purpose':'cash'}).status_code==409
        assert client.post(url,json={**payload,'idempotency_key':'different_request_123456'}).status_code==409
        history=client.get(BASE+f'/{lot_id}/history').json()['items']
        assert len(history)==1 and history[0]['account']=='n035-admin' and history[0]['purpose']=='免费打样'
    with factory() as db:
        lot=db.get(InventoryLot,lot_id)
        assert (lot.quantity_available,lot.quantity_reserved,lot.quantity_consumed,lot.quantity_damaged,lot.quantity_scrapped)==(before[0]-3,before[1],before[2]+3,before[3],before[4])
        assert lot.version==version+1 and lot.warehouse_location_id==ids['location']
        movement=db.scalar(select(InventoryMovement))
        assert movement.related_order_id is None and movement.related_delivery_id is None and movement.reservation_id is None
        assert db.scalar(select(func.count(OperationLog.id)).where(OperationLog.action_code=='warehouse.mobile_stock.take'))==1


@pytest.mark.parametrize('changes', [{'quantity':11},{'quantity':0},{'quantity':1.5},{'quantity':True},{'expected_version':2},{'location_id':999},{'address_version':999},{'purpose':'sale'},{'idempotency_key':'x'}])
def test_reject_invalid_take_without_writes(stock_api,changes):
    app,factory,ids=stock_api
    with TestClient(app) as client:
        _login(client,'n035-admin')
        result=client.post(BASE+f"/{ids['lot1']}/take",json=body(factory,ids,**changes))
        assert result.status_code in (409,422),result.text
    with factory() as db:
        assert db.get(InventoryLot,ids['lot1']).quantity_available==10
        assert db.scalar(select(func.count(InventoryMovement.id)))==0


def test_frozen_inactive_scope_and_permission(stock_api):
    app,factory,ids=stock_api
    payload=body(factory,ids)
    with TestClient(app) as client:
        _login(client,'n035-restricted')
        assert client.post(BASE+f"/{ids['lot1']}/take",json=payload).status_code==403
        _login(client,'n035-admin')
        assert client.post(BASE+f"/{ids['lot2']}/take",json={**payload,'expected_version':5}).status_code==409
        with factory() as db:
            db.get(WarehouseLocation,ids['location']).is_active=False
            db.commit()
        assert client.post(BASE+f"/{ids['lot1']}/take",json=payload).status_code==409


def test_audit_failure_rolls_back_stock_and_movement(stock_api,monkeypatch):
    app,factory,ids=stock_api
    def fail(*args,**kwargs):raise RuntimeError('audit-injected')
    monkeypatch.setattr(mobile_stock_use,'append_audit_event',fail)
    with TestClient(app) as client:
        _login(client,'n035-admin')
        with pytest.raises(RuntimeError,match='audit-injected'):
            client.post(BASE+f"/{ids['lot1']}/take",json=body(factory,ids))
    with factory() as db:
        assert db.get(InventoryLot,ids['lot1']).quantity_available==10
        assert db.scalar(select(func.count(InventoryMovement.id)))==0


def test_mobile_sections_and_buttons():
    root=Path(__file__).resolve().parents[1]
    html=(root/'static/mobile_erp.html').read_text(encoding='utf-8')
    js=(root/'static/ui/mobile-dimension-stock.js').read_text(encoding='utf-8')
    assert html.index('id="dimensionStock"')<html.index('id="lookupInput"')
    assert 'TmDimensionStock?.mount({apiGet, apiPost})' in html
    assert 'maxlength="5"' in js and 'inputmode="numeric"' in js
    assert '.ds-take{background:#b91c1c' in js and '.ds-cancel{background:#15803d' in js
    assert 'get("dsCancel").onclick=close' in js


@pytest.mark.parametrize('same_key',[True,False])
def test_concurrent_requests_do_not_double_consume(stock_api,same_key):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    app,factory,ids=stock_api
    payload=body(factory,ids,quantity=10)
    clients=[TestClient(app),TestClient(app)]
    for client in clients:_login(client,'n035-admin')
    gate=Barrier(2)
    def send(index):
        gate.wait()
        data={**payload}
        if index and not same_key:data['idempotency_key']='other_concurrent_request'
        return clients[index].post(BASE+f"/{ids['lot1']}/take",json=data)
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(send,[0,1]))
    assert sorted(r.status_code for r in results)==([200,200] if same_key else [200,409])
    with factory() as db:
        lot=db.get(InventoryLot,ids['lot1'])
        assert lot.quantity_available==0 and lot.quantity_reserved==2 and lot.quantity_consumed==11
        assert db.scalar(select(func.count(InventoryMovement.id)))==1
    for client in clients:client.close()


def test_empty_lot_receipt_accessible_and_execute_permission_enforced(stock_api):
    from app.models.user import User
    app,factory,ids=stock_api
    with TestClient(app) as client:
        _login(client,'n035-admin')
        result=client.post(BASE+f"/{ids['lot1']}/take",json=body(factory,ids,quantity=10))
        assert result.status_code==200,result.text
        assert client.get(BASE+f"/{ids['lot1']}/detail").json()['item']['available']==0
        with factory() as db:
            db.get(User,ids['workshop']).role='finance'
            db.commit()
        _login(client,'n035-workshop')
        assert client.post(BASE+f"/{ids['lot1']}/take",json=body(factory,ids)).status_code==403


def test_pallet_projection_and_bom_identity_are_preserved(stock_api):
    from app.models.warehouse_inventory import InventoryPallet, InventoryPalletItem
    from app.models.product_bom import ProductBomComponent
    from app.models.product import Product
    app,factory,ids=stock_api
    with factory() as db:
        lot=db.get(InventoryLot,ids['lot1'])
        detail=lot.finished_detail
        child=Product(customer_id=detail.owner_customer_id,product_code='CHILD',customer_material_code='CHILD',product_name='子件',box_category='normal')
        db.add(child);db.flush()
        bom=ProductBomComponent(parent_product_id=detail.product_id,component_product_id=child.id,
                                quantity_per_set=4,display_order=0,internal_component_code='C1',is_die_cut=False)
        pallet=InventoryPallet(pallet_code='TAKE-PALLET',location_id=lot.warehouse_location_id)
        db.add_all([pallet,bom]);db.flush()
        db.add(InventoryPalletItem(pallet_id=pallet.id,inventory_lot_id=lot.id,
                                  customer_id=detail.owner_customer_id,product_id=detail.product_id,
                                  item_type='finished',quantity=12,unit='boxes',match_status='matched'))
        lot.cost_snapshot_detail_json='{"source":"unchanged"}'
        detail.physical_basis_json='{"frozen":"unchanged"}'
        db.commit();bom_id=bom.id
    with TestClient(app) as client:
        _login(client,'n035-admin')
        result=client.post(BASE+f"/{ids['lot1']}/take",json=body(factory,ids))
        assert result.status_code==200,result.text
    with factory() as db:
        lot=db.get(InventoryLot,ids['lot1'])
        assert lot.pallet_item.quantity==9 and lot.pallet_item.pallet.version==2
        assert lot.cost_snapshot_detail_json=='{"source":"unchanged"}'
        assert lot.finished_detail.physical_basis_json=='{"frozen":"unchanged"}'
        assert db.get(ProductBomComponent,bom_id).quantity_per_set==4
        assert db.get(InventoryLot,ids['other_lot']).quantity_available==7
