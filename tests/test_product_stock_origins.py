from datetime import date, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from test_product_workbench import _app, mobile_erp_app, _login
from app.models.product import Product
from app.models.warehouse_inventory import InventoryLot, FinishedGoodsInventoryDetail, InventoryLotTransfer
from app.models.access_control import UserPermissionOverride
from app.models.user import User
from app.services.product_stock_origins import Origins, production_lot_ids


def add_lot(db, product, location_id, name, qty, kind, ref=None, *, consumed=0):
    lot = InventoryLot(lot_number=name, inventory_type='finished', warehouse_location_id=location_id,
        quantity_available=qty, quantity_reserved=0, quantity_consumed=consumed,
        quantity_damaged=0, quantity_scrapped=0, unit='boxes', status='active',
        source_type=kind, source_ref_type=ref, stock_date=date(2026, 10, 17),
        last_movement_at=datetime(2026, 10, 17, 13, 57, 31),
        created_at=datetime(2026, 10, 17, 13, 57, 31))
    db.add(lot); db.flush()
    db.add(FinishedGoodsInventoryDetail(inventory_lot_id=lot.id, owner_customer_id=product.customer_id,
        owner_customer_name_snapshot='客户快照', product_id=product.id, is_general=False,
        inventory_code_snapshot=product.product_code, product_name_snapshot=product.product_name,
        material_code_snapshot='OLD-MATERIAL', box_type_snapshot='普通箱'))
    db.flush()
    return lot


def test_sources_do_not_count_depleted_stock_or_rewrite_provenance(mobile_erp_app):
    app, ids, factory = _app(mobile_erp_app)
    with factory() as db:
        product = db.get(Product, ids['product_two'])
        location = db.scalar(select(InventoryLot.warehouse_location_id))
        a=add_lot(db, product, location, 'STOCKTAKE-1', 1, 'stocktake')
        add_lot(db, product, location, 'OLD-DEPLETED', 0, 'stocktake', consumed=30)
        add_lot(db, product, location, 'PRODUCTION-5', 5, 'production_completion', 'production_completion')
        add_lot(db, product, location, 'REPLENISH-7', 7, 'transfer', 'stock_preparation')
        add_lot(db, product, location, 'OTHER-2', 2, 'manual')
        lot_id=a.id; db.commit()
    path=f"/api/product-workbench/products/{ids['product_two']}/stock-sources"
    with TestClient(app) as c:
        assert c.get(path).status_code==401
        _login(c,'mobile-admin')
        r=c.get(path);assert r.status_code==200,r.text
        data=r.json();assert data['summary']['actual']==15
        assert {k:v['actual'] for k,v in data['sources'].items()}==dict(stocktake=1,order=5,replenishment=7,other=2)
        assert data['total']==4
        history=c.get(path,params={'include_history':True}).json()
        assert history['total']==5 and history['summary']==data['summary']
        filtered=c.get(path,params={'source_kind':'stocktake','page_size':1}).json()
        assert filtered['items'][0]['lot_id']==lot_id and filtered['summary']['actual']==15
        detail=c.get(path+f'/{lot_id}');assert detail.status_code==200,detail.text
        d=detail.json();assert d['batch']['source_kind']=='stocktake'
        assert d['events'][0]['at']=='2026-10-17T13:57:31Z'
        assert not any(e['label'] in ('订单报料','实收材料') for e in d['events'])
        search=c.get('/api/product-workbench/search',params={'q':product.product_code}).json()
        assert search['items'][0]['inventory']['finished']['actual']==15
        assert search['items'][0]['stock_sources']['stocktake']['actual']==1


def test_transfer_keeps_origin_and_production_paging_excludes_stocktake(mobile_erp_app):
    app, ids, factory = _app(mobile_erp_app)
    from app.services.production_placement import page
    with factory() as db:
        p=db.get(Product,ids['product_two']);loc=db.scalar(select(InventoryLot.warehouse_location_id))
        a=add_lot(db,p,loc,'ROOT-STOCKTAKE',0,'stocktake')
        b=add_lot(db,p,loc,'MOVED-STOCKTAKE',3,'transfer')
        produced=add_lot(db,p,loc,'ROOT-PRODUCTION',0,'production_completion','production_completion')
        moved=add_lot(db,p,loc,'MOVED-PRODUCTION',4,'transfer')
        for n,(source,target) in enumerate([(a,b),(produced,moved)]):
            db.add(InventoryLotTransfer(source_lot_id=source.id,target_lot_id=target.id,
                source_location_id=loc,target_location_id=loc,quantity=target.quantity_available,
                available_quantity=target.quantity_available,reserved_quantity=0,
                source_version_before=1,source_version_after=2,idempotency_key=f'origin-move-{n}',
                request_hash='a'*64,transferred_at=datetime(2026,10,18,1)))
        db.commit()
        assert Origins(db).describe(b)['source_kind']=='stocktake'
        assert Origins(db).describe(moved)['source_kind']=='order'
        assert b.id not in set(db.scalars(production_lot_ids()))
        assert moved.id in set(db.scalars(production_lot_ids()))
        rows,total=page(db,allowed_customer_ids=None,placement_state='all',product_code=p.product_code,page_size=1)
        assert total==1 and rows[0]['inventory_lot_id']==moved.id
        assert page(db,allowed_customer_ids=set(),placement_state='all')[1]==0


def test_stock_endpoint_customer_and_permission_boundaries(mobile_erp_app):
    app,ids,factory=_app(mobile_erp_app)
    with factory() as db:
        scoped=db.scalar(select(User).where(User.username=='mobile-scoped'))
        db.add(UserPermissionOverride(user_id=scoped.id,permission_code='warehouse.view',is_allowed=False));db.commit()
    own=f"/api/product-workbench/products/{ids['product']}/stock-sources"
    other=f"/api/product-workbench/products/{ids['other_product']}/stock-sources"
    with TestClient(app) as c:
        _login(c,'mobile-scoped');assert c.get(own).status_code==403
        with factory() as db:
            override=db.scalar(select(UserPermissionOverride).where(UserPermissionOverride.permission_code=='warehouse.view'))
            override.is_allowed=True;db.commit()
        assert c.get(other).status_code==404
        assert c.get(own,params={'source_kind':'unknown'}).status_code==422
        assert c.get(own+'/9223372036854775808').status_code==422
        assert c.get(own,params={'page':9223372036854775808}).status_code==422
