"""Rewrap authenticated credentials inside an unactivated recovery payload only."""
from contextlib import closing
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3

from desktop_assistant import credential_store
from desktop_assistant.credential_transfer import CONFIGS, LIMIT, _validate, _path, _read, unseal
from desktop_assistant.storage import sha


def load_for_recovery(manifest, payload, backup, password, credentials=None, credential_password=None):
    embedded = manifest.get('credential_transfer')
    if embedded is not None:
        if embedded != 'credentials.tmencrypted' or credentials is not None:
            raise ValueError('凭据来源不唯一或格式错误，拒绝恢复')
        record = unseal(_read(_path(payload, embedded), LIMIT), password)
        if record['bound_backup_sha256'] is not None:
            raise ValueError('内置凭据包不能绑定另一个完整备份')
        return record
    if credentials is None:
        return None
    if credential_password is None:
        raise ValueError('外部凭据移交包需要单独的交互口令')
    record = unseal(_read(Path(credentials), LIMIT), credential_password)
    if record['bound_backup_sha256'] != sha(Path(backup)):
        raise ValueError('外部凭据包没有绑定本次完整备份 SHA256')
    return record


def _mailbox(db):
    table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='email_intake_settings'").fetchone()
    return db.execute('SELECT encrypted_secret,version FROM email_intake_settings WHERE id=1').fetchone() if table else None


def _match(row, record):
    entry = record['entries'].get('mailbox')
    if bool(row) != bool(entry):
        raise ValueError('移交包与恢复库的邮箱配置存在状态不一致')
    if row and (not isinstance(row[0], str)
                or hashlib.sha256(row[0].encode('utf-8')).hexdigest() != entry['source_protected_sha256']
                or row[1] != entry['source_version']):
        raise ValueError('移交包与恢复库的邮箱密文或版本不匹配')


def prepare(record, payload: Path):
    """Caller holds Manager lock and has authenticated/verified the backup.

    No destination installation is modified here. Failure preserves the staging
    payload; newly allocated host-secret entries are not automatically deleted.
    """
    _validate(record)
    payload = Path(payload).absolute()
    database = _path(payload, 'shared/data/carton_erp.sqlite3')
    if not database.is_file():
        raise ValueError('凭据恢复要求已经验证的静态数据库副本')
    if any(Path(str(database) + suffix).exists() for suffix in ('-wal', '-shm', '-journal')):
        raise ValueError('凭据恢复拒绝活动日志，必须使用停写的恢复副本')
    output = _path(payload, 'credential-config')
    if output.exists():
        raise ValueError('凭据暂存输出已存在，拒绝重复应用')
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
        _match(_mailbox(db), record)
    # Validate all source bindings before any host credential storage.
    references = {}
    for purpose, entry in record['entries'].items():
        reference = credential_store.protect(entry['value'], purpose)
        checked = credential_store.unprotect(reference, purpose)
        if not hmac.compare_digest(checked.encode('utf-8'), entry['value'].encode('utf-8')):
            raise ValueError('目标用户凭据回读不一致，未启用恢复数据')
        references[purpose] = reference
    output.mkdir(mode=0o700)
    paths = {}
    for purpose, (name, field) in CONFIGS.items():
        if name not in record['files']:
            continue
        settings = json.loads(record['files'][name])
        if purpose in references:
            settings[field] = references[purpose]
        path = output / name
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(settings, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        paths[name] = path
    # No empty database creation; retain all columns and the original version.
    if 'mailbox' in references:
        with closing(sqlite3.connect(database.as_uri() + '?mode=rw', uri=True)) as db:
            try:
                db.execute('PRAGMA foreign_keys=ON')
                db.execute('BEGIN IMMEDIATE')
                row = _mailbox(db)
                _match(row, record)
                changed = db.execute('UPDATE email_intake_settings SET encrypted_secret=? '
                    'WHERE id=1 AND encrypted_secret=? AND version=?',
                    (references['mailbox'], row[0], row[1]))
                if changed.rowcount != 1 or db.total_changes != 1:
                    raise ValueError('凭据重封装影响范围异常，已回滚')
                db.commit()
            except Exception:
                db.rollback()
                raise
    return paths, {'restored': sorted(references), 'mailbox_version_preserved': True}
