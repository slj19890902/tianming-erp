from datetime import date, datetime
import json
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_p1_21b_mobile_admin_product_search import mobile_erp_app, _login
from test_p1_21f3_mobile_warehouse_map import _add_map_target, _mobile_move_identity
from tests.test_n035_stocktake_api import stocktake_api, _submission_payload, _login as stock_login
from app.models.warehouse_inventory import InventoryLot, SemiFinishedInventoryDetail, InventoryMovement, InventoryReservation
from app.models.warehouse_goods import WarehouseGoodsProfile
from tests.test_p1_47d_inventory_adjustment import stocktake_app


@pytest.mark.parametrize('quantity', [4, 8, 10])
@pytest.mark.parametrize('reserved', [0, 7])
def test_mobile_counted_sheet_moves_into_finished_location(mobile_erp_app, quantity, reserved, monkeypatch):
    app, _, factory = mobile_erp_app
    source_id, target_id = _add_map_target(factory)
    with factory() as db:
        source = db.get(InventoryLot, source_id)
        sheet = InventoryLot(lot_number='COUNTED-SHEET-MOVE', inventory_type='semi_finished',
            warehouse_location_id=source.warehouse_location_id, quantity_available=10-reserved, quantity_reserved=reserved,
            unit='sheets', status='active', source_type='stocktake', stock_date=date.today(), last_movement_at=datetime.now())
        sheet.semi_finished_detail = SemiFinishedInventoryDetail(material_code_snapshot='A1B',
            normalized_material_code='A1B', layer_count=3, flute_type='B', board_length_mm=800,
            board_width_mm=600, sheet_type='raw_board', internal_name='盘点原板')
        db.add(sheet); db.flush()
        sheet_id = sheet.id
        if reserved:
            original=db.scalar(select(InventoryReservation).where(InventoryReservation.inventory_lot_id==source_id))
            db.add(InventoryReservation(reservation_number='SHEET-MOVE-RES',inventory_lot_id=sheet_id,
                reservation_type='semi_order',order_id=original.order_id,order_item_id=original.order_item_id,
                reserved_stock_quantity=7,credited_requirement_quantity=13,yield_factor=2,status='active'))
        db.add(WarehouseGoodsProfile(lot_id=sheet_id, data_json=json.dumps({'scope':'general','processing':'raw'})))
        db.commit()
    with TestClient(app) as client:
        _login(client, 'mobile-admin')
        identity = _mobile_move_identity(client, source_lot_id=sheet_id, target_location_id=target_id)
        area = client.get('/api/mobile/erp/warehouse/map/floors/3F?area_code=C1').json()
        good = next(g for location in area['locations'] for g in location['goods'] if g['lot_id']==sheet_id)
        assert good['can_move']
        payload = dict(**identity, expected_version=1, quantity=quantity, target_location_id=target_id,
            idempotency_key='sheet-move-test', physical_move_confirmed=True)
        for change in ({'quantity':11}, {'expected_version':99}, {'expected_target_map_revision':'outdated'}):
            failed=client.post(f'/api/mobile/erp/warehouse/lots/{sheet_id}/moves',json={**payload,**change,'idempotency_key':'invalid-sheet-move'})
            assert failed.status_code==409,failed.text
        if quantity==4 and reserved==7:
            from app.api import mobile_erp
            with monkeypatch.context() as patch:
                def fail_audit(*args,**kwargs):
                    raise RuntimeError('injected audit failure')
                patch.setattr(mobile_erp,'append_audit_event',fail_audit)
                with pytest.raises(RuntimeError,match='injected audit failure'):
                    client.post(f'/api/mobile/erp/warehouse/lots/{sheet_id}/moves',json=payload)
            with factory() as db:
                unchanged=db.get(InventoryLot,sheet_id)
                assert (unchanged.quantity_available,unchanged.quantity_reserved,unchanged.version)==(3,7,1)
        response = client.post(f'/api/mobile/erp/warehouse/lots/{sheet_id}/moves', json=payload)
        assert response.status_code == 200, response.text
        assert client.post(f'/api/mobile/erp/warehouse/lots/{sheet_id}/moves', json=payload).json()['idempotent_replay']
        assert client.post(f'/api/mobile/erp/warehouse/lots/{sheet_id}/moves', json={**payload,'quantity':quantity+1}).status_code==409
        target_lot_id=response.json()['target_lot']['lot_id']
        if quantity==4:
            reverse_identity=_mobile_move_identity(client,source_lot_id=source_id,target_location_id=target_id)
            reverse=client.post(f'/api/mobile/erp/warehouse/lots/{source_id}/moves',json={
                **reverse_identity,'expected_version':1,'quantity':1,'target_location_id':target_id,
                'idempotency_key':'finished-into-sheets','physical_move_confirmed':True})
            assert reverse.status_code==200,reverse.text
    with factory() as db:
        sheet=db.get(InventoryLot,sheet_id); target=db.get(InventoryLot,target_lot_id)
        assert target.quantity_available+target.quantity_reserved==quantity and target.warehouse_location_id==target_id
        assert target.semi_finished_detail.internal_name=='盘点原板'
        assert db.get(WarehouseGoodsProfile,target_lot_id).data_json
        if quantity<10:
            assert sheet.quantity_available+sheet.quantity_reserved==10-quantity
        if reserved:
            rows=list(db.scalars(select(InventoryReservation).where(InventoryReservation.inventory_lot_id.in_({sheet_id,target_lot_id}))))
            assert sum(r.reserved_stock_quantity-r.released_stock_quantity for r in rows)==7
            assert sum(r.credited_requirement_quantity-r.released_requirement_quantity for r in rows)==13
        moves=list(db.scalars(select(InventoryMovement).where(InventoryMovement.inventory_lot_id.in_({sheet_id,target_lot_id}))))
        assert len(moves)==(2 if quantity<10 else 1)


def test_zero_count_is_hidden_but_history_and_next_count_work(stocktake_api):
    app,factory,ids=stocktake_api
    with factory() as db:
        db.get(InventoryLot,ids['lot1']).quantity_reserved=0
        db.commit()
    with TestClient(app) as client:
        stock_login(client,'n035-admin')
        payload=_submission_payload(client,ids['location'],key='zero-hide',counts={ids['lot1']:0})
        response=client.post('/api/warehouse/stocktakes/confirm',json=payload)
        assert response.status_code==201,response.text
        current=client.get(f"/api/warehouse/stocktake/locations/{ids['location']}").json()
        assert ids['lot1'] not in [lot['id'] for lot in current['lots']]
        assert client.post('/api/warehouse/stocktakes/confirm',json=payload).status_code==201
        following=_submission_payload(client,ids['location'],key='after-zero',counts={})
        assert client.post('/api/warehouse/stocktakes/confirm',json=following).status_code==201
    with factory() as db:
        assert db.get(InventoryLot,ids['lot1']) is not None
        assert list(db.scalars(select(InventoryMovement).where(InventoryMovement.inventory_lot_id==ids['lot1'])))


def test_empty_historical_pallet_item_does_not_block_mixed_entry(mobile_erp_app):
    from app.services.warehouse_stocktake_batch import assert_location_add_compatible
    app,_,factory=mobile_erp_app
    source_id,_=_add_map_target(factory)
    with factory() as db:
        lot=db.get(InventoryLot,source_id)
        lot.quantity_available=lot.quantity_reserved=0
        lot.status='closed'
        db.flush()
        assert_location_add_compatible(db,lot.warehouse_location_id)


@pytest.mark.parametrize('processing',['raw','cut'])
def test_add_sheets_to_occupied_finished_location(stocktake_app,processing):
    from app.api.warehouse_goods import SheetEntry, GoodsFacts, create_sheet
    from app.models.material import Material
    from app.models.user import User
    from app.models.warehouse_inventory import Floor3LocationLayout
    app,factory,ids,_=stocktake_app
    with factory() as db:
        user=db.scalar(select(User).where(User.username=='p147d-admin'))
        location=ids['loc_fg3']
        existing=list(db.scalars(select(InventoryLot).where(InventoryLot.warehouse_location_id==location)))
        before={lot.id:(lot.quantity_available,lot.quantity_reserved) for lot in existing}
        assert any(lot.inventory_type=='finished' and lot.quantity_available>0 for lot in existing)
        db.add(Material(code='A1B',supplier_name='测试报价供应商',layer_count=3,
            quote_price=2,price_unit='元/㎡',purchase_currency='CNY',purchase_tax_included=True,purchase_tax_rate=0.13))
        db.commit()
        payload=SheetEntry(facts=GoodsFacts(material_code='A1B',processing=processing),
            location_id=location,expected_layout_version=db.get(Floor3LocationLayout,location).version,
            quantity=15,stock_date=date.today(),internal_name='混放片料',board_length_mm=800,
            board_width_mm=600,layer_count=3,flute_type='B',idempotency_key='mixed-sheet-entry')
        result=create_sheet(payload,db,user)
        assert create_sheet(payload,db,user)==result
        assert db.get(InventoryLot,result['lot_id']).quantity_available==15
        for lot_id,balance in before.items():
            original=db.get(InventoryLot,lot_id)
            assert (original.quantity_available,original.quantity_reserved)==balance
