"""Synthetic GCM/Ed25519/source audit integration; no factory identity loaded."""
from contextlib import contextmanager
import getpass
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import warnings

import pytest

from desktop_assistant import factory_source_issue as issuer
from desktop_assistant import factory_cutover as protocol
from desktop_assistant.operation_lock import operation_lock
from tests.desktop_assistant import test_factory_source_audit as audit_fixtures

case = audit_fixtures.case


@pytest.fixture
def signing_case(case, monkeypatch):
    output = case.fixture.root / 'private-output'
    output.mkdir()
    identity = case.fixture.root / 'synthetic-publisher.json'
    identity.write_text('synthetic adapter only; no real DPAPI here')
    events = []

    @contextmanager
    def directory(path):
        assert path == output
        def check():
            events.append('output-acl')
        check()
        yield check
        check()

    @contextmanager
    def lease(path):
        events.append(('lease', path))
        yield
        events.append(('release', path))

    def load(path, public):
        assert path == identity and public == case.public
        assert 'private-final-check' in case.events
        assert 'lease-exit' not in case.events
        assert events[-1] == 'output-acl'
        with pytest.raises(ValueError, match='另一个'):
            with operation_lock(case.root / 'control/operation.lock'):
                pytest.fail('source lock must remain held')
        events.append('key-loaded')
        return case.fixture.key

    monkeypatch.setattr(issuer, '_windows', lambda: None)
    monkeypatch.setattr(issuer, 'private_directory', directory)
    monkeypatch.setattr(issuer, 'read_file_lease', lease)
    monkeypatch.setattr(issuer, 'load_dpapi_key', load)
    request_bytes = protocol.canonical(case.request)
    args = dict(root=case.root, backup=case.backup, expected_backup_sha256=case.expected,
                password=audit_fixtures.PASSWORD, request_bytes=request_bytes,
                request_sha256=hashlib.sha256(request_bytes).hexdigest(),
                target_package=case.target, public=case.public,
                publisher_sha256=hashlib.sha256(case.public).hexdigest(), identity=identity,
                private_parent=case.parent, output_directory=output,
                authorize_cutover=True, confirm_source_isolated=True)
    return SimpleNamespace(source=case, args=args, output=output, events=events,
                           destination=output / ('cutover-' + case.request['request_id'] + '.json'))


def test_real_synthetic_audit_precedes_key_load_and_signature_is_read_back(signing_case):
    c = signing_case
    before = audit_fixtures.hashes(c.source.root)
    result = issuer.issue(**c.args)
    raw = c.destination.read_bytes()
    body = protocol.verify_authorization(raw, c.source.public, c.source.request)
    assert body['source']['backup_sha256'] == c.source.expected
    assert body['source']['snapshot']['tables'] >= 1
    assert result['authorization_sha256'] == hashlib.sha256(raw).hexdigest()
    assert result['physical_isolation_proved'] is False
    assert result['target_activated'] is False and result['source_started'] is False
    assert audit_fixtures.hashes(c.source.root) == before
    assert c.events.count('key-loaded') == 1
    assert c.source.events[-1] == 'lease-exit'
    assert 'synthetic-secret' not in raw.decode() and audit_fixtures.PASSWORD not in raw.decode()


@pytest.mark.parametrize('field,value', [
    ('authorize_cutover', False), ('authorize_cutover', 1),
    ('confirm_source_isolated', False), ('confirm_source_isolated', 1),
    ('request_sha256', '0' * 64), ('publisher_sha256', '0' * 64),
    ('request_bytes', b'{}'), ('identity', Path('relative.json')),
    ('root', Path('.')), ('output_directory', Path('relative')),
])
def test_unapproved_or_relative_inputs_do_not_audit_or_load_identity(signing_case, field, value):
    c = signing_case
    with pytest.raises(ValueError):
        issuer.issue(**(c.args | {field: value}))
    assert not c.events and not c.source.events and not list(c.output.iterdir())


def test_output_inside_source_is_rejected_before_audit(signing_case):
    c = signing_case
    with pytest.raises(ValueError, match='之外'):
        issuer.issue(**(c.args | {'output_directory': c.source.root / 'control'}))
    assert not c.events and not c.source.events


def test_old_output_keeps_bytes_without_source_audit_or_key_load(signing_case):
    c = signing_case
    c.destination.write_bytes(b'previous artifact keep unchanged')
    with pytest.raises(ValueError, match='已经存在'):
        issuer.issue(**c.args)
    assert not c.source.events and 'key-loaded' not in c.events
    assert c.destination.read_bytes() == b'previous artifact keep unchanged'


def test_any_source_audit_failure_cannot_reach_publisher(signing_case):
    c = signing_case
    with pytest.raises(ValueError):
        issuer.issue(**(c.args | {'expected_backup_sha256': '0' * 64}))
    assert 'key-loaded' not in c.events and not c.destination.exists()


def test_final_plaintext_acl_failure_cannot_reach_publisher(signing_case, monkeypatch):
    c = signing_case
    @contextmanager
    def bad(parent):
        with c.source.workspace(parent) as job:
            yield job
        raise ValueError('synthetic final ACL')
    monkeypatch.setattr(audit_fixtures.audit, 'private_workspace', bad)
    with pytest.raises(ValueError, match='ACL'):
        issuer.issue(**c.args)
    assert 'key-loaded' not in c.events and not c.destination.exists()


def test_valid_signature_for_different_source_never_published(signing_case, monkeypatch):
    c = signing_case
    real_sign = protocol.sign_authorization
    def wrong(request, evidence, key):
        return real_sign(request, evidence | {'source_sha256': '0' * 64}, key)
    monkeypatch.setattr(protocol, 'sign_authorization', wrong)
    with pytest.raises(ValueError, match='刚完成'):
        issuer.issue(**c.args)
    assert not c.destination.exists()


@pytest.mark.parametrize('failure', ['write', 'fsync', 'readback', 'final-acl', 'late-window'])
def test_publication_failure_retains_scene_and_never_restarts(signing_case, monkeypatch, failure):
    c = signing_case
    before = audit_fixtures.hashes(c.source.root)
    if failure == 'write':
        real_open = issuer.os.open
        def reject(path, flags, *args, **kwargs):
            if Path(path) == c.destination:
                raise OSError('synthetic exclusive create denied')
            return real_open(path, flags, *args, **kwargs)
        monkeypatch.setattr(issuer.os, 'open', reject)
    elif failure == 'fsync':
        real_fsync = issuer.os.fsync
        def reject(fd):
            if c.destination.exists():
                raise OSError('synthetic fsync failure')
            return real_fsync(fd)
        monkeypatch.setattr(issuer.os, 'fsync', reject)
    elif failure == 'readback':
        monkeypatch.setattr(issuer, '_read', lambda *a: b'synthetic wrong readback')
    elif failure == 'final-acl':
        @contextmanager
        def directory(path):
            yield lambda: None
            raise ValueError('synthetic final output ACL')
        monkeypatch.setattr(issuer, 'private_directory', directory)
        monkeypatch.setattr(issuer, 'load_dpapi_key', lambda *a: c.source.fixture.key)
    else:
        real_window = protocol.require_install_window
        count = 0
        def window(body):
            nonlocal count
            count += 1
            if count == 2:
                raise ValueError('synthetic expired during output')
            real_window(body)
        monkeypatch.setattr(protocol, 'require_install_window', window)
    with pytest.raises((ValueError, OSError)):
        issuer.issue(**c.args)
    assert audit_fixtures.hashes(c.source.root) == before
    assert c.destination.exists() is (failure != 'write')


def test_exclusive_create_race_preserves_competing_file(signing_case, monkeypatch):
    c = signing_case
    real_sign = protocol.sign_authorization
    def race(*args):
        c.destination.write_bytes(b'racing prior artifact')
        return real_sign(*args)
    monkeypatch.setattr(protocol, 'sign_authorization', race)
    with pytest.raises(FileExistsError):
        issuer.issue(**c.args)
    assert c.destination.read_bytes() == b'racing prior artifact'


def test_cli_pipeline_and_secret_arguments_are_never_echoed(monkeypatch, capsys):
    monkeypatch.setattr(issuer, 'parser', lambda: SimpleNamespace(parse_args=lambda: object()))
    monkeypatch.setattr(issuer.sys.stdin, 'isatty', lambda: False)
    monkeypatch.setattr(issuer, 'run', lambda *a: pytest.fail('must refuse before input'))
    with pytest.raises(SystemExit, match='交互终端'):
        issuer.main()
    assert not capsys.readouterr().out


def test_parser_has_no_password_or_clock_override_and_does_not_echo_bad_input(capsys):
    parser = issuer.parser()
    assert '--password' not in parser.format_help() and '--now' not in parser.format_help()
    with pytest.raises(SystemExit) as caught:
        parser.parse_args(['--password', 'synthetic-secret-never-echo'])
    assert 'synthetic-secret' not in str(caught.value) + capsys.readouterr().err


def test_getpass_warning_fails_before_issue(signing_case, monkeypatch):
    c = signing_case
    public = c.source.fixture.root / 'public.pem'; public.write_bytes(c.source.public)
    request = c.source.fixture.root / 'request.json'; request.write_bytes(c.args['request_bytes'])
    args = SimpleNamespace(public_key=public, request=request,
        request_sha256=c.args['request_sha256'], publisher_sha256=c.args['publisher_sha256'],
        authorize_cutover=True, confirm_source_isolated=True)
    def prompt(*args):
        warnings.warn('synthetic no hidden input', getpass.GetPassWarning)
        pytest.fail('must not continue to fallback input')
    monkeypatch.setattr(issuer, 'issue', lambda **kw: pytest.fail('must not reach issuer'))
    with pytest.raises(getpass.GetPassWarning):
        issuer.run(args, prompt=prompt)


def test_cli_failure_does_not_disclose_native_exception(monkeypatch, capsys):
    monkeypatch.setattr(issuer, 'parser', lambda: SimpleNamespace(parse_args=lambda: object()))
    monkeypatch.setattr(issuer.sys.stdin, 'isatty', lambda: True)
    def fail(*args):
        raise ValueError('synthetic-secret-from-native')
    monkeypatch.setattr(issuer, 'run', fail)
    with pytest.raises(SystemExit) as caught:
        issuer.main()
    assert '可能已经包含有效' in str(caught.value)
    assert 'synthetic-secret' not in str(caught.value) + capsys.readouterr().out
