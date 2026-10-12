"""Authenticate a fixed historical backup as the original stopped Windows user.

This independent v2 entry never stops, restores, exports, rewraps or signs. It
retains the original maintenance/read leases through the caller's signing scope.
Only an authenticated, byte-identical source with its original NAS receipt and
external credential envelope may be retained; last_backup is not an authority
for which historical snapshot was approved. No physical isolation is inferred.
"""
from contextlib import contextmanager, closing, ExitStack
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from desktop_assistant import factory_source_audit as original
from desktop_assistant import factory_cutover as facts
from desktop_assistant import retained_snapshot_contract as contract
from desktop_assistant import credential_transfer as credentials, credential_restore
from desktop_assistant import delivery_dispatch_contract as dispatch
from desktop_assistant import order_inventory_contract as inventory
from desktop_assistant import restore_provenance as history
from desktop_assistant.cleanup import local_path
from desktop_assistant.migration import files
from desktop_assistant.operation_lock import operation_lock
from desktop_assistant.reader_contract import target_readers
from desktop_assistant.release_request import inspect_package, public_identity
from desktop_assistant.retention import archive_synced
from desktop_assistant.runtime_platform import runtime_relative
from desktop_assistant.schema_contract import schema_contract_from_signed_release, validate_database_schema
from desktop_assistant.storage import database_info, decrypt_file, extract_verified, sha
from desktop_assistant.windows_private_workspace import private_workspace, read_file_lease

ERROR = '历史完整备份与原用户已停写来源不一致，未签发；保留全部现场'


def _now(now):
    current = now if now is not None else datetime.now(timezone.utc)
    if not isinstance(current, datetime) or current.utcoffset() is None:
        raise ValueError('本次来源核验时间必须包含时区')
    return current.astimezone(timezone.utc)


def _created(value, retained, current):
    try:
        when = datetime.fromisoformat(value)
        if (when.utcoffset() is None or when > current
                or contract._stamp(when) != retained['backup_created_at']):
            raise ValueError()
    except (TypeError, ValueError):
        raise ValueError('认证历史备份时间与本次明确保留的原日期不一致') from None
    return retained['backup_created_at']


def _same_external(record, root, password, expected_backup_sha256):
    credentials._validate(record)
    if record['bound_backup_sha256'] != expected_backup_sha256:
        raise ValueError(ERROR)
    # Only the already-authenticated external binding is normalized. Full
    # config text, protected identities, versions, purposes and values remain.
    normalized = deepcopy(record)
    normalized['bound_backup_sha256'] = None
    original._same_credentials(normalized, credentials.collect(root, immutable_database=True), password)


@contextmanager
def verified_stopped_retained_backup(root, backup, expected_backup_sha256, credentials_path,
                                     transfer_password, request, target_package, public,
                                     private_parent, *, now=None):
    """Yield ``(evidence, recheck)`` only after the final private DACL check.

    ``recheck()`` is valid only while this context is active. Call it immediately
    before/after signing and after output verification; a final normal-exit
    recheck also prevents callers from overlooking a late mutation. Failure
    retains all staging. Windows-native leases/DPAPI remain mandatory in use.
    """
    original._require_windows()
    root = original._preflight(root)
    request = contract.decode(contract.canonical(contract.validate_request(request)))
    retained = request['retained_snapshot']
    contract._hash(expected_backup_sha256)
    if (expected_backup_sha256 != retained['backup_sha256']
            or public_identity(public)[1] != request['publisher_sha256']):
        raise ValueError('历史备份或公钥不等于本次明确批准的申请')
    audit_started = _now(now)
    if not contract._time(request['created_at']) <= audit_started < contract._time(request['expires_at']):
        raise ValueError('保留副本申请尚未生效或已过期')
    if any(not Path(path).is_absolute() for path in (backup, credentials_path, target_package, private_parent)):
        raise ValueError('历史审计输入必须为明确绝对路径')
    backup = original._archive(backup)
    credentials_path = local_path(credentials_path)
    target_package = local_path(target_package)
    private_parent = local_path(private_parent)
    if private_parent == root or root in private_parent.parents:
        raise ValueError('私有审计暂存不能位于正式来源安装内')
    with operation_lock(root / 'control/operation.lock', existing_only=True), ExitStack() as leases:
        pinned = {}

        def pin(path):
            path = local_path(path)
            if path not in pinned:
                leases.enter_context(read_file_lease(path))
                pinned[path] = sha(path)
            return pinned[path]

        database = root / 'shared/data/carton_erp.sqlite3'
        source_database_sha = pin(database)
        state_sha = pin(root / 'state.json')
        manager = original._SourceReader(root, public)
        state = original._stopped(manager)
        process_path = local_path(root / 'control/process.json')
        process_sha = pin(process_path) if process_path.exists() else None
        receipt_path = local_path(root / 'control/backup-receipts' / (backup.stem + '.json'))
        receipt_sha = pin(receipt_path)
        receipt = original._json(receipt_path)

        def check_archive():
            if (set(receipt) != {'path', 'sha256', 'size', 'verified', 'storage', 'database'}
                    or receipt['verified'] is not True or receipt['storage'] != 'nas'
                    or type(receipt['size']) is not int or receipt['size'] <= 0
                    or original._archive(receipt['path']) != backup
                    or receipt['size'] != backup.stat().st_size
                    or receipt['sha256'] != expected_backup_sha256
                    or sha(backup) != expected_backup_sha256 or not archive_synced(backup)):
                raise ValueError('原历史NAS回执、完整密文摘要或上传状态未通过独立验证')

        check_archive()
        if pin(credentials_path) != retained['credentials_sha256']:
            raise ValueError('原外部凭据密文不等于本次明确批准的摘要')
        raw_credentials = credentials._read(credentials_path, credentials.LIMIT)
        if hashlib.sha256(raw_credentials).hexdigest() != retained['credentials_sha256']:
            raise ValueError(ERROR)
        record = credentials.unseal(raw_credentials, transfer_password)
        if record['bound_backup_sha256'] != expected_backup_sha256 or 'backup' not in record['entries']:
            raise ValueError('外部凭据没有绑定原备份或缺少原备份口令')
        password = record['entries']['backup']['value']
        source_package = local_path(root / 'packages' / (state['current'] + '.zip'))
        cached_manifest = local_path(root / 'releases' / state['current'] / 'manifest.json')
        if pin(source_package) != state['current'] or pin(target_package) != request['package_sha256']:
            raise ValueError(ERROR)
        pin(cached_manifest)
        _, source_manifest = inspect_package(source_package, public)
        _, target_manifest = inspect_package(target_package, public)
        if (source_manifest != original._json(cached_manifest)
                or source_manifest.get('runtime_platform', 'windows') != 'windows'
                or target_manifest.get('runtime_platform') != 'macos-arm64'
                or not {runtime_relative(target_manifest), 'main.py', 'app/main.py',
                        'desktop_assistant/server_entry.py'} <= set(target_manifest['files'])):
            raise ValueError('原来源或目标完整签名程序、平台或缓存不一致')
        source_files = files(root / 'shared')
        environment_sha = sha(root / 'shared/environment.json')
        environment_raw = (root / 'shared/environment.json').read_bytes()
        environment = json.loads(environment_raw, object_pairs_hook=facts._unique)
        if (hashlib.sha256(environment_raw).hexdigest() != environment_sha
                or not isinstance(environment, dict)
                or any(not isinstance(k, str) or not isinstance(v, str) for k, v in environment.items())):
            raise ValueError(ERROR)
        for name, _ in credentials.CONFIGS.values():
            config_path = local_path(root / name)
            if config_path.exists():
                pin(config_path)
        config_hashes = original._config_hashes(root)
        inventory_record = inventory.for_backup(state, source_manifest)
        provenance = history.identities(state)
        for identity in provenance.values():
            if identity is not None:
                pin(root / 'packages' / (identity + '.zip'))
        expected_provenance, _ = history.for_backup(root, state, source_manifest, public)
        with private_workspace(private_parent) as work:
            raw = work / 'recovery.zip'
            authenticated = decrypt_file(backup, raw, password)
            if authenticated != expected_backup_sha256 or sha(backup) != authenticated:
                raise ValueError(ERROR)
            payload = work / 'payload'
            metadata = extract_verified(raw, payload)
            if (metadata.get('type') != 'tianming.recovery.v1'
                    or metadata.get('release') != state['current']
                    or metadata.get('version') != source_manifest['version']
                    or metadata.get('database') != receipt['database']
                    or local_path(metadata.get('source_shared', '')) != root / 'shared'):
                raise ValueError(ERROR)
            created = _created(metadata.get('created'), retained, audit_started)
            # The original recovery codec enforces a unique external source and
            # its bound-backup SHA. credential-config is a later target restore
            # artifact, never an invented member of the original backup format.
            authenticated_record = credential_restore.load_for_recovery(metadata, payload, backup,
                password, raw_credentials, transfer_password, authenticated_backup_sha256=authenticated)
            if authenticated_record != record:
                raise ValueError(ERROR)
            restored = payload / 'shared'
            original._no_journal(restored)
            if (sha(restored / 'data/carton_erp.sqlite3') != source_database_sha
                    or files(restored) != source_files or sha(payload / 'release.zip') != state['current']):
                raise ValueError(ERROR)
            # All schema/business SQL is on the authenticated private copy.
            # Only fresh DPAPI collection uses the pinned source DB, immutable.
            info = database_info(restored / 'data/carton_erp.sqlite3')
            if info != receipt['database']:
                raise ValueError(ERROR)
            source_snapshot = facts.capture_snapshot(restored, recorded_root=root / 'shared')
            # Independently captured current bytes must be exactly the proven
            # backup bytes. Reuse SQL schema/facts/PDF results only under that
            # equality, without opening the source for WAL-aware business SQL.
            current_snapshot = dict(source_snapshot, database_sha256=source_database_sha,
                files_sha256=contract.digest({k: v for k, v in source_files.items() if k != 'environment.json'}),
                files=len(source_files), preserved_environment_sha256=contract.digest(
                    {k: v for k, v in environment.items() if k not in facts.NETWORK_FIELDS}))
            if current_snapshot != source_snapshot:
                raise ValueError(ERROR)
            dispatch_record = dispatch.inspect_activation(restored, state,
                require_active=dispatch.signed_capability(source_manifest))
            _, schema_contract = schema_contract_from_signed_release(payload / 'release.zip', public)
            recovered_provenance, originals = history.for_recovery(payload, metadata, source_manifest, public)
            if recovered_provenance != expected_provenance:
                raise ValueError(ERROR)
            expected_members = {'release.zip', *originals}
            authority = None
            if source_manifest['revision'] != info['revision']:
                authority = state.get('schema_authority')
                contract._hash(authority)
                if (pin(root / 'packages' / (authority + '.zip')) != authority
                        or sha(payload / 'compatibility.zip') != authority):
                    raise ValueError(ERROR)
                _, authorized = inspect_package(payload / 'compatibility.zip', public)
                policy = authorized.get('migration') or {}
                if (authorized['revision'] != info['revision']
                        or policy.get('policy') != 'preserve_existing_facts_v1'
                        or policy.get('rollback_package_sha256') != state['current']):
                    raise ValueError(ERROR)
                _, schema_contract = schema_contract_from_signed_release(payload / 'compatibility.zip', public)
                expected_members.add('compatibility.zip')
            if (metadata.get('schema_authority') != authority
                    or {name for name in metadata['files'] if not name.startswith('shared/')} != expected_members):
                raise ValueError(ERROR)
            validate_database_schema(restored / 'data/carton_erp.sqlite3', schema_contract)
            _, target_contract = schema_contract_from_signed_release(target_package, public)
            if target_manifest['revision'] != info['revision']:
                raise ValueError('保留副本目标必须直接读取原结构，不隐含数据迁移')
            validate_database_schema(restored / 'data/carton_erp.sqlite3', target_contract)
            if (metadata.get(inventory.STATE_KEY) != inventory_record
                    or dispatch.validate_recovery(restored, metadata, source_manifest) != dispatch_record):
                raise ValueError(ERROR)
            recovered_inventory = inventory.for_recovery(metadata, source_manifest, authenticated)
            target_readers(restored / 'data/carton_erp.sqlite3', source_manifest, source_manifest,
                           recovered_inventory, dispatch_record, factory=False)
            target_readers(restored / 'data/carton_erp.sqlite3', source_manifest, target_manifest,
                           recovered_inventory, dispatch_record)
            with closing(sqlite3.connect((restored / 'data/carton_erp.sqlite3').as_uri()
                                         + '?mode=ro&immutable=1', uri=True)) as db:
                db.execute('PRAGMA query_only=ON')
                credential_restore._match(credential_restore._mailbox(db), record)
            _same_external(record, root, password, expected_backup_sha256)
        # private_workspace's final native DACL validation has succeeded. Do
        # not read plaintext staging again or expose evidence before this point.
        evidence = None
        evidence_bytes = None
        active = True

        def recheck():
            if not active:
                raise ValueError('来源审计租约已释放，旧证据不能继续用于签发')
            if (original._stopped(manager) != state
                    or original._config_hashes(root) != config_hashes
                    or files(root / 'shared') != source_files
                    or sha(database) != source_database_sha
                    or sha(root / 'shared/environment.json') != environment_sha
                    or (sha(process_path) if process_path.exists() else None) != process_sha
                    or any(sha(path) != expected for path, expected in pinned.items())):
                raise ValueError('签发期间来源、凭据配置或批准输入变化，保持停止')
            check_archive()
            _same_external(record, root, password, expected_backup_sha256)
            current = _now(now)
            if not audit_started <= current < contract._time(request['expires_at']):
                raise ValueError('审计完成时或签发期间申请过期或时钟倒退，未签发')
            if evidence is not None:
                if contract.canonical(evidence) != evidence_bytes:
                    raise ValueError('来源证据被调用者改变，未签发')
                return deepcopy(evidence)
            return current

        try:
            verified = recheck()
            evidence = {'type': contract.SOURCE,
                'source_sha256': contract.digest({'root': str(root), 'state_sha256': state_sha,
                    'receipt_sha256': receipt_sha, 'publisher_sha256': request['publisher_sha256']}),
                'backup_sha256': expected_backup_sha256, 'backup_created_at': created,
                'source_package_sha256': state['current'], 'credentials_sha256': retained['credentials_sha256'],
                'credential_purposes': sorted(record['entries']), 'snapshot': source_snapshot,
                'current_snapshot': current_snapshot,
                'audit_started_at': contract._stamp(audit_started), 'verified_at': contract._stamp(verified),
                'stop_proof': {'type': contract.STOP_PROOF, 'state_sha256': state_sha,
                    'database_sha256': source_database_sha, 'files_sha256': source_snapshot['files_sha256'],
                    'environment_sha256': environment_sha, 'credential_config_sha256': contract.digest(config_hashes),
                    'process_record_sha256': process_sha, 'backup_receipt_sha256': receipt_sha,
                    'manual_stop': True, 'process_stopped': True, 'maintenance_lock_held': True,
                    'database_read_lease_held': True, 'journals_absent': True}}
            contract.validate_source(evidence)
            evidence_bytes = contract.canonical(evidence)
            yield evidence, recheck
            recheck()
        finally:
            active = False
            del record, authenticated_record, raw_credentials, password
