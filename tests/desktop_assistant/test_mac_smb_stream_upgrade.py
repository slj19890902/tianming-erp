"""Synthetic runtime upgrades: no installed service, NAS or real credentials."""
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import json
import pytest

from desktop_assistant import mac_smb_stream_setup as s
from desktop_assistant import mac_system_upgrade as u, mac_published_runtime as published
from desktop_assistant import mac_smb_stream_transport as transport, mac_smb_stream_session as session
from app.core import mac_service_keychain as keychain
from test_mac_smb_stream_setup import planned

REAL_UPGRADE_PATHS = s._upgrade_paths


@pytest.fixture
def upgrade(planned,monkeypatch):
    a=planned
    monkeypatch.setattr(s.service,'CONTROL',a.base/'system-service.json')
    monkeypatch.setattr(keychain,'CONTEXT',a.base/'keychain-context.json')
    monkeypatch.setattr(s.publication,'read_record',lambda p:p.read_bytes())
    monkeypatch.setattr(s.service,'_program_manifest',lambda meta:{'runtime_capabilities':{s.UPGRADE_CAPABILITY:1}})
    previous=s.control(a.plan,a.digest)
    a.plan.update(type=s.UPGRADE_TYPE,previous_control=previous,package_sha256='1'*64,
                  supervisor_sha256=s.cfg.digest(b'synthetic supervisor'),activation_sha256=None)
    a.raw=s.boot.canonical(a.plan);a.digest=s.cfg.digest(a.raw)
    s.cfg.CONTROL.write_bytes(s.boot.canonical(previous))
    s.cfg.CONTRACT.write_bytes(s.boot.canonical(a.plan['contract']))
    s.service.CONTROL.write_bytes(b'synthetic supervisor')
    a.state=b'synthetic unchanged ERP state';a.events=[]
    monkeypatch.setattr(u,'_entry',lambda:None)
    monkeypatch.setattr(u,'verify_code',lambda:'d'*64)
    monkeypatch.setattr(u,'_publication',lambda:nullcontext())
    monkeypatch.setattr(u,'_locked',lambda pin:nullcontext())
    monkeypatch.setattr(s,'_upgrade_paths',lambda plan:nullcontext(dict(base=a.base,root=a.base,operation='lock',check=lambda:None)))
    monkeypatch.setattr(u,'_state',lambda *args:(a.state,{}))
    monkeypatch.setattr(s.publication,'sync_directory',lambda p:None)
    def write(path,raw,mode=0o600):
        a.events.append(path.name)
        with path.open('xb') as f:f.write(raw)
    monkeypatch.setattr(s.boot,'_write',write)
    def cas(parent,name,previous,replacement,temporary):
        assert (parent/name).read_bytes()==previous
        a.events.append('commit');(parent/name).write_bytes(replacement)
    monkeypatch.setattr(u,'_cas',cas)
    def worker(meta,raw):
        assert s.upgrade_validate(raw,a.digest,started=True)==(a.plan,a.meta)
        a.events.append('probe')
        return s.upgrade_reply(dict(type=s.UPGRADE_RESULT,challenge='f'*32,plan_sha256=a.digest,
            package_sha256=a.plan['package_sha256'],identity=s.boot._expected(499,499),
            proof=a.proof,credential_changed=False,backup_authenticated=False),a.plan,a.meta,'f'*32)
    monkeypatch.setattr(s,'upgrade_worker',worker)
    return a


def test_upgrade_commits_only_after_probe_and_keeps_install_identity(upgrade):
    a=upgrade;before=deepcopy(a.plan['previous_control'])
    result=s.apply_upgrade(a.raw,a.digest)
    after=s.boot.decode(s.cfg.CONTROL.read_bytes())
    assert result['control_committed'] is True
    assert result['backup_authenticated'] is False
    assert a.events==[s.upgrade_pending(a.plan['attempt_id']).name,'probe',
        'smb-stream-upgrade-'+a.plan['attempt_id']+'.verified.json','commit']
    assert {k:v for k,v in after.items() if k not in ('package_sha256','plan_sha256')}=={
        k:v for k,v in before.items() if k not in ('package_sha256','plan_sha256')}
    assert after['package_sha256']==a.plan['package_sha256'] and after['plan_sha256']==a.digest
    assert s.cfg.CONTRACT.read_bytes()==s.boot.canonical(a.plan['contract'])
    assert s.upgrade_pending(a.plan['attempt_id']).read_bytes()==a.raw
    with pytest.raises(ValueError):s.apply_upgrade(a.raw,a.digest)
    assert a.events.count('commit')==1


@pytest.mark.parametrize('fault',['probe','state','registry','control','contract','supervisor','receipt-write','cas'])
def test_failures_never_commit_or_retry(upgrade,monkeypatch,fault):
    a=upgrade;old=s.cfg.CONTROL.read_bytes();worker=s.upgrade_worker
    def fail():raise ValueError('synthetic failure')
    def run(meta,raw):
        if fault=='probe':fail()
        result=worker(meta,raw)
        if fault=='state':a.state=b'concurrent update'
        if fault=='registry':a.meta['registry_sha256']='9'*64
        if fault in ('control','contract','supervisor'):
            {'control':s.cfg.CONTROL,'contract':s.cfg.CONTRACT,'supervisor':s.service.CONTROL}[fault].write_bytes(b'concurrent change')
        return result
    monkeypatch.setattr(s,'upgrade_worker',run)
    if fault=='receipt-write':
        original=s.boot._write
        def write(path,*args,**kw):
            if '.verified.' in path.name:fail()
            return original(path,*args,**kw)
        monkeypatch.setattr(s.boot,'_write',write)
    if fault=='cas':monkeypatch.setattr(u,'_cas',lambda *args:fail())
    with pytest.raises(ValueError):s.apply_upgrade(a.raw,a.digest)
    assert 'commit' not in a.events
    assert s.cfg.CONTROL.read_bytes()==(b'concurrent change' if fault=='control' else old)
    assert s.upgrade_pending(a.plan['attempt_id']).read_bytes()==a.raw


@pytest.mark.parametrize('fault',['expired','extra','same-package','credential','publisher','contract','bool-time','noncanonical'])
def test_plan_refuses_ambiguous_or_changed_identity(upgrade,fault):
    plan=deepcopy(upgrade.plan)
    if fault=='expired':plan['expires_at']=1000
    if fault=='extra':plan['password']='forbidden'
    if fault=='same-package':plan['package_sha256']=plan['previous_control']['package_sha256']
    if fault=='credential':plan['previous_control']['credential_account']='other-account'
    if fault=='publisher':plan['previous_control']['publisher_sha256']='9'*64
    if fault=='contract':plan['contract']['port']=139
    if fault=='bool-time':plan['created_at']=True
    raw=json.dumps(plan,indent=2).encode() if fault=='noncanonical' else s.boot.canonical(plan)
    with pytest.raises(ValueError):s.upgrade_header(raw,s.cfg.digest(raw))


def test_old_or_unapproved_worker_cannot_enable_upgrade(upgrade,monkeypatch):
    monkeypatch.setattr(s.service,'_program_manifest',lambda meta:{'runtime_capabilities':{}})
    with pytest.raises(ValueError):s.apply_upgrade(upgrade.raw,upgrade.digest)
    assert not upgrade.events


@pytest.fixture
def authenticated(upgrade,monkeypatch):
    a=upgrade
    s.upgrade_pending(a.plan['attempt_id']).write_bytes(a.raw)
    flags=SimpleNamespace(isolated=1)
    monkeypatch.setattr(s.sys,'flags',flags)
    monkeypatch.setattr(s.sys,'dont_write_bytecode',True)
    monkeypatch.setattr(keychain,'selected',lambda:True)
    monkeypatch.setattr(keychain,'load_context',lambda:deepcopy(a.meta['context']))
    a.ids=s.boot._expected(499,499)
    monkeypatch.setattr(s.boot,'_actual',lambda:deepcopy(a.ids))
    a.meta['public']=b'synthetic public'
    monkeypatch.setattr(published,'resolve',lambda *args,**kw:(a.meta['tree'],{}))
    from desktop_assistant import mac_smb_stream_runtime as runtime
    a.denied=Mock();monkeypatch.setattr(runtime,'denied_new_connection',a.denied)
    a.args=SimpleNamespace(phase=s.UPGRADE_PHASE,attempt=a.plan['attempt_id'],plan_sha256=a.digest,
                          challenge='f'*32,socket_fd=77)
    a.script=a.meta['tree']/s.ENTRY;a.executable=a.meta['tree']/'runtime/bin/python3.12'
    return a


def test_worker_requires_signed_tree_identity_and_network_denial(authenticated):
    a=authenticated
    assert s.authenticated_upgrade(a.args,script=a.script,executable=a.executable)==(a.plan,a.meta,a.raw)
    a.denied.assert_called_once_with()


@pytest.mark.parametrize('fault',['uid','group','script','interpreter','pending','fd','phase','signature'])
def test_worker_rejects_before_credentials(authenticated,monkeypatch,fault):
    a=authenticated
    if fault=='uid':a.ids['euid']=501
    if fault=='group':a.ids['kernel_groups'].append(0)
    if fault=='script':a.script=Path('/other.py')
    if fault=='interpreter':a.executable=Path('/usr/bin/python3')
    if fault=='pending':s.upgrade_pending(a.args.attempt).write_bytes(a.raw+b'\n')
    if fault=='fd':a.args.socket_fd=True
    if fault=='phase':a.args.phase='provision'
    if fault=='signature':monkeypatch.setattr(published,'resolve',Mock(side_effect=ValueError('bad signature')))
    store=Mock(side_effect=AssertionError('credential not allowed'))
    monkeypatch.setattr(keychain,'ServiceStore',store)
    with pytest.raises(ValueError):s.authenticated_upgrade(a.args,script=a.script,executable=a.executable)
    assert not store.called and not a.denied.called


@pytest.fixture
def dedicated(upgrade,monkeypatch):
    a=upgrade
    monkeypatch.setattr(s,'authenticated_upgrade',lambda *args,**kw:(a.plan,a.meta,a.raw))
    a.records={s.upgrade_pending(a.plan['attempt_id']):a.raw,s.cfg.CONTROL:s.boot.canonical(a.plan['previous_control']),
               s.cfg.CONTRACT:s.boot.canonical(a.plan['contract']),s.service.CONTROL:b'synthetic supervisor'}
    for path,field,raw in [(a.base/'published.json','registry_sha256',b'registry'),
                          (keychain.CONTEXT,'context_sha256',b'context'),
                          (a.base/'publisher-public.pem','publisher_sha256',b'public')]:
        # Other plan validation is already covered; pin real bytes for rechecks.
        a.records[path]=raw
        a.plan[field]=s.cfg.digest(raw)
    a.raw=s.boot.canonical(a.plan);a.digest=s.cfg.digest(a.raw)
    a.records[s.upgrade_pending(a.plan['attempt_id'])]=a.raw
    monkeypatch.setattr(s.publication,'read_record',lambda p:a.records[p])
    monkeypatch.setattr(keychain,'load_context',lambda:a.meta['context'])
    monkeypatch.setattr(s.boot,'_actual',lambda:s.boot._expected(499,499))
    a.reads=[];a.secret=b'synthetic-existing-secret'
    def get(account):a.reads.append(account);return a.secret
    monkeypatch.setattr(keychain,'ServiceStore',lambda:SimpleNamespace(value=a.meta['context'],get=get))
    monkeypatch.setattr(transport,'Channel',lambda *args:SimpleNamespace(close=lambda:a.events.append('closed')))
    a.args=SimpleNamespace(phase=s.UPGRADE_PHASE,attempt=a.plan['attempt_id'],plan_sha256=a.digest,
                          challenge='f'*32,socket_fd=77)
    @contextmanager
    def share(channel,contract,context_sha,uid,secret):
        assert secret()=='synthetic-existing-secret'
        yield a.peer.tree()
    monkeypatch.setattr(session,'open_share',share)
    return a


def test_probe_reads_existing_account_without_creating_credentials(dedicated,monkeypatch):
    a=dedicated
    # Root proof was independently tested; this test isolates credential IO.
    monkeypatch.setattr(s,'upgrade_reply',lambda value,*args:value)
    result=s.run_upgrade_probe(a.args,script='synthetic',executable='synthetic')
    assert a.reads==[a.plan['previous_control']['credential_account']]
    assert result['credential_changed'] is False and result['backup_authenticated'] is False
    assert b'synthetic-existing-secret' not in s.boot.canonical(result)
    assert a.events==['closed']


@pytest.mark.parametrize('fault',['empty','nul','utf8','long','approval'])
def test_bad_existing_credential_or_changed_approval_fails_without_mutation(dedicated,fault):
    a=dedicated
    if fault=='approval':a.records[s.cfg.CONTROL]=b'changed'
    else:a.secret={'empty':b'','nul':b'bad\0','utf8':b'\xff','long':b'x'*1025}[fault]
    with pytest.raises(ValueError):s.run_upgrade_probe(a.args,script='synthetic',executable='synthetic')
    assert len(a.reads)<=(0 if fault=='approval' else 1)


def test_dispatch_uses_only_pinned_socket_no_secret_input_or_extra_groups(upgrade,monkeypatch):
    a=upgrade
    s.upgrade_pending(a.plan['attempt_id']).write_bytes(a.raw)
    commands=[];events=[]
    class Socket:
        def __init__(self,*args):self.peer=None
        def settimeout(self,value):pass
        def connect(self,peer):self.peer=peer;events.append('connect')
        def getpeername(self):return self.peer
        def fileno(self):return 77
        def __enter__(self):return self
        def __exit__(self,*args):events.append('closed')
    monkeypatch.setattr(s.socket,'socket',Socket)
    monkeypatch.setattr(s,'verify_policy',lambda:events.append('policy'))
    class Process:
        returncode=0
        def __init__(self,command,**kwargs):commands.append((command,kwargs))
        def communicate(self):
            command=commands[0][0];challenge=command[command.index('--challenge')+1]
            result=dict(type=s.UPGRADE_RESULT,challenge=challenge,plan_sha256=a.digest,
                package_sha256=a.plan['package_sha256'],identity=s.boot._expected(499,499),
                proof=a.proof,credential_changed=False,backup_authenticated=False)
            return s.boot.canonical(result),b'private diagnostic never forwarded'
    monkeypatch.setattr(s.subprocess,'Popen',Process)
    # The fixture overrides the dispatcher; exercise the real source function.
    import importlib.util
    spec=importlib.util.spec_from_file_location('desktop_assistant._upgrade_dispatch_test',s.__file__)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    monkeypatch.setattr(module,'metadata',lambda package:a.meta)
    monkeypatch.setattr(module,'verify_policy',s.verify_policy)
    module.upgrade_worker(a.meta,a.raw)
    command,kw=commands[0]
    assert command[:7]==['/usr/bin/sandbox-exec','-p',s.cfg.PROFILE,
        str(a.meta['tree']/'runtime/bin/python3.12'),'-I','-B',str(a.meta['tree']/s.ENTRY)]
    assert command[command.index('--phase')+1]==s.UPGRADE_PHASE
    assert kw['stdin'] is s.subprocess.DEVNULL and kw['pass_fds']==(77,)
    assert kw['user']==kw['group']==499 and kw['extra_groups']==[] and kw['close_fds']
    assert set(kw['env'])=={'HOME','PATH','LANG','ERP_HOME_REHEARSAL'}
    assert events==['policy','connect','closed']


@pytest.mark.parametrize('fault', ['missing-gate','unready','wrong-hash','failed','unknown'])
def test_activated_upgrade_requires_completed_exact_owner_handoff(upgrade, fault):
    a = upgrade
    raw = s.boot.canonical(dict(type='tianming.factory-owner-current-mac.activation.v1', context_sha256=a.plan['context_sha256']))
    (a.base/'factory-activation.json').write_bytes(raw)
    gate = dict(ready=True, activation_sha256=s.cfg.digest(raw))
    if fault == 'unready': gate['ready'] = False
    if fault == 'wrong-hash': gate['activation_sha256'] = '9'*64
    if fault == 'failed': (a.base/'owner-current-mac-activation-failed.json').write_bytes(b'failed')
    if fault == 'unknown': (a.base/'factory-activation.json').write_text('{"type":"unknown"}')
    if fault != 'missing-gate':
        (a.base/'owner-current-mac-activation-publication.json').write_bytes(s.boot.canonical(gate))
    with pytest.raises((ValueError,FileNotFoundError)): s._upgrade_activation()
    assert 'commit' not in a.events


def test_factory_activation_is_pinned_and_home_absence_is_explicit(upgrade):
    a = upgrade
    assert s._upgrade_activation() is None
    raw = s.boot.canonical(dict(type='tianming.factory-owner-current-mac.activation.v1', context_sha256=a.plan['context_sha256']))
    (a.base/'factory-activation.json').write_bytes(raw)
    (a.base/'owner-current-mac-activation-publication.json').write_bytes(s.boot.canonical(dict(ready=True,activation_sha256=s.cfg.digest(raw))))
    assert s._upgrade_activation() == s.cfg.digest(raw)
    (a.base/'factory-activation.json').write_bytes(raw+b' ')
    with pytest.raises(ValueError): s._upgrade_activation()


@pytest.mark.parametrize('fault', ['activation-change','context','pending','lock'])
def test_upgrade_path_recheck_blocks_state_change_without_touching_binding(upgrade,monkeypatch,fault):
    a=upgrade
    active=dict(type='tianming.factory-owner-current-mac.activation.v1',context_sha256=a.plan['context_sha256'])
    raw=s.boot.canonical(active);a.plan['activation_sha256']=s.cfg.digest(raw)
    (a.base/'factory-activation.json').write_bytes(raw)
    (a.base/'owner-current-mac-activation-publication.json').write_bytes(s.boot.canonical(dict(ready=True,activation_sha256=s.cfg.digest(raw))))
    class Pin:
        def __init__(self,path,**kw): self.path=Path(path)
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def read(self): return b'bad' if fault=='lock' else b'0'
        def check(self): pass
    monkeypatch.setattr(u,'Pin',Pin)
    monkeypatch.setattr(u,'_no_pending',lambda root:None)
    if fault=='lock':
        with pytest.raises(ValueError):
            with REAL_UPGRADE_PATHS(a.plan): pass
    else:
        with REAL_UPGRADE_PATHS(a.plan) as paths:
            if fault=='activation-change': (a.base/'factory-activation.json').write_bytes(raw+b' ')
            if fault=='context': a.plan['context_sha256']='9'*64
            if fault=='pending': monkeypatch.setattr(u,'_no_pending',Mock(side_effect=ValueError('pending operation')))
            with pytest.raises(ValueError): paths['check']()
    assert 'commit' not in a.events
