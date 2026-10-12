"""Three root-connected SMB channels over one inherited Unix stream.

Internal transport capability, not standalone authorization or a daemon. The
root caller must first approve its code pack, pin the original root plan and
NAS records, verify the fixed signed worker, and retain its actual child handle.
Root only opens TCP: NAS credentials, authentication and data never cross this
control channel. It grants no wildcard route, DNS, retry or caller-chosen peer.
"""
import array
import ctypes
import errno
import os
import socket
import struct
import sys
from contextlib import contextmanager

from desktop_assistant import current_mac_worker_contract as c
from desktop_assistant import mac_smb_stream_contract as archive

LIMIT = 2048
ORDER = ('probe','upload','download')
REQUEST = 'tianming.current-mac-smb-connect.v1'
REPLY = 'tianming.current-mac-smb-channel.v1'
FIELDS = frozenset({'type','challenge','action','plan_sha256','control_sha256','contract_sha256'})
ERROR = 'Current Mac backup channel rejected; preserve scene; no fallback or retry'
CAPABILITY = 'current_mac_worker_broker_v1'
MODULE = 'desktop_assistant/current_mac_smb_broker.py'
_active = None


def require(ok):
    if not ok:raise ValueError(ERROR)


def _unix(channel):
    require(channel.family == socket.AF_UNIX
        and channel.getsockopt(socket.SOL_SOCKET,socket.SO_TYPE) == socket.SOCK_STREAM)


def _peer(channel):
    require(sys.platform == 'darwin')
    _unix(channel)
    library=ctypes.CDLL(None,use_errno=True)
    operation=library.getpeereid
    operation.argtypes=[ctypes.c_int,ctypes.POINTER(ctypes.c_uint),ctypes.POINTER(ctypes.c_uint)]
    operation.restype=ctypes.c_int
    uid,gid=ctypes.c_uint(),ctypes.c_uint()
    require(operation(channel.fileno(),ctypes.byref(uid),ctypes.byref(gid)) == 0)
    return uid.value,gid.value


def _binding(plan,plan_sha,challenge,contract):
    c.validate_plan(plan);require(plan['phase'] == 'update' and c.sha(plan_sha) and c.token(challenge))
    require(c.raw_digest(c.canonical(plan)) == plan_sha)
    archive.validate(contract,plan['context_sha256'],plan['uid'])
    require(archive.digest(contract) == plan['transport_contract_sha256'])
    return dict(challenge=challenge,plan_sha256=plan_sha,
        control_sha256=plan['transport_control_sha256'],contract_sha256=plan['transport_contract_sha256'])


def _frame(value):
    raw=c.canonical(value);require(0 < len(raw) <= LIMIT)
    return struct.pack('!I',len(raw))+raw


def _receive_exact(channel,size):
    parts=[]
    while size:
        chunk=channel.recv(size);require(bool(chunk))
        parts.append(chunk);size-=len(chunk)
    return b''.join(parts)


def _tcp(channel,endpoint):
    require(channel.family == socket.AF_INET
        and channel.getsockopt(socket.SOL_SOCKET,socket.SO_TYPE) == socket.SOCK_STREAM
        and channel.getpeername() == endpoint)


def _administrator():
    require((os.getuid(),os.geteuid(),os.getgid(),os.getegid()) == (0,0,0,0))


class Server:
    """Caller invokes feed for readable control bytes while draining its child.

    No thread, shell, secret input, background job or filesystem socket exists.
    The public recheck callback must continuously authenticate the root inputs.
    A failed feed poisons this instance permanently; no second connection is
    attempted, including after connect or send errors.
    """
    def __init__(self,channel,plan,plan_sha,challenge,contract,recheck):
        _administrator()
        _unix(channel);require(callable(recheck))
        self.expected=_binding(plan,plan_sha,challenge,contract)
        self.channel,self.recheck=channel,recheck
        self.endpoint=(contract['server_address'],contract['port'])
        self.buffer=bytearray();self.sequence=0;self.failed=False
        recheck()

    def feed(self,chunk):
        require(not self.failed)
        try:
            require(type(chunk) is bytes and 0 < len(chunk) <= LIMIT+4)
            self.buffer.extend(chunk);require(len(self.buffer) <= LIMIT+4)
            if len(self.buffer) < 4:return
            size=struct.unpack('!I',self.buffer[:4])[0];require(0 < size <= LIMIT)
            if len(self.buffer) < 4+size:return
            # One outstanding request only. Coalesced or trailing messages are
            # refused before any network connection is opened.
            require(len(self.buffer) == size+4 and self.sequence < len(ORDER))
            value=c.decode(bytes(self.buffer[4:]));c.exact(value,FIELDS)
            require(value == dict(type=REQUEST,action=ORDER[self.sequence],**self.expected))
            self.buffer.clear();self.sequence+=1
            self.recheck()
            with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as connected:
                connected.settimeout(10)
                connected.connect(self.endpoint);_tcp(connected,self.endpoint)
                connected.settimeout(None);self.recheck()
                self.channel.sendall(_frame({**value,'type':REPLY}))
                rights=array.array('i',[connected.fileno()])
                require(self.channel.sendmsg([b'F'],[(socket.SOL_SOCKET,socket.SCM_RIGHTS,rights)]) == 1)
            self.recheck()
        except BaseException:
            self.failed=True
            self.channel.close()  # Wake the client; outer caller still awaits its actual child.
            raise

    def finish(self):
        try:
            require(not self.failed and self.sequence == len(ORDER) and not self.buffer)
            self.recheck()
        except BaseException:
            self.failed=True;self.channel.close()
            raise


class Client:
    """One-use connection claims; all NAS bytes travel on separate TCP FDs."""
    def __init__(self,channel,plan,plan_sha,challenge,contract):
        _unix(channel);require(_peer(channel) == (0,0))
        self.channel=channel
        self.expected=_binding(plan,plan_sha,challenge,contract)
        self.endpoint=(contract['server_address'],contract['port'])
        self.sequence=0;self.failed=False

    def connection(self,action):
        require(not self.failed)
        descriptors=[];connected=None
        try:
            require(self.sequence < len(ORDER) and action == ORDER[self.sequence]
                and _peer(self.channel) == (0,0))
            value=dict(type=REQUEST,action=action,**self.expected)
            self.channel.sendall(_frame(value))
            size=struct.unpack('!I',_receive_exact(self.channel,4))[0]
            require(0 < size <= LIMIT)
            response=c.decode(_receive_exact(self.channel,size));c.exact(response,FIELDS)
            require(response == {**value,'type':REPLY})
            unit=array.array('i').itemsize
            token,ancillary,flags,_=self.channel.recvmsg(1,socket.CMSG_SPACE(unit*16))
            invalid=False
            for level,kind,data in ancillary:
                if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                    rights=array.array('i');rights.frombytes(data[:len(data)-len(data)%unit])
                    descriptors.extend(rights)
                    invalid=invalid or len(data)%unit != 0
                else:invalid=True
            require(token == b'F' and not invalid
                and not flags & (socket.MSG_TRUNC|socket.MSG_CTRUNC) and len(descriptors) == 1)
            fd=descriptors.pop();os.set_inheritable(fd,False)
            try:connected=socket.socket(fileno=fd)
            except BaseException:os.close(fd);raise
            _tcp(connected,self.endpoint)
            require(_peer(self.channel) == (0,0))
            self.sequence+=1
            return connected
        except BaseException:
            self.failed=True
            if connected is not None:connected.close()
            self.channel.close()  # Also discard queued, unclaimed SCM_RIGHTS.
            raise
        finally:
            for fd in descriptors:os.close(fd)


@contextmanager
def dedicated(fd,plan,plan_sha,challenge,contract):
    """Worker-owned session; the CLI supplies only one inherited control FD."""
    global _active
    require(_active is None and type(fd) is int and 3 <= fd <= c.MAX_FD)
    os.set_inheritable(fd,False)
    channel=socket.socket(fileno=fd)
    try:
        client=Client(channel,plan,plan_sha,challenge,contract)
        _active=client
        yield
        require(not client.failed and client.sequence == len(ORDER))
    finally:
        _active=None;channel.close()


def connection(binding,action):
    """None means the old explicit direct mode, never a failed-broker fallback."""
    if _active is None:return None
    require(_active.expected['control_sha256'] == binding.control_sha
        and _active.expected['contract_sha256'] == binding.control['contract_sha256']
        and _active.endpoint == (binding.value['server_address'],binding.value['port']))
    return _active.connection(action)
