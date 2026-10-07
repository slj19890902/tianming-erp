"""Owner-approved, fixed-scope correction; preview by default, never sync all history."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import time

BATCH = 'customer-drawings-147-20261007'
SNAPSHOT = 'customer_document_snapshot_json'
TARGETS = ((3428, '82020083', 10487, 851, '632264', '0632264'),
           (3411, '80012632', 10492, 856, '9620185-1', '9620185'))


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def row(db, table, identity):
    cursor = db.execute(f'SELECT * FROM "{table}" WHERE id=?', (identity,))
    result = cursor.fetchone()
    require(result is not None, f'Missing {table}/{identity}')
    return dict(zip((column[0] for column in cursor.description), result))


def build_plan(db):
    order = row(db, 'sales_orders', 9777)
    delivery = row(db, 'sales_deliveries', 147)
    require(order['customer_id'] == delivery['customer_id'] == 137, 'Customer scope changed')
    require(delivery['delivery_number'] == 'YG-20261007-001'
            and delivery['status'] == 'dispatched' and delivery['version'] == 1,
            'Delivery status/version changed')
    changes, products = [], []
    for product_id, code, order_item_id, delivery_item_id, before, after in TARGETS:
        product = row(db, 'products', product_id)
        require(product['customer_id'] == 137 and product['customer_material_code'] == code
                and product['customer_drawing_number'] == after, 'Approved master drawing changed')
        products.append({key: product[key] for key in
                         ('id', 'customer_id', 'customer_material_code', 'customer_drawing_number', 'version')})
        for table, item_id in (('sales_order_items', order_item_id), ('sales_delivery_items', delivery_item_id)):
            item = row(db, table, item_id)
            if table == 'sales_order_items':
                require(item['order_id'] == 9777 and item['product_id'] == product_id,
                        'Order line identity changed')
            else:
                require(item['delivery_id'] == 147 and item['order_item_id'] == order_item_id
                        and item['source_type'] == 'order' and item['product_id'] is None
                        and item['is_current'] == 1,
                        'Delivery line identity changed')
            snapshot = json.loads(item[SNAPSHOT])
            require(snapshot.get('schema_version') == 1 and snapshot.get('product_id') == product_id
                    and snapshot.get('customer_material_code') == code
                    and snapshot.get('customer_drawing_number') == before, 'Frozen identity changed')
            snapshot['customer_drawing_number'] = after
            changes.append({'table': table, 'id': item_id, 'before_row': item,
                            'after_snapshot': encoded(snapshot)})
    plan = {'batch': BATCH, 'order': order, 'delivery': delivery,
            'products': products, 'changes': changes}
    return {**plan, 'sha256': digest(plan)}


def protected_facts(db):
    """Hash every business cell except the four exact snapshots, version and new audit."""
    ignored = {('sales_order_items', item): SNAPSHOT for _, _, item, _, _, _ in TARGETS}
    ignored.update({('sales_delivery_items', item): SNAPSHOT for _, _, _, item, _, _ in TARGETS})
    ignored[('sales_deliveries', 147)] = 'version'
    facts = {}
    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    for table in tables:
        quoted = '"' + table.replace('"', '""') + '"'
        cursor = db.execute('SELECT * FROM ' + quoted)
        names = [c[0] for c in cursor.description]
        hashes = []
        for values in cursor:
            values = dict(zip(names, values))
            if table == 'operation_logs' and values.get('batch_id') == BATCH:
                continue
            omit = ignored.get((table, values.get('id')))
            if omit:
                values[omit] = '<approved-cell>'
            for key, value in values.items():
                if isinstance(value, bytes):
                    values[key] = {'bytes_sha256': hashlib.sha256(value).hexdigest()}
            hashes.append(digest(values))
        facts[table] = {'rows': len(hashes), 'sha256': digest(sorted(hashes))}
    facts['schema'] = digest(list(db.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name')))
    return facts


def apply_plan(db, expected_sha, backup_evidence):
    """Caller must first verify its backup; lock, CAS, audit and invariant checks are atomic."""
    require(not db.in_transaction, 'Existing transaction is not allowed')
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('BEGIN IMMEDIATE')
    try:
        previous = db.execute('SELECT details FROM operation_logs WHERE batch_id=?', (BATCH,)).fetchall()
        if previous:
            require(len(previous) == 1, 'Duplicate repair audit')
            receipt = json.loads(previous[0][0])
            require(receipt['plan']['sha256'] == expected_sha, 'Repair already applied with another plan')
            db.rollback()
            return {'status': 'already_applied', 'plan_sha256': expected_sha}
        plan = build_plan(db)
        require(plan['sha256'] == expected_sha, 'Plan changed; preview again')
        before = protected_facts(db)
        for change in plan['changes']:
            cursor = db.execute(f'UPDATE {change["table"]} SET {SNAPSHOT}=? WHERE id=? AND {SNAPSHOT}=?',
                                (change['after_snapshot'], change['id'], change['before_row'][SNAPSHOT]))
            require(cursor.rowcount == 1, 'Snapshot version conflict')
            expected = {**change['before_row'], SNAPSHOT: change['after_snapshot']}
            require(row(db, change['table'], change['id']) == expected, 'Unexpected line mutation')
        cursor = db.execute('UPDATE sales_deliveries SET version=2 WHERE id=147 AND version=1 AND customer_id=137 AND status=\'dispatched\'')
        require(cursor.rowcount == 1, 'Delivery version conflict')
        audit_plan = {'sha256': plan['sha256'], 'batch': BATCH, 'order_id': 9777, 'delivery_id': 147,
                      'changes': [{'table': c['table'], 'id': c['id'],
                                   'before_snapshot': c['before_row'][SNAPSHOT],
                                   'after_snapshot': c['after_snapshot']} for c in plan['changes']]}
        details = encoded({'owner_request': 'Fix the diagnosed historical drawing values',
                           'plan': audit_plan, 'backup': backup_evidence, 'version_before': 1, 'version_after': 2})
        audit = db.execute('''INSERT INTO operation_logs
            (action,resource,details,entity_type,entity_id,description,event_category,result,source,
             module_code,action_code,operator_name_snapshot,object_ref,customer_id_snapshot,batch_id,schema_version)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            ('repair', 'sales_deliveries/147', details, 'delivery', 147,
             '老板授权更正82020083与80012632的订单及送货冻结图号；数量、金额和库存不变',
             'business', 'success', 'script', 'deliveries', 'delivery.customer_drawing_repaired',
             'Codex（老板授权）', 'YG-20261007-001', 137, BATCH, 1))
        require(protected_facts(db) == before, 'Unapproved business facts changed; rollback')
        require(not db.execute('PRAGMA foreign_key_check').fetchall(), 'Foreign key failure')
        require(db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok', 'Integrity failure')
        db.commit()
        return {'status': 'applied', 'plan_sha256': expected_sha, 'audit_id': audit.lastrowid,
                'snapshots_changed': 4, 'delivery_version': 2, 'protected_tables': len(before) - 1}
    except BaseException:
        db.rollback()
        raise


def verify_backup(receipt_path):
    receipt_path = Path(receipt_path)
    receipt = json.loads(receipt_path.read_text(encoding='utf-8-sig'))
    require(receipt.get('verified') is True and receipt.get('storage') == 'nas', 'Verified NAS backup required')
    require(0 <= time.time() - receipt_path.stat().st_mtime < 3600, 'Fresh backup receipt required')
    backup = Path(receipt['path'])
    hasher = hashlib.sha256()
    with backup.open('rb') as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            hasher.update(chunk)
    require(hasher.hexdigest() == receipt['sha256'] and backup.stat().st_size == receipt['size'], 'Backup changed')
    require(receipt['database']['revision'] == 'ef1007cp', 'Backup schema mismatch')
    return {'path': str(backup), 'sha256': receipt['sha256'], 'verified': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--apply-plan-sha')
    parser.add_argument('--backup-receipt', type=Path)
    args = parser.parse_args()
    mode = 'rw' if args.apply_plan_sha else 'ro'
    backup = None
    if args.apply_plan_sha:
        require(args.backup_receipt is not None, 'Backup receipt required for apply')
        backup = verify_backup(args.backup_receipt)
    with sqlite3.connect(args.database.resolve().as_uri() + '?mode=' + mode, uri=True) as db:
        result = apply_plan(db, args.apply_plan_sha, backup) if args.apply_plan_sha else build_plan(db)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
