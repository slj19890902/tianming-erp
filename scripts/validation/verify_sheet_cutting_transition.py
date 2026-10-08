"""Read-only before/after proof for the authorized master/order conversion."""
import argparse
import json
from pathlib import Path
import sqlite3


def verify(before, after, plan):
    def connect(path):
        db = sqlite3.connect(path.resolve(strict=True).as_uri() + '?mode=ro', uri=True)
        db.execute('PRAGMA query_only=ON')
        return db
    issues = []
    changes = {row['before']['id']: row for row in plan['products']}
    orders = {row['before']['id']: row for row in plan['orders']}
    with connect(before) as source, connect(after) as target:
        tables = [r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        for table in tables:
            if table in ('operation_logs', 'master_data_object_versions', 'alembic_version'):
                continue
            columns = [r[1] for r in source.execute(f'PRAGMA table_info("{table}")')]
            if table == 'products':
                columns = [c for c in columns if c not in ('updated_at', 'version', 'default_cutting_mode', 'production_process', 'sheet_cutting_settings')]
            if table == 'sales_order_items':
                columns = [c for c in columns if c != 'sheet_cutting_settings_snapshot']
            fields = ','.join('"' + c + '"' for c in columns)
            query = f'SELECT {fields} FROM "{table}" ORDER BY ' + (f'"{columns[0]}"' if columns else 'rowid')
            if source.execute(query).fetchall() != target.execute(query).fetchall():
                issues.append(table)
        for row in target.execute('SELECT id,version,default_cutting_mode,production_process,sheet_cutting_settings FROM products'):
            pid, version, mode, process, settings = row
            old = source.execute('SELECT version,default_cutting_mode,production_process FROM products WHERE id=?', (pid,)).fetchone()
            if pid in changes:
                expected = changes[pid]['updates']
                assert version == old[0] + 1 and mode == '一开一'
                assert process == expected.get('production_process', old[2])
                assert json.loads(settings) == expected['sheet_cutting_settings']
            else:
                assert (version, mode, process) == old
        for oid, settings in target.execute('SELECT id,sheet_cutting_settings_snapshot FROM sales_order_items'):
            if oid in orders:
                assert json.loads(settings) == orders[oid]['settings']
            else:
                cols = {r[1] for r in source.execute('PRAGMA table_info(sales_order_items)')}
                old = source.execute('SELECT sheet_cutting_settings_snapshot FROM sales_order_items WHERE id=?', (oid,)).fetchone()[0] if 'sheet_cutting_settings_snapshot' in cols else None
                assert settings == old
        integrity = target.execute('PRAGMA integrity_check').fetchall()
        fk = target.execute('PRAGMA foreign_key_check').fetchall()
        assert not issues and integrity == [('ok',)] and not fk, (issues, integrity, fk)
    return {'compared_tables': len(tables), 'unexpected_changes': issues, 'integrity': 'ok', 'foreign_key_issues': 0,
            'products_verified': len(changes), 'unreported_orders_verified': len(orders)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('before', 'after', 'plan', 'output'):
        parser.add_argument('--' + name, required=True, type=Path)
    args = parser.parse_args()
    result = verify(args.before, args.after, json.loads(args.plan.read_text(encoding='utf-8')))
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(json.dumps(result, ensure_ascii=False))
