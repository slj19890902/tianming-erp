"""HTTP -> actual JS persistence -> HTTP, using disposable inventory and ledger."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import types
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, event, func

from app.models.warehouse_inventory import Floor3LocationLayout, WarehouseArea, WarehouseFloor, WarehouseLocation
from app.services import delivery_dispatch_commands as service, location_candidates
from app.api import deliveries
from app.models.audit import OperationLog
from app.models.finance import FinanceIdempotencyRecord
from tests.test_n036_delivery_pick import pick_app, _create_task, _login
from tests.test_phase7_deliveries import _seed_historical_finished_delivery_inventory, delivery_api_app
from tests.test_delivery_dispatch_commands import close_request, resolve, state, seed

ROOT = Path(__file__).resolve().parents[1]
NODE = os.environ.get('ERP_TEST_NODE') or shutil.which('node') or '/Users/kimiyf/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node'
LEGACY = ROOT/'tests/fixtures/delivery-dispatch-map-v1'


@pytest.fixture
def old_reader(monkeypatch):
    provenance = json.loads((LEGACY/'provenance.json').read_text())
    assert provenance['source_commit'] == '61e5fafba43ec9fe27e0381d0dbab59d76eb96a5'
    for name, digest in provenance['files'].items():
        assert hashlib.sha256((LEGACY/name).read_bytes()).hexdigest() == digest
    module = types.ModuleType('dispatch_map_baseline_v625')
    monkeypatch.setitem(sys.modules, module.__name__, module)
    source = (LEGACY/'service.py.txt').read_text()
    exec(compile(source, str(LEGACY/'service.py.txt'), 'exec'), module.__dict__)
    return module


def browser(body, display, *, response=None, closing=False, helper=None):
    result = subprocess.run([NODE, str(ROOT/'tests/ui/dispatch_map_roundtrip.cjs'),
        str(helper or ROOT/'static/ui/delivery-dispatch-recovery.js')],
        input=json.dumps(dict(body=body, display=display, response=response, closing=closing)),
        text=True, capture_output=True, check=True)
    return json.loads(result.stdout)


@pytest.fixture
def mapped_app(pick_app, monkeypatch):
    app, factory, ids, _ = pick_app
    _seed_historical_finished_delivery_inventory(factory, *ids['order_items'])
    # Only the published map file identity is synthetic. Map projection,
    # persisted geometry, task planning, auth, ledger and transactions are real.
    monkeypatch.setattr(location_candidates, 'load_warehouse_twin_published_floor_identity',
        lambda floor: {'revision': 'dispatch-map-fixture', 'zones_by_id': {'dispatch-map-area': 'FG'},
            'zone_ids_by_area': {'FG': ('dispatch-map-area',)}} if int(floor) == 3 else None)
    with factory() as db:
        floor = WarehouseFloor(floor_code='3F', floor_name='三楼', floor_number=3, construction_status='enabled')
        db.add(WarehouseArea(floor=floor, area_code='FG', area_name='成品', construction_status='enabled'))
        location = db.scalar(select(WarehouseLocation).where(WarehouseLocation.location_code == 'PHASE7-FG'))
        location.warehouse_floor = 3
        location.area_code = 'FG'
        location.placement_status = 'placed'
        location.storage_type = 'rack'
        location.source_version = 'V11'
        location.floor3_layout = Floor3LocationLayout(left_pct=10, top_pct=20, width_pct=5, height_pct=2.5,
            z_index=0, version=1, source_type='manual')
        db.commit()
    with TestClient(app) as client:
        _login(client, 'admin')
        task = _create_task(client, ids['delivery'])
        _login(client, 'delivery_picker')
        for index, row in enumerate(task['items']):
            result = client.put(f"/api/delivery-picks/{task['id']}/items/{row['id']}",
                json=dict(pick_status='partial' if index == 0 else 'picked', picked_quantity=80 if index == 0 else 50))
            assert result.status_code == 200, result.text
        assert client.post(f"/api/delivery-picks/{task['id']}/submit").status_code == 200
    return app, factory, ids


def preview(client, did):
    response = client.get(f'/api/deliveries/{did}/dispatch-snapshot', params={'pick_action': 'apply'})
    assert response.status_code == 200, response.text
    packet = response.json()
    body = dict(expected_actor_id=packet['current_actor_id'], idempotency_key='dispatch-map-original',
        expected_version=packet['snapshot']['delivery']['version'], snapshot_hash=packet['snapshot_hash'],
        snapshot=packet['snapshot'], confirm_pick_exception=True)
    assert body['snapshot']['pick_task']['items'][0]['location_lines'][0]['map_point']
    return body, packet['display']


@pytest.mark.parametrize('operation', ['execute', 'resolve', 'close'])
@pytest.mark.parametrize('legacy', [False, True])
def test_mapped_http_js_http_preserves_original_request(mapped_app, operation, legacy, old_reader, monkeypatch):
    app, factory, ids = mapped_app
    with TestClient(app) as client:
        _login(client, 'admin')
        with monkeypatch.context() as patch:
            if legacy:
                patch.setattr(deliveries, 'dispatch_commands', old_reader)
            original, display = preview(client, ids['delivery'])
        saved = browser(original, display, closing=operation == 'close')
        if legacy:
            assert service.digest(saved['body']['snapshot']) != original['snapshot_hash']
            assert 'map_coordinate_encoding' not in original['snapshot']
        else:
            assert service.digest(saved['body']['snapshot']) == original['snapshot_hash']
            assert original['snapshot']['map_coordinate_encoding'] == 'decimal-string-v1'
            point = original['snapshot']['pick_task']['items'][0]['location_lines'][0]['map_point']
            assert [point[k] for k in service.MAP_COORDINATES] == ['10','20','5','2.5']
        assert saved['key'] == original['idempotency_key']
        body = saved['body']
        before = state(factory)
        response = (client.put(f"/api/deliveries/{ids['delivery']}/dispatch", json=body) if operation == 'execute'
            else resolve(client, ids['delivery'], body) if operation == 'resolve'
            else close_request(client, ids['delivery'], body))
        assert response.status_code == 200, response.text
        record = browser(body, display, response=response.json())
        if operation == 'execute':
            assert record['state'] == 'confirmed'
            receipt = response.json()['dispatch_receipt']
            assert receipt['stages']['pick_applied']
            assert sum(row['physical_quantity'] for row in receipt['movements']) == 130
            assert sum(row[3] for row in state(factory)['lots']) == 130
        elif operation == 'close':
            assert record['state'] == 'closed'
            after = state(factory)
            assert after['lots'] == before['lots'] and after['deliveries'] == before['deliveries']
        else:
            assert record['state'] == 'unknown' and response.json()['status'] == 'not_recorded'
            assert state(factory) == before


@pytest.mark.parametrize('terminal', ['completed', 'closed'])
def test_real_v625_terminal_receipts_restore_after_browser_roundtrip(mapped_app, old_reader, monkeypatch, terminal):
    app, factory, ids = mapped_app
    did = ids['delivery']
    with TestClient(app) as client:
        _login(client, 'admin')
        with monkeypatch.context() as patch:
            patch.setattr(deliveries, 'dispatch_commands', old_reader)
            body, display = preview(client, did)
            # Original v625 server accepts its Python JSON spelling, creating
            # genuine old-format inventory/ledger facts in this disposable DB.
            first = (client.put(f'/api/deliveries/{did}/dispatch', json=body) if terminal == 'completed'
                else close_request(client, did, body))
            assert first.status_code == 200, first.text
        wire = browser(body, display)['body']
        before = state(factory)
        statements = []
        def sql(_conn, _cursor, statement, *_args):
            statements.append(statement.lstrip().split()[0].upper())
        event.listen(factory.kw['bind'], 'before_cursor_execute', sql)
        try:
            found = resolve(client, did, wire)
        finally:
            event.remove(factory.kw['bind'], 'before_cursor_execute', sql)
        assert found.status_code == 200 and found.json()['status'] == terminal, found.text
        assert not {'INSERT','UPDATE','DELETE'} & set(statements)
        assert browser(wire, display, response=found.json())['state'] == ('confirmed' if terminal == 'completed' else 'closed')
        assert client.put(f'/api/deliveries/{did}/dispatch', json=wire).json() == first.json()
        assert close_request(client, did, wire).json() == first.json()
        assert state(factory) == before
        with factory() as db:
            record = db.scalar(select(FinanceIdempotencyRecord).where(FinanceIdempotencyRecord.idempotency_key == body['idempotency_key']))
            assert record.request_hash == old_reader.request_hash(did, old_reader.DispatchCommand(**body))


@pytest.mark.parametrize('terminal', ['completed', 'closed', 'pending'])
def test_actual_v625_fallback_reader_and_helper_accept_new_terminal_proofs(mapped_app, old_reader, monkeypatch, terminal):
    app, factory, ids = mapped_app
    did = ids['delivery']
    with TestClient(app) as client:
        _login(client, 'admin')
        body, display = preview(client, did)
        wire = browser(body, display, helper=LEGACY/'helper.cjs')['body']
        if terminal != 'pending':
            first = (client.put(f'/api/deliveries/{did}/dispatch', json=wire) if terminal == 'completed'
                else close_request(client, did, wire))
            assert first.status_code == 200, first.text
        before = state(factory)
        with monkeypatch.context() as patch:
            patch.setattr(deliveries, 'dispatch_commands', old_reader)
            found = resolve(client, did, wire)
            assert found.status_code == 200, found.text
            assert found.json()['status'] == ('not_recorded' if terminal == 'pending' else terminal)
            if terminal == 'pending':
                refused = client.put(f'/api/deliveries/{did}/dispatch', json=wire)
                assert refused.status_code == 409 and state(factory) == before
                first = close_request(client, did, wire)
                assert first.status_code == 200 and first.json()['result'] == 'closed', first.text
                terminal = 'closed'
                before = state(factory)
            assert client.put(f'/api/deliveries/{did}/dispatch', json=wire).json() == first.json()
            assert close_request(client, did, wire).json() == first.json()
            assert browser(wire, display, response=first.json(), helper=LEGACY/'helper.cjs')['state'] == ('confirmed' if terminal == 'completed' else 'closed')
        assert state(factory) == before
        assert browser(wire, display, response=resolve(client, did, wire).json())['state'] == ('confirmed' if terminal == 'completed' else 'closed')


@pytest.mark.parametrize('mutation', ['quantity','source','customer','version','id','map-value','map-extra','float-quantity',
    'encoding-unknown','encoding-null','encoding-bool','infinite','out-of-range','boolean-coordinate','string-coordinate'])
def test_legacy_repair_never_normalizes_other_fields_or_rehashes(mapped_app, old_reader, monkeypatch, mutation):
    app, factory, ids = mapped_app
    with TestClient(app) as client:
        _login(client, 'admin')
        with monkeypatch.context() as patch:
            patch.setattr(deliveries, 'dispatch_commands', old_reader)
            original, display = preview(client, ids['delivery'])
        if mutation == 'float-quantity':
            # A body signed with 100.0 outside the four map fields must not be
            # repaired after JS turns it into 100, even though values compare equal.
            original['snapshot']['items'][0]['physical_quantity'] = 100.0
            original['snapshot_hash'] = service.digest(original['snapshot'])
        wire = browser(original, display)['body']
        point = wire['snapshot']['pick_task']['items'][0]['location_lines'][0]['map_point']
        if mutation == 'quantity': wire['snapshot']['items'][0]['physical_quantity'] += 1
        elif mutation == 'source': wire['snapshot']['pick_task']['items'][0]['location_lines'][0]['inventory_lot_id'] = 999
        elif mutation == 'customer': wire['snapshot']['items'][0]['customer_id'] += 1
        elif mutation == 'version': wire['snapshot']['delivery']['version'] += 1
        elif mutation == 'id': wire['snapshot']['delivery']['id'] += 1
        elif mutation == 'map-value': point['left_pct'] += 1
        elif mutation == 'map-extra': point['injected'] = 10
        elif mutation.startswith('encoding-'): wire['snapshot']['map_coordinate_encoding'] = {'encoding-unknown':'future', 'encoding-null':None, 'encoding-bool':True}[mutation]
        elif mutation == 'infinite': point['left_pct'] = float('inf')
        elif mutation == 'out-of-range': point['left_pct'] = 101
        elif mutation == 'boolean-coordinate': point['left_pct'] = True
        elif mutation == 'string-coordinate': point['left_pct'] = '10'
        before = state(factory)
        # Raw JSON lets the server exercise its non-finite rejection too.
        for url, method, value in [(f"/api/deliveries/{ids['delivery']}/dispatch", 'put', wire), *[
            (f"/api/deliveries/{ids['delivery']}/dispatch-results/{wire['idempotency_key']}/{action}", 'post',
                dict(expected_actor_id=wire['expected_actor_id'], original_request=wire)) for action in ['resolve','close']]]:
            rejected = getattr(client, method)(url, content=json.dumps(value), headers={'Content-Type':'application/json'})
            assert rejected.status_code == 409, rejected.text
            assert rejected.headers['x-delivery-dispatch-original-invalid'] == '1'
            assert state(factory) == before


@pytest.mark.parametrize('legacy', [False, True])
@pytest.mark.parametrize('winner', ['execute', 'close'])
def test_map_execute_close_compete_in_same_real_transaction(mapped_app, old_reader, monkeypatch, legacy, winner):
    app, factory, ids = mapped_app
    with TestClient(app) as first_client, TestClient(app) as second_client:
        _login(first_client, 'admin'); _login(second_client, 'admin')
        with monkeypatch.context() as patch:
            if legacy: patch.setattr(deliveries, 'dispatch_commands', old_reader)
            original, display = preview(first_client, ids['delivery'])
        body = browser(original, display)['body']
        before = state(factory)
        ready, waiting, release = Event(), Event(), Event()
        guard = Lock()
        first_commit = True
        real_commit = service.commit_command
        def commit(db):
            nonlocal first_commit
            with guard:
                pause = first_commit
                first_commit = False
            if pause:
                ready.set()
                assert release.wait(10), 'race test did not release first commit'
            real_commit(db)
        def claim(_conn, _cursor, statement, *_args):
            if ready.is_set() and statement.lstrip().upper().startswith('UPDATE '):
                waiting.set()
        monkeypatch.setattr(service, 'commit_command', commit)
        event.listen(factory.kw['bind'], 'before_cursor_execute', claim)
        def request(client, operation):
            return (client.put(f"/api/deliveries/{ids['delivery']}/dispatch", json=body) if operation == 'execute'
                else close_request(client, ids['delivery'], body))
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(request, first_client, winner)
                try:
                    assert ready.wait(10), 'winner did not reach commit'
                    second = pool.submit(request, second_client, 'close' if winner == 'execute' else 'execute')
                    assert waiting.wait(10), 'loser did not attempt the shared write claim'
                finally:
                    release.set()
                a, b = first.result(timeout=15), second.result(timeout=15)
        finally:
            release.set()
            event.remove(factory.kw['bind'], 'before_cursor_execute', claim)
        assert a.status_code == b.status_code == 200, (a.text,b.text)
        assert a.json() == b.json()
        assert a.json()['result'] == ('completed' if winner == 'execute' else 'closed')
        after = state(factory)
        assert after['records'] == before['records'] + 1
        assert sum(row[3] for row in after['lots']) == (130 if winner == 'execute' else 0)
        if winner == 'close':
            assert after['deliveries'] == before['deliveries'] and after['tasks'] == before['tasks']
        assert browser(body, display, response=resolve(first_client, ids['delivery'], body).json())['state'] == ('confirmed' if winner == 'execute' else 'closed')


@pytest.mark.parametrize('operation', ['execute','close'])
def test_map_write_and_audit_failure_roll_back_all_facts(mapped_app, monkeypatch, operation):
    app, factory, ids = mapped_app
    with TestClient(app, raise_server_exceptions=False) as client:
        _login(client, 'admin')
        original, display = preview(client, ids['delivery'])
        body = browser(original, display)['body']
        before = state(factory)
        with factory() as db:
            audits = db.scalar(select(func.count()).select_from(OperationLog))
        real = service.commit_command
        def fail_commit(db):
            # All real writes have been flushed; no ledger/auth/inventory stub.
            assert db.scalar(select(func.count()).select_from(FinanceIdempotencyRecord)) == before['records'] + 1
            raise RuntimeError('synthetic failure before commit')
        monkeypatch.setattr(service, 'commit_command', fail_commit)
        failed = (client.put(f"/api/deliveries/{ids['delivery']}/dispatch", json=body) if operation == 'execute'
            else close_request(client, ids['delivery'], body))
        assert failed.status_code == 500 and state(factory) == before
        with factory() as db:
            assert db.scalar(select(func.count()).select_from(OperationLog)) == audits
        assert resolve(client, ids['delivery'], body).json()['status'] == 'not_recorded'
        monkeypatch.setattr(service, 'commit_command', real)
        succeeded = (client.put(f"/api/deliveries/{ids['delivery']}/dispatch", json=body) if operation == 'execute'
            else close_request(client, ids['delivery'], body))
        assert succeeded.status_code == 200, succeeded.text


@pytest.mark.parametrize('legacy', [False, True])
def test_nonmapped_http_js_control(delivery_api_app, old_reader, monkeypatch, legacy):
    app, factory = delivery_api_app
    from tests.test_phase7_deliveries import _login as login
    with TestClient(app) as client:
        login(client, 'admin')
        did = seed(client, factory)['id']
        with monkeypatch.context() as patch:
            if legacy: patch.setattr(deliveries, 'dispatch_commands', old_reader)
            packet = client.get(f'/api/deliveries/{did}/dispatch-snapshot').json()
        body = dict(expected_actor_id=packet['current_actor_id'], idempotency_key='dispatch-nonmap-control',
            expected_version=packet['snapshot']['delivery']['version'], snapshot_hash=packet['snapshot_hash'],
            snapshot=packet['snapshot'], confirm_pick_exception=False)
        assert body['snapshot']['pick_task'] is None
        wire = browser(body, packet['display'])['body']
        assert service.digest(wire['snapshot']) == body['snapshot_hash']
        result = client.put(f'/api/deliveries/{did}/dispatch', json=wire)
        assert result.status_code == 200, result.text
        assert browser(wire, packet['display'], response=result.json())['state'] == 'confirmed'
        before = state(factory)
        assert close_request(client, did, wire).json() == result.json()
        assert resolve(client, did, wire).json()['status'] == 'completed'
        assert state(factory) == before


@pytest.mark.parametrize('legacy', [False, True])
def test_real_map_drift_still_blocks_execute_but_allows_exact_closure(mapped_app, old_reader, monkeypatch, legacy):
    app, factory, ids = mapped_app
    with TestClient(app) as client:
        _login(client, 'admin')
        with monkeypatch.context() as patch:
            if legacy: patch.setattr(deliveries, 'dispatch_commands', old_reader)
            original, display = preview(client, ids['delivery'])
        body = browser(original, display)['body']
        with factory() as db:
            layout = db.scalar(select(Floor3LocationLayout))
            layout.left_pct = 11
            layout.version += 1
            db.commit()
        before = state(factory)
        rejected = client.put(f"/api/deliveries/{ids['delivery']}/dispatch", json=body)
        assert rejected.status_code == 409 and '实际拿货已变化' in rejected.text, rejected.text
        assert state(factory) == before
        assert resolve(client, ids['delivery'], body).json()['status'] == 'not_recorded'
        closed = close_request(client, ids['delivery'], body)
        assert closed.status_code == 200 and closed.json()['result'] == 'closed'
        assert state(factory)['lots'] == before['lots']


def test_versioned_coordinates_require_exact_finite_decimal_strings(mapped_app):
    app, factory, ids = mapped_app
    with TestClient(app) as client:
        _login(client, 'admin')
        original, _ = preview(client, ids['delivery'])
        before = state(factory)
        for value in [10, True, '10.0', '01', '1e1', '1e-1000000000', '0.00001', 'NaN', 'Infinity', '101', '-1']:
            body = copy.deepcopy(original)
            body['snapshot']['pick_task']['items'][0]['location_lines'][0]['map_point']['left_pct'] = value
            body['snapshot_hash'] = service.digest(body['snapshot'])
            response = close_request(client, ids['delivery'], body)
            assert response.status_code == 409, response.text
            assert response.headers['x-delivery-dispatch-original-invalid'] == '1'
            assert state(factory) == before


@pytest.mark.parametrize('legacy', [False, True])
def test_terminal_map_request_stays_bound_to_payload_actor_and_key(mapped_app, old_reader, monkeypatch, legacy):
    app, factory, ids = mapped_app
    with TestClient(app) as client:
        _login(client, 'admin')
        with monkeypatch.context() as patch:
            if legacy: patch.setattr(deliveries, 'dispatch_commands', old_reader)
            original, display = preview(client, ids['delivery'])
        wire = browser(original, display)['body']
        first = client.put(f"/api/deliveries/{ids['delivery']}/dispatch", json=wire)
        assert first.status_code == 200, first.text
        before = state(factory)
        for mutation in ['quantity', 'customer', 'source', 'actor', 'confirmation']:
            body = copy.deepcopy(original)
            if mutation == 'quantity': body['snapshot']['items'][0]['physical_quantity'] += 1
            elif mutation == 'customer': body['snapshot']['delivery']['customer_id'] += 1
            elif mutation == 'source': body['snapshot']['pick_task']['items'][0]['location_lines'][0]['location_id'] = 999
            elif mutation == 'actor': body['expected_actor_id'] += 1
            else: body['confirm_pick_exception'] = False
            body['snapshot_hash'] = service.digest(body['snapshot'])
            for response in [resolve(client, ids['delivery'], body), close_request(client, ids['delivery'], body),
                client.put(f"/api/deliveries/{ids['delivery']}/dispatch", json=body)]:
                assert response.status_code == 409, response.text
            assert state(factory) == before
        wrong_key = client.post(f"/api/deliveries/{ids['delivery']}/dispatch-results/another-original-key/close",
            json=dict(expected_actor_id=wire['expected_actor_id'],original_request=wire))
        assert wrong_key.status_code == 409 and state(factory) == before
