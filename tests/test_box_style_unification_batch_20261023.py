import pytest
from fastapi import HTTPException
from sqlalchemy import select, func

from tests.test_master_data_versioning_p4_writers import writer_app
from tests.test_product_unknown_structure import seed
from tests.test_box_style_unification_20261023 import ALIASES, A1
from tests.test_shared_finished_stock import reservation_db
from tests.test_yg_gy_shared_master import pair
from app.models.product import Product
from app.models.user import User
from app.models.order import OrderItem
from app.models.master_data_object_version import MasterDataObjectVersion
from app.services import product_box_style_unification as unify


def test_batch_replaces_all_registered_names_and_keeps_structure_history_and_audit(writer_app):
    pid, _, item_id = seed(writer_app)
    with writer_app.state.session_factory() as db:
        original = db.get(Product, pid)
        for n, alias in enumerate(ALIASES + ['天地盖', '未知客户箱型']):
            db.add(Product(customer_id=original.customer_id, product_code=f'SYNTH-{n}', customer_material_code=f'SYNTH-{n}',
                product_name='合成箱型归一', box_category='normal', box_style=alias,
                splice_mode=None, pieces_per_box=None, flap_mm=None, production_process=None))
        item = db.get(OrderItem, item_id); item.snapshot_box_style = 'WC 五层钉箱'
        db.commit()
        actor = db.get(User, writer_app.state.admin_id)
        plan = unify.build_plan(db, actor)
        versions_before = {p.id: p.version for p in db.scalars(select(Product))}
        report = unify.apply_plan(db, actor, plan, approved_sha256=unify.digest(plan))
        db.commit()
        assert report['changed_count'] == len(plan['rows']) > 20
        assert not report['historical_snapshots_written']
        assert db.get(OrderItem, item_id).snapshot_box_style == 'WC 五层钉箱'
        for row in plan['rows']:
            p = db.get(Product, row['id'])
            assert p.box_style == row['after'] and p.version == versions_before[p.id] + 1
            assert (p.splice_mode, p.pieces_per_box, p.flap_mm, p.production_process) == (None, None, None, None)
            version = db.scalar(select(MasterDataObjectVersion).where(
                MasterDataObjectVersion.object_type=='product', MasterDataObjectVersion.object_id==p.id,
                MasterDataObjectVersion.version==p.version))
            assert version is not None and version.operation_log_id
        unknown = db.scalar(select(Product).where(Product.box_style=='未知客户箱型'))
        assert unknown.version == versions_before[unknown.id]
        before_count = db.scalar(select(func.count()).select_from(MasterDataObjectVersion))
        with pytest.raises(HTTPException) as rejected:
            unify.apply_plan(db, actor, plan, approved_sha256=unify.digest(plan))
        assert rejected.value.status_code == 409
        empty = unify.build_plan(db, actor)
        assert unify.apply_plan(db, actor, empty, approved_sha256=unify.digest(empty))['changed_count'] == 0
        assert db.scalar(select(func.count()).select_from(MasterDataObjectVersion)) == before_count


@pytest.mark.parametrize('problem', ['missing_hash', 'changed_hash', 'stale_version', 'no_permission', 'restricted_scope'])
def test_rejected_plan_has_no_mutation(writer_app, problem):
    pid, _, _ = seed(writer_app)
    with writer_app.state.session_factory() as db:
        actor = db.get(User, writer_app.state.admin_id)
        plan = unify.build_plan(db, actor); sha = unify.digest(plan)
        if problem == 'missing_hash': sha = None
        if problem == 'changed_hash': sha = '0'*64
        if problem == 'stale_version': db.get(Product,pid).version += 1
        if problem == 'no_permission': actor.role = 'workshop'
        if problem == 'restricted_scope': actor.role = 'sales'; actor.customer_access_mode = 'selected'
        db.commit()
        before = db.scalar(select(func.count()).select_from(MasterDataObjectVersion))
        with pytest.raises(HTTPException): unify.apply_plan(db, actor, plan, approved_sha256=sha)
        assert db.get(Product,pid).box_style == 'WC 五层钉箱'
        assert db.scalar(select(func.count()).select_from(MasterDataObjectVersion)) == before


def test_late_failure_rolls_back_names_versions_and_audit(writer_app, monkeypatch):
    pid, _, _ = seed(writer_app)
    with writer_app.state.session_factory() as db:
        first = db.get(Product,pid)
        second = Product(customer_id=first.customer_id, product_code='SYNTH-SECOND', customer_material_code='SYNTH-SECOND',
            product_name='合成第二条', box_category='normal', box_style='天地盖')
        db.add(second); db.commit()
        actor = db.get(User,writer_app.state.admin_id); plan=unify.build_plan(db,actor)
        before = db.scalar(select(func.count()).select_from(MasterDataObjectVersion))
        original = unify.apply_versioned_update
        def fail(*args, **kwargs):
            original(*args, **kwargs)
            if kwargs['entity'].id == second.id: raise RuntimeError('synthetic audit failure')
        monkeypatch.setattr(unify,'apply_versioned_update',fail)
        with pytest.raises(RuntimeError): unify.apply_plan(db,actor,plan,approved_sha256=unify.digest(plan))
        db.rollback()
        assert first.box_style == 'WC 五层钉箱' and first.version == 2
        assert second.box_style == '天地盖' and second.version == 1
        assert db.scalar(select(func.count()).select_from(MasterDataObjectVersion)) == before


@pytest.mark.parametrize('conflict', [False, True])
def test_shared_product_writer_preserves_frozen_lot_and_rejects_different_type(reservation_db, conflict):
    db,data=reservation_db; target, item, lot, gid=pair(db,data); source=data['product']
    source.box_style='WC 五层钉箱'; target.box_style='天地盖' if conflict else 'SCX 单瓦箱'
    db.commit(); frozen=lot.finished_detail.physical_basis_json
    actor=data['admin']; plan=unify.build_plan(db,actor)
    versions=(source.version,target.version)
    if conflict:
        with pytest.raises(HTTPException): unify.apply_plan(db,actor,plan,approved_sha256=unify.digest(plan))
        db.rollback()
        assert source.box_style=='WC 五层钉箱' and target.box_style=='天地盖'
        assert (source.version,target.version)==versions
    else:
        unify.apply_plan(db,actor,plan,approved_sha256=unify.digest(plan)); db.commit()
        assert source.box_style==target.box_style==A1
        assert (source.version,target.version)==tuple(v+1 for v in versions)
    assert lot.finished_detail.physical_basis_json==frozen
    assert lot.quantity_available==50


@pytest.mark.parametrize('after_actual_plan', [False, True])
def test_concurrent_peer_reclassification_never_becomes_an_alias_rename(reservation_db, monkeypatch, after_actual_plan):
    from sqlalchemy.orm import Session
    from app.services import shared_product_master
    from app.services.master_data_versioning import apply_versioned_update
    db,data=reservation_db;target,item,lot,gid=pair(db,data);source=data['product']
    source.box_style='WC 五层钉箱';target.box_style='SCX 单瓦箱';db.commit()
    source_id,target_id,initial_version=source.id,target.id,source.version
    frozen=lot.finished_detail.physical_basis_json
    original=shared_product_master.prepare_sync
    def concurrent_change():
        with Session(db.get_bind()) as other:
            peer=other.get(Product,target_id);peer.box_style='天地盖';peer.version+=1;other.commit()
    # First normal-save preview sees a same-type peer.
    original(db,source,{'box_style':A1},data['admin'])
    if after_actual_plan:
        def racing_plan(*args):
            plan=original(*args);concurrent_change();return plan
        monkeypatch.setattr(shared_product_master,'prepare_sync',racing_plan)
    else:
        concurrent_change();db.expire_all()
    with pytest.raises(HTTPException) as rejected:
        apply_versioned_update(db,object_type='product',entity=source,updates={'box_style':A1},
            expected_version=initial_version,user=data['admin'],reason='合成名称归一',
            source='synthetic-alias-race',action='system_mapping')
    assert rejected.value.status_code==409
    db.rollback()
    assert db.get(Product,source_id).box_style=='WC 五层钉箱'
    assert db.get(Product,source_id).version==initial_version
    assert db.get(Product,target_id).box_style=='天地盖'
    assert lot.finished_detail.physical_basis_json==frozen
