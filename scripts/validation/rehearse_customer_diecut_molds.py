"""Rehearse existing mold APIs on a NEW SQLite backup, never the source DB."""
import argparse
import hashlib
import json
import sqlite3
from pathlib import Path
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.core.database import create_sqlite_engine
from app.models.user import User
from app.models.product import Product
from app.models.mold_tool import MoldTool
from app.api.warehouse import (MoldToolPayload, MoldProductBindingPayload,
                               create_mold_tool, bind_mold_products, update_mold_tool)
from scripts.audit.customer_diecut_molds import build_plan, LOCATION


def fingerprints(path):
    with sqlite3.connect(f'file:{quote(path.as_posix(), safe="/:")}?mode=ro', uri=True) as db:
        names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        result = {}
        for name in names:
            quoted = '"' + name.replace('"', '""') + '"'
            rows = sorted(repr(tuple(r)) for r in db.execute(f'SELECT * FROM {quoted}'))
            result[name] = hashlib.sha256('\n'.join(rows).encode()).hexdigest()
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    source = args.source.resolve(strict=True)
    output = args.output.resolve()
    # Rehearsal files must be outside the formal installation and new each run.
    permitted = Path('D:/.codex/workspace_artifacts').resolve()
    if not output.is_relative_to(permitted) or output.exists():
        raise ValueError('output must be a new directory under workspace_artifacts')
    output.mkdir(parents=True)
    target = output / 'molds-isolated.sqlite3'
    assert target != source
    with sqlite3.connect(f'file:{quote(source.as_posix(), safe="/:")}?mode=ro', uri=True) as src:
        src.execute('PRAGMA query_only=ON')
        with sqlite3.connect(target) as dst:
            src.backup(dst)
    with sqlite3.connect(target) as db:
        plan = build_plan(db)
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
    (output/'plan.json').write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding='utf-8')
    before = fingerprints(target)
    engine = create_sqlite_engine(target)
    request = Request({'type': 'http', 'method': 'POST', 'path': '/isolated-mold-rehearsal',
                       'headers': [], 'client': ('127.0.0.1', 0)})
    results = []
    candidates = [row for row in plan['items'] if row['action'] == 'create_and_bind']
    for index, row in enumerate(candidates):
        payload = MoldToolPayload(label_name=row['label_name'], chinese_short_name=row['product_name'],
            customers=[{'customer_id': row['customer_id'], 'display_order': 1}],
            rack_location=LOCATION, idempotency_key=row['idempotency_key'])
        with Session(engine) as db:
            user = db.scalar(select(User).where(User.role == 'admin', User.is_active.is_(True)).order_by(User.id))
            if user is None:
                raise ValueError('no active admin in isolated snapshot')
            current = db.get(Product, row['id'])
            assert current.version == row['version'] and current.mold_tool_id is None
            created = create_mold_tool(payload, request, db, user)
            bound = bind_mold_products(created['id'], MoldProductBindingPayload(items=[{
                'product_id': row['id'], 'expected_version': row['version']}]), db, user)
            assert bound['bound_count'] == 1
            mold = db.get(MoldTool, created['id'])
            assert mold.label_name == row['label_name'] and mold.chinese_short_name == row['product_name']
            assert mold.rack_location == LOCATION
            results.append({'product_id': row['id'], 'mold_id': mold.id, 'mold_code': mold.mold_code})
        # New session exercises persisted idempotency after restart/refresh.
        with Session(engine) as db:
            user = db.scalar(select(User).where(User.role == 'admin', User.is_active.is_(True)).order_by(User.id))
            replay = create_mold_tool(payload, request, db, user)
            assert replay['idempotent_replay'] and replay['id'] == created['id']
            repeat = bind_mold_products(created['id'], MoldProductBindingPayload(items=[{
                'product_id': row['id'], 'expected_version': row['version']}]), db, user)
            assert repeat['bound_count'] == 0
        if (index + 1) % 25 == 0:
            print(f'validated {index + 1}/{len(candidates)}', flush=True)
    updated = []
    for row in plan['items']:
        if row['action'] != 'reuse_bound':
            continue
        existing = row['existing_mold']
        if existing['rack_location'] != LOCATION:
            continue  # Moving an existing physical mold needs actual-location evidence.
        with Session(engine) as db:
            user = db.scalar(select(User).where(User.role == 'admin', User.is_active.is_(True)).order_by(User.id))
            mold = db.get(MoldTool, existing['id'])
            assert mold.version == existing['version']
            payload = MoldToolPayload(label_name=row['label_name'], chinese_short_name=row['product_name'],
                customers=[{'customer_id': link.customer_id, 'display_order': link.display_order}
                           for link in mold.customer_links],
                rack_location=mold.rack_location, remarks=mold.remarks, expected_version=mold.version,
                idempotency_key=f'customer-diecut-mold-update:{mold.id}:{mold.version}')
            result = update_mold_tool(mold.id, payload, request, db, user)
            assert result['chinese_short_name'] == row['product_name']
            updated.append({'mold_id': mold.id, 'product_id': row['id'],
                            'before_short_name': existing['chinese_short_name'],
                            'after_short_name': row['product_name']})
        with Session(engine) as db:
            user = db.scalar(select(User).where(User.role == 'admin', User.is_active.is_(True)).order_by(User.id))
            replay = update_mold_tool(existing['id'], payload, request, db, user)
            assert replay['idempotent_replay'] and replay['id'] == existing['id']
    engine.dispose()
    after = fingerprints(target)
    changed = sorted(name for name in before if before[name] != after[name])
    with sqlite3.connect(target) as db:
        integrity = db.execute('PRAGMA integrity_check').fetchone()[0]
        fk = db.execute('PRAGMA foreign_key_check').fetchall()
        refreshed = build_plan(db)
    evidence = {'source': str(source), 'isolated_database': str(target),
        'source_write': False, 'created_and_replayed': len(results), 'results': results,
        'existing_labels_updated_and_replayed': updated,
        'changed_tables': changed, 'unchanged_tables': len(before)-len(changed),
        'integrity': integrity, 'foreign_keys': fk, 'next_plan_summary': refreshed['summary']}
    (output/'evidence.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
    assert integrity == 'ok' and not fk
    assert set(changed) <= {'master_data_object_versions', 'mold_master_mutations',
                            'mold_tool_customers', 'mold_tools', 'operation_logs', 'products'}
    assert not refreshed['summary'].get('create_and_bind')
    print(json.dumps({k:v for k,v in evidence.items() if k != 'results'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
