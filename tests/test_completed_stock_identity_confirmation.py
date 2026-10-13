import json
import pytest
from sqlalchemy import select, func
from datetime import date, datetime
from sqlalchemy.orm import sessionmaker
from app.core.database import create_sqlite_engine
from app.models import Base
from app.models.user import User
from app.models.customer import Customer
from app.models.product import Product
from app.models.warehouse_inventory import InventoryLot, SemiFinishedInventoryDetail, WarehouseLocation

@pytest.fixture
def lot_db(tmp_path):
    engine=create_sqlite_engine(tmp_path/'completed-identity.sqlite3')
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine,expire_on_commit=False)() as db:
        admin=User(username='identity-admin',password_hash='test',role='admin',real_name='管理员',is_active=True)
        customer=Customer(customer_number=9100,customer_code='IDENTITY',name='测试客户')
        location=WarehouseLocation(location_code='IDENTITY-SF',location_name='已盘点半成品位置',warehouse_type='semi_finished')
        db.add_all([admin,customer,location]);db.flush()
        products=[Product(customer_id=customer.id,product_code='CHILD-'+str(i),customer_material_code='CHILD-'+str(i),product_name='子件',unit='片') for i in range(2)]
        db.add_all(products);db.flush()
        lot=InventoryLot(lot_number='COUNTED-CHILD',inventory_type='semi_finished',warehouse_location_id=location.id,
            quantity_available=10,unit='sheets',status='active',source_type='manual',stock_date=date.today(),
            last_movement_at=datetime.now(),estimated_unit_cost_snapshot=2,cost_snapshot_detail_json='{"unit_cost":2}')
        lot.semi_finished_detail=SemiFinishedInventoryDetail(owner_customer_id=customer.id,
            material_code_snapshot='A416D',normalized_material_code='A416D',layer_count=3,flute_type='B',
            board_length_mm=800,board_width_mm=600,component_type='whole',sheet_type='net_sheet',
            stock_yield_per_sheet=1,pieces_per_box=1)
        db.add(lot);db.commit()
        yield db,dict(admin=admin,customer=customer,products=products,lot=lot)
    engine.dispose()

from app.models.warehouse_goods import WarehouseGoodsProfile
from app.models.warehouse_inventory import InventoryMovement
from app.services.finished_stock_identity import identity_preview, confirm_identity, product_basis
from app.services.processed_component_stock import output_identity, available_outputs
from app.services.warehouse_inventory import WarehouseInventoryError


def prepare(lot_db):
    db, data = lot_db
    lot, product = data['lot'], data['products'][0]
    db.add(WarehouseGoodsProfile(lot_id=lot.id, data_json=json.dumps(dict(
        scope='customers', customer_ids=[data['customer'].id], product_ids=[product.id],
        processing='die_cut', material_code='A416D'))))
    db.commit()
    return db, data, lot, product


def facts(lot):
    return tuple(getattr(lot, key) for key in ('quantity_available','quantity_reserved',
        'quantity_consumed','quantity_damaged','unit','source_ref_type','source_ref_id',
        'estimated_unit_cost_snapshot','cost_snapshot_detail_json','warehouse_location_id'))


def test_confirm_completed_piece_preserves_ledger_and_replays(lot_db):
    db, data, lot, product = prepare(lot_db)
    before = facts(lot)
    assert output_identity(db, lot) is None
    preview = identity_preview(db, lot.id)
    args=dict(lot_id=lot.id,preview_hash=preview['preview_hash'],operation_key='confirm-completed',actor=data['admin'])
    result=confirm_identity(db, **args)
    assert not result['replayed']
    assert output_identity(db, lot)[:2] == (product.id,product.customer_id)
    assert lot.id in [row.id for row in available_outputs(db,product_id=product.id,
        customer_id=product.customer_id,expected_basis=product_basis(product))]
    assert facts(lot)==before
    assert confirm_identity(db,**args)['replayed']
    movements=list(db.scalars(select(InventoryMovement).where(InventoryMovement.reason=='确认批次规格工艺')))
    assert len(movements)==1 and movements[0].quantity==0
    with pytest.raises(WarehouseInventoryError,match='已有完成身份'): identity_preview(db,lot.id)


@pytest.mark.parametrize('change',['raw','many','yield','used','parent'])
def test_cannot_guess_completion_or_convert_quantities(lot_db,change):
    db,data,lot,product=prepare(lot_db)
    record=db.get(WarehouseGoodsProfile,lot.id);profile=json.loads(record.data_json)
    if change=='raw':profile['processing']='raw'
    if change=='many':profile['product_ids'].append(data['products'][1].id)
    if change=='yield':lot.semi_finished_detail.stock_yield_per_sheet=4
    if change=='used':lot.quantity_consumed=1
    if change=='parent':product.is_composite=True
    record.data_json=json.dumps(profile);db.commit()
    with pytest.raises(WarehouseInventoryError):identity_preview(db,lot.id)
    assert output_identity(db,lot) is None


def test_changed_preview_and_failed_audit_are_atomic(lot_db,monkeypatch):
    db,data,lot,product=prepare(lot_db)
    preview=identity_preview(db,lot.id)
    product.production_notes='不同工艺';db.commit()
    with pytest.raises(WarehouseInventoryError,match='已变化'):
        confirm_identity(db,lot_id=lot.id,preview_hash=preview['preview_hash'],operation_key='stale',actor=data['admin'])
    preview=identity_preview(db,lot.id);before=facts(lot);version=lot.version
    def fail(*args,**kwargs):raise RuntimeError('audit failure')
    monkeypatch.setattr('app.services.audit_log.append_audit_event',fail)
    with pytest.raises(RuntimeError,match='audit failure'):
        confirm_identity(db,lot_id=lot.id,preview_hash=preview['preview_hash'],operation_key='failed-audit',actor=data['admin'])
    db.expire_all()
    assert facts(lot)==before and lot.version==version and output_identity(db,lot) is None


def test_confirmed_identity_cannot_be_erased_by_regular_goods_edit(lot_db):
    from fastapi import HTTPException
    from app.api.warehouse_goods import update_goods, GoodsUpdate, GoodsFacts
    from app.services.warehouse_display_units import lot_display_unit
    db,data,lot,product=prepare(lot_db)
    preview=identity_preview(db,lot.id)
    confirm_identity(db,lot_id=lot.id,preview_hash=preview['preview_hash'],operation_key='protect-confirmed',actor=data['admin'])
    db.commit();before=facts(lot)
    assert lot_display_unit(lot)=='片'
    with pytest.raises(HTTPException,match='冻结完成身份'):
        update_goods(lot.id,GoodsUpdate(facts=GoodsFacts(),expected_version=lot.version,
            idempotency_key='erase-confirmation',correction_reason='验证不得覆盖'),db,data['admin'])
    assert output_identity(db,lot) is not None and facts(lot)==before
