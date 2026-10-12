"""Temporary Ed25519/files/locks; native Windows/DACL/DPAPI are explicit fakes."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import getpass
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import warnings

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from desktop_assistant import retained_source_issue as issuer
from desktop_assistant import retained_snapshot_contract as contract
from desktop_assistant import retained_cutover as codec
from desktop_assistant.operation_lock import operation_lock
from desktop_assistant.signing import public_bytes
from tests.desktop_assistant.test_retained_cutover import valid_body
from tests.desktop_assistant.test_retained_source_audit import case as retained_case, original_case

PASSWORD = 'synthetic-transfer-password-not-backup-password'
NOW = datetime(2026, 10, 12, 2, 0, 4, tzinfo=timezone.utc)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


@pytest.fixture
def case(tmp_path, monkeypatch):
    root = tmp_path / 'source'; (root / 'control').mkdir(parents=True)
    (root / 'control/operation.lock').write_bytes(b'0')
    (root / 'state.json').write_bytes(b'preserved synthetic source state')
    output = tmp_path / 'output'; output.mkdir()
    private = tmp_path / 'private'; private.mkdir()
    paths = {name: tmp_path / name for name in ('backup.tmbackup', 'credentials.tmencrypted', 'target.zip', 'publisher.json')}
    for name, path in paths.items(): path.write_bytes(('synthetic ' + name).encode())
    key = Ed25519PrivateKey.generate(); public = public_bytes(key)
    body = valid_body(contract); body['request']['publisher_sha256'] = digest(public)
    request_raw = json.dumps(body['request'], indent=2).encode()
    preparation_raw = json.dumps(body['preparation'], indent=2).encode()
    events = []; checks = []; active = set()

    @contextmanager
    def directory(path):
        assert path == output
        def check(): events.append('output-acl')
        check(); yield check; check()

    @contextmanager
    def lease(path):
        active.add(path); events.append(('lease', path))
        try: yield
        finally: active.remove(path); events.append(('release', path))

    def check():
        with pytest.raises(ValueError, match='另一个'):
            with operation_lock(root / 'control/operation.lock', existing_only=True):
                pytest.fail('original source lock must stay held')
        events.append('recheck'); checks.append(len(checks) + 1)
        return deepcopy(body['source'])

    @contextmanager
    def audit(*args):
        assert args == (root, paths['backup.tmbackup'], body['request']['retained_snapshot']['backup_sha256'],
            paths['credentials.tmencrypted'], PASSWORD, body['request'], paths['target.zip'], public, private)
        events.append('audit-enter')
        with operation_lock(root / 'control/operation.lock', existing_only=True):
            events.append('private-final-checked')  # Explicit synthetic DACL adapter.
            yield deepcopy(body['source']), check
            events.append('audit-exit')

    def load(path, trusted):
        assert path == paths['publisher.json'] and trusted == public
        assert path in active and 'private-final-checked' in events and checks
        assert events[-1] == 'output-acl'
        events.append('key-loaded')
        return key

    monkeypatch.setattr(issuer, '_windows', lambda: None)
    monkeypatch.setattr(issuer, '_current', lambda: NOW)
    monkeypatch.setattr(issuer, 'private_directory', directory)
    monkeypatch.setattr(issuer, 'read_file_lease', lease)
    monkeypatch.setattr(issuer, '_audit', audit)
    monkeypatch.setattr(issuer, 'load_dpapi_key', load)
    arguments = dict(root=root, backup=paths['backup.tmbackup'], credentials_path=paths['credentials.tmencrypted'],
        transfer_password=PASSWORD, request_bytes=request_raw, request_sha256=digest(request_raw),
        preparation_bytes=preparation_raw, preparation_sha256=digest(preparation_raw),
        target_package=paths['target.zip'], public=public, publisher_sha256=digest(public),
        identity=paths['publisher.json'], private_parent=private, output_directory=output,
        authorize_cutover=True, confirm_source_isolated=True)
    return SimpleNamespace(root=root, output=output, paths=paths, args=arguments, body=body,
        events=events, checks=checks, active=active, key=key, public=public, audit=audit,
        destination=output / ('cutover-retained-' + body['request']['request_id'] + '.json'))


def test_audit_and_rechecks_precede_original_key_load_and_hold_until_readback(case):
    before = {p.name: p.read_bytes() for p in case.root.rglob('*') if p.is_file()}
    receipt = issuer.issue(**case.args)
    raw = case.destination.read_bytes()
    body = codec.verify_authorization(raw, case.public, case.body['request'])
    assert body['source'] == case.body['source'] and body['preparation'] == case.body['preparation']
    assert receipt['authorization_sha256'] == digest(raw)
    assert receipt['preparation_file_sha256'] == case.args['preparation_sha256']
    assert receipt['preparation_sha256'] == contract.digest(case.body['preparation'])
    assert receipt['physical_isolation_proved'] is False
    assert receipt['source_started'] is False and receipt['target_activated'] is False
    assert case.events.count('key-loaded') == 1 and len(case.checks) >= 5
    assert case.events.index('private-final-checked') < case.events.index('key-loaded')
    assert case.events.index('key-loaded') < case.events.index('audit-exit')
    assert before == {p.name:p.read_bytes() for p in case.root.rglob('*') if p.is_file()}
    assert PASSWORD not in raw.decode() + json.dumps(receipt)


@pytest.mark.parametrize('field,value', [
    ('authorize_cutover', False), ('authorize_cutover', 1), ('confirm_source_isolated', False),
    ('confirm_source_isolated', 1), ('request_sha256', '0'*64), ('preparation_sha256', '0'*64),
    ('publisher_sha256', '0'*64), ('request_bytes', b'{}'), ('preparation_bytes', b'{}'),
    ('root', Path('.')), ('identity', Path('relative')), ('credentials_path', Path('relative')),
    ('output_directory', Path('relative')), ('private_parent', Path('relative')),
])
def test_unapproved_or_relative_inputs_refuse_before_source_audit(case, field, value):
    with pytest.raises(ValueError): issuer.issue(**(case.args | {field:value}))
    assert not case.events and not case.destination.exists()


@pytest.mark.parametrize('field', ['request_bytes', 'preparation_bytes'])
def test_exact_approved_bytes_and_strict_json_both_required(case, field):
    args = dict(case.args); original = args[field]
    args[field] = original + b' '
    with pytest.raises(ValueError): issuer.issue(**args)
    value = json.loads(original); changed = b'{"type":"duplicate",' + original.lstrip()[1:]
    args[field] = changed; args[field.replace('_bytes', '_sha256')] = digest(changed)
    with pytest.raises(ValueError): issuer.issue(**args)
    assert not case.events


def test_preparation_canonical_binding_is_separate_from_approved_file_sha(case):
    prep = deepcopy(case.body['preparation']); prep['restore']['context_sha256'] = '0'*64
    raw = contract.canonical(prep)
    with pytest.raises(ValueError): issuer.issue(**(case.args | {'preparation_bytes':raw,'preparation_sha256':digest(raw)}))
    assert not case.events


@pytest.mark.parametrize('kind', ['regular', 'dangling-link'])
def test_previous_output_is_never_reused_or_resigned(case, kind):
    if kind == 'regular': case.destination.write_bytes(b'preserve original artifact')
    else: case.destination.symlink_to(case.output / 'missing-target')
    with pytest.raises(ValueError): issuer.issue(**case.args)
    assert 'audit-enter' not in case.events and 'key-loaded' not in case.events
    assert os.path.lexists(case.destination)


def test_output_cannot_be_inside_source(case):
    with pytest.raises(ValueError): issuer.issue(**(case.args | {'output_directory':case.root/'control'}))
    assert not case.events


@pytest.mark.parametrize('when', ['before-yield', 'first-recheck', 'after-load', 'after-sign', 'after-write', 'exit'])
def test_source_change_at_every_signing_boundary_fails_and_preserves_scene(case, monkeypatch, when):
    @contextmanager
    def context(*args):
        with case.audit(*args) as (evidence, check):
            if when == 'before-yield': raise ValueError('synthetic-secret-private-final-acl')
            counter = 0
            def recheck():
                nonlocal counter
                counter += 1
                check()
                reached = (when == 'first-recheck' or
                    when == 'after-load' and 'key-loaded' in case.events or
                    when == 'after-sign' and 'signed' in case.events or
                    when == 'after-write' and case.destination.exists())
                if reached: raise ValueError('synthetic-secret-source-drift')
                return evidence
            yield evidence, recheck
            if when == 'exit': raise ValueError('synthetic-secret-final-source-failure')
    real_sign = codec.sign_authorization
    def sign(*args, **kwargs):
        result = real_sign(*args, **kwargs); case.events.append('signed'); return result
    monkeypatch.setattr(issuer, '_audit', context)
    monkeypatch.setattr(codec, 'sign_authorization', sign)
    with pytest.raises(ValueError): issuer.issue(**case.args)
    assert case.destination.exists() is (when in {'after-write', 'exit'})
    if when in {'before-yield', 'first-recheck'}: assert 'key-loaded' not in case.events


def test_recheck_cannot_silently_replace_audited_evidence(case, monkeypatch):
    @contextmanager
    def context(*args):
        with case.audit(*args) as (evidence, check):
            def changed():
                check(); return evidence | {'source_sha256':'0'*64}
            yield evidence, changed
    monkeypatch.setattr(issuer, '_audit', context)
    with pytest.raises(ValueError): issuer.issue(**case.args)
    assert 'key-loaded' not in case.events and not case.destination.exists()


def test_wrong_but_validly_signed_preparation_or_source_is_not_published(case, monkeypatch):
    real = codec.sign_authorization
    def wrong(request, source, preparation, key, **kwargs):
        return real(request, source | {'source_sha256':'0'*64}, preparation, key, **kwargs)
    monkeypatch.setattr(codec, 'sign_authorization', wrong)
    with pytest.raises(ValueError): issuer.issue(**case.args)
    assert not case.destination.exists()


@pytest.mark.parametrize('failure', ['fsync', 'readback', 'output-acl', 'late-window', 'write-race'])
def test_publication_failure_keeps_possible_valid_output(case, monkeypatch, failure):
    if failure == 'fsync':
        monkeypatch.setattr(issuer.os, 'fsync', lambda *_: (_ for _ in ()).throw(OSError('synthetic fsync')))
    elif failure == 'readback': monkeypatch.setattr(issuer, '_read', lambda *_: b'wrong readback')
    elif failure == 'output-acl':
        @contextmanager
        def directory(path):
            def check():
                case.events.append('output-acl')
                if case.destination.exists(): raise ValueError('output ACL changed')
            yield check
        monkeypatch.setattr(issuer, 'private_directory', directory)
    elif failure == 'late-window':
        monkeypatch.setattr(issuer, '_current', lambda: NOW + timedelta(hours=9) if case.destination.exists() else NOW)
    else:
        real = issuer.os.open
        def race(path, flags, *args, **kwargs):
            if Path(path) == case.destination and flags & os.O_CREAT:
                case.destination.write_bytes(b'competing artifact')
            return real(path, flags, *args, **kwargs)
        monkeypatch.setattr(issuer.os, 'open', race)
    with pytest.raises((ValueError,OSError)): issuer.issue(**case.args)
    assert case.destination.exists()
    if failure == 'write-race': assert case.destination.read_bytes() == b'competing artifact'


def cli_args(case):
    folder = case.root.parent
    for name, raw in [('public.pem',case.public),('request.json',case.args['request_bytes']),('preparation.json',case.args['preparation_bytes'])]:
        (folder/name).write_bytes(raw)
    return SimpleNamespace(root=case.args['root'], backup=case.args['backup'], credentials=case.args['credentials_path'],
        request=folder/'request.json', preparation=folder/'preparation.json', public_key=folder/'public.pem',
        request_sha256=case.args['request_sha256'], preparation_sha256=case.args['preparation_sha256'],
        publisher_sha256=case.args['publisher_sha256'], target_package=case.args['target_package'],
        identity=case.args['identity'], private_parent=case.args['private_parent'], output_directory=case.output,
        authorize_cutover=True, confirm_source_isolated=True)


def test_hidden_transfer_prompt_keeps_three_public_input_leases(case, monkeypatch):
    args = cli_args(case); terminals = []
    monkeypatch.setattr(issuer, '_terminal', lambda: terminals.append('console'))
    def prompt(message):
        assert '移交' in message and '备份口令' not in message
        assert {args.request,args.preparation,args.public_key} <= case.active
        return PASSWORD
    result = issuer.run(args, prompt=prompt)
    assert result['source_started'] is False and len(terminals) >= 2
    assert not case.active


def test_getpass_warning_never_falls_back_or_retries(case, monkeypatch):
    args = cli_args(case); calls = []
    monkeypatch.setattr(issuer, '_terminal', lambda: None)
    def prompt(_):
        calls.append(1); warnings.warn('no hidden console', getpass.GetPassWarning)
        pytest.fail('echoed fallback must not happen')
    with pytest.raises(getpass.GetPassWarning): issuer.run(args, prompt=prompt)
    assert calls == [1] and 'audit-enter' not in case.events


def test_parser_has_no_secret_clock_or_evidence_override(capsys):
    parser = issuer.parser(); help_text = parser.format_help()
    for name in ('--password','--transfer-password','--now','--evidence','--private-key'):
        assert name not in help_text
        with pytest.raises(SystemExit) as error: parser.parse_args([name,'synthetic-secret-never-echo'])
        assert 'synthetic-secret' not in str(error.value) + capsys.readouterr().err


def test_main_errors_never_print_secrets_and_state_possible_valid_output(monkeypatch, capsys):
    monkeypatch.setattr(issuer, 'parser', lambda: SimpleNamespace(parse_args=lambda: object()))
    def bad(_): raise ValueError('synthetic-secret-native-failure')
    monkeypatch.setattr(issuer, 'run', bad)
    with pytest.raises(SystemExit) as error: issuer.main()
    assert '可能已经包含有效' in str(error.value)
    assert 'synthetic-secret' not in str(error.value) + capsys.readouterr().out


def test_complete_source_binding_is_checked_before_key_load(case):
    case.body['source']['backup_sha256'] = '0'*64
    with pytest.raises(ValueError): issuer.issue(**case.args)
    assert 'key-loaded' not in case.events


def test_output_read_lease_is_held_through_source_context_exit(case, monkeypatch):
    @contextmanager
    def context(*args):
        with case.audit(*args) as result:
            yield result
            assert case.destination in case.active
            assert case.paths['publisher.json'] in case.active
    monkeypatch.setattr(issuer, '_audit', context)
    issuer.issue(**case.args)


def test_late_output_change_cannot_claim_confirmed_completion(case, monkeypatch):
    @contextmanager
    def context(*args):
        with case.audit(*args) as (evidence, check):
            counter = 0
            def recheck():
                nonlocal counter
                counter += 1; check()
                if counter == 6:
                    # Simulates a failed native protection adapter; no real ACL claim.
                    case.destination.write_bytes(b'late replacement remains preserved')
                return evidence
            yield evidence, recheck
    monkeypatch.setattr(issuer, '_audit', context)
    with pytest.raises(ValueError): issuer.issue(**case.args)
    assert case.destination.read_bytes() == b'late replacement remains preserved'


@pytest.mark.parametrize('password', ['x'*11, 'x'*1025, None, b'not-a-string'])
def test_transfer_password_uses_original_envelope_limits_before_audit(case, password):
    with pytest.raises(ValueError): issuer.issue(**(case.args | {'transfer_password':password}))
    assert not case.events


@pytest.mark.parametrize('bad_stream', ['stdin','stdout','stderr'])
@pytest.mark.parametrize('kind', ['pipe','replacement'])
def test_terminal_requires_original_interactive_streams(monkeypatch, bad_stream, kind):
    monkeypatch.setattr(issuer, '_windows', lambda: None)
    seen = []
    monkeypatch.setattr(issuer, '_console', lambda stream: seen.append(stream))
    streams = {}
    for name in ('stdin','stdout','stderr'):
        stream = SimpleNamespace(isatty=lambda: True); streams[name] = stream
        monkeypatch.setattr(issuer.sys, name, stream); monkeypatch.setattr(issuer.sys, '__'+name+'__', stream)
    if kind == 'pipe': streams[bad_stream].isatty = lambda: False
    else: monkeypatch.setattr(issuer.sys, bad_stream, SimpleNamespace(isatty=lambda: True))
    with pytest.raises(ValueError): issuer._terminal()


@pytest.mark.parametrize('file_type,mode_ok', [(2,True),(1,True),(2,False)])
def test_native_console_adapter_checks_console_handle_not_only_isatty(monkeypatch, file_type, mode_ok):
    import ctypes
    class Function:
        def __init__(self, value): self.value = value
        def __call__(self, *args): return self.value
    kernel = SimpleNamespace(GetFileType=Function(file_type),GetConsoleMode=Function(mode_ok))
    monkeypatch.setattr(ctypes, 'WinDLL', lambda *a,**k: kernel, raising=False)
    monkeypatch.setitem(issuer.sys.modules, 'msvcrt', SimpleNamespace(get_osfhandle=lambda fd: 123))
    stream = SimpleNamespace(fileno=lambda: 0)
    if file_type == 2 and mode_ok: issuer._console(stream)
    else:
        with pytest.raises(ValueError): issuer._console(stream)


def test_run_pipeline_refuses_before_files_or_secret_prompt(case, monkeypatch):
    args = cli_args(case)
    def reject(): raise ValueError('no console')
    monkeypatch.setattr(issuer, '_terminal', reject)
    with pytest.raises(ValueError): issuer.run(args, prompt=lambda *_: pytest.fail('must not prompt'))
    assert not case.events


def test_run_rechecks_console_after_hidden_input(case, monkeypatch):
    args = cli_args(case); checks = []
    def terminal():
        checks.append(1)
        if len(checks) > 1: raise ValueError('console changed')
    monkeypatch.setattr(issuer, '_terminal', terminal)
    with pytest.raises(ValueError): issuer.run(args, prompt=lambda _: PASSWORD)
    assert 'audit-enter' not in case.events


def test_platform_rejection_has_no_dpapi_or_audit_side_effect(tmp_path, monkeypatch):
    monkeypatch.setattr(issuer.sys, 'platform', 'darwin')
    with pytest.raises(ValueError, match='Windows'): issuer._windows()


def test_print_failure_uses_same_fixed_preserve_scene_message(monkeypatch):
    import builtins
    monkeypatch.setattr(issuer, 'parser', lambda: SimpleNamespace(parse_args=lambda: object()))
    monkeypatch.setattr(issuer, 'run', lambda args: {'public':True})
    def failed(*args, **kwargs): raise OSError('synthetic-secret-terminal-failure')
    monkeypatch.setattr(builtins, 'print', failed)
    with pytest.raises(SystemExit) as error: issuer.main()
    assert str(error.value) == issuer.FAILURE


def test_real_historical_archive_external_seal_sqlite_audit_to_signed_output(retained_case, monkeypatch):
    from desktop_assistant import factory_cutover as original_protocol
    from desktop_assistant import retained_source_audit as real_audit
    from desktop_assistant.storage import sha, read_json
    from tests.desktop_assistant.test_retained_source_audit import TRANSFER_PASSWORD, PASSWORD as BACKUP_PASSWORD, hashes
    c = retained_case
    assert TRANSFER_PASSWORD != BACKUP_PASSWORD
    assert read_json(c.root/'state.json')['last_backup'] != str(c.backup)
    snapshot = original_protocol.capture_snapshot(c.root/'shared')
    preparation = deepcopy(valid_body(contract)['preparation'])
    preparation.update(target=c.request['target'], package_sha256=c.request['package_sha256'],
        prepared_at=contract._stamp(contract._time(c.request['created_at'])-timedelta(seconds=1)),
        baseline_snapshot=snapshot, target_snapshot=deepcopy(snapshot))
    retained = c.request['retained_snapshot']
    preparation['restore'].update({k:retained[k] for k in (
        'backup_sha256','backup_created_at','credentials_sha256','restore_package_sha256','restore_receipt_sha256')})
    delta = preparation['preview_delta']; empty = contract.digest([])
    delta['operation_logs'].update(baseline_count=0,target_count=0,appended_count=0,
        baseline_sha256=empty,target_sha256=empty,appended_sha256=empty,first_appended_id=None,last_appended_id=None)
    delta['sqlite_sequence'].update(table_present=False,baseline_sha256=empty,target_sha256=empty,
        before={'present':False,'value':None},after={'present':False,'value':None})
    delta['environment_chain'][0]['before_sha256'] = sha(c.root/'shared/environment.json')
    retained['preparation_sha256'] = contract.digest(preparation)
    contract.validate_request(c.request, preparation=preparation)
    request_raw = contract.canonical(c.request); preparation_raw = contract.canonical(preparation)
    output = c.fixture.root/'retained-output'; output.mkdir()
    identity = c.fixture.root/'original-synthetic-dpapi.json'; identity.write_bytes(b'explicit fake DPAPI adapter only')
    loads = []

    @contextmanager
    def directory(path):
        assert path == output
        yield lambda: None  # Explicit mock of Windows protected-DACL validation.

    def load(path, public):
        assert path == identity and public == c.public
        assert 'private-final-check' in c.events
        assert {identity,c.credentials_path,c.root/'state.json',c.root/'shared/data/carton_erp.sqlite3'} <= c.active_leases
        with pytest.raises(ValueError, match='另一个'):
            with operation_lock(c.root/'control/operation.lock',existing_only=True):
                pytest.fail('live source lock released before original key load')
        loads.append(1); return c.fixture.key

    monkeypatch.setattr(issuer, '_windows', lambda: None)
    monkeypatch.setattr(issuer, 'private_directory', directory)
    monkeypatch.setattr(issuer, 'read_file_lease', real_audit.read_file_lease)
    monkeypatch.setattr(issuer, 'load_dpapi_key', load)
    before = hashes(c.root)
    result = issuer.issue(root=c.root,backup=c.backup,credentials_path=c.credentials_path,
        transfer_password=TRANSFER_PASSWORD,request_bytes=request_raw,request_sha256=digest(request_raw),
        preparation_bytes=preparation_raw,preparation_sha256=digest(preparation_raw),
        target_package=c.target,public=c.public,publisher_sha256=digest(c.public),identity=identity,
        private_parent=c.parent,output_directory=output,authorize_cutover=True,confirm_source_isolated=True)
    authorization = Path(result['authorization_path']).read_bytes()
    verified = codec.verify_authorization(authorization,c.public,c.request)
    codec.require_install_window(verified)
    assert verified['source']['snapshot'] == snapshot
    assert verified['source']['backup_sha256'] == c.expected
    assert verified['source']['credentials_sha256'] == sha(c.credentials_path)
    assert verified['source']['backup_created_at'] == c.request['retained_snapshot']['backup_created_at']
    assert verified['preparation'] == preparation and loads == [1]
    assert not c.active_leases and hashes(c.root) == before
    assert result['physical_isolation_proved'] is False
    assert TRANSFER_PASSWORD not in authorization.decode() and BACKUP_PASSWORD not in authorization.decode()
