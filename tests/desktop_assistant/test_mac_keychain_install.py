from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from types import SimpleNamespace
import zipfile

import pytest

from app.core import mac_keychain as public
from app.core import mac_service_keychain as keychain
from desktop_assistant import mac_keychain_install as m
from desktop_assistant import mac_service_identity as identity
from scripts.uat import build_mac_keychain_installer as builder


@pytest.fixture
def case(monkeypatch, tmp_path):
    events = []
    context = {'type': keychain.TYPE, 'installation_id': 'a' * 32, 'uid': 499,
               'gid': 499, 'host_sha256': 'b' * 64}
    value = {'type': m.TYPE, 'attempt_id': 'c' * 32, 'identity_plan_sha256': 'd' * 64,
             'context': context, 'observer': {'uid': 501, 'gid': 20, 'home': '/Users/synthetic', 'name': 'synthetic'},
             'code_manifest_sha256': 'e' * 64, 'created_at': 100, 'expires_at': 1000}
    raw = m.canonical(value)
    monkeypatch.setattr(m, 'BASE', tmp_path)
    monkeypatch.setattr(keychain, 'CONTEXT', tmp_path / 'keychain-context.json')
    monkeypatch.setattr(m.os, 'getuid', lambda: 0)
    monkeypatch.setattr(m.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(m.time, 'time', lambda: 200)
    monkeypatch.setattr(m, 'verify_code', lambda **kw: 'e' * 64)
    monkeypatch.setattr(m, 'observer', lambda uid: deepcopy(value['observer']))
    monkeypatch.setattr(m, 'preflight', lambda plan: events.append('preflight'))
    monkeypatch.setattr(identity, '_owned_directory', lambda *args: events.append('mkdir'))
    monkeypatch.setattr(keychain, '_identity', lambda *args, **kw: events.append('identity'))
    secrets = []
    def bootstrap(ctx, secret):
        events.append('bootstrap'); secrets.append(secret)
    monkeypatch.setattr(keychain, 'create_bootstrap', bootstrap)
    writes = []
    def write(path, data, mode=0o600):
        events.append(path.name); writes.append((path, data, mode))
        path.write_bytes(data)
    monkeypatch.setattr(m, '_write', write)
    def child(plan, phase, uid, gid, home):
        events.append((phase, uid))
        if phase in {'initialize', 'verify'}:
            return {'challenge': 'f' * 32, 'proof': m._proof(secrets[0], phase, context, 'f' * 32)}
        return {}
    monkeypatch.setattr(m, 'run_child', child)
    return SimpleNamespace(value=value, raw=raw, sha=m.digest(raw), events=events,
                           writes=writes, child=child, secrets=secrets, base=tmp_path)


def test_install_verifies_both_identities_before_first_write_and_publishes_context_last(case):
    result = m.install(case.raw, case.sha)
    assert case.events[:4] == ['preflight', ('preflight', 499), ('preflight', 501), 'preflight']
    assert case.events.index('bootstrap') < case.events.index(('initialize', 499))
    assert case.events.index(('verify', 499)) < case.events.index(('negative', 501))
    assert [p.name for p, _, _ in case.writes] == [
        'keychain-init-pending.json', 'keychain-init-verified.json', 'keychain-context.json']
    assert [mode for _, _, mode in case.writes] == [0o600, 0o600, 0o644]
    assert len(case.secrets[0]) == 64
    assert all(case.secrets[0] not in raw for _, raw, _ in case.writes)
    assert result['service_new_process_verified'] and result['system_keychain_acl_denial_verified']
    assert not any(result[k] for k in ('launchd_system_verified', 'logout_reboot_verified', 'factory_activated'))


@pytest.mark.parametrize('fault', ['first_preflight', 'observer_preflight', 'initialize', 'verify', 'negative', 'receipt'])
def test_failure_preserves_partial_state_without_publishing_context(case, monkeypatch, fault):
    def child(plan, phase, uid, gid, home):
        if phase == fault or (phase == 'preflight' and (
                fault == 'first_preflight' or (fault == 'observer_preflight' and uid == 501))):
            raise ValueError('synthetic failure')
        return case.child(plan, phase, uid, gid, home)
    monkeypatch.setattr(m, 'run_child', child)
    if fault == 'receipt':
        original = m._write
        def write(path, raw, mode=0o600):
            if path.name == 'keychain-init-verified.json':
                raise OSError('synthetic disk failure')
            original(path, raw, mode)
        monkeypatch.setattr(m, '_write', write)
    with pytest.raises((ValueError, OSError)):
        m.install(case.raw, case.sha)
    assert not keychain.CONTEXT.exists()
    if 'preflight' in fault:
        assert not case.writes and not case.secrets
    else:
        assert (case.base / 'keychain-init-pending.json').read_bytes() == case.raw
        assert case.secrets  # No automatic cleanup, reset or retry.


@pytest.mark.parametrize('fault', ['secret', 'phase', 'challenge', 'context'])
def test_hmac_must_bind_original_secret_and_this_phase_context_challenge(case, monkeypatch, fault):
    def child(plan, phase, uid, gid, home):
        response = case.child(plan, phase, uid, gid, home)
        if phase == 'verify':
            context = dict(plan['context'])
            if fault == 'context':
                context['installation_id'] = '0' * 32
            response['proof'] = m._proof(b'wrong' if fault == 'secret' else case.secrets[0],
                'initialize' if fault == 'phase' else phase, context,
                '0' * 32 if fault == 'challenge' else response['challenge'])
        return response
    monkeypatch.setattr(m, 'run_child', child)
    with pytest.raises(ValueError, match='原始系统'):
        m.install(case.raw, case.sha)
    assert not keychain.CONTEXT.exists()


@pytest.mark.parametrize('fault', ['digest', 'expired', 'future', 'bool_time', 'extra', 'code', 'observer'])
def test_plan_rejects_bad_bindings_before_writes(case, fault):
    value = deepcopy(case.value)
    if fault == 'expired': value['expires_at'] = 200
    if fault == 'future': value['created_at'] = 201
    if fault == 'bool_time': value['created_at'] = True
    if fault == 'extra': value['extra'] = False
    if fault == 'code': value['code_manifest_sha256'] = '0' * 64
    if fault == 'observer': value['observer']['gid'] = 0
    raw = m.canonical(value)
    with pytest.raises(ValueError):
        m.install(raw, '0' * 64 if fault == 'digest' else m.digest(raw))
    assert not case.events and not case.writes


@pytest.mark.parametrize('raw', [b'[]', b'{"a":1,"a":2}', b'{"a":NaN}', b'{}' * 65536])
def test_json_bounds_and_structure(raw):
    with pytest.raises(ValueError): m.decode(raw)


@pytest.mark.parametrize('fault', [None, 'identity', 'challenge', 'fields', 'code', 'status', 'exit', 'json', 'interrupt'])
def test_spawn_contract_and_response_validation(monkeypatch, fault):
    monkeypatch.setattr(m, 'verify_code', lambda **kw: 'e' * 64)
    captures = []
    phase = 'negative' if fault == 'status' else 'preflight'
    class Process:
        returncode = 1 if fault == 'exit' else 0
        calls = 0
        request = None
        def communicate(self, payload):
            self.calls += 1
            if payload is not None: self.request = m.decode(payload)
            if fault == 'interrupt' and self.calls == 1: raise KeyboardInterrupt
            if self.calls == 2: assert payload is None
            response = {k: self.request[k] for k in ('phase', 'identity', 'challenge')}
            response.update({'denied': True, 'os_status': -25300} if phase == 'negative' else {'code_verified': True})
            if fault == 'identity': response['identity']['kernel_groups'] += [80]
            if fault == 'challenge': response['challenge'] = '0' * 32
            if fault == 'fields': response['unknown'] = True
            if fault == 'code': response['code_verified'] = False
            return (b'not-json' if fault == 'json' else m.canonical(response)), b'synthetic-private-error'
    process = Process()
    def popen(argv, **kwargs):
        captures.append((argv, kwargs)); return process
    monkeypatch.setattr(m.subprocess, 'Popen', popen)
    if fault:
        with pytest.raises(ValueError): m.run_child({'context': {}}, phase, 499, 499, '/fixed/home')
    else:
        assert m.run_child({'context': {}}, phase, 499, 499, '/fixed/home')['code_verified']
    argv, kw = captures[0]
    assert argv[:4] == ['/usr/bin/python3', '-I', '-B', '-S'] and argv[-1] == '_worker'
    assert kw['user'] == kw['group'] == 499 and kw['extra_groups'] == []
    assert kw['close_fds'] and kw['pass_fds'] == () and not kw['shell']
    assert kw['umask'] == 0o077 and kw['cwd'] == '/'
    assert kw['env'] == {'HOME': '/fixed/home', 'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'LANG': 'en_US.UTF-8'}
    assert 'preexec_fn' not in kw
    if fault == 'interrupt': assert process.calls == 2


@pytest.mark.parametrize('status', [-25293, -25308, -25300, -50, None, 'readable'])
def test_negative_worker_requires_actual_native_acl_denial(monkeypatch, status):
    expected = m._expected(501, 20)
    monkeypatch.setattr(m, 'verify_code', lambda **kw: 'e' * 64)
    monkeypatch.setattr(m, '_actual', lambda: expected)
    monkeypatch.setattr(m.os, 'getuid', lambda: 501)
    monkeypatch.setattr(keychain, '_identity', lambda *a, **k: pytest.fail('Business identity is not ACL evidence'))
    calls = []
    class Native:
        @contextmanager
        def system(self): yield 'exact-system'
        def get(self, ref, service, account):
            calls.append((ref, service, account))
            if status == 'readable': return b'synthetic-secret'
            error = keychain.KeychainError('synthetic')
            if status is not None: error.os_status = status
            raise error
    monkeypatch.setattr(keychain, 'Security', Native)
    request = {'phase': 'negative', 'identity': expected, 'context': {'installation_id': 'a' * 32}, 'challenge': 'f' * 32}
    if status in {-25293, -25308}:
        result = m._worker(request)
        assert result['denied'] is True and result['os_status'] == status
    else:
        with pytest.raises(ValueError): m._worker(request)
    assert calls == [('exact-system', keychain.BOOTSTRAP_SERVICE, keychain.binding(request['context']))]


def test_public_native_error_retains_numeric_status_without_secret():
    with pytest.raises(public.KeychainError) as error:
        public.Keychain.check(None, -25293)
    assert error.value.os_status == -25293


def test_late_context_readback_failure_keeps_already_published_context(case, monkeypatch):
    original = m._write
    def write(path, raw, mode=0o600):
        original(path, raw, mode)
        if path == keychain.CONTEXT:
            raise ValueError('synthetic late readback failure')
    monkeypatch.setattr(m, '_write', write)
    with pytest.raises(ValueError): m.install(case.raw, case.sha)
    assert keychain.CONTEXT.read_bytes() == m.canonical(case.value['context'])
    assert (case.base / 'keychain-init-verified.json').exists()


@pytest.mark.parametrize('count,second', [(-1, 0), (0, 0), (1025, 0), (1, -1), (1, 0), (1, 1)])
def test_kernel_group_read_must_be_bounded_and_complete(monkeypatch, count, second):
    class Function:
        def __call__(self, size, values):
            if size == 0: return count
            values[0] = 499
            return second
    monkeypatch.setattr(m.C, 'CDLL', lambda *a, **kw: SimpleNamespace(getgroups=Function()))
    if (count, second) == (1, 1): assert m.kernel_groups() == [499]
    else:
        with pytest.raises(ValueError): m.kernel_groups()


@pytest.mark.parametrize('field,wrong', [('uid', 0), ('euid', 0), ('gid', 0), ('egid', 0), ('kernel_groups', [499, 0])])
def test_worker_rejects_wrong_real_identity_before_import_or_api(monkeypatch, field, wrong):
    actual = m._expected(499, 499); actual[field] = wrong
    monkeypatch.setattr(m, 'verify_code', lambda: 'e' * 64)
    monkeypatch.setattr(m, '_actual', lambda: actual)
    monkeypatch.setattr(m, 'modules', lambda: pytest.fail('must reject before imports'))
    with pytest.raises(ValueError, match='降到'):
        m._worker({'phase': 'preflight', 'context': {}, 'identity': m._expected(499, 499), 'challenge': 'a' * 32})


@pytest.mark.parametrize('fault', [None, 'origin', 'context', 'receipt', 'od', 'existing', 'dangling'])
def test_real_preflight_requires_exact_prior_identity_and_no_setup_scene(monkeypatch, tmp_path, fault):
    base = tmp_path.resolve(); monkeypatch.setattr(m, 'BASE', base)
    monkeypatch.setattr(keychain, 'CONTEXT', base / 'keychain-context.json')
    monkeypatch.setattr(identity, '_administrator', lambda: None)
    monkeypatch.setattr(keychain, '_identity', lambda *a, **kw: None)
    @contextmanager
    def allowed(*a, **kw): yield
    monkeypatch.setattr(keychain, '_checked_path', allowed)
    origin = {'installation_id': 'a' * 32, 'uid': 499, 'gid': 499, 'host_sha256': 'b' * 64}
    raw = m.canonical(origin)
    monkeypatch.setattr(m, '_origin', lambda value: value)
    context = {'type': keychain.TYPE, **origin}
    value = {'context': context, 'identity_plan_sha256': m.digest(raw)}
    receipt = {'type': 'tianming.mac-service-identity.v1', 'plan_sha256': m.digest(raw),
               'uid': 499, 'gid': 499, 'installation_id': 'a' * 32,
               'home': str(base / 'home'), 'root': str(base / 'instance'), 'account_created': True,
               'keychain_created': False, 'service_started': False, 'factory_activated': False}
    if fault == 'origin': value['identity_plan_sha256'] = '0' * 64
    if fault == 'context': context['uid'] = 498
    if fault == 'receipt': receipt['keychain_created'] = True
    if fault == 'existing': keychain.CONTEXT.write_text('{}')
    if fault == 'dangling': (base / 'keychain-init-pending.json').symlink_to(base / 'absent')
    def read(path, **kw):
        if path.name == '00-plan.json': return raw
        if path.name == 'provisioned.json': return m.canonical(receipt)
        return b''
    monkeypatch.setattr(m, 'protected', read)
    attrs = {'a': ['synthetic']}
    monkeypatch.setattr(identity, '_attributes', lambda value: (attrs, attrs))
    directory = SimpleNamespace(read=lambda *a: {} if fault == 'od' else
                                {**attrs, identity.ATTR + 'RecordName': [identity.ACCOUNT]})
    monkeypatch.setattr(identity, 'Directory', lambda: directory)
    if fault:
        with pytest.raises(ValueError): m.preflight(value)
    else: m.preflight(value)


@pytest.mark.parametrize('fault', [None, 'mode', 'owner', 'hardlink', 'unreadable', 'acl', 'change'])
def test_protected_file_checks_metadata_acl_and_stability(tmp_path, monkeypatch, fault):
    path = tmp_path / 'own'; path.write_bytes(b'synthetic')
    actual = path.stat()
    info = SimpleNamespace(**{key: getattr(actual, key) for key in
        ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns')}, st_uid=0, st_gid=0, st_nlink=1, st_mode=0o100444)
    if fault == 'mode': info.st_mode = 0o100464
    if fault == 'owner': info.st_uid = 501
    if fault == 'hardlink': info.st_nlink = 2
    if fault == 'unreadable': info.st_mode = 0o100400
    monkeypatch.setattr(m.os, 'fstat', lambda fd: info)
    changed = deepcopy(info)
    if fault == 'change': changed.st_ino += 1
    monkeypatch.setattr(m.os, 'stat', lambda *a, **kw: changed)
    acl_calls = []
    def acl(fd):
        acl_calls.append(fd)
        if fault == 'acl': raise ValueError('synthetic ACL')
    monkeypatch.setattr(m, 'no_acl', acl)
    if fault:
        with pytest.raises(ValueError): m.protected(path, readable=True)
    else:
        assert m.protected(path, readable=True) == b'synthetic' and acl_calls


def test_minimal_pack_builds_only_committed_allowlist_and_verifies_inventory(tmp_path, monkeypatch):
    assert builder.FILES == m.FILES
    repo = tmp_path / 'repo'; repo.mkdir()
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=repo).decode().strip()
    git('init', '-q')
    for name in builder.FILES:
        p = repo / name; p.parent.mkdir(parents=True, exist_ok=True); p.write_text('# synthetic original\n')
    (repo / 'unrelated-secret.txt').write_text('synthetic-excluded')
    git('add', '.'); git('-c', 'user.name=Synthetic', '-c', 'user.email=synthetic@invalid', 'commit', '-qm', 'fixture')
    sha = git('rev-parse', 'HEAD')
    (repo / builder.FILES[0]).write_text('# uncommitted must not enter pack\n')
    output = tmp_path / 'code.zip'
    report = builder.build(repo, sha, output)
    assert report['files'] == 8 and report['sha256'] == m.digest(output.read_bytes())
    with zipfile.ZipFile(output) as archive:
        assert archive.read(builder.FILES[0]) == b'# synthetic original\n'
        archive.extractall(tmp_path / 'pack')
    monkeypatch.setattr(m, 'code_root', lambda: tmp_path / 'pack')
    assert m.verify_code(trusted=False) == report['manifest_sha256']
    with pytest.raises(FileExistsError): builder.build(repo, sha, output)
    (tmp_path / 'pack/app/unlisted.py').write_text('# extra')
    with pytest.raises(ValueError, match='额外文件'): m.verify_code(trusted=False)
    (tmp_path / 'pack/app/unlisted.py').unlink()
    (tmp_path / 'pack/app/__init__.py').write_text('# corrupt')
    with pytest.raises(ValueError, match='摘要'): m.verify_code(trusted=False)


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS read-only kernel/ACL checks')
def test_native_readonly_groups_acl_and_unprivileged_refusal(tmp_path):
    groups = m.kernel_groups()
    assert groups and groups[0] == os.getegid() and all(type(x) is int for x in groups)
    path = tmp_path / 'own-file'; path.write_text('synthetic')
    fd = os.open(path, os.O_RDONLY)
    try: m.no_acl(fd)
    finally: os.close(fd)
    with pytest.raises(ValueError, match='root保护'): m.protected(path)
    with pytest.raises(ValueError, match='管理员'): m.install(b'{}', '0' * 64)
