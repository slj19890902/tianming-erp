"""Real SMB structures with a synthetic peer; no NAS login or business files."""
from contextlib import contextmanager
import types
import pytest

pytest.importorskip('smbprotocol')
from smbprotocol import open as smb
from smbprotocol.exceptions import ObjectNameNotFound, SMB2ErrorResponse
from smbprotocol.file_info import FileDispositionInformation, FileFsFullSizeInformation
from smbprotocol.header import SMB2HeaderResponse

from desktop_assistant import mac_smb_stream_probe as subject
from desktop_assistant import mac_smb_stream_runtime as runtime
from test_mac_smb_stream_files import Peer as FilePeer, contract
from test_mac_smb_stream_runtime import approved, dispatched, control

ATTEMPT='a'*32
PREFIX='BoxERP\\mac-server-backups\\tianming-connectivity-'+ATTEMPT
PENDING=PREFIX+'.partial'
FINAL=PREFIX+'.verified'
REQUIRED=1024**3


def error(status):
    header=SMB2HeaderResponse();header['status']=status;header['data']=SMB2ErrorResponse().pack()
    return header


class Peer(FilePeer):
    def __init__(self):
        super().__init__()
        self.capacity=dict(total_allocation_units=1000000,caller_available_units=900000,
            actual_available_units=900000,sectors_per_unit=8,bytes_per_sector=512)
        self.removal=True;self.after=lambda *a:None

    def reply(self,request):
        cmd=request.COMMAND
        if cmd==5 and request['create_disposition'].get_value()==1:
            name=request['buffer_path'].get_value().decode('utf-16-le').rstrip('\0')
            if name not in self.nodes:
                self.requests.append(request);self.events.append(('absent',name))
                raise ObjectNameNotFound(error(0xc0000034))
        if cmd==16 and request['info_type'].get_value()==FileFsFullSizeInformation.INFO_TYPE:
            assert request['file_info_class'].get_value()==FileFsFullSizeInformation.INFO_CLASS
            assert request['output_buffer_length'].get_value()==32
            node=self.handles[request['file_id'].get_value()]
            assert node['directory'] and node['name']=='BoxERP\\mac-server-backups'
            info=FileFsFullSizeInformation()
            for key,value in self.capacity.items():info[key]=value
            result=smb.SMB2QueryInfoResponse();result['buffer']=info.pack()
            self.events.append(('space',node['name']))
        elif cmd==17 and request['file_info_class'].get_value()==FileDispositionInformation.INFO_CLASS:
            assert request['info_type'].get_value()==FileDispositionInformation.INFO_TYPE
            node=self.handles[request['file_id'].get_value()]
            assert node['name']==FINAL
            info=FileDispositionInformation();assert info.unpack(request['buffer'].get_value())==b''
            assert info['delete_pending'].get_value() is True
            node['delete_pending']=True
            self.events.append(('delete-held',node['name']))
            result=smb.SMB2SetInfoResponse()
        else:
            node=self.handles.get(request['file_id'].get_value()) if cmd==6 else None
            result=super().reply(request)
            if cmd==6 and node['delete_pending'] and self.removal:
                assert self.nodes[node['name']] is node
                del self.nodes[node['name']]
            self.after(cmd,node,result)
            return result
        self.requests.append(request);self.hook(cmd,node,result)
        header=SMB2HeaderResponse();header['command']=cmd;header['session_id']=17;header['tree_id']=23
        header['data']=result.pack();self.after(cmd,node,header)
        return header


def run(peer,required=REQUIRED):
    return subject.check(peer.tree(),contract(),ATTEMPT,required)


def test_canary_write_read_rename_delete_and_capacity_are_all_confirmed():
    peer=Peer();proof=run(peer)
    assert subject.validate(proof,contract(),ATTEMPT,REQUIRED)==proof
    assert proof['available_bytes']==900000*4096 and proof['bytes']==16384
    assert proof['canary_removed'] is True and proof['backup_authenticated'] is False
    assert set(peer.nodes)==set(subject.files.DIRECTORIES) and not peer.handles
    events=[kind for kind,_ in peer.events]
    assert events.index('read')<events.index('rename')<events.index('delete-held')
    assert peer.events.count(('delete-held',FINAL))==1
    create=[r for r in peer.requests if r.COMMAND==5 and r['create_disposition'].get_value()==2]
    assert len(create)==1 and create[0]['desired_access'].get_value() & 0x10003==0x10003


def test_caller_quota_is_not_assumed_to_be_total_volume_capacity():
    peer=Peer();peer.capacity.update(total_allocation_units=100,caller_available_units=90,
        actual_available_units=900000)
    proof=run(peer,subject.SIZE)
    assert proof['available_bytes']==90*4096


def test_physical_space_is_used_when_lower_than_caller_report():
    peer=Peer();peer.capacity['actual_available_units']=50
    assert run(peer,subject.SIZE)['available_bytes']==50*4096


@pytest.mark.parametrize('key,value',[('caller_available_units',0),('caller_available_units',1000001),
    ('total_allocation_units',-1),('actual_available_units',-1),('sectors_per_unit',0),
    ('bytes_per_sector',0),('overflow',0)])
def test_unavailable_or_invalid_space_rejected_before_canary_creation(key,value):
    peer=Peer()
    if key=='overflow':peer.capacity.update(sectors_per_unit=2**32-1,bytes_per_sector=2**32-1)
    else:peer.capacity[key]=value
    with pytest.raises(ValueError):run(peer)
    assert set(peer.nodes)==set(subject.files.DIRECTORIES)
    assert not any(e[0] in ('write','delete-held') for e in peer.events)


@pytest.mark.parametrize('kind',['write-zero','write-too-large','read-corrupt','read-empty','file-identity',
    'directory-change','rename-collision','old-canary'])
def test_failed_checks_never_delete_existing_or_unverified_objects(kind):
    peer=Peer();original=b'older-evidence'
    old=peer.new_node('BoxERP\\mac-server-backups\\existing.tmbackup',data=original)
    if kind=='old-canary':peer.new_node(PENDING,data=original)
    if kind=='rename-collision':peer.new_node(FINAL,data=original)
    def hook(cmd,node,result):
        if cmd==9 and kind=='write-zero':result['count']=0
        if cmd==9 and kind=='write-too-large':result['count']=999999
        if cmd==8 and kind=='read-corrupt':result['buffer']=b'bad'
        if cmd==8 and kind=='read-empty':result['buffer']=b''
        if cmd==9 and kind=='file-identity':node['index']+=1
        if cmd==8 and kind=='directory-change':peer.nodes['BoxERP']['index']+=1
    peer.hook=hook
    with pytest.raises((ValueError,FileExistsError)):run(peer)
    assert not any(e[0]=='delete-held' for e in peer.events)
    assert bytes(old['data'])==original
    assert any(n not in subject.files.DIRECTORIES for n in peer.nodes)


def test_delete_ack_without_actual_absence_is_not_success():
    peer=Peer();peer.removal=False
    with pytest.raises(ValueError):run(peer)
    assert FINAL in peer.nodes and len([e for e in peer.events if e[0]=='delete-held'])==1


def test_delete_failure_never_issues_a_second_request():
    peer=Peer()
    def hook(cmd,node,result):
        if cmd==17 and node['delete_pending']:raise OSError('uncertain deletion')
    peer.hook=hook
    with pytest.raises(OSError):run(peer)
    # Server may already have acted; client only closes its handle afterward.
    assert len([e for e in peer.events if e[0]=='delete-held'])==1


def test_original_name_must_disappear_after_rename_before_any_delete():
    peer=Peer()
    def hook(cmd,node,result):
        if cmd==17 and node['name']==FINAL:peer.new_node(PENDING,data=b'unexpected alias')
    peer.hook=hook
    with pytest.raises(ValueError):run(peer)
    assert PENDING in peer.nodes and FINAL in peer.nodes
    assert not any(e[0]=='delete-held' for e in peer.events)


def test_postprobe_space_loss_does_not_report_ready():
    peer=Peer()
    def hook(cmd,node,result):
        if cmd==17 and node['delete_pending']:peer.capacity['caller_available_units']=0
    peer.hook=hook
    with pytest.raises(ValueError):run(peer)
    assert not peer.handles


@pytest.mark.parametrize('field,bad',[('type','backup-success'),('contract_sha256','0'*64),
    ('connection_guid','bad'),('attempt_id','0'*32),('bytes',True),('sha256','bad'),
    ('available_bytes',True),('required_bytes',True),('canary_removed',False),
    ('backup_authenticated',True),('directories',[]),('file',{'index':0,'created':1})])
def test_unbound_or_incomplete_proofs_rejected(field,bad):
    proof=run(Peer());proof[field]=bad
    with pytest.raises(ValueError):subject.validate(proof,contract(),ATTEMPT,REQUIRED)


def probe_request(binding):
    return dict(challenge='2'*32,control_sha256=binding.control_sha,
        contract_sha256=binding.control['contract_sha256'],action='probe',socket_fd=100,
        attempt_id=ATTEMPT,required_bytes=REQUIRED)


def bound_proof(binding):
    proof=run(Peer());proof['contract_sha256']=binding.control['contract_sha256'];return proof


@pytest.mark.parametrize('field,bad',[('file_fd',101),('job','other'),('required_bytes',True),
    ('required_bytes',0),('attempt_id','../x'),('socket_fd',2),('socket_fd',True)])
def test_probe_request_never_accepts_data_descriptor_or_arbitrary_scope(approved,field,bad):
    binding=runtime.load_binding();request=probe_request(binding);request[field]=bad
    with pytest.raises(ValueError):runtime._request(request,binding)


def test_probe_parent_only_passes_network_descriptor(dispatched,monkeypatch):
    case=dispatched;binding=case.binding;request=probe_request(binding);events=[]
    def forbidden(*a):raise AssertionError('probe cannot lease business data')
    monkeypatch.setattr(runtime,'file_lease',forbidden)
    def popen(argv,**kw):
        events.append(kw)
        def communicate(raw,**options):
            assert 0<options['timeout']<=runtime.PROBE_TIMEOUT
            assert runtime.boot.decode(raw)==request
            result=dict(type=runtime.RESULT,challenge=request['challenge'],control_sha256=binding.control_sha,
                package_sha256=binding.control['package_sha256'],identity=binding.identity,
                state_published=False,observation=bound_proof(binding))
            return runtime.cfg.canonical(result),b''
        return types.SimpleNamespace(communicate=communicate,returncode=0)
    monkeypatch.setattr(runtime.subprocess,'Popen',popen)
    result=runtime._dispatch(binding,request)
    assert result['type']==subject.TYPE and events[0]['pass_fds']==(100,)


def test_probe_timeout_kills_only_exact_temporary_child_and_observes_exit(dispatched,monkeypatch):
    case=dispatched;events=[]
    def communicate(raw,**kw):
        events.append(('communicate',raw,kw))
        if len(events)==1:raise runtime.subprocess.TimeoutExpired('owned-probe',runtime.PROBE_TIMEOUT)
        return b'{}',b''
    process=types.SimpleNamespace(communicate=communicate,returncode=-9,kill=lambda:events.append(('kill',)))
    monkeypatch.setattr(runtime.subprocess,'Popen',lambda *a,**kw:process)
    with pytest.raises(ValueError):runtime._dispatch(case.binding,probe_request(case.binding))
    assert [e[0] for e in events]==['communicate','kill','communicate']
    assert events[-1][1] is None and events[-1][2]=={}
    assert case.events.count('socket')==1


def test_probe_worker_authenticates_and_only_tests_its_canary(approved,monkeypatch):
    peer=Peer();events=[];cfg=runtime.cfg
    ctl=control();value=contract();ctl['contract_sha256']=subject.contract.digest(value)
    ctl['credential_account']=cfg.account(ctl['context_sha256'],ctl['contract_sha256'],ctl['attempt_id'])
    binding=types.SimpleNamespace(control=ctl,value=value,context=approved.context,control_sha='9'*64,
        tree=approved.tree,check=lambda:None)
    monkeypatch.setattr(runtime,'load_binding',lambda:binding)
    monkeypatch.setattr(runtime,'__file__',str(binding.tree/cfg.MODULE))
    flags={name:getattr(runtime.sys.flags,name) for name in dir(runtime.sys.flags) if not name.startswith('_')}
    monkeypatch.setattr(runtime.sys,'flags',types.SimpleNamespace(**(flags|{'isolated':1})))
    monkeypatch.setattr(runtime.sys,'dont_write_bytecode',True)
    monkeypatch.setattr(runtime,'denied_new_connection',lambda:events.append('denied'))
    def forbidden(*a):raise AssertionError('probe has no local backup descriptor')
    monkeypatch.setattr(runtime,'file_lease',forbidden)
    monkeypatch.setattr(runtime.transport,'Channel',lambda fd,peer:types.SimpleNamespace(close=lambda:events.append('closed')))
    def get(account):events.append(account);return b'SYNTHETIC_ONLY'
    monkeypatch.setattr(runtime.keychain,'ServiceStore',lambda:types.SimpleNamespace(value=binding.context,get=get))
    @contextmanager
    def share(channel,value,context,uid,provider):
        events.append('negotiation_mocked');assert provider()=='SYNTHETIC_ONLY'
        yield peer.tree()
    monkeypatch.setattr(runtime.session,'open_share',share)
    result=runtime.run(probe_request(binding),script=binding.tree/cfg.ENTRY,
        executable=binding.tree/'runtime/bin/python3.12')
    assert result['state_published'] is False and result['observation']['canary_removed'] is True
    assert events==['denied','negotiation_mocked',ctl['credential_account'],'closed']
    assert set(peer.nodes)==set(subject.files.DIRECTORIES)
