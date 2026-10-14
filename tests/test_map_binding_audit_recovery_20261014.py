"""Synthetic-only finite audit recovery: no real database or protected path."""
from copy import deepcopy
from hashlib import sha256
import json

import pytest
from sqlalchemy import select, inspect

from app.models import Base
from app.models.audit import OperationLog
from app.models.finance import FinanceIdempotencyRecord
from app.models.receipt_putaway import ReceiptStagingArea
from app.models.user import User
from app.models.warehouse_inventory import (
    WarehouseArea, WarehouseAreaStoragePolicy, WarehouseGroundLayoutPlan,
    WarehouseLocation, Floor3LocationLayout, InventoryLot,
)
from app.services import map_binding_audit_recovery_20261014 as recovery
from app.services.warehouse_area_activation import WarehouseAreaActivationError
from app.services.warehouse_ground_map_application import location_signature
from tests.test_map_binding_retirement_preview_v3 import two_retired_service_case


def all_facts(db):
    available=set(inspect(db.connection()).get_table_names())
    return {name:recovery._hash(recovery._rows(db,table)) for name,table in Base.metadata.tables.items() if name in available}


@pytest.fixture
def scene(tmp_path,monkeypatch):
    factory,old,new,rack_ids,runtime,draft=two_retired_service_case(tmp_path,monkeypatch)
    old_doc=json.loads(runtime.read_text());old_doc['floors']['1F']=old
    source_bytes=json.dumps(old_doc,ensure_ascii=False).encode()
    afterimage=deepcopy(next(r for r in old['racks'] if r['id']==rack_ids[1]));afterimage['version']+=1
    tomb=next(r for r in new['retired_racks'] if r['id']==rack_ids[1]);tomb.update(afterimage)
    events=[]
    for n,rack_id in enumerate(rack_ids):
        rack=next(r for r in new['retired_racks'] if r['id']==rack_id)
        event=dict(floor_code='1F',id=rack_id,rack_code=rack['rack_code'],area_code=rack['area_code'],
            deleted=True,inventory_changed=False,published_map_changed=True,inactive_location_count=0,
            operation_key=f'synthetic-delete-{n}',request_fingerprint=str(n)*64,published_revision='intermediate',draft_revision='draft')
        events.append(event)
    new['layout_edit_receipts']=deepcopy(old.get('layout_edit_receipts',[]))+[
        dict(operation_key=e['operation_key'],action='rack.delete.published',applied_at='2026-10-13T00:00:00Z',
             result={k:v for k,v in e.items() if k not in {'floor_code','operation_key','published_revision','draft_revision'}})
        for e in events]
    new['revision']=recovery._hash({k:v for k,v in new.items() if k!='revision'})[:16]
    events[-1]['published_revision']=new['revision']
    current_doc=deepcopy(old_doc);current_doc['floors']['1F']=new
    runtime.write_text(json.dumps(current_doc,ensure_ascii=False))
    binding='audited-not-available';binding_sha='b'*64
    with factory() as db:
        actor=db.scalar(select(User))
        areas=list(db.scalars(select(WarehouseArea).order_by(WarehouseArea.id)))
        staging=[a for a in areas if a.area_code in {'AREA-005','AREA-006','AREA-007','AREA-008'}]
        for area in staging:
            db.add(ReceiptStagingArea(area_id=area.id))
            loc=WarehouseLocation(location_code=f'STAGING-{area.id}',location_name='合成功能位',warehouse_type='finished',
                is_active=True,warehouse_floor=1,area_code=area.area_code,address_area_id=area.id,address_kind='functional',
                storage_type='ground',address_version=1,placement_status='placed',source_version='CURRENT_MAP')
            db.add(loc);db.flush()
            db.add(Floor3LocationLayout(location_id=loc.id,left_pct=5,top_pct=5,width_pct=10,height_pct=10,
                layout_kind='logical_anchor',source_type='manual',version=1))
        plans=list(db.scalars(select(WarehouseGroundLayoutPlan).order_by(WarehouseGroundLayoutPlan.id)))
        for plan in plans[1:]:
            plan.status='draft'
            for field in ('published_map_revision','publish_idempotency_key','publish_request_hash','published_by','published_at'):
                setattr(plan,field,None)
        plan=plans[0]
        for area in areas:area.storage_policy.published_map_revision=binding
        pins=tuple(sorted((a.storage_policy.id,a.id,a.storage_policy.version) for a in areas))
        source_sha=sha256(source_bytes).hexdigest()
        publication=dict(status='published',floor_code='1F',published_revision=binding,published_sha256=binding_sha,
            backup_sha256=source_sha,inventory_changed=False,mold_location_changed=False,
            formal_areas=[dict(area_code=events[1]['area_code'])],location_coordinate_adjustments=[],
            rack_cell_created_location_ids=[],rack_cell_enabled_location_ids=[],rack_cell_disabled_location_ids=[],
            rack_cell_updated_location_ids=[],rack_cell_bound_legacy_location_ids=[])
        audits={i:dict(id=i,action='SYNTHETIC',resource='synthetic',entity_type=None,entity_id=None,
            module_code=None,action_code=None,result=None,user_id=actor.id,schema_version=None,details={}) for i in range(41374,41429)}
        audits[41374]['details']=dict(published_revision=old['revision'],published_sha256=source_sha)
        audits[41376]['details']=dict(floor_code='1F',rack=afterimage,inventory_changed=False)
        for i,area in zip(range(41377,41399),[a for a in areas if a.area_code!=events[1]['area_code']]):
            p=area.storage_policy
            audits[i].update(entity_id=p.id,details=dict(area_id=area.id,map_feature_id=p.map_feature_id,
                map_revision=binding,policy_version=p.version,previous_map_revision=old['revision'],
                previous_policy_version=p.version-1,unchanged_area_and_assets=True))
        audits[41399]['details']=dict(plan_id=plan.id,plan_version=plan.version,area_id=plan.area_id,
            original_map_revision=plan.published_map_revision,map_revision=binding,
            policy_version=plan.area.storage_policy.version,map_feature_id=plan.area.storage_policy.map_feature_id,
            locations={str(s.location_id):location_signature(s.location,s.location.floor3_layout) for s in plan.slots})
        audits[41400]['details']=publication
        audits[41401]['details']=dict(result=dict(publication,rack_id=rack_ids[1],scope='rack_layout'))
        for i,e in zip((41406,41428),events):audits[i]['details']=e
        for row in audits.values():db.add(OperationLog(**dict(row,details=json.dumps(row['details']))))
        db.commit()
        staging_ids=tuple(a.id for a in staging)
        audit_hash=recovery._hash(list(audits.values()))
        plan_id=plan.id
        expected_staging_pins=staging_pins(db,staging_ids)
    for key,value in dict(SOURCE_SHA=source_sha,SOURCE_REVISION=old['revision'],BINDING_SHA=binding_sha,
        BINDING_REVISION=binding,CURRENT_SHA=sha256(runtime.read_bytes()).hexdigest(),CURRENT_REVISION=new['revision'],
        DRAFT_SHA=sha256(draft.read_bytes()).hexdigest(),HISTORICAL_AUDIT_SHA=audit_hash,POLICY_PINS=pins,
        STAGING_AREA_IDS=staging_ids,STAGING_LOCATION_PINS=expected_staging_pins,GROUND_PLAN_ID=plan_id,RETIRED_RACK_IDS=rack_ids).items():monkeypatch.setattr(recovery,key,value)
    yield dict(factory=factory,source=source_bytes,runtime=runtime,draft=draft,old=old,new=new,staging_ids=staging_ids)
    factory.kw['bind'].dispose()


def invoke(db,scene,plan=None,key='synthetic-recovery',**kwargs):
    actor=db.scalar(select(User))
    if plan is None:plan=recovery.preview(db,actor=actor,source_bytes=scene['source'])
    return recovery.apply(db,actor=actor,source_bytes=scene['source'],expected_preview=plan,operation_key=key,**kwargs)


def test_success_preserves_every_other_table_files_and_replays_without_increment(scene):
    with scene['factory']() as db:
        before=all_facts(db);files=scene['runtime'].read_bytes(),scene['draft'].read_bytes()
        plan=recovery.preview(db,actor=db.scalar(select(User)),source_bytes=scene['source'])
        assert all_facts(db)==before
        assert plan['geometry_source_revision']!=plan['audited_binding_revision']
        receipt=invoke(db,scene,plan)
        assert receipt['staging_verified_count']==4 and receipt['ground_map_application_count']==1
        after=all_facts(db)
        assert {k for k in before if before[k]!=after[k]}=={'warehouse_area_storage_policies','operation_logs','finance_idempotency_records'}
        assert all(p.version==2 and p.published_map_revision==recovery.CURRENT_REVISION for p in db.scalars(select(WarehouseAreaStoragePolicy)))
        assert scene['old']['revision']==recovery.SOURCE_REVISION
        assert files==(scene['runtime'].read_bytes(),scene['draft'].read_bytes())
        assert invoke(db,scene,plan)==receipt
        assert all_facts(db)==after
        with pytest.raises(WarehouseAreaActivationError):invoke(db,scene,plan,key='another-key')
        assert all_facts(db)==after


@pytest.mark.parametrize('change',['policy_version','policy_scope','location','stock','staging','audit','runtime','draft','actor'])
def test_stale_preview_is_rejected_without_repair_writes(scene,change):
    with scene['factory']() as db:
        plan=recovery.preview(db,actor=db.scalar(select(User)),source_bytes=scene['source'])
        if change=='policy_version':db.scalar(select(WarehouseAreaStoragePolicy)).version+=1
        elif change=='policy_scope':db.scalar(select(WarehouseAreaStoragePolicy)).allowed_inventory_types_json='["mold"]'
        elif change=='location':db.scalar(select(Floor3LocationLayout)).left_pct+=1
        elif change=='stock':db.scalar(select(InventoryLot)).quantity_available+=1
        elif change=='staging':db.delete(db.scalar(select(ReceiptStagingArea)))
        elif change=='audit':db.get(OperationLog,41376).details='{}'
        elif change in {'runtime','draft'}:scene[change].write_bytes(scene[change].read_bytes()+b' ')
        else:db.scalar(select(User)).auth_version+=1
        db.commit();before=all_facts(db)
        with pytest.raises(WarehouseAreaActivationError):invoke(db,scene,plan)
        assert all_facts(db)==before


@pytest.mark.parametrize('stage',['second_cas','ground','staging_post','audit','commit','file_during','stock_during'])
def test_failures_roll_back_every_table(scene,monkeypatch,stage):
    with scene['factory']() as db:
        before=all_facts(db);files=scene['runtime'].read_bytes(),scene['draft'].read_bytes()
        plan=recovery.preview(db,actor=db.scalar(select(User)),source_bytes=scene['source'])
        def fail(*a,**k):raise RuntimeError('injected failure')
        if stage=='second_cas':
            real=recovery._advance_policy_version;count=0
            def cas(*a,**k):
                nonlocal count
                count+=1
                if count==2:fail()
                return real(*a,**k)
            monkeypatch.setattr(recovery,'_advance_policy_version',cas)
        elif stage=='ground':monkeypatch.setattr(recovery,'record_map_applications',fail)
        elif stage=='staging_post':monkeypatch.setattr(recovery,'operational_location_issue',lambda *a,**k:'synthetic unavailable')
        elif stage=='audit':monkeypatch.setattr(recovery,'append_audit_event',fail)
        elif stage=='commit':monkeypatch.setattr(db,'commit',fail)
        else:
            real=recovery.record_map_applications
            def mutate(*a,**k):
                result=real(*a,**k)
                if stage=='file_during':scene['draft'].write_bytes(scene['draft'].read_bytes()+b' ')
                else:db.scalar(select(InventoryLot)).quantity_available+=1
                return result
            monkeypatch.setattr(recovery,'record_map_applications',mutate)
        with pytest.raises((RuntimeError,WarehouseAreaActivationError)):invoke(db,scene,plan)
        assert all_facts(db)==before
        if stage!='file_during':assert files==(scene['runtime'].read_bytes(),scene['draft'].read_bytes())


def test_parent_transaction_rolls_back_success_and_failure(scene,monkeypatch):
    with scene['factory']() as db:
        before=all_facts(db)
        invoke(db,scene,commit=False);db.rollback()
        assert all_facts(db)==before
        real=recovery.append_audit_event
        def fail(*a,**k):raise RuntimeError('after ground')
        monkeypatch.setattr(recovery,'append_audit_event',fail)
        with pytest.raises(RuntimeError):invoke(db,scene,commit=False)
        assert all_facts(db)==before
        monkeypatch.setattr(recovery,'append_audit_event',real)
        invoke(db,scene);assert all_facts(db)!=before


@pytest.mark.parametrize('role',['finance','workshop','inactive','selected'])
def test_actor_scope_is_checked_from_database(scene,role):
    with scene['factory']() as db:
        actor=db.scalar(select(User))
        if role=='inactive':actor.is_active=False
        elif role=='selected':actor.customer_access_mode='selected'
        else:actor.role=role
        db.commit();before=all_facts(db)
        with pytest.raises(WarehouseAreaActivationError):invoke(db,scene)
        assert all_facts(db)==before


def test_preview_and_idempotency_payload_tamper_rejected(scene):
    with scene['factory']() as db:
        plan=recovery.preview(db,actor=db.scalar(select(User)),source_bytes=scene['source'])
        bad=deepcopy(plan);bad['changes'][0]['after_version']+=1
        with pytest.raises(WarehouseAreaActivationError):invoke(db,scene,bad)
        invoke(db,scene,plan);before=all_facts(db)
        bad['preview_sha256']=recovery._hash({k:v for k,v in bad.items() if k!='preview_sha256'})
        with pytest.raises(WarehouseAreaActivationError):invoke(db,scene,bad)
        assert all_facts(db)==before
        db.scalar(select(FinanceIdempotencyRecord)).response_json='{}';db.commit();before=all_facts(db)
        with pytest.raises(WarehouseAreaActivationError):invoke(db,scene,plan)
        assert all_facts(db)==before


@pytest.mark.parametrize('change',['remaining_rack','tombstone','version_geometry','wrong_revision',
    'receipt_removed','receipt_extra','receipt_fingerprint','active_location','live_mold',
    'publish_source','publish_moved','afterimage_version'])
def test_independent_geometry_and_audit_proof_rejects_unproved_changes(scene,change):
    with scene['factory']() as db:
        old=deepcopy(scene['old']);new=deepcopy(scene['new']);audits=deepcopy(recovery._audits(db))
        locations=recovery._rows(db,WarehouseLocation.__table__);molds=[]
        rack_id=recovery.RETIRED_RACK_IDS[0]
        if change=='remaining_rack':new['racks'][0]['name']='unexpected'
        elif change=='tombstone':new['retired_racks'][0]['width_mm']+=1
        elif change=='version_geometry':audits[41376]['details']['rack']['width_mm']+=1
        elif change=='wrong_revision':audits[41428]['details']['published_revision']='unproved'
        elif change=='receipt_removed':new['layout_edit_receipts'].pop()
        elif change=='receipt_extra':new['layout_edit_receipts'].append(dict(action='other'))
        elif change=='receipt_fingerprint':new['layout_edit_receipts'][0]['result']['request_fingerprint']='bad'
        elif change=='active_location':locations.append(dict(is_active=True,map_rack_id=rack_id))
        elif change=='live_mold':
            # A retired/historical cell is still a physical identity; it cannot
            # be ignored because it is no longer present among active cells.
            cell=dict(id='synthetic-historical-cell')
            next(r for r in old['racks'] if r['id']==rack_id)['retired_mold_cells']=[cell]
            next(r for r in new['retired_racks'] if r['id']==rack_id)['retired_mold_cells']=[cell]
            molds=[dict(rack_location='MCELL-SYNTHETIC-HISTORICAL-CELL')]
        elif change=='publish_source':audits[41400]['details']['backup_sha256']='0'*64
        elif change=='publish_moved':audits[41400]['details']['location_coordinate_adjustments']=[dict(location_id=1)]
        else:audits[41376]['details']['rack']['version']+=1
        with pytest.raises(WarehouseAreaActivationError):
            recovery._geometry_and_audit_proof(old,new,audits,locations,molds)


def test_true_cas_conflict_on_last_policy_rolls_back_first_22(scene,monkeypatch):
    from sqlalchemy import update
    with scene['factory']() as db:
        before=all_facts(db);real=recovery._advance_policy_version
        last=max(x[0] for x in recovery.POLICY_PINS)
        def racing(session,*,policy,**kwargs):
            if policy.id==last:
                session.execute(update(WarehouseAreaStoragePolicy).where(WarehouseAreaStoragePolicy.id==last)
                    .values(version=WarehouseAreaStoragePolicy.version+1).execution_options(synchronize_session=False))
            return real(session,policy=policy,**kwargs)
        monkeypatch.setattr(recovery,'_advance_policy_version',racing)
        with pytest.raises(WarehouseAreaActivationError,match='其他操作更新'):invoke(db,scene)
        assert all_facts(db)==before


@pytest.mark.parametrize('change',['stock','historical_audit'])
def test_final_audit_cannot_hide_late_business_or_history_mutation(scene,monkeypatch,change):
    with scene['factory']() as db:
        before=all_facts(db);real=recovery.append_audit_event
        def polluted(session,**kwargs):
            result=real(session,**kwargs)
            if change=='stock':session.scalar(select(InventoryLot)).quantity_available+=1
            else:session.get(OperationLog,41376).details='{}'
            return result
        monkeypatch.setattr(recovery,'append_audit_event',polluted)
        with pytest.raises(WarehouseAreaActivationError):invoke(db,scene)
        assert all_facts(db)==before


def test_claim_rereads_complete_snapshot_before_cas(scene,monkeypatch):
    with scene['factory']() as db:
        before=all_facts(db);real=recovery.claim_warehouse_floor_projection
        def changed(session,**kwargs):
            result=real(session,**kwargs)
            session.scalar(select(Floor3LocationLayout)).left_pct+=1
            session.flush()
            return result
        monkeypatch.setattr(recovery,'claim_warehouse_floor_projection',changed)
        with pytest.raises(WarehouseAreaActivationError):invoke(db,scene)
        assert all_facts(db)==before


def test_replay_releases_transaction_and_does_not_accept_changed_source(scene):
    with scene['factory']() as db:
        actor=db.scalar(select(User));plan=recovery.preview(db,actor=actor,source_bytes=scene['source'])
        first=invoke(db,scene,plan);assert not db.in_transaction()
        assert invoke(db,scene,plan)==first
        assert not db.in_transaction()
        before=all_facts(db)
        with pytest.raises(WarehouseAreaActivationError):
            recovery.apply(db,actor=actor,source_bytes=scene['source']+b' ',expected_preview=plan,operation_key='synthetic-recovery')
        assert all_facts(db)==before


@pytest.mark.parametrize('raw',[b'{"a":1,"a":2}',b'{"value":NaN}',b'{"value":Infinity}'])
def test_strict_source_parser_rejects_ambiguous_or_nonfinite_json(raw):
    with pytest.raises(WarehouseAreaActivationError):recovery._read(raw,sha256(raw).hexdigest())


def staging_pins(db,area_ids):
    result={}
    for aid in area_ids:
        area=db.get(WarehouseArea,aid)
        locs=list(db.scalars(select(WarehouseLocation).where(WarehouseLocation.warehouse_floor==1,
            WarehouseLocation.area_code==area.area_code,WarehouseLocation.is_active.is_(True)).order_by(WarehouseLocation.id)))
        result[aid]=tuple((l.id,l.address_version,location_signature(l,l.floor3_layout)) for l in locs)
    return result


def test_exact_legacy_null_address_uses_formal_floor_area_without_inventing_fk(scene,monkeypatch):
    with scene['factory']() as db:
        for aid in scene['staging_ids']:
            area=db.get(WarehouseArea,aid)
            for loc in db.scalars(select(WarehouseLocation).where(WarehouseLocation.area_code==area.area_code)):
                loc.address_area_id=None;loc.address_version=2
        db.commit()
        monkeypatch.setattr(recovery,'STAGING_LOCATION_PINS',staging_pins(db,scene['staging_ids']),raising=False)
        before=all_facts(db)
        invoke(db,scene)
        assert all_facts(db)['warehouse_locations']==before['warehouse_locations']
        assert all(l.address_area_id is None for l in db.scalars(select(WarehouseLocation).where(WarehouseLocation.address_kind=='functional')))


def test_boss_cannot_bypass_existing_map_admin_only_contract(scene):
    with scene['factory']() as db:
        db.scalar(select(User)).role='boss';db.commit()
        before=all_facts(db)
        with pytest.raises(WarehouseAreaActivationError):invoke(db,scene)
        assert all_facts(db)==before


def test_preview_strictly_checks_current_ground_geometry_even_if_old_receipt_matches(scene,monkeypatch):
    from app.services.warehouse_floor1_candidate_planner import validate_capacity_layout_slots_for_zone
    with scene['factory']() as db:
        before=all_facts(db)
        def obstacle(floor,**kwargs):
            assert kwargs['allow_spatial_conflicts'] is False
            measured=deepcopy(floor)
            measured['features'].append(dict(id='new-obstacle',feature_kind='no_go',
                subtype='door_clearance',points=[[2100,2900],[3300,2900],[3300,3900],[2100,3900]]))
            return validate_capacity_layout_slots_for_zone(measured,**kwargs)
        monkeypatch.setattr(recovery,'validate_capacity_layout_slots_for_zone',obstacle,raising=False)
        with pytest.raises(WarehouseAreaActivationError,match='严格几何'):
            recovery.preview(db,actor=db.scalar(select(User)),source_bytes=scene['source'])
        assert all_facts(db)==before


@pytest.mark.parametrize('field,value', [('address_version',2),('warehouse_type','shared'),
    ('remarks','changed'),('location_code','CHANGED-LOCATION'),('source_version','LEGACY'),
    ('address_kind','ground_slot')])
def test_any_staging_position_field_drift_invalidates_reviewed_preview(scene,field,value):
    with scene['factory']() as db:
        actor=db.scalar(select(User));plan=recovery.preview(db,actor=actor,source_bytes=scene['source'])
        location=db.scalar(select(WarehouseLocation).where(WarehouseLocation.address_kind=='functional'))
        setattr(location,field,value);db.commit();before=all_facts(db)
        with pytest.raises(WarehouseAreaActivationError):invoke(db,scene,plan)
        assert all_facts(db)==before


def test_wrong_nonnull_staging_fk_is_rejected_even_with_an_independent_signature(scene,monkeypatch):
    with scene['factory']() as db:
        loc=db.scalar(select(WarehouseLocation).where(WarehouseLocation.address_kind=='functional'))
        loc.address_area_id=scene['staging_ids'][1];db.commit()
        monkeypatch.setattr(recovery,'STAGING_LOCATION_PINS',staging_pins(db,scene['staging_ids']))
        before=all_facts(db)
        with pytest.raises(WarehouseAreaActivationError,match='不是原正式功能位'):invoke(db,scene)
        assert all_facts(db)==before
