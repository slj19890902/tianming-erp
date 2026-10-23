"""Credential removal from a private, completed SQLite snapshot, using stdlib.

This is a fixed ERP schema policy, not PII anonymization or a generic database
exporter. The caller owns private staging and its directory leases. Only the
four declared cells may change. No source connection is writable, no original
trigger executes, and only a verified, compacted database is published.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile


POLICY = 'tianming.uat-credentials.v1'
DISABLED_PASSWORD = '!uat-export-disabled-v1!'
CHANGED = {
    'users': frozenset({'password_hash', 'auth_version'}),
    'email_intake_settings': frozenset({'encrypted_secret', 'automatic_enabled'}),
}
COUNTERS = frozenset((table, column) for table in ('ai_analysis_runs', 'ai_usage_ledger')
                     for column in ('input_tokens', 'output_tokens'))
_CREDENTIAL = re.compile(r'password|passwd|passphrase|passcode|secret|token|credential|'
                         r'api_?key|private_?key|access_?key|session_?key|signing_?key|'
                         r'auth_?key|encryption_?key|(?:^|_)pwd(?:_|$)', re.I)
_MAX_AUTH_VERSION = 2**63 - 1


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _stamp(info):
    return tuple(getattr(info, key) for key in ('st_dev', 'st_ino', 'st_mode', 'st_uid',
        'st_gid', 'st_nlink', 'st_size', 'st_mtime_ns', 'st_ctime_ns'))


def _sidecars(path):
    return any(os.path.lexists(str(path) + suffix) for suffix in ('-wal', '-shm', '-journal'))


@contextmanager
def _private_file(path):
    path = Path(path).absolute()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or stat.S_IMODE(before.st_mode) & 0o077 or _sidecars(path)):
            raise ValueError('UAT snapshot must be a private single file without sidecars')
        def check():
            if (_stamp(os.fstat(fd)) != _stamp(before)
                    or _stamp(os.stat(path, follow_symlinks=False)) != _stamp(before)
                    or _sidecars(path)):
                raise ValueError('UAT snapshot changed during verification')
        check()
        yield path, fd, check
        check()
    finally:
        os.close(fd)


def _file_hash(fd):
    digest = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    while chunk := os.read(fd, 1024 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


def _read_database(path):
    connection = sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)
    connection.execute('PRAGMA query_only=ON')
    connection.execute('PRAGMA trusted_schema=OFF')
    return connection


def _integrity(connection):
    if connection.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
        raise ValueError('UAT database integrity check failed')
    if connection.execute('PRAGMA foreign_key_check').fetchone() is not None:
        raise ValueError('UAT database foreign key check failed')
    if connection.execute('PRAGMA encoding').fetchone()[0] != 'UTF-8':
        raise ValueError('UAT credential policy requires UTF-8 SQLite')


def _schema(connection):
    rows = connection.execute('SELECT type,name,tbl_name,sql FROM sqlite_schema '
                              'ORDER BY type,name,tbl_name').fetchall()
    return hashlib.sha256(_json(rows)).hexdigest()


def _columns(connection):
    """An unrecognized credential column cannot silently become business data."""
    tables = {}
    for _, name, kind, *_ in connection.execute('PRAGMA table_list'):
        if name in ('sqlite_schema', 'sqlite_temp_schema'):
            continue
        if kind in ('virtual', 'shadow'):
            raise ValueError('UAT credential policy does not support virtual tables')
        columns = connection.execute('PRAGMA table_xinfo(' + _quote(name) + ')').fetchall()
        for column in columns:
            field, declared, hidden = column[1], column[2].upper(), column[6]
            pair = (name, field)
            if pair in COUNTERS:
                if 'INT' not in declared or hidden or connection.execute(
                        'SELECT 1 FROM ' + _quote(name) + ' WHERE typeof(' + _quote(field)
                        + ") != 'integer' OR " + _quote(field) + ' < 0 LIMIT 1').fetchone():
                    raise ValueError('UAT token-count exception is not an integer counter')
            elif pair == ('users', 'must_change_password'):
                if declared not in ('BOOLEAN', 'INTEGER') or hidden or connection.execute(
                        'SELECT 1 FROM users WHERE typeof(must_change_password) != '
                        "'integer' OR must_change_password NOT IN (0,1) LIMIT 1").fetchone():
                    raise ValueError('UAT password-policy flag is not Boolean')
            elif _CREDENTIAL.search(field) and pair not in {
                    ('users', 'password_hash'), ('email_intake_settings', 'encrypted_secret')}:
                # No field name/value is echoed: even schema identifiers are untrusted.
                raise ValueError('UAT database has an unrecognized credential column')
        if kind == 'table':
            tables[name] = columns
    for table, fields in CHANGED.items():
        columns = {row[1]: row for row in tables.get(table, [])}
        if not fields <= columns.keys() or any(columns[field][6] for field in fields):
            raise ValueError('UAT database lacks the reviewed authentication schema')
        text_field = 'password_hash' if table == 'users' else 'encrypted_secret'
        if not any(t in columns[text_field][2].upper() for t in ('CHAR', 'CLOB', 'TEXT')):
            raise ValueError('UAT credential column must have text affinity')
        integer_field = 'auth_version' if table == 'users' else 'automatic_enabled'
        if columns[integer_field][2].upper() not in ('INTEGER', 'INT', 'BOOLEAN'):
            raise ValueError('UAT authentication control must have integer affinity')
    return tables


def _cell(value):
    if value is None: return ['null']
    if isinstance(value, bytes): return ['blob', value.hex()]
    if isinstance(value, int): return ['integer', str(value)]
    if isinstance(value, float): return ['real', value.hex()]
    return ['text', value]


def _business(connection, tables):
    digest, counts = hashlib.sha256(), {}
    for table in sorted(tables):
        columns = [row[1] for row in tables[table] if row[1] not in CHANGED.get(table, ())]
        # Typed row hashes preserve duplicate counts and ignore VACUUM's row order.
        select = ','.join(_quote(name) for name in columns) or 'NULL'
        hashes = [hashlib.sha256(_json([_cell(value) for value in row])).hexdigest()
                  for row in connection.execute('SELECT ' + select + ' FROM ' + _quote(table))]
        counts[table] = len(hashes)
        digest.update(_json([table, columns, sorted(hashes)]))
    return digest.hexdigest(), counts


def _auth_rows(connection, *, clean):
    users = connection.execute('SELECT password_hash,auth_version FROM users').fetchall()
    emails = connection.execute('SELECT encrypted_secret,automatic_enabled '
                                'FROM email_intake_settings').fetchall()
    for password, version in users:
        if (not isinstance(password, str) or type(version) is not int
                or not 1 <= version <= _MAX_AUTH_VERSION - (0 if clean else 1)
                or (clean and (password != DISABLED_PASSWORD or version < 2))):
            raise ValueError('UAT user credential state is invalid')
    for secret, automatic in emails:
        if (not isinstance(secret, str) or type(automatic) is not int or automatic not in (0, 1)
                or (clean and (secret != '' or automatic != 0))):
            raise ValueError('UAT mail credential state is invalid')
    return users, emails


def _report(connection, fd, *, clean):
    _integrity(connection)
    tables = _columns(connection)
    users, emails = _auth_rows(connection, clean=clean)
    business, counts = _business(connection, tables)
    if clean and connection.execute('PRAGMA freelist_count').fetchone()[0] != 0:
        raise ValueError('UAT credential-stripped database is not compacted')
    return {'policy': POLICY, 'sanitized_sha256': _file_hash(fd), 'schema_sha256': _schema(connection),
            'business_sha256': business, 'table_counts': counts,
            'credential_rows': {'users': len(users), 'email_intake_settings': len(emails)},
            'pii_anonymized': False}


def verify_sanitized(path: Path) -> dict:
    """Read-only structural, relational and credential-policy verification.

    The receipt contains hashes/counts only. Verifying the exact +1 transformation
    requires the source-bound receipt returned by sanitize, not this function.
    """
    with _private_file(path) as (path, fd, _):
        connection = _read_database(path)
        try:
            return _report(connection, fd, clean=True)
        except sqlite3.Error:
            raise ValueError('UAT SQLite verification failed') from None
        finally:
            connection.close()


def _no_secret_bytes(path, secrets, *, ignore_disabled_password=True):
    patterns = {value.encode('utf-8') for value in secrets
                if value and (not ignore_disabled_password or value != DISABLED_PASSWORD)}
    if any(len(pattern) > 1024 * 1024 for pattern in patterns):
        raise ValueError('UAT credential size exceeds policy')
    # A SQLite overflow page can split a credential copied into a business
    # TEXT/BLOB value. Physical-byte searching alone would miss that value.
    connection = _read_database(path)
    try:
        # Schema SQL and identifiers are retained too, and may span overflow
        # pages. _columns intentionally excludes this internal table.
        for row in connection.execute('SELECT type,name,tbl_name,sql FROM sqlite_schema'):
            for value in row:
                if isinstance(value, str) and any(pattern in value.encode('utf-8') for pattern in patterns):
                    raise ValueError('UAT database retains original credential bytes')
        for table, columns in _columns(connection).items():
            names = ','.join(_quote(column[1]) for column in columns)
            for row in connection.execute('SELECT ' + names + ' FROM ' + _quote(table)):
                for value in row:
                    value = value.encode('utf-8') if isinstance(value, str) else value
                    if isinstance(value, bytes) and any(pattern in value for pattern in patterns):
                        raise ValueError('UAT database retains original credential bytes')
    finally:
        connection.close()
    longest = max(map(len, patterns), default=1)
    with open(path, 'rb') as stream:
        tail = b''
        while chunk := stream.read(1024 * 1024):
            data = tail + chunk
            if any(pattern in data for pattern in patterns):
                raise ValueError('UAT database retains original credential bytes')
            tail = data[-(longest - 1):] if longest > 1 else b''


def verify_credential_values_absent(path, values, *, database=False, json_document=False):
    """Reject known inline UTF-8 credential bytes without rewriting any payload.

    Database checking also visits logical TEXT/BLOB cells, including overflow
    pages. Other files are checked as raw bytes, not decoded document formats.
    The caller must bind values to the approved source environment digest.
    """
    if (not isinstance(values, tuple) or any(not isinstance(v, str) for v in values)
            or sum(len(v.encode('utf-8')) for v in values) > 65536):
        raise ValueError('UAT inline credential patterns exceed fixed policy')
    with _private_file(path) as (bound, fd, check):
        if database:
            _no_secret_bytes(bound, values, ignore_disabled_password=False)
        elif json_document:
            from desktop_assistant.uat_export_contract import MAX_MANIFEST, reject_credential_json
            chunks = []; size = 0
            while chunk := os.read(fd, min(1024*1024, MAX_MANIFEST+1-size)):
                chunks.append(chunk); size += len(chunk)
                if size > MAX_MANIFEST:
                    raise ValueError('UAT JSON payload exceeds fixed policy')
            reject_credential_json(values, b''.join(chunks))
        else:
            patterns = {v.encode('utf-8') for v in values if v}
            longest = max(map(len, patterns), default=1)
            tail = b''
            while chunk := os.read(fd, 1024 * 1024):
                data = tail + chunk
                if any(pattern in data for pattern in patterns):
                    raise ValueError('UAT payload retains known inline credential bytes')
                tail = data[-(longest - 1):] if longest > 1 else b''
        check()


def sanitize(source_snapshot: Path, target: Path) -> dict:
    """Create a verified, credential-stripped file without overwriting any path.

    Target and temporary files must be inside caller-owned private staging; this
    function never changes a source's owner/mode or publishes to the console user.
    """
    target = Path(target).absolute()
    if os.path.lexists(target) or _sidecars(target):
        raise ValueError('UAT sanitized target already exists')
    parent_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    published = None
    try:
        parent_identity = os.fstat(parent_fd)
        if stat.S_IMODE(parent_identity.st_mode) & 0o077:
            raise ValueError('UAT sanitization requires a private staging directory')
        with _private_file(source_snapshot) as (source, source_fd, source_check):
            with tempfile.TemporaryDirectory(prefix='.uat-sanitize-', dir=target.parent) as temporary:
                work, compact = Path(temporary)/'working.db', Path(temporary)/'compacted.db'
                fd = os.open(work, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fd)
                original = _read_database(source)
                working = sqlite3.connect(work)
                try:
                    before = _report(original, source_fd, clean=False)
                    users, emails = _auth_rows(original, clean=False)
                    originals = [row[0] for row in users + emails]
                    original.backup(working)
                    working.execute('PRAGMA journal_mode=DELETE')
                    working.execute('PRAGMA trusted_schema=OFF')
                    working.execute('PRAGMA foreign_keys=ON')
                    triggers = working.execute("SELECT name,sql FROM sqlite_schema WHERE type='trigger' ORDER BY name").fetchall()
                    with working:
                        for name, _ in triggers:
                            working.execute('DROP TRIGGER ' + _quote(name))
                        working.execute('UPDATE users SET password_hash=?,auth_version=auth_version+1', (DISABLED_PASSWORD,))
                        working.execute("UPDATE email_intake_settings SET encrypted_secret='',automatic_enabled=0")
                        for _, sql in triggers:
                            working.execute(sql)
                    tables = _columns(working)
                    if (_schema(working) != before['schema_sha256'] or
                            _business(working, tables) != (before['business_sha256'], before['table_counts'])):
                        raise ValueError('UAT credential removal changed business data or schema')
                    _integrity(working)
                    working.execute('VACUUM INTO ?', (str(compact),))
                finally:
                    working.close()
                    original.close()
                os.chmod(compact, 0o600)
                after = verify_sanitized(compact)
                if any(after[key] != before[key] for key in ('schema_sha256', 'business_sha256', 'table_counts', 'credential_rows')):
                    raise ValueError('UAT compaction changed business data or schema')
                _no_secret_bytes(compact, originals)
                source_check()
                if _file_hash(source_fd) != before['sanitized_sha256']:
                    raise ValueError('UAT source snapshot content changed')
                current_parent = os.stat(target.parent, follow_symlinks=False)
                if (current_parent.st_dev, current_parent.st_ino) != (parent_identity.st_dev, parent_identity.st_ino):
                    raise ValueError('UAT target directory changed')
                # link is create-only; do not use replace/rename over an existing file.
                final_fd = os.open(compact, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                try:
                    expected = os.fstat(final_fd)
                    os.link(compact, target.name, dst_dir_fd=parent_fd, follow_symlinks=False)
                    published = expected
                    os.unlink(compact)
                    os.fsync(final_fd)
                    named = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
                    if (_stamp(named) != _stamp(os.fstat(final_fd)) or named.st_nlink != 1
                            or _file_hash(final_fd) != after['sanitized_sha256']):
                        raise ValueError('UAT sanitized target changed before publication')
                finally:
                    os.close(final_fd)
                os.fsync(parent_fd)
                source_check()
                return {**after, 'source_sha256': before['sanitized_sha256']}
    except BaseException as error:
        if published is not None:
            try:
                current = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
                if (current.st_dev, current.st_ino) == (published.st_dev, published.st_ino):
                    os.unlink(target.name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        if isinstance(error, sqlite3.Error):
            raise ValueError('UAT SQLite credential transformation failed') from None
        raise
    finally:
        os.close(parent_fd)
