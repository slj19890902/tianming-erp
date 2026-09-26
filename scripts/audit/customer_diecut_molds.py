"""Read-only YG/GY/YL die-cut mold registration preview; never writes ERP."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from collections import Counter
from pathlib import Path
from urllib.parse import quote

CUSTOMERS = ('YG', 'GY', 'YL')
LOCATION = '1F-M-R04'


def build_plan(db):
    from app.services.mold_identity import (
        normalize_mold_label_name, normalize_mold_chinese_short_name, MoldIdentityError,
    )
    db.row_factory = sqlite3.Row
    products = [dict(row) for row in db.execute('''
        SELECT p.id, p.version, p.customer_id, c.customer_code,
               c.chinese_short_name, p.product_code, p.customer_material_code, p.product_name,
               p.box_category, p.production_process, p.is_active, p.deleted_at,
               p.mold_tool_id
        FROM products p JOIN customers c ON c.id=p.customer_id
        WHERE c.customer_code IN ('YG','GY','YL') ORDER BY c.customer_code,p.id
    ''')]
    molds = {row['id']: dict(row) for row in db.execute('''
        SELECT id,mold_code,mold_name,label_name,chinese_short_name,rack_location,
               version,location_version,is_active,archive_status FROM mold_tools
    ''')}
    links = {}
    for row in db.execute('SELECT mold_tool_id,customer_id FROM mold_tool_customers'):
        links.setdefault(row[0], set()).add(row[1])
    bindings = {}
    for row in db.execute('SELECT id,mold_tool_id FROM products WHERE mold_tool_id IS NOT NULL'):
        bindings.setdefault(row[1], []).append(row[0])
    def inventory_code(product):
        return product['customer_material_code'] or product['product_code']

    sku_counts = Counter((p['customer_id'], inventory_code(p)) for p in products
                         if p['is_active'] and not p['deleted_at'])
    rows = []
    for product in products:
        if product['box_category'] != 'die_cut' and '模切' not in (product['production_process'] or ''):
            continue
        row = dict(product, proposed_location=LOCATION,
                   label_name=inventory_code(product), short_name=product['product_name'])
        reasons = []
        if not product['is_active'] or product['deleted_at']:
            reasons.append('停用或已删除产品')
        if not product['chinese_short_name']:
            reasons.append('缺少正式客户中文简称')
        if sku_counts[(product['customer_id'], inventory_code(product))] > 1:
            reasons.append('客户存货编码非唯一')
        try:
            normalize_mold_label_name(row['label_name'])
            normalize_mold_chinese_short_name(row['short_name'])
        except MoldIdentityError as error:
            reasons.append(str(error))
        candidates = [m for m in molds.values()
                      if m['label_name'] == inventory_code(product)
                      and product['customer_id'] in links.get(m['id'], set())]
        bound = molds.get(product['mold_tool_id'])
        if product['mold_tool_id'] and bound is None:
            reasons.append('模具绑定不存在')
        if bound:
            row['existing_mold'] = bound
            row['bound_product_ids'] = bindings.get(bound['id'], [])
            if len(row['bound_product_ids']) > 1:
                reasons.append('共享模具，需逐款核对后保留一个本体')
            if not bound['is_active'] or bound['archive_status'] != 'active':
                reasons.append('已绑定模具停用或封存')
            row['action'] = 'reuse_bound'
        elif len(candidates) == 1:
            row['existing_mold'] = candidates[0]
            row['action'] = 'bind_existing'
            if not candidates[0]['is_active'] or candidates[0]['archive_status'] != 'active':
                reasons.append('同客户同标签模具停用或封存')
            if bindings.get(candidates[0]['id']):
                reasons.append('同标签模具已绑定其他产品，需核对共享关系')
        elif candidates:
            row['action'] = 'review'
            reasons.append('同客户同标签存在多个模具')
            row['candidate_mold_ids'] = [m['id'] for m in candidates]
        else:
            row['action'] = 'create_and_bind'
            row['idempotency_key'] = f'customer-diecut-mold:{product["customer_id"]}:{product["id"]}'
            # Legacy names are collision evidence only, never proof of identity.
            # An unlinked old mold must be reviewed rather than duplicated.
            token = re.compile(r'(?<![A-Za-z0-9])' + re.escape(str(inventory_code(product))) + r'(?![A-Za-z0-9])')
            collisions = [m['id'] for m in molds.values() if token.search(
                str(m['mold_code']) + ' ' + str(m['mold_name']))]
            if collisions:
                row['candidate_mold_ids'] = collisions
                reasons.append('未关联旧模具名称或编号含相同存货编码，需核对')
        if reasons:
            row['action'] = 'review'
        row['review_reasons'] = reasons
        rows.append(row)
    encoded = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return {'schema': 1, 'customers': list(CUSTOMERS), 'location': LOCATION,
            'source_fingerprint': hashlib.sha256(encoded.encode()).hexdigest(),
            'summary': dict(Counter(r['action'] for r in rows)), 'items': rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    source = args.database.resolve(strict=True)
    if args.output.resolve() == source:
        raise ValueError('output must not overwrite the source database')
    with sqlite3.connect(f'file:{quote(source.as_posix(), safe="/:")}?mode=ro', uri=True) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        result = build_plan(db)
    result['database'] = str(source)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'summary': result['summary'], 'fingerprint': result['source_fingerprint']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
