"""SMB identity metadata, synthetic buffers; real NAS proof is kept separately."""
from copy import deepcopy
import ctypes as C
import errno
import json
import os
import platform
import socket
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from desktop_assistant import mac_smb_mount as m
from tests.desktop_assistant import test_mac_smb_contract as f


def test_sized_darwin_commands_match_native_c_sdk_oracle():
    # Independent xcrun/Clang oracle uses Apple's exact struct declarations
    # and the installed Darwin sys/ioccom.h. _IOR is IOC_OUT=0x40000000.
    assert m._commands() == (0xC5987A13, 0x40907A1E)


def buffers():
    access=m.Access();access.flags=1;access.error=errno.EEXIST;access.auth=0;access.user=b'erp_admin'
    peer=m.Peer();peer.uuid[:]=bytes.fromhex('a'*32)
    address=bytes((16,socket.AF_INET))+b'\x01\xbd'+socket.inet_pton(socket.AF_INET,'192.168.1.245')+b'\0'*120
    C.memmove(C.addressof(peer)+m.Peer.address.offset,address,128)
    return access,peer


def document():
    return [{'share_name':'erp_archive','SERVER_NAME':'NAS','USER_ID':499,'SMB_SHARE_TYPE':'DISK',
        'VOLUME_RDONLY':False,'DFS_SHARE':False,'SNAPSHOT_MOUNT':False,'ENCRYPTION_REQUIRED':True,
        'SIGNING_ON':False,'SMB_VERSION':'SMB_3.1.1','SMB_CURR_ENCRYPT_ALGORITHM':'AES-128-GCM',
        'SESSION_RECONNECT_COUNT':0,'ENCRYPTION_SUPPORTED':True}]


def test_native_buffers_decode_real_layout():
    access,peer=buffers()
    assert m._decode(access,peer)==f.observation()['session']


@pytest.mark.parametrize('auth,name',[(1,'guest'),(2,'anonymous'),(4,'kerberos')])
def test_non_password_authentication_is_observed_never_approved(auth,name):
    access,peer=buffers();access.auth=auth
    result=m._decode(access,peer)
    assert result['auth_type']==name
    o=f.observation();o['session']=result
    with pytest.raises(ValueError):m.cfg.require_observation(f.value(),o)


@pytest.mark.parametrize('bad',['flags','missing_success','unknown_auth','unterminated','invalid_utf8','ipv6','bad_length','zero_uuid','zero_port'])
def test_incomplete_or_ambiguous_native_buffer_is_refused(bad):
    access,peer=buffers()
    if bad=='flags':access.flags=3
    elif bad=='missing_success':access.error=0
    elif bad=='unknown_auth':access.auth=8
    elif bad=='unterminated':C.memset(C.addressof(access)+m.Access.user.offset,65,129)
    elif bad=='invalid_utf8':access.user=b'\xff'
    elif bad=='ipv6':C.memmove(C.addressof(peer)+m.Peer.address.offset+1,bytes([socket.AF_INET6]),1)
    elif bad=='bad_length':C.memset(C.addressof(peer)+m.Peer.address.offset,128,1)
    elif bad=='zero_uuid':peer.uuid[:]=b'\0'*16
    elif bad=='zero_port':C.memset(C.addressof(peer)+m.Peer.address.offset+2,0,2)
    with pytest.raises(ValueError):m._decode(access,peer)


class Function:
    def __init__(self,fn):self.fn=fn
    def __call__(self,*args):return self.fn(*args)


@pytest.fixture
def native(monkeypatch):
    state={'calls':[],'failure':None,'peer_change':False,'mount_change':False}
    access,peer=buffers();queries=[0]
    def fsctl(fd,command,ptr,flags):
        assert fd==171 and flags==0
        state['calls'].append(command)
        if len(state['calls'])==state['failure']:return -1
        if command==0x40907A1E:
            data=bytes(peer)
            if state['peer_change'] and len(state['calls'])==3:data=b'\xbb'+data[1:]
        else:
            assert command==0xC5987A13
            request=C.cast(ptr,C.POINTER(m.Access)).contents
            assert request.auth==-1 and request.flags==1
            data=bytes(access)
        C.memmove(ptr,data,len(data));return 0
    lib=SimpleNamespace(ffsctl=Function(fsctl))
    monkeypatch.setattr(m.mounts,'_lib',lambda:lib)
    def identity(fd):
        assert fd==171;queries[0]+=1;k=f.kernel()
        if state['mount_change'] and queries[0]>1:k['fsid'][0]+=1
        return k
    monkeypatch.setattr(m.mounts,'fd_identity',identity)
    return state


def test_sized_calls_pin_session_across_authentication_query(native):
    assert m.session(171)==f.observation()['session']
    assert native['calls']==[0x40907A1E,0xC5987A13,0x40907A1E]


@pytest.mark.parametrize('failed_call',[1,2,3])
def test_failed_fsctl_never_uses_partially_filled_buffer(native,failed_call):
    native['failure']=failed_call
    with pytest.raises(ValueError):m.session(171)
    assert len(native['calls'])==failed_call


@pytest.mark.parametrize('change',['peer_change','mount_change'])
def test_native_session_or_mount_change_refused(native,change):
    native[change]=True
    with pytest.raises(ValueError):m.session(171)


def test_local_fd_never_invokes_smb_fsctl(monkeypatch):
    monkeypatch.setattr(m.mounts,'fd_identity',lambda fd:{'filesystem':'apfs'})
    lib=Mock();monkeypatch.setattr(m.mounts,'_lib',lib)
    with pytest.raises(ValueError):m.session(171)
    lib.assert_not_called()


def test_statshares_parser_extracts_only_required_metadata():
    doc=document();doc[0]['arbitrary_capability']='unused'
    assert m.parse_share(json.dumps(doc).encode())==f.observation()['share']


@pytest.mark.parametrize('key,bad',[
    ('SMB_SHARE_TYPE','PIPE'),('VOLUME_RDONLY',True),('VOLUME_RDONLY',0),('DFS_SHARE',True),
    ('SNAPSHOT_MOUNT',True),('ENCRYPTION_REQUIRED',False),('ENCRYPTION_REQUIRED',1)])
def test_readonly_dfs_snapshot_or_only_negotiated_cipher_not_approved(key,bad):
    doc=document();doc[0][key]=bad
    with pytest.raises(ValueError):m.parse_share(json.dumps(doc).encode())


@pytest.mark.parametrize('raw',[b'',b'x'*65537,b'{',b'[]',b'{}',b'[{},{}]',b'[null]',
    b'[{"uid":1,"uid":2}]',b'[{"x":NaN}]',b'[{"x":Infinity}]',None,'text'])
def test_invalid_ambiguous_or_unbounded_json_refused(raw):
    with pytest.raises(ValueError):m.parse_share(raw)


@pytest.fixture
def observer(monkeypatch):
    state={'kernel':f.kernel(),'session':f.observation()['session'],'document':document(),'queries':0,
        'change_mount_after':False,'change_session_after':False,'result_code':0,'timeout':False}
    monkeypatch.setattr(m.os,'getuid',lambda:499);monkeypatch.setattr(m.os,'geteuid',lambda:499)
    monkeypatch.setattr(m.mounts,'local_addresses',lambda:{'127.0.0.1','192.168.1.30'})
    def identity(fd):
        assert fd==171;k=deepcopy(state['kernel'])
        if state['queries'] and state['change_mount_after']:k['fsid'][0]+=1
        return k
    monkeypatch.setattr(m.mounts,'fd_identity',identity)
    monkeypatch.setattr(m.mounts,'_select',lambda path:identity(171))
    def session(fd):
        result=deepcopy(state['session'])
        if state['queries'] and state['change_session_after']:result['uuid']='b'*32
        return result
    monkeypatch.setattr(m,'session',session)
    def run(command,**kwargs):
        state['queries']+=1
        assert command==['/usr/bin/smbutil','statshares','-m',str(m.cfg.MOUNT),'-f','JSON']
        assert kwargs==dict(stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
            timeout=5,env={'PATH':'/usr/bin:/bin','LC_ALL':'C','HOME':'/var/empty'})
        if state['timeout']:raise subprocess.TimeoutExpired(command,5)
        return SimpleNamespace(returncode=state['result_code'],stdout=json.dumps(state['document']).encode())
    monkeypatch.setattr(m.subprocess,'run',run)
    return state


def observe():return m.observe(f.value(),171,context_sha=f.CONTEXT,uid=499)


def test_full_observation_binds_fd_kernel_authenticated_peer_and_current_encryption(observer):
    assert observe()==f.observation() and observer['queries']==1


@pytest.mark.parametrize('change',['root','ordinary','euid','local','guest','foreign_user','foreign_address',
    'readonly','wrong_mount','wrong_source','wrong_owner','wrong_fsid','topology'])
def test_unapproved_identity_rejected_before_system_query(observer,monkeypatch,change):
    if change=='root':monkeypatch.setattr(m.os,'getuid',lambda:0)
    elif change=='ordinary':monkeypatch.setattr(m.os,'getuid',lambda:501)
    elif change=='euid':monkeypatch.setattr(m.os,'geteuid',lambda:0)
    elif change=='local':monkeypatch.setattr(m.mounts,'local_addresses',lambda:{'192.168.1.245'})
    elif change=='guest':observer['session']['auth_type']='guest'
    elif change=='foreign_user':observer['session']['username']='other'
    elif change=='foreign_address':observer['session']['address']='192.168.1.246'
    elif change=='readonly':observer['kernel']['flags']|=1
    elif change=='wrong_mount':observer['kernel']['mountpoint']='/Volumes/unbound'
    elif change=='wrong_source':observer['kernel']['source']='//erp_admin@192.168.1.245/other'
    elif change=='wrong_owner':observer['kernel']['owner']=0
    elif change=='wrong_fsid':observer['kernel']['fsid']=[0,0]
    else:monkeypatch.setattr(m.mounts,'_select',lambda path:{**f.kernel(),'fsid':[1,2]})
    with pytest.raises(ValueError):observe()
    assert observer['queries']==0


@pytest.mark.parametrize('change',['change_mount_after','change_session_after','result_code','timeout','crypto'])
def test_changes_or_query_failure_never_produce_approved_observation(observer,change):
    if change=='crypto':observer['document'][0]['ENCRYPTION_REQUIRED']=False
    else:observer[change]=1
    with pytest.raises((ValueError,subprocess.TimeoutExpired)):observe()
    assert observer['queries']==1


@pytest.mark.skipif(sys.platform!='darwin' or platform.machine()!='arm64',reason='native Darwin arm64 only')
def test_real_local_fd_is_classified_and_rejected_before_smb_ioctl(tmp_path):
    fd=os.open(tmp_path,os.O_RDONLY|os.O_DIRECTORY)
    try:
        assert m.mounts.fd_identity(fd)['filesystem']=='apfs'
        with pytest.raises(ValueError):m.session(fd)
    finally:os.close(fd)
