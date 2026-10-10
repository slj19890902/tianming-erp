"""Read-only discovery of persisted path candidates in an offline ERP copy.

Reports contain private source values and belong outside the source repository.
This discovers candidates for review; it never authorizes automatic rewriting.
"""
from contextlib import closing
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3


def _sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _kind(value, field):
    if re.match(r'^[A-Za-z]:[\\/]', value):
        return 'windows_absolute'
    if value.startswith('\\\\'):
        return 'windows_unc'
    if value.startswith(('private:', '/static/uploads/')):
        return 'managed_reference'
    if value.startswith('/'):
        return 'posix_absolute_or_url_path'
    if re.search(r'(?:^|[\s"\x27(])[A-Za-z]:[\\/]', value):
        return 'embedded_windows_path_requires_review'
    if re.search(r'(?:path|filename|file_name|stored_name)$', field, re.I) and value:
        return 'named_relative_or_url_requires_review'
    return None


def _strings(value, pointer='', field=''):
    if isinstance(value, str):
        yield pointer, field, value
    elif isinstance(value, dict):
        for key, child in value.items():
            escaped = key.replace('~', '~0').replace('/', '~1')
            yield from _strings(child, pointer + '/' + escaped, key)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _strings(child, pointer + '/' + str(index), field)


def inventory(database: Path):
    database = database.resolve(strict=True)
    wal = Path(str(database) + '-wal')
    if wal.exists() and wal.stat().st_size:
        raise ValueError('路径盘点需要已核验的静态副本，不能忽略WAL')
    before = _sha(database)
    records = []
    tables = {}
    binary_values = 0
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        names = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        for table in names:
            columns = db.execute('PRAGMA table_info(' + _quote(table) + ')').fetchall()
            primary = sorted(((c[5], c[1]) for c in columns if c[5]), key=lambda item: item[0])
            count = 0
            cursor = db.execute('SELECT * FROM ' + _quote(table))
            labels = [column[0] for column in cursor.description]
            for row in cursor:
                count += 1
                values = dict(zip(labels, row))
                identity = {name: values[name] if not isinstance(values[name], bytes) else {'blob_sha256': hashlib.sha256(values[name]).hexdigest()}
                            for _, name in primary}
                for column, raw in values.items():
                    if isinstance(raw, bytes):
                        binary_values += 1
                        continue
                    if not isinstance(raw, str):
                        continue
                    value = raw
                    if raw.lstrip().startswith(('{', '[')):
                        try:
                            value = json.loads(raw)
                        except (ValueError, RecursionError):
                            pass
                    for pointer, field, text in _strings(value, field=column):
                        kind = _kind(text, field)
                        if kind:
                            records.append({'table': table, 'primary_key': identity,
                                            'row_ordinal': count, 'column': column,
                                            'json_pointer': pointer, 'kind': kind, 'original_value': text,
                                            'automatic_rewrite_allowed': False})
            tables[table] = count
    if _sha(database) != before or (wal.exists() and wal.stat().st_size):
        raise ValueError('路径盘点期间来源改变，报告无效')
    return {'read_only': True, 'source_sha256': before, 'tables_scanned': tables,
            'binary_values_not_interpreted': binary_values, 'candidates': records,
            'ready_for_takeover': False,
            'remaining': ['attachment registry and configuration audit', 'review and map each applicable reference',
                          'verify all copied attachment content and references', 'review encoded/binary storage if applicable']}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.resolve().is_relative_to(Path(__file__).resolve().parents[1]):
        raise ValueError('私有路径报告必须保存在程序及源码目录之外')
    result = inventory(args.database)
    # Exclusive creation and private permissions; never print path contents.
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(json.dumps({'read_only': True, 'tables': len(result['tables_scanned']),
                      'path_candidates': len(result['candidates']), 'ready_for_takeover': False}))


if __name__ == '__main__':
    main()
