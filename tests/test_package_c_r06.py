import json
from test_material_candidates import prepare
from test_semi_finished_lot_product_bindings import lot_db
from app.api.warehouse_goods import GoodsFacts, GoodsUpdate, update_goods, get_goods
from app.models.warehouse_goods import WarehouseGoodsProfile
import pytest
from fastapi import HTTPException
from app.api.warehouse_goods import preview_correction
from app.models.warehouse_inventory import InventoryLot
from tests.test_p1_47d_inventory_adjustment import stocktake_app, _login


def test_r06_correction_keeps_original_customer_and_physical_provenance(lot_db):
    db,data,lot,user=prepare(lot_db)
    original_customer=lot.semi_finished_detail.owner_customer_id
    original_name=lot.semi_finished_detail.internal_name
    db.add(WarehouseGoodsProfile(lot_id=lot.id,data_json=json.dumps(dict(
        scope='customers',customer_ids=[original_customer],product_ids=[],processing='cut',
        mold_tool_id=None,verified_material_id=None,material_code='A416D',face_paper='kraft',
        output_piece=True,physical_basis='frozen-basis',dimension_basis='source_board'))))
    db.commit()
    payload=GoodsUpdate(facts=GoodsFacts(processing='cut',display_name='盘点核实名',note='盘点备注'),
        expected_version=lot.version,idempotency_key='r06-correction-name',correction_reason='录入名称错误')
    update_goods(lot.id,payload,db,user)
    current=get_goods(lot.id,db,user)
    assert current['physical']['name']=='盘点核实名'
    assert current['facts']['source_customer_id']==original_customer
    assert current['facts']['physical_basis']=='frozen-basis'
    assert current['facts']['output_piece'] is True
    assert lot.semi_finished_detail.owner_customer_id==original_customer
    assert lot.semi_finished_detail.internal_name=='盘点核实名'
    assert current['facts']['source_display_name']==(original_name or '')


def test_r06_master_sync_requires_matching_preview_and_default_does_not_sync(lot_db):
    db,data,lot,user=prepare(lot_db)
    product=data['products'][0];original=product.product_name
    payload=GoodsUpdate(facts=GoodsFacts(scope='customers',customer_ids=[product.customer_id],product_ids=[product.id],
        processing='cut',display_name='核实后的名称'),correction_reason='盘点核实',expected_version=lot.version,
        idempotency_key='r06-explicit-sync',sync_product_id=product.id)
    with pytest.raises(HTTPException):update_goods(lot.id,payload,db,user)
    assert product.product_name==original
    preview=preview_correction(lot.id,payload,db,user)
    payload.impact_fingerprint=preview['fingerprint']
    result=update_goods(lot.id,payload,db,user)
    db.refresh(product)
    assert product.product_name=='核实后的名称'
    assert update_goods(lot.id,payload,db,user)==result


def test_r06_partial_scope_splits_only_available_quantity(lot_db):
    db,data,lot,user=prepare(lot_db)
    payload=GoodsUpdate(facts=GoodsFacts(processing='cut',display_name='转通用部分'),correction_reason='部分转通用',
        correction_quantity=3,expected_version=lot.version,idempotency_key='r06-partial-scope')
    result=update_goods(lot.id,payload,db,user)
    assert result['lot_id']!=lot.id
    db.refresh(lot)
    target=db.get(InventoryLot,result['lot_id'])
    assert (lot.quantity_available,target.quantity_available)==(7,3)
    assert lot.warehouse_location_id==target.warehouse_location_id
    assert db.get(WarehouseGoodsProfile,lot.id) is None
    assert target.semi_finished_detail.owner_customer_id==lot.semi_finished_detail.owner_customer_id
    assert update_goods(lot.id,payload,db,user)==result


def test_r06_used_lot_history_and_split_audit_are_atomic(lot_db,monkeypatch):
    from sqlalchemy import select,func
    from app.models.audit import OperationLog
    db,data,lot,user=prepare(lot_db)
    lot.quantity_available=2;lot.quantity_consumed=8;db.commit()
    before_count=db.scalar(select(func.count()).select_from(InventoryLot))
    payload=GoodsUpdate(facts=GoodsFacts(processing='cut',display_name='剩余片料'),correction_reason='剩余用途核实',
        expected_version=lot.version,idempotency_key='r06-used-remainder')
    original=db.add
    with monkeypatch.context() as patch:
        def fail(row):
            if isinstance(row,OperationLog):raise RuntimeError('audit unavailable')
            original(row)
        patch.setattr(db,'add',fail)
        with pytest.raises(RuntimeError):update_goods(lot.id,payload,db,user)
    db.refresh(lot)
    assert (lot.quantity_available,lot.quantity_consumed)==(2,8)
    assert db.scalar(select(func.count()).select_from(InventoryLot))==before_count
    result=update_goods(lot.id,payload,db,user)
    db.refresh(lot)
    assert (lot.quantity_available,lot.quantity_consumed)==(0,8)
    assert db.get(WarehouseGoodsProfile,lot.id) is None
    assert db.get(InventoryLot,result['lot_id']).quantity_available==2


def test_r06_pallet_partial_correction_preserves_physical_total(lot_db):
    from app.models.warehouse_inventory import InventoryPallet, InventoryPalletItem
    db,data,lot,user=prepare(lot_db)
    pallet=InventoryPallet(pallet_code='R06-TEST',location_id=lot.warehouse_location_id,
        is_current=True,status='active')
    db.add(pallet);db.flush()
    original=InventoryPalletItem(pallet_id=pallet.id,inventory_lot_id=lot.id,
        customer_id=lot.semi_finished_detail.owner_customer_id,product_name='原名',
        item_type='semi_finished',quantity=10,unit='sheets',match_status='pending')
    db.add(original);db.commit();db.expire(lot,['pallet_item'])
    payload=GoodsUpdate(facts=GoodsFacts(processing='cut',display_name='修正部分'),
        correction_reason='核实部分用途',correction_quantity=3,expected_version=lot.version,
        idempotency_key='r06-pallet-partial')
    result=update_goods(lot.id,payload,db,user)
    target=db.get(InventoryLot,result['lot_id'])
    assert target.pallet_item.pallet_id==pallet.id
    assert (original.quantity,target.pallet_item.quantity)==(7,3)
    assert (lot.quantity_available,target.quantity_available)==(7,3)
    assert target.warehouse_location_id==lot.warehouse_location_id
    assert update_goods(lot.id,payload,db,user)==result


def test_r06_default_master_unchanged_and_audit_contains_old_values(lot_db):
    from app.models.audit import OperationLog
    from sqlalchemy import select
    db,data,lot,user=prepare(lot_db)
    product=data['products'][0]; original=product.product_name
    old_name=lot.semi_finished_detail.internal_name or ''
    payload=GoodsUpdate(facts=GoodsFacts(processing='cut',display_name='核实名称'),
        correction_reason='标签有误',expected_version=lot.version,idempotency_key='r06-default-audit')
    update_goods(lot.id,payload,db,user)
    assert product.product_name==original
    audit=db.scalar(select(OperationLog).where(OperationLog.entity_type=='warehouse_goods_profile'))
    details=json.loads(audit.details)
    assert details['before']['display_name']==old_name
    assert details['after']['display_name']=='核实名称'
    assert details['reason']=='标签有误'
    with pytest.raises(HTTPException):
        update_goods(lot.id,payload.model_copy(update={'correction_reason':'不同请求'}),db,user)


def finished_fixture(lot_db):
    from app.models.warehouse_inventory import FinishedGoodsInventoryDetail
    from app.services.finished_stock_identity import product_basis
    from app.services.warehouse_sheet_transfer import _copy_columns
    db,data,semi,user=prepare(lot_db)
    product=data['products'][0]
    lot=InventoryLot(**_copy_columns(semi,{'id','lot_number','inventory_type','unit'}),
        lot_number='R06-FINISHED',inventory_type='finished',unit='boxes')
    lot.finished_detail=FinishedGoodsInventoryDetail(product_id=product.id,
        owner_customer_id=product.customer_id,owner_customer_name_snapshot=data['customer'].name,
        is_general=False,inventory_code_snapshot=product.product_code,product_name_snapshot='原成品名',
        physical_basis_json=product_basis(product))
    db.add(lot);db.commit()
    return db,data,lot,user


def test_r06_finished_general_partial_preserves_history(lot_db):
    db,data,lot,user=finished_fixture(lot_db)
    original_customer=lot.finished_detail.owner_customer_id
    payload=GoodsUpdate(facts=GoodsFacts(processing='cut',display_name='核实成品名'),
        correction_reason='部分转通用',correction_quantity=3,expected_version=lot.version,
        idempotency_key='r06-finished-partial')
    result=update_goods(lot.id,payload,db,user)
    target=db.get(InventoryLot,result['lot_id'])
    assert target.finished_detail.is_general
    assert not lot.finished_detail.is_general
    assert lot.finished_detail.product_name_snapshot=='原成品名'
    assert target.finished_detail.product_name_snapshot=='核实成品名'
    assert result['facts']['source_customer_id']==original_customer
    assert (lot.quantity_available,target.quantity_available)==(7,3)
    assert get_goods(target.id,db,user)['inventory_type']=='finished'
    assert update_goods(lot.id,payload,db,user)==result


def test_r06_finished_different_physical_product_is_rejected(lot_db):
    db,data,lot,user=finished_fixture(lot_db)
    other=data['other_product']; other.length_mm=1234; db.commit()
    payload=GoodsUpdate(facts=GoodsFacts(processing='cut',display_name='改客户',scope='customers',
        customer_ids=[other.customer_id],product_ids=[other.id]),correction_reason='核对',
        expected_version=lot.version,idempotency_key='r06-finished-identity')
    with pytest.raises(HTTPException,match='冻结实物'):
        update_goods(lot.id,payload,db,user)
    assert lot.quantity_available==10 and lot.finished_detail.product_id==data['products'][0].id


def test_r06_http_permissions_and_reason(stocktake_app):
    from fastapi.testclient import TestClient
    from sqlalchemy import select
    from app.api.warehouse_goods import router
    from app.models.user import User
    app,factory,ids,_=stocktake_app
    app.include_router(router,prefix='/api/warehouse/goods')
    with factory() as db:
        lot=db.scalar(select(InventoryLot).where(InventoryLot.inventory_type=='finished',
            InventoryLot.status=='active',InventoryLot.quantity_available>0,InventoryLot.quantity_reserved==0))
        user=db.get(User,ids['admin'])
        source=get_goods(lot.id,db,user)
        lot_id=lot.id; quantity=lot.quantity_available
    body=dict(facts=source['facts'],expected_version=source['version'],idempotency_key='r06-http-permission')
    with TestClient(app) as client:
        assert client.put(f'/api/warehouse/goods/{lot_id}',json=body).status_code==401
        _login(client)
        assert client.put(f'/api/warehouse/goods/{lot_id}',json=body).status_code==403
        _login(client,'p147d-admin')
        assert client.put(f'/api/warehouse/goods/{lot_id}',json=body).status_code==422
        body['correction_reason']='现场核实名称'
        body['facts']['display_name']='HTTP核实名称'
        response=client.put(f'/api/warehouse/goods/{lot_id}',json=body)
        assert response.status_code==200,response.text
        assert client.put(f'/api/warehouse/goods/{lot_id}',json=body).json()==response.json()
    with factory() as db:
        assert db.get(InventoryLot,lot_id).quantity_available==quantity
