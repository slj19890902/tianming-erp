"""Synthetic root approvals and file/socket ownership, never NAS credentials."""
from contextlib import contextmanager
from copy import deepcopy
import errno
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import pytest

from desktop_assistant import mac_smb_stream_runtime as subject
from desktop_assistant import mac_smb_stream_runtime_contract as cfg
from test_mac_smb_stream_transport import contract

MAC=pytest.mark.skipif(sys.platform!='darwin',reason='actual macOS FD/path/ACL leases')
NAME='20261012-030000-1234abcd.tmbackup'


def control():
    value=dict(type=cfg.TYPE,attempt_id='1'*32,plan_sha256='c'*64,package_sha256='d'*64,
        context_sha256='a'*64,publisher_sha256='e'*64,contract_sha256=subject.archive.digest(contract()))
    value['credential_account']=cfg.account(value['context_sha256'],value['contract_sha256'],value['attempt_id'])
    return value


@pytest.mark.parametrize('key,bad',[('type','tianming.mac-smb-service.v1'),('attempt_id','../x'),
    ('plan_sha256',True),('package_sha256',''),('context_sha256','f'*64),('publisher_sha256',None),
    ('contract_sha256','f'*64),('credential_account','backup:old'),('password','SYNTHETIC')])
def test_control_rejects_other_backend_or_identity_or_secret(key,bad):
    value=control();value[key]=bad
    with pytest.raises(ValueError):cfg.validate(value,contract(),'a'*64,499)


def test_control_separate_namespace_and_deny_all_profile():
    value=control();assert cfg.validate(value,contract(),'a'*64,499)==value
    assert value['credential_account'].startswith('nas-smb-stream-v1:')
    assert cfg.PROFILE=='(version 1)(allow default)(deny network-outbound)'
    assert cfg.CAPABILITY=='archive_smb_stream_v1'


@pytest.mark.parametrize('value',[None,True,False,0,2,'1'])
def test_unsigned_or_missing_capability_fails(value):
    with pytest.raises(ValueError):cfg.capability(dict(runtime_capabilities={cfg.CAPABILITY:value},files=dict.fromkeys(cfg.FILES)))


@pytest.fixture
def approved(monkeypatch,tmp_path):
    context=dict(uid=499,gid=499)
    ids=dict(uid=499,euid=499,gid=499,egid=499,kernel_groups=[499])
    tree=tmp_path/'approved'
    records={cfg.CONTROL:cfg.canonical(control()),cfg.CONTRACT:cfg.canonical(contract()),
             subject.published.REGISTRY:b'registry'}
    meta=dict(context=context,context_sha256='a'*64,publisher_sha256='e'*64,
        registry_sha256=cfg.digest(b'registry'),tree=tree,public=b'public')
    manifest=dict(runtime_capabilities={cfg.CAPABILITY:1},files=dict.fromkeys(cfg.FILES))
    monkeypatch.setattr(subject.keychain,'load_context',lambda:deepcopy(context))
    monkeypatch.setattr(subject.keychain,'selected',lambda:True)
    monkeypatch.setattr(subject.boot,'_actual',lambda:deepcopy(ids))
    monkeypatch.setattr(subject.identity,'permitted_groups',lambda gid:{499})
    monkeypatch.setattr(subject.service,'metadata',lambda package:meta)
    monkeypatch.setattr(subject.publication,'read_record',lambda path:records[path])
    monkeypatch.setattr(subject.published,'resolve',lambda *a,**kw:(tree,manifest))
    monkeypatch.setattr(subject.mounts,'local_addresses',lambda:{'127.0.0.1'})
    monkeypatch.setattr(subject.sys,'platform','darwin')
    return SimpleNamespace(context=context,ids=ids,tree=tree,records=records,meta=meta,manifest=manifest)


def test_root_binding_rechecks_records_and_actual_identity(approved):
    binding=subject.load_binding();binding.check()
    approved.records[cfg.CONTROL]+=b' '
    with pytest.raises(ValueError):binding.check()


@pytest.mark.parametrize('kind',['uid','group','publisher','registry','files','capability','canonical','local'])
def test_root_substitutions_fail_before_any_worker(approved,monkeypatch,kind):
    if kind=='uid':approved.ids['euid']=501
    if kind=='group':approved.ids['kernel_groups']=[499,0]
    if kind=='publisher':approved.meta['publisher_sha256']='0'*64
    if kind=='registry':approved.records[subject.published.REGISTRY]=b'changed'
    if kind=='files':approved.manifest['files'].pop(cfg.ENTRY)
    if kind=='capability':approved.manifest['runtime_capabilities'][cfg.CAPABILITY]=True
    if kind=='canonical':approved.records[cfg.CONTRACT]+=b'\n'
    if kind=='local':monkeypatch.setattr(subject.mounts,'local_addresses',lambda:{'192.168.1.245'})
    with pytest.raises(ValueError):subject.load_binding()


def request(binding,action='upload'):
    return dict(challenge='2'*32,control_sha256=binding.control_sha,
        contract_sha256=binding.control['contract_sha256'],action=action,socket_fd=100,file_fd=101,
        job='smb-stream-'+'3'*32,filename=NAME,size=256,sha256='4'*64,attempt_id='5'*32,upload=None)


def proof(binding,kind=subject.files.UPLOAD):
    return dict(type=kind,contract_sha256=binding.control['contract_sha256'],
        connection_guid=('6' if kind==subject.files.UPLOAD else '7')*32,
        filename=NAME,bytes=256,sha256='4'*64,directories=[dict(index=i,created=100) for i in (1,2,3)],
        file=dict(index=4,created=200),backup_authenticated=False)


@pytest.mark.parametrize('key,bad',[('challenge','x'),('control_sha256','0'*64),('contract_sha256','0'*64),
    ('action','delete'),('socket_fd',True),('file_fd',2),('file_fd',100),('job','../other'),
    ('filename','a/other'),('size',True),('sha256','missing'),('attempt_id','bad'),('upload',{})])
def test_request_refuses_arbitrary_file_action_or_descriptors(approved,key,bad):
    binding=subject.load_binding();value=request(binding);value[key]=bad
    with pytest.raises(ValueError):subject._request(value,binding)


@pytest.mark.parametrize('key,bad',[('type','other'),('filename','other'),('bytes',True),('sha256','0'*64),
    ('directories',[]),('file',dict(index=0,created=1)),('connection_guid','bad'),('backup_authenticated',True)])
def test_readback_requires_exact_provisional_upload(approved,key,bad):
    binding=subject.load_binding();value=request(binding,'download');value['upload']=proof(binding)
    subject._request(value,binding)
    value['upload'][key]=bad
    with pytest.raises(ValueError):subject._request(value,binding)


@pytest.fixture
def local_file(monkeypatch,tmp_path):
    base=tmp_path/'base';root=base/'instance';job=root/'staging'/('smb-stream-'+'3'*32)
    job.mkdir(parents=True,mode=0o700)
    for path in (base,root,root/'staging',job):path.chmod(0o700)
    source=job/NAME;source.write_bytes(b'x'*256);source.chmod(0o600)
    dest=job/'download.tmbackup';dest.touch(mode=0o600)
    source_fd=os.open(source,os.O_RDONLY);dest_fd=os.open(dest,os.O_RDWR)
    monkeypatch.setattr(cfg,'BASE',base)
    binding=SimpleNamespace(context=dict(uid=os.geteuid(),gid=os.getegid()))
    yield SimpleNamespace(binding=binding,source=source,dest=dest,source_fd=source_fd,dest_fd=dest_fd,job=job)
    os.close(source_fd);os.close(dest_fd)


@MAC
def test_file_lease_preserves_parent_descriptors_and_checks_download(local_file):
    case=local_file
    value=dict(action='upload',file_fd=case.source_fd,job=case.job.name,filename=NAME,size=256)
    with subject.file_lease(case.binding,value) as held:assert os.read(held.fd,1)==b'x'
    assert os.fstat(case.source_fd).st_size==256
    value.update(action='download',file_fd=case.dest_fd)
    with subject.file_lease(case.binding,value) as held:os.pwrite(held.fd,b'download',0)
    assert os.pread(case.dest_fd,8,0)==b'download'


@MAC
@pytest.mark.parametrize('kind',['other-name','symlink','hardlink','mode','replacement','nonempty'])
def test_file_lease_rejects_foreign_or_changed_file(local_file,kind):
    case=local_file
    value=dict(action='upload',file_fd=case.source_fd,job=case.job.name,filename=NAME,size=256)
    if kind=='other-name':value['filename']='20261012-030000-1234abce.tmbackup'
    if kind=='symlink':case.source.rename(case.job/'old');case.source.symlink_to(case.job/'old')
    if kind=='hardlink':os.link(case.source,case.job/'other')
    if kind=='mode':case.source.chmod(0o640)
    if kind=='nonempty':case.dest.write_bytes(b'x');value.update(action='download',file_fd=case.dest_fd)
    with pytest.raises((ValueError,FileNotFoundError)):
        with subject.file_lease(case.binding,value):
            if kind=='replacement':case.source.rename(case.job/'old');case.source.write_bytes(b'x'*256)


@pytest.mark.parametrize('result',[0,errno.ECONNREFUSED,errno.ETIMEDOUT])
def test_unrestricted_or_unknown_network_probe_prevents_secrets(monkeypatch,result):
    @contextmanager
    def sock(*a):yield SimpleNamespace(settimeout=lambda n:None,connect_ex=lambda p:result)
    monkeypatch.setattr(subject.socket,'socket',sock)
    with pytest.raises(ValueError):subject.denied_new_connection()


@pytest.fixture
def dispatched(approved,monkeypatch):
    binding=subject.load_binding();events=[]
    @contextmanager
    def sock(*a):
        events.append('socket')
        yield SimpleNamespace(settimeout=lambda n:None,connect=lambda p:events.append(('connect',p)),
            getpeername=lambda:('192.168.1.245',445),fileno=lambda:100)
        events.append('socket_closed')
    @contextmanager
    def lease(*a):yield
    monkeypatch.setattr(subject.socket,'socket',sock)
    monkeypatch.setattr(subject,'file_lease',lease)
    outputs=[]
    def popen(argv,**kw):
        events.append(('popen',argv,kw))
        def communicate(raw):
            req=subject.boot.decode(raw);outputs.append(req)
            value=dict(type=subject.RESULT,challenge=req['challenge'],control_sha256=binding.control_sha,
                package_sha256=binding.control['package_sha256'],identity=binding.identity,
                observation=proof(binding),state_published=False)
            return cfg.canonical(value),b''
        return SimpleNamespace(communicate=communicate,returncode=0)
    monkeypatch.setattr(subject.subprocess,'Popen',popen)
    return SimpleNamespace(binding=binding,events=events,outputs=outputs)


def test_parent_connects_only_approved_peer_and_passes_exact_two_fds(dispatched):
    case=dispatched
    result=subject._dispatch(case.binding,request(case.binding))
    assert result==proof(case.binding)
    assert case.events[:2]==['socket',('connect',('192.168.1.245',445))]
    event=case.events[2];argv,kw=event[1:]
    assert argv[:3]==['/usr/bin/sandbox-exec','-p',cfg.PROFILE]
    assert kw['pass_fds']==(100,101) and kw['close_fds'] and kw['umask']==0o077
    assert 'PYTHONPATH' not in kw['env'] and kw['cwd']=='/'
    assert case.events[-1]=='socket_closed'
    assert case.outputs[0]['socket_fd']==100


@pytest.mark.parametrize('kind',['failure','oversize','challenge','identity','success-state','proof'])
def test_parent_rejects_failed_or_unbound_output(dispatched,monkeypatch,kind):
    case=dispatched;binding=case.binding;req=request(binding)
    value=dict(type=subject.RESULT,challenge=req['challenge'],control_sha256=binding.control_sha,
        package_sha256=binding.control['package_sha256'],identity=binding.identity,
        observation=proof(binding),state_published=False)
    if kind=='challenge':value['challenge']='f'*32
    if kind=='identity':value['identity']={}
    if kind=='success-state':value['state_published']=True
    if kind=='proof':value['observation']['backup_authenticated']=True
    raw=b'x'*(subject.LIMIT+1) if kind=='oversize' else cfg.canonical(value)
    monkeypatch.setattr(subject.subprocess,'Popen',lambda *a,**kw:SimpleNamespace(
        communicate=lambda data:(raw,b'never expose synthetic stderr'),returncode=1 if kind=='failure' else 0))
    with pytest.raises(ValueError):subject._dispatch(binding,req)
    assert sum(e=='socket' for e in case.events)==1


def test_policy_rejected_before_dispatch(approved,monkeypatch):
    monkeypatch.setattr(subject.subprocess,'run',lambda *a,**kw:SimpleNamespace(returncode=65))
    with pytest.raises(ValueError):subject.verify_network_policy(subject.load_binding())


@pytest.mark.parametrize('raw',[b'',b'a\0b',b'\xff',b'x'*1025])
def test_credential_exact_namespace_rejects_invalid_secret(approved,monkeypatch,raw):
    binding=subject.load_binding();calls=[]
    def get(account):calls.append(account);return raw
    monkeypatch.setattr(subject.keychain,'ServiceStore',lambda:SimpleNamespace(value=binding.context,get=get))
    with pytest.raises((ValueError,UnicodeError)):subject._credential(binding)
    assert calls==[binding.control['credential_account']]


@MAC
def test_callback_uses_new_dispatch_for_each_leg_and_retains_files(local_file,monkeypatch):
    case=local_file;calls=[]
    binding=SimpleNamespace(control=control(),value=contract(),control_sha='9'*64,check=lambda:None,
        tree=Path(subject.__file__).resolve().parent.parent,context=case.binding.context)
    monkeypatch.setattr(subject,'load_binding',lambda:binding)
    monkeypatch.setattr(subject,'verify_network_policy',lambda b:None)
    def dispatch(b,req):
        calls.append(deepcopy(req))
        return proof(binding,subject.files.UPLOAD if req['action']=='upload' else subject.files.READBACK)
    monkeypatch.setattr(subject,'_dispatch',dispatch)
    result=subject.transfer(case.source_fd,case.dest_fd,NAME,256,'4'*64,'5'*32)
    assert [r['action'] for r in calls]==['upload','download']
    assert calls[0]['challenge']!=calls[1]['challenge']
    assert calls[1]['upload']==result['upload']
    assert os.fstat(case.source_fd).st_size==256 and os.fstat(case.dest_fd).st_size==0


@MAC
def test_callback_never_downloads_after_uncertain_upload(local_file,monkeypatch):
    case=local_file;calls=[]
    binding=SimpleNamespace(control=control(),value=contract(),control_sha='9'*64,check=lambda:None,
        tree=Path(subject.__file__).resolve().parent.parent,context=case.binding.context)
    monkeypatch.setattr(subject,'load_binding',lambda:binding)
    monkeypatch.setattr(subject,'verify_network_policy',lambda b:None)
    def dispatch(b,req):calls.append(req['action']);raise ValueError('uncertain')
    monkeypatch.setattr(subject,'_dispatch',dispatch)
    with pytest.raises(ValueError):subject.transfer(case.source_fd,case.dest_fd,NAME,256,'4'*64,'5'*32)
    assert calls==['upload'] and case.source.exists() and case.dest.exists()


@MAC
def test_worker_upload_and_download_actual_protocol_files_with_synthetic_auth(local_file,monkeypatch):
    import hashlib
    from test_mac_smb_stream_files import Peer, contract as peer_contract
    from desktop_assistant.storage import MAGIC
    case=local_file;peer=Peer();calls=[];channels=[]
    data=MAGIC+b'x'*(256-len(MAGIC));case.source.write_bytes(data)
    binding=SimpleNamespace(control=control(),value=peer_contract(),control_sha='9'*64,check=lambda:None,
        tree=Path(subject.__file__).resolve().parent.parent,context=case.binding.context)
    binding.control['contract_sha256']=subject.archive.digest(binding.value)
    binding.control['credential_account']=cfg.account('a'*64,binding.control['contract_sha256'],'1'*32)
    monkeypatch.setattr(subject,'load_binding',lambda:binding)
    flags={name:getattr(sys.flags,name) for name in dir(sys.flags) if not name.startswith('_')}
    monkeypatch.setattr(subject.sys,'flags',SimpleNamespace(**(flags|{'isolated':1})))
    monkeypatch.setattr(subject.sys,'dont_write_bytecode',True)
    monkeypatch.setattr(subject,'actual_identity',lambda context:context)
    monkeypatch.setattr(subject,'denied_new_connection',lambda:calls.append('denied'))
    def channel(fd,peer):
        channels.append(SimpleNamespace(closed=False))
        item=channels[-1];item.close=lambda:setattr(item,'closed',True)
        return item
    monkeypatch.setattr(subject.transport,'Channel',channel)
    def get(account):calls.append(account);return b'SYNTHETIC_ONLY'
    monkeypatch.setattr(subject.keychain,'ServiceStore',lambda:SimpleNamespace(value=binding.context,get=get))
    @contextmanager
    def share(channel,value,context,uid,provider):
        calls.append('negotiation_mocked')
        assert provider()=='SYNTHETIC_ONLY'
        yield peer.tree()
    monkeypatch.setattr(subject.session,'open_share',share)
    up=request(binding);up.update(file_fd=case.source_fd,sha256=hashlib.sha256(data).hexdigest())
    kwargs=dict(script=binding.tree/cfg.ENTRY,executable=binding.tree/'runtime/bin/python3.12')
    try: result=subject.run(up,**kwargs)
    except ValueError as error:
        # Synthetic peer failures should retain their cause in the test log;
        # the production entry deliberately returns only a generic message.
        if error.__context__ is not None: raise error.__context__
        raise
    down=dict(up,action='download',file_fd=case.dest_fd,upload=result['observation'],challenge='8'*32)
    second=subject.run(down,**kwargs)
    assert case.dest.read_bytes()==data and not peer.handles
    assert all(c.closed for c in channels) and len(channels)==2
    assert calls==['denied','negotiation_mocked',binding.control['credential_account']]*2
    assert second['observation']['connection_guid']!=result['observation']['connection_guid']
    assert second['state_published'] is False and result['state_published'] is False
    assert second['observation']['backup_authenticated'] is False


@pytest.mark.parametrize('kind',['script','executable','isolation','request','network'])
def test_worker_preflight_rejects_before_secret_or_file_access(approved,monkeypatch,kind):
    binding=subject.load_binding();value=request(binding);calls=[]
    monkeypatch.setattr(subject,'load_binding',lambda:binding)
    monkeypatch.setattr(subject,'__file__',str(binding.tree/cfg.MODULE))
    monkeypatch.setattr(subject.sys,'flags',SimpleNamespace(isolated=0 if kind=='isolation' else 1))
    monkeypatch.setattr(subject.sys,'dont_write_bytecode',True)
    def denied():raise ValueError('policy not proved')
    monkeypatch.setattr(subject,'denied_new_connection',denied)
    monkeypatch.setattr(subject,'file_lease',lambda *a:calls.append('unexpected-file'))
    monkeypatch.setattr(subject.keychain,'ServiceStore',lambda:calls.append('unexpected-secret'))
    if kind=='request':value['job']='../outside'
    with pytest.raises(ValueError):subject.run(value,
        script=binding.tree/('wrong' if kind=='script' else cfg.ENTRY),
        executable=binding.tree/('wrong' if kind=='executable' else 'runtime/bin/python3.12'))
    assert calls==[]


def test_dispatch_interruption_waits_for_same_child_then_refuses_success(dispatched,monkeypatch):
    case=dispatched;calls=[]
    def communicate(raw):
        calls.append(raw)
        if len(calls)==1:raise KeyboardInterrupt
        return b'{}',b''
    monkeypatch.setattr(subject.subprocess,'Popen',lambda *a,**kw:SimpleNamespace(
        communicate=communicate,returncode=0))
    with pytest.raises(ValueError):subject._dispatch(case.binding,request(case.binding))
    assert len(calls)==2 and calls[1] is None and case.events.count('socket')==1


@MAC
def test_actual_deny_all_child_can_use_only_inherited_connection():
    import socket
    import subprocess
    with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as listener:
        listener.bind(('127.0.0.1',0));listener.listen(1)
        with socket.create_connection(listener.getsockname(),timeout=3) as outgoing:
            incoming,_=listener.accept()
            with incoming:
                incoming.sendall(b'preconnected')
                program='''import sys
sys.path.insert(0,sys.argv[1])
from desktop_assistant.mac_smb_stream_runtime import denied_new_connection
from desktop_assistant.mac_smb_stream_transport import Channel
denied_new_connection()
channel=Channel(int(sys.argv[2]),('127.0.0.1',int(sys.argv[3])))
try:
    connection=channel.claim('127.0.0.1',int(sys.argv[3]))
    assert connection.recv(32)==b'preconnected'
    connection.sendall(b'reply')
finally: channel.close()
print('deny-all-and-inherited-channel-ok')
'''
                result=subprocess.run(['/usr/bin/sandbox-exec','-p',cfg.PROFILE,sys.executable,
                    '-I','-B','-c',program,str(Path(subject.__file__).resolve().parent.parent),
                    str(outgoing.fileno()),str(listener.getsockname()[1])],
                    pass_fds=(outgoing.fileno(),),close_fds=True,stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=10)
                assert result.returncode==0,result.stderr
                assert result.stdout.strip()==b'deny-all-and-inherited-channel-ok'
                incoming.settimeout(3);assert incoming.recv(32)==b'reply'
