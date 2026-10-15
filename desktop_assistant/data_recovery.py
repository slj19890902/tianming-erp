"""Small data recovery points, separate from immutable signed programs.

The legacy ``tianming.recovery.v1`` format remains the portable all-in-one
recovery point.  This module adds a deliberately different v2 format: it uses
SQLite's online backup API, includes irreplaceable shared business files and
configuration, and binds (but does not embed) the exact signed program package.
It must never be presented as a legacy complete recovery package.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import time

from desktop_assistant.manager import CN
from desktop_assistant.storage import (archive_path, database_info, decrypt_file,
    encrypt_file, extract_verified, pack_data_recovery, sha, signed_release_manifest,
    write_json)

TYPE = 'tianming.data-recovery.v2'
DATABASE_ONLY_TYPE = 'tianming.database-snapshot.v1'
DATABASE = Path('data/carton_erp.sqlite3')
SIDECARS = {Path(str(DATABASE) + suffix) for suffix in ('-wal', '-shm', '-journal')}
PROGRAM_SEGMENTS = {'packages', 'package', 'installers', 'installer', 'releases', 'release'}
CACHE_SEGMENTS = {'__pycache__', '.pytest_cache', 'cache', 'caches', 'tmp', 'temp'}
BACKUP_SEGMENTS = {'backups', 'backup-history', 'historical-backups'}


def _safe_category(value):
    return value if value in {
        'database', 'business_file', 'configuration', 'program_runtime',
        'ocr_model', 'cache', 'old_installer', 'historical_backup', 'sqlite_sidecar'
    } else 'business_file'


def classify(relative: Path) -> str:
    """Conservative recovery classification for a path below ``shared``."""
    relative = Path(relative)
    if relative == DATABASE:
        return 'database'
    if relative in SIDECARS:
        return 'sqlite_sidecar'
    parts = {part.casefold() for part in relative.parts}
    name = relative.name.casefold()
    if name == 'environment.json' or relative.parts[:1] == ('config',):
        return 'configuration'
    if name.endswith('.tmbackup') or parts & BACKUP_SEGMENTS:
        return 'historical_backup'
    if parts & PROGRAM_SEGMENTS:
        return 'old_installer'
    if ('ocr' in parts or 'model' in parts or 'models' in parts) and name.endswith(('.pth', '.onnx', '.pt')):
        return 'ocr_model'
    if 'runtime' in parts and name != 'erp_managed_installation.json':
        return 'program_runtime'
    if parts & CACHE_SEGMENTS or name == '.ds_store' or name.endswith(('.pyc', '.pyo')):
        return 'cache'
    return 'business_file'


def included(relative: Path, *, database_only=False) -> bool:
    if database_only:
        return False
    return classify(relative) in {'business_file', 'configuration'}


def shared_inventory(shared: Path) -> dict:
    """Read-only size/class inventory; links are rejected rather than followed."""
    shared = Path(shared)
    if not shared.is_dir() or shared.is_symlink() or shared.is_junction():
        raise ValueError('数据目录不存在或属于链接')
    rows = {}
    for path in sorted(shared.rglob('*')):
        if path.is_symlink() or path.is_junction():
            raise ValueError('数据目录包含链接，不能生成恢复点')
        if not path.is_file():
            continue
        relative = path.relative_to(shared)
        stat = path.stat()
        rows[relative.as_posix()] = {
            'category': _safe_category(classify(relative)),
            'bytes': stat.st_size,
            'mtime_ns': stat.st_mtime_ns,
        }
    return rows


def summarize(inventory: dict) -> dict:
    result = defaultdict(lambda: {'files': 0, 'bytes': 0})
    for row in inventory.values():
        item = result[_safe_category(row['category'])]
        item['files'] += 1
        item['bytes'] += row['bytes']
    return dict(sorted(result.items()))


def top_items(inventory: dict, limit=10) -> list:
    return [
        {'path': name, 'category': row['category'], 'bytes': row['bytes']}
        for name, row in sorted(inventory.items(), key=lambda item: (-item[1]['bytes'], item[0]))[:limit]
    ]


def _append_log(path: Path | None, phase: str, **fields):
    if path is None:
        return
    record = {'at': datetime.now(CN).isoformat(), 'phase': phase, **fields}
    raw = (json.dumps(record, ensure_ascii=False, sort_keys=True) + '\n').encode('utf-8')
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'ab') as output:
        output.write(raw)
        output.flush()
        os.fsync(output.fileno())


def _publish_status(path: Path | None, phase: str, *, files=0, bytes_done=0,
                    bytes_total=0, error=None):
    if path is None:
        return
    value = {
        'type': 'tianming.data-backup-status.v1',
        'phase': phase,
        'files': int(files),
        'bytes_done': int(bytes_done),
        'bytes_total': int(bytes_total),
        'updated_at': datetime.now(CN).isoformat(),
        'error': error,
    }
    write_json(path, value)


def sqlite_snapshot(source: Path, target: Path, *, status_path: Path | None = None,
                    max_seconds: float = 60.0) -> dict:
    """Create an online, transactionally consistent SQLite backup."""
    source, target = Path(source), Path(target)
    if target.exists() or target.is_symlink() or not source.is_file():
        raise ValueError('SQLite 快照源或全新目标无效')
    if not isinstance(max_seconds, (int, float)) or isinstance(max_seconds, bool) or max_seconds <= 0:
        raise ValueError('SQLite 快照最长执行时间无效')
    target.parent.mkdir(parents=True, exist_ok=True)
    source_bytes = source.stat().st_size
    _publish_status(status_path, 'database_snapshot', bytes_total=source_bytes)
    started = time.monotonic()

    def progress(_status, remaining, total):
        if time.monotonic() - started > max_seconds:
            raise TimeoutError('SQLite 在线快照在持续写入下未能于限定时间完成；本次不发布')
        completed = max(0, total - remaining)
        _publish_status(status_path, 'database_snapshot',
                        bytes_done=min(source_bytes, completed * 4096), bytes_total=source_bytes)

    uri = archive_path(source).as_uri() + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True, timeout=30)) as source_db, \
            closing(sqlite3.connect(target, timeout=30)) as target_db:
        source_db.execute('PRAGMA query_only=ON')
        source_db.execute('PRAGMA busy_timeout=30000')
        source_db.backup(target_db, pages=4096, progress=progress, sleep=.01)
    snapshot_seconds = time.monotonic() - started
    checked = time.monotonic()
    result = database_info(target)
    result['bytes'] = target.stat().st_size
    result['snapshot_seconds'] = round(snapshot_seconds, 6)
    result['check_seconds'] = round(time.monotonic() - checked, 6)
    _publish_status(status_path, 'database_snapshot_complete', bytes_done=result['bytes'],
                    bytes_total=result['bytes'])
    return result


def _selected_fingerprint(inventory: dict, *, database_only=False):
    return {
        name: (row['bytes'], row['mtime_ns'], row['category'])
        for name, row in inventory.items()
        if included(Path(name), database_only=database_only)
    }


def create(shared: Path, program_package: Path, output: Path, password: str,
           public_key: bytes, *, database_only=False, status_path: Path | None = None,
           log_path: Path | None = None) -> dict:
    """Create and independently verify a v2 data point.

    The output is a new encrypted file.  The signed program is authenticated and
    referenced by SHA-256, never copied into the data point.
    """
    shared, program_package, output = Path(shared), Path(program_package), Path(output)
    if output.exists() or output.is_symlink() or not output.parent.is_dir():
        raise ValueError('数据恢复点输出必须是现有目录中的全新文件')
    started = time.monotonic()
    inventory_before = shared_inventory(shared)
    source_database = shared / DATABASE
    program_sha = sha(program_package)
    program = signed_release_manifest(program_package, public_key)
    kind = DATABASE_ONLY_TYPE if database_only else TYPE
    selected = _selected_fingerprint(inventory_before, database_only=database_only)
    selected_bytes = sum(row[0] for row in selected.values())
    _append_log(log_path, 'started', files=len(selected) + 1,
                bytes_total=source_database.stat().st_size + selected_bytes)
    try:
        with tempfile.TemporaryDirectory(prefix='tianming-data-recovery-', dir=output.parent) as temporary:
            work = Path(temporary)
            snapshot = work / 'carton_erp.sqlite3'
            database = sqlite_snapshot(source_database, snapshot, status_path=status_path)
            if database['revision'] != program.get('revision'):
                raise ValueError('数据恢复点与签名程序的数据库版本不一致')
            metadata = {
                'type': kind,
                'created': datetime.now(CN).isoformat(),
                'source_shared': str(shared),
                'database': {k: value for k, value in database.items()
                             if k not in {'snapshot_seconds', 'check_seconds', 'bytes'}},
                'database_bytes': database['bytes'],
                'program': {
                    'sha256': program_sha,
                    'version': program.get('version'),
                    'revision': program.get('revision'),
                },
                'complete_business_files': not database_only,
                'contents': summarize(inventory_before),
                'excluded_categories': ['program_runtime', 'ocr_model', 'cache',
                                        'old_installer', 'historical_backup', 'sqlite_sidecar'],
            }
            raw = work / 'data-recovery.zip'
            compressed_at = time.monotonic()
            manifest = pack_data_recovery(
                shared, snapshot, raw, metadata,
                include=lambda relative: included(relative, database_only=database_only),
            )
            compress_seconds = time.monotonic() - compressed_at
            inventory_after = shared_inventory(shared)
            if _selected_fingerprint(inventory_after, database_only=database_only) != selected:
                raise ValueError('业务文件在备份期间发生变化，本次恢复点未发布')
            if any(name in manifest['files'] for name in ('release.zip', 'compatibility.zip')):
                raise ValueError('数据恢复点不得重复嵌入程序包')
            encrypted = work / output.name
            encrypted_at = time.monotonic()
            encrypt_file(raw, encrypted, password)
            encryption_seconds = time.monotonic() - encrypted_at
            check = work / 'verified.zip'
            readback_at = time.monotonic()
            cipher_sha = decrypt_file(encrypted, check, password)
            verified = extract_verified(check, work / 'verified')
            if verified != manifest:
                raise ValueError('数据恢复点独立回读清单不一致')
            if database_info(work / 'verified/shared/data/carton_erp.sqlite3') != metadata['database']:
                raise ValueError('数据恢复点独立回读数据库不一致')
            readback_seconds = time.monotonic() - readback_at
            written_at = time.monotonic()
            descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with encrypted.open('rb') as source, os.fdopen(descriptor, 'wb') as destination:
                shutil.copyfileobj(source, destination, 1024 * 1024)
                destination.flush()
                os.fsync(destination.fileno())
            if sha(output) != cipher_sha or output.stat().st_size != encrypted.stat().st_size:
                raise ValueError('数据恢复点最终副本与已认证原件不一致')
            write_seconds = time.monotonic() - written_at
        receipt = {
            'type': 'tianming.data-backup-result.v1',
            'format': kind,
            'backup_sha256': sha(output),
            'backup_bytes': output.stat().st_size,
            'database_bytes': metadata['database_bytes'],
            'snapshot_seconds': database['snapshot_seconds'],
            'check_seconds': database['check_seconds'],
            'compress_seconds': round(compress_seconds, 6),
            'encryption_seconds': round(encryption_seconds, 6),
            'write_seconds': round(write_seconds, 6),
            'readback_seconds': round(readback_seconds, 6),
            'program_sha256': program_sha,
            'program_embedded': False,
            'business_files': len(selected),
            'business_bytes': selected_bytes,
            'elapsed_seconds': round(time.monotonic() - started, 6),
            'restored_verified': True,
        }
        _publish_status(status_path, 'complete', files=len(selected) + 1,
                        bytes_done=receipt['backup_bytes'], bytes_total=receipt['backup_bytes'])
        _append_log(log_path, 'complete', files=len(selected) + 1,
                    bytes_done=receipt['backup_bytes'], elapsed_seconds=receipt['elapsed_seconds'])
        return receipt
    except Exception as error:
        safe = '数据恢复点未完成；源数据和已有恢复点未修改'
        _publish_status(status_path, 'failed', error={
            'code': 'DATA_BACKUP_FAILED', 'message': safe, 'error_type': type(error).__name__})
        _append_log(log_path, 'failed', error_code='DATA_BACKUP_FAILED', error_type=type(error).__name__)
        raise


def restore(backup: Path, password: str, target_shared: Path, program_package: Path,
            public_key: bytes, *, expected_backup_sha256: str | None = None) -> dict:
    """Restore v2 into a new shared directory using a separately supplied program."""
    backup, target_shared, program_package = Path(backup), Path(target_shared), Path(program_package)
    if target_shared.exists() or target_shared.is_symlink():
        raise ValueError('数据恢复目标必须不存在')
    if expected_backup_sha256 is not None and not re.fullmatch('[0-9a-f]{64}', expected_backup_sha256):
        raise ValueError('批准备份摘要格式无效')
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='tianming-data-restore-', dir=target_shared.parent) as temporary:
        work = Path(temporary)
        raw = work / 'data-recovery.zip'
        decrypted_at = time.monotonic()
        authenticated = decrypt_file(backup, raw, password)
        decrypt_seconds = time.monotonic() - decrypted_at
        if expected_backup_sha256 is not None and authenticated != expected_backup_sha256:
            raise ValueError('实际认证的数据恢复点不属于批准摘要')
        if sha(backup) != authenticated:
            raise ValueError('数据恢复点在认证期间变化')
        extracted_at = time.monotonic()
        manifest = extract_verified(raw, work / 'payload')
        extract_seconds = time.monotonic() - extracted_at
        if manifest.get('type') != TYPE or manifest.get('complete_business_files') is not True:
            raise ValueError('不是可独立恢复的完整数据恢复点')
        checked_at = time.monotonic()
        program = signed_release_manifest(program_package, public_key)
        identity = sha(program_package)
        binding = manifest.get('program') or {}
        if binding != {'sha256': identity, 'version': program.get('version'),
                       'revision': program.get('revision')}:
            raise ValueError('签名程序与数据恢复点绑定不一致')
        payload_shared = work / 'payload/shared'
        database = payload_shared / DATABASE
        info = database_info(database)
        if info != manifest.get('database') or info['revision'] != program.get('revision'):
            raise ValueError('恢复数据库与清单或签名程序不一致')
        from desktop_assistant.manager import (schema_contract_from_signed_release,
                                               validate_database_schema,
                                               rebind_pdf_sources)
        release, contract = schema_contract_from_signed_release(program_package, public_key)
        if release != program or not contract or contract.get('revision') != info['revision']:
            raise ValueError('缺少与数据恢复点匹配的签名结构合同')
        validate_database_schema(database, contract)
        rebind_pdf_sources(database, manifest['source_shared'], payload_shared, target_shared)
        from desktop_assistant.preflight import inspect
        checks = inspect(database, payload_shared, managed=True, recorded_root=target_shared)
        if any(checks['counts'][key] for key in ('missing', 'external', 'hash_mismatch')):
            raise ValueError('数据恢复点缺少数据库登记的业务原件')
        if database_info(database) != info:
            raise ValueError('数据恢复后的数据库完整性、外键、版本或表计数不一致')
        validate_database_schema(database, contract)
        if sha(backup) != authenticated or sha(program_package) != identity:
            raise ValueError('恢复输入在最终提升前变化')
        check_seconds = time.monotonic() - checked_at
        promoted_at = time.monotonic()
        payload_shared.rename(target_shared)
        promote_seconds = time.monotonic() - promoted_at
    return {
        'type': 'tianming.data-restore-result.v1',
        'backup_sha256': authenticated,
        'program_sha256': identity,
        'version': program.get('version'),
        'revision': info['revision'],
        'data_time': manifest['created'],
        'database': info,
        'started': False,
        'decrypt_seconds': round(decrypt_seconds, 6),
        'extract_seconds': round(extract_seconds, 6),
        'check_seconds': round(check_seconds, 6),
        'promote_seconds': round(promote_seconds, 6),
        'elapsed_seconds': round(time.monotonic() - started, 6),
    }
