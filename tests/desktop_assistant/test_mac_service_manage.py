"""Actual private IO/locks, with explicit root, identity and runtime adapters."""
from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from desktop_assistant import mac_service_manage as m, mac_system_service as system
from desktop_assistant import mac_keychain_install as boot, manager as manager_module
from desktop_assistant import credential_store
from desktop_assistant.storage import write_json, read_json
from desktop_assistant.operation_lock import operation_lock, BUSY

pytestmark=pytest.mark.skipif(sys.platform!='darwin',reason='Mac service management native FD/ACL')


@pytest.fixture
def instance(tmp_path,monkeypatch):
    base=tmp_path.resolve()/'system';base.mkdir(mode=0o755)
    root=base/'instance';root.mkdir(mode=0o700)
    for name in ('releases','shared','control','backups','packages','staging'):(root/name).mkdir(mode=0o700)
    old_umask=os.umask(0o077)
    write_json(root/'state.json',{'current':'c'*64,'manual_stop':True,'arbitrary_history':'preserve'})
    preferences={'nas':'Z:\\old\\backups','protected_password':'tm-keychain-v1:backup:'+'d'*32,
                 'nightly_schedule':{'registered':True,'requires_user_login':True},'extra':{'preserve':[1,2]}}
    write_json(root/'preferences.json',preferences)
    (root/'control/operation.lock').write_bytes(b'0')
    context={'uid':os.getuid(),'gid':os.getgid()}
    monkeypatch.setattr(m,'BASE',base);monkeypatch.setattr(m,'ROOT',root)
    original=m.Lease
    def lease(path,**kwargs):
        if path==base:
            assert kwargs['uid']==0 and kwargs['gid'] is None
            kwargs['uid']=os.getuid()
        return original(path,**kwargs)
    monkeypatch.setattr(m,'Lease',lease)
    monkeypatch.setattr(m,'authenticated',lambda *a:(context,b'synthetic public'))
    monkeypatch.setattr(boot,'_actual',lambda:boot._expected(context['uid'],context['gid']))
    destination=tmp_path/'approved-nas';destination.mkdir()
    contract={'destination':str(destination)}
    monkeypatch.setattr(m.remote,'_contract',lambda:(contract,'e'*64,context))
    nas=Mock(return_value={'synthetic-approved':True});monkeypatch.setattr(m.remote,'before_write',nas)
    monkeypatch.setattr(m.remote,'verified_at',lambda root,state:state.get('last_backup_at'))
    canary=Mock();monkeypatch.setattr(system,'verify_keychain',canary)
    unprotect=Mock(return_value='synthetic-only-password');monkeypatch.setattr(credential_store,'unprotect_backup',unprotect)
    service={'registered':True,'enabled':True,'scheduled_backups':True}
    monkeypatch.setattr(system,'management_service_state',lambda:deepcopy(service))
    class Manager:
        def __init__(self):self.running=False;self.starts=self.stops=self.backups=0
        @property
        def state(self):return read_json(root/'state.json')
        def _process(self):return (object(),{}) if self.running else None
        def start(self):
            self.starts+=1;self.running=True
            # The outer held FD must exclude an independent open of the lock.
            with pytest.raises(ValueError,match=BUSY):
                with operation_lock(root/'control/operation.lock'):pass
        def stop(self):
            assert self.state['manual_stop'] is True
            self.stops+=1;self.running=False
        def _backup_locked(self,password,nas):
            assert password=='synthetic-only-password' and nas==destination
            self.backups+=1
            value=self.state;value.update(last_backup=str(destination/'one.tmbackup'),last_backup_at='2026-10-11T01:00:00+00:00')
            write_json(root/'state.json',value)
            return destination/'one.tmbackup'
    manager=Manager();constructor=Mock(return_value=manager);monkeypatch.setattr(manager_module,'Manager',constructor)
    request={'action':'status','package_sha256':'a'*64,'challenge':'b'*32,'context_sha256':'e'*64,
             'publisher_sha256':'f'*64,'registry_sha256':'d'*64}
    def run(action):return m.run({**request,'action':action},script='synthetic',executable='synthetic')
    try:
        yield SimpleNamespace(base=base,root=root,context=context,preferences=preferences,destination=destination,
            canary=canary,unprotect=unprotect,nas=nas,service=service,manager=manager,constructor=constructor,request=request,run=run)
    finally:os.umask(old_umask)


def test_status_is_readonly_and_never_reads_secrets_or_touches_nas(instance):
    a=instance;before={p:p.read_bytes() for p in a.root.rglob('*') if p.is_file()}
    result=a.run('status')
    assert result['snapshot']=={'current':'c'*64,'configured_mode':'home','manual_stop':True,'running':False,
        'backup_configured':False,'backup_verified_at':None,'maintenance_attention':False,'backup_attention':False}
    assert {p:p.read_bytes() for p in before}==before
    a.canary.assert_not_called();a.unprotect.assert_not_called();a.nas.assert_not_called()
    assert 'protected_password' not in json.dumps(result) and 'arbitrary_history' not in json.dumps(result)


def test_retained_backup_work_is_visible_without_secret_or_remote_access(instance):
    a=instance;state=a.manager.state
    state['backup_work_retained']=[str(a.root/'staging'/('smb-stream-'+'e'*32))]
    write_json(a.root/'state.json',state)
    result=a.run('status')
    assert result['snapshot']['backup_attention'] is True
    assert 'smb-stream-' not in json.dumps(result)
    a.canary.assert_not_called();a.unprotect.assert_not_called();a.nas.assert_not_called()


@pytest.mark.parametrize('action', ['status', 'stop'])
def test_public_status_and_stop_survive_archive_identity_failure(instance, monkeypatch, action):
    a=instance
    monkeypatch.setattr(m.remote, '_contract', Mock(side_effect=subprocess.SubprocessError('synthetic private detail')))
    state=a.manager.state;state['backup_error']='synthetic private backup detail'
    write_json(a.root/'state.json', state)
    a.canary.side_effect=ValueError('synthetic inaccessible keychain')
    result=a.run(action)
    assert result['snapshot']['backup_attention'] is True
    assert result['snapshot']['backup_configured'] is False
    assert 'synthetic private' not in json.dumps(result)
    a.canary.assert_not_called();a.unprotect.assert_not_called();a.nas.assert_not_called()


def test_existing_pending_symlink_is_neither_followed_nor_removed(instance):
    a=instance
    target=a.root/'retained-evidence';target.write_bytes(b'retain original')
    pending=a.root/'state.json.pending';pending.symlink_to(target)
    a.run('stop')
    assert pending.is_symlink() and target.read_bytes()==b'retain original'
    assert a.manager.state['manual_stop'] is True


def test_configure_changes_only_approved_path_without_unwrapping_credentials(instance):
    a=instance;state=(a.root/'state.json').read_bytes()
    result=a.run('configure-backup')
    assert read_json(a.root/'preferences.json')=={**a.preferences,'nas':str(a.destination)}
    assert (a.root/'state.json').read_bytes()==state
    assert result['snapshot']['backup_configured'] is True
    a.canary.assert_not_called();a.unprotect.assert_not_called();a.nas.assert_called_once_with(a.destination)
    assert (a.root/'preferences.json').stat().st_mode&0o777==0o600


@pytest.mark.parametrize('fault',['nas','context','reference','settings-link','state-link','missing-lock','lock-content','root-link','permissions'])
def test_bad_metadata_refuses_before_constructing_or_changing_business(instance,monkeypatch,fault):
    a=instance
    if fault=='nas':a.nas.side_effect=ValueError('synthetic NAS failure')
    if fault=='context':monkeypatch.setattr(m.remote,'_contract',lambda:({'destination':str(a.destination)},'e'*64,{'uid':99}))
    if fault=='reference':write_json(a.root/'preferences.json',{**a.preferences,'protected_password':'windows-dpapi-not-service-reference'})
    if fault in {'settings-link','state-link'}:
        path=a.root/('preferences.json' if fault=='settings-link' else 'state.json')
        path.rename(path.with_suffix('.saved'));path.symlink_to(path.with_suffix('.saved'))
    if fault=='missing-lock':(a.root/'control/operation.lock').unlink()
    if fault=='lock-content':(a.root/'control/operation.lock').write_bytes(b'changed')
    if fault=='root-link':
        (a.root/'staging').rename(a.root/'saved-staging');(a.root/'staging').symlink_to(a.root/'saved-staging')
    if fault=='permissions':(a.root/'shared').chmod(0o755)
    before={str(p):p.read_bytes() for p in a.root.rglob('*') if p.is_file()}
    with pytest.raises((ValueError,OSError)):a.run('configure-backup')
    assert {str(p):p.read_bytes() for p in a.root.rglob('*') if p.is_file()}==before
    assert a.manager.starts==a.manager.stops==a.manager.backups==0
    a.unprotect.assert_not_called();a.canary.assert_not_called()


def test_backup_requires_approved_destination_then_loads_existing_key_and_preserves_stop(instance):
    a=instance
    with pytest.raises(ValueError,match='先配置'):a.run('backup')
    a.unprotect.assert_not_called();assert a.manager.backups==0
    a.run('configure-backup');result=a.run('backup')
    assert a.manager.backups==1 and a.manager.starts==a.manager.stops==0
    assert result['snapshot']['manual_stop'] and result['snapshot']['backup_verified_at']
    a.unprotect.assert_called_once_with(a.preferences['protected_password'])
    assert 'synthetic-only-password' not in json.dumps(result)


def test_stop_persists_manual_gate_even_when_graceful_stop_fails_without_credentials(instance):
    a=instance;a.manager.running=True
    state=a.manager.state;state['manual_stop']=False;write_json(a.root/'state.json',state)
    def fail():
        assert a.manager.state['manual_stop'] is True
        raise ValueError('synthetic failure')
    a.manager.stop=fail
    with pytest.raises(ValueError):a.run('stop')
    assert a.manager.state['manual_stop'] is True
    a.canary.assert_not_called();a.unprotect.assert_not_called();a.nas.assert_not_called()


@pytest.mark.parametrize('scheduled_backups', [True, False])
def test_start_stop_share_existing_lock_and_do_not_rewrite_unrelated_state(instance, scheduled_backups):
    a=instance
    a.service['scheduled_backups']=scheduled_backups
    result=a.run('start');assert result['snapshot']['running'] and not result['snapshot']['manual_stop']
    assert a.manager.state['arbitrary_history']=='preserve'
    result=a.run('stop');assert not result['snapshot']['running'] and result['snapshot']['manual_stop']
    assert a.manager.starts==a.manager.stops==1
    assert a.service['scheduled_backups'] is scheduled_backups
    a.canary.assert_called_once();a.unprotect.assert_not_called();a.nas.assert_not_called()


@pytest.mark.parametrize('fault',['disabled','unregistered','maintenance','canary','start-fails'])
def test_start_rejects_incomplete_system_without_clearing_manual_stop(instance,fault):
    a=instance
    if fault in {'disabled','unregistered'}:
        a.service[{'disabled':'enabled','unregistered':'registered'}[fault]]=False
    if fault=='maintenance':
        state=a.manager.state;state['operation']='migration_failed';write_json(a.root/'state.json',state)
    if fault=='canary':a.canary.side_effect=ValueError('synthetic canary unavailable')
    if fault=='start-fails':a.manager.start=Mock(side_effect=ValueError('synthetic failure'))
    with pytest.raises(ValueError):a.run('start')
    assert a.manager.state['manual_stop'] is True


@pytest.mark.parametrize('field', ['registered', 'enabled'])
@pytest.mark.parametrize('value', [None, 0, 1, 'true'])
def test_start_requires_true_service_flags_before_keychain_or_start(instance, field, value):
    a=instance;a.service[field]=value;a.service['scheduled_backups']=False
    with pytest.raises(ValueError, match='系统监护'):a.run('start')
    assert a.manager.starts==0 and a.manager.state['manual_stop'] is True
    a.canary.assert_not_called();a.unprotect.assert_not_called();a.nas.assert_not_called()


def test_busy_lock_rejects_without_initialization_or_io(instance):
    a=instance
    with operation_lock(a.root/'control/operation.lock'):
        with pytest.raises(ValueError,match=BUSY):a.run('configure-backup')
    a.constructor.assert_not_called();a.nas.assert_not_called();a.unprotect.assert_not_called()


def test_late_preference_change_is_preserved_and_not_overwritten(instance,monkeypatch):
    a=instance;original=m.record;calls=[]
    def read(root,name,context):
        if name=='preferences.json':
            calls.append(name)
            if len(calls)==3:write_json(a.root/name,{**a.preferences,'external-admin-change':True})
        return original(root,name,context)
    monkeypatch.setattr(m,'record',read)
    with pytest.raises(ValueError,match='并发变化'):a.run('configure-backup')
    assert read_json(a.root/'preferences.json')['external-admin-change'] is True
    assert list(a.root.glob('preferences-*.pending.json'))


def test_replaced_lock_is_detected_by_same_held_fd_before_setting_promotion(instance,monkeypatch):
    a=instance;path=a.root/'control/operation.lock';old=path.read_bytes()
    def substitute(*args):
        path.rename(path.with_suffix('.retained'));path.write_bytes(old)
        return {'synthetic-approved':True}
    a.nas.side_effect=substitute
    with pytest.raises(ValueError,match='身份变化'):a.run('configure-backup')
    assert read_json(a.root/'preferences.json')==a.preferences


def test_ordinary_uid_is_rejected_without_importing_application_or_echoing_arguments():
    entry=Path(__file__).resolve().parents[2]/'desktop_assistant/service_manage_entry.py'
    value=subprocess.run([sys.executable,'-I','-B',str(entry),'synthetic-sensitive-argument'],capture_output=True)
    assert value.returncode and '专用管理未确认完成'.encode() in value.stderr
    assert b'Traceback' not in value.stderr and b'synthetic-sensitive-argument' not in value.stderr


@pytest.mark.parametrize('fault',['none','uid','groups','context','publisher','registry','package','script','python','capability'])
def test_authentication_binds_actual_identity_and_current_approved_entry(monkeypatch,fault):
    repo=Path(m.__file__).resolve().parent.parent
    context={'uid':499,'gid':499};actual=boot._expected(499,499)
    request={'action':'status','package_sha256':'a'*64,'challenge':'b'*32,'context_sha256':'c'*64,
             'publisher_sha256':'d'*64,'registry_sha256':'e'*64}
    meta={**{k:request[k] for k in ('context_sha256','publisher_sha256','registry_sha256')},
          'context':context,'public':b'synthetic public'}
    monkeypatch.setattr(sys,'flags',SimpleNamespace(**{**{k:getattr(sys.flags,k) for k in dir(sys.flags)
                         if not k.startswith('_')},'isolated':1}))
    monkeypatch.setattr(sys,'dont_write_bytecode',True)
    monkeypatch.setattr(m.keychain,'selected',lambda:True);monkeypatch.setattr(m.keychain,'load_context',lambda:context)
    monkeypatch.setattr(boot,'_actual',lambda:actual)
    metadata=Mock(return_value=meta);monkeypatch.setattr(system,'metadata',metadata)
    manifest={'runtime_capabilities':{'service_manage_v1':1,'system_nightly_v1':1},
              'files':{system.MANAGE_ENTRY:'hash',system.MANAGE_MODULE:'hash'}}
    resolver=Mock(return_value=(repo,manifest));monkeypatch.setattr(m.published,'resolve',resolver)
    script=repo/system.MANAGE_ENTRY;executable=repo/'runtime/bin/python3.12'
    if fault=='uid':actual['euid']=0
    if fault=='groups':actual['kernel_groups']=[499,80]
    if fault in ('context','publisher','registry'):request[fault+'_sha256']='f'*64
    if fault=='package':request['package_sha256']='invalid'
    if fault=='script':script=repo/'wrong.py'
    if fault=='python':executable=Path(sys.executable)
    if fault=='capability':manifest['runtime_capabilities']['system_nightly_v1']=True
    if fault=='none':
        assert m.authenticated(request,script,executable)==(context,b'synthetic public')
        resolver.assert_called_once_with(m.ROOT,'a'*64,b'synthetic public',kind='migration')
    else:
        with pytest.raises(ValueError):m.authenticated(request,script,executable)


@pytest.mark.parametrize('fault',['none','backup-paused','disabled','unregistered','nonbool-enabled','nonbool-registered',
    'identity','extra-secret','timestamp','manual','stopped','exit','interrupt','capability'])
def test_root_dispatch_drops_privilege_and_rejects_bad_or_secret_reply(monkeypatch,tmp_path,fault):
    context={'uid':499,'gid':499};meta={'package':'a'*64,'context':context,'context_sha256':'c'*64,
        'publisher_sha256':'d'*64,'registry_sha256':'e'*64,'tree':tmp_path/'approved'}
    monkeypatch.setattr(boot,'modules',lambda:(SimpleNamespace(_administrator=lambda:None),None))
    monkeypatch.setattr(boot,'verify_code',lambda:'f'*64);monkeypatch.setattr(system,'metadata',lambda p:meta)
    manifest={'runtime_capabilities':{'service_manage_v1':1,'system_nightly_v1':1},
              'files':{system.MANAGE_ENTRY:'hash',system.MANAGE_MODULE:'hash'}}
    if fault=='capability':manifest['runtime_capabilities'].pop('service_manage_v1')
    monkeypatch.setattr(system,'_program_manifest',lambda meta:manifest)
    service={'registered':True,'enabled':True,'scheduled_backups':fault!='backup-paused'}
    if fault in {'disabled','unregistered','nonbool-enabled','nonbool-registered'}:
        field={'disabled':'enabled','unregistered':'registered','nonbool-enabled':'enabled',
               'nonbool-registered':'registered'}[fault]
        service[field]=1 if fault.startswith('nonbool-') else False
    monkeypatch.setattr(system,'management_service_state',lambda:deepcopy(service))
    calls=[]
    class Process:
        returncode=1 if fault=='exit' else 0
        def communicate(self,raw):
            calls.append(raw)
            if fault=='interrupt' and len(calls)==1:raise KeyboardInterrupt
            request=json.loads(raw if raw is not None else calls[0])
            result={'type':'tianming.service-management.v1','action':'start','package_sha256':'a'*64,
                'challenge':request['challenge'],'identity':boot._expected(499,499),'home_rehearsal':True,
                'snapshot':{'current':'c'*64,'configured_mode':'home','manual_stop':False,'running':True,'backup_configured':False,
                            'backup_verified_at':None,'maintenance_attention':False,'backup_attention':False}}
            if fault=='identity':result['identity']['uid']=501
            if fault=='extra-secret':result['protected_password']='synthetic-forbidden-reference'
            if fault=='timestamp':result['snapshot']['backup_verified_at']='not-a-time'
            if fault=='manual':result['snapshot']['manual_stop']=True
            if fault=='stopped':result['snapshot']['running']=False
            return boot.canonical(result),b''
        def kill(self):pytest.fail('never force-kill a management process')
    def popen(args,**kwargs):
        assert args==[str(meta['tree']/'runtime/bin/python3.12'),'-I','-B',str(meta['tree']/system.MANAGE_ENTRY)]
        assert kwargs['user']==kwargs['group']==499 and kwargs['extra_groups']==[]
        assert kwargs['cwd']=='/' and kwargs['umask']==0o077 and kwargs['pass_fds']==()
        assert kwargs['env']['ERP_HOME_REHEARSAL']=='1'
        return Process()
    monkeypatch.setattr(system.subprocess,'Popen',popen)
    if fault in {'none','backup-paused'}:
        result=system.manage('a'*64,'start')
        assert result['result']['snapshot']['running']
        assert result['system']==service
    else:
        with pytest.raises(ValueError):system.manage('a'*64,'start')
    if fault=='interrupt':assert len(calls)==2 and calls[1] is None
    if fault in {'capability','disabled','unregistered','nonbool-enabled','nonbool-registered'}:assert not calls


def test_full_synthetic_backup_through_management_entry_preserves_complete_facts(instance,monkeypatch):
    from tests.desktop_assistant import test_recovery as r
    from tests.desktop_assistant.synthetic_storage import approved_local_storage
    from desktop_assistant.storage import decrypt_file,extract_verified,database_info
    from desktop_assistant import credential_transfer
    a=instance;case=r.RecoveryTests();case.setUp()
    try:
        # Use the real core on its cryptographically signed synthetic DB/release;
        # only OS identity/remote storage and the credential backend are adapters.
        for folder in ('','releases','shared','control','backups','packages','staging'):
            (case.manager.root/folder).chmod(0o700)
        case.root.chmod(0o755)
        (case.manager.root/'control/operation.lock').write_bytes(b'0')
        write_json(case.manager.root/'preferences.json',{**a.preferences,'nas':str(case.nas)})
        monkeypatch.setattr(m,'BASE',case.root);monkeypatch.setattr(m,'ROOT',case.manager.root)
        real=m.Lease
        def lease(path,**kw):
            if path==case.root:kw['uid']=os.getuid()
            return real(path,**kw)
        monkeypatch.setattr(m,'Lease',lease)
        monkeypatch.setattr(manager_module,'Manager',lambda *args:case.manager)
        monkeypatch.setattr(m.remote,'_contract',lambda:({'destination':str(case.nas)},'e'*64,a.context))
        monkeypatch.setattr(credential_store,'unprotect_backup',lambda ref:r.PASSWORD)
        monkeypatch.setattr(credential_transfer,'unprotect',lambda *args:r.PASSWORD)
        with approved_local_storage(case.root):
            result=a.run('backup')
        backup=Path(case.manager.state['last_backup'])
        raw=case.root/'verification.zip';decrypt_file(backup,raw,r.PASSWORD)
        payload=case.root/'verification';extract_verified(raw,payload)
        assert database_info(payload/'shared/data/carton_erp.sqlite3')==database_info(case.manager.root/'shared/data/carton_erp.sqlite3')
        assert (payload/'shared/data/drawing.pdf').read_bytes()==b'synthetic attachment'
        assert result['snapshot']['backup_verified_at'] and not result['snapshot']['running']
    finally:case.tearDown()


@pytest.mark.parametrize('action',['status','stop'])
def test_broken_activation_attention_does_not_disable_status_or_stop(instance,monkeypatch,action):
    from desktop_assistant import mac_factory_runtime
    a=instance
    monkeypatch.setattr(mac_factory_runtime,'configured_mode',lambda manager:'attention')
    a.canary.side_effect=ValueError('synthetic keychain failure')
    result=a.run(action)
    assert result['snapshot']['configured_mode']=='attention'
    if action=='stop':assert result['snapshot']['manual_stop'] and not result['snapshot']['running']
    a.canary.assert_not_called();a.unprotect.assert_not_called();a.nas.assert_not_called()
