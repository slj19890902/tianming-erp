"""Synthetic actual Unix/TCP FD passing; explicit root/endpoint adapters.

No NAS credential, SMB authentication, installed ERP, real UID transition or
production connection. Native sandbox/peer checks are labeled separately.
"""
import array
from copy import deepcopy
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock,MagicMock

import pytest

from desktop_assistant import current_mac_smb_broker as b,current_mac_worker_contract as c
from desktop_assistant.home_process import PROFILE
from tests.desktop_assistant.test_current_mac_worker_contract import chain


@pytest.fixture
def bound(chain,monkeypatch):
    plan=deepcopy(chain[0]['update'])
    contract=dict(type=b.archive.TYPE,context_sha256=plan['context_sha256'],uid=499,
        server_address='172.16.3.96',port=445,username='synthetic',share='synthetic',
        relative_directory=b.archive.DIRECTORY,server_guid='a'*32,protection='smb311-session-aes-gcm')
    plan['transport_contract_sha256']=b.archive.digest(contract)
    plan_sha=c.raw_digest(c.canonical(plan));challenge='c'*32
    monkeypatch.setattr(b,'_administrator',lambda:None)
    return SimpleNamespace(plan=plan,contract=contract,plan_sha=plan_sha,challenge=challenge)


def server(bound,channel,recheck=None):
    return b.Server(channel,bound.plan,bound.plan_sha,bound.challenge,bound.contract,
        recheck or Mock())


def request(s,action='probe'):
    return dict(type=b.REQUEST,action=action,**s.expected)


@pytest.mark.parametrize('field',sorted(b.FIELDS))
def test_wrong_message_poisoned_before_connection(bound,field):
    left,right=socket.socketpair()
    try:
        s=server(bound,left);value=request(s);value[field]='wrong'
        with pytest.raises(ValueError):s.feed(b._frame(value))
        assert s.sequence==0 and s.failed
        with pytest.raises(ValueError):s.feed(b._frame(request(s)))
    finally:left.close();right.close()


@pytest.mark.parametrize('raw',[b'',struct.pack('!I',0),struct.pack('!I',b.LIMIT+1),b'x'*(b.LIMIT+5)])
def test_bad_frame_refused_without_connection(bound,raw):
    left,right=socket.socketpair()
    try:
        s=server(bound,left)
        with pytest.raises(ValueError):s.feed(raw)
        assert s.failed and s.sequence==0
    finally:left.close();right.close()


def test_partial_header_then_bad_frame_and_unfinished_plan_refused(bound):
    left,right=socket.socketpair()
    try:
        s=server(bound,left);s.feed(b'\0\0')
        with pytest.raises(ValueError):s.finish()
        with pytest.raises(ValueError):s.feed(b'\0\0')
        assert s.sequence==0
    finally:left.close();right.close()


def test_coalesced_requests_never_open_multiple_channels(bound):
    left,right=socket.socketpair()
    try:
        s=server(bound,left);raw=b._frame(request(s))
        with pytest.raises(ValueError):s.feed(raw+raw)
        assert s.failed and s.sequence==0
    finally:left.close();right.close()


@pytest.mark.skipif(sys.platform!='darwin',reason='native getpeereid on macOS')
def test_real_peer_identity_is_not_falsely_root(bound):
    left,right=socket.socketpair()
    try:
        assert b._peer(left)==(os.getuid(),os.getgid())
        if os.getuid()!=0:
            with pytest.raises(ValueError):b.Client(left,bound.plan,bound.plan_sha,bound.challenge,bound.contract)
    finally:left.close();right.close()


@pytest.mark.parametrize('fault',['two_fds','file_fd','wrong_peer','wrong_reply','wrong_token'])
def test_received_bad_fd_or_reply_closed_without_fallback(bound,monkeypatch,tmp_path,fault):
    left,right=socket.socketpair()
    listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(1)
    connected=socket.create_connection(listener.getsockname());accepted,_=listener.accept()
    monkeypatch.setattr(b,'_peer',lambda channel:(0,0))
    file=tmp_path/'synthetic';file.write_bytes(b'no secret');held=file.open('rb')
    try:
        client=b.Client(right,bound.plan,bound.plan_sha,bound.challenge,bound.contract)
        response={**dict(type=b.REQUEST,action='probe',**client.expected),'type':b.REPLY}
        if fault=='wrong_reply':response['challenge']='0'*32
        left.sendall(b._frame(response))
        rights=array.array('i',[held.fileno()] if fault=='file_fd' else [connected.fileno()])
        if fault=='two_fds':rights.append(connected.fileno())
        left.sendmsg([b'X' if fault=='wrong_token' else b'F'],
            [(socket.SOL_SOCKET,socket.SCM_RIGHTS,rights)])
        before=len(os.listdir('/dev/fd'))
        with pytest.raises((ValueError,OSError)):client.connection('probe')
        assert client.failed and client.channel.fileno()==-1 and len(os.listdir('/dev/fd'))<=before
        with pytest.raises(ValueError):client.connection('probe')
    finally:
        held.close();left.close();right.close();connected.close();accepted.close();listener.close()


@pytest.mark.parametrize('field,value',[('server_address','example.com'),('server_address','8.8.8.8'),
    ('server_address','172.16.3.96:445'),('port',8000),('protection','none')])
def test_unapproved_endpoint_or_protection_rejected_before_any_request(bound,field,value):
    left,right=socket.socketpair()
    try:
        bound.contract[field]=value
        bound.plan['transport_contract_sha256']=b.archive.digest(bound.contract)
        bound.plan_sha=c.raw_digest(c.canonical(bound.plan))
        with pytest.raises(ValueError):server(bound,left)
    finally:left.close();right.close()


def test_root_identity_cannot_be_self_asserted_from_personal_user(chain):
    if os.getuid()==0:pytest.skip('This negative case requires a personal non-root test process')
    with pytest.raises(ValueError):b._administrator()


@pytest.mark.parametrize('fault',['connect','recheck_after_connect'])
def test_parent_connection_failure_closes_control_and_never_retries(bound,monkeypatch,fault):
    left,right=socket.socketpair();check=Mock()
    try:
        s=server(bound,left,check)
        connected=MagicMock();connected.__enter__.return_value=connected
        connected.family=socket.AF_INET
        connected.getsockopt.return_value=socket.SOCK_STREAM
        connected.getpeername.return_value=s.endpoint
        if fault=='connect':connected.connect.side_effect=OSError('synthetic connect failure')
        else:check.side_effect=[None,ValueError('synthetic binding changed')]
        create=Mock(return_value=connected);monkeypatch.setattr(b.socket,'socket',create)
        with pytest.raises((OSError,ValueError)):s.feed(b._frame(request(s)))
        assert s.failed and s.channel.fileno()==-1 and create.call_count==1
        assert connected.__exit__.called and right.recv(1)==b''
        with pytest.raises(ValueError):s.feed(b._frame(request(s)))
        assert create.call_count==1
    finally:left.close();right.close()


@pytest.mark.skipif(sys.platform!='darwin',reason='real Apple deny-all sandbox and Unix FD channel')
def test_three_actual_channels_reach_sandboxed_child_without_new_outbound_connections(bound,monkeypatch):
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0));listener.listen(3);listener.settimeout(5)
        # Endpoint adapter only: real loopback/random port, never a NAS.
        bound.contract.update(server_address='127.0.0.1',port=listener.getsockname()[1])
        bound.plan['transport_contract_sha256']=b.archive.digest(bound.contract)
        bound.plan_sha=c.raw_digest(c.canonical(bound.plan))
        monkeypatch.setattr(b.archive,'validate',lambda value,context,uid:value)
        parent,child=socket.socketpair();parent.settimeout(5)
        s=server(bound,parent)
        program='''import errno,json,os,socket,sys
sys.path.insert(0,sys.argv[1])
from desktop_assistant import current_mac_smb_broker as b
from desktop_assistant import current_mac_worker_contract as c
plan=json.loads(sys.argv[2]);contract=json.loads(sys.argv[3])
channel=socket.socket(fileno=int(sys.argv[4]))
assert b._peer(channel)==(os.getuid(),os.getgid())
# Only test adapters: no real root identity and a local ephemeral TCP port.
b._peer=lambda channel:(0,0)
b.archive.validate=lambda value,context,uid:value
client=b.Client(channel,plan,c.raw_digest(c.canonical(plan)),'c'*32,contract)
for action in b.ORDER:
    with socket.socket() as denied:
        assert denied.connect_ex(('127.0.0.1',9))==errno.EPERM
    with client.connection(action) as connected:
        assert os.get_inheritable(connected.fileno()) is False
        assert connected.recv(32)==b'synthetic greeting'
        connected.sendall(action.encode())
channel.close()
print('three-inherited-channels-with-deny-all-ok')
'''
        repo=str(Path(b.__file__).resolve().parent.parent)
        process=subprocess.Popen(['/usr/bin/sandbox-exec','-p',PROFILE,sys.executable,'-I','-B','-c',
            program,repo,c.canonical(bound.plan).decode(),b.archive.canonical(bound.contract).decode(),str(child.fileno())],
            pass_fds=(child.fileno(),),stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            cwd='/',env={'PATH':'/usr/bin:/bin:/usr/sbin:/sbin','LANG':'en_US.UTF-8'})
        child.close()
        try:
            for action in b.ORDER:
                prefix=b._receive_exact(parent,4);size=struct.unpack('!I',prefix)[0]
                body=b._receive_exact(parent,size)
                s.feed(prefix[:1]);s.feed(prefix[1:]+body)
                incoming,_=listener.accept()
                with incoming:
                    incoming.sendall(b'synthetic greeting');incoming.settimeout(5)
                    assert incoming.recv(32)==action.encode()
            output,error=process.communicate(timeout=10)
            assert process.returncode==0,error
            assert output.strip()==b'three-inherited-channels-with-deny-all-ok'
            s.finish()
            with pytest.raises(ValueError):s.feed(b._frame(request(s)))
        finally:
            parent.close()
            if process.poll() is None:
                # Only this owned synthetic test process, never ERP/backup.
                process.kill();process.wait()
