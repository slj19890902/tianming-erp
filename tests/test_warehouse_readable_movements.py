from datetime import date, datetime
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def ledger(tmp_path):
    from app.api import warehouse
    from app.api.deps import get_db
    from app.core.database import create_sqlite_engine
    from app.models import Base
    from app.models.customer import Customer
    from app.models.product import Product
    from app.models.user import User
    from app.models.access_control import UserCustomerScope
    from app.models.warehouse_inventory import InventoryLot, InventoryMovement, InventoryLotTransfer, WarehouseLocation, FinishedGoodsInventoryDetail as Finished, SemiFinishedInventoryDetail as Semi, SemiFinishedLotAllowedProduct as Binding
    engine=create_sqlite_engine(tmp_path/'ledger.sqlite3')
    Base.metadata.create_all(engine)
    factory=sessionmaker(bind=engine,expire_on_commit=False)
    with factory() as db:
        admin=User(username='admin-fixture',password_hash='test',role='admin',real_name='仓库员',must_change_password=False)
        sales=User(username='sales-fixture',password_hash='test',role='sales',real_name='业务员',customer_access_mode='selected',must_change_password=False)
        a=Customer(customer_number=1,customer_code='YKE',chinese_short_name='研光',name='研光公司')
        b=Customer(customer_number=2,customer_code='OTHER',name='未授权客户')
        locs=[WarehouseLocation(location_code=c,location_name='货架 '+c,warehouse_type='finished') for c in ['A1-1-1','B1-2-1','C1-1-1']]
        db.add_all([admin,sales,a,b,*locs]);db.flush()
        db.add(UserCustomerScope(user_id=sales.id,customer_id=a.id))
        products=[Product(customer_id=c.id,product_code=code,customer_material_code=code,product_name=name) for c,code,name in [(a,'80012211','纸盒'),(b,'80012211','保密产品'),(b,'SECRET-CODE','秘密纸板')]]
        db.add_all(products);db.flush()
        lots=[]
        for i,product in enumerate(products[:2]):
            lot=InventoryLot(lot_number=f'LOT{i}',inventory_type='finished',warehouse_location_id=locs[0].id,unit='boxes',source_type='stocktake',stock_date=date(2026,10,9),last_movement_at=datetime(2026,10,9),quantity_available=100)
            lot.finished_detail=Finished(owner_customer_id=product.customer_id,product_id=product.id,inventory_code_snapshot=product.product_code,product_name_snapshot=product.product_name)
            db.add(lot);lots.append(lot)
        semi=InventoryLot(lot_number='SEMI',inventory_type='semi_finished',warehouse_location_id=locs[0].id,unit='sheets',source_type='stocktake',stock_date=date(2026,10,9),last_movement_at=datetime(2026,10,9))
        semi.semi_finished_detail=Semi(owner_customer_id=a.id,internal_name='半成品片料',layer_count=3,flute_type='B',board_length_mm=100,board_width_mm=100,material_code_snapshot='VIK',normalized_material_code='VIK',sheet_type='net_sheet',component_type='whole')
        db.add(semi);db.flush()
        db.add_all([Binding(inventory_lot_id=semi.id,product_id=p.id,confirmed_at=datetime(2026,10,9)) for p in [products[0],products[2]]])
        def movement(lot,kind,before,after,**extra):
            count=len(moves)+1
            values={f'{side}_{key}':0 for side in ['before','after'] for key in ['available','reserved','consumed','damaged','scrapped']}
            for side,balances in [('before',before),('after',after)]:
                values.update({f'{side}_{key}':value for key,value in balances.items()})
            row=InventoryMovement(movement_number=f'M{count}',lot=lot,movement_type=kind,quantity=10,unit=lot.unit,operator_id=admin.id,reason='实盘核对',created_at=datetime(2026,10,8,16,30),**values,**extra)
            db.add(row);moves.append(row);return row
        moves=[]
        movement(lots[0],'manual_in',{'available':0},{'available':100})
        movement(lots[0],'reserve',{'available':100},{'available':70,'reserved':30})
        movement(lots[0],'damage',{'available':70,'reserved':30},{'available':60,'reserved':30,'damaged':10})
        movement(lots[0],'scrap',{'available':60,'reserved':30,'damaged':10},{'available':60,'reserved':30,'scrapped':10})
        movement(lots[1],'manual_in',{'available':0},{'available':900})
        movement(semi,'manual_in',{'available':0},{'available':40})
        # Whole move: no stock change. Current place later changed again; do not use it as history.
        movement(lots[0],'location_transfer',{'available':100},{'available':100},idempotency_key='location-transfer:move-1:source')
        db.flush()
        db.add(InventoryLotTransfer(source_lot_id=lots[0].id,target_lot_id=lots[0].id,source_location_id=locs[0].id,target_location_id=locs[1].id,quantity=100,available_quantity=100,reserved_quantity=0,source_version_before=1,source_version_after=2,idempotency_key='move-1',request_hash='a'*64,transferred_at=datetime(2026,10,9)))
        lots[0].warehouse_location_id=locs[2].id
        db.commit();ids={'admin':admin.id,'sales':sales.id,'a':a.id,'b':b.id}
    app=FastAPI();app.include_router(warehouse.router,prefix='/api/warehouse')
    def get_session():
        with factory() as db:yield db
    who={'id':ids['admin']}
    def read_user():
        with factory() as db:return db.get(User,who['id'])
    app.dependency_overrides[get_db]=get_session;app.dependency_overrides[warehouse.can_read]=read_user
    statements=[]
    event.listen(engine,'before_cursor_execute',lambda conn,cursor,statement,parameters,context,many:statements.append(statement))
    with TestClient(app) as client:yield client,who,ids,statements,factory
    engine.dispose()


def get(ledger,**params):
    response=ledger[0].get('/api/warehouse/movements',params=params)
    assert response.status_code==200,response.text
    return response.json()


def test_product_customer_search_and_paging_are_readonly(ledger):
    data=get(ledger,keyword='80012211',customer_id=ledger[2]['a'],page_size=2)
    assert data['total']==6 and len(data['items'])==2
    assert all(x['customer_name']=='研光' for x in data['items'])
    next_page=get(ledger,keyword='80012211',customer_id=ledger[2]['a'],page=2,page_size=2)
    assert not ({x['id'] for x in data['items']} & {x['id'] for x in next_page['items']})
    assert get(ledger,keyword='研光')['total']==6
    assert get(ledger,keyword='YKE')['total']==6
    assert get(ledger,keyword='%')['total']==0
    assert not any(s.lstrip().split()[0].upper() in {'INSERT','UPDATE','DELETE'} for s in ledger[3])


def test_customer_scope_filters_before_total_and_redacts_other_bindings(ledger):
    ledger[1]['id']=ledger[2]['sales']
    result=get(ledger,keyword='80012211')
    assert result['total']==6
    assert '未授权客户' not in str(result) and 'SECRET-CODE' not in str(result)
    assert all(x['reason'] is None for x in result['items'])
    assert get(ledger,keyword='SECRET-CODE')['total']==0
    assert ledger[0].get('/api/warehouse/movements',params={'customer_id':ledger[2]['b']}).status_code==403


def test_physical_quantity_and_authoritative_transfer_locations(ledger):
    rows=get(ledger,customer_id=ledger[2]['a'])['items']
    by_type={x['movement_type']:x for x in rows if x['lot_number']=='LOT0'}
    assert by_type['reserve']['before_physical']==by_type['reserve']['after_physical']==100
    assert by_type['damage']['physical_delta']==0
    assert by_type['scrap']['physical_delta']==-10
    moved=by_type['location_transfer']
    assert moved['physical_delta']==0 and moved['transfer_quantity']==100
    assert 'A1-1-1' in moved['from_location'] and 'B1-2-1' in moved['to_location']
    assert 'C1-1-1' in moved['current_location']
    assert by_type['manual_in']['from_location'] is None
    assert by_type['manual_in']['operator_name']=='仓库员'


def test_beijing_date_boundaries_and_invalid_range(ledger):
    assert get(ledger,date_from='2026-10-09',date_to='2026-10-09')['total']==7
    assert get(ledger,date_from='2026-10-08',date_to='2026-10-08')['total']==0
    assert ledger[0].get('/api/warehouse/movements',params={'date_from':'2026-10-10','date_to':'2026-10-09'}).status_code==422


def test_split_transfer_has_two_explicit_sides_without_inventing_stock(ledger):
    from sqlalchemy import select
    from app.models.warehouse_inventory import InventoryLot, InventoryLotTransfer, InventoryMovement
    from app.services.asset_time_archive import _transfer_movement_key
    with ledger[4]() as db:
        transfer=db.scalar(select(InventoryLotTransfer))
        target=db.scalar(select(InventoryLot).where(InventoryLot.lot_number=='SEMI'))
        transfer.target_lot_id=target.id
        transfer.quantity=transfer.available_quantity=20
        original=db.scalar(select(InventoryMovement).where(InventoryMovement.movement_type=='location_transfer'))
        original.before_available=100;original.after_available=80;original.quantity=20
        values={f'{side}_{key}':0 for side in ['before','after'] for key in ['available','reserved','consumed','damaged','scrapped']}
        values['after_available']=20
        db.add(InventoryMovement(movement_number='SPLIT-TARGET',lot=target,movement_type='location_transfer',quantity=20,unit='sheets',idempotency_key=_transfer_movement_key('move-1','target'),created_at=datetime(2026,10,9),**values))
        db.commit()
    rows=get(ledger,movement_type='location_transfer')['items']
    assert {r['operation_label'] for r in rows}=={'移库转入','移库转出'}
    assert sum(r['physical_delta'] for r in rows)==0
    assert all('A1-1-1' in r['from_location'] and 'B1-2-1' in r['to_location'] for r in rows)
