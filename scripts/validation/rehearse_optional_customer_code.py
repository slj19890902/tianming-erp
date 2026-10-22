"""Rehearse only a scripts/uat/run_task.py copy, never a live database."""
import argparse
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.core.uat_isolation import validate_uat_environment
from scripts.admin.database_table_fingerprints import database_fingerprints

IDENTITY = 'uq_products_customer_material_code_name'


def schema(database):
    with sqlite3.connect(f'{database.as_uri()}?mode=ro', uri=True) as connection:
        connection.execute('PRAGMA query_only=ON')
        tables = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )]
        product_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='products' AND type='table'"
        ).fetchone()[0]
        # Compare the rest of the original CREATE statement, including every
        # check/default/FK; only the explicitly changed identity is excluded.
        product_sql = re.sub(
            r',\s*CONSTRAINT\s+"?' + IDENTITY + r'"?\s+UNIQUE\s*\(\s*'
            r'customer_id\s*,\s*customer_material_code\s*,\s*product_name\s*\)',
            '', product_sql, flags=re.IGNORECASE,
        )
        product_sql = re.sub(
            r'^CREATE TABLE\s+(?:"products"|products)',
            'CREATE TABLE products', product_sql, flags=re.IGNORECASE,
        )
        return {
            'product_other_definition': re.sub(r'\s+', '', product_sql),
            'columns': {table: connection.execute(
                'PRAGMA table_info("' + table + '")').fetchall() for table in tables},
            'fks': {table: connection.execute(
                'PRAGMA foreign_key_list("' + table + '")').fetchall() for table in tables},
            'other_tables': connection.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='table' "
                "AND name<>'products' ORDER BY name"
            ).fetchall(),
            'objects': connection.execute(
                "SELECT type,name,sql FROM sqlite_master "
                "WHERE type IN ('index','trigger','view') AND sql IS NOT NULL "
                "AND name<>? ORDER BY type,name", (IDENTITY,),
            ).fetchall(),
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise RuntimeError('Evidence output already exists')
    validate_uat_environment()
    database = Path(os.environ['ERP_DATABASE_PATH'])
    before = database_fingerprints(database, excluded_tables={'alembic_version'})
    original = schema(database)
    stages = []
    for action, revision in [
        ('upgrade', 'fa1023oc'), ('downgrade', 'ez1022hc'), ('upgrade', 'fa1023oc'),
    ]:
        result = subprocess.run([
            sys.executable, '-m', 'alembic', '-x',
            f'expected_database_path={database}', action, revision,
        ], capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(result.stderr[-7000:])
        assert database_fingerprints(database, excluded_tables={'alembic_version'}) == before
        assert schema(database) == original
        with sqlite3.connect(f'{database.as_uri()}?mode=ro', uri=True) as connection:
            connection.execute('PRAGMA query_only=ON')
            assert connection.execute('PRAGMA integrity_check').fetchone() == ('ok',)
            assert not connection.execute('PRAGMA foreign_key_check').fetchall()
            assert connection.execute('SELECT version_num FROM alembic_version').fetchone() == (revision,)
        stages.append({
            'action': action, 'revision': revision,
            'all_original_table_fields_equal': True,
            'all_original_columns_and_foreign_keys_equal': True,
            'all_other_tables_and_explicit_objects_equal': True,
            'all_other_product_definition_preserved': True,
            'integrity': 'ok', 'foreign_key_violations': 0,
        })
    receipt = {
        'status': 'historical_managed_copy_roundtrip_passed_not_formal',
        'original_business_tables': len(before),
        'explicit_objects': len(original['objects']),
        'triggers': sum(row[0] == 'trigger' for row in original['objects']),
        'stages': stages, 'formal_database_opened': False,
        'business_rows_changed': False, 'source_is_current_formal': False,
    }
    args.output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(receipt, ensure_ascii=False))


if __name__ == '__main__':
    main()
