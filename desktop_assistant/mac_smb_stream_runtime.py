"""Root-bound, one-file stream workers over parent-connected TCP descriptors.

Candidate only. The caller must hold the existing maintenance lock and stop ERP
before building a full backup. This layer returns transfer observations, never a
backup success receipt. Build/installation/routing remain disabled until signed
provisioning and dedicated-UID acceptance have been completed.
"""
from contextlib import ExitStack, contextmanager, nullcontext
from copy import deepcopy
import errno
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
import uuid

from app.core import mac_service_keychain as keychain
from . import mac_keychain_install as boot, mac_service_identity as identity
from . import mac_system_service as service, mac_release_publish as publication
from . import mac_published_runtime as published, mac_mount as mounts
from . import mac_smb_stream_runtime_contract as cfg, mac_smb_stream_contract as archive
from . import mac_smb_stream_transport as transport, mac_smb_stream_files as files
from . import mac_smb_stream_session as session, mac_smb_stream_backup as backup
from . import mac_smb_stream_probe as probe

LIMIT = 8192
RESULT = 'tianming.mac-smb-stream-worker.v1'
REQUEST = {'challenge','control_sha256','contract_sha256','action','socket_fd','file_fd',
           'job','filename','size','sha256','attempt_id','upload'}
PROBE_REQUEST = {'challenge','control_sha256','contract_sha256','action','socket_fd','attempt_id','required_bytes'}
PROBE_TIMEOUT = 120


def require(ok):
    if not ok: raise ValueError(cfg.ERROR)


def actual_identity(context):
    actual=boot._actual()
    require(all(actual[k]==context['uid' if k in ('uid','euid') else 'gid']
                for k in ('uid','euid','gid','egid')))
    groups=actual['kernel_groups']
    require(isinstance(groups,list) and all(type(g) is int for g in groups)
            and context['gid'] in groups and set(groups)<=identity.permitted_groups(context['gid']))
    return actual


class Binding:
    def __init__(self,control,value,context,meta,records):
        self.control,self.value,self.context=map(deepcopy,(control,value,context))
        self.tree=meta['tree'];self.records=dict(records)
        self.identity=actual_identity(context)

    @property
    def control_sha(self): return cfg.digest(self.records[cfg.CONTROL])

    def check(self):
        require(cfg.canonical(self.control)==self.records[cfg.CONTROL]
                and cfg.canonical(self.value)==self.records[cfg.CONTRACT]
                and keychain.load_context()==self.context
                and actual_identity(self.context)==self.identity)
        require(all(publication.read_record(p)==raw for p,raw in self.records.items()))
        require(self.value['server_address'] not in mounts.local_addresses())


def load_binding():
    require(sys.platform=='darwin' and keychain.selected())
    context=keychain.load_context();actual_identity(context)
    records={p:publication.read_record(p) for p in (cfg.CONTROL,cfg.CONTRACT,published.REGISTRY)}
    control,value=(boot.decode(records[p]) for p in (cfg.CONTROL,cfg.CONTRACT))
    require(isinstance(control,dict))
    meta=service.metadata(control.get('package_sha256'))
    cfg.validate(control,value,meta['context_sha256'],context['uid'])
    require(context==meta['context'] and control['publisher_sha256']==meta['publisher_sha256']
            and cfg.digest(records[published.REGISTRY])==meta['registry_sha256'])
    tree,manifest=published.resolve(cfg.BASE/'instance',control['package_sha256'],meta['public'],kind='migration')
    require(tree==meta['tree']);cfg.capability(manifest)
    binding=Binding(control,value,context,meta,records);binding.check()
    return binding


def _request(value,binding):
    require(isinstance(value,dict))
    is_probe=value.get('action')=='probe'
    require(set(value)==(PROBE_REQUEST if is_probe else REQUEST)
            and value['control_sha256']==binding.control_sha
            and value['contract_sha256']==binding.control['contract_sha256']
            and value['action'] in ('upload','download','probe'))
    for key in ('challenge','attempt_id'):
        require(isinstance(value[key],str) and re.fullmatch('[0-9a-f]{32}',value[key]))
    require(type(value['socket_fd']) is int and 3<=value['socket_fd']<=2**31-1)
    if is_probe:
        probe.specification(value['attempt_id'],value['required_bytes'])
        return value
    require(isinstance(value['job'],str) and re.fullmatch('smb-stream-[0-9a-f]{32}',value['job']))
    require(all(type(value[k]) is int and 3<=value[k]<=2**31-1 for k in ('socket_fd','file_fd'))
            and value['socket_fd']!=value['file_fd'])
    archive.filename(value['filename']);files._spec(value['size'],value['sha256'])
    if value['action']=='upload': require(value['upload'] is None)
    else: _observation(value['upload'],files.UPLOAD,value,binding)
    return value


def _observation(proof,kind,request,binding):
    if kind==probe.TYPE:
        probe.validate(proof,binding.value,request['attempt_id'],request['required_bytes'])
        return
    fields={'type','contract_sha256','connection_guid','filename','bytes','sha256',
            'directories','file','backup_authenticated'}
    require(isinstance(proof,dict) and set(proof)==fields and proof['type']==kind
            and proof['contract_sha256']==binding.control['contract_sha256']
            and proof['filename']==request['filename'] and type(proof['bytes']) is int
            and proof['bytes']==request['size'] and proof['sha256']==request['sha256']
            and proof['backup_authenticated'] is False
            and isinstance(proof['connection_guid'],str)
            and re.fullmatch('[0-9a-f]{32}',proof['connection_guid']))
    require(isinstance(proof['directories'],list) and len(proof['directories'])==3)
    for item in [*proof['directories'],proof['file']]:
        require(isinstance(item,dict) and set(item)=={'index','created'}
                and all(type(v) is int and 0<v<2**64 for v in item.values()))


@contextmanager
def file_lease(binding,request):
    from .mac_service_restore import Lease
    writable=request['action']=='download'
    files._local(request['file_fd'],writable=writable)
    with ExitStack() as stack:
        parent=None
        path=cfg.BASE/'instance'
        for name in ('', 'staging', request['job']):
            if name: path=path/name
            parent=stack.enter_context(Lease(path,uid=binding.context['uid'],gid=binding.context['gid'],
                mode=0o700,directory=True,mutable=True,parent=parent))
        name='download.tmbackup' if writable else request['filename']
        held=stack.enter_context(Lease(path/name,uid=binding.context['uid'],gid=binding.context['gid'],
            mode=0o600,parent=parent,fd=os.dup(request['file_fd']),mutable=writable))
        size=os.fstat(held.fd).st_size
        require(size==(0 if writable else request['size']))
        yield held
        held.check()


def denied_new_connection():
    # Same fixed deny-all profile also applies to children of the protocol
    # worker. Reject a direct/unrestricted invocation before accessing secrets.
    with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as probe:
        probe.settimeout(1)
        require(probe.connect_ex(('127.0.0.1',9))==errno.EPERM)


def _credential(binding):
    binding.check()
    store=keychain.ServiceStore()
    require(store.value==binding.context)
    raw=store.get(binding.control['credential_account'])
    try:
        require(isinstance(raw,bytes) and 0<len(raw)<=1024 and b'\0' not in raw)
        password=raw.decode('utf-8','strict')
        binding.check()
        return password
    finally: raw=None


def run(request,*,script,executable):
    require(sys.flags.isolated and sys.dont_write_bytecode)
    binding=load_binding()
    require(Path(script).resolve()==binding.tree/cfg.ENTRY
            and Path(__file__).resolve()==binding.tree/cfg.MODULE
            and Path(executable).resolve()==binding.tree/'runtime/bin/python3.12')
    _request(request,binding);denied_new_connection()
    is_probe=request['action']=='probe'
    with nullcontext() if is_probe else file_lease(binding,request) as held:
        channel=transport.Channel(request['socket_fd'],(binding.value['server_address'],445))
        try:
            binding.check()
            with session.open_share(channel,binding.value,binding.control['context_sha256'],
                    binding.context['uid'],lambda:_credential(binding)) as tree:
                if is_probe:
                    proof=probe.check(tree,binding.value,request['attempt_id'],request['required_bytes'])
                else:
                    with files.open_archive(tree,binding.value) as remote:
                        if request['action']=='upload':
                            proof=remote.upload(held.fd,request['filename'],request['size'],
                                                request['sha256'],request['attempt_id'])
                        else: proof=remote.download(held.fd,request['upload'])
            if held is not None: held.check()
            binding.check()
        finally: channel.close()
    _observation(proof,_kind(request),request,binding)
    return dict(type=RESULT,challenge=request['challenge'],control_sha256=binding.control_sha,
        package_sha256=binding.control['package_sha256'],identity=actual_identity(binding.context),
        observation=proof,state_published=False)


def command(binding):
    return ['/usr/bin/sandbox-exec','-p',cfg.PROFILE,str(binding.tree/'runtime/bin/python3.12'),
            '-I','-B',str(binding.tree/cfg.ENTRY)]


def verify_network_policy(binding):
    result=subprocess.run(command(binding)[:3]+['/usr/bin/true'],cwd='/',
        env={'PATH':'/usr/bin:/bin:/usr/sbin:/sbin'},stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,close_fds=True,timeout=10)
    require(result.returncode==0)


def _dispatch(binding,request):
    binding.check()
    with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as connected:
        connected.settimeout(10)
        # No DNS, alternate address, retry, or password in this parent.
        connected.connect((binding.value['server_address'],445))
        require(connected.getpeername()==(binding.value['server_address'],445))
        connected.settimeout(None)
        request=dict(request,socket_fd=connected.fileno())
        _request(request,binding);binding.check()
        is_probe=request['action']=='probe'
        with nullcontext() if is_probe else file_lease(binding,request):
            process=subprocess.Popen(command(binding),stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,cwd='/',close_fds=True,
                pass_fds=((connected.fileno(),) if is_probe else (connected.fileno(),request['file_fd'])),umask=0o077,
                env={'HOME':str(cfg.BASE/'home'),'PATH':'/usr/bin:/bin:/usr/sbin:/sbin',
                     'LANG':'en_US.UTF-8','ERP_HOME_REHEARSAL':'1'})
            first=True;interrupted=False
            deadline=time.monotonic()+PROBE_TIMEOUT if is_probe else None
            while True:
                try:
                    options={'timeout':max(.001,deadline-time.monotonic())} if deadline is not None else {}
                    output,_=process.communicate(cfg.canonical(request) if first else None,**options)
                    break
                except KeyboardInterrupt: first=False;interrupted=True
                except subprocess.TimeoutExpired:
                    # Only this pre-stop canary worker is cancellable. No ERP,
                    # local backup FD or maintenance write is owned by it.
                    require(is_probe)
                    interrupted=True;first=False;deadline=None
                    try: process.kill()
                    except ProcessLookupError: pass
                    # Reap the exact child; never restart from a timeout.
            require(not interrupted and process.returncode==0 and 0<len(output)<=LIMIT)
            result=boot.decode(output)
            expected=dict(type=RESULT,challenge=request['challenge'],control_sha256=binding.control_sha,
                package_sha256=binding.control['package_sha256'],identity=actual_identity(binding.context),
                state_published=False)
            require(isinstance(result,dict) and set(result)==set(expected)|{'observation'}
                    and all(result[k]==v and type(result[k]) is type(v) for k,v in expected.items()))
            _observation(result['observation'],_kind(request),request,binding)
            binding.check()
            return result['observation']


def _kind(request):
    return {'probe':probe.TYPE,'upload':files.UPLOAD,'download':files.READBACK}[request['action']]


def preflight(required_bytes):
    """No ERP stop here; one authenticated canary worker and no local data FD."""
    binding=load_binding()
    require(Path(__file__).resolve()==binding.tree/cfg.MODULE)
    request=dict(challenge=uuid.uuid4().hex,control_sha256=binding.control_sha,
        contract_sha256=binding.control['contract_sha256'],action='probe',socket_fd=2**31-1,
        attempt_id=uuid.uuid4().hex,required_bytes=required_bytes)
    _request(request,binding);verify_network_policy(binding);binding.check()
    return _dispatch(binding,request)


def transfer(source_fd,destination_fd,filename,size,sha256,attempt_id):
    """Callback for build_and_verify_stopped; retains the caller's file FDs."""
    import fcntl
    binding=load_binding()
    require(Path(__file__).resolve()==binding.tree/cfg.MODULE)
    # macOS F_GETPATH names the held file; the worker independently checks its
    # FD against the fixed staging path through held parent directory leases.
    path=Path(os.fsdecode(fcntl.fcntl(source_fd,fcntl.F_GETPATH,bytes(1024)).split(b'\0',1)[0]))
    require(path.parent.parent==cfg.BASE/'instance/staging' and path.name==filename)
    common=dict(control_sha256=binding.control_sha,contract_sha256=binding.control['contract_sha256'],
        job=path.parent.name,filename=filename,size=size,sha256=sha256,attempt_id=attempt_id)
    # Validate both file capabilities before making any connection.
    requests=[dict(common,challenge=uuid.uuid4().hex,action=action,file_fd=fd,socket_fd=2**31-1,upload=None)
              for action,fd in (('upload',source_fd),('download',destination_fd))]
    _request(requests[0],binding)
    require(source_fd!=destination_fd)
    with file_lease(binding,requests[0]),file_lease(binding,requests[1]):
        verify_network_policy(binding);binding.check()
        upload=_dispatch(binding,requests[0])
        requests[1]['upload']=upload
        download=_dispatch(binding,requests[1])
        payload=dict(encrypted=Path(filename),size=size,sha256=sha256)
        proofs=dict(upload=upload,download=download)
        backup._proofs(binding.value,payload,proofs)
        binding.check()
        return proofs


def main(*,script,executable):
    raw=sys.stdin.buffer.read(LIMIT+1)
    require(0<len(raw)<=LIMIT)
    result=run(boot.decode(raw),script=script,executable=executable)
    output=cfg.canonical(result)
    require(len(output)<=LIMIT)
    sys.stdout.buffer.write(output+b'\n');sys.stdout.buffer.flush()
