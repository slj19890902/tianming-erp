"""Production output may share a valid storage cell; lots remain independent."""
import copy

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func

from app.models.customer import Customer
from app.models.product import Product
from app.models.production import ProductionCompletion, ProductionCompletionBatch
from app.models.receipt_putaway import ProductStoragePreference
from app.models.user import User
from app.models.warehouse_inventory import InventoryLot, WarehouseLocation
from tests.test_n029_production_service import production_app, _login, _complete, _finished_lot
from tests.test_p1_134_production_storage_routing import _seed_production_customer_areas


def formal_cells(production_app, monkeypatch, *, preference=False):
    app,factory,ids=production_app
    with factory() as db:
        actor=db.scalar(select(User.id).where(User.username=='n029-admin'))
    cells=_seed_production_customer_areas(factory,monkeypatch,
        customer_id=ids['customer_a'],actor_id=actor,
        specs=(('CUSTOMER','rack',False),('PRODUCT','rack',False)))
    with factory() as db:
        area,location=cells['PRODUCT' if preference else 'CUSTOMER']
        customer=db.get(Customer,ids['customer_a']);product=db.get(Product,ids['product_a'])
        old=_finished_lot(db,key='OCCUPIED-CELL',customer=customer,product=product,
            location=db.get(WarehouseLocation,location),quantity_available=11)
        if preference:
            db.add(ProductStoragePreference(product_id=product.id,area_id=area,location_id=location,version=1))
        db.commit()
        return app,factory,ids,cells,old.id


def snapshot(factory,lot_id):
    with factory() as db:
        return tuple(db.execute(select(*InventoryLot.__table__.columns).where(InventoryLot.id==lot_id)).one())


def test_production_can_add_distinct_lot_to_occupied_cell_and_replay(production_app,monkeypatch):
    app,factory,ids,cells,old_id=formal_cells(production_app,monkeypatch)
    old=snapshot(factory,old_id)
    with TestClient(app) as c:
        _login(c)
        r=_complete(c,ids,'stock',idempotency_key='occupied-production',disposition='stock',
            location_id=cells['CUSTOMER'][1],expected_layout_version=1)
        assert r.status_code==200,r.text
        assert _complete(c,ids,'stock',idempotency_key='occupied-production',disposition='stock',
            location_id=cells['CUSTOMER'][1],expected_layout_version=1).status_code==200
    assert snapshot(factory,old_id)==old
    with factory() as db:
        completions=list(db.scalars(select(ProductionCompletion).where(ProductionCompletion.task_id==ids['cases']['stock']['task'])))
        assert len(completions)==1
        new=db.get(InventoryLot,completions[0].inventory_lot_id)
        assert new.id!=old_id and new.warehouse_location_id==cells['CUSTOMER'][1]
        assert new.quantity_available+new.quantity_reserved==completions[0].quantity


def test_product_default_overrides_customer_area_and_is_projected(production_app,monkeypatch):
    app,factory,ids,cells,old_id=formal_cells(production_app,monkeypatch,preference=True)
    with TestClient(app) as c:
        _login(c)
        listing=c.get('/api/production/tasks',params={'status':'pending','page':1,'page_size':50})
        assert listing.status_code==200,listing.text
        rows=listing.json();rows=rows.get('items',rows) if isinstance(rows,dict) else rows
        row=next(row for row in rows if row['id']==ids['cases']['stock']['task'])
        default=row['production_default_storage']
        assert default['configured'] and default['source']=='product_location'
        assert default['location_id']==cells['PRODUCT'][1] and default['layout_version']==1
        assert default['issue'] is None
        r=_complete(c,ids,'stock',idempotency_key='default-production',disposition='stock',
            location_id=default['location_id'],expected_layout_version=default['layout_version'])
        assert r.status_code==200,r.text


def test_product_area_with_only_occupied_cells_remains_a_valid_default(production_app,monkeypatch):
    app,factory,ids,cells,old_id=formal_cells(production_app,monkeypatch,preference=True)
    with factory() as db:
        db.get(ProductStoragePreference,ids['product_a']).location_id=None;db.commit()
    with TestClient(app) as c:
        _login(c)
        rows=c.get('/api/production/tasks',params={'status':'pending'}).json()
        rows=rows.get('items',rows) if isinstance(rows,dict) else rows
        default=next(r for r in rows if r['id']==ids['cases']['stock']['task'])['production_default_storage']
        assert default['source']=='product_area' and default['issue'] is None
        assert default['location_id']==cells['PRODUCT'][1]
        r=_complete(c,ids,'stock',idempotency_key='occupied-area',disposition='stock',
            location_id=default['location_id'],expected_layout_version=1)
        assert r.status_code==200,r.text


@pytest.mark.parametrize('failure',['disabled_default','stale_layout','foreign_scope','audit'])
def test_invalid_default_cas_scope_and_audit_failure_do_not_change_old_lot(production_app,monkeypatch,failure):
    app,factory,ids,cells,old_id=formal_cells(production_app,monkeypatch,preference=True)
    old=snapshot(factory,old_id)
    if failure=='disabled_default':
        with factory() as db:
            db.get(WarehouseLocation,cells['PRODUCT'][1]).is_active=False;db.commit()
    with TestClient(app,raise_server_exceptions=False) as c:
        _login(c,'n029-scoped' if failure=='foreign_scope' else 'n029-admin')
        if failure=='audit':
            from app.api import production
            def broken(*args,**kwargs):raise RuntimeError('synthetic audit failure')
            monkeypatch.setattr(production,'append_audit_event',broken)
        r=_complete(c,ids,'cross' if failure=='foreign_scope' else 'stock',
            idempotency_key='occupied-rejected',disposition='stock',
            location_id=cells['CUSTOMER'][1] if failure=='disabled_default' else cells['PRODUCT'][1],
            expected_layout_version=99 if failure=='stale_layout' else 1)
        assert r.status_code==(500 if failure=='audit' else 403 if failure=='foreign_scope' else 409),r.text
        if failure=='disabled_default':
            assert '默认货位不可用' in r.text
            rows=c.get('/api/production/tasks',params={'status':'pending'}).json()
            rows=rows.get('items',rows) if isinstance(rows,dict) else rows
            default=next(x for x in rows if x['id']==ids['cases']['stock']['task'])['production_default_storage']
            assert default['issue'] and default['location_id'] is None
    assert snapshot(factory,old_id)==old
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(ProductionCompletion))==0


@pytest.mark.parametrize('failure',['exclusive_product','shelf_capacity'])
def test_fixed_shelf_mixing_and_real_capacity_guards_remain(production_app,monkeypatch,failure):
    from app.models.fixed_shelf import ShelfBinding,ShelfProfile
    app,factory,ids,cells,old_id=formal_cells(production_app,monkeypatch)
    with factory() as db:
        product=db.get(Product,ids['product_a'])
        if failure=='exclusive_product':
            product=db.scalar(select(Product).where(Product.customer_id==ids['customer_b']))
        db.add(ShelfProfile(product_id=product.id,units_per_bundle=1,version=1));db.flush()
        db.add(ShelfBinding(location_id=cells['CUSTOMER'][1],product_id=product.id,priority=0,
            capacity=15 if failure=='shelf_capacity' else None));db.commit()
    old=snapshot(factory,old_id)
    with TestClient(app) as c:
        _login(c)
        r=_complete(c,ids,'stock',idempotency_key='fixed-reject',disposition='stock',
            location_id=cells['CUSTOMER'][1],expected_layout_version=1)
        assert r.status_code==409,r.text
        assert ('放不下' if failure=='shelf_capacity' else '不能混放') in r.text
    assert snapshot(factory,old_id)==old


def test_two_tasks_can_complete_into_same_cell_without_merging_lots(production_app,monkeypatch):
    app,factory,ids,cells,old_id=formal_cells(production_app,monkeypatch)
    old=snapshot(factory,old_id)
    body=dict(idempotency_key='co-located-batch',items=[dict(task_id=ids['cases'][key]['task'],
        expected_version=1,disposition='stock',location_id=cells['CUSTOMER'][1],expected_layout_version=1)
        for key in ('stock','direct')])
    with TestClient(app) as c:
        _login(c);r=c.post('/api/production/completion-batches',json=body)
        assert r.status_code==200,r.text
        assert len({item['inventory_lot_id'] for item in r.json()['items']})==2
        assert c.post('/api/production/completion-batches',json=body).status_code==200
        other=copy.deepcopy(body);other['items'][0]['actual_output_quantity']=1
        assert c.post('/api/production/completion-batches',json=other).status_code==409
    assert snapshot(factory,old_id)==old


def ground_seed(production_app,monkeypatch):
    app,factory,ids=production_app
    with factory() as db:actor=db.scalar(select(User.id).where(User.username=='n029-admin'))
    cells=_seed_production_customer_areas(factory,monkeypatch,
        customer_id=ids['customer_a'],actor_id=actor,specs=(('GROUND','ground',True),))
    return app,factory,ids,cells['GROUND'],actor


@pytest.mark.parametrize('after_move',[False,True])
def test_reused_ground_pallet_reversal_ignores_only_moves_before_new_lot(production_app,monkeypatch,after_move):
    from app.models.warehouse_inventory import InventoryPallet,InventoryLocationMovement
    from app.core.time_contract import beijing_now_naive
    app,factory,ids,(area,location),actor=ground_seed(production_app,monkeypatch)
    with TestClient(app) as c:
        _login(c)
        first=_complete(c,ids,'stock',idempotency_key='ground-first',disposition='stock',location_id=location,expected_layout_version=1)
        assert first.status_code==200,first.text
        old_id=first.json()['items'][0]['inventory_lot_id'];old=snapshot(factory,old_id)
        with factory() as db:
            pallet=db.scalar(select(InventoryPallet).where(InventoryPallet.location_id==location,InventoryPallet.is_current.is_(True)))
            pallet_id=pallet.id
            db.add(InventoryLocationMovement(pallet_id=pallet.id,from_location_id=ids['temp1'],
                to_location_id=location,movement_type='move',operator_id=actor,moved_at=beijing_now_naive(),remarks='本次新批次入库前的历史移位'))
            db.commit()
        second=_complete(c,ids,'direct',idempotency_key='ground-second',disposition='stock',location_id=location,expected_layout_version=1)
        assert second.status_code==200,second.text
        completion=second.json()['items'][0]['id']
        if after_move:
            with factory() as db:
                db.add(InventoryLocationMovement(pallet_id=pallet_id,from_location_id=location,
                    to_location_id=ids['temp1'],movement_type='move',operator_id=actor,moved_at=beijing_now_naive(),remarks='本次新批次入库后的移位'))
                db.commit()
        reversed_result=c.post(f'/api/production/completions/{completion}/revert',json={})
        assert reversed_result.status_code==(409 if after_move else 200),reversed_result.text
        if after_move:assert '移过库位' in reversed_result.text
        with factory() as db:
            assert db.get(InventoryPallet,pallet_id).is_current
            assert db.get(InventoryLot,second.json()['items'][0]['inventory_lot_id']).status==('active' if after_move else 'closed')
    assert snapshot(factory,old_id)==old


def test_controlled_ground_capacity_is_not_increased_by_shared_production(production_app,monkeypatch):
    from app.models.warehouse_inventory import WarehouseGroundOccupancy,WarehouseGroundPlacementMutation
    app,factory,ids,(area,location),actor=ground_seed(production_app,monkeypatch)
    with TestClient(app) as c:
        _login(c)
        first=_complete(c,ids,'stock',idempotency_key='ground-controlled-first',disposition='stock',location_id=location,expected_layout_version=1)
        assert first.status_code==200,first.text
        old_id=first.json()['items'][0]['inventory_lot_id'];old=snapshot(factory,old_id)
        with factory() as db:
            occupancy=db.scalar(select(WarehouseGroundOccupancy).where(WarehouseGroundOccupancy.primary_location_id==location,WarehouseGroundOccupancy.status=='active'))
            capacity=occupancy.capacity_quantity
            db.add(WarehouseGroundPlacementMutation(idempotency_key='ground-controlled-capacity',request_hash='f'*64,
                actor_user_id=actor,operation='finished_inbound',source_lot_id=None,result_lot_id=old_id,occupancy_id=occupancy.id));db.commit()
        second=_complete(c,ids,'direct',idempotency_key='ground-controlled-second',disposition='stock',location_id=location,expected_layout_version=1)
        assert second.status_code==409 and '现场容量不足' in second.text,second.text
        with factory() as db:
            assert db.scalar(select(func.count()).select_from(ProductionCompletion))==1
            assert db.scalar(select(WarehouseGroundOccupancy.capacity_quantity).where(WarehouseGroundOccupancy.primary_location_id==location,WarehouseGroundOccupancy.status=='active'))==capacity
    assert snapshot(factory,old_id)==old


def test_default_projection_query_count_does_not_grow_per_task(production_app,monkeypatch):
    from sqlalchemy import event
    from app.services.production_workflow import _annotate_product_production_storage
    app,factory,ids,cells,old_id=formal_cells(production_app,monkeypatch,preference=True)
    counts=[]
    for count in (1,50):
        queries=[]
        def record(*args):queries.append(1)
        with factory() as db:
            engine=db.get_bind();event.listen(engine,'before_cursor_execute',record)
            try:
                rows=[dict(product_id=ids['product_a'],customer_id=ids['customer_a'],
                    preferred_finished_storage_area_ids=[cells['CUSTOMER'][0]]) for _ in range(count)]
                _annotate_product_production_storage(db,rows)
                assert all(row['production_default_storage']['location_id']==cells['PRODUCT'][1] for row in rows)
            finally:event.remove(engine,'before_cursor_execute',record)
        counts.append(len(queries))
    assert counts[1]<=counts[0]+1,counts


@pytest.mark.parametrize('disabled',[False,True])
def test_primary_fixed_shelf_default_precedes_customer_area_and_invalid_binding_blocks(production_app,monkeypatch,disabled):
    from app.models.fixed_shelf import ShelfBinding,ShelfProfile
    app,factory,ids,cells,old_id=formal_cells(production_app,monkeypatch,preference=True)
    with factory() as db:
        db.delete(db.get(ProductStoragePreference,ids['product_a']))
        db.add(ShelfProfile(product_id=ids['product_a'],units_per_bundle=1,version=1));db.flush()
        db.add(ShelfBinding(product_id=ids['product_a'],location_id=cells['PRODUCT'][1],priority=0,capacity=1000))
        if disabled:db.get(WarehouseLocation,cells['PRODUCT'][1]).is_active=False
        db.commit()
    with TestClient(app) as c:
        _login(c);rows=c.get('/api/production/tasks',params={'status':'pending'}).json()
        rows=rows.get('items',rows) if isinstance(rows,dict) else rows
        default=next(r for r in rows if r['id']==ids['cases']['stock']['task'])['production_default_storage']
        assert default['source']=='fixed_shelf'
        assert bool(default['issue'])==disabled
        r=_complete(c,ids,'stock',idempotency_key='fixed-default',disposition='stock',
            location_id=cells['CUSTOMER'][1] if disabled else cells['PRODUCT'][1],expected_layout_version=1)
        assert r.status_code==(409 if disabled else 200),r.text


def test_invalid_product_default_cannot_be_bypassed_by_another_cell_in_same_area(production_app,monkeypatch):
    from app.models.fixed_shelf import ShelfBinding,ShelfProfile
    app,factory,ids,cells,old_id=formal_cells(production_app,monkeypatch,preference=True)
    with factory() as db:
        other=db.scalar(select(Product).where(Product.customer_id==ids['customer_b']))
        db.add(ShelfProfile(product_id=other.id,units_per_bundle=1,version=1));db.flush()
        db.add(ShelfBinding(product_id=other.id,location_id=cells['PRODUCT'][1],priority=0))
        alternative=db.get(WarehouseLocation,cells['CUSTOMER'][1]);default=db.get(WarehouseLocation,cells['PRODUCT'][1])
        alternative.area_code=default.area_code;alternative.address_area_id=default.address_area_id
        db.commit()
    with TestClient(app) as c:
        _login(c)
        r=_complete(c,ids,'stock',idempotency_key='invalid-default-other-cell',disposition='stock',
            location_id=cells['CUSTOMER'][1],expected_layout_version=1)
        assert r.status_code==409 and '默认货位已被其他产品专用绑定' in r.text,r.text
