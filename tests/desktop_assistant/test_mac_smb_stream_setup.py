"""Synthetic first-install plans, TTY, Keychain and NAS; no real credentials."""
from contextlib import contextmanager, nullcontext
from copy import deepcopy
import getpass
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock
import warnings

import pytest
from desktop_assistant import mac_smb_stream_setup as subject
from desktop_assistant import mac_smb_stream_runtime as runtime
from desktop_assistant import mac_smb_stream_probe as probe
from desktop_assistant import mac_smb_stream_session as session, mac_smb_stream_transport as transport
from app.core import mac_service_keychain as keychain
from test_mac_smb_stream_files import contract
from test_mac_smb_stream_probe import Peer, ATTEMPT

pytestmark=pytest.mark.skipif(sys.platform!='darwin',reason='Mac dedicated provisioning and path protocol')


@pytest.fixture
def planned(tmp_path, monkeypatch):
    base = tmp_path.resolve(); cfg = subject.cfg
    monkeypatch.setattr(cfg,'BASE',base)
    monkeypatch.setattr(cfg,'CONTROL',base/'smb-stream-service.json')
    monkeypatch.setattr(cfg,'CONTRACT',base/'smb-stream-archive.json')
    value = contract()
    context = {'uid':499,'gid':499}
    meta = dict(tree=Path(subject.__file__).resolve().parent.parent,context=context,
        context_sha256=value['context_sha256'],publisher_sha256='b'*64,registry_sha256='c'*64)
    monkeypatch.setattr(subject,'metadata',lambda package:deepcopy(meta))
    monkeypatch.setattr(subject.boot,'verify_code',lambda **kw:'d'*64)
    monkeypatch.setattr(subject.mounts,'local_addresses',lambda:set())
    monkeypatch.setattr(subject.time,'time',lambda:1000)
    plan = subject.make_plan('e'*64,value); plan['attempt_id']=ATTEMPT
    raw = subject.boot.canonical(plan); digest=cfg.digest(raw)
    peer = Peer(); proof=probe.check(peer.tree(),value,ATTEMPT,subject.SIZE)
    return SimpleNamespace(base=base,plan=plan,raw=raw,digest=digest,meta=meta,proof=proof,peer=peer)


def test_plan_control_and_stdlib_proof_bind_exact_original_approval(planned):
    a=planned
    assert subject.header(a.raw,a.digest)==a.plan
    ctl=subject.control(a.plan,a.digest)
    assert ctl['credential_account'].startswith('nas-smb-stream-v1:')
    assert ctl['plan_sha256']==a.digest and ctl['contract_sha256']==subject.archive.digest(a.plan['contract'])
    assert subject.validate(a.raw,a.digest)==(a.plan,a.meta)
    assert subject.probe_proof(a.proof,a.plan) is None


@pytest.mark.parametrize('change',['extra','expires','future','bool-time','overlong','contract','hash','noncanonical'])
def test_plan_rejects_modified_expired_or_ambiguous_values(planned,change):
    a=planned; value=deepcopy(a.plan)
    if change=='extra':value['password']='never permitted'
    if change=='expires':value['expires_at']=1000
    if change=='future':value['created_at']=1001
    if change=='bool-time':value['created_at']=True
    if change=='overlong':value['expires_at']=value['created_at']+7*86400+1
    if change=='contract':value['contract']['port']=139
    raw=subject.boot.canonical(value)
    if change=='noncanonical':raw=json.dumps(value,indent=2).encode()
    with pytest.raises(ValueError):subject.header(raw,'0'*64 if change=='hash' else subject.cfg.digest(raw))


@pytest.mark.parametrize('field,value',[('context_sha256','9'*64),('publisher_sha256','9'*64),('registry_sha256','9'*64)])
def test_plan_does_not_adopt_changed_root_approval(planned,field,value):
    meta=deepcopy(planned.meta);meta[field]=value
    with pytest.raises(ValueError):subject.binding(planned.plan,meta)


def test_local_address_never_accepted_as_nas(planned,monkeypatch):
    monkeypatch.setattr(subject.mounts,'local_addresses',lambda:{planned.plan['contract']['server_address']})
    with pytest.raises(ValueError):subject.validate(planned.raw,planned.digest)


@pytest.mark.parametrize('name',['smb-stream-service.json','smb-stream-archive.json','smb-stream-install-old.pending.json'])
def test_existing_or_dangling_install_scene_is_not_adopted(planned,name):
    (planned.base/name).symlink_to(planned.base/'missing')
    with pytest.raises(ValueError):subject.vacant()
    assert (planned.base/name).is_symlink()


@pytest.fixture
def installer(planned,monkeypatch):
    a=planned;a.events=[]
    monkeypatch.setattr(subject.boot,'modules',lambda:(SimpleNamespace(_administrator=lambda:None),None))
    monkeypatch.setattr(subject.publication,'lock',lambda:nullcontext())
    monkeypatch.setattr(subject.publication,'read_record',lambda p:p.read_bytes())
    monkeypatch.setattr(subject.publication,'sync_directory',lambda p:None)
    monkeypatch.setattr(subject,'foreground',lambda:None)
    monkeypatch.setattr(subject,'verify_policy',lambda:None)
    def write(path,raw,mode=0o600):
        a.events.append(path.name)
        with path.open('xb') as f:f.write(raw)
        path.chmod(mode)
    monkeypatch.setattr(subject.boot,'_write',write)
    def worker(meta,raw,phase):
        a.events.append(phase)
        assert not subject.cfg.CONTROL.exists()
        assert subject.publication.read_record(subject.pending(a.plan['attempt_id']))==a.raw
        result=subject.expected(a.plan,phase,'f'*32)
        result['identity']=subject.boot._expected(499,499)
        if phase!='identity':result['credential_readback_verified']=True
        if phase=='provision':result.update(proof=a.proof,credential_created=True)
        return subject.reply(result,a.plan,meta,phase,'f'*32)
    monkeypatch.setattr(subject,'worker',worker)
    return a


def test_install_publishes_control_last_after_new_process_readback(installer):
    a=installer; result=subject.apply(a.raw,a.digest)
    assert a.events==['smb-stream-install-'+ATTEMPT+'.pending.json','identity','provision','verify',
        'smb-stream-archive.json','smb-stream-install-'+ATTEMPT+'.verified.json','smb-stream-service.json']
    assert result['new_process_credential_verified'] is True
    assert result['preferences_changed'] is result['erp_started'] is result['backup_executed'] is False
    assert subject.boot.decode(subject.cfg.CONTROL.read_bytes())==subject.control(a.plan,a.digest)
    assert subject.pending(ATTEMPT).read_bytes()==a.raw
    with pytest.raises(ValueError):subject.apply(a.raw,a.digest)


@pytest.mark.parametrize('phase',['identity','provision','verify'])
def test_failed_worker_keeps_pending_without_publishing_any_control(installer,monkeypatch,phase):
    a=installer; original=subject.worker
    def fail(meta,raw,selected):
        if selected==phase:raise ValueError('synthetic failure')
        return original(meta,raw,selected)
    monkeypatch.setattr(subject,'worker',fail)
    with pytest.raises(ValueError):subject.apply(a.raw,a.digest)
    assert subject.pending(ATTEMPT).read_bytes()==a.raw
    assert not subject.cfg.CONTRACT.exists() and not subject.cfg.CONTROL.exists()


@pytest.mark.parametrize('name',['smb-stream-archive.json','smb-stream-install-'+ATTEMPT+'.verified.json',
                                 'smb-stream-service.json'])
def test_publication_failure_preserves_new_evidence_without_retry(installer,monkeypatch,name):
    original=subject.boot._write; calls=[]
    def fail(path,raw,mode=0o600):
        calls.append(path.name)
        if path.name==name:raise OSError('synthetic disk failure')
        original(path,raw,mode)
    monkeypatch.setattr(subject.boot,'_write',fail)
    with pytest.raises(OSError):subject.apply(installer.raw,installer.digest)
    assert calls.count(name)==1 and subject.pending(ATTEMPT).exists()
    assert not subject.cfg.CONTROL.exists()


def test_approval_change_after_secret_write_cannot_publish_control(installer,monkeypatch):
    a=installer; original=subject.worker
    def changed(meta,raw,phase):
        result=original(meta,raw,phase)
        if phase=='provision':a.meta['registry_sha256']='9'*64
        return result
    monkeypatch.setattr(subject,'worker',changed)
    with pytest.raises(ValueError):subject.apply(a.raw,a.digest)
    assert not subject.cfg.CONTROL.exists() and not subject.cfg.CONTRACT.exists()
    assert subject.pending(ATTEMPT).exists()


@pytest.fixture
def dedicated(planned,monkeypatch):
    a=planned;a.events=[];a.values={};a.puts=[]
    args=SimpleNamespace(phase='provision',attempt=ATTEMPT,plan_sha256=a.digest,challenge='f'*32,socket_fd=77)
    a.args=args
    monkeypatch.setattr(subject,'authenticated',lambda *args,**kw:(a.plan,deepcopy(a.meta),a.raw))
    monkeypatch.setattr(subject.boot,'_actual',lambda:subject.boot._expected(499,499))
    monkeypatch.setattr(subject,'foreground',lambda:a.events.append('foreground'))
    @contextmanager
    def unlocked(*args):yield 'synthetic keychain'
    monkeypatch.setattr(keychain,'unlocked',unlocked)
    def absent(ref,service,account):
        a.events.append('absent')
        if account in a.values:raise ValueError('exists')
    def put(account,raw):
        assert account not in a.values
        a.events.append('put');a.values[account]=raw;a.puts.append(account)
    def get(account):a.events.append('get');return a.values[account]
    a.store=SimpleNamespace(value=a.meta['context'],api=SimpleNamespace(require_absent=absent),put=put,get=get)
    monkeypatch.setattr(keychain,'ServiceStore',lambda:a.store)
    class Channel:
        def __init__(self,fd,endpoint):
            assert fd==77 and endpoint==(a.plan['contract']['server_address'],445)
            a.events.append('channel')
        def close(self):a.events.append('close')
    monkeypatch.setattr(transport,'Channel',Channel)
    @contextmanager
    def share(channel,value,context_sha,uid,credential_provider,timeout):
        assert (value,context_sha,uid)==(a.plan['contract'],a.plan['context_sha256'],499)
        assert timeout==600
        a.events.append('negotiated')
        assert credential_provider()=='synthetic-nas-password'
        a.events.append('login')
        yield a.peer.tree()
        a.events.append('session-checked')
    monkeypatch.setattr(session,'open_share',share)
    def prompt(label):
        assert 'NAS' in label;a.events.append('prompt');return 'synthetic-nas-password'
    a.prompt=prompt
    return a


def run(a):return subject.run(a.args,script='synthetic',executable='synthetic',prompt=a.prompt)


def test_secret_stored_only_after_complete_canary_and_second_process_reads_it(dedicated):
    a=dedicated;result=run(a)
    assert a.events.index('absent')<a.events.index('prompt')
    assert a.events.index('negotiated')<a.events.index('prompt')<a.events.index('login')
    assert a.events.index('session-checked')<a.events.index('put')<a.events.index('get')
    subject.probe_proof(result['proof'],a.plan)
    assert len(a.puts)==1 and b'synthetic-nas-password' not in subject.boot.canonical(result)
    a.events.clear();a.args.phase='verify';a.args.socket_fd=None
    assert run(a)['credential_readback_verified'] is True and a.events==['get']


def test_identity_check_does_not_touch_keychain_or_input(dedicated,monkeypatch):
    a=dedicated;a.args.phase='identity';a.args.socket_fd=None
    monkeypatch.setattr(keychain,'ServiceStore',Mock(side_effect=AssertionError('no keychain')))
    assert run(a)['phase']=='identity' and not a.events


@pytest.mark.parametrize('kind',['old-entry','prompt-fallback','empty','nul','too-long','nas-space','login-failed','binding-change'])
def test_failure_never_stores_password_or_deletes_prior_entry(dedicated,monkeypatch,kind):
    a=dedicated; account=subject.control(a.plan,a.digest)['credential_account']
    if kind=='old-entry':a.values[account]=b'prior retained credential'
    if kind=='prompt-fallback':
        def prompt(label):warnings.warn('no tty',getpass.GetPassWarning);return 'synthetic-nas-password'
        a.prompt=prompt
    if kind in ('empty','nul','too-long'):a.prompt=lambda label:{'empty':'','nul':'a\0b','too-long':'x'*1025}[kind]
    if kind=='nas-space':a.peer.capacity['caller_available_units']=0
    if kind=='login-failed':
        @contextmanager
        def fail(*args,**kwargs):
            kwargs['credential_provider']();raise ValueError('synthetic login rejected');yield
        monkeypatch.setattr(session,'open_share',fail)
    if kind=='binding-change':
        def prompt(label):a.raw=b'changed';return 'synthetic-nas-password'
        a.prompt=prompt
    with pytest.raises((ValueError,getpass.GetPassWarning)):run(a)
    assert a.puts==[]
    assert a.values==({account:b'prior retained credential'} if kind=='old-entry' else {})


@pytest.mark.parametrize('field,value',[('backup_authenticated',True),('canary_removed',False),
    ('required_bytes',False),('available_bytes',16383),('contract_sha256','9'*64),('attempt_id','9'*32)])
def test_root_does_not_accept_incomplete_worker_proof(planned,field,value):
    proof=deepcopy(planned.proof);proof[field]=value
    with pytest.raises(ValueError):subject.probe_proof(proof,planned.plan)


def test_bad_new_entry_readback_retains_created_keychain_item(dedicated):
    a=dedicated;a.store.get=lambda account:b'wrong'
    with pytest.raises(ValueError):run(a)
    assert len(a.puts)==1 and list(a.values.values())==[b'synthetic-nas-password']


@pytest.fixture
def dispatched(planned,monkeypatch):
    a=planned;a.commands=[];a.connections=[];a.communications=[];a.interrupt=False;a.bad_reply=False
    monkeypatch.setattr(subject,'verify_policy',lambda:None)
    monkeypatch.setattr(subject,'foreground',lambda:None)
    class Socket:
        def __init__(self,family,kind):
            assert (family,kind)==(subject.socket.AF_INET,subject.socket.SOCK_STREAM)
            self.closed=False;self.address=None;a.connections.append(self)
        def settimeout(self,n):assert n==10
        def connect(self,address):
            assert address==(a.plan['contract']['server_address'],445);self.address=address
        def getpeername(self):return self.address
        def fileno(self):return 77
        def close(self):self.closed=True
    monkeypatch.setattr(subject.socket,'socket',Socket)
    class Process:
        returncode=0
        def __init__(self,command,**kwargs):
            a.commands.append((command,kwargs));self.count=0
            phase=command[command.index('--phase')+1];challenge=command[command.index('--challenge')+1]
            self.value=subject.expected(a.plan,phase,challenge)
            self.value['identity']=subject.boot._expected(499,499)
            if phase!='identity':self.value['credential_readback_verified']=True
            if phase=='provision':self.value.update(proof=a.proof,credential_created=True)
        def communicate(self,*args,**kwargs):
            assert not args and not kwargs  # Root never sends a password/input payload.
            self.count+=1;a.communications.append(self.count)
            if a.interrupt and self.count==1:raise KeyboardInterrupt
            if a.bad_reply:self.value['challenge']='0'*32
            return subject.boot.canonical(self.value),b'synthetic private stderr never forwarded'
    monkeypatch.setattr(subject.subprocess,'Popen',Process)
    return a


@pytest.mark.parametrize('phase',['identity','provision','verify'])
def test_parent_only_passes_exact_socket_and_no_credential_channels(dispatched,phase):
    a=dispatched;result=subject.worker(a.meta,a.raw,phase)
    assert result['phase']==phase and len(a.commands)==1
    command,kw=a.commands[0]
    assert command[:3]==['/usr/bin/sandbox-exec','-p',subject.cfg.PROFILE]
    assert command[3:7]==[str(a.meta['tree']/'runtime/bin/python3.12'),'-I','-B',str(a.meta['tree']/subject.ENTRY)]
    assert kw['user']==kw['group']==499 and kw['extra_groups']==[] and kw['umask']==0o077
    assert kw['pass_fds']==((77,) if phase=='provision' else ()) and kw['close_fds'] is True
    assert kw['stdin'] is (None if phase=='provision' else subject.subprocess.DEVNULL)
    assert kw['cwd']=='/' and set(kw['env'])=={'HOME','PATH','LANG','ERP_HOME_REHEARSAL'}
    assert len(a.connections)==(1 if phase=='provision' else 0)
    assert all(c.closed for c in a.connections) and a.communications==[1]


@pytest.mark.parametrize('fault',['interrupt','reply','network-policy','foreground'])
def test_parent_failure_never_spawns_replacement_or_reconnects(dispatched,monkeypatch,fault):
    a=dispatched
    if fault=='interrupt':a.interrupt=True
    if fault=='reply':a.bad_reply=True
    def fail():raise ValueError('synthetic check failure')
    if fault=='network-policy':monkeypatch.setattr(subject,'verify_policy',fail)
    if fault=='foreground':monkeypatch.setattr(subject,'foreground',fail)
    with pytest.raises(ValueError):subject.worker(a.meta,a.raw,'provision')
    assert len(a.commands)<=1 and len(a.connections)<=1
    assert all(c.closed for c in a.connections)
    if fault=='interrupt':assert a.communications==[1,2]
    if fault in ('network-policy','foreground'):assert not a.commands and not a.connections


@pytest.fixture
def authenticated(planned,monkeypatch):
    a=planned
    flags={k:getattr(sys.flags,k) for k in dir(sys.flags) if not k.startswith('_')}
    flags['isolated']=1
    monkeypatch.setattr(subject.sys,'flags',SimpleNamespace(**flags))
    monkeypatch.setattr(subject.sys,'dont_write_bytecode',True)
    monkeypatch.setattr(keychain,'selected',lambda:True)
    monkeypatch.setattr(keychain,'load_context',lambda:deepcopy(a.meta['context']))
    a.ids=subject.boot._expected(499,499)
    monkeypatch.setattr(subject.boot,'_actual',lambda:deepcopy(a.ids))
    monkeypatch.setattr(subject.publication,'read_record',lambda p:a.raw if p==subject.pending(ATTEMPT) else b'wrong')
    a.denial=Mock();monkeypatch.setattr(runtime,'denied_new_connection',a.denial)
    a.args=SimpleNamespace(phase='identity',attempt=ATTEMPT,plan_sha256=a.digest,challenge='f'*32,socket_fd=None)
    a.script=a.meta['tree']/subject.ENTRY;a.executable=a.meta['tree']/'runtime/bin/python3.12'
    return a


def test_worker_authenticates_pending_identity_program_and_network_boundary(authenticated):
    a=authenticated
    assert subject.authenticated(a.args,script=a.script,executable=a.executable)==(a.plan,a.meta,a.raw)
    a.denial.assert_called_once_with()


@pytest.mark.parametrize('change',['uid','groups','script','interpreter','pending','challenge','unexpected-fd','missing-fd'])
def test_worker_refuses_other_identity_or_script_before_any_secret(authenticated,change):
    a=authenticated
    if change=='uid':a.ids['euid']=501
    if change=='groups':a.ids['kernel_groups'].append(0)
    if change=='script':a.script=a.meta['tree']/'other.py'
    if change=='interpreter':a.executable=Path('/usr/bin/python3')
    if change=='pending':a.raw+=b'\n'
    if change=='challenge':a.args.challenge='bad'
    if change=='unexpected-fd':a.args.socket_fd=77
    if change=='missing-fd':a.args.phase='provision'
    with pytest.raises(ValueError):subject.authenticated(a.args,script=a.script,executable=a.executable)
    assert not a.denial.called


def test_bootstrap_pack_and_runtime_both_require_provisioning_code():
    assert {subject.MODULE,'desktop_assistant/mac_smb_stream_contract.py',
            'desktop_assistant/mac_smb_stream_runtime_contract.py'} <= set(subject.boot.FILES)
    assert {subject.MODULE,subject.ENTRY} <= subject.cfg.FILES


def test_real_parent_foreground_gate_rejects_nonterminal(monkeypatch):
    monkeypatch.setattr(subject.os,'isatty',lambda fd:False)
    with pytest.raises(ValueError):subject.foreground()


def test_foreground_gate_rejects_old_background_heredoc_launch(monkeypatch):
    monkeypatch.setattr(subject.os,'isatty',lambda fd:True)
    monkeypatch.setattr(subject.os,'tcgetpgrp',lambda fd:5)
    monkeypatch.setattr(subject.os,'getpgrp',lambda:6)
    with pytest.raises(ValueError):subject.foreground()


def test_real_sandboxed_tty_with_captured_output_uses_hidden_input_and_restores_echo():
    # Ordinary test UID, not the installed service. A private pseudo terminal
    # supplies only a synthetic test string; the user's actual terminal is unused.
    import errno
    import pty
    import select
    import signal
    import subprocess
    import time
    child = """
import errno,getpass,json,os,socket,termios,warnings
before=termios.tcgetattr(0)
assert os.isatty(0) and os.tcgetpgrp(0)==os.getpgrp()
with warnings.catch_warnings():
    warnings.simplefilter('error',getpass.GetPassWarning)
    value=getpass.getpass('NAS synthetic hidden input: ')
with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as sock:
    denied=sock.connect_ex(('127.0.0.1',9))==errno.EPERM
print(json.dumps(dict(hidden_input_ok=value=='synthetic-tty-only',network_denied=denied,
    tty_restored=termios.tcgetattr(0)==before,uid=os.getuid())))
"""
    pid,fd=pty.fork()
    if pid==0:
        try:
            process=subprocess.Popen(['/usr/bin/sandbox-exec','-p',subject.cfg.PROFILE,
                sys.executable,'-I','-B','-c',child],stdin=None,stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,close_fds=True,pass_fds=(),umask=0o077,cwd='/',
                env={'HOME':os.environ['HOME'],'PATH':'/usr/bin:/bin','LANG':'en_US.UTF-8'})
            out,err=process.communicate(timeout=15)
            assert process.returncode==0 and not err
            os.write(1,b'RESULT:'+out);os._exit(0)
        except BaseException:os._exit(1)
    output=bytearray();sent=False;status=None;deadline=time.monotonic()+20
    try:
        while time.monotonic()<deadline:
            if select.select([fd],[],[],.1)[0]:
                try:raw=os.read(fd,65536)
                except OSError as error:
                    if error.errno==errno.EIO:break
                    raise
                if not raw:break
                output.extend(raw)
                if not sent and b'NAS synthetic hidden input: ' in output:
                    os.write(fd,b'synthetic-tty-only\n');sent=True
            ended,state=os.waitpid(pid,os.WNOHANG)
            if ended:status=state;break
        if status is None:
            ended,state=os.waitpid(pid,os.WNOHANG)
            if ended:status=state
            else:
                os.killpg(pid,signal.SIGTERM);_,status=os.waitpid(pid,0)
        # The child may exit immediately after the input write. Drain any final
        # PTY output before evaluating it, even if waitpid won that race.
        while select.select([fd],[],[],0)[0]:
            try:raw=os.read(fd,65536)
            except OSError as error:
                if error.errno==errno.EIO:break
                raise
            if not raw:break
            output.extend(raw)
        assert os.waitstatus_to_exitcode(status)==0 and sent
        assert b'synthetic-tty-only' not in output  # Prompt never echoed the fake password.
        result=json.loads(bytes(output).split(b'RESULT:',1)[1])
        assert result==dict(hidden_input_ok=True,network_denied=True,tty_restored=True,uid=os.getuid())
    finally:os.close(fd)
