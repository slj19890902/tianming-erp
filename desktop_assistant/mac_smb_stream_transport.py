"""Bounded Direct TCP framing over exactly one inherited connected socket.

This primitive does not authorize a connection or read a credential. Its caller
must validate the root contract and signed worker, pass only the approved FD,
and launch under a deny-network-outbound policy. No socket creation, DNS,
authentication, directory access, fallback or reconnect occurs here.
"""
from contextlib import contextmanager
import importlib.metadata
import math
import os
import select
import socket
import stat
import struct
import threading
import time

MAX_FRAME = 0x00ffffff
MAX_TIMEOUT = 600
VERSIONS = {'smbprotocol':'1.17.0', 'pyspnego':'0.12.4'}
ERROR = 'SMB单通道传输未确认；保留现场，不重新连接'
_binding_lock = threading.Lock()


def require(ok):
    if not ok: raise ValueError(ERROR)


def duration(value):
    require(type(value) in (int,float) and 0 < value <= MAX_TIMEOUT and math.isfinite(value))
    return float(value)


class Channel:
    """Own a transferred connected FD. A failed claim cannot be reused."""
    def __init__(self, fd, expected_peer):
        require(type(fd) is int and 3 <= fd <= 2**31-1)
        self._sock = socket.socket(fileno=fd)
        self.peer = expected_peer
        self._claim_lock = threading.Lock()
        self._claimed = False
        try:
            require(self._sock.family == socket.AF_INET and self._sock.type == socket.SOCK_STREAM)
            info = os.fstat(fd)
            require(stat.S_ISSOCK(info.st_mode))
            self._identity = (info.st_dev,info.st_ino,info.st_mode)
            self._local = self._sock.getsockname()
            self._sock.set_inheritable(False)
            self.check()
        except BaseException:
            self._sock.close(); raise

    def check(self):
        info = os.fstat(self._sock.fileno())
        require((info.st_dev,info.st_ino,info.st_mode) == self._identity
            and self._sock.getpeername() == self.peer and self._sock.getsockname() == self._local
            and self._sock.getsockopt(socket.SOL_SOCKET,socket.SO_TYPE) == socket.SOCK_STREAM)

    def claim(self, server, port):
        with self._claim_lock:
            require(not self._claimed)
            self._claimed = True
            require((server,port) == self.peer)
            self.check()
            self._sock.setblocking(False)
            return self._sock

    def close(self): self._sock.close()


class Transport:
    """smbprotocol Tcp interface, with no ability to open another connection."""
    def __init__(self, channel, server, port, timeout):
        self.channel, self.server, self.port = channel, server, port
        self.timeout = duration(timeout)
        self.connected = False
        self._sock = None
        self._send_lock, self._recv_lock, self._close_lock = (threading.Lock() for _ in range(3))
        self._attempted = False

    def connect(self):
        with self._close_lock:
            require(not self._attempted)
            self._attempted = True
            self._sock = self.channel.claim(self.server,self.port)
            self.connected = True

    def close(self):
        with self._close_lock:
            self.connected = False
            if self._sock is not None:
                try: self._sock.shutdown(socket.SHUT_RDWR)
                except OSError: pass
            self.channel.close()

    def _wait(self, deadline, *, writing=False):
        require(self.connected)
        self.channel.check()
        remaining = deadline-time.monotonic()
        if remaining <= 0: raise TimeoutError(ERROR)
        readable,writable,_ = select.select([] if writing else [self._sock], [self._sock] if writing else [], [], remaining)
        if not (writable if writing else readable): raise TimeoutError(ERROR)

    def send(self, header):
        view = memoryview(header)
        require(0 < view.nbytes <= MAX_FRAME)
        data = memoryview(struct.pack('>I',view.nbytes)+view.tobytes())
        deadline = time.monotonic()+self.timeout
        with self._send_lock:
            try:
                offset = 0
                while offset < len(data):
                    self._wait(deadline,writing=True)
                    try: sent = self._sock.send(data[offset:])
                    except BlockingIOError: continue
                    require(sent > 0); offset += sent
            except BaseException:
                self.close(); raise

    def _read_exact(self, size, deadline, *, header=False):
        data = bytearray()
        while len(data) < size:
            self._wait(deadline)
            try: part = self._sock.recv(min(size-len(data),1024*1024))
            except BlockingIOError: continue
            if not part:
                if header and not data: return b''
                raise ValueError(ERROR)
            data.extend(part)
        return bytes(data)

    def recv(self, timeout):
        deadline = time.monotonic()+duration(timeout)
        with self._recv_lock:
            if not self.connected: return b''
            try:
                header = self._read_exact(4,deadline,header=True)
                if not header:
                    self.close(); return b''
                size = struct.unpack('>I',header)[0]
                require(0 < size <= MAX_FRAME)
                return self._read_exact(size,deadline)
            except (OSError,ValueError):
                if not self.connected: return b''
                self.close()
                raise
            except BaseException:
                self.close(); raise


@contextmanager
def bind_library(channel):
    """One isolated worker / one library connection, restored after disconnect.

    Changing this factory redirects no addresses: the factory only claims the
    prevalidated socket once. All new connects remain denied by the OS policy.
    """
    require(_binding_lock.acquire(blocking=False))
    try:
        require(all(importlib.metadata.version(name) == version for name,version in VERSIONS.items()))
        from smbprotocol import connection
        from smbprotocol.transport import Tcp
        require(connection.Tcp is Tcp)
        def factory(server,port,timeout=None):
            return Transport(channel,server,port,timeout)
        connection.Tcp = factory
        try: yield connection
        finally: connection.Tcp = Tcp
    finally:
        channel.close()
        _binding_lock.release()
