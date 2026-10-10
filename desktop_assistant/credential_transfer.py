"""Authenticated, password-encrypted operational credentials; no plaintext files.

Run export as the original service user. This is not a publisher-key export and
does not alter the source, restore a database, or enable any business traffic.
"""
import argparse
from contextlib import closing
import getpass
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from desktop_assistant.credential_store import unprotect

MAGIC = b'TIANMING-CREDENTIALS\x00\x01'
LIMIT = 256 * 1024
TYPE = 'tianming.credentials.v1'
CONFIGS = {
    'backup': ('preferences.json', 'protected_password'),
    'openai': ('control/ai-provider.json', 'protected_api_key'),
    'deepseek': ('control/deepseek-provider.json', 'protected_api_key'),
}


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _read(path, limit):
    with path.open('rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('凭据文件大小超限')
    return raw


def _path(root, relative):
    path = root / relative
    if any(p.is_symlink() or p.is_junction() for p in (path, *path.parents)):
        raise ValueError('凭据来源不得通过目录或文件链接跳转')
    return path


def _entry(value, purpose):
    if not isinstance(value, str) or not value:
        raise ValueError('原凭据格式不完整，未导出')
    try:
        secret = unprotect(value, purpose)
    except Exception:
        raise ValueError(f'{purpose} 凭据不能由当前服务用户读取，未导出') from None
    if not isinstance(secret, str) or not 0 < len(secret.encode('utf-8')) <= 16384:
        raise ValueError('原凭据长度无效，未导出')
    return {'value': secret, 'source_protected_sha256': _hash(value.encode('utf-8'))}


def collect(root: Path, *, bound_backup_sha256=None) -> dict:
    """Read only known operational locations; never enumerate system secrets."""
    root = Path(root).absolute()
    if not root.is_dir():
        raise ValueError('ERP 来源目录不存在')
    files, entries, originals = {}, {}, {}
    for purpose, (name, field) in CONFIGS.items():
        path = _path(root, name)
        if not path.exists():
            originals[name] = None
            continue
        raw = _read(path, 65536)
        try:
            settings = json.loads(raw)
            if not isinstance(settings, dict):
                raise ValueError()
            if purpose != 'backup' and settings.get('provider') != purpose:
                raise ValueError()
            value = settings.get(field)
            if purpose != 'backup' or value is not None:
                entries[purpose] = _entry(value, purpose)
        except (UnicodeError, json.JSONDecodeError, TypeError, AttributeError):
            raise ValueError('原凭据配置格式错误，未导出') from None
        files[name] = raw.decode('utf-8-sig')
        originals[name] = raw
    database = _path(root, 'shared/data/carton_erp.sqlite3')
    if not database.is_file():
        raise ValueError('缺少正式来源数据库，不能确认邮箱凭据是否存在')
    try:
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=5)) as db:
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='email_intake_settings'").fetchone()
            mailbox = db.execute('SELECT encrypted_secret,version FROM email_intake_settings WHERE id=1').fetchone() if table else None
            if mailbox:
                entries['mailbox'] = {**_entry(mailbox[0], 'mailbox'), 'source_version': mailbox[1]}
    except sqlite3.Error:
        raise ValueError('来源数据库无法只读核对邮箱凭据，未导出') from None
    for name, original in originals.items():
        path = _path(root, name)
        current = _read(path, 65536) if path.exists() else None
        if current != original:
            raise ValueError('凭据配置在导出期间变化，请重新导出')
    result = {'type': TYPE, 'source_platform': sys.platform, 'files': files, 'entries': entries,
              'bound_backup_sha256': bound_backup_sha256}
    _validate(result)
    return result


def _validate(record):
    if (not isinstance(record, dict) or set(record) != {'type', 'source_platform', 'files', 'entries', 'bound_backup_sha256'}
            or record['type'] != TYPE or record['source_platform'] not in {'win32', 'darwin'}):
        raise ValueError('凭据移交内容格式错误')
    binding = record['bound_backup_sha256']
    if binding is not None and (not isinstance(binding, str) or len(binding) != 64
                               or any(c not in '0123456789abcdef' for c in binding)):
        raise ValueError('凭据包绑定的完整备份指纹无效')
    if not isinstance(record['files'], dict) or not set(record['files']) <= {v[0] for v in CONFIGS.values()}:
        raise ValueError('凭据移交包含未知配置路径')
    if not isinstance(record['entries'], dict) or not set(record['entries']) <= {*CONFIGS, 'mailbox'}:
        raise ValueError('凭据移交包含未知用途')
    for purpose, entry in record['entries'].items():
        expected = {'value', 'source_protected_sha256'} | ({'source_version'} if purpose == 'mailbox' else set())
        if not isinstance(entry, dict) or set(entry) != expected:
            raise ValueError('凭据条目格式错误')
        if not isinstance(entry['value'], str) or not 0 < len(entry['value'].encode('utf-8')) <= 16384:
            raise ValueError('凭据长度错误')
        digest = entry['source_protected_sha256']
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise ValueError('来源凭据指纹无效')
        if purpose == 'mailbox' and (type(entry['source_version']) is not int or entry['source_version'] < 1):
            raise ValueError('来源邮箱版本无效')
    for purpose, (name, field) in CONFIGS.items():
        if name not in record['files']:
            if purpose in record['entries']:
                raise ValueError('凭据缺少原配置')
            continue
        raw = record['files'][name]
        if not isinstance(raw, str) or len(raw.encode('utf-8')) > 65536:
            raise ValueError('原配置格式错误')
        config = json.loads(raw)
        if not isinstance(config, dict) or (purpose != 'backup' and config.get('provider') != purpose):
            raise ValueError('原配置类型错误')
        value = config.get(field)
        if value is not None or purpose != 'backup':
            if (purpose not in record['entries'] or not isinstance(value, str)
                    or _hash(value.encode('utf-8')) != record['entries'][purpose]['source_protected_sha256']):
                raise ValueError('原配置与凭据指纹不一致')
        elif purpose in record['entries']:
            raise ValueError('凭据与原配置缺失状态不一致')


def _key(password, salt):
    if not isinstance(password, str) or not 12 <= len(password) <= 1024:
        raise ValueError('移交口令需为12至1024个字符')
    return hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 600000, 32)


def seal(record, password):
    _validate(record)
    raw = json.dumps(record, ensure_ascii=False, sort_keys=True).encode('utf-8')
    if len(raw) > LIMIT - len(MAGIC) - 44:
        raise ValueError('凭据移交内容过大')
    salt, nonce = os.urandom(16), os.urandom(12)
    header = MAGIC + salt + nonce
    return header + AESGCM(_key(password, salt)).encrypt(nonce, raw, header)


def unseal(raw, password):
    try:
        start = len(MAGIC)
        if not isinstance(raw, bytes) or not start + 44 < len(raw) <= LIMIT or not raw.startswith(MAGIC):
            raise ValueError()
        salt, nonce = raw[start:start+16], raw[start+16:start+28]
        content = AESGCM(_key(password, salt)).decrypt(nonce, raw[start+28:], raw[:start+28])
        record = json.loads(content)
        _validate(record)
        return record
    except Exception:
        raise ValueError('移交口令错误、内容不兼容或加密凭据包已损坏') from None


def export(root, destination, password, *, backup=None):
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise ValueError('移交输出已存在，拒绝覆盖')
    from desktop_assistant.storage import sha
    record = collect(root, bound_backup_sha256=sha(Path(backup)) if backup is not None else None)
    encrypted = seal(record, password)
    if unseal(encrypted, password) != record:
        raise ValueError('移交加密回读不一致')
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(encrypted)
        stream.flush()
        os.fsync(stream.fileno())
    if _read(destination, LIMIT) != encrypted:
        raise ValueError('移交输出回读失败，不能使用该文件')
    return {'sha256': _hash(encrypted), 'size': len(encrypted), 'purposes': sorted(record['entries'])}


def main():
    parser = argparse.ArgumentParser(description='原服务用户导出加密运营凭据，不导出发布私钥')
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--backup', type=Path, help='绑定需要恢复的完整加密备份；旧备份跨机恢复必须指定')
    args = parser.parse_args()
    if not sys.stdin.isatty():
        raise SystemExit('请在原服务用户的交互终端运行，口令不能通过参数、管道或日志传入')
    password = getpass.getpass('移交口令（至少12位）：')
    if getpass.getpass('再次输入移交口令：') != password:
        raise SystemExit('两次口令不一致，未导出')
    try:
        print(json.dumps(export(args.root, args.output, password, backup=args.backup), ensure_ascii=False))
    except Exception:
        raise SystemExit('移交未完成：请核对原服务用户、来源目录、输出目录及口令；未输出秘密详情') from None


if __name__ == '__main__':
    main()
