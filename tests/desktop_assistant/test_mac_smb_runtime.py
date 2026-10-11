"""SMB worker authorization and orchestration; no actual NAS passwords."""
from copy import deepcopy
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import json
import os
import pytest

from desktop_assistant import mac_smb_runtime as runtime
from desktop_assistant import mac_smb_runtime_contract as contract
from tests.desktop_assistant import test_mac_smb_contract as fixtures


def control():
    value = dict(type=contract.TYPE, attempt_id='a'*32, plan_sha256='b'*64,
                 package_sha256='d'*64, context_sha256=fixtures.CONTEXT,
                 publisher_sha256='e'*64, contract_sha256=contract.digest(contract.canonical(fixtures.value())))
    value['credential_account'] = contract.account(value['context_sha256'], value['contract_sha256'], value['attempt_id'])
    return value


def test_exact_contract_separates_nas_account_and_limits_network():
    value = control()
    assert contract.validate(value, fixtures.value(), fixtures.CONTEXT, 499) == value
    assert value['credential_account'].startswith('nas-smb-v1:')
    profile = contract.profile(fixtures.value(), fixtures.CONTEXT, 499)
    assert profile == '(version 1)(allow default)(deny network-outbound)(allow network-outbound (remote ip "192.168.1.245:445"))'


@pytest.mark.parametrize('key,bad', [('type','other'),('attempt_id','../other'),('context_sha256','f'*64),
    ('package_sha256',''),('plan_sha256',None),('publisher_sha256',True),('contract_sha256','f'*64),
    ('credential_account','backup:old-reference'),('password','SYNTHETIC_PRIVATE')])
def test_control_refuses_substitution_and_secret_fields(key, bad):
    value = control(); value[key] = bad
    with pytest.raises(ValueError): contract.validate(value, fixtures.value(), fixtures.CONTEXT, 499)


@pytest.mark.parametrize('bad', [None, False, True, 0, 2, '1'])
def test_missing_or_noninteger_capability_is_not_approval(bad):
    with pytest.raises(ValueError):
        contract.capability({'runtime_capabilities':{contract.CAPABILITY:bad}, 'files':dict.fromkeys(contract.FILES)})


@pytest.fixture
def binding(monkeypatch, tmp_path):
    value = control(); cfg = fixtures.value()
    context = {'uid':499, 'gid':499}
    identity = {'uid':499, 'euid':499, 'gid':499, 'egid':499, 'kernel_groups':[499]}
    tree = tmp_path/'approved'
    meta = {'context':context, 'context_sha256':fixtures.CONTEXT, 'publisher_sha256':'e'*64,
            'registry_sha256':contract.digest(b'registry'), 'tree':tree, 'public':b'synthetic-public'}
    records = {contract.CONTROL:contract.canonical(value), contract.CONTRACT:contract.canonical(cfg),
               runtime.published.REGISTRY:b'registry'}
    monkeypatch.setattr(runtime.keychain, 'load_context', lambda:deepcopy(context))
    monkeypatch.setattr(runtime.keychain, 'selected', lambda:True)
    monkeypatch.setattr(runtime.boot, '_actual', lambda:deepcopy(identity))
    monkeypatch.setattr(runtime.identity, 'permitted_groups', lambda gid:{499})
    monkeypatch.setattr(runtime.service, 'metadata', lambda package:deepcopy(meta))
    monkeypatch.setattr(runtime.publication, 'read_record', lambda path:records[path])
    manifest = {'runtime_capabilities':{contract.CAPABILITY:1}, 'files':dict.fromkeys(contract.FILES)}
    monkeypatch.setattr(runtime.published, 'resolve', lambda *a, **kw:(tree, manifest))
    monkeypatch.setattr(runtime.mounts, 'local_addresses', lambda:{'127.0.0.1'})
    monkeypatch.setattr(runtime.sys, 'platform', 'darwin')
    return SimpleNamespace(value=value, cfg=cfg, context=context, identity=identity,
                           records=records, tree=tree, meta=meta, manifest=manifest)


def test_root_binding_can_be_rechecked_but_not_replaced(binding):
    approved = runtime.load_binding()
    approved.check()
    binding.records[contract.CONTROL] = contract.canonical({**binding.value, 'plan_sha256':'9'*64})
    with pytest.raises(ValueError): approved.check()


def test_another_host_address_rejects_before_secret_access(binding, monkeypatch):
    monkeypatch.setattr(runtime.mounts, 'local_addresses', lambda:{'192.168.1.245'})
    with pytest.raises(ValueError): runtime.load_binding()


@pytest.mark.parametrize('kind', ['capability','tree','publisher','context','registry','control-bytes'])
def test_incomplete_approval_fails_before_worker(binding, monkeypatch, kind):
    if kind == 'capability': binding.manifest['runtime_capabilities'].clear()
    if kind == 'tree': binding.meta['tree'] = binding.tree/'other'
    if kind == 'publisher': binding.meta['publisher_sha256'] = 'f'*64
    if kind == 'context': binding.meta['context'] = {'uid':498, 'gid':498}
    if kind == 'registry': binding.records[runtime.published.REGISTRY] = b'changed'
    if kind == 'control-bytes': binding.records[contract.CONTROL] += b'\n'
    process = Mock(); monkeypatch.setattr(runtime.subprocess, 'Popen', process)
    with pytest.raises(ValueError): runtime.ensure_destination(Path(binding.cfg['destination']))
    process.assert_not_called()


@pytest.fixture
def connector(binding, monkeypatch):
    events = []
    state = {'mounted':False, 'error':False}
    observation = fixtures.observation()
    directories = [{'dev':1,'ino':i,'uid':499,'gid':499,'mode':0o40700} for i in (1,2,3)]
    def selected(path):
        return deepcopy(observation['kernel'] if state['mounted'] else
                        {'filesystem':'apfs','flags':0x1000,'mountpoint':'/System/Volumes/Data'})
    monkeypatch.setattr(runtime.mounts, '_select', selected)
    @contextmanager
    def lock(context):
        assert context == binding.context
        events.append('locked'); yield; events.append('unlocked')
    monkeypatch.setattr(runtime, 'connection_lock', lock)
    @contextmanager
    def empty(uid):
        assert uid == 499
        def unmounted():
            events.append('empty-checked'); assert not state['mounted']
        unmounted(); yield unmounted, lambda:events.append('parents-checked')
    monkeypatch.setattr(runtime.client, 'empty_mountpoint', empty)
    secret = 'SYNTHETIC_PRIVATE_中文'.encode()
    store = SimpleNamespace(value=binding.context)
    def get(account):
        events.append('secret-read'); assert account == binding.value['credential_account']
        return secret
    store.get = Mock(side_effect=get)
    monkeypatch.setattr(runtime.keychain, 'ServiceStore', lambda:store)
    def mount(value, password, **kw):
        events.append('mount')
        assert password == secret.decode() and value == binding.cfg
        if state['error']: raise ValueError('synthetic native failure')
        state['mounted'] = True
        return deepcopy(observation)
    monkeypatch.setattr(runtime.client, 'mount', mount)
    @contextmanager
    def location(value, **kw):
        assert state['mounted'] and value == binding.cfg
        yield SimpleNamespace(check=lambda:deepcopy(observation), directory_identity=deepcopy(directories))
    monkeypatch.setattr(runtime.storage, 'open_location', location)
    # The current native parser rejects exact non-loopback addresses. Protocol
    # tests explicitly simulate an accepted profile, not an actual OS boundary.
    monkeypatch.setattr(runtime.subprocess, 'run', lambda *a, **kw:SimpleNamespace(returncode=0))
    return SimpleNamespace(binding=binding, events=events, state=state, store=store,
                           observation=observation, directories=directories, secret=secret)


def test_cold_connection_checks_empty_location_before_one_secret_read(connector):
    result = runtime.connect(runtime.load_binding())
    assert result['credential_read'] is True and result['mount'] == connector.observation
    assert connector.events.index('empty-checked') < connector.events.index('secret-read') < connector.events.index('mount')
    assert connector.store.get.call_count == 1
    assert connector.secret.decode() not in json.dumps(result)


def test_existing_mount_does_not_read_credentials(connector):
    connector.state['mounted'] = True
    result = runtime.connect(runtime.load_binding())
    assert result['credential_read'] is False
    connector.store.get.assert_not_called()
    assert 'mount' not in connector.events


def test_native_failure_has_no_retry_or_cleanup(connector):
    connector.state['error'] = True
    with pytest.raises(ValueError): runtime.connect(runtime.load_binding())
    assert connector.events.count('mount') == connector.store.get.call_count == 1
    assert 'unmount' not in connector.events


def test_native_policy_rejection_stops_before_secret_worker(connector, monkeypatch):
    probe = Mock(return_value=SimpleNamespace(returncode=65))
    worker = Mock()
    monkeypatch.setattr(runtime.subprocess,'run',probe)
    monkeypatch.setattr(runtime.subprocess,'Popen',worker)
    with pytest.raises(ValueError, match='不可部署'):
        runtime.ensure_destination(Path(connector.binding.cfg['destination']))
    probe.assert_called_once()
    assert probe.call_args.args[0][-1] == '/usr/bin/true'
    assert '192.168.1.245:445' in probe.call_args.args[0][2]
    worker.assert_not_called(); connector.store.get.assert_not_called()


@pytest.mark.parametrize('raw', [b'', b'x'*129, b'invalid\x00secret', b'\xff', 'wrong type'])
def test_invalid_stored_secret_never_reaches_native_library(connector, raw):
    connector.store.get.side_effect = lambda account:raw
    with pytest.raises((ValueError, UnicodeError)): runtime.connect(runtime.load_binding())
    assert 'mount' not in connector.events


@pytest.mark.parametrize('kind', ['foreign','empty-check','lock'])
def test_invalid_mount_or_lock_fails_before_keychain(connector, monkeypatch, kind):
    if kind == 'foreign': monkeypatch.setattr(runtime.mounts,'_select',lambda path:{'filesystem':'nfs','flags':0,'mountpoint':str(runtime.cfg.MOUNT)})
    def refuse(*args): raise ValueError('synthetic refused')
    if kind == 'empty-check': monkeypatch.setattr(runtime.client,'empty_mountpoint',refuse)
    if kind == 'lock': monkeypatch.setattr(runtime,'connection_lock',refuse)
    with pytest.raises(ValueError): runtime.connect(runtime.load_binding())
    connector.store.get.assert_not_called()


def test_parent_uses_fixed_nas_sandbox_and_rechecks_actual_mount(connector, monkeypatch):
    captured = {}
    def start(command, **kw):
        captured.update(command=command, **kw)
        def communicate(raw):
            request = json.loads(raw)
            connector.state['mounted'] = True
            result = {'type':runtime.RESULT, **request, 'package_sha256':connector.binding.value['package_sha256'],
                'context_sha256':fixtures.CONTEXT, 'identity':connector.binding.identity,
                'mount':connector.observation, 'directory_identity':connector.directories, 'credential_read':True}
            return contract.canonical(result), b''
        return SimpleNamespace(returncode=0, communicate=communicate)
    monkeypatch.setattr(runtime.subprocess,'Popen',start)
    runtime.ensure_destination(Path(connector.binding.cfg['destination']))
    command = captured.pop('command')
    assert command[:3] == ['/usr/bin/sandbox-exec','-p',contract.profile(fixtures.value(),fixtures.CONTEXT,499)]
    assert command[-3:] == ['-I','-B',str(connector.binding.tree/contract.ENTRY)]
    assert captured['close_fds'] and captured['pass_fds'] == () and captured['umask'] == 0o077
    assert set(captured['env']) == {'HOME','PATH','LANG','ERP_HOME_REHEARSAL'}
    assert connector.secret.decode() not in repr(captured)+repr(command)
    connector.store.get.assert_not_called()  # Parent never fetches the NAS secret.


def test_worker_requires_isolation_and_exact_executing_tree(connector, monkeypatch):
    monkeypatch.setattr(runtime,'sys',SimpleNamespace(platform='darwin',flags=SimpleNamespace(isolated=True),dont_write_bytecode=True))
    monkeypatch.setattr(runtime,'__file__',str(connector.binding.tree/contract.MODULE))
    b = runtime.load_binding()
    request = dict(challenge='1'*32, control_sha256=b.control_sha, contract_sha256=b.control['contract_sha256'])
    with pytest.raises(ValueError): runtime.run(request, script='/wrong', executable='/wrong')
    connector.store.get.assert_not_called()
    result = runtime.run(request, script=connector.binding.tree/contract.ENTRY,
                         executable=connector.binding.tree/'runtime/bin/python3.12')
    assert result['challenge'] == '1'*32 and result['identity'] == connector.binding.identity


def test_prepare_phase_only_connects_approved_smb_and_readback_does_not(connector, monkeypatch):
    from desktop_assistant import mac_backup_storage as remote
    from unittest.mock import call
    anchor = {'synthetic':'checked'}
    before = Mock(side_effect=[ValueError('not mounted'),anchor])
    monkeypatch.setattr(remote,'before_write',before)
    monkeypatch.setattr(remote,'required',lambda:True)
    monkeypatch.setattr(remote,'_contract',lambda:(connector.binding.cfg,'a'*64,connector.binding.context))
    ensure = Mock(); monkeypatch.setattr(runtime,'ensure_destination',ensure)
    path = Path(connector.binding.cfg['destination'])
    assert remote.prepare_destination(path) == anchor
    assert before.call_args_list == [call(path),call(path)]
    ensure.assert_called_once_with(path)
    before.side_effect = [ValueError('not mounted')]
    monkeypatch.setattr(remote,'_contract',lambda:({'type':'nfs'},'a'*64,connector.binding.context))
    with pytest.raises(ValueError): remote.prepare_destination(path)
    assert ensure.call_count == 1


@pytest.mark.parametrize('failure', ['exit','oversize','challenge','identity','contract','changed-record','changed-mount','interrupted'])
def test_parent_rejects_failed_or_inconsistent_worker_without_retry(connector, monkeypatch, failure):
    calls = []
    saved = {}
    def communicate(raw):
        calls.append(raw)
        if raw is not None: saved.update(json.loads(raw))
        if failure == 'interrupted' and len(calls) == 1: raise KeyboardInterrupt
        connector.state['mounted'] = True
        result = {'type':runtime.RESULT, **saved, 'package_sha256':connector.binding.value['package_sha256'],
            'context_sha256':fixtures.CONTEXT, 'identity':deepcopy(connector.binding.identity),
            'mount':deepcopy(connector.observation), 'directory_identity':connector.directories, 'credential_read':True}
        if failure == 'oversize': return b'x'*(runtime.LIMIT+1), b''
        if failure == 'challenge': result['challenge'] = '0'*32
        if failure == 'identity': result['identity']['euid'] = 0
        if failure == 'contract': result['contract_sha256'] = 'f'*64
        if failure == 'changed-record': connector.binding.records[contract.CONTROL] += b'\n'
        if failure == 'changed-mount': result['directory_identity'] = deepcopy(connector.directories); result['directory_identity'][0]['ino'] += 10
        return contract.canonical(result), b'synthetic private stderr must not escape'
    process = SimpleNamespace(returncode=1 if failure == 'exit' else 0, communicate=communicate)
    start = Mock(return_value=process); monkeypatch.setattr(runtime.subprocess,'Popen',start)
    with pytest.raises(ValueError) as error: runtime.ensure_destination(Path(connector.binding.cfg['destination']))
    assert 'private stderr' not in str(error.value)
    start.assert_called_once()
    assert len(calls) == (2 if failure == 'interrupted' else 1)
    if failure == 'interrupted': assert calls[1] is None
    connector.store.get.assert_not_called()


@pytest.fixture
def actual_lock(tmp_path, monkeypatch):
    home = tmp_path/'private-home'; home.mkdir(mode=0o700)
    lock = home/'smb-connection.lock'; lock.write_bytes(b'0'); lock.chmod(0o600)
    monkeypatch.setattr(runtime,'LOCK',lock)
    # POSIX files, inode checks and flock are real. ACL inspection is native on
    # macOS; other platforms cannot implement the Darwin ACL call.
    if runtime.sys.platform != 'darwin': monkeypatch.setattr(runtime.keychain,'_no_acl',lambda fd:None)
    return lock, {'uid':os.getuid(),'gid':os.getgid()}


def test_existing_lock_excludes_a_second_opener_and_is_preserved(actual_lock):
    lock, context = actual_lock; original = lock.stat()
    with runtime.connection_lock(context):
        with pytest.raises(ValueError):
            with runtime.connection_lock(context): pytest.fail('concurrent owner accepted')
    with runtime.connection_lock(context): pass
    assert lock.stat().st_ino == original.st_ino and lock.read_bytes() == b'0'


@pytest.mark.parametrize('kind', ['missing','symlink','hardlink','mode','content','oversize'])
def test_bad_lock_is_not_repaired(actual_lock, kind):
    lock, context = actual_lock
    if kind in ('missing','symlink'): lock.unlink()
    if kind == 'symlink': lock.symlink_to(lock.parent/'absent')
    if kind == 'hardlink': os.link(lock,lock.parent/'other-name')
    if kind == 'mode': lock.chmod(0o640)
    if kind == 'content': lock.write_bytes(b'1')
    if kind == 'oversize': lock.write_bytes(b'00')
    with pytest.raises((ValueError,OSError)):
        with runtime.connection_lock(context): pytest.fail('bad lock accepted')
    if kind == 'missing': assert not lock.exists()
    if kind == 'symlink': assert lock.is_symlink()


def test_lock_replacement_during_operation_fails_and_preserves_both_files(actual_lock):
    lock, context = actual_lock
    with pytest.raises(ValueError):
        with runtime.connection_lock(context):
            lock.rename(lock.parent/'original')
            lock.write_bytes(b'0'); lock.chmod(0o600)
    assert lock.exists() and (lock.parent/'original').exists()
