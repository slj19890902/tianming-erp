from contextlib import contextmanager
from copy import deepcopy
import os
import socket
import struct
import threading
import time
import sys
import types
import pytest

from desktop_assistant import mac_smb_stream_contract as cfg
from desktop_assistant import mac_smb_stream_transport as wire


def contract():
    return dict(type=cfg.TYPE,context_sha256='a'*64,uid=499,server_address='192.168.1.245',
        port=445,username='erp_admin',share='天明ERP备份',relative_directory=cfg.DIRECTORY,
        server_guid='b'*32,protection='smb311-session-aes-gcm')


def test_stream_contract_preserves_chinese_names_and_is_not_a_mount_contract():
    value=contract()
    assert cfg.validate(value,'a'*64,499)==value
    assert cfg.remote_path(value,'20261012-003000-1234abcd.tmbackup')=='BoxERP\\mac-server-backups\\20261012-003000-1234abcd.tmbackup'
    assert 'mountpoint' not in value and 'password' not in value
    assert cfg.digest(value)==cfg.digest(deepcopy(value))


@pytest.mark.parametrize('key,bad', [('type','tianming.mac-smb-archive.v1'),('context_sha256','c'*64),
    ('uid',501),('uid',True),('server_address','127.0.0.1'),('server_address','NAS.local'),
    ('server_address','8.8.8.8'),('server_address','192.168.001.245'),('server_address','::1'),
    ('port',True),('port',1445),('username','Guest'),('username','domain\\admin'),
    ('username','admin@domain'),('username',' admin'),('share','..'),('share','folder/share'),
    ('share','share:'),('share','IPC$'),('share','e\u0301'),('share','bad\0name'),('share','x\ud800'),
    ('share','a'*241),('relative_directory','other'),('server_guid','0'*32),
    ('server_guid','uppercase-not-a-guid'),('protection','unencrypted'),('password','SYNTHETIC')])
def test_contract_rejects_unapproved_paths_identities_and_secret_fields(key,bad):
    value=contract();value[key]=bad
    with pytest.raises(ValueError):cfg.validate(value,'a'*64,499)


@pytest.mark.parametrize('name',['../x.tmbackup','20261012-003000-1234abcd.tmbackup/other',
    '20261012-003000-1234abcd.tmbackup:stream','old.tmbackup','20261012-003000-1234abcd.tmbackup.pending'])
def test_backup_basename_cannot_select_another_remote_object(name):
    with pytest.raises(ValueError):cfg.remote_path(contract(),name)


@contextmanager
def connected():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0));listener.listen(1)
        client=socket.create_connection(listener.getsockname(),timeout=2)
        peer,_=listener.accept()
    expected=client.getpeername()
    channel=wire.Channel(client.detach(),expected)
    try:yield channel,peer,expected
    finally:channel.close();peer.close()


def transport(channel,expected,timeout=1):
    result=wire.Transport(channel,*expected,timeout);result.connect();return result


def test_claim_pins_peer_and_consumes_only_one_connection():
    with connected() as (channel,peer,expected):
        assert not channel._sock.get_inheritable()
        with pytest.raises(ValueError):channel.claim('192.168.1.245',445)
        with pytest.raises(ValueError):channel.claim(*expected)


def test_no_reconnect_after_close_or_second_transport():
    with connected() as (channel,peer,expected):
        tcp=transport(channel,expected)
        with pytest.raises(ValueError):tcp.connect()
        with pytest.raises(ValueError):transport(channel,expected)
        tcp.close();tcp.close()
        with pytest.raises(ValueError):tcp.connect()
        assert tcp.recv(1)==b''


@pytest.mark.parametrize('timeout',[None,False,0,-1,float('inf'),float('nan'),601,'60',10**1000])
def test_timeout_must_be_finite_and_bounded(timeout):
    with pytest.raises(ValueError):wire.duration(timeout)


@pytest.mark.parametrize('fd',[True,-1,0,1,2,2**80])
def test_standard_stream_or_invalid_fd_never_taken_over(fd):
    with pytest.raises(ValueError):wire.Channel(fd,('127.0.0.1',1234))


def test_rejects_non_socket_and_wrong_peer(tmp_path):
    path=tmp_path/'ordinary';path.write_bytes(b'0')
    fd=os.open(path,os.O_RDONLY)
    try:
        with pytest.raises(OSError):wire.Channel(fd,('127.0.0.1',1234))
    finally:os.close(fd)
    a,b=socket.socketpair()
    try:
        with pytest.raises(ValueError):wire.Channel(a.detach(),('127.0.0.1',1234))
    finally:b.close()
    with connected() as (channel,peer,expected):
        fd=os.dup(channel._sock.fileno())
        with pytest.raises(ValueError):wire.Channel(fd,('127.0.0.1',expected[1]+1))
        with pytest.raises(OSError):os.fstat(fd)


def test_listening_socket_is_not_an_already_connected_channel():
    listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(1)
    address=listener.getsockname()
    with pytest.raises(OSError):wire.Channel(listener.detach(),address)


def test_frame_send_and_fragmented_receive():
    with connected() as (channel,peer,expected):
        tcp=transport(channel,expected);peer.settimeout(2)
        tcp.send(b'original')
        assert peer.recv(12)==b'\0\0\0\x08original'
        def fragments():
            for part in [b'\0',b'\0\0',b'\x05',b'hel',b'lo']:
                peer.sendall(part);time.sleep(.002)
        thread=threading.Thread(target=fragments);thread.start()
        try:assert tcp.recv(1)==b'hello'
        finally:thread.join(2);tcp.close()


@pytest.mark.parametrize('frame',[b'\0',b'\0\0\0\x04ab'])
def test_truncated_header_or_body_is_not_clean_eof(frame):
    with connected() as (channel,peer,expected):
        tcp=transport(channel,expected);peer.sendall(frame);peer.shutdown(socket.SHUT_WR)
        with pytest.raises(ValueError):tcp.recv(1)
        tcp.close()


@pytest.mark.parametrize('length',[0,wire.MAX_FRAME+1,0xffffffff])
def test_invalid_length_rejected_before_body_allocation(length):
    with connected() as (channel,peer,expected):
        tcp=transport(channel,expected);peer.sendall(struct.pack('>I',length))
        with pytest.raises(ValueError):tcp.recv(1)
        tcp.close()


def test_clean_eof_closes_without_reconnect():
    with connected() as (channel,peer,expected):
        tcp=transport(channel,expected);peer.shutdown(socket.SHUT_WR)
        assert tcp.recv(1)==b'' and not tcp.connected
        with pytest.raises(ValueError):tcp.connect()


def test_timeout_on_partial_packet_does_not_reset_deadline():
    with connected() as (channel,peer,expected):
        tcp=transport(channel,expected);peer.sendall(b'\0\0\0\x04a')
        start=time.monotonic()
        with pytest.raises(TimeoutError):tcp.recv(.03)
        assert time.monotonic()-start<1
        assert not tcp.connected
        tcp.close()


def test_close_wakes_blocked_receiver():
    with connected() as (channel,peer,expected):
        tcp=transport(channel,expected);results=[]
        worker=threading.Thread(target=lambda:results.append(tcp.recv(3)));worker.start()
        time.sleep(.03);tcp.close();worker.join(1)
        assert not worker.is_alive() and results==[b'']


def test_large_frame_and_small_socket_buffers_do_not_drop_partial_writes():
    with connected() as (channel,peer,expected):
        tcp=transport(channel,expected,timeout=5);channel._sock.setsockopt(socket.SOL_SOCKET,socket.SO_SNDBUF,4096)
        payload=b'SYNTHETIC'*131072;received=[]
        def receive():
            peer.settimeout(5);remaining=len(payload)+4
            while remaining:
                part=peer.recv(min(4096,remaining));assert part
                received.append(part);remaining-=len(part)
        worker=threading.Thread(target=receive);worker.start()
        try:tcp.send(payload)
        finally:worker.join(6);tcp.close()
        assert not worker.is_alive() and b''.join(received)==struct.pack('>I',len(payload))+payload


def test_partial_send_failure_permanently_closes_this_transport():
    with connected() as (channel,peer,expected):
        tcp=transport(channel,expected);real=tcp._sock
        class FailsAfterPartialWrite:
            count=0
            def __getattr__(self,name):return getattr(real,name)
            def send(self,data):
                self.count+=1
                if self.count>1:raise OSError('synthetic write failure')
                return real.send(data[:2])
        tcp._sock=FailsAfterPartialWrite()
        with pytest.raises(OSError):tcp.send(b'content')
        assert not tcp.connected
        peer.settimeout(1)
        assert peer.recv(10)==b'\0\0' and peer.recv(10)==b''
        with pytest.raises(ValueError):tcp.send(b'do not append')


@pytest.fixture
def library(monkeypatch):
    package=types.ModuleType('smbprotocol');connection=types.ModuleType('smbprotocol.connection')
    transport_module=types.ModuleType('smbprotocol.transport')
    class Original:pass
    connection.Tcp=transport_module.Tcp=Original
    package.connection=connection
    for name,module in [('smbprotocol',package),('smbprotocol.connection',connection),('smbprotocol.transport',transport_module)]:
        monkeypatch.setitem(sys.modules,name,module)
    monkeypatch.setattr(wire.importlib.metadata,'version',lambda name:wire.VERSIONS[name])
    return connection,Original


def test_library_factory_cannot_open_a_second_connection_and_is_restored(library):
    module,original=library
    with connected() as (channel,peer,expected):
        with wire.bind_library(channel) as bound:
            assert bound is module
            tcp=bound.Tcp(*expected,timeout=1);tcp.connect()
            with pytest.raises(ValueError):
                with wire.bind_library(channel):pytest.fail('concurrent global adapter')
            assert tcp.connected  # Failed second binding does not close the owner.
            with pytest.raises(ValueError):bound.Tcp(*expected,timeout=1).connect()
            tcp.close()
        assert module.Tcp is original


@pytest.mark.parametrize('kind',['version','foreign-factory'])
def test_dependency_or_foreign_factory_rejected_and_transferred_fd_closed(library,monkeypatch,kind):
    module,original=library
    if kind=='version':monkeypatch.setattr(wire.importlib.metadata,'version',lambda name:'unknown')
    else:module.Tcp=object()
    with connected() as (channel,peer,expected):
        with pytest.raises(ValueError):
            with wire.bind_library(channel):pytest.fail('unreviewed adapter')
        assert channel._sock.fileno()==-1
    assert not wire._binding_lock.locked()
