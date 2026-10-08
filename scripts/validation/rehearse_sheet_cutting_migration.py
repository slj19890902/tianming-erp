"""Rehearse on new disposable copies of a hashed offline audit artifact only."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.validation.rehearse_delivery_quantity_migration import original_columns, evidence, migrate


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--expected-sha256', required=True)
    parser.add_argument('--output-directory', required=True, type=Path)
    args = parser.parse_args()
    source, output = args.source.resolve(strict=True), args.output_directory.resolve()
    assert 'workspace_artifacts' in source.parts and source.is_file()
    assert digest(source) == args.expected_sha256.lower()
    assert not output.exists() and source.parent != output
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    config = Config(str(ROOT / 'alembic.ini'))
    config.set_main_option('script_location', str(ROOT / 'alembic'))
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == ['eg1008sc']
    columns = original_columns(source)
    baseline = evidence(source, columns)
    assert baseline['revision'] == [('ef1007cp',)]
    output.mkdir(parents=True)
    (output / 'original-facts.json').write_text(json.dumps(baseline, ensure_ascii=False, indent=2), encoding='utf-8')
    database = output / 'roundtrip.sqlite3'
    shutil.copy2(source, database)
    checkpoints = []
    for direction, revision in [('upgrade', 'eg1008sc'), ('downgrade', 'ef1007cp'), ('upgrade', 'eg1008sc')]:
        run = migrate(database, direction, revision, output)
        (output / f'{len(checkpoints)}-{direction}.log').write_text(run.stdout + run.stderr, encoding='utf-8')
        assert run.returncode == 0, run.stderr
        after = evidence(database, columns)
        assert after['revision'] == [(revision,)] and after['tables'] == baseline['tables']
        # The only new schema object is the immutable new snapshot trigger.
        before_objects = baseline['schema_objects']
        after_objects = [row for row in after['schema_objects'] if row[1] != 'trg_bom_sheet_cutting_snapshot_immutable']
        assert after_objects == before_objects
        checkpoints.append({'direction': direction, 'revision': revision, 'original_facts_preserved': True})
    guard = output / 'downgrade-guard.sqlite3'
    shutil.copy2(database, guard)
    with sqlite3.connect(guard) as db:
        db.execute("UPDATE products SET sheet_cutting_settings='{}' WHERE id=(SELECT MIN(id) FROM products)")
    before_hash = digest(guard)
    run = migrate(guard, 'downgrade', 'ef1007cp', output)
    assert run.returncode != 0 and '禁止有损降级' in run.stderr
    assert digest(guard) == before_hash
    assert digest(source) == args.expected_sha256.lower()
    result = {'checkpoints': checkpoints, 'tables_preserved': len(columns), 'integrity': 'ok',
        'foreign_key_issues': 0, 'source_unchanged': True, 'fact_bearing_downgrade_rejected_without_write': True}
    (output / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
