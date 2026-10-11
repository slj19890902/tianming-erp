"""Real signed synthetic trees, simulated root/UID/launchd; no system changes."""
from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from desktop_assistant import mac_system_service as m
from desktop_assistant import mac_keychain_install as b
from desktop_assistant import mac_release_publish as publication
from desktop_assistant.operation_lock import BUSY
from tests.desktop_assistant.test_mac_release_publish import case, make_package, publish

pytestmark = pytest.mark.skipif(sys.platform != 'darwin', reason='Mac system service contract')


@pytest.fixture
def installed_case(case, monkeypatch):
    package = case.root/'system-runtime.zip'
    raw, manifest = make_package(package, case.key,
        metadata={'runtime_capabilities': {'immutable_execution_v1': 1, 'system_service_v1': 1}},
        additions={m.ENTRY: b'synthetic supervisor', 'desktop_assistant/mac_system_service.py': b'synthetic service'})
    case.plan = publication.prepare(package, case.public, case.context_raw, case.code,
                                   publisher_sha256=b.digest(case.public))
    case.package, case.manifest, case.manifest_raw = package, manifest, raw
    publish(case)
    plists = case.root/'LaunchDaemons'; plists.mkdir()
    for name, value in {'BASE': case.base, 'ROOT': case.base/'instance', 'CONTROL': case.base/'system-service.json',
                        'PLISTS': plists, 'PLIST': plists/'cn.tianming.erp.plist'}.items():
        monkeypatch.setattr(m, name, value)
    from app.core import mac_service_keychain as keychain
    identity = SimpleNamespace(_administrator=lambda: None, _parent=lambda: None)
    context_check = Mock()
    monkeypatch.setattr(b, 'modules', lambda: (identity, SimpleNamespace(
        FIELDS=keychain.FIELDS, TYPE=keychain.TYPE, _identity=context_check)))
    events = []; registry = {'present': False}
    def launch(arguments):
        events.append(tuple(arguments))
        if arguments[0] == 'print':
            return (0, b'job', b'') if registry['present'] else (
                113, b'', b'Bad request.\nCould not find service "cn.tianming.erp" in domain for system')
        if arguments[0] == 'bootstrap': registry['present'] = True
        if arguments[0] == 'bootout': registry['present'] = False
        return 0, b'', b''
    monkeypatch.setattr(m, 'launch', launch)
    def worker(meta, action):
        events.append(('worker', action))
        if action == 'stop':
            with pytest.raises(ValueError, match='已停用'): m.check_start()
    monkeypatch.setattr(m, 'worker', worker)
    def create_log(path, gid, *, readable=False):
        events.append(('create-log', path.name, gid, readable))
        with path.open('xb') as f: f.write(b'')
        path.chmod(0o660 if readable else 0o620)
    monkeypatch.setattr(m, 'create_log', create_log)
    return SimpleNamespace(case=case, events=events, registry=registry,
                           context_check=context_check, launch=launch, worker=worker)


def plan_apply(c, action):
    value = m.make_plan(action, c.case.plan['package_sha256'])
    raw = b.canonical(value)
    return m.apply(raw, b.digest(raw))


def test_install_pins_immutable_runtime_and_preserves_stopped_state(installed_case):
    c = installed_case
    result = plan_apply(c, 'install')
    spec = plistlib.loads(m.PLIST.read_bytes())
    assert spec['UserName'] == spec['GroupName'] == '_tianmingerp'
    assert spec['InitGroups'] is True
    assert spec['ProgramArguments'][:3] == [str(c.case.base/'published'/c.case.plan['package_sha256']/'migration/runtime/bin/python3.12'), '-I', '-B']
    assert spec['ExitTimeOut'] >= 75+60+60 and spec['AbandonProcessGroup'] is True
    assert spec['EnvironmentVariables']['ERP_HOME_REHEARSAL'] == '1'
    assert spec['WorkingDirectory'] == str(m.ROOT)
    assert '/Users/' not in str(spec['ProgramArguments']) or str(c.case.root) in spec['ProgramArguments'][0]
    assert c.events.index(('worker', 'check')) < c.events.index(('bootstrap', 'system', str(m.PLIST)))
    assert result['registered'] and not result['health_verified'] and not result['factory_activated']
    assert not result['system_keychain_session_verified'] and not result['logout_reboot_verified']
    assert json.loads(m.CONTROL.read_bytes())['enabled'] is True
    assert ('create-log', 'system-service.stdout.log', 499, False) in c.events
    assert ('create-log', 'system-service.stderr.log', 499, True) in c.events
    assert not (m.ROOT/'state.json').exists()  # Fake preflight did not restore/start/create data.
    m.check_start()


def test_disable_closes_start_gate_then_stops_before_bootout_and_can_reenable(installed_case):
    c = installed_case; plan_apply(c, 'install')
    archive = (c.case.base/'published'/c.case.plan['package_sha256']/'package.zip').read_bytes()
    c.events.clear(); plan_apply(c, 'disable')
    assert c.events.index(('worker', 'stop')) < c.events.index(('bootout', 'system', str(m.PLIST)))
    assert ('disable', 'system/'+m.LABEL) in c.events
    with pytest.raises(ValueError, match='已停用'): m.check_start()
    assert m.PLIST.exists() and (c.case.base/'published'/c.case.plan['package_sha256']/'package.zip').read_bytes() == archive
    c.events.clear(); plan_apply(c, 'enable')
    assert c.events.index(('worker', 'check')) < c.events.index(('enable', 'system/'+m.LABEL))
    assert ('bootstrap', 'system', str(m.PLIST)) in c.events
    m.check_start()


@pytest.mark.parametrize('fault', ['expired', 'extra', 'wrong-sha', 'wrong-code', 'registry', 'control', 'plist'])
def test_bad_install_plan_has_no_system_writes(installed_case, monkeypatch, fault):
    c = installed_case; plan = m.make_plan('install', c.case.plan['package_sha256'])
    if fault == 'expired': plan['expires_at'] = plan['created_at']
    elif fault == 'extra': plan['activate_factory'] = True
    elif fault == 'wrong-code': plan['code_manifest_sha256'] = 'f'*64
    elif fault == 'registry': plan['registry_sha256'] = 'f'*64
    elif fault == 'control': plan['control_sha256'] = 'f'*64
    elif fault == 'plist': plan['plist_sha256'] = 'f'*64
    raw = b.canonical(plan)
    lock=c.case.base/'publication.lock'
    if fault in {'expired','extra','wrong-sha','wrong-code','control'}:lock.unlink()
    with pytest.raises(ValueError): m.apply(raw, 'f'*64 if fault == 'wrong-sha' else b.digest(raw))
    if fault in {'expired','extra','wrong-sha','wrong-code','control'}:assert not lock.exists()
    assert not m.CONTROL.exists() and not m.PLIST.exists()
    assert not c.events


@pytest.mark.parametrize('fault', ['live-job', 'check-failed', 'native-identity', 'unknown-launchctl'])
def test_preflight_failure_does_not_create_pending_or_launch(installed_case, monkeypatch, fault):
    c = installed_case
    plan = m.make_plan('install', c.case.plan['package_sha256']); raw = b.canonical(plan)
    if fault == 'live-job': c.registry['present'] = True
    elif fault == 'check-failed': monkeypatch.setattr(m, 'worker', Mock(side_effect=ValueError('failed')))
    elif fault == 'native-identity': c.context_check.side_effect = ValueError('identity failed')
    else: monkeypatch.setattr(m, 'launch', lambda _: (5, b'', b'unknown native failure'))
    with pytest.raises(ValueError): m.apply(raw, b.digest(raw))
    assert not list(c.case.base.glob('system-service-*.pending.json'))
    assert not m.CONTROL.exists() and not m.PLIST.exists()
    assert not any(e[0] in {'bootstrap', 'bootout', 'enable', 'disable'} for e in c.events)


@pytest.mark.parametrize('fault', ['stop-failed', 'bootout-failed', 'disable-failed'])
def test_partial_disable_keeps_disabled_gate_and_all_evidence(installed_case, monkeypatch, fault):
    c = installed_case; plan_apply(c, 'install'); c.events.clear()
    if fault == 'stop-failed': monkeypatch.setattr(m, 'worker', Mock(side_effect=ValueError('busy')))
    else:
        original = c.launch
        def launch(args):
            if args[0] == fault.split('-')[0]:
                c.events.append(tuple(args)); return 5, b'', b'failed'
            return original(args)
        monkeypatch.setattr(m, 'launch', launch)
    with pytest.raises(ValueError): plan_apply(c, 'disable')
    with pytest.raises(ValueError, match='已停用'): m.check_start()
    assert m.PLIST.exists() and list(c.case.base.glob('system-service-*.pending.json'))
    if fault == 'stop-failed': assert not any(e[0] == 'bootout' for e in c.events)
    if fault != 'disable-failed': assert not any(e[0] == 'disable' for e in c.events)


@pytest.mark.parametrize('fault', ['stop', 'bootout', 'disable'])
def test_new_disable_plan_finishes_partial_disable_without_reopening_gate(installed_case, monkeypatch, fault):
    c = installed_case; plan_apply(c, 'install')
    before = {p: p.read_bytes() for p in m.BASE.glob('system-service-*.verified.json')}
    if fault == 'stop':
        monkeypatch.setattr(m, 'worker', Mock(side_effect=ValueError(BUSY)))
    else:
        monkeypatch.setattr(m, 'launch', lambda args: (5, b'', b'failed')
                           if args[0] == fault else c.launch(args))
    with pytest.raises(ValueError): plan_apply(c, 'disable')
    old_pending = {p: p.read_bytes() for p in m.BASE.glob('system-service-*.pending.json')}
    gate = m.CONTROL.read_bytes()
    assert json.loads(gate)['enabled'] is False
    monkeypatch.setattr(m, 'worker', c.worker)
    monkeypatch.setattr(m, 'launch', c.launch)
    c.events.clear()
    plan = m.make_plan('disable', c.case.plan['package_sha256'])
    assert plan['control_sha256'] == b.digest(gate)
    raw = b.canonical(plan)
    result = m.apply(raw, b.digest(raw))
    assert not result['registered'] and not c.registry['present']
    assert m.CONTROL.read_bytes() == gate
    assert all(p.read_bytes() == content for p, content in {**before, **old_pending}.items())
    assert not any(e[0] in {'enable', 'bootstrap'} for e in c.events)
    with pytest.raises(FileExistsError): m.apply(raw, b.digest(raw))  # Fresh approval, no replay.


def test_new_disable_plan_still_rejects_changed_control(installed_case):
    c = installed_case; plan_apply(c, 'install'); plan_apply(c, 'disable')
    plan = m.make_plan('disable', c.case.plan['package_sha256']); raw = b.canonical(plan)
    m._set_control(m.metadata(c.case.plan['package_sha256']), True, 'd'*32)
    c.events.clear()
    with pytest.raises(ValueError, match='状态变化'): m.apply(raw, b.digest(raw))
    assert not c.events


@pytest.mark.parametrize('readable,mode', [(False, 0o620), (True, 0o660)])
def test_logs_preserve_root_owner_and_stderr_group_read_write(tmp_path, monkeypatch, readable, mode):
    path = tmp_path/'synthetic.log'
    ownership = Mock(); monkeypatch.setattr(m.os, 'fchown', ownership)
    checks = Mock(); monkeypatch.setattr(publication, 'guard', checks)
    m.create_log(path, os.getgid(), readable=readable)
    ownership.assert_called_once()
    assert ownership.call_args.args[1:] == (0, os.getgid())
    assert path.stat().st_mode & 0o777 == mode
    checks.assert_called_once_with(path, mode=mode)
    # Real file IO as this test UID; group permission contract is checked above.
    # It does not claim a root-owned file was opened by the actual service UID.
    fd = os.open(path, os.O_RDWR if readable else os.O_WRONLY)
    try: os.write(fd, b'synthetic log')
    finally: os.close(fd)
    with pytest.raises(FileExistsError): m.create_log(path, os.getgid(), readable=readable)


def test_failed_bootstrap_retains_files_and_never_claims_registration(installed_case, monkeypatch):
    c = installed_case
    original = c.launch
    monkeypatch.setattr(m, 'launch', lambda args: (5, b'', b'failed') if args[0] == 'bootstrap' else original(args))
    with pytest.raises(ValueError, match='注册未确认'): plan_apply(c, 'install')
    assert m.CONTROL.exists() and m.PLIST.exists()
    assert not list(c.case.base.glob('system-service-*.verified.json'))
    with pytest.raises(ValueError): plan_apply(c, 'install')


def test_changed_owned_plist_is_not_stopped_or_overwritten(installed_case):
    c = installed_case; plan_apply(c, 'install'); c.events.clear()
    m.PLIST.write_bytes(b'other service')
    with pytest.raises(ValueError): plan_apply(c, 'disable')
    assert not c.events and m.PLIST.read_bytes() == b'other service'


@pytest.mark.parametrize('fault', ['context', 'publisher', 'extra-field', 'boolean', 'plist'])
def test_start_gate_refuses_damaged_root_control(installed_case, fault):
    c = installed_case; plan_apply(c, 'install')
    record = json.loads(m.CONTROL.read_bytes())
    if fault == 'context': record['context_sha256'] = 'f'*64
    elif fault == 'publisher': record['publisher_sha256'] = 'f'*64
    elif fault == 'extra-field': record['allow'] = True
    elif fault == 'boolean': record['enabled'] = 1
    else: m.PLIST.write_bytes(b'damaged')
    m.CONTROL.write_bytes(b.canonical(record))
    with pytest.raises(ValueError): m.check_start()


def test_metadata_refuses_old_signed_runtime_without_new_capability(case, monkeypatch):
    publish(case)
    monkeypatch.setattr(m, 'BASE', case.base)
    from app.core import mac_service_keychain as keychain
    monkeypatch.setattr(b, 'modules', lambda: (SimpleNamespace(_parent=lambda: None), keychain))
    with pytest.raises(ValueError, match='尚未实现系统服务'): m.metadata(case.plan['package_sha256'])


@pytest.mark.parametrize('interrupted', [False, True])
def test_real_worker_command_is_fresh_dedicated_exec_and_interrupt_waits(installed_case, monkeypatch, interrupted):
    c = installed_case; meta = m.metadata(c.case.plan['package_sha256'])
    # Fixture substitutes worker for orchestration tests; reload just this callable.
    import importlib
    real_worker = importlib.reload(m).worker
    monkeypatch.setattr(m, 'BASE', c.case.base); monkeypatch.setattr(m, 'ROOT', c.case.base/'instance')
    calls=[]
    class Process:
        returncode=0
        def communicate(self):
            calls.append('wait')
            if interrupted and len(calls)==1: raise KeyboardInterrupt()
            return b.canonical(self.result), b''
        def kill(self): pytest.fail('must not kill a maintenance worker')
    def popen(args, **kwargs):
        assert kwargs['user'] == kwargs['group'] == 499 and kwargs['extra_groups'] == []
        assert kwargs['close_fds'] and kwargs['pass_fds'] == () and kwargs['umask'] == 0o077
        assert kwargs['cwd'] == '/' and kwargs['env']['ERP_HOME_REHEARSAL'] == '1'
        assert args[1:3] == ['-I','-B'] and args[-3] == 'check'
        proc=Process(); proc.result={'type':'tianming.system-service-worker.v1','action':'check',
            'challenge':args[-1],'package_sha256':meta['package'],'identity':b._expected(499,499),
            'manual_stop':True,'process_stopped':True,'home_rehearsal':True,'keychain_canary_verified':True}
        return proc
    monkeypatch.setattr(m.subprocess,'Popen',popen)
    if interrupted:
        with pytest.raises(ValueError,match='已观察'): real_worker(meta,'check')
        assert len(calls)==2
    else: real_worker(meta,'check')


class RuntimeManager:
    def __init__(self, root):
        self.root=root;self.value={'current':'a'*64,'manual_stop':True};self.running=None
        self.actions=[];self.busy=False
    @property
    def state(self): return deepcopy(self.value)
    @contextmanager
    def lock(self):
        if self.busy: raise ValueError(BUSY)
        self.actions.append('lock');yield;self.actions.append('unlock')
    def _approve_execution(self,*ids):self.actions.append(('approve',ids))
    def _published_release(self, identity):
        return None, {'runtime_capabilities': {'system_service_v1': 1}}
    def _process(self):return self.running
    def stop(self):
        assert json.loads((self.root/'state.json').read_bytes())['manual_stop'] is True
        self.actions.append('stop');self.running=None


@pytest.mark.parametrize('fault',['none','running','not-stopped','failed','pending','busy'])
def test_runtime_preflight_never_starts_or_repairs_instance(tmp_path, monkeypatch, fault):
    manager=RuntimeManager(tmp_path)
    args=SimpleNamespace(system_action='check',challenge='a'*32,supervisor_package='b'*64)
    monkeypatch.setattr(b,'_actual',lambda:b._expected(499,499))
    if fault=='running':manager.running=object()
    elif fault=='not-stopped':manager.value['manual_stop']=False
    elif fault=='failed':manager.value['operation']='migration_failed'
    elif fault=='pending':manager.value['onboarding_pending']=True
    elif fault=='busy':manager.busy=True
    if fault=='none':assert m.runtime_worker(manager,args,{})['process_stopped']
    else:
        with pytest.raises(ValueError):m.runtime_worker(manager,args,{})
    assert not (tmp_path/'state.json').exists() and 'stop' not in manager.actions


def test_runtime_stop_sets_manual_stop_under_lock_before_graceful_stop(tmp_path,monkeypatch):
    manager=RuntimeManager(tmp_path);manager.value['manual_stop']=False;manager.running=object()
    args=SimpleNamespace(system_action='stop',challenge='a'*32,supervisor_package='b'*64)
    monkeypatch.setattr(b,'_actual',lambda:b._expected(499,499))
    assert m.runtime_worker(manager,args,{})['process_stopped']
    assert manager.actions==['lock','stop','unlock']


@pytest.mark.parametrize('failure', ['transient', 'persistent', 'unrelated'])
def test_stop_waits_only_for_lock_and_preserves_failure_scene(tmp_path, monkeypatch, failure):
    manager = RuntimeManager(tmp_path); manager.value['manual_stop'] = False
    args = SimpleNamespace(system_action='stop', challenge='a'*32, supervisor_package='b'*64)
    monkeypatch.setattr(b, '_actual', lambda: b._expected(499, 499))
    attempts = []; sleeps = []; clock = [0]
    original = manager.lock
    @contextmanager
    def lock():
        attempts.append('attempt')
        if failure == 'unrelated': raise ValueError('invalid lock')
        if failure == 'persistent' or len(attempts) == 1: raise ValueError(BUSY)
        with original(): yield
    monkeypatch.setattr(manager, 'lock', lock)
    monkeypatch.setattr(m.time, 'monotonic', lambda: clock[0])
    def sleep(seconds):
        sleeps.append(seconds); clock[0] += 75
    monkeypatch.setattr(m.time, 'sleep', sleep)
    if failure == 'transient':
        assert m.runtime_worker(manager, args, {})['process_stopped']
        assert len(attempts) == 2 and len(sleeps) == 1
        assert manager.actions == ['lock', 'stop', 'unlock']
    else:
        with pytest.raises(ValueError, match=BUSY if failure == 'persistent' else 'invalid lock'):
            m.runtime_worker(manager, args, {})
        assert not (tmp_path/'state.json').exists() and not manager.actions
        assert len(attempts) == (2 if failure == 'persistent' else 1)


def test_busy_from_stop_body_is_not_retried(tmp_path, monkeypatch):
    manager = RuntimeManager(tmp_path)
    stop = Mock(side_effect=ValueError(BUSY)); monkeypatch.setattr(manager, 'stop', stop)
    monkeypatch.setattr(m.time, 'sleep', lambda _: pytest.fail('must not retry stop body'))
    args = SimpleNamespace(system_action='stop', challenge='a'*32, supervisor_package='b'*64)
    with pytest.raises(ValueError, match=BUSY): m.runtime_worker(manager, args, {})
    stop.assert_called_once()
    assert json.loads((tmp_path/'state.json').read_bytes())['manual_stop'] is True


@pytest.mark.parametrize('fault', ['none', 'root', 'extra-group', 'wrong-root', 'wrong-public',
    'wrong-script', 'wrong-exe', 'missing-lock', 'directory-link', 'world-data', 'bad-capability',
    'disabled-stop', 'enabled-stop', 'keychain-failed'])
def test_runtime_configuration_checks_actual_identity_entry_and_existing_private_tree(installed_case,monkeypatch,fault):
    c=installed_case; plan_apply(c,'install')
    from desktop_assistant import mac_published_runtime as published
    from app.core import mac_service_keychain as keychain
    for name in ('releases','control','backups','packages','staging'):
        (m.ROOT/name).mkdir(mode=0o700)
    for path,raw in ((m.ROOT/'state.json',b'{}'),(m.ROOT/'control/operation.lock',b'0')):
        path.write_bytes(raw);path.chmod(0o600)
    tree=c.case.base/'published'/c.case.plan['package_sha256']/'migration'
    args=SimpleNamespace(root=m.ROOT,public_key=c.case.base/'publisher-public.pem',
        publisher_sha256=b.digest(c.case.public),supervisor_package=c.case.plan['package_sha256'],system_action='run')
    script=tree/m.ENTRY; executable=tree/'runtime/bin/python3.12'
    actual=b._expected(499,499);manifest=deepcopy(c.case.manifest)
    monkeypatch.setattr(keychain,'load_context',lambda:c.case.context)
    monkeypatch.setattr(keychain,'CONTEXT',c.case.base/'keychain-context.json')
    monkeypatch.setattr(published,'PUBLIC',c.case.base/'publisher-public.pem')
    monkeypatch.setattr(published,'_read',lambda path:path.read_bytes())
    resolver=Mock(return_value=(tree,manifest));monkeypatch.setattr(published,'resolve',resolver)
    monkeypatch.setattr(b,'_actual',lambda:actual)
    canary=Mock();monkeypatch.setattr(m,'verify_keychain',canary)
    @contextmanager
    def checked(path,**kw):
        publication.guard(path,**kw)
        fd=os.open(path,os.O_RDONLY)
        try:yield fd
        finally:os.close(fd)
    monkeypatch.setattr(keychain,'_checked_path',checked)
    if fault=='root':actual['uid']=actual['euid']=0
    elif fault=='extra-group':actual['kernel_groups'].append(0)
    elif fault=='wrong-root':args.root=c.case.root
    elif fault=='wrong-public':args.publisher_sha256='f'*64
    elif fault=='wrong-script':script=c.case.root/'other.py'
    elif fault=='wrong-exe':executable=Path('/usr/bin/python3')
    elif fault=='missing-lock':(m.ROOT/'control/operation.lock').unlink()
    elif fault=='directory-link':
        (m.ROOT/'staging').rmdir();(m.ROOT/'staging').symlink_to(c.case.root,target_is_directory=True)
    elif fault=='world-data':(m.ROOT/'shared').chmod(0o755)
    elif fault=='bad-capability':manifest['runtime_capabilities']['system_service_v1']=True
    elif fault=='keychain-failed':canary.side_effect=ValueError('unavailable keychain')
    elif fault in {'disabled-stop','enabled-stop'}:
        args.system_action='stop'
        if fault=='disabled-stop':m._set_control(m.metadata(args.supervisor_package),False,'a'*32)
    before={str(p) for p in m.ROOT.rglob('*')}
    if fault in {'none','disabled-stop'}:
        context,public,control=m.runtime_configuration(args,script=script,executable=executable)
        assert context==c.case.context and public==c.case.public
        assert control['enabled']==(fault=='none')
        resolver.assert_called_once_with(m.ROOT,args.supervisor_package,c.case.public,kind='migration')
        if fault=='none':canary.assert_called_once_with(c.case.context)
        else:canary.assert_not_called()
    else:
        with pytest.raises((ValueError,OSError)):m.runtime_configuration(args,script=script,executable=executable)
    assert {str(p) for p in m.ROOT.rglob('*')}==before


def test_system_entry_rejects_before_manager_construction(monkeypatch):
    from desktop_assistant import service_supervisor as supervisor
    from desktop_assistant import manager
    monkeypatch.setattr(sys,'argv',['entry','--root','/Library/TianmingERP/instance',
        '--public-key','/Library/TianmingERP/publisher-public.pem','--publisher-sha256','a'*64,
        '--system-action','run','--supervisor-package','b'*64])
    monkeypatch.setattr(m,'runtime_configuration',Mock(side_effect=ValueError('identity invalid')))
    constructor=Mock();monkeypatch.setattr(manager,'Manager',constructor)
    with pytest.raises(ValueError,match='identity invalid'):supervisor.main()
    constructor.assert_not_called()


def test_keychain_check_uses_only_existing_installation_canary(monkeypatch):
    from app.core import mac_service_keychain as keychain
    calls=[]; api=object(); context={'synthetic':'context'}
    monkeypatch.setattr(keychain,'Security',lambda:api)
    @contextmanager
    def unlocked(actual,value):
        assert actual is api and value is context
        calls.append('canary-open');yield object();calls.append('closed')
    monkeypatch.setattr(keychain,'unlocked',unlocked)
    monkeypatch.setattr(keychain,'ServiceStore',lambda:pytest.fail('must not read operational credentials'))
    m.verify_keychain(context)
    assert calls==['canary-open','closed']


@pytest.mark.parametrize('enabled',[False,True])
def test_system_entry_dispatches_home_supervision_or_disabled_exit(installed_case,monkeypatch,enabled):
    c=installed_case
    from desktop_assistant import service_supervisor as supervisor
    from desktop_assistant import manager
    (m.ROOT/'control').mkdir(mode=0o700)
    monkeypatch.setattr(sys,'argv',['entry','--root',str(m.ROOT),'--public-key',str(c.case.base/'publisher-public.pem'),
        '--publisher-sha256',b.digest(c.case.public),'--system-action','run','--supervisor-package',c.case.plan['package_sha256']])
    monkeypatch.setattr(m,'runtime_configuration',lambda *a,**kw:(c.case.context,c.case.public,{'enabled':enabled}))
    monkeypatch.setattr(b,'_actual',lambda:b._expected(499,499))
    monkeypatch.setenv('ERP_HOME_REHEARSAL','0')
    fake_manager=object();constructor=Mock(return_value=fake_manager)
    monkeypatch.setattr(manager,'Manager',constructor)
    run=Mock();monkeypatch.setattr(supervisor,'supervise',run)
    monkeypatch.setattr(supervisor.signal,'signal',lambda *a:None)
    supervisor.main()
    assert os.environ['ERP_HOME_REHEARSAL']=='1'
    if enabled:
        assert run.call_args.args[0] is fake_manager
        assert run.call_args.kwargs['system_identity']==b._expected(499,499)
        assert run.call_args.kwargs['keychain_canary_verified'] is True
    else:
        run.assert_not_called()
        status=json.loads((m.ROOT/'control/service-status.json').read_bytes())
        assert status['status']=='system-disabled' and status['home_rehearsal'] is True
        assert status['requires_user_login'] is False


def test_personal_entry_still_checks_publisher_before_manager(monkeypatch,tmp_path):
    from desktop_assistant import service_supervisor as supervisor
    from desktop_assistant import manager,mac_service
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    public=tmp_path/'public.pem'
    public.write_bytes(Ed25519PrivateKey.generate().public_key().public_bytes(
        serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo))
    monkeypatch.setattr(sys,'argv',['entry','--root',str(tmp_path),'--public-key',str(public),'--publisher-sha256','a'*64])
    monkeypatch.setattr(mac_service,'local_root',lambda _:tmp_path)
    constructor=Mock();monkeypatch.setattr(manager,'Manager',constructor)
    with pytest.raises(ValueError,match='指纹不匹配'):supervisor.main()
    constructor.assert_not_called()


def test_system_module_uses_real_system_python_stdlib_only():
    repo=Path(__file__).resolve().parents[2]
    code="import sys;sys.path.insert(0,sys.argv[1]);import desktop_assistant.mac_system_service;assert 'cryptography' not in sys.modules;assert 'app' not in sys.modules;print('stdlib_only')"
    result=subprocess.run(['/usr/bin/python3','-I','-B','-S','-c',code,str(repo)],capture_output=True,text=True,check=True)
    assert result.stdout.strip()=='stdlib_only'
