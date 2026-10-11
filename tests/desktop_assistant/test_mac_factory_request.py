"""Root/identity adapters, actual bounded IO/FD locks; no real activation."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock
import pytest

from desktop_assistant import mac_factory_request_install as m, mac_factory_request as worker
from desktop_assistant import mac_keychain_install as boot, mac_system_service as system
from desktop_assistant import factory_target_contract as contract, factory_cutover as cutover
from desktop_assistant.storage import write_json
from desktop_assistant.operation_lock import operation_lock, BUSY
from tests.desktop_assistant.test_factory_network import lan
from tests.desktop_assistant.test_mac_service_manage import instance

pytestmark=pytest.mark.skipif(sys.platform!='darwin',reason='Mac target request native FD/ACL and system Python')


def manifest():
    return {'runtime_capabilities':{'factory_request_v1':1},'reader_capabilities':{'factory_cutover_v1':1},
        'files':{key:'a'*64 for key in [m.ENTRY,m.MODULE,'desktop_assistant/factory_network.py',
                                     'desktop_assistant/factory_target_contract.py']}}


@pytest.fixture
def root_case(tmp_path,monkeypatch):
    base=tmp_path/'system';base.mkdir();context={'uid':499,'gid':499,'installation_id':'b'*32}
    monkeypatch.setattr(system,'BASE',base);monkeypatch.setattr(system,'ROOT',base/'instance')
    monkeypatch.setattr(boot,'modules',lambda:(SimpleNamespace(_administrator=lambda:None),None))
    monkeypatch.setattr(boot,'verify_code',lambda:'d'*64)
    monkeypatch.setattr(boot,'protected',lambda path,**kw:Path(path).read_bytes())
    monkeypatch.setattr(m.publication,'read_record',lambda path:Path(path).read_bytes())
    @contextmanager
    def locked():yield
    monkeypatch.setattr(m.publication,'lock',locked)
    meta={'context':context,'context_sha256':'c'*64,'registry_sha256':'d'*64,'publisher_sha256':'e'*64,
          'tree':base/'published','package':'f'*64}
    monkeypatch.setattr(system,'metadata',lambda package:deepcopy(meta))
    monkeypatch.setattr(system,'_program_manifest',lambda meta:manifest())
    monkeypatch.setattr(contract,'host_hash',lambda:'a'*64)
    calls=[];responses=[]
    class Process:
        returncode=0
        def communicate(self,raw):
            calls.append(raw)
            value=boot.decode(raw if raw is not None else calls[0]);reply=m.expected_reply(value,context)
            if responses:
                replacement=responses.pop(0)
                if isinstance(replacement,BaseException):raise replacement
                replacement(value,reply)
            return boot.canonical(reply),b''
        def kill(self):pytest.fail('never kill a request worker')
    process=Process()
    def popen(argv,**kw):
        assert argv==[str(meta['tree']/'runtime/bin/python3.12'),'-I','-B',str(meta['tree']/m.ENTRY)]
        assert kw['user']==kw['group']==499 and kw['extra_groups']==[] and kw['pass_fds']==()
        assert kw['env']['ERP_HOME_REHEARSAL']=='1' and kw['cwd']=='/' and kw['umask']==0o077
        return process
    launch=Mock(side_effect=popen);monkeypatch.setattr(m.subprocess,'Popen',launch)
    return SimpleNamespace(base=base,context=context,meta=meta,calls=calls,responses=responses,launch=launch,process=process)


def test_root_saves_public_network_then_exact_request_without_activation(root_case):
    c=root_case;result=m.prepare('f'*64,lan())
    request=boot.decode(Path(result['request_path']).read_bytes());contract.validate_request(request)
    assert request['target']['root']==str(system.ROOT) and request['target']['service_uid']==499
    assert request['network_sha256']==contract.digest(lan())
    assert boot.decode(Path(result['network_path']).read_bytes())==lan()
    assert all(Path(result[key]).stat().st_mode&0o777==0o644 for key in ('request_path','network_path'))
    assert not any(result[key] for key in ('factory_activated','runtime_configuration_changed',
        'network_reachability_verified','final_backup_verified','credentials_verified','source_isolation_verified'))
    assert not system.ROOT.exists() and len(list(c.base.iterdir()))==2


@pytest.mark.parametrize('fault',['identity','extra','challenge','hash','flags','bool-type','interrupt','exit','changed-root','expired','capability'])
def test_root_rejects_invalid_worker_or_changed_binding_before_files(root_case,monkeypatch,fault):
    c=root_case
    def corrupt(value,reply):
        if fault=='identity':reply['identity']['uid']=501
        if fault=='extra':reply['secret']='synthetic forbidden'
        if fault=='challenge':reply['challenge']='0'*32
        if fault=='hash':reply['request_sha256']='0'*64
        if fault=='flags':reply['factory_activated']=True
        if fault=='bool-type':reply['manual_stop']=1
        if fault=='changed-root':c.meta['registry_sha256']='0'*64
        if fault=='expired':
            expired=contract._time(value['request']['expires_at'])
            monkeypatch.setattr(contract,'_now',lambda value=None:expired)
    c.responses.append(corrupt)
    if fault=='interrupt':c.responses[:]=[KeyboardInterrupt()]
    if fault=='exit':c.process.returncode=1
    if fault=='capability':monkeypatch.setattr(system,'_program_manifest',lambda _: {'runtime_capabilities':{},'files':{}})
    with pytest.raises(ValueError):m.prepare('f'*64,lan())
    assert not list(c.base.iterdir())
    if fault=='interrupt':assert len(c.calls)==2 and c.calls[1] is None
    if fault=='capability':c.launch.assert_not_called()


def test_failed_final_request_write_retains_network_and_does_not_change_instance(root_case,monkeypatch):
    c=root_case;write=boot._write
    def fail(path,raw,mode):
        if path.name.startswith('factory-target-'):raise OSError('synthetic full disk')
        write(path,raw,mode)
    monkeypatch.setattr(boot,'_write',fail)
    with pytest.raises(OSError):m.prepare('f'*64,lan())
    files=list(c.base.iterdir());assert len(files)==1 and files[0].name.startswith('factory-network-')
    assert boot.decode(files[0].read_bytes())==lan() and not system.ROOT.exists()


def test_request_collision_preserves_old_bytes(root_case,monkeypatch):
    c=root_case;monkeypatch.setattr(contract.uuid,'uuid4',lambda:SimpleNamespace(hex='a'*32))
    old=c.base/('factory-target-'+'a'*32+'.json');old.write_bytes(b'retained prior evidence')
    with pytest.raises(ValueError,match='已存在'):m.prepare('f'*64,lan())
    assert old.read_bytes()==b'retained prior evidence' and len(list(c.base.iterdir()))==1


@pytest.mark.parametrize('fault',['none','sha','extra','link','hardlink','fifo','oversize','duplicate','changing'])
def test_approved_network_file_is_bounded_nofollow_and_exact(tmp_path,monkeypatch,fault):
    path=tmp_path/'network.json';raw=boot.canonical(lan());path.write_bytes(raw);expected=boot.digest(raw)
    if fault=='sha':expected='0'*64
    if fault=='extra':path.write_bytes(boot.canonical({**lan(),'secret':'synthetic'}));expected=boot.digest(path.read_bytes())
    if fault=='duplicate':path.write_bytes(b'{"x":1,"x":2}');expected=boot.digest(path.read_bytes())
    if fault=='link':path.rename(tmp_path/'retained');path.symlink_to(tmp_path/'retained')
    if fault=='hardlink':os.link(path,tmp_path/'hardlink')
    if fault=='fifo':path.unlink();os.mkfifo(path)
    if fault=='oversize':path.write_bytes(b'x'*65537)
    if fault=='changing':
        read=os.read
        def changed(fd,count):
            data=read(fd,count)
            if data:path.write_bytes(raw+b' ')
            return data
        monkeypatch.setattr(m.os,'read',changed)
    if fault=='none':assert m.read_network(path,expected)==lan()
    else:
        with pytest.raises((OSError,ValueError)):m.read_network(path,expected)


@pytest.fixture
def target_case(instance,monkeypatch):
    a=instance;a.context['installation_id']='b'*32
    repo=Path(worker.__file__).resolve().parent.parent
    monkeypatch.setattr(sys,'flags',SimpleNamespace(**{**{k:getattr(sys.flags,k) for k in dir(sys.flags)
        if not k.startswith('_')},'isolated':1}));monkeypatch.setattr(sys,'dont_write_bytecode',True)
    monkeypatch.setattr(worker.keychain,'selected',lambda:True)
    monkeypatch.setattr(worker.keychain,'load_context',lambda:a.context)
    meta={'context':a.context,'context_sha256':'c'*64,'registry_sha256':'d'*64,'publisher_sha256':'e'*64,'public':b'synthetic'}
    monkeypatch.setattr(system,'metadata',lambda _:meta)
    current_manifest=manifest();monkeypatch.setattr(worker.published,'resolve',lambda *a,**kw:(repo,current_manifest))
    target={'host_sha256':'a'*64,'installation_id':'b'*32,'service_uid':a.context['uid'],'service_gid':a.context['gid'],
        'service_account':'_tianmingerp','root':str(a.root)}
    actual_target=Mock(return_value=target);monkeypatch.setattr(cutover,'local_target',actual_target)
    value={'request':contract.make_request(target,'c'*64,contract.digest(lan()),'e'*64),'network':lan(),
           'challenge':'f'*32,'context_sha256':'c'*64,'registry_sha256':'d'*64}
    a.manager._approve_execution=Mock()
    def run(**kw):return worker.run(value,script=kw.get('script',repo/m.ENTRY),executable=kw.get('executable',repo/'runtime/bin/python3.12'))
    return SimpleNamespace(a=a,value=value,meta=meta,manifest=current_manifest,target=actual_target,run=run)


def test_worker_is_readonly_under_existing_lock_and_never_reads_credentials(target_case):
    c=target_case;a=c.a;before={p:p.read_bytes() for p in a.root.rglob('*') if p.is_file()}
    def approve(package):
        assert package=='c'*64
        with pytest.raises(ValueError,match=BUSY):
            with operation_lock(a.root/'control/operation.lock'):pass
    a.manager._approve_execution.side_effect=approve
    assert c.run()==m.expected_reply(c.value,a.context)
    assert before=={p:p.read_bytes() for p in before}
    a.canary.assert_not_called();a.unprotect.assert_not_called();a.nas.assert_not_called()
    assert a.manager.starts==a.manager.stops==a.manager.backups==0


@pytest.mark.parametrize('fault',['network','identity','registry','context','publisher','script','interpreter',
    'capability','target','current','running','manual','pending','failed','lock','approve'])
def test_worker_refuses_unprepared_or_changed_target(target_case,monkeypatch,fault):
    c=target_case;a=c.a;options={}
    if fault=='network':c.value['network']['ERP_PORT']='8001'
    if fault=='identity':monkeypatch.setattr(boot,'_actual',lambda:{'uid':0})
    if fault in {'registry','context'}:c.value[fault+'_sha256']='0'*64
    if fault=='publisher':c.meta['publisher_sha256']='0'*64
    if fault=='script':options['script']=a.root/'wrong.py'
    if fault=='interpreter':options['executable']=sys.executable
    if fault=='capability':c.manifest['reader_capabilities']['factory_cutover_v1']=True
    if fault=='target':c.target.return_value={**c.value['request']['target'],'host_sha256':'0'*64}
    state=a.manager.state
    if fault=='current':state['current']='0'*64
    if fault=='running':a.manager.running=True
    if fault=='manual':state['manual_stop']=False
    if fault=='pending':state['onboarding_pending']=True
    if fault=='failed':state['operation']='backup_failed'
    write_json(a.root/'state.json',state)
    if fault=='lock':(a.root/'control/operation.lock').write_bytes(b'changed')
    if fault=='approve':a.manager._approve_execution.side_effect=ValueError('unapproved program')
    before={p:p.read_bytes() for p in a.root.rglob('*') if p.is_file()}
    with pytest.raises((ValueError,OSError)):c.run(**options)
    assert before=={p:p.read_bytes() for p in before}
    a.canary.assert_not_called();a.unprotect.assert_not_called();a.nas.assert_not_called()
    assert a.manager.starts==a.manager.stops==a.manager.backups==0


def test_ordinary_uid_entry_fails_without_echoing_arguments():
    path=Path(worker.__file__).parent/'factory_request_entry.py'
    r=subprocess.run([sys.executable,'-I','-B',str(path),'synthetic-private-argument'],capture_output=True)
    assert r.returncode and '目标切换申请未确认'.encode() in r.stderr
    assert b'Traceback' not in r.stderr and b'synthetic-private-argument' not in r.stderr


def test_root_contract_is_importable_in_actual_system_stdlib_interpreter():
    root=Path(worker.__file__).resolve().parent.parent
    code="import sys;sys.path.insert(0,sys.argv[1]);import desktop_assistant.mac_factory_request_install;assert 'cryptography' not in sys.modules;assert 'app' not in sys.modules;print('stdlib_only')"
    r=subprocess.run(['/usr/bin/python3','-I','-B','-S','-c',code,str(root)],capture_output=True,text=True,check=True)
    assert r.stdout.strip()=='stdlib_only'
