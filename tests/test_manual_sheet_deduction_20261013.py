from datetime import date, datetime
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func

from tests.test_n039_composite_bom_requisition import composite_requisition_app, _login


def seed(sessions, *, double=False, yield_factor=1, owned=False):
    from app.models.product import Product
    from app.models.product_bom import SalesOrderItemBomComponent
    from app.models.warehouse_inventory import InventoryLot, SemiFinishedInventoryDetail, WarehouseLocation
    with sessions() as db:
        snapshot = db.get(SalesOrderItemBomComponent, 1)
        product = db.get(Product, snapshot.component_product_id)
        product.layer_count = 5
        product.flute_type = 'AB'
        product.default_material_code = 'K616K'
        if double:
            snapshot.snapshot_component_box_style = product.box_style = 'A1/0201 普通开槽箱'
            snapshot.snapshot_component_pieces_per_box = product.pieces_per_box = 2
            product.splice_mode = 'double'
        if yield_factor > 1:
            from app.models.mold_tool import MoldTool
            mold=MoldTool(mold_code='MANUAL-SHEET-MOLD',mold_name='片料测试模具',rack_location='测试架')
            db.add(mold);db.flush();snapshot.snapshot_mold_tool_id=mold.id
            snapshot.is_die_cut = True
            snapshot.mold_max_yield_per_sheet = yield_factor
            snapshot.sheet_cutting_settings_snapshot = {'schema_version': 2, 'whole': {
                'length_parts': 2, 'width_parts': 1, 'mold_count': yield_factor, 'is_die_cut': True}}
        location = WarehouseLocation(location_code='MANUAL-SHEET', location_name='片料测试位置', warehouse_type='semi_finished')
        db.add(location); db.flush()
        lot = InventoryLot(lot_number='MANUAL-SHEET-ONE', inventory_type='semi_finished',
            warehouse_location_id=location.id, quantity_available=204, quantity_reserved=0,
            quantity_consumed=0, quantity_damaged=0, quantity_scrapped=0, unit='sheets',
            status='active', source_type='manual', stock_date=date.today(),last_movement_at=datetime.now())
        db.add(lot);db.flush()
        db.add(SemiFinishedInventoryDetail(inventory_lot_id=lot.id,
            supplier_name='N039 供应商', owner_customer_id=1 if owned else None,
            material_code_snapshot='K616K', normalized_material_code='K616K',layer_count=5,
            flute_type='AB',board_length_mm=1000,board_width_mm=700,component_type='whole',
            pieces_per_box=1,stock_yield_per_sheet=yield_factor,sheet_type='raw_board',internal_name='现有片料'))
        if owned:
            from app.models.warehouse_inventory import SemiFinishedLotAllowedProduct
            db.add(SemiFinishedLotAllowedProduct(inventory_lot_id=lot.id, product_id=product.id))
        db.commit();return lot.id


BASE = '/api/warehouse/semi-finished/bom-components/1'


def preview(client):
    result=client.get(BASE+'/manual-inventory')
    assert result.status_code == 200, result.text
    return result.json()


def payload(data, quantity=3, key='manual-sheet-operation-one'):
    row=data['items'][0]
    return {'component_type':'whole','lot_id':row['lot_id'], 'stock_quantity':quantity,
        'expected_version':row['version'],'expected_order_item_revision':data['expected_order_item_revision'],
        'review_hash':row['review_hash'],'warning_acknowledged_codes':row['warning_codes'],'idempotency_key':key}


def test_readonly_preview_and_user_quantity_double_splice(composite_requisition_app):
    from app.models.warehouse_inventory import InventoryLot, OrderItemSemiRequirement, InventoryReservation
    app,sessions=composite_requisition_app;lot_id=seed(sessions,double=True)
    with TestClient(app) as client:
        _login(client)
        data=preview(client);assert data['pieces_per_box']==2;assert data['remaining_required_piece_qty']==40
        with sessions() as db:
            assert db.scalar(select(func.count(OrderItemSemiRequirement.id)))==0
            assert db.scalar(select(func.count(InventoryReservation.id)))==0
        request=payload(data,7)
        result=client.post(BASE+'/manual-reserve',json=request)
        assert result.status_code==200,result.text
        assert result.json()['stock_quantity']==7
        assert result.json()['allocated_requirement_quantity']==7
        assert result.json()['remaining_required_piece_qty']==33
        assert client.post(BASE+'/manual-reserve',json=request).json()['replayed'] is True
        changed={**request,'stock_quantity':8}
        assert client.post(BASE+'/manual-reserve',json=changed).status_code==409
    with sessions() as db:
        lot=db.get(InventoryLot,lot_id)
        assert (lot.quantity_available,lot.quantity_reserved)==(197,7)
        assert db.scalar(select(func.count(InventoryReservation.id)))==1


def test_partial_batches_use_stock_mold_yield_once_not_supplier_splitting(composite_requisition_app):
    app,sessions=composite_requisition_app;seed(sessions,yield_factor=3)
    with TestClient(app) as client:
        _login(client);data=preview(client)
        assert data['items'][0]['stock_yield_per_sheet']==3
        first=client.post(BASE+'/manual-reserve',json=payload(data,2))
        assert first.status_code==200,first.text
        assert first.json()['allocated_requirement_quantity']==6  # not 2 * (2 supplier splits * 3 molds)
        fresh=preview(client)
        second=client.post(BASE+'/manual-reserve',json=payload(fresh,1,'manual-sheet-operation-two'))
        assert second.status_code==200,second.text
        assert second.json()['remaining_required_piece_qty']==11


@pytest.mark.parametrize('change',[{'stock_quantity':205},{'stock_quantity':21},{'stock_quantity':0},{'stock_quantity':1.5},{'warning_acknowledged_codes':[]}])
def test_invalid_or_unreviewed_quantity_cannot_reserve(composite_requisition_app,change):
    from app.models.warehouse_inventory import InventoryLot,InventoryReservation,OrderItemSemiRequirement
    app,sessions=composite_requisition_app;lot_id=seed(sessions)
    with TestClient(app) as client:
        _login(client);request=payload(preview(client));request.update(change)
        response=client.post(BASE+'/manual-reserve',json=request)
        assert response.status_code in (409,422),response.text
    with sessions() as db:
        assert db.get(InventoryLot,lot_id).quantity_available==204
        assert db.scalar(select(func.count(InventoryReservation.id)))==0
        assert db.scalar(select(func.count(OrderItemSemiRequirement.id)))==0


def test_stale_lot_and_frozen_order_contract(composite_requisition_app):
    from app.models.warehouse_inventory import InventoryLot
    from app.models.product import Product
    from app.models.product_bom import SalesOrderItemBomComponent
    app,sessions=composite_requisition_app;lot_id=seed(sessions)
    with TestClient(app) as client:
        _login(client);data=preview(client)
        with sessions() as db:
            snapshot=db.get(SalesOrderItemBomComponent,1)
            db.get(Product,snapshot.component_product_id).report_length_mm=9999
            db.commit()
        assert preview(client)['board_length_mm']==1000
        with sessions() as db:
            db.get(InventoryLot,lot_id).version+=1;db.commit()
        response=client.post(BASE+'/manual-reserve',json=payload(data))
        assert response.status_code==409,response.text


def test_reviewed_raw_material_variance_and_trim_preserve_original_stock(composite_requisition_app):
    from decimal import Decimal
    from app.models.warehouse_inventory import InventoryLot, SemiFinishedInventoryDetail, InventoryReservation
    app,sessions=composite_requisition_app;lot_id=seed(sessions,double=True)
    with sessions() as db:
        detail=db.get(SemiFinishedInventoryDetail,lot_id)
        detail.material_code_snapshot=detail.normalized_material_code='X616X'
        detail.board_width_mm=724
        db.get(InventoryLot,lot_id).estimated_unit_cost_snapshot=Decimal('1.5000')
        db.commit()
    with TestClient(app) as client:
        _login(client);data=preview(client);row=data['items'][0]
        assert row['selectable'] is True
        assert 'material_code' in row['signature_differences']
        assert row['cut_plan']['target_width_mm']==700
        request=payload(data,7)
        response=client.post(BASE+'/manual-reserve',json=request)
        assert response.status_code==200,response.text
    with sessions() as db:
        assert db.get(SemiFinishedInventoryDetail,lot_id).material_code_snapshot=='X616X'
        assert db.get(SemiFinishedInventoryDetail,lot_id).board_width_mm==724
        assert db.get(InventoryLot,lot_id).estimated_unit_cost_snapshot==Decimal('1.5000')
        reservation=db.scalar(select(InventoryReservation))
        assert reservation.reserved_stock_quantity==7
        assert reservation.credited_requirement_quantity==7


def test_foreign_customer_and_wrong_flute_not_selectable(composite_requisition_app):
    from app.models.customer import Customer
    from app.models.warehouse_inventory import SemiFinishedInventoryDetail
    from decimal import Decimal
    app,sessions=composite_requisition_app;lot_id=seed(sessions)
    with sessions() as db:
        db.add(Customer(customer_number=2,customer_code='FOREIGN',name='另一客户',payment_term_days=30,credit_limit=Decimal(0)))
        db.flush();db.get(SemiFinishedInventoryDetail,lot_id).owner_customer_id=2;db.commit()
    with TestClient(app) as client:
        _login(client);assert preview(client)['items']==[]
        with sessions() as db:
            detail=db.get(SemiFinishedInventoryDetail,lot_id);detail.owner_customer_id=None;detail.flute_type='BE';db.commit()
        assert preview(client)['items']==[]


def test_choose_100_from_204_does_not_use_the_whole_batch(composite_requisition_app):
    from decimal import Decimal
    from app.models.product_bom import SalesOrderItemBomComponent
    from app.models.order import OrderItem
    from app.models.warehouse_inventory import InventoryLot
    app,sessions=composite_requisition_app;lot_id=seed(sessions,double=True)
    with sessions() as db:
        db.get(OrderItem,1).quantity=100
        snapshot=db.get(SalesOrderItemBomComponent,1)
        snapshot.order_set_quantity=100;snapshot.required_piece_quantity=Decimal(200)
        db.commit()
    with TestClient(app) as client:
        _login(client);data=preview(client)
        assert data['items'][0]['max_stock_quantity']==204
        response=client.post(BASE+'/manual-reserve',json=payload(data,100))
        assert response.status_code==200,response.text
        assert response.json()['allocated_requirement_quantity']==100
        assert response.json()['remaining_required_piece_qty']==300
    with sessions() as db:
        lot=db.get(InventoryLot,lot_id)
        assert (lot.quantity_available,lot.quantity_reserved)==(104,100)


def test_last_sheet_rounding_and_changed_order_rejects_old_review(composite_requisition_app):
    from app.models.order import OrderItem
    app,sessions=composite_requisition_app;seed(sessions,yield_factor=3)
    with TestClient(app) as client:
        _login(client);data=preview(client)
        assert data['items'][0]['max_stock_quantity']==7
        old=payload(data,7)
        with sessions() as db:
            db.get(OrderItem,1).quantity+=1;db.commit()
        assert client.post(BASE+'/manual-reserve',json=old).status_code==409
        current=preview(client)
        result=client.post(BASE+'/manual-reserve',json=payload(current,7,'manual-sheet-rounding'))
        assert result.status_code==200,result.text
        assert result.json()['allocated_requirement_quantity']==20
        assert result.json()['remaining_required_piece_qty']==0


def test_audit_failure_rolls_back_stock_requirement_and_learning(composite_requisition_app,monkeypatch):
    import app.api.warehouse as api
    from app.models.warehouse_inventory import InventoryLot,OrderItemSemiRequirement,InventoryReservation
    app,sessions=composite_requisition_app;lot_id=seed(sessions)
    def fail(*args,**kwargs):raise RuntimeError('audit unavailable')
    with TestClient(app,raise_server_exceptions=False) as client:
        _login(client);request=payload(preview(client));monkeypatch.setattr(api,'append_audit_event',fail)
        assert client.post(BASE+'/manual-reserve',json=request).status_code==500
    with sessions() as db:
        assert db.get(InventoryLot,lot_id).quantity_available==204
        assert db.scalar(select(func.count(OrderItemSemiRequirement.id)))==0
        assert db.scalar(select(func.count(InventoryReservation.id)))==0


def test_read_and_write_require_customer_scope_and_reserve_permission(composite_requisition_app):
    app,sessions=composite_requisition_app;seed(sessions)
    with TestClient(app) as client:
        assert client.get(BASE+'/manual-inventory').status_code==401
        _login(client);request=payload(preview(client))
        from app.models.access_control import UserPermissionOverride
        from app.models.user import User
        with sessions() as db:
            db.get(User,1).role='finance'
            db.get(User,1).customer_access_mode='all'
            db.add(UserPermissionOverride(user_id=1,permission_code='warehouse.reserve',is_allowed=False));db.commit()
        assert client.post(BASE+'/manual-reserve',json=request).status_code==403


def test_manual_partial_reservation_refreshes_purchase_plan_and_saves_remaining_only(composite_requisition_app):
    from tests.test_n039_composite_bom_requisition import _component_payload, _parent_payload
    from app.models.requisition import RequisitionItem
    app,sessions=composite_requisition_app;seed(sessions,double=True)
    with TestClient(app) as client:
        _login(client)
        original=client.post('/api/requisition/bom-sheet-cutting/preview',json=_component_payload(1))
        assert original.status_code==200,original.text
        data=preview(client)
        result=client.post(BASE+'/manual-reserve',json=payload(data,7))
        assert result.status_code==200,result.text
        updated=client.post('/api/requisition/bom-sheet-cutting/preview',json=_component_payload(1))
        assert updated.status_code==200,updated.text
        draft=updated.json()
        assert draft['remaining_required_piece_qty']==33
        assert draft['order_purpose_sheet_qty']==33
        assert draft['purpose_plan_fingerprint']!=original.json()['purpose_plan_fingerprint']
        request={**_component_payload(1,requisition_qty=33),
            'purchase_total_sheet_qty':33,'order_purpose_sheet_qty':33,'stock_purpose_sheet_qty':0,
            'purpose_plan_version':draft['purpose_plan_version'],
            'purpose_plan_fingerprint':draft['purpose_plan_fingerprint']}
        saved=client.post('/api/requisition/batches',json={'supplier_name':'N039 供应商','items':[_parent_payload(),request,_component_payload(2)]})
        assert saved.status_code==201,saved.text
        assert client.get(BASE+'/manual-inventory').status_code==409
        # Same operation remains replayable even after the order has been reported.
        replay=client.post(BASE+'/manual-reserve',json=payload(data,7))
        assert replay.status_code==200 and replay.json()['replayed']
    with sessions() as db:
        assert [row.requisition_qty for row in db.scalars(select(RequisitionItem).order_by(RequisitionItem.id))]==[10,33,30]


def test_old_auto_cover_still_skips_general_and_material_variance(composite_requisition_app):
    from app.models.warehouse_inventory import InventoryLot,SemiFinishedInventoryDetail
    app,sessions=composite_requisition_app;lot_id=seed(sessions)
    with TestClient(app) as client:
        _login(client)
        auto=client.post('/api/warehouse/finished/bom-components/1/auto-cover',json={
            'order_item_id':1,'idempotency_key':'auto-general-manual-regression'})
        assert auto.status_code==200,auto.text
        assert auto.json()['semi_finished_reserved_piece_qty']==0
        assert preview(client)['items'][0]['selectable']
        with sessions() as db:
            detail=db.get(SemiFinishedInventoryDetail,lot_id)
            detail.material_code_snapshot=detail.normalized_material_code='X616X';db.commit()
        auto=client.post('/api/warehouse/finished/bom-components/1/auto-cover',json={
            'order_item_id':1,'idempotency_key':'auto-variance-manual-regression'})
        assert auto.status_code==200,auto.text
        assert auto.json()['semi_finished_reserved_piece_qty']==0
    with sessions() as db:assert db.get(InventoryLot,lot_id).quantity_available==204


def ordinary_seed(sessions):
    from app.models.order import OrderItem
    from app.models.product import Product
    from app.models.product_bom import SalesOrderItemBomComponent
    from sqlalchemy import delete
    lot_id=seed(sessions)
    with sessions() as db:
        item=db.get(OrderItem,1);product=db.get(Product,item.product_id)
        db.execute(delete(SalesOrderItemBomComponent))
        product.is_composite=False;product.layer_count=5;product.flute_type='AB'
        product.default_material_code='K616K';product.box_style='A1/0201 普通开槽箱'
        item.snapshot_report_length_mm=1000;item.snapshot_report_width_mm=700;item.flute_type='AB'
        item.snapshot_pieces_per_box=2;item.snapshot_splice_mode='double'
        db.commit()
    return lot_id


def test_ordinary_double_splice_operator_chooses_sheets_and_pending_updates(composite_requisition_app):
    from app.models.warehouse_inventory import InventoryLot
    app,sessions=composite_requisition_app;lot_id=ordinary_seed(sessions)
    base='/api/warehouse/semi-finished/order-items/1'
    with TestClient(app) as client:
        _login(client)
        read=client.get(base+'/manual-inventory');assert read.status_code==200,read.text
        data=read.json();assert data['remaining_required_piece_qty']==20
        request=payload(data,7,'ordinary-double-seven')
        result=client.post(base+'/manual-reserve',json=request)
        assert result.status_code==200,result.text
        assert result.json()['remaining_required_piece_qty']==13
        assert client.post(base+'/manual-reserve',json=request).json()['replayed']
        pending=client.get('/api/requisition/pending');assert pending.status_code==200,pending.text
        row=pending.json()['items'][0]
        assert row['semi_finished_reserved_piece_qty']==7
        assert row['requisition_qty']==13
    with sessions() as db:
        lot=db.get(InventoryLot,lot_id);assert (lot.quantity_available,lot.quantity_reserved)==(197,7)


def test_ordinary_reviewed_raw_material_trim_and_geometry_yield(composite_requisition_app):
    from app.models.warehouse_inventory import InventoryLot,SemiFinishedInventoryDetail
    app,sessions=composite_requisition_app;lot_id=ordinary_seed(sessions)
    with sessions() as db:
        detail=db.get(SemiFinishedInventoryDetail,lot_id)
        detail.board_length_mm=2000;detail.board_width_mm=724
        detail.material_code_snapshot=detail.normalized_material_code='X616X';db.commit()
    base='/api/warehouse/semi-finished/order-items/1'
    with TestClient(app) as client:
        _login(client);read=client.get(base+'/manual-inventory')
        assert read.status_code==200,read.text
        data=read.json();row=data['items'][0]
        assert row['cut_plan']['yield_factor']==2
        result=client.post(base+'/manual-reserve',json=payload(data,3,'ordinary-trim-three'))
        assert result.status_code==200,result.text
        assert result.json()['allocated_requirement_quantity']==6
        assert result.json()['remaining_required_piece_qty']==14
    with sessions() as db:
        assert db.get(InventoryLot,lot_id).quantity_available==201
        assert db.get(SemiFinishedInventoryDetail,lot_id).board_length_mm==2000


@pytest.mark.parametrize('bom',[True,False])
def test_force_closed_order_rejects_new_manual_reservation(composite_requisition_app,bom):
    from app.models.order import OrderItem
    app,sessions=composite_requisition_app
    seed(sessions) if bom else ordinary_seed(sessions)
    base=BASE if bom else '/api/warehouse/semi-finished/order-items/1'
    with TestClient(app) as client:
        _login(client);data=client.get(base+'/manual-inventory').json()
        with sessions() as db:db.get(OrderItem,1).is_force_closed=True;db.commit()
        response=client.post(base+'/manual-reserve',json=payload(data))
        assert response.status_code==409,response.text
