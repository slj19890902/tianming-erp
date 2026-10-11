"""Authenticate an already stopped final backup under the existing source lock.

No backup, restore, stop/start, credential rewrap or signing takes place here.
The yielded evidence is usable only within the context: the maintenance lock
and Windows database read lease remain held. Private staging has already passed
its final ACL check before yielding. This is not proof of physical isolation.
"""
from contextlib import contextmanager, closing, ExitStack
from datetime import datetime, timezone
import hmac
import json
from pathlib import Path
import sqlite3
import sys

from desktop_assistant import factory_cutover as protocol
from desktop_assistant import credential_transfer as credentials
from desktop_assistant import delivery_dispatch_contract as dispatch
from desktop_assistant import order_inventory_contract as inventory
from desktop_assistant.cleanup import assert_idle, local_path, is_link
from desktop_assistant.manager import Manager
from desktop_assistant.reader_contract import (TABLE_READERS, capabilities as _capabilities,
                                              target_readers as _target_readers)
from desktop_assistant.migration import files
from desktop_assistant.operation_lock import operation_lock
from desktop_assistant.release_request import inspect_package, public_identity
from desktop_assistant.retention import archive_synced
from desktop_assistant.runtime_platform import runtime_relative
from desktop_assistant.schema_contract import schema_contract_from_signed_release, validate_database_schema
from desktop_assistant.storage import archive_path, database_info, decrypt_file, extract_verified, sha
from desktop_assistant.windows_private_workspace import private_workspace, read_file_lease


ERROR = '最终备份与已停写来源不一致，未签发；保持原端停止'
# Unknown or unsuccessful operations require separate recovery investigation.
STABLE_OPERATIONS = {None, 'import_existing', 'update', 'rollback', 'migration_recovered', 'restore'}

class _SourceReader(Manager):
    """Reuse process identity checks without a directory-creating constructor."""
    def __init__(self, root, public):
        self.root, self.public_key = root, public


def _require_windows():
    if sys.platform != 'win32':
        raise ValueError('最终来源审计只支持原Windows服务用户')


def _json(path, limit=16 * 1024**2):
    path = local_path(path)
    if not path.is_file() or path.stat().st_size > limit:
        raise ValueError(ERROR)
    raw = path.read_bytes()
    if len(raw) > limit:
        raise ValueError(ERROR)
    value = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=protocol._unique)
    if not isinstance(value, dict):
        raise ValueError(ERROR)
    return value


def _archive(path):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError(ERROR)
    for component in (path, *path.parents):
        if is_link(component):
            raise ValueError(ERROR)
    return archive_path(path)


def _no_journal(shared):
    database = shared / 'data/carton_erp.sqlite3'
    if any(Path(str(database) + suffix).exists() for suffix in ('-wal', '-shm', '-journal')):
        raise ValueError('最终备份拒绝任何数据库旁路日志；不自动删除或checkpoint')


def _preflight(root):
    if not Path(root).is_absolute():
        raise ValueError(ERROR)
    root = local_path(root)
    for relative in ('', 'releases', 'shared', 'control', 'backups', 'packages', 'staging',
                     'shared/data', 'control/backup-receipts'):
        if not local_path(root / relative).is_dir():
            raise ValueError('已有来源安装不完整，不补建目录')
    for relative in ('state.json', 'shared/data/carton_erp.sqlite3', 'control/operation.lock'):
        if not local_path(root / relative).is_file():
            raise ValueError('已有来源文件不完整，不构造空安装')
    if (root / 'control/operation.lock').stat().st_size < 1:
        raise ValueError('维护锁文件未初始化，不在审计中创建')
    return root


def _stopped(manager):
    _preflight(manager.root)
    state = _json(manager.root / 'state.json')
    if (state.get('manual_stop') is not True or state.get('onboarding_pending')
            or state.get('backup_error') or state.get('operation') not in STABLE_OPERATIONS):
        raise ValueError('来源未明确停止或仍有失败状态，未签发')
    protocol._hash(state.get('current'))
    local_path(manager.root / 'control/process.json')
    if manager._process() is not None:
        raise ValueError('来源ERP仍在运行，未签发')
    assert_idle(manager.root)
    _no_journal(manager.root / 'shared')
    return state


def _config_hashes(root):
    result = {}
    for name, _ in credentials.CONFIGS.values():
        path = local_path(root / name)
        result[name] = sha(path) if path.exists() else None
    return result


def _created(value, request, now):
    try:
        when = datetime.fromisoformat(value)
        if when.utcoffset() is None:
            raise ValueError()
        if not protocol._time(request['created_at']) <= when <= now < protocol._time(request['expires_at']):
            raise ValueError()
        return protocol._stamp(when)
    except (TypeError, ValueError):
        raise ValueError('认证最终备份时间不在申请之后和本次核验之前') from None


def _same_credentials(embedded, fresh, password):
    credentials._validate(embedded)
    credentials._validate(fresh)
    if (embedded['source_platform'] != 'win32' or fresh['source_platform'] != 'win32'
            or embedded['bound_backup_sha256'] is not None or fresh['bound_backup_sha256'] is not None
            or 'backup' not in embedded['entries']
            or not hmac.compare_digest(protocol.canonical(embedded), protocol.canonical(fresh))
            or not hmac.compare_digest(embedded['entries']['backup']['value'].encode(), password.encode())):
        raise ValueError('最终备份内运营凭据与原用户当前配置不一致')


@contextmanager
def verified_stopped_backup(root, backup, expected_backup_sha256, password, request,
                            target_package, public, private_parent, *, now=None):
    """Yield nonsecret evidence only after authenticated original-byte checks.

    Caller must separately authorize cutover and inspect/verify final output.
    No call to this audit alone permits target activation or old-host retirement.
    """
    _require_windows()
    root = _preflight(root)  # Must precede Manager's directory-creating constructor.
    request = json.loads(protocol.canonical(protocol.validate_request(request)))
    protocol._hash(expected_backup_sha256)
    if public_identity(public)[1] != request['publisher_sha256']:
        raise ValueError('目标申请不是既有发布身份')
    current_time = now if now is not None else datetime.now(timezone.utc)
    if current_time.utcoffset() is None:
        raise ValueError('本次核验时间必须包含时区')
    if not protocol._time(request['created_at']) <= current_time < protocol._time(request['expires_at']):
        raise ValueError('目标申请尚未生效或已过期')
    backup = _archive(backup)
    target_package = local_path(target_package)
    private_parent = local_path(private_parent)
    if private_parent == root or root in private_parent.parents:
        raise ValueError('私有审计目录不能位于正式来源安装内')
    with operation_lock(root / 'control/operation.lock', existing_only=True), ExitStack() as leases:
        leases.enter_context(read_file_lease(root / 'shared/data/carton_erp.sqlite3'))
        leases.enter_context(read_file_lease(root / 'state.json'))
        manager = _SourceReader(root, public)
        state = _stopped(manager)
        state_sha = sha(root / 'state.json')
        if backup != _archive(state.get('last_backup', '')):
            raise ValueError(ERROR)
        receipt_path = local_path(root / 'control/backup-receipts' / (backup.stem + '.json'))
        leases.enter_context(read_file_lease(receipt_path))
        receipt = _json(receipt_path)
        receipt_sha = sha(receipt_path)
        if (set(receipt) != {'path', 'sha256', 'size', 'verified', 'storage', 'database'}
                or receipt['verified'] is not True or receipt['storage'] != 'nas'
                or type(receipt['size']) is not int or receipt['size'] <= 0
                or _archive(receipt['path']) != backup
                or receipt['size'] != backup.stat().st_size
                or receipt['sha256'] != expected_backup_sha256 or sha(backup) != expected_backup_sha256
                or not archive_synced(backup)):
            raise ValueError('最新NAS完整备份回执、独立摘要或上传状态不符')
        source_package = local_path(root / 'packages' / (state['current'] + '.zip'))
        # Pin all inputs consumed by separate digest/ZIP/model readers. These
        # are local reviewed packages, never a mutable mapped NAS input.
        leases.enter_context(read_file_lease(source_package))
        leases.enter_context(read_file_lease(target_package))
        leases.enter_context(read_file_lease(root / 'releases' / state['current'] / 'manifest.json'))
        if sha(source_package) != state['current'] or sha(target_package) != request['package_sha256']:
            raise ValueError(ERROR)
        _, source_manifest = inspect_package(source_package, public)
        _, target_manifest = inspect_package(target_package, public)
        if (source_manifest != _json(root / 'releases' / state['current'] / 'manifest.json')
                or source_manifest.get('runtime_platform', 'windows') != 'windows'
                or target_manifest.get('runtime_platform') != 'macos-arm64'
                or not {runtime_relative(target_manifest), 'main.py', 'app/main.py',
                        'desktop_assistant/server_entry.py'} <= set(target_manifest['files'])):
            raise ValueError('来源或目标签名程序平台/缓存不一致')
        source_files = files(root / 'shared')
        source_database_sha = sha(root / 'shared/data/carton_erp.sqlite3')
        for name, _ in credentials.CONFIGS.values():
            config_path = local_path(root / name)
            if config_path.exists():
                leases.enter_context(read_file_lease(config_path))
        config_hashes = _config_hashes(root)
        inventory_record = inventory.for_backup(state, source_manifest)
        with private_workspace(private_parent) as work:
            raw = work / 'recovery.zip'
            authenticated = decrypt_file(backup, raw, password)
            if authenticated != expected_backup_sha256 or sha(backup) != authenticated:
                raise ValueError(ERROR)
            payload = work / 'payload'
            manifest = extract_verified(raw, payload)
            if (manifest.get('type') != 'tianming.recovery.v1' or manifest.get('release') != state['current']
                    or manifest.get('version') != source_manifest['version']
                    or manifest.get('database') != receipt['database'] or manifest.get('credential_transfer') != 'credentials.tmencrypted'
                    or local_path(manifest.get('source_shared', '')) != root / 'shared'):
                raise ValueError(ERROR)
            created = _created(manifest.get('created'), request, current_time)
            restored = payload / 'shared'
            _no_journal(restored)
            if (sha(restored / 'data/carton_erp.sqlite3') != source_database_sha or files(restored) != source_files
                    or sha(payload / 'release.zip') != state['current']):
                raise ValueError(ERROR)
            # Every structural/business SQL query uses this authenticated,
            # byte-identical private copy, never a WAL-aware source connection.
            info = database_info(restored / 'data/carton_erp.sqlite3')
            if info != receipt['database']:
                raise ValueError(ERROR)
            source_snapshot = protocol.capture_snapshot(restored, recorded_root=root / 'shared')
            dispatch_record = dispatch.inspect_activation(restored, state,
                require_active=dispatch.signed_capability(source_manifest))
            _, contract = schema_contract_from_signed_release(payload / 'release.zip', public)
            expected_members = {'release.zip', 'credentials.tmencrypted'}
            authority = None
            if source_manifest['revision'] != info['revision']:
                authority = state.get('schema_authority')
                protocol._hash(authority)
                authority_source = local_path(root / 'packages' / (authority + '.zip'))
                leases.enter_context(read_file_lease(authority_source))
                if (sha(authority_source) != authority or sha(payload / 'compatibility.zip') != authority):
                    raise ValueError(ERROR)
                _, authority_manifest = inspect_package(payload / 'compatibility.zip', public)
                policy = authority_manifest.get('migration') or {}
                if (authority_manifest['revision'] != info['revision']
                        or policy.get('policy') != 'preserve_existing_facts_v1'
                        or policy.get('rollback_package_sha256') != state['current']):
                    raise ValueError(ERROR)
                _, contract = schema_contract_from_signed_release(payload / 'compatibility.zip', public)
                expected_members.add('compatibility.zip')
            if (manifest.get('schema_authority') != authority
                    or {name for name in manifest['files'] if not name.startswith('shared/')} != expected_members):
                raise ValueError(ERROR)
            validate_database_schema(restored / 'data/carton_erp.sqlite3', contract)
            _, target_contract = schema_contract_from_signed_release(target_package, public)
            if target_manifest['revision'] != info['revision']:
                raise ValueError('本次切换目标必须原版本读取最终事实，不隐含数据库迁移')
            validate_database_schema(restored / 'data/carton_erp.sqlite3', target_contract)
            if (manifest.get(inventory.STATE_KEY) != inventory_record
                    or dispatch.validate_recovery(restored, manifest, source_manifest) != dispatch_record):
                raise ValueError(ERROR)
            recovered_inventory = inventory.for_recovery(manifest, source_manifest, authenticated)
            _target_readers(restored / 'data/carton_erp.sqlite3', source_manifest, source_manifest,
                            recovered_inventory, dispatch_record, factory=False)
            _target_readers(restored / 'data/carton_erp.sqlite3', source_manifest, target_manifest,
                            recovered_inventory, dispatch_record)
            embedded = credentials.unseal(credentials._read(payload / 'credentials.tmencrypted', credentials.LIMIT), password)
            fresh = credentials.collect(root, immutable_database=True)
            _same_credentials(embedded, fresh, password)
            purposes = sorted(embedded['entries'])
            del fresh
        # The final private DACL check has now succeeded. No caller can load a
        # signing identity or publish an authorization before this point.
        if (_stopped(manager) != state or sha(root / 'state.json') != state_sha
                or sha(receipt_path) != receipt_sha or _config_hashes(root) != config_hashes
                or files(root / 'shared') != source_files
                or sha(root / 'shared/data/carton_erp.sqlite3') != source_database_sha
                or sha(source_package) != state['current'] or sha(target_package) != request['package_sha256']
                or sha(backup) != expected_backup_sha256 or not archive_synced(backup)):
            raise ValueError('核验期间来源、备份或目标包变化，保持停止')
        _same_credentials(embedded, credentials.collect(root, immutable_database=True), password)
        del embedded
        finished_time = now if now is not None else datetime.now(timezone.utc)
        if finished_time >= protocol._time(request['expires_at']):
            raise ValueError('审计完成时目标申请已过期，未签发')
        evidence = {'source_sha256': protocol.digest({'root': str(root), 'state_sha256': state_sha,
                    'receipt_sha256': receipt_sha, 'publisher_sha256': request['publisher_sha256']}),
                    'backup_sha256': expected_backup_sha256, 'source_package_sha256': state['current'],
                    'backup_created_at': created, 'manual_stop': True, 'process_stopped': True,
                    'credential_purposes': purposes, 'snapshot': source_snapshot}
        protocol._evidence(evidence)
        yield evidence
