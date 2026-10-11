from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest

from app.core import mac_keychain as public
from app.core import mac_service_keychain as module


@contextmanager
def allowed(*args, **kwargs):
    yield


@pytest.fixture
def case(monkeypatch, tmp_path):
    value = {'type': module.TYPE, 'installation_id': 'a' * 32,
             'uid': 499, 'gid': 499, 'host_sha256': 'b' * 64}
    events = []
    records = {('system', module.BOOTSTRAP_SERVICE, module.binding(value)): b'c' * 64,
               ('private', module.CANARY_SERVICE, module.binding(value)): module._canary(b'c' * 64, value)}
    class Fake:
        @contextmanager
        def system(self, *, writable=False):
            events.append(('system', writable)); yield 'system'
        @contextmanager
        def opened(self, path):
            events.append('opened'); yield 'private'
        @contextmanager
        def created(self, path, secret):
            events.append('created'); yield 'private'
        def unlock(self, ref, secret):
            events.append('unlock')  # Intentionally already-unlocked semantics.
        def get(self, ref, service, account):
            events.append(('get', ref, service))
            return records[(ref, service, account)]
        def put(self, ref, service, account, raw, uid, gid):
            events.append(('put', ref, service))
            assert (uid, gid) == (499, 499)
            key = (ref, service, account)
            if key in records:
                raise module.KeychainError('duplicate')
            records[key] = raw
    @contextmanager
    def paths(value, *, existing=True):
        events.append(('path', existing)); yield tmp_path / 'synthetic.keychain-db'
    identities = []
    monkeypatch.setattr(module, '_identity', lambda value, **kw: identities.append(kw))
    monkeypatch.setattr(module, '_private_paths', paths)
    monkeypatch.setattr(module, '_keychain_file', allowed)
    monkeypatch.setattr(module, 'load_context', lambda: deepcopy(value))
    api = Fake()
    monkeypatch.setattr(module, 'Security', lambda: api)
    return SimpleNamespace(value=value, events=events, records=records, api=api, identities=identities)


def test_service_routes_all_purposes_and_rotates_without_losing_old_reference(case, monkeypatch):
    monkeypatch.setattr(module, 'selected', lambda: True)
    for purpose in sorted(public.PURPOSES):
        old = public.protect('合成\0old', purpose)
        new = public.protect('合成\0new', purpose)
        assert old != new
        assert public.unprotect(old, purpose) == '合成\0old'
        assert public.unprotect(new, purpose) == '合成\0new'
    assert not any(event in ('created',) or event == ('system', True) for event in case.events)


@pytest.mark.parametrize('fault', ['context', 'identity', 'paths', 'bootstrap-missing',
                                  'bootstrap-malformed', 'bootstrap-wrong', 'canary-missing', 'canary-wrong'])
def test_incomplete_or_mismatched_service_never_creates_or_uses_personal_store(case, monkeypatch, fault):
    def refuse(*args, **kwargs):
        raise module.KeychainError('synthetic refusal')
    if fault == 'context':
        monkeypatch.setattr(module, 'load_context', refuse)
    elif fault == 'identity':
        monkeypatch.setattr(module, '_identity', refuse)
    elif fault == 'paths':
        monkeypatch.setattr(module, '_private_paths', refuse)
    elif fault.startswith('bootstrap'):
        key = ('system', module.BOOTSTRAP_SERVICE, module.binding(case.value))
        if fault == 'bootstrap-missing':
            del case.records[key]
        else:
            case.records[key] = b'wrong-format' if fault == 'bootstrap-malformed' else b'd' * 64
    else:
        key = ('private', module.CANARY_SERVICE, module.binding(case.value))
        if fault == 'canary-missing':
            del case.records[key]
        else:
            case.records[key] = b'wrong-value'
    monkeypatch.setattr(module, 'selected', lambda: True)
    monkeypatch.setattr(public, 'Keychain', lambda: pytest.fail('no personal fallback'))
    before = deepcopy(case.records)
    with pytest.raises((ValueError, KeyError)):
        public.protect('synthetic-business-secret', 'backup')
    assert case.records == before
    assert 'created' not in case.events


def test_explicit_bootstrap_and_private_init_are_separate_from_context_publication(case):
    case.records.clear()
    module.create_bootstrap(case.value, b'c' * 64)
    assert case.identities[0] == {'administrator': True}
    result = module.initialize_private(case.value)
    assert result == {'keychain_created': True, 'canary_verified': True, 'context_published': False}
    assert 'created' in case.events
    assert set(key[1] for key in case.records) == {module.BOOTSTRAP_SERVICE, module.CANARY_SERVICE}


def test_post_write_readback_failure_preserves_new_record_without_claiming_success(case):
    get = case.api.get
    def corrupt(ref, service, account):
        return b'mismatch' if service == module.SERVICE else get(ref, service, account)
    case.api.get = corrupt
    with pytest.raises(module.KeychainError, match='可能已写入'):
        module.ServiceStore().put('new-reference', b'synthetic')
    assert case.records[('private', module.SERVICE, 'new-reference')] == b'synthetic'


@pytest.mark.parametrize('kind', ['legacy-file', 'legacy-dangling-link', 'current-file', 'current-dangling-link'])
def test_creation_preserves_legacy_or_current_scene_before_native_api(tmp_path, kind):
    path = tmp_path.resolve() / 'synthetic.keychain-db'
    existing = path.with_name('synthetic.keychain') if kind.startswith('legacy') else path
    if kind.endswith('file'):
        existing.write_bytes(b'existing-synthetic-only')
    else:
        existing.symlink_to(tmp_path / 'absent')
    # Uninitialized native object makes any accidental API entry an error.
    api = module.Security.__new__(module.Security)
    with pytest.raises(module.KeychainError, match='已存在'):
        with api.created(path, b'a' * 64):
            pytest.fail('existing scene adopted')
    if existing.is_symlink():
        assert existing.readlink() == tmp_path / 'absent'
    else:
        assert existing.read_bytes() == b'existing-synthetic-only'


@pytest.mark.parametrize('fault', ['missing', 'duplicate', 'too-large', 'invalid-json', 'wrong-identity'])
def test_completion_record_load_fails_without_writing_or_initializing(tmp_path, monkeypatch, fault):
    from desktop_assistant import mac_service_identity as identity
    base = tmp_path.resolve(); path = base / 'keychain-context.json'
    raw = {'duplicate': b'{"uid":499,"uid":0}', 'too-large': b'x' * 65537,
           'invalid-json': b'not-json', 'wrong-identity': b'{}'}.get(fault)
    if raw is not None:
        path.write_bytes(raw); path.chmod(0o644)
    monkeypatch.setattr(module, 'BASE', base)
    monkeypatch.setattr(module, 'CONTEXT', path)
    monkeypatch.setattr(identity, '_parent', lambda: None)
    check = module._checked_path
    @contextmanager
    def synthetic_owner(path, **kwargs):
        if kwargs.get('directory'):
            yield None
        else:
            with check(path, uid=os.getuid(), mode=kwargs['mode']) as fd:
                yield fd
    monkeypatch.setattr(module, '_checked_path', synthetic_owner)
    def invalid(value):
        raise module.KeychainError('wrong-identity')
    monkeypatch.setattr(module, '_identity', invalid)
    before = set(base.iterdir())
    with pytest.raises((ValueError, OSError)):
        module.load_context()
    assert set(base.iterdir()) == before
    if raw is not None:
        assert path.read_bytes() == raw


@pytest.mark.parametrize('field,value', [('uid', 0), ('gid', True), ('installation_id', '../bad'),
                                      ('host_sha256', 'wrong-host'), ('type', 'unknown')])
def test_real_identity_validator_rejects_wrong_binding_before_any_keychain(monkeypatch, field, value):
    from desktop_assistant import mac_service_identity as identity
    monkeypatch.setattr(identity, '_platform', lambda: None)
    monkeypatch.setattr(identity, '_host', lambda: 'b' * 64)
    spec = {'type': module.TYPE, 'installation_id': 'a' * 32, 'uid': 499, 'gid': 499,
            'host_sha256': 'b' * 64}
    spec[field] = value
    with pytest.raises(module.KeychainError, match='身份无效'):
        module._identity(spec)


@pytest.mark.parametrize('wrong', ['uid', 'euid', 'gid', 'egid', 'groups', None])
def test_real_identity_validator_checks_actual_process_credentials(monkeypatch, wrong):
    from desktop_assistant import mac_service_identity as identity
    for name in ('_platform', '_parent'):
        monkeypatch.setattr(identity, name, lambda: None)
    monkeypatch.setattr(identity, '_host', lambda: 'b' * 64)
    monkeypatch.setattr(identity, '_check_identity', lambda value: None)
    monkeypatch.setattr(module, '_checked_path', allowed)
    for name in ('uid', 'euid', 'gid', 'egid'):
        monkeypatch.setattr(os, 'get' + name, lambda name=name: 0 if name == wrong else 499)
    monkeypatch.setattr(os, 'getgroups', lambda: [499, 80] if wrong == 'groups' else [499, 12, 61])
    value = {'type': module.TYPE, 'installation_id': 'a' * 32, 'uid': 499, 'gid': 499,
             'host_sha256': 'b' * 64}
    if wrong:
        with pytest.raises(module.KeychainError, match='实际服务身份'):
            module._identity(value)
    else:
        module._identity(value)


@pytest.mark.parametrize('fault', ['mode', 'hardlink', 'symlink', 'missing', 'acl'])
@pytest.mark.skipif(sys.platform != 'darwin', reason='native macOS path/ACL checks')
def test_private_file_refuses_unsafe_paths_without_repair(tmp_path, fault):
    path = tmp_path.resolve() / 'synthetic.keychain-db'
    path.write_bytes(b'non-secret synthetic'); path.chmod(0o600)
    if fault == 'mode':
        path.chmod(0o644)
    elif fault == 'hardlink':
        os.link(path, path.parent / 'second')
    elif fault == 'symlink':
        target = path.with_name('target'); path.rename(target); path.symlink_to(target)
    elif fault == 'missing':
        path.unlink()
    else:
        subprocess.run(['/bin/chmod', '+a', 'everyone allow read', str(path)], check=True)
    before = path.lstat() if path.exists() or path.is_symlink() else None
    with pytest.raises((ValueError, OSError)):
        with module._keychain_file(path, os.getuid()):
            pytest.fail('unsafe file accepted')
    after = path.lstat() if path.exists() or path.is_symlink() else None
    assert before == after


@pytest.mark.skipif(sys.platform != 'darwin', reason='native file permission checks')
def test_private_keychain_allows_protected_atomic_replace_but_rejects_permission_loss(tmp_path):
    path = tmp_path.resolve() / 'synthetic.keychain-db'
    path.write_bytes(b'before'); path.chmod(0o600)
    with module._keychain_file(path, os.getuid()):
        new = path.with_name('new'); new.write_bytes(b'after'); new.chmod(0o600); new.replace(path)
    with pytest.raises(module.KeychainError):
        with module._keychain_file(path, os.getuid()):
            path.chmod(0o644)
    assert stat.S_IMODE(path.stat().st_mode) == 0o644  # no silent repair


@pytest.mark.skipif(sys.platform != 'darwin' or os.getenv('ERP_TEST_MAC_SERVICE_KEYCHAIN') != '1',
                    reason='explicit new synthetic native keychain, isolated child processes')
def test_native_uid_acl_other_interpreter_rotation_lock_and_persistent_preferences(tmp_path):
    probe = Path(__file__).parent / 'desktop_assistant/mac_service_keychain_probe.py'
    def invoke(value):
        result = subprocess.run([sys.executable, '-I', str(probe)], input=json.dumps(value),
                                text=True, capture_output=True, timeout=60)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)
    before = invoke({'fingerprint_only': True})
    report = invoke({'path': str(tmp_path.resolve() / 'synthetic.keychain-db')})
    after = invoke({'fingerprint_only': True})
    assert before == after
    assert all(report[key] for key in ('uid_acl_same_uid_other_interpreter', 'rotation_preserves_old',
               'duplicate_refused', 'locked_wrong_password_refused', 'private_file_permissions_verified',
               'during_creation_preferences_unchanged', 'after_deletion_preferences_unchanged',
               'missing_private_file_refused'))
    assert not any(report[key] for key in ('system_item_read', 'system_item_write',
                                          'dedicated_service_uid_tested', 'other_uid_rejection_tested'))
