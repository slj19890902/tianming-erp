"""Rehearse eb0926dq on disposable copies of an explicitly hashed offline source."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def digest(path):
    with open(path, 'rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def readonly(path):
    connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    connection.execute('PRAGMA query_only=ON')
    return connection


def original_columns(path):
    with readonly(path) as db:
        return {name: [row[1] for row in db.execute(f'PRAGMA table_info({quote(name)})')]
                for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' AND name <> 'alembic_version' ORDER BY name")}


def evidence(path, columns):
    with readonly(path) as db:
        assert db.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        hashes = {}
        for table, names in columns.items():
            selected = ','.join(map(quote, names))
            sha, count = hashlib.sha256(), 0
            for row in db.execute(f'SELECT {selected} FROM {quote(table)} ORDER BY {selected}'):
                sha.update(json.dumps(row, ensure_ascii=False, default=str).encode('utf-8'))
                sha.update(b'\n')
                count += 1
            hashes[table] = {'rows': count, 'sha256': sha.hexdigest()}
        return {'tables': hashes, 'schema_objects': db.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE type IN ('index','trigger') "
            "ORDER BY type,name").fetchall(), 'revision': db.execute(
                'SELECT version_num FROM alembic_version ORDER BY version_num').fetchall()}


def migrate(path, direction, target, output):
    env = {**os.environ, 'ERP_DATABASE_PATH': str(path),
           'ERP_BACKUP_DIR': str(output / 'backups'),
           'PYTHONUTF8': '1',
           'ERP_SECRET_KEY': 'isolated-migration-test-secret-not-production'}
    result = subprocess.run([sys.executable, '-B', '-m', 'alembic',
        '-x', f'expected_database_path={path}', direction, target], cwd=ROOT,
        env=env, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=120)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--expected-sha256', required=True)
    parser.add_argument('--output-directory', type=Path, required=True)
    args = parser.parse_args()
    source, output = args.source.resolve(), args.output_directory.resolve()
    assert source.is_file() and digest(source) == args.expected_sha256.lower()
    assert 'workspace_artifacts' in source.parts, 'Only an offline audit artifact is allowed'
    assert source.parent != output and not output.exists(), 'Use a new disposable directory'
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    config = Config(str(ROOT / 'alembic.ini'))
    config.set_main_option('script_location', str(ROOT / 'alembic'))
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == ['eb0926dq']
    assert script.get_revision('eb0926dq').down_revision == 'ea0926'
    columns = original_columns(source)
    baseline = evidence(source, columns)
    assert baseline['revision'] == [('ea0926',)]
    output.mkdir(parents=True)
    (output / 'original-facts.json').write_text(
        json.dumps(baseline, ensure_ascii=False, indent=2), encoding='utf-8')
    database = output / 'roundtrip.sqlite3'
    shutil.copy2(source, database)
    assert digest(database) == digest(source)
    checkpoints = []
    for direction, target in [('upgrade', 'eb0926dq'), ('downgrade', 'ea0926'), ('upgrade', 'eb0926dq')]:
        result = migrate(database, direction, target, output)
        (output / f'{len(checkpoints)}-{direction}.log').write_text(result.stdout + result.stderr, encoding='utf-8')
        assert result.returncode == 0, result.stderr
        current = evidence(database, columns)
        assert current['revision'] == [(target,)]
        assert current['tables'] == baseline['tables'], 'Original business fields changed'
        assert current['schema_objects'] == baseline['schema_objects'], 'Indexes or triggers changed'
        checkpoints.append({'direction': direction, 'revision': target, 'original_facts_preserved': True})
    with sqlite3.connect(database) as db:
        assert db.execute('SELECT COUNT(*) FROM sales_delivery_items WHERE quantity_contract_json IS NOT NULL').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM stock_replenishment_order_items WHERE quantity_contract_json IS NOT NULL').fetchone()[0] == 0
        for table in ('inventory_reservations', 'delivery_inventory_allocations'):
            assert db.execute(f'SELECT COUNT(*) FROM {table} WHERE requirement_quantity_denominator <> 1').fetchone()[0] == 0
            row = db.execute(f'SELECT id FROM {table} LIMIT 1').fetchone()
            assert row, f'No existing {table} rows to test'
            for bad in (0, -1, None):
                try:
                    db.execute(f'UPDATE {table} SET requirement_quantity_denominator=? WHERE id=?', (bad, row[0]))
                except sqlite3.IntegrityError:
                    db.rollback()
                else:
                    db.rollback()
                    raise AssertionError(f'{table} accepted invalid denominator {bad}')
    guards = []
    for table, field, value in [('sales_delivery_items', 'quantity_contract_json', '{}'),
            ('stock_replenishment_order_items', 'quantity_contract_json', '{}'),
            ('inventory_reservations', 'requirement_quantity_denominator', 2),
            ('delivery_inventory_allocations', 'requirement_quantity_denominator', 3)]:
        candidate = output / f'guard-{table}.sqlite3'
        shutil.copy2(database, candidate)
        with sqlite3.connect(candidate) as db:
            db.execute(f'UPDATE {table} SET {field}=? WHERE id=(SELECT id FROM {table} LIMIT 1)', (value,))
        before = digest(candidate)
        result = migrate(candidate, 'downgrade', 'ea0926', output)
        (output / f'guard-{table}.log').write_text(result.stdout + result.stderr, encoding='utf-8')
        assert result.returncode != 0 and '禁止有损降级' in result.stderr
        assert digest(candidate) == before, 'Refused downgrade changed the database'
        guards.append({'table': table, 'refused_without_writes': True})
    assert digest(source) == args.expected_sha256.lower(), 'Offline source changed'
    report = {'source': str(source), 'source_sha256': args.expected_sha256.lower(),
        'original_table_count': len(columns), 'schema_object_count': len(baseline['schema_objects']),
        'checkpoints': checkpoints, 'denominator_constraints': 'passed', 'downgrade_guards': guards,
        'database': str(database), 'integrity_check': 'ok', 'foreign_key_check': 0,
        'production_modified': False}
    (output / 'evidence.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
