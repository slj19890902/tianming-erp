"""Missing department masters create no second inventory ledger."""
from unittest.mock import patch
import pytest
from sqlalchemy import select, func
from app.models.customer import Customer
from app.models.product import Product
from app.models.shared_finished_stock import SharedFinishedMember
from app.services import shared_customer_access as access, shared_finished_stock as shared
from app.services.warehouse_inventory import WarehouseInventoryError, finished_inventory_candidates_for_product
from tests.test_shared_finished_stock import reservation_db, setup_pair, activate


def prepare(db,data):
    _,_,lot=setup_pair(db,data)
    customer=Customer(name='虚构海外部门',customer_code='FICTION-OVERSEAS');db.add(customer);db.commit()
    return dict(source_product_id=data['product'].id,product_ids=[data['product'].id],
        new_customer_ids=[customer.id],lot_ids=[lot.id],auto_enroll=True),lot


def save(db,data,args,key='reference'):
    plan=access.preview(db,**args)
    return access.confirm(db,**args,actor=data['admin'],preview_hash=plan['preview_hash'],
        operation_key=key,evidence='虚构客户实物已核对'),plan


def test_reference_is_atomic_read_only_preview_and_one_stock(reservation_db):
    db,data=reservation_db;args,lot=prepare(db,data)
    before=(db.scalar(select(func.count()).select_from(Product)),lot.quantity_available,lot.quantity_reserved,
        lot.location.id,shared.lot_identity(lot))
    plan=access.preview(db,**args)
    assert db.scalar(select(func.count()).select_from(Product))==before[0]
    result,plan=save(db,data,args);db.commit()
    new=db.get(Product,result['created_sales_references'][0]['product_id'])
    assert new.customer_id==args['new_customer_ids'][0]
    assert new.sale_unit_price is new.sale_unit_price_no_tax is new.customer_drawing_number is new.customer_model is new.customer_category is None
    assert shared.member_identity(new)==shared.member_identity(data['product'])
    assert finished_inventory_candidates_for_product(db,product_id=new.id,customer_id=new.customer_id)==[lot]
    assert before[1:]==(lot.quantity_available,lot.quantity_reserved,lot.location.id,shared.lot_identity(lot))
    replay=access.confirm(db,**args,actor=data['admin'],preview_hash=plan['preview_hash'],operation_key='reference',evidence='虚构客户实物已核对')
    assert replay['replayed'] and replay['group_id']==result['group_id']


def test_stale_customer_and_no_admin_fail_without_product(reservation_db):
    db,data=reservation_db;args,lot=prepare(db,data);plan=access.preview(db,**args)
    db.get(Customer,args['new_customer_ids'][0]).version+=1;db.commit()
    with pytest.raises(WarehouseInventoryError,match='变化'):
        access.confirm(db,**args,actor=data['admin'],preview_hash=plan['preview_hash'],operation_key='stale',evidence='核对')
    db.rollback()
    with pytest.raises(WarehouseInventoryError,match='管理员'):
        access.confirm(db,**args,actor=None,preview_hash=plan['preview_hash'],operation_key='noadmin',evidence='核对')
    assert not db.scalar(select(Product.id).where(Product.customer_id==args['new_customer_ids'][0]))


def test_audit_failure_rolls_back_whole_creation(reservation_db):
    db,data=reservation_db;args,lot=prepare(db,data);version=lot.version
    with patch.object(access,'append_audit_event',side_effect=RuntimeError('audit-failed')):
        with pytest.raises(RuntimeError,match='audit-failed'):save(db,data,args)
    db.rollback()
    assert not db.scalar(select(Product.id).where(Product.customer_id==args['new_customer_ids'][0]))
    assert db.get(SharedFinishedMember,data['product'].id) is None
    assert lot.version==version


def test_append_to_existing_group_and_duplicate_blocked(reservation_db):
    db,data=reservation_db;args,lot=prepare(db,data)
    target=db.scalar(select(Product).where(Product.customer_id==data['other_customer'].id))
    gid=activate(db,data,target,lot)['group_id']
    args.update(lot_ids=[],expected_group_version=1,auto_enroll=None,disabled_product_ids=[target.id])
    result,_=save(db,data,args);db.commit()
    assert result['group_id']==gid
    with pytest.raises(WarehouseInventoryError,match='变化|已有'):
        access.preview(db,**args)


def test_duplicate_code_and_bom_not_silently_created(reservation_db):
    db,data=reservation_db;args,lot=prepare(db,data)
    source=data['product'];source.is_composite=True;db.commit()
    with pytest.raises(WarehouseInventoryError,match='BOM'):access.preview(db,**args)
    source.is_composite=False
    db.add(Product(customer_id=args['new_customer_ids'][0],product_code=source.product_code,
        product_name='已存在',customer_material_code='已有',is_active=False));db.commit()
    with pytest.raises(WarehouseInventoryError,match='已有'):access.preview(db,**args)


def test_private_old_lot_proof_is_not_a_public_bypass(reservation_db):
    db,data=reservation_db;args,lot=prepare(db,data);value=access.preview(db,**args)
    with pytest.raises(WarehouseInventoryError,match='维护'):
        access.confirm(db,**args,actor=data['admin'],preview_hash=value['preview_hash'],operation_key='bad-proof',
            evidence='虚构',confirmed_lot_fields={lot.id:{'fields':{'spec':'猜测'}}})
    with pytest.raises(WarehouseInventoryError,match='备份'):
        access.confirm(db,**args,actor=None,source='script',preview_hash=value['preview_hash'],operation_key='no-backup',evidence='虚构')
