"""Synthetic proof only: no exported business evidence or retained DB is loaded."""
from copy import deepcopy
from hashlib import sha256
import json

import pytest

from scripts.audit.map_binding_retirement_preview import preview


def raw(doc):
    return (json.dumps(doc, ensure_ascii=False, indent=2) + '\n').encode()


def revision(floor):
    floor['revision'] = sha256(json.dumps({k: v for k, v in floor.items() if k != 'revision'},
        ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()[:16]


def make_proof():
    floor = dict(floor_code='1F', layout_id='synthetic-layout', source_name='synthetic.dxf',
        bounds_mm=dict(min_x=0, min_y=0, max_x=10000, max_y=10000),
        features=[], racks=[], pallets=[], retired_racks=[], layout_edit_receipts=[], layout_edited_at='old')
    snapshot = dict(areas=[], policies=[], locations=[], mold_tools=[])
    for n in range(1, 24):
        code = f'SYN-{n:03}'
        feature = dict(id=f'zone-{n}', feature_kind='zone', erp_area_code=code,
            formal_area_name=f'synthetic {n}', formal_area_id=n, formal_floor_id=1,
            storage_layout='rack', allowed_inventory_types=['mold'], points=[[0,0],[100,0],[100,100],[0,100]])
        floor['features'].append(feature)
        snapshot['areas'].append(dict(id=n, area_code=code, area_name=feature['formal_area_name'],
                                     floor_id=1, construction_status='enabled'))
        snapshot['policies'].append(dict(id=n, area_id=n, map_feature_id=feature['id'],
            storage_layout='rack', allowed_inventory_types_json='["mold"]', status='published',
            draft_map_revision=None, published_map_revision=None, version=3))
    for n in (1, 2):
        floor['racks'].append(dict(id=f'rack-{n}', rack_code=f'R{n}', name=f'rack {n}', area_code=f'SYN-{n:03}',
            area_feature_id=f'zone-{n}', x_mm=n*100, y_mm=0, version=1,
            mold_cells=[dict(id=f'cell-{n}', level=1, grid=1, alias=f'C{n}')]))
    revision(floor)
    for p in snapshot['policies']:
        p['published_map_revision'] = floor['revision']
    source = dict(schema_version=1, generated_at='old', floors={'1F':floor,'3F':{'revision':'untouched'}})
    current = deepcopy(source)
    target = current['floors']['1F']
    events = []
    for rack in target.pop('racks'):
        target['retired_racks'].append(dict(rack, retired_at='2026-10-13T00:00:00Z', retired_reason='synthetic empty retirement'))
        event = dict(rack_id=rack['id'], operation_key=f'delete-{rack["id"]}', request_fingerprint='a'*64)
        events.append(event)
        target['layout_edit_receipts'].append(dict(operation_key=event['operation_key'], action='rack.delete.published',
            result=dict(id=rack['id'], rack_code=rack['rack_code'], area_code=rack['area_code'],
                deleted=True, inventory_changed=False, published_map_changed=True, inactive_location_count=0,
                request_fingerprint=event['request_fingerprint']), applied_at='2026-10-13T00:00:01Z'))
    target['racks'] = []
    target['layout_edited_at'] = current['generated_at'] = 'new'
    revision(target)
    return source, current, snapshot, events


def run_proof(source, current, snapshot, events, **overrides):
    pins = dict(source_sha256=sha256(raw(source)).hexdigest(), current_sha256=sha256(raw(current)).hexdigest(),
        source_revision=source['floors']['1F']['revision'], current_revision=current['floors']['1F']['revision'],
        floor_code='1F', snapshot_sha256=sha256(json.dumps(snapshot, ensure_ascii=False, sort_keys=True,
            separators=(',', ':')).encode()).hexdigest())
    args = dict(source_bytes=raw(source), current_bytes=raw(current), pins=pins, snapshot=snapshot, retirement_events=events)
    args.update(overrides)
    return preview(**args)


def test_exact_proof_only_proposes_owned_copy_rehearsal_and_does_not_mutate():
    case = make_proof(); before = deepcopy(case)
    answer = run_proof(*case)
    assert answer['ready_for_owned_copy_rehearsal']
    assert not answer['eligible_for_formal_write']
    assert len(answer['proposed_policy_changes']) == 23
    assert sum(c['strategy'] == 'explicit_retirement' for c in answer['proposed_policy_changes']) == 2
    assert all(c['before_version']==3 and c['proposed_version']==4 for c in answer['proposed_policy_changes'])
    assert case == before and answer['pending_checks']


@pytest.mark.parametrize('change', ['source_missing', 'raw_source_not_pinned', 'raw_current_not_pinned',
    'snapshot_changed', 'unknown_feature', 'changed_boundary', 'changed_source_identity', 'other_floor',
    'extra_rack', 'missing_tombstone', 'different_tombstone_cell', 'active_location', 'current_mold',
    'duplicate_tombstone', 'receipt_changed', 'receipt_removed', 'receipt_history_truncated',
    'policy_draft', 'policy_source', 'wrong_area_name', 'unbound_retirement', 'duplicate_feature',
    'forged_floor_revision'])
def test_proof_rejects_unproved_source_geometry_identity_occupancy_and_receipts(change):
    source, target, snapshot, events = make_proof()
    floor = target['floors']['1F']
    overrides = {}
    if change == 'source_missing': overrides['source_bytes'] = None
    elif change == 'raw_source_not_pinned': overrides['source_bytes'] = b'{}'
    elif change == 'raw_current_not_pinned': overrides['current_bytes'] = b'{}'
    elif change == 'snapshot_changed':
        answer = run_proof(source, target, snapshot, events, pins=dict(source_sha256='0'*64,
            current_sha256='0'*64, source_revision='', current_revision='', floor_code='1F', snapshot_sha256='0'*64))
        assert answer['blockers'] == ['snapshot_sha_mismatch']; return
    elif change == 'unknown_feature': floor['features'][0]['id'] = 'different-id'
    elif change == 'changed_boundary': floor['features'][0]['points'][0][0] += 1
    elif change == 'changed_source_identity': floor['source_name'] = 'different.dxf'
    elif change == 'other_floor': target['floors']['3F']['revision'] = 'different'
    elif change == 'extra_rack': floor['racks'].append(dict(id='new-rack'))
    elif change == 'missing_tombstone': floor['retired_racks'].pop()
    elif change == 'different_tombstone_cell': floor['retired_racks'][0]['mold_cells'][0]['id'] = 'other-cell'
    elif change == 'active_location': snapshot['locations'].append(dict(id=1,is_active=True,map_rack_id='rack-1'))
    elif change == 'current_mold': snapshot['mold_tools'].append(dict(id=1,rack_location='MCELL-CELL-1'))
    elif change == 'duplicate_tombstone': floor['retired_racks'].append(deepcopy(floor['retired_racks'][0]))
    elif change == 'receipt_changed': floor['layout_edit_receipts'][0]['result']['inventory_changed'] = True
    elif change == 'receipt_removed': floor['layout_edit_receipts'].pop()
    elif change == 'receipt_history_truncated': floor['layout_edit_receipts'].insert(0,dict(action='unproved'))
    elif change == 'policy_draft': snapshot['policies'][0]['draft_map_revision'] = 'pending'
    elif change == 'policy_source': snapshot['policies'][0]['published_map_revision'] = 'different'
    elif change == 'wrong_area_name': snapshot['areas'][0]['area_name'] = 'other'
    elif change == 'unbound_retirement': snapshot['policies'][1]['map_feature_id'] = 'zone-3'
    elif change == 'duplicate_feature': floor['features'].append(deepcopy(floor['features'][0]))
    elif change == 'forged_floor_revision': floor['revision'] = '0'*16
    if change != 'forged_floor_revision': revision(floor)
    answer = run_proof(source, target, snapshot, events, **overrides)
    assert answer['blockers'] and not answer['ready_for_owned_copy_rehearsal']
    assert answer['proposed_policy_changes'] == []


@pytest.mark.parametrize('invalid', [b'{"a":1,"a":2}', b'{"value":NaN}', b'{"value":Infinity}'])
def test_ambiguous_and_nonfinite_json_is_rejected(invalid):
    source, target, snapshot, events = make_proof()
    # Pin the invalid raw bytes to ensure parser strictness, not hash mismatch.
    from scripts.audit.map_binding_retirement_preview import _read
    with pytest.raises(ValueError): _read(invalid, sha256(invalid).hexdigest())


def two_retired_service_case(tmp_path, monkeypatch):
    from sqlalchemy import select
    from app.models.warehouse_inventory import WarehouseAreaStoragePolicy, WarehouseLocation
    from app.services import warehouse_twin_layout_editor as editor
    from tests.test_inventory_map_publication_closure import first_floor_case
    case = first_floor_case(tmp_path, monkeypatch)
    factory, _, _, rack_id, runtime, draft = case
    doc = json.loads(runtime.read_text()); source = doc['floors']['1F']
    second = deepcopy(next(r for r in source['racks'] if r['id'] == rack_id))
    second.update(id='second-retired-rack', rack_code='SECOND', name='Second',
                  area_code='AREA-004', area_feature_id='zone-AREA-004')
    next(f for f in source['features'] if f['id']=='zone-AREA-004')['storage_layout']='rack'
    source['racks'].append(second); source['revision'] = editor._floor_revision(source)
    with factory() as db:
        for p in db.scalars(select(WarehouseAreaStoragePolicy)):
            p.published_map_revision=source['revision']
            if p.map_feature_id=='zone-AREA-004': p.storage_layout='rack'
        for loc in db.scalars(select(WarehouseLocation).where(WarehouseLocation.map_rack_id==rack_id)):
            loc.is_active=False
        db.commit()
    target = deepcopy(source); target['retired_racks'] = []
    for key in (rack_id, second['id']):
        rack = next(r for r in target['racks'] if r['id']==key)
        target['racks'].remove(rack)
        target['retired_racks'].append(dict(rack, retired_at='2026-10-13T00:00:00Z', retired_reason='synthetic'))
    target['revision'] = editor._floor_revision(target)
    doc['floors']['1F'] = target; runtime.write_text(json.dumps(doc))
    return factory, source, target, (rack_id, second['id']), runtime, draft


def service_apply(db, source, target, rack_ids):
    from sqlalchemy import select
    from app.models.user import User
    from app.services.warehouse_map_publication import carry_retired_rack_area, complete_floor_publication
    actor=db.scalar(select(User))
    retired_count=sum(carry_retired_rack_area(db, previous_floor_layout=source, floor_layout=target,
        retired_rack_id=key, actor=actor, operation_key='synthetic-recovery') for key in rack_ids)
    counts=complete_floor_publication(db, previous_floor_layout=source, floor_layout=target,
        actor=actor, operation_key='synthetic-recovery')
    return retired_count, counts


def test_one_retired_argument_is_insufficient_and_two_explicit_checks_close_all_23(tmp_path, monkeypatch):
    from sqlalchemy import select,func
    from app.models.user import User
    from app.models.audit import OperationLog
    from app.models.warehouse_inventory import WarehouseAreaStoragePolicy
    from app.services.warehouse_area_activation import WarehouseAreaActivationError
    from app.services.warehouse_map_publication import complete_floor_publication, assert_current_floor_bindings
    from tests.test_inventory_map_publication_closure import immutable_facts
    factory, source, target, ids, runtime, draft = two_retired_service_case(tmp_path, monkeypatch)
    files = runtime.read_bytes(), draft.read_bytes()
    try:
        with factory() as db:
            before = immutable_facts(db)
            with pytest.raises(WarehouseAreaActivationError):
                complete_floor_publication(db, previous_floor_layout=source, floor_layout=target,
                    actor=db.scalar(select(User)), operation_key='synthetic-one-rack', retired_rack_id=ids[0])
            db.rollback()
            assert all(p.version==1 and p.published_map_revision==source['revision'] for p in db.scalars(select(WarehouseAreaStoragePolicy)))
            assert db.scalar(select(func.count()).select_from(OperationLog)) == 0
            assert service_apply(db, source, target, ids) == (2, (21, 3, 0))
            db.commit(); assert_current_floor_bindings(db, target)
            assert all(p.version==2 and p.published_map_revision==target['revision'] for p in db.scalars(select(WarehouseAreaStoragePolicy)))
            assert immutable_facts(db)==before
            audits=db.scalar(select(func.count()).select_from(OperationLog))
            assert audits==26
            assert service_apply(db, source, target, ids)==(0,(0,0,0))
            db.commit()
            assert db.scalar(select(func.count()).select_from(OperationLog))==audits
            assert immutable_facts(db)==before and files==(runtime.read_bytes(),draft.read_bytes())
    finally: factory.kw['bind'].dispose()


@pytest.mark.parametrize('failure', ['second_identity', 'ground_audit', 'commit'])
def test_existing_transaction_rolls_back_both_retirements_and_all_sibling_policies(tmp_path, monkeypatch, failure):
    from sqlalchemy import select,func
    from app.models.audit import OperationLog
    from app.models.warehouse_inventory import WarehouseAreaStoragePolicy
    from app.services.warehouse_area_activation import WarehouseAreaActivationError
    from app.services import warehouse_ground_map_application as ground
    from tests.test_inventory_map_publication_closure import immutable_facts
    factory, source, target, ids, runtime, draft = two_retired_service_case(tmp_path, monkeypatch)
    files=runtime.read_bytes(),draft.read_bytes()
    def fail(*a,**k): raise RuntimeError('synthetic transaction failure')
    try:
        with factory() as db:
            before=immutable_facts(db)
            if failure=='second_identity': target['retired_racks'][1]['width_mm']+=1
            elif failure=='ground_audit': monkeypatch.setattr(ground,'append_audit_event',fail)
            else: monkeypatch.setattr(db,'commit',fail)
            with pytest.raises((RuntimeError,WarehouseAreaActivationError)):
                service_apply(db,source,target,ids); db.commit()
            db.rollback()
            assert all(p.version==1 and p.published_map_revision==source['revision'] for p in db.scalars(select(WarehouseAreaStoragePolicy)))
            assert db.scalar(select(func.count()).select_from(OperationLog))==0
            assert immutable_facts(db)==before and files==(runtime.read_bytes(),draft.read_bytes())
    finally: factory.kw['bind'].dispose()
