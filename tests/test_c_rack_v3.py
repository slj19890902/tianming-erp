"""C-rack aliases are current names; physical cell UUIDs preserve history."""
from copy import deepcopy
from uuid import uuid4
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, func

from app.services.mold_cells import reconcile_mold_cells, projected_cells
from app.services import mold_location
from app.services import warehouse_twin_layout_editor as editor
from app.api import warehouse as api
from app.models.user import User
from app.models.mold_tool import MoldTool, MoldLocationMovement, MoldLabelPrintJob
from app.models.audit import OperationLog


def rack(counts=(3,)):
    return reconcile_mold_cells(dict(id=str(uuid4()), name='模具C架', mold_rack_code='C',
        levels=len(counts), bays=max(counts), level_cell_counts=list(counts),
        area_code='MOLD', area_feature_id='mold-zone', version=1))


def resize(previous, counts):
    changed = deepcopy(previous)
    changed.update(levels=len(counts), bays=max(counts), level_cell_counts=list(counts))
    changed.pop('mold_cells')
    return reconcile_mold_cells(changed, previous)


def test_c1_c2_c3_shrink_expand_restores_unique_physical_cells_and_keeps_history():
    original = rack()
    reduced = resize(original, (1,))
    retired = deepcopy(reduced['retired_mold_cells'])
    expanded = resize(reduced, (3,))
    assert [cell['alias'] for cell in expanded['mold_cells']] == ['C1','C2','C3']
    assert expanded['mold_cells'] == original['mold_cells']
    assert expanded['retired_mold_cells'] == retired
    assert original['retired_mold_cells'] == []


def test_historical_alias_can_name_a_different_slot_without_rebinding_retired_qr():
    original = rack()
    reduced = resize(original, (1,))
    expanded = resize(reduced, (1,2))
    assert [cell['alias'] for cell in expanded['mold_cells']] == ['C1','C2','C3']
    assert {c['id'] for c in original['mold_cells'][1:]}.isdisjoint(c['id'] for c in expanded['mold_cells'])
    floors = [dict(floor_code='3F', racks=[expanded])]
    for retired in projected_cells(original)[1:]:
        assert mold_location.find_mold_cell(retired['location_code'], floors=floors) is None
        history = mold_location.find_mold_cell(retired['location_code'], floors=floors, include_retired=True)
        assert history[2]['id'] == retired['id']


def test_legacy_already_recreated_cells_can_reuse_short_alias_without_replacing_ids():
    original = rack()
    reduced = resize(original, (1,))
    legacy = deepcopy(reduced)
    legacy['level_cell_counts'] = [3]
    legacy['mold_cells'] += [dict(id=str(uuid4()), level=1, grid=grid, alias=f'C{grid+2}') for grid in (2,3)]
    changed = deepcopy(legacy)
    for index, cell in enumerate(changed['mold_cells'], 1):
        cell['alias'] = f'C{index}'
    result = reconcile_mold_cells(changed, legacy)
    assert [c['id'] for c in result['mold_cells']] == [c['id'] for c in legacy['mold_cells']]
    assert result['retired_mold_cells'] == reduced['retired_mold_cells']


def test_ambiguous_same_slot_history_gets_new_identity_and_keeps_both_old_codes_retired():
    original = rack()
    reduced = resize(original, (1,))
    reduced['retired_mold_cells'].append({**reduced['retired_mold_cells'][0], 'id':str(uuid4()), 'alias':'C4'})
    expanded = resize(reduced, (3,))
    slot2 = expanded['mold_cells'][1]
    assert slot2['alias'] == 'C2'
    assert slot2['id'] not in {c['id'] for c in reduced['retired_mold_cells']}
    assert expanded['mold_cells'][2]['id'] == original['mold_cells'][2]['id']
    assert expanded['retired_mold_cells'] == reduced['retired_mold_cells']


@pytest.fixture
def c_http(tmp_path, monkeypatch):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.core.security import hash_password
    from app.models.customer import Customer
    from tests.test_p1_47b_warehouse_area_planning import _isolate_layout_paths, _database, _confirm_area_payload
    from tests.test_p1_58_mold_label_print_status import _printable_mold
    published, draft = _isolate_layout_paths(tmp_path, monkeypatch)
    runtime = editor.TWIN_LAYOUT_PATH
    def live(code):
        source = runtime if runtime.exists() else published
        floors = json.loads(source.read_text())['floors']
        if code not in floors:
            from app.services.warehouse_twin_layout import WarehouseTwinLayoutNotFoundError
            raise WarehouseTwinLayoutNotFoundError(code)
        return floors[code]
    monkeypatch.setattr(api, 'load_warehouse_twin_floor', live)
    monkeypatch.setattr(mold_location, 'load_warehouse_twin_floor', live)
    monkeypatch.setattr(api, 'list_production_projection_mappings', lambda *a, **k: [])
    engine, factory = _database(tmp_path)
    with factory() as db:
        db.scalar(select(User)).password_hash = hash_password('Isolated-C-Rack123!')
        db.add(Customer(customer_number=1, customer_code='C-RACK', name='C架合成客户', payment_term_days=30, credit_limit=1000))
        db.commit()
    mold_id, mold_code, _ = _printable_mold(factory, suffix='1')
    app = FastAPI()
    app.include_router(auth_router, prefix='/api/auth')
    app.include_router(api.router, prefix='/api/warehouse')
    def db_override():
        with factory() as db:
            yield db
    app.dependency_overrides[get_db] = db_override
    with TestClient(app, raise_server_exceptions=False) as client:
        login = client.post('/api/auth/login', json=dict(username='p1-47b-admin', password='Isolated-C-Rack123!'))
        assert login.status_code == 200, login.text
        area = client.post('/api/warehouse/twin-layout/floors/3F/zones/zone-f1/confirm-area',
            json=_confirm_area_payload(revision=live('3F')['revision'], operation_key='c-v3-confirm-area',
                usage='mold', storage_layout='rack', area_name='三楼模具区', capacity=5).model_dump())
        assert area.status_code == 200, area.text
        yield dict(client=client, factory=factory, live=live, runtime=runtime, draft=draft,
            mold_id=mold_id, mold_code=mold_code)
    engine.dispose()


def values(counts=(3,), **extra):
    from tests.test_p1_47b_warehouse_area_planning import _rack_values
    return {**_rack_values(levels=len(counts), level_cell_counts=list(counts)),
        'name':'模具C架', 'mold_rack_code':'C', **extra}


def create(case, *, key='c-v3-create-rack', **extra):
    result = case['client'].post('/api/warehouse/twin-layout/floors/3F/racks', json={**values(**extra),
        'expected_revision':editor.load_effective_warehouse_twin_floor_for_edit('3F')['revision'],
        'operation_key':key, 'area_feature_id':'zone-f1'})
    assert result.status_code == 201, result.text
    return result.json()


def apply(case, saved, key):
    result = case['client'].post(f"/api/warehouse/twin-layout/floors/3F/racks/{saved['item']['id']}/apply", json={
        'expected_revision':editor.load_effective_warehouse_twin_floor_for_edit('3F')['revision'],
        'expected_published_revision':case['live']('3F')['revision'],
        'expected_version':saved['item']['version'], 'operation_key':key})
    assert result.status_code == 200, result.text
    return result.json()


def update(case, prior, *, key, counts=(3,), cells=None):
    payload = {**values(counts), 'expected_revision':editor.load_effective_warehouse_twin_floor_for_edit('3F')['revision'],
        'expected_version':prior['version'], 'operation_key':key}
    if cells is not None: payload['mold_cells'] = cells
    return case['client'].patch(f"/api/warehouse/twin-layout/floors/3F/racks/{prior['id']}", json=payload)


def delete(case, item, key):
    return case['client'].delete(f"/api/warehouse/twin-layout/floors/3F/racks/{item['id']}", params={
        'expected_revision':editor.load_effective_warehouse_twin_floor_for_edit('3F')['revision'],
        'expected_published_revision':case['live']('3F')['revision'],
        'expected_version':item['version'], 'operation_key':key})


def move(case, target, version, key):
    return case['client'].post('/api/warehouse/molds/location-movement/batch', json={
        'items':[{'mold_code':case['mold_code'],'expected_version':version}],
        'target_location':target, 'idempotency_key':key})


def test_http_c_rack_full_chain_keeps_identity_location_history_and_prints(c_http):
    case = c_http; client = case['client']
    original = create(case)
    apply(case, original, 'c-v3-first-apply')
    identity = original['item']['id']
    cells = deepcopy(original['item']['mold_cells'])
    labels = client.get('/api/warehouse/molds/location-labels', params={'floor_code':'3F','rack_id':identity})
    assert labels.status_code == 200, labels.text
    qr_before = {r['cell_id']:r['url'] for r in labels.json()['labels'] if r['cell_id']}
    reduced = update(case, original['item'], key='c-v3-shrink', counts=(1,))
    assert reduced.status_code == 200, reduced.text
    apply(case, reduced.json(), 'c-v3-shrink-apply')
    assert client.get('/api/warehouse/molds/cell', params={'cell_id':cells[1]['id']}).status_code == 404
    archived = deepcopy(reduced.json()['item']['retired_mold_cells'])
    expanded = update(case, reduced.json()['item'], key='c-v3-expand')
    assert expanded.status_code == 200, expanded.text
    assert expanded.json()['item']['mold_cells'] == cells
    assert expanded.json()['item']['retired_mold_cells'] == archived
    apply(case, expanded.json(), 'c-v3-expand-apply')
    # Exercise explicit alias editing, including names retained in history.
    swapped = deepcopy(cells)
    swapped[1]['alias'], swapped[2]['alias'] = 'C3', 'C2'
    mapped = update(case, expanded.json()['item'], key='c-v3-renumber', cells=swapped)
    assert mapped.status_code == 200, mapped.text
    apply(case, mapped.json(), 'c-v3-renumber-apply')
    natural = update(case, mapped.json()['item'], key='c-v3-natural', cells=cells)
    assert natural.status_code == 200, natural.text
    # An unrelated saved D draft must not be published by C's single-rack apply.
    other = create(case, key='c-v3-unrelated-d', mold_rack_code='D', name='模具D架', x_mm=8500)
    other_before = deepcopy(other['item'])
    apply(case, natural.json(), 'c-v3-natural-apply')
    refreshed = client.get('/api/warehouse/twin-layout/floors/3F')
    assert refreshed.status_code == 200, refreshed.text
    current = next(r for r in refreshed.json()['racks'] if r['id'] == identity)
    assert current['mold_cells'] == cells
    assert current['retired_mold_cells'] == archived
    assert not any(r.get('mold_rack_code') == 'D' for r in refreshed.json()['racks'])
    assert next(r for r in editor.load_effective_warehouse_twin_floor_for_edit('3F')['racks'] if r['id'] == other_before['id']) == other_before
    target = projected_cells(current)[1]
    moved = client.post('/api/warehouse/molds/location-movement/batch', json={
        'items':[{'mold_code':case['mold_code'],'expected_version':1}], 'target_location':target['location_code'],
        'idempotency_key':'c-v3-putaway'})
    assert moved.status_code == 200, moved.text
    found = client.get('/api/warehouse/molds', params={'q':'C2'})
    assert found.status_code == 200 and [r['id'] for r in found.json()['items']] == [case['mold_id']], found.text
    by_filter = client.get('/api/warehouse/molds', params={'rack_location':'C2'})
    assert [r['id'] for r in by_filter.json()['items']] == [case['mold_id']]
    content = client.get('/api/warehouse/molds/cell', params={'cell_id':target['id']})
    assert content.status_code == 200 and content.json()['items'][0]['id'] == case['mold_id']
    labels_after = client.get('/api/warehouse/molds/location-labels', params={'floor_code':'3F','rack_id':identity})
    assert {r['cell_id']:r['url'] for r in labels_after.json()['labels'] if r['cell_id']} == qr_before
    options = client.get('/api/warehouse/molds/label-options', params={'mold_ids':str(case['mold_id'])})
    assert options.status_code == 200 and options.json()['items'][0]['printable'], options.text
    with case['factory']() as db:
        assert db.scalar(select(func.count()).select_from(MoldLabelPrintJob)) == 0
    printed = client.post('/api/warehouse/molds/label-prints', json={
        'mold_ids':[case['mold_id']], 'source':'batch', 'idempotency_key':'c-v3-print'})
    assert printed.status_code == 200, printed.text
    frozen = client.get('/api/warehouse/molds/labels', params={'mold_ids':str(case['mold_id']),
        'print_job_id':printed.json()['print_job_id']})
    assert frozen.status_code == 200, frozen.text
    with case['factory']() as db:
        assert db.get(MoldTool, case['mold_id']).rack_location == target['location_code']
        assert db.scalar(select(func.count()).select_from(MoldLocationMovement)) == 1
    before = (case['runtime'].read_bytes(), editor.snapshot_warehouse_twin_layout_draft())
    assert delete(case, current, 'c-v3-occupied-delete').status_code == 409
    assert update(case, current, key='c-v3-occupied-shrink', counts=(1,)).status_code == 409
    assert before == (case['runtime'].read_bytes(), editor.snapshot_warehouse_twin_layout_draft())
    apply(case, other, 'c-v3-d-apply')
    d_target = projected_cells(other_before)[0]['location_code']
    assert move(case, d_target, 2, 'c-v3-move-out').status_code == 200
    removed = delete(case, current, 'c-v3-empty-delete')
    assert removed.status_code == 200, removed.text
    assert client.get('/api/warehouse/molds/cell', params={'cell_id':target['id']}).status_code == 404
    assert move(case, target['location_code'], 3, 'c-v3-retired-putaway').status_code == 409
    restored = create(case, key='c-v3-restore-rack')
    assert restored['item']['id'] == identity
    assert restored['item']['mold_cells'] == cells
    assert restored['item']['restoration_history'][-1]['mold_cells'] == cells
    apply(case, restored, 'c-v3-restore-apply')
    original_qr = client.get('/api/warehouse/molds/cell', params={'cell_id':target['id']})
    assert original_qr.status_code == 200 and original_qr.json()['cell']['id'] == target['id']
    assert move(case, target['location_code'], 3, 'c-v3-return-home').status_code == 200
    with case['factory']() as db:
        movements = db.scalars(select(MoldLocationMovement).order_by(MoldLocationMovement.id)).all()
        assert len(movements) == 3
        assert movements[0].to_location == movements[-1].to_location == target['location_code']
        assert db.scalar(select(func.count()).select_from(MoldLabelPrintJob)) == 1


def test_http_alias_reuse_never_redirects_old_qr_or_accepts_short_label_as_destination(c_http):
    case = c_http; client = case['client']
    original = create(case); apply(case, original, 'c-v3-alias-first')
    old = projected_cells(original['item'])[1]
    reduced = update(case, original['item'], key='c-v3-alias-shrink', counts=(1,)).json()
    apply(case, reduced, 'c-v3-alias-shrink-apply')
    different = update(case, reduced['item'], key='c-v3-different-slot', counts=(1,2)).json()
    apply(case, different, 'c-v3-different-apply')
    new = projected_cells(different['item'])[1]
    assert new['alias'] == old['alias'] == 'C2' and new['id'] != old['id']
    assert client.get('/api/warehouse/molds/cell', params={'cell_id':old['id']}).status_code == 404
    assert client.get('/api/warehouse/molds/cell', params={'cell_id':new['id']}).status_code == 200
    assert move(case, old['location_code'], 1, 'c-v3-old-qr-refused').status_code == 409
    alias_only = move(case, 'C2', 1, 'c-v3-alias-refused')
    assert alias_only.status_code == 409 and '短号' in alias_only.text
    assert move(case, new['location_code'], 1, 'c-v3-new-qr-accepted').status_code == 200
    printed = client.get('/api/warehouse/molds/location-labels', params={'floor_code':'3F','rack_id':original['item']['id']})
    label = next(r for r in printed.json()['labels'] if r['cell_id'] == new['id'])
    assert label['label'] == '模具位 C2' and new['id'] in label['url'] and old['id'] not in label['url']
    assert mold_location.describe_mold_location(old['location_code'])['kind'] == 'retired_cell'


@pytest.mark.parametrize('failure', ['duplicate', 'stale', 'identity', 'audit', 'commit'])
def test_http_rejected_alias_edit_preserves_files_ids_and_audit(c_http, monkeypatch, failure):
    case = c_http
    original = create(case); apply(case, original, 'c-v3-rollback-first')
    cells = deepcopy(original['item']['mold_cells'])
    prior = deepcopy(original['item'])
    if failure == 'duplicate': cells[1]['alias'] = cells[0]['alias']
    elif failure == 'stale': prior['version'] += 1
    elif failure == 'identity': cells[1]['id'] = str(uuid4())
    else: cells[1]['alias'] = 'C99'
    before = (case['runtime'].read_bytes(), editor.snapshot_warehouse_twin_layout_draft())
    with case['factory']() as db:
        audits = db.scalar(select(func.count()).select_from(OperationLog))
    def fail(*a, **k): raise RuntimeError('c-v3 injected transaction failure')
    if failure == 'audit': monkeypatch.setattr(api, '_twin_layout_asset_log', fail)
    elif failure == 'commit':
        from sqlalchemy.orm import Session
        monkeypatch.setattr(Session, 'commit', fail)
    result = update(case, prior, key='c-v3-rollback-edit', cells=cells)
    assert result.status_code == {'duplicate':422, 'stale':409, 'identity':422, 'audit':500, 'commit':500}[failure], result.text
    assert before == (case['runtime'].read_bytes(), editor.snapshot_warehouse_twin_layout_draft())
    with case['factory']() as db:
        assert db.scalar(select(func.count()).select_from(OperationLog)) == audits
        assert db.scalar(select(func.count()).select_from(MoldLocationMovement)) == 0


def test_repeated_retirement_keeps_each_alias_snapshot_and_rejects_history_slot_corruption():
    original = rack()
    reduced = resize(original, (1,))
    restored = resize(reduced, (3,))
    restored['mold_cells'][1]['alias'] = 'C22'
    latest = resize(restored, (1,))
    assert latest['retired_mold_cells'][0]['alias'] == 'C2'
    assert latest['retired_mold_cells'][-1]['alias'] == 'C22'
    historical = mold_location.find_mold_cell(original['mold_cells'][1]['id'],
        floors=[{'racks':[latest]}], include_retired=True)
    assert historical[2]['alias'] == 'C22'
    invalid = deepcopy(latest)
    invalid['retired_mold_cells'].append({**invalid['retired_mold_cells'][0], 'grid':20})
    with pytest.raises(ValueError, match='多个物理层格'): resize(invalid, (3,))
