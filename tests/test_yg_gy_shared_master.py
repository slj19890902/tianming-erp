import json
from decimal import Decimal
import pytest
from sqlalchemy import select
from fastapi import HTTPException
from tests.test_shared_finished_stock import reservation_db, setup_pair, activate, reserve_for, add_lot
from app.services import shared_finished_stock as shared
from app.services import shared_product_master as policy
from app.services.master_data_versioning import apply_versioned_update, preview_versioned_update
from app.models.shared_finished_stock import SharedFinishedMember, SharedFinishedMutation
from app.models.master_data_object_version import MasterDataObjectVersion


def pair(db,data):
    data['customer'].customer_code='YG';data['other_customer'].customer_code='GY'
    target,item,lot=setup_pair(db,data);gid=activate(db,data,target,lot)['group_id']
    from app.services.customer_sharing import activate_master
    activate_master(db,gid,data['admin']);db.commit()
    return target,item,lot,gid


def edit(db,data,product,values):
    args=dict(object_type='product',entity=product,updates=values,expected_version=product.version,user=data['admin'])
    preview=preview_versioned_update(db,**args)
    return apply_versioned_update(db,**args,reason='隔离验证',source='test',confirmation_token=preview['confirmation_token'])


def repair(db,data,product):
    plan=policy.preview_reconcile(db,product.id)
    args=dict(product_id=product.id,preview_hash=plan['sha256'],operation_key='confirmed-same-code',
              evidence='老板确认同编码现货完全通用，资料差异为录入问题',actor=data['admin'])
    assert not policy.reconcile(db,**args)['replayed'];db.commit()
    assert policy.reconcile(db,**args)['replayed'];db.commit()


def test_explicit_reconcile_old_drift_and_frozen_reservation_keeps_original_identity(reservation_db):
    db,data=reservation_db;target,item,lot,gid=pair(db,data)
    original=db.get(SharedFinishedMember,target.id).identity_json
    physical=lot.finished_detail.physical_basis_json
    target.production_process='模切';target.version+=1;db.commit()
    assert shared.match(db,lot,product_id=target.id,customer_id=target.customer_id) is None
    repair(db,data,target)
    assert shared.match(db,lot,product_id=target.id,customer_id=target.customer_id)
    reservation=reserve_for(db,data,item,lot,20,'verified-use');db.commit()
    assert (lot.quantity_available,lot.quantity_reserved)==(30,20)
    assert shared.reserved_match(db,reservation,lot,product_id=target.id,customer_id=target.customer_id)
    assert db.get(SharedFinishedMember,target.id).identity_json==original
    assert lot.finished_detail.physical_basis_json==physical


def test_bidirectional_report_sync_has_cas_history_and_keeps_prices(reservation_db):
    db,data=reservation_db;target,item,lot,gid=pair(db,data);source=data['product']
    before=lot.finished_detail.physical_basis_json;price=target.sale_unit_price
    initial=(source.version,target.version)
    edit(db,data,source,{'report_length_mm':802});db.commit()
    assert source.report_length_mm==target.report_length_mm==802
    assert (source.version,target.version)==tuple(v+1 for v in initial)
    assert target.sale_unit_price==price and lot.quantity_available==50
    edit(db,data,source,{'supplier_edge_type':'净料'});db.commit()
    assert source.supplier_edge_type==target.supplier_edge_type=='净料'
    assert target.sale_unit_price==price and lot.quantity_available==50
    initial=(source.version-1,target.version-1)
    assert shared.match(db,lot,product_id=target.id,customer_id=target.customer_id)
    edit(db,data,target,{'report_length_mm':804});db.commit()
    assert source.report_length_mm==target.report_length_mm==804
    assert (source.version,target.version)==tuple(v+2 for v in initial)
    assert lot.finished_detail.physical_basis_json==before
    assert all(db.scalar(select(MasterDataObjectVersion.id).where(MasterDataObjectVersion.object_type=='product',
        MasterDataObjectVersion.object_id==p.id,MasterDataObjectVersion.version==p.version)) for p in (source,target))


def test_material_change_syncs_but_does_not_relabel_old_lot(reservation_db):
    from app.models.material import Material
    db,data=reservation_db;target,item,lot,gid=pair(db,data);source=data['product']
    frozen=reserve_for(db,data,item,lot,10,'old-reserve');db.commit()
    material=Material(code='B-NEW',supplier_name='测试供应商',quote_price=2,price_unit='元/㎡',
                      purchase_currency='CNY',purchase_tax_included=True,layer_count=5,flute_type='BE',is_active=True)
    db.add(material);db.commit()
    edit(db,data,source,{'material_id':material.id});db.commit()
    assert target.material_id==source.material_id==material.id
    assert shared.match(db,lot,product_id=target.id,customer_id=target.customer_id) is None
    assert shared.reserved_match(db,frozen,lot,product_id=target.id,customer_id=target.customer_id)


def test_failure_rolls_back_both_product_versions_and_policy(reservation_db,monkeypatch):
    db,data=reservation_db;target,item,lot,gid=pair(db,data);source=data['product'];before=source.report_length_mm
    initial=(source.version,target.version)
    original=policy.finish_sync
    def fail(*args):
        original(*args);raise RuntimeError('audit failed')
    monkeypatch.setattr(policy,'finish_sync',fail)
    with pytest.raises(RuntimeError): edit(db,data,source,{'report_length_mm':802})
    db.rollback()
    assert (source.version,target.version)==initial and source.report_length_mm==target.report_length_mm==before
    assert len(list(db.scalars(select(SharedFinishedMutation))))==1


def test_other_customer_and_bom_role_not_silently_combined(reservation_db):
    db,data=reservation_db;target,item,lot=setup_pair(db,data);activate(db,data,target,lot)
    edit(db,data,data['product'],{'report_length_mm':802});db.commit()
    assert target.report_length_mm==800
    from app.services.customer_sharing import activate_master
    # Activate against unchanged identities, then reject later role drift.
    data['product'].report_length_mm=800;db.commit()
    activate_master(db,db.get(SharedFinishedMember,target.id).group_id,data['admin']);db.commit()
    target.is_composite=True;db.commit()
    with pytest.raises(HTTPException):
        edit(db,data,data['product'],{'report_length_mm':804})
    db.rollback();assert data['product'].report_length_mm==800


def test_reconcile_adds_current_unshared_stock_without_changing_quantities_or_costs(reservation_db):
    db,data=reservation_db;target,item,old,gid=pair(db,data)
    fresh=add_lot(db,data,quantity=30,key='new-unshared');db.commit()
    assert shared.match(db,fresh,product_id=target.id,customer_id=target.customer_id) is None
    before=(fresh.quantity_available,fresh.quantity_reserved,fresh.estimated_unit_cost_snapshot,fresh.finished_detail.physical_basis_json)
    repair(db,data,target)
    assert shared.match(db,fresh,product_id=target.id,customer_id=target.customer_id)
    assert before==(fresh.quantity_available,fresh.quantity_reserved,fresh.estimated_unit_cost_snapshot,fresh.finished_detail.physical_basis_json)


def test_moved_old_batch_does_not_gain_new_material_eligibility(reservation_db):
    from app.models.material import Material
    db,data=reservation_db;target,item,lot,gid=pair(db,data);source=data['product']
    material=Material(code='NEW-MOVED',supplier_name='测试供应商',quote_price=2,price_unit='元/㎡',
        purchase_currency='CNY',purchase_tax_included=True,layer_count=5,flute_type='BE',is_active=True)
    db.add(material);db.commit()
    old_identity=lot.finished_detail.physical_basis_json
    edit(db,data,source,{'material_id':material.id});db.commit()
    moved=add_lot(db,data,quantity=1,key='moved-old-identity')
    moved.finished_detail.physical_basis_json=old_identity;db.flush()
    shared.inherit_lot(db,lot,moved);db.commit()
    assert shared.match(db,moved,product_id=target.id,customer_id=target.customer_id) is None
