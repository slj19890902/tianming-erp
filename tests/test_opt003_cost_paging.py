from datetime import date
from decimal import Decimal
from types import SimpleNamespace
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, insert, select

from app.api import warehouse
from app.api.deps import get_db
from app.models.warehouse_inventory import InventoryLot, FinishedGoodsInventoryDetail, SemiFinishedInventoryDetail, WarehouseLocation
from app.services.inventory_valuation import cost_payload
from app.services.warehouse_inventory import manual_finished_in
from tests.test_inventory_valuation import db, product


@pytest.fixture
def cost_app(db, monkeypatch):
    item, _ = product(db)
    location = WarehouseLocation(location_code='OPT003', location_name='三楼 南A1', warehouse_type='finished')
    db.add(location); db.flush()
    seed = manual_finished_in(db, customer_id=item.customer_id, product_id=item.id, location_id=location.id,
        quantity=1, stock_date=date(2026,9,12), source_type='stocktake', remarks=None,
        operator_id=None, idempotency_key='opt003-seed')
    seed.estimated_unit_cost_snapshot = Decimal('0.0050')
    seed.cost_snapshot_source = 'inventory_confirmed_material'
    seed.cost_snapshot_detail_json = '{"currency":"CNY"}'
    seed.quantity_available=1; seed.quantity_reserved=1; seed.quantity_damaged=1
    db.flush()
    raw = {c.name:getattr(seed,c.name) for c in InventoryLot.__table__.columns if c.name!='id'}
    detail = {c.name:getattr(seed.finished_detail,c.name) for c in FinishedGoodsInventoryDetail.__table__.columns if c.name!='inventory_lot_id'}
    db.execute(insert(InventoryLot), [{**raw,'id':1000+i,'lot_number':f'OPT003-{i}'} for i in range(69)])
    db.execute(insert(FinishedGoodsInventoryDetail), [{**detail,'inventory_lot_id':1000+i,
        'inventory_code_snapshot':f'COST-{i:03d}', 'product_name_snapshot':'后页纸箱' if i>=65 else '普通纸箱'} for i in range(69)])
    db.commit(); db.expunge_all()
    app=FastAPI(); app.include_router(warehouse.router,prefix='/api/warehouse')
    app.dependency_overrides[get_db]=lambda:db
    app.dependency_overrides[warehouse.can_read]=lambda:SimpleNamespace(role='admin',is_active=True)
    monkeypatch.setattr(warehouse,'_visible_customer_ids',lambda *_:None)
    with TestClient(app) as client:
        yield client, db, app


def test_cost_pages_bound_details_and_keep_whole_stock_rounded_total(cost_app):
    client, db, _ = cost_app
    loaded=[]; statements=[]
    def record(_session, obj):
        if isinstance(obj,InventoryLot): loaded.append(obj.id)
    def sql(_c,_cursor,statement,_params,_context,_many): statements.append(statement)
    event.listen(db,'loaded_as_persistent',record)
    event.listen(db.bind,'before_cursor_execute',sql)
    try:
        first=client.get('/api/warehouse/costs').json()
    finally:
        event.remove(db,'loaded_as_persistent',record)
        event.remove(db.bind,'before_cursor_execute',sql)
    assert len(first['rows'])==50
    assert len(loaded)<=50
    assert first['total_lots']==70 and first['total']==70
    assert Decimal(first['inventory_value'])==Decimal('1.40')  # round each 0.015 to 0.02, then sum
    assert first['page']==1 and first['has_more']
    second=client.get('/api/warehouse/costs',params={'page':2}).json()
    assert len(second['rows'])==20 and not second['has_more']
    assert second['inventory_value']==first['inventory_value']
    assert len({r['lot_id'] for r in first['rows']+second['rows']})==70
    assert not any(s.lstrip().upper().startswith(('INSERT','UPDATE','DELETE')) for s in statements)
    assert not any('inventory_pallet_items' in s or 'semi_finished_lot_allowed_products' in s for s in statements)


def test_search_hits_later_pages_and_summary_only_loads_no_lot_entities(cost_app):
    client,db,_=cost_app
    for query in ('后页','cost-065','三楼 南A1'):
        data=client.get('/api/warehouse/costs',params={'keyword':query,'page_size':2}).json()
        assert data['total']==(4 if query=='后页' else 1 if query=='cost-065' else 70)
        assert len(data['rows'])<=2 and data['total_lots']==70
        assert data['inventory_value']=='1.40'
    db.expunge_all(); loaded=[]
    def record(_session,obj):
        if isinstance(obj,InventoryLot): loaded.append(obj.id)
    event.listen(db,'loaded_as_persistent',record)
    try: data=client.get('/api/warehouse/costs',params={'summary_only':True}).json()
    finally: event.remove(db,'loaded_as_persistent',record)
    assert data['rows']==[] and loaded==[] and data['inventory_value']=='1.40'


@pytest.mark.parametrize('params',[{'page':0},{'page_size':0},{'page_size':201},{'keyword':'x'*151}])
def test_invalid_paging_is_rejected(cost_app,params):
    client,_,_=cost_app
    assert client.get('/api/warehouse/costs',params=params).status_code==422


def test_scope_and_empty_page_never_expose_other_costs(cost_app,monkeypatch):
    client,db,_=cost_app
    monkeypatch.setattr(warehouse,'_visible_customer_ids',lambda *_:set())
    data=client.get('/api/warehouse/costs',params={'page':2}).json()
    assert data['rows']==[] and data['total_lots']==0 and data['inventory_value']=='0'
    monkeypatch.setattr(warehouse,'_visible_customer_ids',lambda *_:None)
    data=client.get('/api/warehouse/costs',params={'location_id':999999,'page_size':1}).json()
    assert data['total']==0 and not data['has_more']
    data=client.get('/api/warehouse/costs',params={'page':999}).json()
    assert data['rows']==[] and data['total']==70 and data['inventory_value']=='1.40'


def test_summary_reuses_frozen_currency_lineage_and_old_dimension_checks(cost_app):
    client,db,_=cost_app
    lots=list(db.scalars(select(InventoryLot).order_by(InventoryLot.id)))
    lots[0].cost_snapshot_detail_json='bad-json'
    lots[1].cost_snapshot_detail_json='{"currency":"USD"}'
    lots[2].cost_snapshot_source='material_quote_area'
    lots[2].cost_snapshot_detail_json=json.dumps({'currency':'CNY','components':[{'component':'whole','length_mm':1,'width_mm':1}]})
    lots[2].finished_detail.length_mm=500
    lots[3].cost_snapshot_detail_json=json.dumps({'source_semi_inventory_lot_id':lots[4].id})
    lots[5].cost_snapshot_detail_json=json.dumps({'source_semi_inventory_lot_id':lots[5].id})
    lots[6].cost_snapshot_source='owner_current_reference_backfill'
    lots[6].cost_snapshot_detail_json='{"currency":"CNY"}'  # no owner authorization
    db.commit()
    expected=[cost_payload(lot,db) for lot in lots]
    total=sum((Decimal(r['inventory_value']) for r in expected if r['inventory_value'] is not None),Decimal(0))
    response=client.get('/api/warehouse/costs',params={'page_size':200})
    assert response.status_code==200,response.text
    actual=response.json()
    assert Decimal(actual['inventory_value'])==total
    assert actual['missing_lots']==sum(r['inventory_value'] is None for r in expected)==5
    assert [(r['lot_id'],r['unit_cost'],r['inventory_value'],r['validation_issue']) for r in actual['rows']]==[
        (r['lot_id'],r['unit_cost'],r['inventory_value'],r['validation_issue']) for r in expected]
    assert 'no-store' in response.headers['cache-control']


def test_nonempty_customer_scope_preserves_real_owner_binding(cost_app,monkeypatch):
    client,db,_=cost_app
    detail=db.get(FinishedGoodsInventoryDetail,1000)
    owner=detail.owner_customer_id
    detail.owner_customer_id=None;detail.is_general=True;db.commit()
    monkeypatch.setattr(warehouse,'_visible_customer_ids',lambda *_:{owner})
    data=client.get('/api/warehouse/costs',params={'page_size':200}).json()
    assert data['total_lots']==69 and all(row['lot_id']!=1000 for row in data['rows'])
    assert data['inventory_value']=='1.38'


def test_material_name_search_and_zero_physical_stock_exclusion(cost_app):
    client,db,_=cost_app
    source=db.get(InventoryLot,1000)
    raw={c.name:getattr(source,c.name) for c in InventoryLot.__table__.columns if c.name!='id'}
    db.execute(insert(InventoryLot),[{**raw,'id':9000,'lot_number':'OPT003-MATERIAL','inventory_type':'semi_finished','unit':'sheets'}])
    db.add(SemiFinishedInventoryDetail(inventory_lot_id=9000,layer_count=3,flute_type='B',
        board_length_mm=1000,board_width_mm=500,internal_name='通用衬板',
        material_code_snapshot='CCC',normalized_material_code='CCC',sheet_type='raw_board'))
    source.quantity_available=0;source.quantity_reserved=0;source.quantity_damaged=0
    db.commit()
    response=client.get('/api/warehouse/costs',params={'keyword':'通用衬板'})
    assert response.status_code==200,response.text
    data=response.json()
    assert data['total_lots']==70 and data['total']==1
    assert data['rows'][0]['lot_id']==9000 and data['rows'][0]['unit']=='sheets'
    assert data['inventory_value']=='1.40'


def test_large_population_keeps_only_page_entities_and_one_scalar_summary_query(cost_app):
    client,db,_=cost_app
    source=db.get(InventoryLot,1000)
    raw={c.name:getattr(source,c.name) for c in InventoryLot.__table__.columns if c.name!='id'}
    detail={c.name:getattr(source.finished_detail,c.name) for c in FinishedGoodsInventoryDetail.__table__.columns if c.name!='inventory_lot_id'}
    db.execute(insert(InventoryLot),[{**raw,'id':10000+i,'lot_number':f'LARGE-{i}'} for i in range(5000)])
    db.execute(insert(FinishedGoodsInventoryDetail),[{**detail,'inventory_lot_id':10000+i} for i in range(5000)])
    db.commit();db.expunge_all();loaded=[];sql=[]
    def record(_session,obj):
        if isinstance(obj,InventoryLot):loaded.append(obj.id)
    def statement(_c,_cur,s,_p,context,_many):sql.append((s,context.execution_options.get('yield_per')))
    event.listen(db,'loaded_as_persistent',record);event.listen(db.bind,'before_cursor_execute',statement)
    try:data=client.get('/api/warehouse/costs').json()
    finally:
        event.remove(db,'loaded_as_persistent',record);event.remove(db.bind,'before_cursor_execute',statement)
    assert data['total_lots']==5070 and data['inventory_value']=='101.40'
    assert len(data['rows'])==50 and len(loaded)==50
    assert len(sql)<=5 and sql[0][1]==200
