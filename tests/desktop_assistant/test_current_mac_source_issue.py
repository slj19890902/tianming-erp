"""Synthetic current-Mac authorization source; no live Windows or business IO."""
import importlib.util
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
from unittest.mock import Mock
import zipfile

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from desktop_assistant import current_mac_source_issue as issue
from desktop_assistant import current_mac_cutover_contract as contract
from desktop_assistant import current_mac_cutover as codec
from desktop_assistant.signing import public_bytes
from tests.desktop_assistant.test_current_mac_cutover_contract import valid_body


def test_current_mac_source_entry_exists():
    assert importlib.util.find_spec('desktop_assistant.current_mac_source_issue') is not None


def digest(raw): return hashlib.sha256(raw).hexdigest()


@pytest.fixture
def case(tmp_path, monkeypatch):
    root = tmp_path / 'source'
    for name in ('releases', 'shared/data', 'shared/uploads', 'control/backup-receipts', 'backups', 'packages', 'staging'):
        (root / name).mkdir(parents=True)
    (root / 'control/operation.lock').write_bytes(b'0')
    output = tmp_path / 'output'; output.mkdir()
    identity = tmp_path / 'publisher.json'; identity.write_bytes(b'synthetic protected identity')
    key = Ed25519PrivateKey.generate(); public = public_bytes(key)
    program = {'main.py': b'print("synthetic old Windows only")\n', 'runtime/python.exe': b'not executed'}
    manifest = {'type': 'tianming.release.v1', 'version': 'v1', 'revision': 'old-win-revision',
        'runtime_platform': 'windows', 'files': {name: digest(raw) for name, raw in program.items()}}
    raw = json.dumps(manifest, indent=2).encode()
    candidate = tmp_path / 'program.zip'
    with zipfile.ZipFile(candidate, 'w') as archive:
        for name, value in program.items(): archive.writestr(name, value)
        archive.writestr('manifest.json', raw); archive.writestr('manifest.sig', key.sign(raw))
    package_sha = digest(candidate.read_bytes()); package = root / 'packages' / (package_sha + '.zip')
    candidate.rename(package); release = root / 'releases' / package_sha; release.mkdir()
    (release / 'manifest.json').write_bytes(raw)
    state = {'current': package_sha, 'manual_stop': True, 'operation': 'restore',
        'last_backup': 'Z:/historical/not-read.tmbackup', 'native_note': {'float': 1.25}}
    state_raw = json.dumps(state, indent=2).encode(); (root / 'state.json').write_bytes(state_raw)
    database = root / 'shared/data/carton_erp.sqlite3'
    with sqlite3.connect(database) as db:
        db.execute('create table old_facts (id integer primary key, value text)')
        db.execute('insert into old_facts values (1, ?)', ('old Windows business unlike today Mac',))
    (root / 'shared/environment.json').write_bytes(b'{"ERP_ENVIRONMENT":"production","ERP_BIND_HOST":"old-host"}')
    (root / 'shared/uploads/complete.pdf').write_bytes(b'complete synthetic original attachment')
    (root / 'preferences.json').write_bytes(b'{"protected_password":"synthetic-DPAPI-only"}')
    process = {'pid': 2147000000, 'created': 1700000000.25, 'exe': str(release / 'runtime/python.exe'), 'nonce': '7' * 32}
    (root / 'control/process.json').write_text(json.dumps(process))
    body = valid_body(contract); body['preparation']['publisher_sha256'] = digest(public)
    body['request']['publisher_sha256'] = digest(public)
    body['request']['preparation_sha256'] = contract.digest(body['preparation'])
    request_raw = json.dumps(body['request'], indent=2).encode()
    prep_raw = json.dumps(body['preparation'], indent=2).encode()
    clock = [datetime(2026, 10, 12, 2, 0, 10, tzinfo=timezone.utc)]
    monkeypatch.setattr(issue, '_current', lambda: clock[0])
    monkeypatch.setattr(issue.original, '_require_windows', lambda: None)
    process_check = Mock(return_value=None)
    monkeypatch.setattr(issue.original._SourceReader, '_process', process_check)
    idle = Mock(); monkeypatch.setattr(issue.original, 'assert_idle', idle)
    active, events = {}, []
    @contextmanager
    def lease(path):
        path = Path(path); assert path.is_file()
        active[path] = active.get(path, 0) + 1; events.append(('pin', path))
        try: yield
        finally:
            events.append(('release', path)); active[path] -= 1
            if not active[path]: del active[path]
    monkeypatch.setattr(issue, 'read_file_lease', lease)
    @contextmanager
    def directory(path):
        assert path == output
        def check(): events.append('output-acl')
        check(); yield check; check()
    monkeypatch.setattr(issue, 'private_directory', directory)
    def load(path, actual_public):
        assert path == identity and actual_public == public
        assert {database, root / 'state.json', package, identity} <= active.keys()
        with pytest.raises(ValueError):
            with issue.operation_lock(root / 'control/operation.lock', existing_only=True):
                pytest.fail('maintenance lock must remain held')
        events.append('load-key'); return key
    load_key = Mock(side_effect=load); monkeypatch.setattr(issue, 'load_dpapi_key', load_key)
    forbidden = []
    for module, names in ((issue.original.credentials, ('collect', 'unseal', 'seal')),
        (issue.original, ('archive_synced', 'decrypt_file', 'database_info', 'validate_database_schema')),
        (issue.original._SourceReader, ('stop', 'start'))):
        for name in names:
            mock = Mock(side_effect=AssertionError('source v3 cannot access historical backup, operational secrets, SQL or service actions'))
            monkeypatch.setattr(module, name, mock); forbidden.append(mock)
    kwargs = dict(root=root, request_bytes=request_raw, request_sha256=digest(request_raw),
        preparation_bytes=prep_raw, preparation_sha256=digest(prep_raw), public=public,
        publisher_sha256=digest(public), identity=identity, output_directory=output,
        authorize_cutover=True, confirm_source_isolated=True)
    return SimpleNamespace(root=root, output=output, identity=identity, key=key, public=public, body=body,
        package=package, release=release, manifest=manifest, database=database, state=state, state_raw=state_raw,
        process=process, kwargs=kwargs, active=active, events=events, lease=lease, directory=directory,
        process_check=process_check, idle=idle, load_key=load_key, forbidden=forbidden, clock=clock)


def source_bytes(c):
    return {str(path.relative_to(c.root)): path.read_bytes() for path in c.root.rglob('*') if path.is_file()}


def test_real_synthetic_signed_source_approves_exact_mac_request_without_business_equality(case, monkeypatch):
    c = case; before = source_bytes(c)
    monkeypatch.setattr(sqlite3, 'connect', Mock(side_effect=AssertionError('no business SQL')))
    receipt = issue.issue(**c.kwargs)
    raw = Path(receipt['authorization_path']).read_bytes()
    body = codec.verify_authorization(raw, c.public, c.body['request'])
    evidence = body['source_stop']
    assert evidence['database_sha256'] != body['preparation']['target_snapshot']['database_sha256']
    assert evidence['source_package_sha256'] == digest(c.package.read_bytes())
    assert evidence['request_sha256'] == contract.digest(c.body['request'])
    assert receipt['request_file_sha256'] != receipt['request_sha256']
    assert receipt['mac_business_audited_by_source'] is receipt['physical_isolation_proved'] is False
    assert body['preparation'] == c.body['preparation'] and before == source_bytes(c)
    assert set(evidence) == contract.SOURCE_STOP_FIELDS and not c.active
    c.load_key.assert_called_once()
    assert c.idle.call_count >= 8 and c.process_check.call_count >= 8
    for mock in c.forbidden: mock.assert_not_called()
    assert 'old Windows business' not in raw.decode() and 'synthetic-DPAPI' not in raw.decode()


def test_audit_recheck_is_active_only_and_preserves_raw_bytes(case):
    c = case
    with issue.verified_stopped_source(c.root, c.public, c.body['request']) as (evidence, recheck):
        assert evidence['state_sha256'] == digest(c.state_raw)
        assert evidence == recheck() and evidence is not recheck()
        assert c.database in c.active and c.root / 'state.json' in c.active
        assert c.root / 'shared/uploads/complete.pdf' in c.active
    with pytest.raises(ValueError): recheck()
    c.load_key.assert_not_called()


@pytest.mark.parametrize('fault', ['manual_false', 'manual_int', 'failed', 'onboarding', 'backup_error', 'work_retained',
    'pending', 'cas_pending', 'pending_directory', 'nan', 'infinite', 'duplicate', 'process_duplicate', 'process_nonce',
    'process_pid_bool', 'wal', 'shm', 'journal', 'package', 'manifest', 'platform', 'running', 'denied', 'referenced'])
def test_invalid_source_refused_before_key_access(case, monkeypatch, fault):
    c = case; state = deepcopy(c.state)
    fields = {'manual_false': ('manual_stop', False), 'manual_int': ('manual_stop', 1),
        'failed': ('operation', 'update_failed'), 'onboarding': ('onboarding_pending', True),
        'backup_error': ('backup_error', 'old unresolved error'), 'work_retained': ('backup_work_retained', True)}
    if fault in fields:
        state[fields[fault][0]] = fields[fault][1]; (c.root / 'state.json').write_text(json.dumps(state))
    elif fault in {'pending', 'cas_pending', 'pending_directory'}:
        path = c.root / ('state.json.pending' if fault == 'pending' else 'state-' + 'a' * 32 + '.pending.json')
        if fault == 'pending_directory': path.mkdir()
        else: path.write_bytes(b'preserve')
    elif fault in {'nan', 'infinite', 'duplicate'}:
        insertion = '"x":NaN,' if fault == 'nan' else '"x":1e999,' if fault == 'infinite' else '"current":"duplicate",'
        (c.root / 'state.json').write_bytes(b'{' + insertion.encode() + c.state_raw.lstrip()[1:])
    elif fault.startswith('process_'):
        value = deepcopy(c.process)
        if fault == 'process_duplicate':
            (c.root / 'control/process.json').write_text('{"pid":1,' + json.dumps(value)[1:])
        else:
            value['nonce' if fault == 'process_nonce' else 'pid'] = '../bad' if fault == 'process_nonce' else True
            (c.root / 'control/process.json').write_text(json.dumps(value))
    elif fault in {'wal', 'shm', 'journal'}: Path(str(c.database) + '-' + fault).write_bytes(b'')
    elif fault == 'package': c.package.write_bytes(c.package.read_bytes() + b'tamper')
    elif fault == 'manifest': (c.release / 'manifest.json').write_text('{}')
    elif fault == 'platform': monkeypatch.setattr(issue, 'inspect_package', lambda *a: (b'', {**c.manifest, 'runtime_platform': 'macos-arm64'}))
    elif fault == 'running': c.process_check.return_value = object()
    elif fault == 'denied': c.process_check.side_effect = PermissionError('private process detail')
    else: c.idle.side_effect = ValueError('still referenced')
    before = source_bytes(c)
    with pytest.raises((ValueError, OSError)): issue.issue(**c.kwargs)
    assert before == source_bytes(c) and not list(c.output.iterdir())
    c.load_key.assert_not_called()


@pytest.mark.parametrize('fault', ['state', 'database', 'file', 'new_file', 'environment', 'config', 'process', 'process_missing',
    'new_process', 'package', 'manifest', 'pending', 'nan', 'clock', 'expiry', 'evidence'])
def test_recheck_rejects_source_change_and_preserves_scene(case, fault):
    c = case
    if fault == 'new_process': (c.root / 'control/process.json').unlink()
    with pytest.raises(ValueError):
        with issue.verified_stopped_source(c.root, c.public, c.body['request']) as (evidence, recheck):
            if fault == 'state': (c.root / 'state.json').write_bytes(json.dumps(c.state, separators=(',', ':')).encode())
            elif fault == 'database': c.database.write_bytes(c.database.read_bytes() + b'raw changed')
            elif fault == 'file': (c.root / 'shared/uploads/complete.pdf').write_bytes(b'changed')
            elif fault == 'new_file': (c.root / 'shared/uploads/new.pdf').write_bytes(b'new')
            elif fault == 'environment': (c.root / 'shared/environment.json').write_bytes(b'{}')
            elif fault == 'config': (c.root / 'preferences.json').write_bytes(b'{}')
            elif fault == 'process': (c.root / 'control/process.json').write_text(json.dumps({**c.process, 'nonce': '9'*32}))
            elif fault == 'process_missing': (c.root / 'control/process.json').unlink()
            elif fault == 'new_process': (c.root / 'control/process.json').write_text(json.dumps(c.process))
            elif fault == 'package': c.package.write_bytes(c.package.read_bytes() + b'changed')
            elif fault == 'manifest': (c.release / 'manifest.json').write_bytes(b'{}')
            elif fault == 'pending': (c.root / 'state.json.pending').write_bytes(b'{}')
            elif fault == 'nan': (c.root / 'state.json').write_bytes(b'{"bad":NaN}')
            elif fault == 'clock': c.clock[0] -= timedelta(seconds=1)
            elif fault == 'expiry': c.clock[0] += timedelta(days=8)
            else: evidence['state_sha256'] = 'f' * 64
            recheck()
    c.load_key.assert_not_called()


@pytest.mark.parametrize('field', ['request_sha256', 'preparation_sha256', 'publisher_sha256', 'authorize_cutover', 'confirm_source_isolated'])
def test_request_approval_failure_never_loads_key(case, field):
    c = case; kwargs = dict(c.kwargs); kwargs[field] = False if field.endswith(('cutover', 'isolated')) else 'f' * 64
    with pytest.raises(ValueError): issue.issue(**kwargs)
    c.load_key.assert_not_called(); assert not list(c.output.iterdir())


def test_mismatched_mac_preparation_is_not_signed(case):
    c = case; prep = deepcopy(c.body['preparation']); prep['state_sha256'] = 'f' * 64
    raw = contract.canonical(prep); kwargs = {**c.kwargs, 'preparation_bytes': raw, 'preparation_sha256': digest(raw)}
    with pytest.raises(ValueError): issue.issue(**kwargs)
    c.load_key.assert_not_called()


def test_existing_output_refuses_resign_without_touching_identity(case):
    c = case; path = c.output / ('cutover-current-mac-' + c.body['request']['request_id'] + '.json')
    path.write_bytes(b'preserve original')
    with pytest.raises(ValueError): issue.issue(**c.kwargs)
    c.load_key.assert_not_called(); assert path.read_bytes() == b'preserve original'


@pytest.mark.parametrize('point', ['load', 'sign', 'readback', 'source_exit', 'output_final'])
def test_failures_during_signing_keep_leases_and_preserve_any_output(case, monkeypatch, point):
    c = case; original_load = c.load_key.side_effect
    if point == 'load':
        def load(*args):
            result = original_load(*args); (c.root / 'state.json.pending').write_bytes(b'pending'); return result
        c.load_key.side_effect = load
    elif point == 'sign':
        real_sign = codec.sign_authorization
        def sign(*args, **kw):
            result = real_sign(*args, **kw); (c.root / 'state.json.pending').write_bytes(b'pending'); return result
        monkeypatch.setattr(codec, 'sign_authorization', sign)
    elif point == 'readback':
        real_read = issue._read
        def read(path, limit):
            if Path(path).parent == c.output: raise ValueError('private failure detail')
            return real_read(path, limit)
        monkeypatch.setattr(issue, '_read', read)
    elif point == 'source_exit':
        real_source = issue.verified_stopped_source
        @contextmanager
        def source(*args):
            with real_source(*args) as value:
                yield value
                assert c.identity in c.active and any(path.parent == c.output for path in c.active)
                (c.root / 'state.json.pending').write_bytes(b'pending')
        monkeypatch.setattr(issue, 'verified_stopped_source', source)
    else:
        @contextmanager
        def directory(path):
            yield lambda: None
            assert c.identity in c.active and any(item.parent == c.output for item in c.active)
            raise ValueError('final output DACL failure')
        monkeypatch.setattr(issue, 'private_directory', directory)
    with pytest.raises(ValueError): issue.issue(**c.kwargs)
    assert not c.active
    if point in {'readback', 'source_exit', 'output_final'}:
        paths = list(c.output.iterdir()); assert len(paths) == 1
        assert codec.verify_authorization(paths[0].read_bytes(), c.public, c.body['request'])


def test_missing_process_is_explicit_null_and_no_nas_or_secret_input_needed(case):
    c = case; (c.root / 'control/process.json').unlink()
    receipt = issue.issue(**c.kwargs)
    body = codec.verify_authorization(Path(receipt['authorization_path']).read_bytes(), c.public, c.body['request'])
    assert body['source_stop']['process_record_sha256'] is None
    help_text = issue.parser().format_help()
    assert '--password' not in help_text and '--backup' not in help_text and '--credentials' not in help_text


def test_original_platform_guard_never_creates_files_on_mac(tmp_path):
    if sys.platform == 'win32': pytest.skip('Non-Windows rejection applies on other hosts')
    with pytest.raises(ValueError):
        with issue.verified_stopped_source(tmp_path, b'not read', {}): pytest.fail('not Windows')
    assert list(tmp_path.iterdir()) == []


def test_shared_file_lease_failure_prevents_key_access_and_keeps_all_files(case, monkeypatch):
    c = case; before = source_bytes(c)
    @contextmanager
    def lease(path):
        if Path(path) == c.root / 'shared/uploads/complete.pdf': raise OSError('synthetic existing attachment writer')
        with c.lease(path): yield
    monkeypatch.setattr(issue, 'read_file_lease', lease)
    with pytest.raises(OSError): issue.issue(**c.kwargs)
    assert source_bytes(c) == before and not c.active
    c.load_key.assert_not_called()


def test_full_binding_recheck_before_dpapi_prevents_expired_source(case, monkeypatch):
    c = case; real = issue.verified_stopped_source
    @contextmanager
    def audit(*args):
        with real(*args) as (evidence, recheck):
            c.clock[0] = contract._time(c.body['request']['expires_at'])
            yield evidence, recheck
    monkeypatch.setattr(issue, 'verified_stopped_source', audit)
    with pytest.raises(ValueError): issue.issue(**c.kwargs)
    c.load_key.assert_not_called()


def test_exclusive_create_race_retains_competing_output(case, monkeypatch):
    c = case; real_open = issue.os.open; destination = c.output / ('cutover-current-mac-' + c.body['request']['request_id'] + '.json')
    def opening(path, flags, *args, **kwargs):
        if Path(path) == destination and flags & os.O_EXCL:
            destination.write_bytes(b'preserve concurrent output')
        return real_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(issue.os, 'open', opening)
    with pytest.raises(FileExistsError): issue.issue(**c.kwargs)
    assert destination.read_bytes() == b'preserve concurrent output' and not c.active


def test_real_dpapi_loader_receives_only_original_leased_identity(case, monkeypatch):
    from desktop_assistant import signing
    from cryptography.hazmat.primitives import serialization
    c = case
    # Real restricted loader with a synthetic DPAPI adapter, not native DPAPI.
    private_raw = c.key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                    serialization.NoEncryption())
    c.identity.write_text(json.dumps({'type': 'tianming.publisher.dpapi.v1', 'public_key': c.public.decode(),
        'fingerprint': digest(c.public), 'protected_key': 'opaque-synthetic-protected'}))
    unprotect = Mock(side_effect=lambda value: private_raw.hex() if value == 'opaque-synthetic-protected' else None)
    monkeypatch.setattr(signing, 'unprotect', unprotect)
    original_load = signing.load_dpapi_key
    def load(path, public):
        assert path in c.active and c.database in c.active
        with monkeypatch.context() as nested:
            nested.setattr(signing.sys, 'platform', 'win32')
            return original_load(path, public)
    monkeypatch.setattr(issue, 'load_dpapi_key', load)
    receipt = issue.issue(**c.kwargs)
    assert codec.verify_authorization(Path(receipt['authorization_path']).read_bytes(), c.public, c.body['request'])
    unprotect.assert_called_once_with('opaque-synthetic-protected')


def test_run_holds_public_file_leases_for_the_complete_issue(case, monkeypatch):
    c = case
    request = c.output.parent / 'request.json'; request.write_bytes(c.kwargs['request_bytes'])
    prep = c.output.parent / 'preparation.json'; prep.write_bytes(c.kwargs['preparation_bytes'])
    public = c.output.parent / 'public.pem'; public.write_bytes(c.public)
    real_issue = issue.issue
    def checked_issue(**kwargs):
        assert {request, prep, public} <= c.active.keys()
        result = real_issue(**kwargs)
        assert {request, prep, public} <= c.active.keys()
        return result
    monkeypatch.setattr(issue, 'issue', checked_issue)
    args = SimpleNamespace(root=c.root, request=request, preparation=prep, public_key=public,
        request_sha256=c.kwargs['request_sha256'], preparation_sha256=c.kwargs['preparation_sha256'],
        publisher_sha256=c.kwargs['publisher_sha256'], identity=c.identity, output_directory=c.output,
        authorize_cutover=True, confirm_source_isolated=True)
    assert issue.run(args)['mac_business_audited_by_source'] is False
    assert not c.active


def test_cli_failure_is_neutral_and_does_not_leak_exception(monkeypatch, capsys):
    monkeypatch.setattr(issue, 'parser', lambda: SimpleNamespace(parse_args=lambda: None))
    monkeypatch.setattr(issue, 'run', Mock(side_effect=ValueError('fake password secret')))
    with pytest.raises(SystemExit) as error: issue.main()
    assert str(error.value) == issue.FAILURE and 'fake password' not in str(error.value)
    assert '已经停止' not in str(error.value) and capsys.readouterr().out == ''


@pytest.mark.parametrize('bad', ['--password', '--backup', '--credentials', '--target-package', '--private-key'])
def test_cli_cannot_accept_unneeded_secrets_or_old_protocol_inputs(bad, capsys):
    with pytest.raises(SystemExit) as error: issue.parser().parse_args([bad, 'not-a-real-secret'])
    assert 'not-a-real-secret' not in str(error.value)
    assert 'not-a-real-secret' not in capsys.readouterr().err


def test_cli_requires_complete_authorization_flag_spelling():
    args = []
    for name in ('root', 'request', 'preparation', 'public-key', 'identity', 'output-directory',
                 'request-sha256', 'preparation-sha256', 'publisher-sha256'):
        args += ['--' + name, 'synthetic-public-value']
    args += ['--authorize-cut', '--confirm-source-isolated']
    with pytest.raises(SystemExit): issue.parser().parse_args(args)


def test_native_windows_read_lease_blocks_writer_and_rename_only_on_synthetic_file(tmp_path):
    if sys.platform != 'win32': pytest.skip('Native Windows file sharing; never simulated as passed on Mac')
    from desktop_assistant.windows_private_workspace import read_file_lease
    path = tmp_path / 'synthetic-frozen.bin'; path.write_bytes(b'unchanged')
    with read_file_lease(path):
        with pytest.raises(OSError): path.open('wb')
        with pytest.raises(OSError): path.rename(tmp_path / 'renamed.bin')
        assert path.read_bytes() == b'unchanged'
    assert path.read_bytes() == b'unchanged'


def test_full_large_shared_inventory_is_hashed_without_public_record_size_limit(case):
    c = case
    for index in range(800):
        (c.root / 'shared/uploads' / ('完整原附件-' + str(index).zfill(4) + '.pdf')).write_bytes(str(index).encode())
    complete = issue.files(c.root / 'shared')
    raw_inventory = issue.original.protocol.canonical(complete)
    assert len(raw_inventory) > contract.LIMIT
    receipt = issue.issue(**c.kwargs)
    raw = Path(receipt['authorization_path']).read_bytes()
    body = codec.verify_authorization(raw, c.public, c.body['request'])
    assert body['source_stop']['files_sha256'] == digest(raw_inventory)
    assert len(raw) <= contract.LIMIT
    assert sum(1 for action in c.events if isinstance(action, tuple) and action[0] == 'pin'
               and action[1].parent == c.root / 'shared/uploads') == 801
