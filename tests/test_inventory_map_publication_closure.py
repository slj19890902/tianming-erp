"""Real mutation API regression: all published area bindings follow the floor."""
import hashlib
import json
from pathlib import Path
from copy import deepcopy
from datetime import datetime

import pytest
from sqlalchemy import select, func

from app.api import warehouse as api
from app.models.user import User
from app.models.warehouse_inventory import (WarehouseArea, WarehouseAreaStoragePolicy, WarehouseLocation,
    WarehouseFloor, WarehouseGroundLayoutPlan, WarehouseGroundLayoutSlot, Floor3LocationLayout, InventoryLot)
from app.models.audit import OperationLog
from app.services import warehouse_twin_layout_editor as editor
from tests.test_f7f8_published_rack_delete import _setup, _delete, _request


def published_case(tmp_path, monkeypatch):
    factory, _, _, rack_id, runtime, draft = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(editor, 'TWIN_LAYOUT_BACKUP_DIR', tmp_path / 'backups')
    doc = json.loads(runtime.read_text())
    floor = doc['floors']['3F']
    floor['features'][0].update(erp_area_code='A', storage_layout='rack',
        formal_area_name='测试区', allowed_inventory_types=['finished'])
    floor['features'].append(dict(id='no-go-test', feature_kind='no_go',
        subtype='door_clearance', feature_code='NO-GO-TEST',
        points=[[9000,9000],[9100,9000],[9100,9100],[9000,9100]], version=1))
    floor['revision'] = editor._floor_revision(floor)
    runtime.write_text(json.dumps(doc))
    Path(editor.TWIN_LAYOUT_BASELINE_PATH).write_bytes(runtime.read_bytes())
    draft_doc = json.loads(draft.read_text())
    draft_doc['floors']['3F']['features'] = floor['features']
    draft_doc['floors']['3F']['revision'] = editor._floor_revision(draft_doc['floors']['3F'])
    draft_doc['draft_meta'].update(base_floor_revisions={'3F':floor['revision']},
        base_published_sha256=hashlib.sha256(runtime.read_bytes()).hexdigest())
    draft.write_text(json.dumps(draft_doc))
    with factory() as db:
        area = db.scalar(select(WarehouseArea))
        area.storage_policy = WarehouseAreaStoragePolicy(map_feature_id='zone-a', storage_layout='rack',
            status='published', published_map_revision=floor['revision'],
            allowed_inventory_types_json='["finished"]', version=1)
        db.commit()
    return factory, floor['revision'], draft_doc['floors']['3F']['revision'], rack_id, runtime, draft


def mutate(case, db, kind):
    factory, old, draft_revision, rack_id, runtime, draft = case
    actor = db.scalar(select(User))
    if kind == 'rack':
        return _delete(db, actor, old, draft_revision, rack_id)
    return api.remove_twin_no_go_features('3F', api.TwinNoGoRemovalPayload(
        expected_revision=draft_revision, expected_published_revision=old,
        feature_ids=['no-go-test'], operation_key='remove-no-go-test'), _request(), db, actor)


@pytest.mark.parametrize('kind', ['rack', 'no_go'])
def test_mutation_keeps_published_policy_current_and_replay_stable(tmp_path, monkeypatch, kind):
    case = published_case(tmp_path, monkeypatch)
    factory, old, _, _, runtime, _ = case
    try:
        with factory() as db:
            result = mutate(case, db, kind)
            assert result['applied']
            area = db.scalar(select(WarehouseArea))
            current = json.loads(runtime.read_text())['floors']['3F']['revision']
            assert current != old
            assert area.storage_policy.published_map_revision == current
            version = area.storage_policy.version
            replay = mutate(case, db, kind)
            assert replay['applied'] is False
            assert area.storage_policy.version == version
    finally:
        factory.kw['bind'].dispose()


def first_floor_case(tmp_path, monkeypatch):
    """23 synthetic published areas, including three immutable FIN ground plans.

    F1 is the database's legacy spelling; the actual map uses 1F. These are
    created in this test, never copied from the formal database or map.
    """
    case = published_case(tmp_path, monkeypatch)
    factory, old, _, rack_id, runtime, draft = case
    doc = json.loads(runtime.read_text())
    floor = doc['floors'].pop('3F')
    floor['floor_code'] = '1F'
    doc['floors']['1F'] = floor
    with factory() as db:
        physical_floor = db.scalar(select(WarehouseFloor))
        physical_floor.floor_code = 'F1'; physical_floor.floor_number = 1
        actor = db.scalar(select(User))
        for location in db.scalars(select(WarehouseLocation)):
            location.warehouse_floor = 1
        for number in range(1,23):
            code = f'FIN-{number:03}' if number <= 3 else f'AREA-{number:03}'
            layout = 'pallet_ground' if number <= 3 else 'functional'
            x, y = ((2000,2000), (5000,2000), (2000,5000))[number-1] if number <= 3 else (8000,1000)
            area = WarehouseArea(floor_id=physical_floor.id, area_code=code, area_name=f'合成区域 {code}',
                construction_status='enabled', planned_location_count=1 if number <= 3 else 0)
            db.add(area); db.flush()
            area.storage_policy = WarehouseAreaStoragePolicy(map_feature_id=f'zone-{code}', storage_layout=layout,
                status='published', published_map_revision=old, allowed_inventory_types_json='["finished"]', version=1)
            floor['features'].append(dict(id=f'zone-{code}', feature_kind='zone', feature_code=f'ZONE-{code}',
                erp_area_code=code, formal_area_name=area.area_name, storage_layout=layout,
                allowed_inventory_types=['finished'], points=[[x,y],[x+2000,y],[x+2000,y+2000],[x,y+2000]], version=1))
            if number > 3: continue
            location = WarehouseLocation(location_code=f'F1-{code}-01', location_name=f'{code} 原排位 01',
                warehouse_type='finished', is_active=True, warehouse_floor=1, area_code=code,
                address_area_id=area.id, address_kind='ground_slot', storage_type='ground',
                ground_row_no=1, slot_no=1, address_version=1, placement_status='placed', source_version='CURRENT_MAP')
            db.add(location); db.flush()
            db.add(Floor3LocationLayout(location_id=location.id, left_pct=5, top_pct=5, width_pct=60, height_pct=50,
                layout_kind='physical_pallet', source_type='manual', version=1))
            plan = WarehouseGroundLayoutPlan(area_id=area.id, status='published', target_slot_count=1,
                numbering_origin='south', row_direction='from_aisle_inward', slot_direction='left_to_right',
                row_start_no=1, slot_start_no=1, draft_map_revision=old, published_map_revision=old,
                preview_fingerprint='a'*64, publish_idempotency_key=f'original-{code}', publish_request_hash='b'*64,
                updated_by=actor.id, published_by=actor.id, published_at=datetime(2026,10,1))
            db.add(plan); db.flush()
            db.add(WarehouseGroundLayoutSlot(plan_id=plan.id, location_id=location.id, route_sequence=1,
                row_no=1, slot_no=1, x_mm=x+100, y_mm=y+900, width_mm=1200, depth_mm=1000))
            db.add(InventoryLot(lot_number=f'original-{code}', inventory_type='finished', warehouse_location_id=location.id,
                quantity_available=10+number, quantity_reserved=0, quantity_consumed=0, quantity_damaged=0,
                quantity_scrapped=0, unit='boxes', status='active', source_type='manual', stock_date=datetime(2026,10,1).date(),
                stock_date_accuracy='exact', last_movement_at=datetime(2026,10,1), version=1))
        floor['revision'] = editor._floor_revision(floor)
        for policy in db.scalars(select(WarehouseAreaStoragePolicy)):
            policy.published_map_revision = floor['revision']
        for plan in db.scalars(select(WarehouseGroundLayoutPlan)):
            plan.draft_map_revision = plan.published_map_revision = floor['revision']
        db.commit()
    runtime.write_text(json.dumps(doc))
    Path(editor.TWIN_LAYOUT_BASELINE_PATH).write_bytes(runtime.read_bytes())
    draft_doc = json.loads(draft.read_text())
    previous_draft = draft_doc['floors'].pop('3F')
    previous_draft.update(floor_code='1F', features=deepcopy(floor['features']))
    # This unrelated edit stays pending while only the empty rack/no-go is applied.
    next(f for f in previous_draft['features'] if f['id'] == 'zone-FIN-001')['points'][0][0] += 10
    next(r for r in previous_draft['racks'] if r['id'] == 'rack-keep')['name'] = '未发布的另架改名'
    previous_draft['revision'] = editor._floor_revision(previous_draft)
    draft_doc['floors']['1F'] = previous_draft
    draft_doc['draft_meta'].update(base_floor_revisions={'1F':floor['revision']},
        base_published_sha256=hashlib.sha256(runtime.read_bytes()).hexdigest())
    draft.write_text(json.dumps(draft_doc))
    from app.services import warehouse_twin_layout as reader
    monkeypatch.setattr(reader, 'TWIN_LAYOUT_RUNTIME_PATH', runtime)
    monkeypatch.setattr(reader, 'TWIN_LAYOUT_PATH', editor.TWIN_LAYOUT_BASELINE_PATH)
    return factory, floor['revision'], previous_draft['revision'], rack_id, runtime, draft


def first_floor_mutate(case, db, kind):
    _, old, draft_revision, rack_id, _, _ = case
    actor = db.scalar(select(User))
    if kind == 'rack':
        return api.delete_twin_layout_rack('1F', rack_id, expected_revision=draft_revision,
            expected_published_revision=old, expected_version=3, operation_key='first-floor-delete', request=_request(), db=db, user=actor)
    return api.remove_twin_no_go_features('1F', api.TwinNoGoRemovalPayload(
        expected_revision=draft_revision, expected_published_revision=old,
        feature_ids=['no-go-test'], operation_key='first-floor-no-go'), _request(), db, actor)


def immutable_facts(db):
    result = {}
    for model in (WarehouseGroundLayoutPlan, WarehouseGroundLayoutSlot, Floor3LocationLayout, InventoryLot):
        result[model.__name__] = [{c.name:getattr(row,c.name) for c in model.__table__.columns}
            for row in db.scalars(select(model).order_by(model.id))]
    result['fin_locations'] = [{c.name:getattr(row,c.name) for c in WarehouseLocation.__table__.columns}
        for row in db.scalars(select(WarehouseLocation).where(WarehouseLocation.area_code.like('FIN-%')).order_by(WarehouseLocation.id))]
    return result


@pytest.mark.parametrize('kind', ['rack', 'no_go'])
def test_first_floor_23_areas_and_fin_original_ids_remain_usable_without_consuming_other_drafts(tmp_path, monkeypatch, kind):
    from app.services import warehouse_map_publication as publication
    from app.services.warehouse_ground_slots import published_ground_plan
    case = first_floor_case(tmp_path, monkeypatch)
    factory, old, _, _, runtime, draft = case
    pending = json.loads(draft.read_text())['floors']['1F']
    try:
        with factory() as db:
            before = immutable_facts(db)
            assert len(list(db.scalars(select(WarehouseArea)))) == 23
            result = first_floor_mutate(case, db, kind)
            assert result['applied']
            current = json.loads(runtime.read_text())['floors']['1F']
            publication.assert_current_floor_bindings(db, current)
            assert all(p.published_map_revision == current['revision'] and p.version == 2
                for p in db.scalars(select(WarehouseAreaStoragePolicy)))
            assert immutable_facts(db) == before
            for code in ('FIN-001','FIN-002','FIN-003'):
                plan = published_ground_plan(db, floor_code='F1', area_code=code, required_inventory_type='finished')
                assert plan.published_map_revision == old
                assert plan.slots[0].location_id == next(row['id'] for row in before['fin_locations'] if row['area_code']==code)
            kept = json.loads(draft.read_text())['floors']['1F']
            assert next(f for f in kept['features'] if f['id']=='zone-FIN-001') == next(f for f in pending['features'] if f['id']=='zone-FIN-001')
            assert next(r for r in kept['racks'] if r['id']=='rack-keep') == next(r for r in pending['racks'] if r['id']=='rack-keep')
            assert next(r for r in current['racks'] if r['id']=='rack-keep')['name'] == 'F9'
            audits = db.scalar(select(func.count()).select_from(OperationLog))
            assert not first_floor_mutate(case, db, kind)['applied']
            assert db.scalar(select(func.count()).select_from(OperationLog)) == audits
    finally:
        factory.kw['bind'].dispose()


@pytest.mark.parametrize('failure', ['stale_binding', 'audit', 'commit'])
def test_first_floor_failure_keeps_fin_ledger_policies_archive_and_maps_atomic(tmp_path, monkeypatch, failure):
    from fastapi import HTTPException
    from app.services import warehouse_map_publication as publication
    case = first_floor_case(tmp_path, monkeypatch)
    factory, old, _, _, runtime, draft = case
    files_before = runtime.read_bytes(), draft.read_bytes()
    def fail(*args, **kwargs): raise RuntimeError('injected floor closure failure')
    try:
        with factory() as db:
            if failure == 'stale_binding':
                db.scalar(select(WarehouseAreaStoragePolicy).order_by(WarehouseAreaStoragePolicy.id.desc())).published_map_revision = 'unproved-old-map'
                db.commit()
            before = immutable_facts(db)
            policy_before = [(p.id,p.version,p.published_map_revision) for p in db.scalars(select(WarehouseAreaStoragePolicy))]
            audits = db.scalar(select(func.count()).select_from(OperationLog))
            if failure == 'audit': monkeypatch.setattr(publication, 'append_audit_event', fail)
            elif failure == 'commit': monkeypatch.setattr(db, 'commit', fail)
            with pytest.raises((RuntimeError, HTTPException)):
                first_floor_mutate(case, db, 'rack')
            assert (runtime.read_bytes(), draft.read_bytes()) == files_before
            assert immutable_facts(db) == before
            assert [(p.id,p.version,p.published_map_revision) for p in db.scalars(select(WarehouseAreaStoragePolicy))] == policy_before
            assert all(loc.is_active for loc in db.scalars(select(WarehouseLocation)))
            assert db.scalar(select(func.count()).select_from(OperationLog)) == audits
    finally:
        factory.kw['bind'].dispose()


@pytest.mark.parametrize('change', ['unarchived_reference', 'altered_tombstone', 'duplicate_tombstone',
    'changed_remaining_rack', 'changed_zone', 'missing_tombstone', 'active_target'])
def test_finalization_never_proves_a_different_rack_identity_or_changed_area(tmp_path, monkeypatch, change):
    from app.services.warehouse_map_publication import complete_floor_publication
    from app.services.warehouse_area_activation import WarehouseAreaActivationError
    case = published_case(tmp_path, monkeypatch)
    factory, _, _, rack_id, runtime, _ = case
    source = json.loads(runtime.read_text())['floors']['3F']
    target = deepcopy(source)
    removed = next(r for r in target['racks'] if r['id']==rack_id)
    target['racks'].remove(removed)
    target['retired_racks'] = [{**removed, 'retired_at':'2026-10-14T00:00:00Z'}]
    if change == 'altered_tombstone': target['retired_racks'][0]['width_mm'] += 1
    elif change == 'duplicate_tombstone': target['retired_racks'].append(deepcopy(target['retired_racks'][0]))
    elif change == 'changed_remaining_rack': target['racks'][0]['name'] = 'unproved rename'
    elif change == 'changed_zone': target['features'][0]['points'][0][0] += 1
    elif change == 'missing_tombstone': target['retired_racks'] = []
    elif change == 'active_target': target['racks'].append(removed)
    target['revision'] = editor._floor_revision(target)
    try:
        with factory() as db:
            if change != 'unarchived_reference':
                for loc in db.scalars(select(WarehouseLocation)): loc.is_active = False
                db.flush()
            with pytest.raises(WarehouseAreaActivationError):
                complete_floor_publication(db, previous_floor_layout=source, floor_layout=target,
                    actor=db.scalar(select(User)), operation_key='unproved-retirement', retired_rack_id=rack_id)
            db.rollback()
            assert db.scalar(select(WarehouseAreaStoragePolicy)).published_map_revision == source['revision']
            assert all(loc.is_active for loc in db.scalars(select(WarehouseLocation)))
            assert db.scalar(select(func.count()).select_from(OperationLog)) == 0
    finally:
        factory.kw['bind'].dispose()


@pytest.mark.parametrize('kind', ['rack', 'no_go'])
@pytest.mark.parametrize('failure', ['binding', 'commit'])
def test_mutation_failure_restores_policy_locations_and_both_files(tmp_path, monkeypatch, kind, failure):
    from app.services import warehouse_map_publication as publication
    case = published_case(tmp_path, monkeypatch)
    factory, old, _, _, runtime, draft = case
    before = runtime.read_bytes(), draft.read_bytes()
    def fail(*args, **kwargs):
        raise RuntimeError('injected publication failure')
    try:
        with factory() as db:
            if failure == 'binding':
                monkeypatch.setattr(publication, 'assert_current_floor_bindings', fail)
            else:
                monkeypatch.setattr(db, 'commit', fail)
            with pytest.raises(RuntimeError, match='injected publication failure'):
                mutate(case, db, kind)
            assert (runtime.read_bytes(), draft.read_bytes()) == before
            assert db.scalar(select(WarehouseArea)).storage_policy.published_map_revision == old
            assert all(loc.is_active for loc in db.scalars(select(WarehouseLocation)))
    finally:
        factory.kw['bind'].dispose()
