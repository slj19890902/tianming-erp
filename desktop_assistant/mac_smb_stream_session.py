"""Encrypted single-channel SMB session, not an installation/credential authority.

The signed worker must validate root records, actual identity and its outbound
deny policy before supplying a Channel and credential callback here. This layer
does not open sockets, read Keychain, upload files, or publish backup success.
"""
from contextlib import contextmanager
import logging
import struct
import threading
import time
import uuid

from . import mac_smb_stream_contract as contract
from .mac_smb_stream_transport import bind_library, duration

ERROR = 'SMB加密会话未确认；保留现场，不重新连接'


def require(ok):
    if not ok: raise ValueError(ERROR)


class FrameGuard:
    """Reject plaintext after authentication, before library message parsing.

    AEAD authentication remains the pinned library's job. Structural validation
    alone is never evidence of authenticated ciphertext or a completed backup.
    """
    def __init__(self):
        self._lock = threading.Lock()
        self._session_id = None
        self._closed = False
        self._negotiate_count = self._setup_count = 0

    def authenticated(self, session_id):
        with self._lock:
            require(not self._closed and self._session_id is None
                    and type(session_id) is int and 0 < session_id < 2**64)
            self._session_id = session_id

    def close(self):
        with self._lock: self._closed = True

    def __call__(self, data, *, writing):
        with self._lock:
            require(not self._closed and type(data) is bytes)
            if self._session_id is None:
                require(len(data) >= 64 and data[:4] == b'\xfeSMB')
                size, = struct.unpack_from('<H',data,4)
                command, = struct.unpack_from('<H',data,12)
                flags, next_command = struct.unpack_from('<II',data,16)
                require(size == 64 and command in (0,1) and next_command == 0
                        and bool(flags & 1) != writing)
                if writing:
                    if command == 0:
                        self._negotiate_count += 1
                        require(self._negotiate_count == 1 and self._setup_count == 0)
                    else:
                        self._setup_count += 1
                        require(self._negotiate_count == 1 and self._setup_count <= 8)
                return
            require(len(data) >= 116 and data[:4] == b'\xfdSMB')
            original_size,reserved,flags,session_id = struct.unpack_from('<IHHQ',data,36)
            require(original_size == len(data)-52 and reserved == 0 and flags == 1
                    and session_id == self._session_id and data[32:36] == b'\0'*4)


@contextmanager
def _quiet_protocol():
    # The worker is single-purpose. Protocol DEBUG output includes NTLM tokens;
    # suppress both existing module loggers and subsequently inherited levels.
    names = {'smbprotocol','spnego'} | {n for n in logging.Logger.manager.loggerDict
        if n.startswith(('smbprotocol.','spnego.'))}
    saved = []
    for name in names:
        logger = logging.getLogger(name)
        saved.append((logger,logger.level,logger.disabled))
        logger.setLevel(logging.CRITICAL+1); logger.disabled = True
    try: yield
    finally:
        for logger,level,disabled in saved:
            logger.setLevel(level); logger.disabled = disabled


def _connection_class(base, timeout):
    class RestrictedConnection(base):
        final_setup = None
        handshake_done = False

        def receive(self, request, wait=True, timeout=None, resolve_symlinks=False):
            remaining = self.operation_timeout if self.handshake_done else self.handshake_deadline-time.monotonic()
            require(remaining > 0)
            limit = min(remaining, self.operation_timeout,
                        self.operation_timeout if timeout is None else duration(timeout))
            result = super().receive(request,wait=wait,timeout=limit,resolve_symlinks=False)
            if result['command'].get_value() == 1 and result['status'].get_value() == 0:
                require(not self.handshake_done and self.final_setup is None)
                self.final_setup = result
            if result['command'].get_value() == 3 and result['status'].get_value() == 0:
                _disk_share_response(result['data'].get_value())
            return result
    RestrictedConnection.operation_timeout = timeout
    return RestrictedConnection


def _disk_share_response(data):
    # MS-SMB2 TREE_CONNECT Response: disk only, no DFS share flag/capability.
    # TreeConnect.is_dfs_share in the pinned library checks capabilities only.
    require(type(data) is bytes and len(data) == 16)
    size,share_type,reserved,flags,capabilities,_access = struct.unpack('<HBBIII',data)
    require(size == 16 and share_type == 1 and reserved == 0
            and flags & 3 == 0 and capabilities & 8 == 0)


def _negotiated(connection, value):
    require(connection.dialect == 0x311 and connection.cipher_id == 2
            and connection.preauth_integrity_hash_id == 1
            and connection.supports_encryption is True and connection.require_signing is True
            and connection.server_guid.hex == value['server_guid'])


def _authenticated(connection, session, value):
    _negotiated(connection,value)
    require(session.connection is connection and session._connected is True
            and session.require_encryption is True and session.encrypt_data is True
            and session.auth_protocol == 'ntlm' and session.username == value['username']
            and type(session.session_id) is int and 0 < session.session_id < 2**64
            and connection.session_table.get(session.session_id) is session)
    for field in ('session_key','signing_key','encryption_key','decryption_key'):
        key = getattr(session,field)
        require(type(key) is bytes and len(key) == 16 and key != b'\0'*16)


@contextmanager
def open_share(channel, value, context_sha, uid, credential_provider, *, timeout=30):
    """Own and close channel; yield only a verified encrypted non-DFS TreeConnect.

    Provider is called once after pinned negotiation, never on a retry. The
    unencrypted GUID is an endpoint check, not server authentication. Server
    authentication is only accepted after the final setup signature verifies.
    """
    connection = session = None
    guard = FrameGuard()
    try:
        value = dict(contract.validate(value,context_sha,uid))
        timeout = duration(timeout)
        require(channel.peer == (value['server_address'],445))
        channel.check()
        with _quiet_protocol(), bind_library(channel,guard) as module:
            from smbprotocol.session import Session
            from smbprotocol.tree import TreeConnect
            connection = _connection_class(module.Connection,timeout)(
                uuid.uuid4(),value['server_address'],port=445,require_signing=True)
            connection.client_capabilities = 0x44  # LARGE_MTU | ENCRYPTION; no DFS/multichannel
            connection._receive_timeout = timeout
            connection.handshake_deadline = time.monotonic()+timeout
            try:
                connection.connect(dialect=0x311,timeout=timeout,preferred_encryption_algos=[2])
                _negotiated(connection,value)
                channel.check()
                password = credential_provider()
                try:
                    require(type(password) is str and 0 < len(password.encode('utf-8')) <= 1024
                            and '\0' not in password)
                    session = Session(connection,username=value['username'],password=password,
                                      require_encryption=True,auth_protocol='ntlm')
                finally: password = None
                try: session.connect()
                finally: session.password = None
                _authenticated(connection,session,value)
                response = connection.final_setup
                require(response is not None and response['session_id'].get_value() == session.session_id
                        and response['flags'].get_value() & 8)
                connection.verify_signature(response,session.session_id,force=True)
                connection.final_setup = None  # No authentication response retained for consumers.
                connection.handshake_done = True
                guard.authenticated(session.session_id)
                tree = TreeConnect(session,'\\\\'+value['server_address']+'\\'+value['share'])
                tree.connect(require_secure_negotiate=True)
                require(tree._connected is True and tree.is_dfs_share is False
                        and type(tree.tree_connect_id) is int and 0 < tree.tree_connect_id < 2**32
                        and session.tree_connect_table.get(tree.tree_connect_id) is tree)
                _authenticated(connection,session,value)
                channel.check()
                yield tree
                _authenticated(connection,session,value)
                channel.check()
            finally:
                # Closing the TCP session releases server handles even after a
                # partial authentication. No extra logoff/retry can mask failure.
                try:
                    if connection.transport is not None: connection.disconnect(close=False)
                finally:
                    if session is not None:
                        session.password = None
                        for field in ('session_key','full_session_key','signing_key','application_key',
                                      'encryption_key','decryption_key'):
                            setattr(session,field,None)
    except Exception:
        raise ValueError(ERROR) from None
    finally:
        guard.close()
        channel.close()
