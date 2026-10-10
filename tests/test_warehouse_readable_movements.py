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


def test_replenishment_sheet_source_code_is_searchable_without_use_binding(ledger):
    from sqlalchemy import select
    from app.models.customer import Customer
    from app.models.product import Product
    from app.models.stock_replenishment import StockReplenishmentOrder, StockReplenishmentOrderItem
    from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem
    from app.models.warehouse_inventory import (
        InventoryLot, InventoryMovement, InventoryLotTransfer, WarehouseLocation,
        SemiFinishedInventoryDetail as Semi,
    )

    with ledger[4]() as db:
        customer = db.get(Customer, ledger[2]['a'])
        location = db.scalar(select(WarehouseLocation))
        source_product = Product(customer_id=customer.id, product_code='80012273',
                                 customer_material_code='80012273', product_name='来源产品')
        db.add(source_product)
        db.flush()
        order = StockReplenishmentOrder(order_number='CBW-SOURCE-1', customer_id=customer.id,
                                        source_type='stock_warning', status='stocked')
        db.add(order)
        db.flush()
        item = StockReplenishmentOrderItem(replenishment_order_id=order.id,
            target_inventory_type='semi_finished', customer_id=customer.id,
            reference_product_id=source_product.id, product_code_snapshot='80012273',
            product_name_snapshot='来源产品', material_code_snapshot='DRD', layer_count=3,
            flute_type='A', report_length_mm=1032, report_width_mm=368,
            quantity=500, stocked_quantity=500)
        db.add(item)
        db.flush()
        receipt = IncomingReceipt(receipt_number='IR-SOURCE-1', status='posted',
            received_at=datetime(2026, 10, 10), idempotency_key='receipt-source-1')
        db.add(receipt)
        db.flush()
        line = IncomingReceiptItem(receipt_id=receipt.id,
            stock_replenishment_item_id=item.id, planned_quantity=500,
            received_quantity=500, cumulative_received_quantity=500,
            variance_quantity=0, variance_type='matched',
            resolution_status='not_required', status='posted')
        db.add(line)
        db.flush()
        lot = InventoryLot(lot_number='SEMI-SOURCE-1', inventory_type='semi_finished',
            warehouse_location_id=location.id, quantity_available=0, quantity_reserved=500,
            unit='sheets', status='active', source_type='replenishment',
            source_ref_type='stock_replenishment_receipt', source_ref_id=line.id,
            stock_date=date(2026, 10, 10), last_movement_at=datetime(2026, 10, 10))
        lot.semi_finished_detail = Semi(owner_customer_id=customer.id,
            material_code_snapshot='DRD', normalized_material_code='DRD', layer_count=3, flute_type='A',
            board_length_mm=1032, board_width_mm=368, sheet_type='net_sheet',
            component_type='whole')
        db.add(lot)
        db.flush()
        line.received_inventory_lot_id = lot.id
        item.inventory_lot_id = lot.id
        db.add(InventoryMovement(movement_number='M-SOURCE-1', inventory_lot_id=lot.id,
            movement_type='manual_in', quantity=500, unit='sheets', before_available=0,
            after_available=500, before_reserved=0, after_reserved=0,
            before_consumed=0, after_consumed=0, before_damaged=0, after_damaged=0,
            before_scrapped=0, after_scrapped=0, created_at=datetime(2026, 10, 10)))
        db.commit()

    result = get(ledger, keyword='80012273')
    assert result['total'] == 1
    row = result['items'][0]
    assert row['source_product_code'] == '80012273'
    assert row['source_product_name'] == '来源产品'
    assert row['inventory_code'] is None  # A source is not a confirmed use binding.
    assert (row['before_physical'], row['after_physical'], row['unit']) == (0, 500, 'sheets')
    assert get(ledger, keyword='80012273', customer_id=ledger[2]['a'])['total'] == 1
    ledger[1]['id'] = ledger[2]['sales']
    assert get(ledger, keyword='80012273')['total'] == 1
    assert ledger[0].get('/api/warehouse/movements',
        params={'customer_id': ledger[2]['b'], 'keyword': '80012273'}).status_code == 403

    # A partial location move retains the receipt key but has its own lot ID.
    # The transfer, rather than the copied key alone, proves its ancestry.
    ledger[1]['id'] = ledger[2]['admin']
    with ledger[4]() as db:
        source = db.scalar(select(InventoryLot).where(InventoryLot.lot_number == 'SEMI-SOURCE-1'))
        locations = list(db.scalars(select(WarehouseLocation).order_by(WarehouseLocation.id)))
        for number, with_transfer in ((2, True), (3, False)):
            moved = InventoryLot(lot_number=f'SEMI-SOURCE-{number}',
                inventory_type='semi_finished', warehouse_location_id=locations[1].id,
                quantity_available=100, quantity_reserved=0, unit='sheets', status='active',
                source_type='transfer', source_ref_type=source.source_ref_type,
                source_ref_id=source.source_ref_id, stock_date=date(2026, 10, 10),
                last_movement_at=datetime(2026, 10, 10))
            moved.semi_finished_detail = Semi(owner_customer_id=ledger[2]['a'],
                material_code_snapshot='DRD', normalized_material_code='DRD',
                layer_count=3, flute_type='A', board_length_mm=1032,
                board_width_mm=368, sheet_type='net_sheet', component_type='whole')
            db.add(moved)
            db.flush()
            if with_transfer:
                db.add(InventoryLotTransfer(source_lot_id=source.id,
                    target_lot_id=moved.id, source_location_id=locations[0].id,
                    target_location_id=locations[1].id, quantity=100,
                    available_quantity=100, reserved_quantity=0,
                    source_version_before=1, source_version_after=2,
                    idempotency_key='partial-source-move', request_hash='a'*64,
                    transferred_at=datetime(2026, 10, 10)))
            db.add(InventoryMovement(movement_number=f'M-SOURCE-{number}',
                inventory_lot_id=moved.id, movement_type='location_transfer',
                quantity=100, unit='sheets', before_available=0, after_available=100,
                before_reserved=0, after_reserved=0, before_consumed=0,
                after_consumed=0, before_damaged=0, after_damaged=0,
                before_scrapped=0, after_scrapped=0,
                created_at=datetime(2026, 10, 10)))
        db.commit()
    found = get(ledger, keyword='80012273', page_size=1)
    assert found['total'] == 2
    second = get(ledger, keyword='80012273', page=2, page_size=1)
    assert {found['items'][0]['lot_number'], second['items'][0]['lot_number']} == {
        'SEMI-SOURCE-1', 'SEMI-SOURCE-2',
    }
    assert get(ledger, lot_number='SEMI-SOURCE-3')['items'][0]['source_product_code'] is None


def test_merged_purchase_reserve_uses_each_frozen_order_line_code(ledger):
    from decimal import Decimal
    from sqlalchemy import select
    from app.models.customer import Customer
    from app.models.product import Product
    from app.models.order import Order, OrderItem
    from app.models.supplier_requisition_order import SupplierRequisitionOrder, SupplierRequisitionOrderItem
    from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem
    from app.models.purchase_receipt import IncomingReceiptPurposeAllocation
    from app.models.warehouse_inventory import (
        InventoryLot, InventoryMovement, WarehouseLocation,
        SemiFinishedInventoryDetail as Semi,
    )

    with ledger[4]() as db:
        customer = db.get(Customer, ledger[2]['a'])
        admin_id = ledger[2]['admin']
        location = db.scalar(select(WarehouseLocation))
        order = Order(order_number='ORDER-MERGED', customer_id=customer.id,
                      order_date=date(2026, 10, 10), status='pending_production',
                      payment_status='unpaid', total_amount=Decimal('0'))
        purchase = SupplierRequisitionOrder(order_number='SRO-MERGED',
            total_quantity=200, requisition_qty=200, status='confirmed')
        db.add_all([order, purchase])
        db.flush()
        merged = SupplierRequisitionOrderItem(supplier_order_id=purchase.id,
            product_code='MERGED-SUPPLIER-LABEL', quantity=200,
            requisition_qty=200)
        db.add(merged)
        db.flush()
        for number, code in enumerate(('MERGE-A', 'MERGE-B'), 1):
            product = Product(customer_id=customer.id, product_code=code,
                customer_material_code=code, product_name=f'产品{number}')
            db.add(product)
            db.flush()
            item = OrderItem(order_id=order.id, product_id=product.id,
                quantity=100, delivered_quantity=0, unit_price=Decimal('0'),
                subtotal=Decimal('0'), snapshot_product_name=f'冻结产品{number}',
                snapshot_product_code=code)
            db.add(item)
            db.flush()
            receipt = IncomingReceipt(receipt_number=f'IR-MERGED-{number}',
                received_at=datetime(2026, 10, 10), status='posted',
                idempotency_key=f'ir-merged-{number}')
            db.add(receipt)
            db.flush()
            line = IncomingReceiptItem(receipt_id=receipt.id, order_id=order.id,
                order_item_id=item.id, supplier_order_id=purchase.id,
                supplier_order_item_id=merged.id, planned_quantity=100,
                received_quantity=100, cumulative_received_quantity=100,
                variance_quantity=0, variance_type='matched',
                resolution_status='not_required', status='posted')
            db.add(line)
            db.flush()
            lot = InventoryLot(lot_number=f'MERGED-SHEET-{number}',
                inventory_type='semi_finished', warehouse_location_id=location.id,
                quantity_available=100, unit='sheets', status='active',
                source_type='purchase_reserve', source_ref_type='incoming_receipt_item',
                source_ref_id=line.id, stock_date=date(2026, 10, 10),
                last_movement_at=datetime(2026, 10, 10))
            lot.semi_finished_detail = Semi(owner_customer_id=customer.id,
                material_code_snapshot='DRD', normalized_material_code='DRD',
                layer_count=3, flute_type='A', board_length_mm=1032,
                board_width_mm=368, sheet_type='net_sheet', component_type='whole')
            db.add(lot)
            db.flush()
            movement = InventoryMovement(movement_number=f'M-MERGED-{number}',
                inventory_lot_id=lot.id, movement_type='manual_in', quantity=100,
                unit='sheets', before_available=0, after_available=100,
                before_reserved=0, after_reserved=0, before_consumed=0,
                after_consumed=0, before_damaged=0, after_damaged=0,
                before_scrapped=0, after_scrapped=0,
                created_at=datetime(2026, 10, 10))
            db.add(movement)
            db.flush()
            db.add(IncomingReceiptPurposeAllocation(
                incoming_receipt_item_id=line.id,
                purpose_contract_status_snapshot='legacy_unset',
                surplus_disposition='semi_finished_reserve',
                supplier_requisition_order_item_id=merged.id,
                source_kind='order_item', source_key=f'order_item:{item.id}',
                source_order_item_id=item.id, customer_id=customer.id,
                customer_name_snapshot=customer.name, component_type='whole',
                receipt_total_sheet_qty=100, receipt_order_purpose_sheet_qty=0,
                receipt_reserve_purpose_sheet_qty=100,
                cumulative_total_sheet_qty_before=0,
                cumulative_total_sheet_qty_after=100,
                cumulative_order_purpose_sheet_qty_before=0,
                cumulative_order_purpose_sheet_qty_after=0,
                cumulative_reserve_purpose_sheet_qty_before=0,
                cumulative_reserve_purpose_sheet_qty_after=100,
                finished_output_qty_before=0, finished_output_qty_after=0,
                finished_output_qty_delta=0,
                semi_finished_inventory_lot_id=lot.id,
                initial_semi_inventory_movement_id=movement.id,
                request_hash='a'*64, created_by=admin_id,
            ))
        db.commit()

    for code in ('MERGE-A', 'MERGE-B'):
        result = get(ledger, keyword=code)
        assert result['total'] == 1
        assert result['items'][0]['source_product_code'] == code
        assert result['items'][0]['inventory_code'] is None
    assert get(ledger, keyword='MERGED-SUPPLIER-LABEL')['total'] == 0
