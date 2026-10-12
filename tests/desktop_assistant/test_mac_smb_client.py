"""Synthetic native handles and real local FDs; never authenticate to a NAS."""
from contextlib import contextmanager
import ctypes as C
from copy import deepcopy
import os
import sys
from pathlib import Path
import stat
from types import SimpleNamespace
from urllib.parse import unquote,urlsplit
from unittest.mock import Mock
import pytest

pytestmark = pytest.mark.skipif(sys.platform != "darwin",
    reason="Mac UID, filesystem and native service protocol; run on macOS")

from desktop_assistant import mac_smb_client as m
from tests.desktop_assistant import test_mac_smb_contract as f


class Function:
    def __init__(self,fn):self.fn=fn
    def __call__(self,*args):return self.fn(*args)


@pytest.fixture
def transport(tmp_path,monkeypatch):
    point=tmp_path/'archive';point.mkdir(mode=0o700);monkeypatch.setattr(m.cfg,'MOUNT',point)
    for name in ('getuid','geteuid','getgid','getegid'):monkeypatch.setattr(m.os,name,lambda:499)
    monkeypatch.setattr(m.boot,'_actual',lambda:{'kernel_groups':[499]})
    monkeypatch.setattr(m.mounts,'local_addresses',lambda:{'192.168.1.100'})
    state={'events':[],'open_status':0,'mount_status':0,'null_handle':False,'throw':False,'guard_change':False}
    @contextmanager
    def guard(uid):
        assert uid==499;state['events'].append('pin')
        def empty():
            state['events'].append('empty')
            if state['guard_change']:raise ValueError('synthetic changed mountpoint')
        yield empty,lambda:state['events'].append('parents')
        state['events'].append('unpin')
    monkeypatch.setattr(m,'empty_mountpoint',guard)
    def open_server(url,ptr,options):
        state['events'].append('connect');state['url']=url;state['open_options']=options
        if not state['null_handle']:C.cast(ptr,C.POINTER(C.c_void_p)).contents.value=1234
        if state['throw']:raise RuntimeError('SYNTHETIC_PASSWORD_PRIVATE')
        return state['open_status']
    def mount(handle,share,path,flags,options,filemode,dirmode,callout,args):
        state['events'].append('mount');assert handle.value==1234
        assert share==b'erp_archive' and path==os.fsencode(point)
        state['mount_args']=(flags,options,filemode,dirmode,callout,args)
        return state['mount_status']
    def release(handle):state['events'].append('release');assert handle.value==1234;return 0
    lib=SimpleNamespace(SMBOpenServerEx=Function(open_server),SMBMountShareEx=Function(mount),SMBReleaseServer=Function(release))
    monkeypatch.setattr(m,'library',lambda:lib)
    def observe(value,fd,*,context_sha,uid):
        state['events'].append('observe');assert stat.S_ISDIR(os.fstat(fd).st_mode)
        assert context_sha==f.CONTEXT and uid==499
        return f.observation()
    monkeypatch.setattr(m.observer,'observe',observe)
    return state,point


def call():return m.mount(f.value(),'SYNTHETIC @:/#%密码',context_sha=f.CONTEXT,uid=499)


def test_one_new_authenticated_encrypted_session_then_observed_mount(transport):
    state,point=transport
    assert call()==f.observation()
    url=urlsplit(state['url'].decode())
    assert url.hostname=='192.168.1.245' and url.username=='erp_admin' and url.path=='/erp_archive'
    assert unquote(url.password)=='SYNTHETIC @:/#%密码'
    assert state['open_options']==0x10107
    assert not state['open_options'] & (8|16|32|64)  # guest/anonymous are never allowed.
    assert state['mount_args']==(0x100018,0x317,0o600,0o700,None,None)
    assert state['events']==['pin','connect','empty','mount','release','parents','observe','parents','unpin']
    assert not list(point.iterdir())


@pytest.mark.parametrize('bad',['auth','null','mount','changed','exception','unverified'])
def test_native_failures_do_not_fall_back_retry_unmount_or_disclose_secret(transport,monkeypatch,bad):
    state,_=transport
    if bad=='auth':state['open_status']=0xC000006D
    elif bad=='null':state['null_handle']=True
    elif bad=='mount':state['mount_status']=0xC0000001
    elif bad=='changed':state['guard_change']=True
    elif bad=='exception':state['throw']=True
    else:monkeypatch.setattr(m.observer,'observe',Mock(side_effect=ValueError(m.cfg.ERROR)))
    with pytest.raises(ValueError) as error:call()
    assert 'SYNTHETIC' not in str(error.value)
    assert state['events'].count('connect')==1 and state['events'].count('mount')<=1
    assert state['events'].count('release')==(0 if bad=='null' else 1)
    if bad in ('auth','null','changed','exception'):assert 'mount' not in state['events']


@pytest.mark.parametrize('bad',['root','user','group','effective','admin','local','wrong_context','guest'])
def test_wrong_identity_or_endpoint_never_connects(transport,monkeypatch,bad):
    state,_=transport;value=f.value();context=f.CONTEXT
    if bad=='root':monkeypatch.setattr(m.os,'getuid',lambda:0)
    elif bad=='user':monkeypatch.setattr(m.os,'getuid',lambda:501)
    elif bad=='group':monkeypatch.setattr(m.os,'getgid',lambda:20)
    elif bad=='effective':monkeypatch.setattr(m.os,'geteuid',lambda:0)
    elif bad=='admin':monkeypatch.setattr(m.boot,'_actual',lambda:{'kernel_groups':[80,499]})
    elif bad=='local':monkeypatch.setattr(m.mounts,'local_addresses',lambda:{'192.168.1.245'})
    elif bad=='wrong_context':context='d'*64
    else:value['username']='guest'
    with pytest.raises(ValueError):m.mount(value,'SYNTHETIC',context_sha=context,uid=499)
    assert state['events']==[]


@pytest.mark.parametrize('password',['','x'*129,'x\0x','\ud800',b'bytes',None])
def test_password_rejection_happens_before_native_call(transport,password):
    state,_=transport
    with pytest.raises(ValueError):m.mount(f.value(),password,context_sha=f.CONTEXT,uid=499)
    assert not state['events']


def test_installed_library_signatures_are_scalar_and_match_darwin_mode_t(monkeypatch):
    fake=SimpleNamespace(**{n:Function(lambda *a:0) for n in ('SMBOpenServerEx','SMBMountShareEx','SMBReleaseServer')})
    check=Mock();monkeypatch.setattr(m.mounts,'_lib',check)
    load=Mock(return_value=fake);monkeypatch.setattr(m.C,'CDLL',load)
    assert m.library() is fake
    check.assert_called_once_with();load.assert_called_once_with(m.LIBRARY,use_errno=True)
    assert fake.SMBOpenServerEx.argtypes==[C.c_char_p,C.POINTER(C.c_void_p),C.c_uint64]
    assert fake.SMBMountShareEx.argtypes==[C.c_void_p,C.c_char_p,C.c_char_p,C.c_uint,C.c_uint64,C.c_uint16,C.c_uint16,C.c_void_p,C.c_void_p]
    assert fake.SMBMountShareEx.restype is C.c_uint32 and C.sizeof(C.c_uint16)==2


@pytest.mark.parametrize('path,uid,gid,mode,allowed',[
    ('/System/Volumes/Data',0,80,0o775,True),('/System/Volumes/Data',0,0,0o755,True),
    ('/System/Volumes/Data',501,80,0o775,False),('/System/Volumes/Data',0,20,0o775,False),
    ('/System/Volumes/Data',0,80,0o777,False),('/System/Volumes/Data/Library',0,80,0o775,False),
    ('/System/Volumes/Data/Library',0,0,0o755,True),('/System/Volumes/Data/Library',499,499,0o755,False),
])
def test_only_system_data_volume_allows_trusted_admin_group_write(path,uid,gid,mode,allowed):
    assert m._parent_permissions(Path(path),SimpleNamespace(st_uid=uid,st_gid=gid,st_mode=stat.S_IFDIR|mode))==allowed


@pytest.fixture
def point(tmp_path,monkeypatch):
    p=tmp_path/'archive';p.mkdir(mode=0o700);monkeypatch.setattr(m.cfg,'MOUNT',p)
    monkeypatch.setattr(m,'_parent_permissions',lambda path,info:True) # fixture's personal temp ancestors only
    monkeypatch.setattr(m.boot,'no_acl',lambda fd:None)
    # Real local paths/FDs/inodes/symlinks/modes. OS mount metadata is synthetic.
    local=dict(filesystem='apfs',flags=0x1000,mountpoint='/',fsid=[1,2])
    monkeypatch.setattr(m.mounts,'fd_identity',lambda fd:deepcopy(local))
    monkeypatch.setattr(m.mounts,'_select',lambda path:deepcopy(local))
    uid=os.getuid();gid=os.getgid();real=os.fstat
    def identity(fd):
        st=real(fd)
        return SimpleNamespace(st_dev=st.st_dev,st_ino=st.st_ino,st_mode=st.st_mode,st_uid=st.st_uid,st_gid=uid)
    named=os.stat
    def names(*args,**kwargs):
        st=named(*args,**kwargs)
        return SimpleNamespace(st_dev=st.st_dev,st_ino=st.st_ino,st_mode=st.st_mode,st_uid=st.st_uid,st_gid=uid)
    monkeypatch.setattr(m.os,'fstat',identity);monkeypatch.setattr(m.os,'stat',names)
    return p,uid,local


def test_empty_local_mountpoint_is_pinned_without_file_writes(point):
    p,uid,_=point;before=list(p.iterdir())
    with m.empty_mountpoint(uid) as (check,parents):check();parents()
    assert list(p.iterdir())==before==[]


@pytest.mark.parametrize('bad',['contents','symlink','mode','mounted','replaced','parent_changed','acl'])
def test_mountpoint_changes_and_unknown_existing_objects_refused(point,monkeypatch,bad):
    p,uid,local=point
    if bad=='contents':(p/'old').write_bytes(b'preserve')
    elif bad=='symlink':q=p.with_name('other');p.rename(q);p.symlink_to(q)
    elif bad=='mode':p.chmod(0o755)
    elif bad=='mounted':local['filesystem']='smbfs';local['mountpoint']=str(p)
    elif bad=='acl':monkeypatch.setattr(m.boot,'no_acl',Mock(side_effect=ValueError('fixture ACL')))
    with pytest.raises((ValueError,OSError)):
        with m.empty_mountpoint(uid) as (check,parents):
            if bad=='replaced':p.rename(p.with_name('old'));p.mkdir(mode=0o700)
            elif bad=='parent_changed':p.parent.chmod(0o755)
            check()
