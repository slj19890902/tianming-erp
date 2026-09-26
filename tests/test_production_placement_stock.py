from datetime import date
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select,func
from test_direct_external_finished import prepare,receive,routing_app,p1_40a_app,_p181_published_map_identity,_login
from app.api.production import router
from app.models.order import OrderItem
from app.models.receipt_putaway import ReceiptStagingArea
from app.models.warehouse_inventory import InventoryLot,WarehouseArea,WarehouseLocation,InventoryMovement
from app.services.warehouse_inventory import manual_finished_in


def seed(app):
    app.include_router(router,prefix='/api/production')
    with TestClient(app) as client:
        oid,pid,line=prepare(app,client)
        r=receive(client,pid,line,1000);assert r.status_code==200,r.text
    with app.state.factory() as db:
        item=db.scalar(select(OrderItem).where(OrderItem.order_id==oid))
        # Partially delivered orders are not excluded from physical placement.
        item.delivered_quantity=200
        lot=db.scalar(select(InventoryLot))
        loc=db.get(WarehouseLocation,lot.warehouse_location_id)
        area=db.scalar(select(WarehouseArea).where(WarehouseArea.area_code==loc.area_code))
        db.add(ReceiptStagingArea(area_id=area.id))
        from app.models.external_packaging_purchase import ExternalPackagingPurchaseItem
        purchase = db.scalar(select(ExternalPackagingPurchaseItem).where(ExternalPackagingPurchaseItem.id == line))
        purchase.customer_product_id_snapshot = item.product_id
        purchase.currency = 'CNY'
        purchase.unit_price = lot.estimated_unit_cost_snapshot
        purchase.purchase_quantity_basis_snapshot = 1
        purchase.order_quantity_basis_snapshot = 1
        purchase.tax_mode = 'tax_inclusive'
        purchase.tax_rate = 0.13
        db.flush()
        ids=[lot.id]
        for n in range(8):
            extra=manual_finished_in(db,customer_id=app.state.fixture['customer_a'],product_id=item.product_id,
                location_id=loc.id,quantity=10+n,stock_date=date.today(),source_type='purchase_reserve',
                remarks='盘点样本',operator_id=1,idempotency_key=f'stage-{n}',
                expected_layout_version=loc.floor3_layout.version,require_empty_pallet=False)
            extra.source_type='stocktake'  # Explicit legacy fixture, not a production admission path.
            extra.estimated_unit_cost_snapshot=lot.estimated_unit_cost_snapshot
            ids.append(extra.id)
        target=db.scalar(select(WarehouseLocation).where(WarehouseLocation.area_code==loc.area_code,WarehouseLocation.id!=loc.id).order_by(WarehouseLocation.id))
        db.commit()
        return ids,lot.version,target.id,target.floor3_layout.version


def test_all_sources_paged_scoped_and_filters(routing_app):
    ids,_,_,_=seed(routing_app)
    with TestClient(routing_app) as c:
        assert c.get('/api/production/placement-stock').status_code==401
        _login(c,'p1-40a-admin')
        pages=[c.get('/api/production/placement-stock',params={'page':p,'page_size':6}).json() for p in (1,2)]
        assert all(p['total']==9 for p in pages)
        assert set(x['inventory_lot_id'] for p in pages for x in p['items'])==set(ids)
        assert len(pages[0]['items'])==6 and len(pages[1]['items'])==3
        assert c.get('/api/production/placement-stock',params={'product_code':'DOES-NOT-EXIST'}).json()['total']==0
        _login(c,'p1-40b-scoped')
        scoped=c.get('/api/production/placement-stock')
        assert scoped.status_code in (200,403)
        if scoped.status_code==200:assert scoped.json()['total']==0
        with routing_app.state.factory() as db:
            db.get(InventoryLot,ids[1]).status='frozen'
            db.commit()
        _login(c,'p1-40a-admin')
        frozen=next(r for r in c.get('/api/production/placement-stock').json()['items'] if r['inventory_lot_id']==ids[1])
        assert not frozen['can_place'] and frozen['placement_block']=='库存已冻结'


def test_reserved_move_replay_stale_scope_and_audit_rollback(routing_app,monkeypatch):
    ids,version,target,target_version=seed(routing_app)
    payload=dict(expected_version=version,quantity=1000,location_id=target,expected_layout_version=target_version,idempotency_key='stage-move')
    url=f'/api/production/placement-stock/{ids[0]}/transfer'
    with TestClient(routing_app) as c:
        _login(c,'p1-40b-scoped')
        assert c.post(url,json=payload).status_code==403
        _login(c,'p1-40a-admin')
        assert c.post(url,json=payload|{'expected_version':version+99}).status_code==409
        import app.api.production as api
        original=api.append_audit_event
        def fail(*a,**kw):raise RuntimeError('audit failure')
        monkeypatch.setattr(api,'append_audit_event',fail)
        with pytest.raises(RuntimeError,match='audit failure'):c.post(url,json=payload)
        with routing_app.state.factory() as db:
            lot=db.get(InventoryLot,ids[0]);assert lot.version==version and lot.quantity_reserved==1000
        monkeypatch.setattr(api,'append_audit_event',original)
        r=c.post(url,json=payload);assert r.status_code==200,r.text
        assert c.post(url,json=payload).json()['replayed']
        assert c.post(url,json=payload|{'quantity':999}).status_code==409
        with routing_app.state.factory() as db:
            moved=db.get(InventoryLot,r.json()['target_lot_id'])
            assert moved.warehouse_location_id==target and moved.quantity_reserved==1000
        pending = c.get('/api/production/placement-stock', params={'placement_state':'pending'}).json()
        placed = c.get('/api/production/placement-stock', params={'placement_state':'placed'}).json()
        all_stock = c.get('/api/production/placement-stock', params={'placement_state':'all'}).json()
        assert all_stock['total'] == pending['total'] + placed['total']
        # This fixture moves within one staging area, so it remains pending.
        assert next(row for row in pending['items'] if row['inventory_lot_id'] == r.json()['target_lot_id'])['placement_pending']
        with routing_app.state.factory() as db:
            for staging in db.scalars(select(ReceiptStagingArea)):
                db.delete(staging)
            db.commit()
        placed = c.get('/api/production/placement-stock', params={'placement_state':'placed'}).json()
        target_row = next(row for row in placed['items'] if row['inventory_lot_id'] == r.json()['target_lot_id'])
        assert not target_row['placement_pending'] and not target_row['can_place']
        assert target_row['reserved_quantity'] == 1000
