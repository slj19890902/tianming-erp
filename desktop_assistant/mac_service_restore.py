"""Full stopped restore under the dedicated UID, with immutable native binding.

No root application execution, migration, reader activation, service start or
factory takeover. Input ciphertext is prepared separately by administrator OS
file tools; this module never repairs its protection or widens a personal home.
"""
from contextlib import contextmanager, ExitStack
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import warnings

from app.core import mac_service_keychain as keychain
from desktop_assistant import mac_keychain_install as bootstrap
from desktop_assistant import mac_published_runtime as published
from desktop_assistant import mac_release_publish as publication
from desktop_assistant import restore_provenance as history
from desktop_assistant import delivery_dispatch_contract as dispatch
from desktop_assistant import order_inventory_contract as inventory
from desktop_assistant.reader_contract import target_readers
from desktop_assistant.release_request import inspect_package, public_identity
from desktop_assistant.schema_contract import schema_contract_from_signed_release, validate_database_schema
from desktop_assistant.storage import authenticate_stream, sha

BASE = Path('/Library/TianmingERP')
ROOT = BASE / 'instance'
DATA_VOLUME = Path('/System/Volumes/Data')
ENTRY = 'desktop_assistant/service_restore_entry.py'
MODULE = 'desktop_assistant/mac_service_restore.py'


def _hash(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value)


def _isolated():
    return sys.flags.isolated and sys.dont_write_bytecode


def runtime_identity():
    if sys.platform != 'darwin' or not _isolated() or not keychain.selected():
        raise ValueError('不是隔离解释器中的专用恢复身份')
    context = keychain.load_context()
    actual = bootstrap._actual()
    if (any(actual[k] != context['uid'] for k in ('uid', 'euid'))
            or any(actual[k] != context['gid'] for k in ('gid', 'egid'))
            or not actual['kernel_groups'] or not set(actual['kernel_groups']) <= {context['gid'], 12, 61}):
        raise ValueError('专用恢复实际UID/GID/内核组不符')
    return context


def native_target(manager, identity):
    """Called inside Manager's maintenance lock, before any credential writes."""
    context = runtime_identity()
    if manager.root != ROOT or not _hash(identity):
        raise ValueError('原生恢复必须绑定固定本机实例及批准程序')
    tree, manifest = published.resolve(ROOT, identity, manager.public_key, kind='migration')
    publication.require_service_restore(manifest)
    if Path(__file__).resolve() != tree / MODULE or Path(sys.executable).resolve() != tree / 'runtime/bin/python3.12':
        raise ValueError('恢复核心不是正在执行的root批准原生程序')
    publication._no_service()
    package = tree.parent / 'package.zip'
    _, verified = inspect_package(package, manager.public_key)
    modeled, contract = schema_contract_from_signed_release(package, manager.public_key)
    if verified != manifest or modeled != manifest or sha(package) != identity:
        raise ValueError('原生目标签名、结构契约或原包身份不一致')
    return {'id': identity, 'package': package, 'manifest': manifest, 'contract': contract, 'public': manager.public_key,
            'context_sha256': bootstrap.digest(published._read(keychain.CONTEXT)),
            'context': context}


def validate_binding(target, payload, metadata, source, info, inventory_record, dispatch_record, provenance):
    database = payload / 'shared/data/carton_erp.sqlite3'
    manifest = target['manifest']
    if manifest['revision'] != info['revision'] or target['contract']['revision'] != info['revision']:
        raise ValueError('原生绑定只允许完整恢复库的同一revision，不执行迁移')
    validate_database_schema(database, target['contract'])
    target_readers(database, source, manifest, inventory_record, dispatch_record, factory=False)
    # Current managed server entry requires a real dispatch marker, even when
    # the source was a valid older backup. Never fabricate an activation here.
    if not dispatch.signed_capability(manifest):
        raise ValueError('原生程序缺少发货保护能力')
    restored_dispatch = dispatch.validate_recovery(payload / 'shared', metadata, manifest)
    dispatch.inspect_activation(payload / 'shared',
        {dispatch.STATE_KEY: dispatch.state_index(restored_dispatch)} if restored_dispatch else {}, require_active=True)
    if inventory.signed_capability(manifest) and inventory_record is None:
        raise ValueError('原生目标不能隐式启用来源未声明的库存reader')
    if not provenance:
        provenance = {history.RELEASE: metadata['release'], history.AUTHORITY: metadata.get('schema_authority')}
        history.identities(provenance)
        paths = {history.MEMBERS[history.RELEASE]: payload / 'release.zip'}
        if provenance[history.AUTHORITY] is not None:
            paths[history.MEMBERS[history.AUTHORITY]] = payload / 'compatibility.zip'
        history.verify(provenance, paths, target['public'])
    # Recovery ZIPs have no entries for empty directories. Fill only the fixed
    # managed directories in the isolated payload, never in the final instance.
    for name in publication.MANAGED.values():
        path = payload / 'shared' / name
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise ValueError('恢复共享目录含非目录对象')
        if not path.exists(): path.mkdir(mode=0o700)
    return provenance


def _fingerprint(info):
    return tuple(getattr(info, key) for key in ('st_dev', 'st_ino', 'st_uid', 'st_gid', 'st_mode',
                                               'st_nlink', 'st_size', 'st_mtime_ns', 'st_ctime_ns'))


def _owned(info, uid, gid):
    return info.st_uid == uid and (gid is None or info.st_gid == gid)


class Lease:
    """An opened FD and its parent-relative name; no FIFO/blocking file opens."""
    def __init__(self, path, *, uid, gid, mode, directory=False, parent=None, fd=None, mutable=False):
        self.path, self.parent = path, parent
        self.uid, self.gid, self.mode, self.directory = uid, gid, mode, directory
        self.mutable = mutable
        self.device = DATA_VOLUME.stat().st_dev
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | (os.O_DIRECTORY if directory else 0)
        self.fd = fd if fd is not None else os.open(path.name if parent else path, flags,
                                                      dir_fd=parent.fd if parent else None)
        try:
            self.initial = _fingerprint(os.fstat(self.fd))
            self.check()
        except BaseException:
            os.close(self.fd)
            raise

    def check(self, *, empty=False):
        if self.parent: self.parent.check()
        info = os.fstat(self.fd)
        named = os.stat(self.path.name if self.parent else self.path,
                        dir_fd=self.parent.fd if self.parent else None, follow_symlinks=False)
        kind = stat.S_ISDIR if self.directory else stat.S_ISREG
        if (not kind(info.st_mode) or not _owned(info, self.uid, self.gid)
                or stat.S_IMODE(info.st_mode) != self.mode or info.st_dev != self.device
                or (not self.directory and info.st_nlink != 1)
                or _fingerprint(info) != _fingerprint(named)
                or (not self.mutable and _fingerprint(info) != self.initial)):
            raise ValueError('专用恢复路径保护、设备或文件身份变化')
        keychain._no_acl(self.fd)
        if empty and os.listdir(self.fd): raise ValueError('专用恢复实例必须完全为空')
        return info

    def __enter__(self): return self

    def __exit__(self, exc_type, *_):
        try:
            if exc_type is None: self.check()
        finally: os.close(self.fd)


@contextmanager
def restore_lock(home, context):
    import fcntl
    name = 'service-restore.lock'
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        fd = os.open(name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=home.fd)
    except FileExistsError:
        fd = os.open(name, flags, dir_fd=home.fd)
    else:
        try:
            os.write(fd, b'0'); os.fsync(fd); os.fsync(home.fd)
        except BaseException:
            os.close(fd); raise
    with Lease(home.path/name, uid=context['uid'], gid=context['gid'], mode=0o600, parent=home, fd=fd) as lock:
        os.lseek(fd, 0, os.SEEK_SET)
        if os.fstat(fd).st_size != 1 or os.read(fd, 1) != b'0':
            raise ValueError('专用恢复锁不是已有完整锁文件')
        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            from desktop_assistant.operation_lock import BUSY
            raise ValueError(BUSY) from None
        try:
            lock.check()
            yield lock
            lock.check()
        finally: fcntl.flock(fd, fcntl.LOCK_UN)


def _digest(fd):
    os.lseek(fd, 0, os.SEEK_SET)
    result = hashlib.sha256()
    while block := os.read(fd, 1024**2): result.update(block)
    return result.hexdigest()


def _terminal():
    return all(stream.isatty() for stream in (sys.stdin, sys.stdout, sys.stderr))


def _read_complete(lease, limit):
    size = lease.check().st_size
    if not 0 < size <= limit: raise ValueError('专用恢复文件长度超限')
    os.lseek(lease.fd, 0, os.SEEK_SET)
    chunks, count = [], 0
    while block := os.read(lease.fd, min(65536, size + 1 - count)):
        chunks.append(block); count += len(block)
        if count > size: raise ValueError('专用恢复文件长度变化')
    lease.check()
    if count != size: raise ValueError('专用恢复文件读取不完整')
    return b''.join(chunks)


def _receipt(home, name, raw, context, recheck):
    fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK,
                 0o600, dir_fd=home.fd)
    with Lease(home.path/name, uid=context['uid'], gid=context['gid'], mode=0o600,
               parent=home, fd=fd, mutable=True) as result:
        with os.fdopen(os.dup(fd), 'wb') as out:
            out.write(raw); out.flush(); os.fsync(out.fileno())
        result.check()
        if _read_complete(result, len(raw)) != raw: raise ValueError('恢复回执原字节回读不一致')
        result.initial = _fingerprint(os.fstat(fd)); result.mutable = False
        os.fsync(home.fd)
        recheck(); result.check()


def run(args, *, script, executable, prompt=getpass.getpass):
    if not _terminal(): raise ValueError('恢复要求三路本机终端，口令不接受文件或管道')
    if (not isinstance(args.input_attempt, str) or not re.fullmatch('[0-9a-f]{32}', args.input_attempt)
            or any(not _hash(v) for v in (args.restore_package, args.backup_sha256, args.publisher_sha256))
            or (args.credentials_sha256 is not None and not _hash(args.credentials_sha256))
            or (args.backup_password_from_transfer and args.credentials_sha256 is None)):
        raise ValueError('专用恢复参数无效')
    context = runtime_identity()
    public = published._read(published.PUBLIC)
    if public_identity(public)[1] != args.publisher_sha256: raise ValueError('恢复发布公钥身份不符')
    tree, manifest = published.resolve(ROOT, args.restore_package, public, kind='migration')
    publication.require_service_restore(manifest)
    if (Path(script).resolve() != tree/ENTRY or Path(__file__).resolve() != tree/MODULE
            or Path(executable).resolve() != tree/'runtime/bin/python3.12'
            or Path(sys.executable).resolve() != tree/'runtime/bin/python3.12'):
        raise ValueError('专用恢复未执行批准的签名入口或解释器')
    publication._no_service()
    from app.core.home_rehearsal import install_network_guard
    os.environ['ERP_HOME_REHEARSAL'] = '1'
    install_network_guard()
    old_umask = os.umask(0o077)
    try:
        with ExitStack() as stack:
            base = stack.enter_context(Lease(BASE, uid=0, gid=None, mode=0o755, directory=True))
            home = stack.enter_context(Lease(BASE/'home', uid=context['uid'], gid=context['gid'], mode=0o700,
                                            directory=True, parent=base, mutable=True))
            lock = stack.enter_context(restore_lock(home, context))
            receipt_name = 'service-restore-'+args.input_attempt+'.json'
            try: os.stat(receipt_name, dir_fd=home.fd, follow_symlinks=False)
            except FileNotFoundError: pass
            else: raise ValueError('已有本次恢复回执或现场，保留并拒绝重复恢复')
            root = stack.enter_context(Lease(ROOT, uid=context['uid'], gid=context['gid'], mode=0o700,
                                            directory=True, parent=base))
            root.check(empty=True)
            inputs = stack.enter_context(Lease(BASE/'restore-inputs', uid=0, gid=context['gid'], mode=0o750,
                                              directory=True, parent=base))
            attempt = stack.enter_context(Lease(inputs.path/args.input_attempt, uid=0, gid=context['gid'],
                                               mode=0o750, directory=True, parent=inputs))
            expected = {'complete.tmbackup'} | ({'credentials.tmencrypted'} if args.credentials_sha256 else set())
            if set(os.listdir(attempt.fd)) != expected: raise ValueError('恢复输入清单不一致')
            backup = stack.enter_context(Lease(attempt.path/'complete.tmbackup', uid=0, gid=context['gid'],
                                              mode=0o440, parent=attempt))
            if _digest(backup.fd) != args.backup_sha256: raise ValueError('批准备份摘要不符')
            transfer = None
            raw = None
            if args.credentials_sha256:
                from desktop_assistant.credential_transfer import LIMIT
                transfer = stack.enter_context(Lease(attempt.path/'credentials.tmencrypted', uid=0,
                    gid=context['gid'], mode=0o440, parent=attempt))
                raw = _read_complete(transfer, LIMIT)
                if hashlib.sha256(raw).hexdigest() != args.credentials_sha256:
                    raise ValueError('凭据加密原件长度或批准摘要不符')
            def recheck():
                lock.check(); root.check(empty=not root.mutable); backup.check()
                if transfer: transfer.check()
            recheck()
            from desktop_assistant.mac_system_service import verify_keychain
            verify_keychain(context)
            def hidden(message):
                with warnings.catch_warnings():
                    warnings.simplefilter('error', getpass.GetPassWarning)
                    return prompt(message)
            transfer_password = None
            if raw is not None:
                from desktop_assistant.credential_transfer import unseal
                transfer_password = hidden('凭据移交口令（隐藏输入）：')
                record = unseal(raw, transfer_password)
                if record['bound_backup_sha256'] != args.backup_sha256:
                    raise ValueError('凭据包不属于本次完整备份')
            if args.backup_password_from_transfer:
                password = record['entries']['backup']['value']
            else: password = hidden('完整备份恢复口令（隐藏输入）：')
            recheck()
            with os.fdopen(os.dup(backup.fd), 'rb') as stream:
                if authenticate_stream(stream, password) != args.backup_sha256:
                    raise ValueError('实际认证备份摘要不符')
            recheck(); publication._no_service()
            from desktop_assistant.manager import Manager
            root.mutable = True
            manager = Manager(ROOT, public)
            result = manager.restore(backup.path, password, credentials=raw, credential_password=transfer_password,
                                     expected_backup_sha256=args.backup_sha256, native_package=args.restore_package)
            recheck()
            state = manager.state
            if (state.get('current') != args.restore_package or state.get('manual_stop') is not True
                    or state.get('restored_backup_sha256') != args.backup_sha256 or result['started'] is not False
                    or result.get('credentials', {}).get('status') != 'restored'):
                raise ValueError('恢复最终状态不属于批准输入，未发布完成回执')
            receipt = {'type': 'tianming.mac-service-restore.v1', 'backup_sha256': args.backup_sha256,
                       'package_sha256': args.restore_package, 'publisher_sha256': args.publisher_sha256,
                       'credentials_sha256': args.credentials_sha256, 'started': False, 'factory_cutover': False,
                       'manual_stop': True, 'native_binding': state['native_restore_binding'], 'result': result}
            _receipt(home, receipt_name, bootstrap.canonical(receipt), context, recheck)
            recheck()
            return receipt
    finally: os.umask(old_umask)


def main(*, script, executable):
    from desktop_assistant.restore_cli import SafeParser
    parser = SafeParser(description='专用账号完整恢复；保持停止及家庭隔离', allow_abbrev=False)
    for name in ('input-attempt', 'restore-package', 'backup-sha256', 'publisher-sha256'):
        parser.add_argument('--'+name, required=True)
    parser.add_argument('--credentials-sha256')
    parser.add_argument('--backup-password-from-transfer', action='store_true')
    args = parser.parse_args()
    try: receipt = run(args, script=script, executable=executable)
    except (Exception, KeyboardInterrupt):
        raise SystemExit('专用恢复未确认完成；未启动ERP。保留输入与恢复现场，勿覆盖重试；不输出秘密或异常详情。') from None
    print(json.dumps(receipt, ensure_ascii=False))
