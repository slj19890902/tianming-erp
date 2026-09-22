from datetime import date, datetime
from decimal import Decimal
import pytest
from tests.test_inventory_cost_snapshot import db
from tests.test_inventory_valuation import product
from app.models.warehouse_inventory import WarehouseLocation
from app.services.warehouse_inventory import manual_finished_in, WarehouseInventoryError
from tests.test_p1_47d_inventory_adjustment import stocktake_app


def enter(db, row, source='manual'):
    location = WarehouseLocation(location_code='ENTRY-COMPLETE', location_name='实物入库位', warehouse_type='finished')
    db.add(location); db.flush()
    return manual_finished_in(db, customer_id=row.customer_id, product_id=row.id,
        location_id=location.id, quantity=2, stock_date=date(2026,9,22), source_type=source,
        remarks=None, operator_id=None, idempotency_key='complete-entry')


def test_manual_path_cannot_silently_create_unpriced_inventory(db):
    row, material = product(db)
    material.quote_price = None
    with pytest.raises(WarehouseInventoryError, match='成本'):
        enter(db, row)


def test_new_entry_does_not_use_sale_as_material_cost(db):
    row, material = product(db, sale_unit_price=Decimal('99'))
    material.quote_price = None
    with pytest.raises(WarehouseInventoryError, match='成本'):
        enter(db, row, 'stocktake')


def test_new_entry_does_not_reuse_fixed_historical_reference_without_recipe(db):
    import json
    from app.models.inventory_cost_rule import InventoryCostRule
    from app.models.user import User
    from app.services.inventory_valuation import resolve_product_cost
    row, material = product(db)
    material.quote_price = None
    actor=User(username='old-rule-admin',password_hash='isolated',real_name='测试',role='admin',is_active=True)
    db.add(actor);db.flush()
    db.add(InventoryCostRule(product_id=row.id, version=1, updated_by=actor.id,
        config_json=json.dumps(dict(mode='fixed',unit_cost='2',basis='旧库存历史参考'))))
    db.flush()
    assert resolve_product_cost(db,row).estimate is not None
    assert resolve_product_cost(db,row,for_entry=True).estimate is None


def test_missing_sales_unit_has_actionable_error(db):
    row, _ = product(db, unit='')
    with pytest.raises(WarehouseInventoryError, match='单位'):
        enter(db, row)


def unassigned_setup(db):
    from app.models.user import User
    from app.api.warehouse_goods import UnassignedFinishedEntry
    row, material = product(db)
    material.flute_type = 'B'
    user = User(username='entry-admin', password_hash='test-only', real_name='隔离测试', role='admin', is_active=True)
    loc = WarehouseLocation(location_code='UNKNOWN-ENTRY', location_name='待认领库位', warehouse_type='finished')
    from app.models.warehouse_inventory import Floor3LocationLayout
    loc.floor3_layout=Floor3LocationLayout(left_pct=1,top_pct=1,width_pct=1,height_pct=1,version=1,source_type='manual')
    loc.placement_status='placed'
    db.add_all([loc,user]); db.flush(); db.commit()
    payload = UnassignedFinishedEntry(location_id=loc.id,expected_layout_version=1,quantity=7,
        stock_date=date(2026,9,22),name='现场 A1 箱',length_mm=500,width_mm=300,height_mm=200,
        material_id=material.id,idempotency_key='unassigned-entry-test')
    return row, material, user, payload


def test_unknown_customer_a1_has_frozen_cost_no_fake_master_and_replay(db):
    from app.api.warehouse_goods import create_unassigned, preview_unassigned, get_goods
    from app.models.warehouse_inventory import InventoryLot, InventoryMovement
    from app.models.product import Product
    from app.models.customer import Customer
    from sqlalchemy import select, func
    from fastapi import HTTPException
    from app.services.warehouse_display_units import lot_display_unit
    row, material, user, payload = unassigned_setup(db)
    plan = preview_unassigned(payload, db, user)
    assert Decimal(plan['unit_cost']) == Decimal('1.6300')
    assert plan['evidence']['report_length_mm'] == 1630
    payload.fingerprint = plan['fingerprint']
    result = create_unassigned(payload, db, user)
    lot = db.get(InventoryLot,result['lot_id'])
    from app.core.inventory_entry_guard import validate_entries
    db.info['new_inventory_entry_ids'] = {lot.id}
    validate_entries(db)
    assert lot.finished_detail.product_id is None and lot.finished_detail.owner_customer_id is None
    assert lot_display_unit(lot) == '只'
    assert get_goods(lot.id, db, user)['facts']['material_confidence'] == 'estimated'
    material.quote_price = 10; db.commit()
    assert create_unassigned(payload,db,user) == result
    assert lot.estimated_unit_cost_snapshot == Decimal('1.6300')
    assert db.scalar(select(func.count()).select_from(InventoryMovement)) == 1
    assert db.scalar(select(func.count()).select_from(Product)) == 1
    assert db.scalar(select(func.count()).select_from(Customer)) == 1
    with pytest.raises(HTTPException) as error:
        create_unassigned(payload.model_copy(update={'quantity':8}),db,user)
    assert error.value.status_code == 409


def test_unknown_customer_quote_change_and_audit_failure_leave_no_stock(db,monkeypatch):
    from app.api.warehouse_goods import create_unassigned,preview_unassigned
    from app.models.warehouse_inventory import InventoryLot
    from app.models.audit import OperationLog
    from fastapi import HTTPException
    from sqlalchemy import select,func
    row,material,user,payload=unassigned_setup(db)
    payload.fingerprint=preview_unassigned(payload,db,user)['fingerprint']
    material.quote_price=3;material.version+=1;db.commit()
    with pytest.raises(HTTPException) as error:create_unassigned(payload,db,user)
    assert error.value.status_code==409
    payload.fingerprint=preview_unassigned(payload,db,user)['fingerprint']
    original=db.add
    def fail(obj):
        if isinstance(obj,OperationLog):raise RuntimeError('audit failed')
        return original(obj)
    monkeypatch.setattr(db,'add',fail)
    with pytest.raises(RuntimeError,match='audit failed'):create_unassigned(payload,db,user)
    assert db.scalar(select(func.count()).select_from(InventoryLot))==0


def test_strict_bom_cost_cannot_hide_unpriced_child_with_sale(db):
    from app.services.inventory_valuation import resolve_product_cost
    row,material=product(db,sale_unit_price=99)
    material.quote_price=None
    assert resolve_product_cost(db,row,for_entry=True).estimate is None
    assert resolve_product_cost(db,row).estimate is not None  # historical reference reader preserved


def test_manual_transfer_requires_cost_and_replay_rejects_changed_quantity(db):
    row,material=product(db)
    lot=enter(db,row,'transfer')
    assert lot.estimated_unit_cost_snapshot==Decimal('1.6300')
    with pytest.raises(WarehouseInventoryError,match='同一入库请求'):
        manual_finished_in(db,customer_id=row.customer_id,product_id=row.id,location_id=lot.warehouse_location_id,
            quantity=999,stock_date=date(2026,9,22),source_type='transfer',remarks=None,operator_id=None,idempotency_key='complete-entry')


def test_downgrade_blocks_unassigned_facts(db):
    import importlib.util
    from pathlib import Path
    from alembic.runtime.migration import MigrationContext
    from alembic.operations import Operations
    from app.api.warehouse_goods import create_unassigned,preview_unassigned
    _,_,user,payload=unassigned_setup(db)
    payload.fingerprint=preview_unassigned(payload,db,user)['fingerprint']
    create_unassigned(payload,db,user)
    spec=importlib.util.spec_from_file_location('entry_migration',Path(__file__).parents[1]/'alembic/versions/dz0922_unassigned_finished_identity.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    with Operations.context(MigrationContext.configure(db.connection())):
        with pytest.raises(RuntimeError,match='已有客户待认领库存'):module.downgrade()


def test_receipt_transaction_guard_rejects_new_zero_cost_and_preserves_old_move(db):
    from sqlalchemy.orm import sessionmaker
    from app.core.receipt_price_guard import ReceiptPriceGuardSession
    from app.models.warehouse_inventory import InventoryLot
    from fastapi import HTTPException
    from sqlalchemy import select,func
    row,material=product(db)
    location=WarehouseLocation(location_code='COMMIT',location_name='提交检查',warehouse_type='finished')
    db.add(location);db.commit()
    factory=sessionmaker(db.get_bind(),class_=ReceiptPriceGuardSession)
    with factory() as session:
        session.add(InventoryLot(lot_number='MISSING-COST',inventory_type='finished',warehouse_location_id=location.id,
            quantity_available=1,unit='boxes',status='active',source_type='production_completion',last_movement_at=datetime(2026,9,22),stock_date=date(2026,9,22)))
        with pytest.raises(HTTPException) as error:session.commit()
        assert '有效材料成本' in error.value.detail['missing']
        session.rollback()
        assert session.scalar(select(func.count()).select_from(InventoryLot))==0


def test_unknown_ground_projection_api_permissions_and_claim(stocktake_app):
    from tests.test_p1_47d_inventory_adjustment import _login
    from app.api.warehouse_goods import router
    from app.models.material import Material
    from app.models.product import Product
    from app.models.warehouse_inventory import InventoryLot, WarehouseGroundOccupancy
    from fastapi.testclient import TestClient
    from sqlalchemy import select
    app,factory,ids,_=stocktake_app
    app.include_router(router,prefix='/api/warehouse/goods')
    with factory() as db:
        material=Material(code='ENTRY3B',supplier_name='测试供应商',layer_count=3,flute_type='B',quote_price=2,
            price_unit='元/㎡',purchase_currency='CNY',purchase_tax_included=True)
        db.add(material);db.flush();mid=material.id
        product=db.get(Product,ids['product']);product.material_id=mid;product.splice_mode='single';product.unit='只';product.report_length_mm=1630;product.report_width_mm=500
        db.commit()
    payload=dict(location_id=ids['loc_fg1_add'],expected_layout_version=1,quantity=7,stock_date='2026-09-22',
        name='现场箱',length_mm=500,width_mm=300,height_mm=200,material_id=mid,idempotency_key='http-unassigned-entry')
    with TestClient(app) as client:
        _login(client,'p147d-operator')
        assert client.post('/api/warehouse/goods/finished-unassigned/preview',json=payload).status_code==403
        _login(client,'p147d-admin')
        response=client.post('/api/warehouse/goods/finished-unassigned/preview',json=payload)
        assert response.status_code==200,response.text
        payload['fingerprint']=response.json()['fingerprint']
        response=client.post('/api/warehouse/goods/finished-unassigned',json=payload)
        assert response.status_code==200,response.text
        lid=response.json()['lot_id']
        assert client.post('/api/warehouse/goods/finished-unassigned',json=payload).json()['lot_id']==lid
        with factory() as db:
            lot=db.get(InventoryLot,lid)
            assert lot.pallet_item and lot.pallet_item.match_status=='pending'
            occupancy=db.scalar(select(WarehouseGroundOccupancy).where(WarehouseGroundOccupancy.pallet_id==lot.pallet_item.pallet_id))
            assert occupancy and occupancy.product_id is None and occupancy.customer_id is None
        correction=dict(facts=dict(scope='customers',customer_ids=[ids['customer']],product_ids=[ids['product']],
            verified_material_id=mid),expected_version=1,idempotency_key='claim-unassigned-entry',correction_reason='现场核实客户及规格')
        response=client.put(f'/api/warehouse/goods/{lid}',json=correction)
        assert response.status_code==200,response.text
        with factory() as db:
            lot=db.get(InventoryLot,lid)
            assert lot.finished_detail.product_id==ids['product']
            assert lot.pallet_item.match_status=='matched'
            assert lot.estimated_unit_cost_snapshot==Decimal('1.6300')
            occupancy=db.scalar(select(WarehouseGroundOccupancy).where(WarehouseGroundOccupancy.pallet_id==lot.pallet_item.pallet_id))
            assert occupancy.product_id==ids['product'] and occupancy.customer_id==ids['customer']
