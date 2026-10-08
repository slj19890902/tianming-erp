import json
from datetime import date, datetime
from copy import deepcopy

import pytest
from sqlalchemy import select
from app.api import warehouse as api
from app.models.user import User
from app.models.warehouse_inventory import (
    InventoryLot, WarehouseAreaStoragePolicy, WarehouseGroundLayoutPlan,
    WarehouseGroundLayoutSlot, WarehouseLocation,
)
from app.services import warehouse_twin_layout_editor as editor
from app.services.warehouse_ground_map_application import record_map_applications
from app.services.warehouse_area_activation import WarehouseAreaActivationError
from app.services.location_candidates import operational_location_issue
from test_p1_47b_warehouse_area_planning import (
    _isolate_layout_paths, _database, _request, _confirm_area_payload,
)


@pytest.fixture
def ready_ground_sibling(tmp_path, monkeypatch):
    published, draft = _isolate_layout_paths(tmp_path, monkeypatch)
    document = json.loads(published.read_text('utf-8'))
    floor = document['floors']['3F']
    first = floor['features'][0]
    first['points'] = [[0, 0], [5000, 0], [5000, 10000], [0, 10000]]
    first['area_mm2'] = 50000000
    second = deepcopy(first)
    second.update(id='zone-f2', feature_code='ZONE-3F-F2', erp_area_code='F2',
                  name='F2', points=[[5000, 0], [10000, 0], [10000, 10000], [5000, 10000]])
    floor['features'].append(second)
    floor['revision'] = editor._floor_revision(floor)
    published.write_text(json.dumps(document), encoding='utf-8')
    runtime = editor.TWIN_LAYOUT_PATH
    from app.services import warehouse_twin_layout
    monkeypatch.setattr(warehouse_twin_layout, 'TWIN_LAYOUT_RUNTIME_PATH', runtime)
    monkeypatch.setattr(warehouse_twin_layout, 'TWIN_LAYOUT_PATH', published)
    def live(code):
        return json.loads((runtime if runtime.exists() else published).read_text('utf-8'))['floors'][code]
    monkeypatch.setattr(api, 'load_warehouse_twin_floor', live)
    monkeypatch.setattr(api, 'list_production_projection_mappings', lambda *a, **k: [])
    engine, factory = _database(tmp_path)
    try:
        with factory() as db:
            actor = db.scalar(select(User))
            result = api.confirm_twin_zone_area('3F', 'zone-f1', _confirm_area_payload(
                revision=floor['revision'], operation_key='r18-first-area', capacity=4), _request(), db, actor)
            plan = db.scalar(select(WarehouseGroundLayoutPlan))
            location = db.get(WarehouseLocation, plan.slots[0].location_id)
            lot = InventoryLot(lot_number='R18-FICTIONAL-OCCUPIED', inventory_type='finished',
                warehouse_location_id=location.id, quantity_available=5,
                quantity_reserved=0, quantity_consumed=0, quantity_damaged=0,
                quantity_scrapped=0, unit='boxes', status='active', source_type='manual', created_by=actor.id,
                stock_date=date(2026, 10, 7), last_movement_at=datetime(2026, 10, 7, 12, 0))
            db.add(lot);db.commit()
            yield db, actor, live, runtime, plan, lot, result
    finally:
        engine.dispose()


def stable_rows(db, model):
    return [tuple(getattr(row, c.name) for c in model.__table__.columns)
            for row in db.scalars(select(model).order_by(model.id)).all()]


def test_second_ground_area_keeps_occupied_sibling_operational(ready_ground_sibling):
    db, actor, live, runtime, plan, lot, first = ready_ground_sibling
    inventory_before = stable_rows(db, InventoryLot)
    slots_before = stable_rows(db, WarehouseGroundLayoutSlot)
    policy = db.get(WarehouseAreaStoragePolicy, plan.area.storage_policy.id)
    policy_version = policy.version
    first_floor = deepcopy(live('3F'))
    result = api.confirm_twin_zone_area('3F', 'zone-f2', _confirm_area_payload(
        revision=first['published_revision'], operation_key='r18-second-area',
        area_code='F2', area_name='虚构备库区', usage='raw_material', capacity=4), _request(), db, actor)
    assert result['status'] == 'published'
    assert stable_rows(db, InventoryLot) == inventory_before
    assert stable_rows(db, WarehouseGroundLayoutSlot)[:len(slots_before)] == slots_before
    db.refresh(policy)
    assert policy.published_map_revision == result['published_revision']
    assert policy.version == policy_version + 1
    assert plan.published_map_revision == first['published_revision']
    assert next(x for x in live('3F')['features'] if x['id'] == 'zone-f1') == next(x for x in first_floor['features'] if x['id'] == 'zone-f1')
    location = db.get(WarehouseLocation, lot.warehouse_location_id)
    assert operational_location_issue(db, location, warehouse_types={'finished', 'shared'},
        require_published=True, require_map_geometry=True, required_inventory_type='finished') is None
    from app.services.production_workflow import receipt_auto_finished_location_projection
    assert receipt_auto_finished_location_projection(db)['ready'] is True
    # Only the new area's first operational receipt is added; the carried sibling does not duplicate.
    args=dict(floor_layout=live('3F'), previous_floor_layout=live('3F'), actor=actor,
              operation_key='r18-repeat-map-receipt', isolated_area_feature_id='zone-f2')
    assert record_map_applications(db, **args) == 1
    assert record_map_applications(db, **args) == 0


@pytest.mark.parametrize('invalid', ['unscoped', 'changed_feature', 'unknown_previous_revision'])
def test_carry_requires_explicit_scope_and_unchanged_verified_source(ready_ground_sibling, invalid):
    db, actor, live, runtime, plan, lot, first = ready_ground_sibling
    previous = deepcopy(live('3F'));target = deepcopy(previous)
    if invalid == 'changed_feature':
        target['features'][0]['points'][1][0] -= 100
    if invalid == 'unknown_previous_revision':
        previous['revision'] = 'unverified-revision'
    target['revision'] = 'new-current-revision'
    before = stable_rows(db, WarehouseAreaStoragePolicy)
    with pytest.raises(WarehouseAreaActivationError, match='版本不一致'):
        record_map_applications(db, floor_layout=target, previous_floor_layout=previous,
            actor=actor, operation_key='r18-invalid-carry',
            isolated_area_feature_id=None if invalid == 'unscoped' else 'zone-f2')
    db.rollback()
    assert stable_rows(db, WarehouseAreaStoragePolicy) == before


def test_second_area_failure_restores_map_policy_and_inventory(ready_ground_sibling, monkeypatch):
    db, actor, live, runtime, plan, lot, first = ready_ground_sibling
    before_file=runtime.read_bytes();before_policy=stable_rows(db, WarehouseAreaStoragePolicy)
    before_lots=stable_rows(db, InventoryLot)
    def fail(*a, **k):raise RuntimeError('r18 injected failure after publish')
    monkeypatch.setattr(api, '_ensure_one_step_ground_plan', fail)
    with pytest.raises(RuntimeError, match='r18 injected'):
        api.confirm_twin_zone_area('3F', 'zone-f2', _confirm_area_payload(
            revision=first['published_revision'], operation_key='r18-second-area-rollback',
            area_code='F2', area_name='虚构备库区', usage='raw_material', capacity=4), _request(), db, actor)
    assert runtime.read_bytes()==before_file
    assert stable_rows(db, WarehouseAreaStoragePolicy)==before_policy
    assert stable_rows(db, InventoryLot)==before_lots
