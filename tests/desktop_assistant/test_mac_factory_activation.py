"""Real synthetic backup proof and inherited locks; OS/root adapters are explicit."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import fcntl
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from desktop_assistant import mac_factory_activation_install as install, mac_factory_activation as worker
from desktop_assistant import factory_activation_contract as contract, factory_target_contract as target_contract
from desktop_assistant import factory_cutover as cutover, mac_keychain_install as boot
from desktop_assistant import mac_service_manage as manage, mac_system_service as system
from desktop_assistant import mac_service_restore as restore
from desktop_assistant.storage import read_json,write_json,sha
from tests.desktop_assistant.test_factory_target_audit import target,case,recovery_case,host_store,rewrite,protected_facts
from tests.desktop_assistant import test_mac_service_restore as native_fixture

pytestmark=pytest.mark.skipif(sys.platform!='darwin',reason='Mac publication/FD/ownership protocol')
TEST_PYTHON=sys.executable


def caps():
    return {'runtime_capabilities':{k:1 for k in ('factory_activation_v1','factory_audit_v1','factory_execution_v1')},
        'reader_capabilities':{'factory_cutover_v1':1},'files':{k:'a'*64 for k in
        (contract.ENTRY,contract.MODULE,'desktop_assistant/factory_target_audit.py','desktop_assistant/factory_activation_contract.py')}}


@pytest.fixture(autouse=True)
def signed_activation_protocol(monkeypatch):
    original=native_fixture.pack_tree
    monkeypatch.setattr(native_fixture.publication,'SERVICE_RESTORE_FILES',
        native_fixture.publication.SERVICE_RESTORE_FILES|set(caps()['files']))
    def pack(source,destination,meta,key):
        if meta.get('runtime_platform')=='macos-arm64':
            meta=deepcopy(meta);meta['runtime_capabilities'].update(caps()['runtime_capabilities'])
        return original(source,destination,meta,key)
    monkeypatch.setattr(native_fixture,'pack_tree',pack)


def bound_request(t,fd):
    x=t.x
    return {'package_sha256':x.id,'request_id':t.request['request_id'],'input_attempt':x.args.input_attempt,
        'authorization_sha256':sha(x.attempt/'authorization.json'),'context_sha256':sha(x.base/'keychain-context.json'),
        'registry_sha256':sha(x.base/'published.json'),'challenge':'c'*32,'lock_fd':fd}


@pytest.fixture
def root_case(target,monkeypatch):
    t=target;x=t.x
    actual_uid,actual_gid=os.getuid(),os.getgid()
    monkeypatch.setattr(install,'_owned',lambda info,uid,gid:info.st_uid==actual_uid and info.st_gid==actual_gid)
    monkeypatch.setattr(boot,'modules',lambda:(SimpleNamespace(_administrator=lambda:None),None))
    monkeypatch.setattr(boot,'verify_code',lambda:'d'*64)
    monkeypatch.setattr(boot,'protected',lambda path,**kw:Path(path).read_bytes())
    monkeypatch.setattr(install.publication,'read_record',lambda path:Path(path).read_bytes())
    @contextmanager
    def locked():yield
    monkeypatch.setattr(install.publication,'lock',locked)
    meta={'context':x.context,'context_sha256':sha(x.base/'keychain-context.json'),
        'registry_sha256':sha(x.base/'published.json'),'publisher_sha256':x.args.publisher_sha256,
        'public':t.c.public,'tree':x.slot/'migration','package':x.id}
    monkeypatch.setattr(system,'metadata',lambda package:deepcopy(meta))
    monkeypatch.setattr(system,'_program_manifest',lambda meta:caps())
    monkeypatch.setattr(target_contract,'host_hash',lambda:t.request['target']['host_sha256'])
    # Only actual executable location/OS identity is adapted. The complete
    # signed package, capabilities and both immutable trees are really checked.
    monkeypatch.setattr(worker,'__file__',str(x.slot/'migration'/contract.MODULE))
    monkeypatch.setattr(system,'management_service_state',lambda:{'registered':True,'enabled':True,'scheduled_backups':True})
    when=datetime.now(timezone.utc).isoformat()
    monkeypatch.setattr(manage.remote,'verified_at',lambda root,state:when)
    monkeypatch.setattr(worker.sys,'flags',SimpleNamespace(**{**{k:getattr(sys.flags,k) for k in dir(sys.flags)
        if not k.startswith('_')},'isolated':1}))
    monkeypatch.setattr(worker.sys,'dont_write_bytecode',True)
    calls=[];mutations=[];checkpoints=[]
    class Process:
        returncode=0
        def communicate(self,raw):
            calls.append(raw)
            if raw is None:return self.output,b''
            request=boot.decode(raw)
            value=worker.run(request,script=x.slot/'migration'/contract.ENTRY,executable=sys.executable)
            # Audit worker exited, but independently opened contenders remain
            # excluded until root has completed create/readback publication.
            with (x.root/'control/operation.lock').open('rb') as contender:
                with pytest.raises(BlockingIOError):fcntl.flock(contender,fcntl.LOCK_EX|fcntl.LOCK_NB)
            checkpoints.append('worker-completed-lock-retained')
            self.output=boot.canonical(value)
            if mutations:mutations.pop(0)(request,value)
            self.output=boot.canonical(value)
            return self.output,b''
    process=Process()
    def popen(argv,**kw):
        assert argv[:3]==['/usr/bin/sandbox-exec','-p','(version 1)(allow default)(deny network-outbound)']
        assert argv[3:]==[str(meta['tree']/'runtime/bin/python3.12'),'-I','-B',str(meta['tree']/contract.ENTRY)]
        assert kw['user']==kw['group']==499 and kw['extra_groups']==[] and len(kw['pass_fds'])==1
        assert kw['env']['ERP_HOME_REHEARSAL']=='1' and kw['umask']==0o077 and kw['cwd']=='/'
        assert fcntl.fcntl(kw['pass_fds'][0],fcntl.F_GETFL)&os.O_ACCMODE==os.O_RDONLY
        return process
    launch=Mock(side_effect=popen);monkeypatch.setattr(install.subprocess,'Popen',launch)
    def run(**kwargs):return install.activate(x.id,t.request['request_id'],x.args.input_attempt,
            sha(x.attempt/'authorization.json'),cutover_authorized=True,source_isolated=True,**kwargs)
    return SimpleNamespace(t=t,x=x,meta=meta,run=run,launch=launch,calls=calls,mutations=mutations,
                           checkpoints=checkpoints,process=process)


def test_publish_holds_original_lock_through_real_backup_audit_and_final_write(root_case,monkeypatch):
    c=root_case;x=c.x;before=(x.root/'state.json').read_bytes();write=boot._write
    def checked_write(path,raw,mode):
        with (x.root/'control/operation.lock').open('rb') as contender:
            with pytest.raises(BlockingIOError):fcntl.flock(contender,fcntl.LOCK_EX|fcntl.LOCK_NB)
        return write(path,raw,mode=mode)
    monkeypatch.setattr(boot,'_write',checked_write)
    result=c.run();assert result['factory_activated'] is True and result['started'] is False and result['manual_stop'] is True
    raw=(x.base/contract.NAME).read_bytes();record=boot.decode(raw);contract.validate_record(record)
    cutover.verify_authorization(record['authorization'].encode(),c.t.c.public,c.t.request)
    assert result['activation_sha256']==boot.digest(raw) and (x.base/contract.NAME).stat().st_mode&0o777==0o644
    assert (x.root/'state.json').read_bytes()==before and c.checkpoints==['worker-completed-lock-retained']
    assert record['operator_attestation']==contract.ATTESTATION
    assert c.t.case.secrets and c.t.record['entries']['backup']['value'] not in raw.decode()
    with (x.root/'control/operation.lock').open('rb') as stream:
        fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
    with pytest.raises(ValueError,match='已有'):c.run()
    assert c.launch.call_count==1 and (x.base/contract.NAME).read_bytes()==raw


def test_fd_lock_survives_real_exec_and_borrower_exit(tmp_path):
    path=tmp_path/'operation.lock';path.write_bytes(b'0');path.chmod(0o600)
    with path.open('rb') as held:
        fcntl.flock(held,fcntl.LOCK_EX|fcntl.LOCK_NB)
        code="""
import os,sys
from types import SimpleNamespace
sys.path.insert(0,sys.argv[1])
from desktop_assistant.mac_service_manage import maintenance
fd=int(sys.argv[2])
with maintenance(SimpleNamespace(fd=fd,check=lambda:None),borrowed=True):
    assert os.read(fd,1)==b'0'
"""
        from desktop_assistant.home_process import command
        argv=command([TEST_PYTHON,'-I','-B','-c',code,str(Path(__file__).resolve().parents[2]),str(held.fileno())],
                     {'ERP_HOME_REHEARSAL':'1'})
        result=subprocess.run(argv,pass_fds=(held.fileno(),),capture_output=True,text=True,timeout=10,
                              env=dict(os.environ,ERP_HOME_REHEARSAL='1'))
        assert result.returncode==0,result.stderr
        with path.open('rb') as contender:
            with pytest.raises(BlockingIOError):fcntl.flock(contender,fcntl.LOCK_EX|fcntl.LOCK_NB)
    with path.open('rb') as stream:fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)


@pytest.mark.parametrize('fault',['identity','challenge','extra','bool','snapshot','network','context','registry',
    'authorization','window','current-meta','late-input','late-lock','exit','interrupt',
    'service-disabled','service-unregistered','service-unscheduled','no-backup','old-backup','canary',
    'existing','pending','dangling-active','bad-capability','input-mode','input-link','input-hardlink','fifo',
    'lock-bytes','request-host','authorization-signature'])
def test_failure_preserves_stopped_instance_and_never_publishes_active_record(root_case,monkeypatch,fault):
    c=root_case;x=c.x
    def changed(request,value):
        if fault=='identity':value['identity']['uid']=501
        elif fault=='challenge':value['challenge']='d'*32
        elif fault=='extra':value['secret']='synthetic-forbidden'
        elif fault=='bool':value['manual_stop']=1
        elif fault=='snapshot':value['record']['target_snapshot']['facts_sha256']='f'*64
        elif fault=='network':value['record']['network']['ERP_PORT']='8001'
        elif fault=='context':value['record']['context_sha256']='f'*64
        elif fault=='registry':value['record']['registry_sha256']='f'*64
        elif fault=='authorization':value['record']['authorization']+=' '
        elif fault=='window':monkeypatch.setattr(target_contract,'_now',lambda:datetime.now(timezone.utc)+timedelta(hours=9))
        elif fault=='current-meta':c.meta['registry_sha256']='f'*64
        elif fault=='late-input':rewrite(x.attempt/'authorization.json',lambda v:v['body'].update(issued_at='2000-01-01T00:00:00Z'))
        elif fault=='late-lock':
            p=x.root/'control/operation.lock';p.rename(p.with_name('kept-original.lock'));p.write_bytes(b'0');p.chmod(0o600)
        elif fault=='interrupt':raise KeyboardInterrupt()
    if fault in {'identity','challenge','extra','bool','snapshot','network','context','registry','authorization',
                 'window','current-meta','late-input','late-lock','interrupt'}:
        c.mutations.append(changed)
    if fault=='exit':c.process.returncode=1
    if fault.startswith('service-'):
        value={'registered':True,'enabled':True,'scheduled_backups':True}
        value[{'service-disabled':'enabled','service-unregistered':'registered','service-unscheduled':'scheduled_backups'}[fault]]=False
        monkeypatch.setattr(system,'management_service_state',lambda:value)
    if fault in {'no-backup','old-backup'}:
        monkeypatch.setattr(manage.remote,'verified_at',lambda *a:None if fault=='no-backup' else '2000-01-01T00:00:00+00:00')
    if fault=='canary':monkeypatch.setattr(system,'verify_keychain',Mock(side_effect=ValueError('synthetic denied')))
    if fault in {'existing','pending','dangling-active'}:
        path=x.base/(contract.NAME if fault!='pending' else 'factory-activation-existing.pending.json')
        if fault=='dangling-active':path.symlink_to(x.base/'missing')
        else:path.write_bytes(b'prior evidence')
    if fault=='bad-capability':monkeypatch.setattr(system,'_program_manifest',lambda _: {'runtime_capabilities':{}})
    if fault=='input-mode':(x.attempt/'authorization.json').chmod(0o640)
    if fault=='input-link':
        path=x.attempt/'authorization.json';path.rename(x.attempt/'preserved');path.symlink_to(x.attempt/'preserved')
    if fault=='input-hardlink':os.link(x.attempt/'authorization.json',x.attempt/'preserved')
    if fault=='fifo':
        path=x.base/('factory-target-'+c.t.request['request_id']+'.json');path.rename(x.base/'preserved-request');os.mkfifo(path)
    if fault=='lock-bytes':(x.root/'control/operation.lock').write_bytes(b'1')
    if fault=='request-host':rewrite(x.base/('factory-target-'+c.t.request['request_id']+'.json'),lambda v:v['target'].update(host_sha256='f'*64))
    if fault=='authorization-signature':rewrite(x.attempt/'authorization.json',lambda v:v['body']['source'].update(source_sha256='f'*64))
    before=protected_facts(x)
    with pytest.raises(Exception) as error:c.run()
    assert not isinstance(error.value,AssertionError)
    active=x.base/contract.NAME
    if fault=='existing':assert active.read_bytes()==b'prior evidence'
    elif fault=='dangling-active':assert active.is_symlink()
    else:assert not os.path.lexists(active)
    if fault=='late-lock':before.pop('control/operation.lock')
    after=protected_facts(x)
    if fault=='late-lock':after.pop('control/operation.lock');after.pop('control/kept-original.lock')
    assert after==before
    if fault=='interrupt':
        assert isinstance(error.value,ValueError)
        assert c.calls[-1] is None and len(c.calls)==2


@pytest.mark.parametrize('phase',['pending-write','pending-read','final-write','final-read'])
def test_publication_failures_keep_pending_and_never_clear_or_retry(root_case,monkeypatch,phase):
    c=root_case;x=c.x;write=boot._write;read=install.publication.read_record
    def writing(path,raw,mode):
        if (phase=='pending-write' and path.name.endswith('.pending.json')) or (phase=='final-write' and path.name==contract.NAME):
            raise OSError('synthetic disk full')
        write(path,raw,mode=mode)
    def reading(path):
        value=read(path)
        if (phase=='pending-read' and path.name.endswith('.pending.json')) or (phase=='final-read' and path.name==contract.NAME):
            return value+b' '
        return value
    monkeypatch.setattr(boot,'_write',writing);monkeypatch.setattr(install.publication,'read_record',reading)
    state=(x.root/'state.json').read_bytes()
    with pytest.raises((ValueError,OSError)):c.run()
    assert (x.root/'state.json').read_bytes()==state and c.launch.call_count==1
    assert bool(list(x.base.glob('factory-activation-*.pending.json')))==(phase!='pending-write')
    assert (x.base/contract.NAME).exists()==(phase=='final-read')
    if phase=='final-read':contract.validate_record(boot.decode((x.base/contract.NAME).read_bytes()))


@pytest.mark.parametrize('values',[{}, {'cutover_authorized':True},{'source_isolated':True},
                                  {'cutover_authorized':1,'source_isolated':True}])
def test_explicit_current_authorization_and_source_isolation_are_required(root_case,values):
    c=root_case
    with pytest.raises(ValueError,match='当次'):
        install.activate(c.x.id,c.t.request['request_id'],c.x.args.input_attempt,sha(c.x.attempt/'authorization.json'),**values)
    c.launch.assert_not_called()


@pytest.mark.parametrize('fd_kind',['other-file','other-lock','standard-fd','bool'])
def test_inherited_fd_must_be_exact_existing_private_lock(target,fd_kind):
    t=target
    path=t.c.root/'wrong.lock';path.write_bytes(b'0');path.chmod(0o600)
    if fd_kind=='other-file':path.write_bytes(b'other')
    with path.open('rb') as stream:
        fd=0 if fd_kind=='standard-fd' else True if fd_kind=='bool' else stream.fileno()
        with pytest.raises(ValueError):
            with worker.audit.verified_restored_target(t.request['request_id'],t.x.args.input_attempt,
                    sha(t.x.attempt/'authorization.json'),inherited_lock=fd):pytest.fail('foreign FD accepted')


def test_borrowing_does_not_skip_real_lock_acquisition(target):
    t=target;path=t.x.root/'control/operation.lock'
    with path.open('rb') as owner,path.open('rb') as independent:
        fcntl.flock(owner,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with pytest.raises(ValueError):
            with worker.audit.verified_restored_target(t.request['request_id'],t.x.args.input_attempt,
                    sha(t.x.attempt/'authorization.json'),inherited_lock=independent.fileno()):pytest.fail('contended FD accepted')


def test_stdlib_administrator_pack_imports_on_system_python_without_runtime_dependencies():
    root=Path(__file__).resolve().parents[2]
    code="""
import sys
sys.path.insert(0,sys.argv[1])
from desktop_assistant import factory_activation_contract,mac_factory_activation_install,mac_smb_stream_setup
from desktop_assistant.mac_keychain_install import FILES
from scripts.uat.build_mac_keychain_installer import FILES as BUILT
assert FILES==BUILT and len(FILES)==len(set(FILES))==21
assert {'desktop_assistant/mac_smb_stream_contract.py',
        'desktop_assistant/mac_smb_stream_runtime_contract.py',
        'desktop_assistant/mac_smb_stream_setup.py'} <= set(FILES)
assert not any(name in sys.modules for name in ('cryptography','sqlalchemy','psutil','desktop_assistant.manager'))
"""
    result=subprocess.run(['/usr/bin/python3','-I','-B','-S','-c',code,str(root)],capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr


def test_actual_ordinary_user_entry_refuses_before_runtime_imports():
    entry=Path(__file__).resolve().parents[2]/contract.ENTRY
    result=subprocess.run([TEST_PYTHON,'-I','-B',str(entry)],capture_output=True,text=True,timeout=10)
    assert result.returncode!=0 and '最终激活核验未确认' in result.stderr
    assert 'Traceback' not in result.stderr and not result.stdout
