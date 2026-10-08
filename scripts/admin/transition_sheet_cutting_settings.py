"""Plan explicit cutting settings; apply an unchanged reviewed plan with audited CAS writes.

Never converts frozen BOM, purchase, receipt, inventory, delivery, or cost facts.
Ambiguous non-die-cut legacy output greater than one stays unchanged.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app.services.requisition_quantities import cutting_factor, normalize_cutting_mode
from app.services.box_type_rules import box_type_code
from app.services.sheet_cutting_settings import SheetCuttingSettings
from scripts.admin.audit_cutting_mold_transition import PRODUCT_FIELDS


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def readonly(path):
    db = sqlite3.connect(path.resolve(strict=True).as_uri() + '?mode=ro', uri=True, timeout=3)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    return db


def build_plan(database, confirmed_die_product_ids=()):
    confirmed_die_product_ids = set(confirmed_die_product_ids)
    products, review, skipped, orders = [], [], [], []
    with readonly(database) as db:
        db.execute('BEGIN')
        assert [r[0] for r in db.execute('SELECT version_num FROM alembic_version')] == ['eg1008sc']
        fields = ','.join('p.' + field for field in PRODUCT_FIELDS)
        for row in db.execute(f'SELECT {fields},p.sheet_cutting_settings,p.customer_id,c.name customer_name FROM products p JOIN customers c ON c.id=p.customer_id ORDER BY p.id'):
            before = dict(row)
            if before['sheet_cutting_settings'] is not None:
                skipped.append({'product_id': row['id'], 'reason': 'already_v2'})
                continue
            if row['supply_mode'] == 'external_purchase' or row['is_virtual_composite_parent'] or row['box_style'] == 'BOM组合':
                skipped.append({'product_id': row['id'], 'reason': 'no_own_sheet'})
                continue
            try:
                old = cutting_factor(normalize_cutting_mode(row['default_cutting_mode'], strict=True))
            except ValueError:
                review.append({'before': before, 'reason': 'invalid_legacy_mode'})
                continue
            die = '模切' in {part.strip() for part in re.split(r'[,，、;；]', row['production_process'] or '')}
            owner_confirmed_die = row['id'] in confirmed_die_product_ids
            if owner_confirmed_die:
                die = True
            if not die and old > 1:
                review.append({'before': before, 'reason': 'non_die_multi_output'})
                continue
            registered = {int(r[0]) for r in db.execute('SELECT mold_max_yield_per_sheet FROM product_bom_components WHERE component_product_id=? AND is_die_cut=1 AND mold_max_yield_per_sheet IS NOT NULL', (row['id'],))}
            if die and any(value != old for value in registered):
                review.append({'before': before, 'reason': 'bom_mold_count_conflict'})
                continue
            part = SheetCuttingSettings(1, 1, old if die else 1, die).to_dict()
            keys = ('cover', 'base') if box_type_code(row['box_style']) == 'a3_set' else ('whole',)
            settings = {'schema_version': 2, **{key: dict(part) for key in keys}}
            updates = {'sheet_cutting_settings': settings, 'default_cutting_mode': '一开一'}
            if owner_confirmed_die:
                updates['production_process'] = ','.join([part.strip() for part in re.split(r'[,，、;；]', row['production_process'] or '') if part.strip() and part.strip() != '模切'] + ['模切'])
            products.append({'before': before, 'updates': updates})
        planned = {row['before']['id']: row for row in products}
        frozen = set()
        for table, field in (
            ('material_requisition_items', 'order_item_id'), ('supplier_requisition_order_items', 'order_item_id'),
            ('incoming_receipt_items', 'order_item_id'), ('production_completions', 'order_item_id'),
            ('sales_delivery_items', 'order_item_id'), ('sales_order_item_bom_components', 'sales_order_item_id'),
            ('order_bom_graphs', 'order_item_id')):
            frozen.update(r[0] for r in db.execute(f'SELECT DISTINCT "{field}" FROM "{table}" WHERE "{field}" IS NOT NULL'))
        order_skips = Counter()
        for row in db.execute('SELECT i.*,o.status order_status FROM sales_order_items i JOIN sales_orders o ON o.id=i.order_id ORDER BY i.id'):
            if row['product_id'] not in planned or row['sheet_cutting_settings_snapshot'] is not None:
                continue
            if row['id'] in frozen or row['order_status'] not in ('pending_confirmation', 'pending_production') or row['is_force_closed'] or row['requisition_status'] != '未报料' or row['material_status'] != 'pending':
                order_skips['historical_or_frozen'] += 1
                continue
            master = planned[row['product_id']]
            try:
                if cutting_factor(row['special_process']) != cutting_factor(master['before']['default_cutting_mode']):
                    raise ValueError('legacy yield differs')
            except ValueError:
                order_skips['order_master_yield_conflict'] += 1
                continue
            orders.append({'before': dict(row), 'settings': master['updates']['sheet_cutting_settings']})
    return {'schema': 1, 'revision': 'eg1008sc', 'confirmed_die_product_ids': sorted(confirmed_die_product_ids), 'products': products, 'orders': orders,
            'review': review, 'skipped_products': skipped, 'skipped_orders': dict(order_skips)}


def apply_plan(database, plan, backup, backup_hash):
    assert database.resolve() != backup.resolve() and digest(backup) == backup_hash
    with readonly(backup) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    assert build_plan(database, plan.get('confirmed_die_product_ids', ())) == plan, '资料已变化，必须重新生成转换清单'
    os.environ['ERP_DATABASE_PATH'] = str(database.resolve())
    from sqlalchemy.orm import Session
    from app.core.database import create_sqlite_engine
    from app.models.product import Product
    from app.models.order import OrderItem
    from app.models.audit import OperationLog
    from app.services.master_data_versioning import apply_versioned_update
    engine = create_sqlite_engine(database)
    actor = SimpleNamespace(id=None, username='sheet-cutting-v2-transition', role='system')
    checksum = hashlib.sha256(canonical(plan).encode('utf-8')).hexdigest()
    with Session(engine) as db:
        # A short deployment maintenance transaction; no per-row commits.
        db.connection().exec_driver_sql('BEGIN IMMEDIATE')
        assert build_plan(database, plan.get('confirmed_die_product_ids', ())) == plan, '转换事务开始前资料已变化，已取消写入'
        for change in plan['products']:
            before = change['before']
            product = db.get(Product, before['id'])
            if product.sheet_cutting_settings is not None:
                raise RuntimeError('转换状态已变化')
            apply_versioned_update(db, object_type='product', entity=product, updates=change['updates'],
                expected_version=before['version'], user=actor, action='system_consistency_fix',
                reason='老板确认：供应商开料默认一开一，原模切每张产出转独立模数', source='CUTTING-MOLD-CONTRACT-20261008:' + checksum)
        for change in plan['orders']:
            item = db.get(OrderItem, change['before']['id'])
            if item.sheet_cutting_settings_snapshot is not None:
                raise RuntimeError('订单转换状态已变化')
            item.sheet_cutting_settings_snapshot = change['settings']
            db.add(OperationLog(user_id=None, username=actor.username, role='system',
                action='SHEET_CUTTING_TRANSITION', resource='orders', entity_type='order_item', entity_id=item.id,
                description='未报料且无历史业务事实的订单启用独立开料与模数',
                details=canonical({'plan_sha256': checksum, 'before': None, 'after': change['settings']})))
        db.flush()
        assert db.connection().exec_driver_sql('PRAGMA foreign_key_check').all() == []
        db.commit()
    engine.dispose()
    return {'products_updated': len(plan['products']), 'unreported_orders_updated': len(plan['orders']),
            'pending_review': len(plan['review']), 'plan_sha256': checksum}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True, type=Path)
    parser.add_argument('--plan-output', type=Path)
    parser.add_argument('--apply-plan', type=Path)
    parser.add_argument('--backup', type=Path)
    parser.add_argument('--backup-sha256')
    parser.add_argument('--confirmed-die-products', default='', help='老板明确确认的产品ID，逗号分隔；不能按类别猜填')
    args = parser.parse_args()
    database = args.database.resolve(strict=True)
    if args.apply_plan:
        if not args.backup or not args.backup_sha256:
            parser.error('正式或副本应用均需要已验证备份和SHA256')
        plan = json.loads(args.apply_plan.read_text(encoding='utf-8'))
        print(canonical(apply_plan(database, plan, args.backup, args.backup_sha256)))
    else:
        if not args.plan_output or args.plan_output.suffix.lower() != '.json':
            parser.error('请指定一个不存在的独立JSON计划文件')
        confirmed = [int(value) for value in args.confirmed_die_products.split(',') if value.strip()]
        plan = build_plan(database, confirmed)
        with args.plan_output.open('x', encoding='utf-8') as out:
            json.dump(plan, out, ensure_ascii=False, indent=2)
        print(canonical({'products': len(plan['products']), 'unreported_orders': len(plan['orders']),
            'pending_review': len(plan['review']), 'skipped_orders': plan['skipped_orders']}))


if __name__ == '__main__':
    main()
