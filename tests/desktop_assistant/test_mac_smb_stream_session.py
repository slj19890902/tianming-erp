"""Protocol-state tests use explicit doubles; crypto/socket tests use real bytes."""
from contextlib import contextmanager
import importlib.metadata
import logging
import os
import socket
import struct
import sys
import time
import types
import uuid

import pytest

from desktop_assistant import mac_smb_stream_session as subject
from desktop_assistant.mac_smb_stream_transport import Channel, Transport


def value():
    return {'type':'tianming.mac-smb-stream-archive.v1','context_sha256':'a'*64,
        'uid':499,'server_address':'192.168.1.245','port':445,'username':'backup_admin',
        'share':'备份','relative_directory':'BoxERP/mac-server-backups',
        'server_guid':'b'*32,'protection':'smb311-session-aes-gcm'}


def plain(command=0, *, response=False):
    data=bytearray(64); data[:4]=b'\xfeSMB'
    struct.pack_into('<H',data,4,64); struct.pack_into('<H',data,12,command)
    struct.pack_into('<I',data,16,int(response))
    return bytes(data)


def encrypted(sid=17):
    data=bytearray(116); data[:4]=b'\xfdSMB'
    struct.pack_into('<IHHQ',data,36,64,0,1,sid)
    return bytes(data)


def test_handshake_allows_only_negotiate_then_bounded_setup():
    guard=subject.FrameGuard()
    guard(plain(),writing=True); guard(plain(response=True),writing=False)
    for _ in range(8):
        guard(plain(1),writing=True); guard(plain(1,response=True),writing=False)
    with pytest.raises(ValueError): guard(plain(1),writing=True)


@pytest.mark.parametrize('data,writing',[(b'',True),(plain(3),True),(plain(),False),
    (plain(response=True),True),(encrypted(),False)])
def test_handshake_rejects_non_handshake_or_wrong_direction(data,writing):
    with pytest.raises(ValueError): subject.FrameGuard()(data,writing=writing)


def test_repeated_negotiate_or_setup_before_negotiate_rejected():
    with pytest.raises(ValueError): subject.FrameGuard()(plain(1),writing=True)
    guard=subject.FrameGuard(); guard(plain(),writing=True)
    with pytest.raises(ValueError): guard(plain(),writing=True)


@pytest.mark.parametrize('writing',[True,False])
@pytest.mark.parametrize('offset,replacement',[(0,b'\xfeSMB'),(32,b'bad!'),
    (36,struct.pack('<I',63)),(40,b'\1\0'),(42,b'\0\0'),(44,struct.pack('<Q',18))])
def test_encrypted_frame_rejects_header_downgrade(offset,replacement,writing):
    guard=subject.FrameGuard(); guard.authenticated(17)
    data=bytearray(encrypted()); data[offset:offset+len(replacement)]=replacement
    with pytest.raises(ValueError): guard(bytes(data),writing=writing)


def test_encrypted_phase_is_one_way_and_closed_guard_rejects():
    guard=subject.FrameGuard(); guard.authenticated(17)
    for writing in (False,True):
        guard(encrypted(),writing=writing)
        with pytest.raises(ValueError): guard(plain(1,response=not writing),writing=writing)
    with pytest.raises(ValueError): guard.authenticated(18)
    guard.close()
    with pytest.raises(ValueError): guard(encrypted(),writing=False)


class Field:
    def __init__(self,value): self.value=value
    def get_value(self): return self.value


def response(command=1,flags=9,sid=17):
    return {k:Field(v) for k,v in dict(command=command,status=0,session_id=sid,flags=flags).items()}


@pytest.fixture
def protocol(monkeypatch):
    state=types.SimpleNamespace(events=[], change_connection={},change_session={},
        change_tree={}, password='synthetic password', failure=None,instances={})
    def fail(phase):
        if state.failure == phase: raise RuntimeError('synthetic password must not leak')
    class FakeChannel:
        peer=('192.168.1.245',445)
        def check(self): state.events.append('channel_check'); fail('channel')
        def close(self): state.events.append('channel_close')
    class FakeConnection:
        def __init__(self,guid,server,**kwargs):
            assert server=='192.168.1.245' and kwargs=={'port':445,'require_signing':True}
            self.server_guid=uuid.UUID(hex='b'*32); self.session_table={}; self.transport=None
            self.dialect=0x311; self.cipher_id=2; self.preauth_integrity_hash_id=1
            self.supports_encryption=True; self.require_signing=True
            state.instances['connection']=self
        def connect(self,**kwargs):
            state.events.append('negotiate'); self.transport=object()
            assert kwargs=={'dialect':0x311,'timeout':30.0,'preferred_encryption_algos':[2]}
            assert self.client_capabilities==0x44
            self.__dict__.update(state.change_connection); fail('negotiate')
        def receive(self,request,**kwargs):
            assert kwargs['resolve_symlinks'] is False and 0<kwargs['timeout']<=30
            return state.setup_response
        def verify_signature(self,r,sid,**kwargs):
            assert r is state.setup_response and sid==17 and kwargs=={'force':True}
            state.events.append('verify_signature'); fail('signature')
        def disconnect(self,**kwargs):
            assert kwargs=={'close':False}; state.events.append('disconnect'); fail('disconnect')
    class FakeSession:
        def __init__(self,connection,**kwargs):
            assert kwargs=={'username':'backup_admin','password':state.password,
                            'require_encryption':True,'auth_protocol':'ntlm'}
            self.__dict__.update(kwargs); self.connection=connection; self.session_id=17
            state.instances['session']=self
        def connect(self):
            state.events.append('authenticate'); fail('authenticate')
            self.connection.receive(object())
            self._connected=True; self.encrypt_data=True
            for k in ('session_key','full_session_key','signing_key','encryption_key','decryption_key','application_key'):
                setattr(self,k,b'k'*16)
            self.connection.session_table[17]=self; self.tree_connect_table={}
            self.__dict__.update(state.change_session)
    class FakeTree:
        def __init__(self,session,share):
            assert share=='\\\\192.168.1.245\\备份'
            assert session.password is None
            self.session=session; self.tree_connect_id=23; self.is_dfs_share=False
            state.instances['tree']=self
        def connect(self,**kwargs):
            assert kwargs=={'require_secure_negotiate':True}
            assert 'verify_signature' in state.events
            state.events.append('tree_connect'); fail('tree')
            self._connected=True; self.session.tree_connect_table[23]=self
            self.__dict__.update(state.change_tree)
    @contextmanager
    def binding(channel,guard):
        state.guard=guard
        yield types.SimpleNamespace(Connection=FakeConnection)
        state.events.append('binding_exit')
    monkeypatch.setattr(subject,'bind_library',binding)
    monkeypatch.setitem(sys.modules,'smbprotocol.session',types.SimpleNamespace(Session=FakeSession))
    monkeypatch.setitem(sys.modules,'smbprotocol.tree',types.SimpleNamespace(TreeConnect=FakeTree))
    state.setup_response=response()
    state.channel=FakeChannel()
    def provider(): state.events.append('credential'); fail('credential'); return state.password
    state.provider=provider
    return state


def enter(p): return subject.open_share(p.channel,value(),'a'*64,499,p.provider)


def test_only_verified_encrypted_share_is_yielded_and_all_secret_references_cleared(protocol):
    p=protocol
    with enter(p) as tree:
        assert tree is p.instances['tree']
        p.guard(encrypted(),writing=True)
        assert p.events.index('negotiate')<p.events.index('credential')<p.events.index('authenticate')
        assert p.events.count('credential')==1
    assert p.events.count('disconnect')==1 and p.events[-1]=='channel_close'
    s=p.instances['session']
    assert s.password is s.session_key is s.encryption_key is s.decryption_key is None


@pytest.mark.parametrize('field,new',[('dialect',0x302),('cipher_id',1),
    ('preauth_integrity_hash_id',None),('server_guid',uuid.UUID(hex='c'*32)),
    ('supports_encryption',False),('require_signing',False)])
def test_negotiation_mismatch_never_reads_credential(protocol,field,new):
    protocol.change_connection[field]=new
    with pytest.raises(ValueError,match=subject.ERROR):
        with enter(protocol): pytest.fail('yielded')
    assert 'credential' not in protocol.events
    assert protocol.events.count('disconnect')==1


@pytest.mark.parametrize('password',['',None,b'bytes','a\0b','x'*1025])
def test_invalid_password_never_authenticates(protocol,password):
    protocol.password=password
    with pytest.raises(ValueError):
        with enter(protocol): pytest.fail('yielded')
    assert 'authenticate' not in protocol.events


@pytest.mark.parametrize('field,new',[('encrypt_data',False),('_connected',False),
    ('require_encryption',False),('session_key',None),('signing_key',b'\0'*16),
    ('decryption_key',b'x'*32),('encryption_key',None),('auth_protocol','negotiate'),
    ('session_id',0),('session_id',True),('username','guest')])
def test_guest_null_or_bad_session_never_opens_share(protocol,field,new):
    protocol.change_session[field]=new
    with pytest.raises(ValueError):
        with enter(protocol): pytest.fail('yielded')
    assert 'tree_connect' not in protocol.events
    assert protocol.instances['session'].password is None


@pytest.mark.parametrize('field,new',[('is_dfs_share',True),('_connected',False),
    ('tree_connect_id',0),('tree_connect_id',24),('tree_connect_id',True)])
def test_unusable_or_dfs_share_not_yielded(protocol,field,new):
    protocol.change_tree[field]=new
    with pytest.raises(ValueError):
        with enter(protocol): pytest.fail('yielded')
    assert protocol.events.count('disconnect')==1


@pytest.mark.parametrize('phase',['channel','negotiate','credential','authenticate','signature','tree','disconnect'])
def test_failure_is_generic_no_reconnect_and_channel_closed(protocol,phase):
    protocol.failure=phase
    with pytest.raises(ValueError) as error:
        with enter(protocol): pass
    assert str(error.value)==subject.ERROR and error.value.__suppress_context__
    assert protocol.events.count('credential')<=1 and protocol.events.count('negotiate')<=1
    assert protocol.events[-1]=='channel_close'
    if 'session' in protocol.instances: assert protocol.instances['session'].password is None


@pytest.mark.parametrize('r',[response(flags=1),response(sid=18)])
def test_final_setup_requires_signature_and_exact_session(protocol,r):
    protocol.setup_response=r
    with pytest.raises(ValueError):
        with enter(protocol): pytest.fail('yielded')
    assert 'tree_connect' not in protocol.events


def test_runtime_encryption_change_fails_even_after_consumer_returns(protocol):
    with pytest.raises(ValueError):
        with enter(protocol) as tree: tree.session.encrypt_data=False


def test_peer_mismatch_never_negotiates_or_reads_secret(protocol):
    protocol.channel.peer=('192.168.1.244',445)
    with pytest.raises(ValueError):
        with enter(protocol): pytest.fail('yielded')
    assert 'negotiate' not in protocol.events and 'credential' not in protocol.events
    assert protocol.events[-1]=='channel_close'


def test_consumer_failure_closes_session_and_clears_keys(protocol):
    with pytest.raises(ValueError,match=subject.ERROR):
        with enter(protocol): raise RuntimeError('synthetic consumer failure')
    assert protocol.events.count('disconnect')==1
    assert protocol.instances['session'].encryption_key is None


def test_keyboard_interrupt_still_closes_channel(protocol):
    with pytest.raises(KeyboardInterrupt):
        with enter(protocol): raise KeyboardInterrupt()
    assert protocol.events.count('disconnect')==1
    assert protocol.instances['session'].decryption_key is None


def test_protocol_logging_suppressed_and_restored(caplog):
    logger=logging.getLogger('smbprotocol.synthetic')
    logger.setLevel(logging.DEBUG)
    old=(logger.level,logger.disabled)
    with caplog.at_level(logging.DEBUG),subject._quiet_protocol():
        logger.error('do not expose synthetic NTLM token')
        logging.getLogger('spnego.newly_created').warning('synthetic secret')
    assert not caplog.records and (logger.level,logger.disabled)==old


def test_receive_deadline_and_symlink_resolution_cannot_be_disabled():
    seen=[]
    class Base:
        def receive(self,request,**kwargs): seen.append(kwargs); return response(command=5)
    cls=subject._connection_class(Base,30); c=cls(); c.handshake_deadline=time.monotonic()-1
    with pytest.raises(ValueError): c.receive(None)
    assert seen==[]
    c.handshake_done=True
    c.receive(None,timeout=500,resolve_symlinks=True)
    assert seen==[{'wait':True,'timeout':30,'resolve_symlinks':False}]


@pytest.mark.parametrize('share_type,flags,caps,allowed',[(1,0,0,True),(1,0x8000,0,True),
    (2,0,0,False),(3,0,0,False),(1,1,0,False),(1,2,0,False),(1,0,8,False)])
def test_real_tree_response_requires_disk_without_dfs(share_type,flags,caps,allowed):
    body=struct.pack('<HBBIII',16,share_type,0,flags,caps,0x1f01ff)
    class Base:
        def receive(self,request,**kwargs):
            r=response(command=3); r['data']=Field(body); return r
    cls=subject._connection_class(Base,30); c=cls(); c.handshake_done=True
    if allowed: c.receive(None)
    else:
        with pytest.raises(ValueError): c.receive(None)


def test_real_library_encrypted_structure_roundtrip_and_tamper():
    connection=pytest.importorskip('smbprotocol.connection')
    assert importlib.metadata.version('smbprotocol')=='1.17.0'
    from cryptography.exceptions import InvalidTag
    c=connection.Connection(uuid.uuid4(),'192.168.1.245')
    c.dialect=0x311; c.cipher_id=2
    session=types.SimpleNamespace(session_id=17,encryption_key=b'k'*16,decryption_key=b'k'*16)
    c.session_table[17]=session
    ciphertext=c._encrypt(plain(5),session)
    guard=subject.FrameGuard(); guard.authenticated(17)
    guard(ciphertext.pack(),writing=True)
    assert c._decrypt(ciphertext)==plain(5)
    damaged=bytearray(ciphertext.pack()); damaged[-1]^=1
    parsed=connection.SMB2TransformHeader(); parsed.unpack(bytes(damaged))
    with pytest.raises(InvalidTag): c._decrypt(parsed)
    if os.name!='posix': pytest.skip('real POSIX socket FD transfer')
    listener=socket.socket(); listener.bind(('127.0.0.1',0)); listener.listen(1)
    client=socket.create_connection(listener.getsockname()); peer,_=listener.accept(); listener.close()
    ch=Channel(client.detach(),peer.getsockname()); t=Transport(ch,*peer.getsockname(),2,guard)
    try:
        t.connect(); t.send(ciphertext)  # Regression: encrypted Structure isn't a buffer.
        peer.settimeout(2)
        expected=struct.pack('>I',len(ciphertext.pack()))+ciphertext.pack()
        received=b''
        while len(received)<len(expected): received+=peer.recv(len(expected)-len(received))
        assert received==expected
        peer.sendall(expected)
        actual=t.recv(2)
        decoded=connection.SMB2TransformHeader(); decoded.unpack(actual)
        assert c._decrypt(decoded)==plain(5)
        # A frame guard is structural; the library must still reject a bad tag.
        bad=bytearray(actual); bad[4]^=1
        guard(bytes(bad),writing=False)
        decoded.unpack(bytes(bad))
        with pytest.raises(InvalidTag): c._decrypt(decoded)
        peer.sendall(struct.pack('>I',64)+plain(5,response=True))
        with pytest.raises(ValueError): t.recv(2)
        assert not t.connected
    finally: t.close(); peer.close()
