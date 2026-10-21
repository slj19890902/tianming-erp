"""Prepare the exact, user-authorized current-price and YG/GY repair manifest.

This reads only the specified isolated database and prior workbook audit JSON.
No production connection, stock quantity rewrite or historical price backfill.
"""
import argparse,json,sqlite3,hashlib
from pathlib import Path


def prepare(database,audit_path,prior_path):
    audit=json.loads(audit_path.read_text());prior=json.loads(prior_path.read_text())
    with sqlite3.connect(database.as_uri()+'?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row
        products=[dict(r) for r in db.execute('SELECT id,customer_id,product_code,is_active,is_internal_component,deleted_at,purged_at,version,sale_unit_price,sale_unit_price_no_tax FROM products WHERE customer_id IN(137,138)')]
        changes=[];exceptions=[]
        for name,cid in (('光洋',138),('研光',137)):
            data=audit['customers'][name]
            references={}
            for code,rows in data['reference_rows'].items():
                values={r['price'] for r in rows if r['price'] is not None}
                if len(values)==1:references[code]=(next(iter(values)),rows)
                else:exceptions.append(dict(customer_id=cid,code=code,reason='R列缺价或同码冲突',rows=rows))
            for code,rows in data['inventory_only'].items():
                values={r['price'] for r in rows if r['price'] is not None}
                if len(values)==1:references[code]=(next(iter(values)),rows)
            for code,(price,rows) in sorted(references.items()):
                matched=[p for p in products if p['customer_id']==cid and p['product_code']==code and p['is_active'] and not p['is_internal_component'] and not p['deleted_at'] and not p['purged_at']]
                if len(matched)!=1:
                    exceptions.append(dict(customer_id=cid,code=code,reason='当前有效主产品不能唯一匹配',product_ids=[p['id'] for p in matched]));continue
                p=matched[0]
                from decimal import Decimal
                if all(p[f] is not None and Decimal(str(p[f]))==Decimal(price) for f in ('sale_unit_price','sale_unit_price_no_tax')):continue
                changes.append(dict(product_id=p['id'],customer_id=cid,code=code,version=p['version'],price=price,
                    before={k:p[k] for k in ('sale_unit_price','sale_unit_price_no_tax')},source=rows))
        groups=[]
        for row in prior['groups']:
            members=[dict(r) for r in db.execute('SELECT product_id,customer_id FROM shared_finished_members WHERE group_id=? ORDER BY product_id',(row['group_id'],))]
            expected={row['source_id'],row['target_id']}
            if {r['product_id'] for r in members}!=expected:raise ValueError('原共用组成员变化，须重新核对')
            group=dict(db.execute('SELECT id,version,enabled FROM shared_finished_groups WHERE id=?',(row['group_id'],)).fetchone())
            if not group['enabled']:
                exceptions.append(dict(group_id=group['id'],reason='原组已暂停，本次不重新开启'));continue
            groups.append(dict(group_id=group['id'],version=group['version'],product_ids=sorted(expected),
                source_id=row['source_id'],target_id=row['target_id'],code=row['code']))
        plan=dict(schema='tianming.shared-catalog-release-repair.v2',
            source_database_sha256=hashlib.sha256(database.read_bytes()).hexdigest(),
            source_revision=db.execute('SELECT version_num FROM alembic_version').fetchone()[0],
            workbook_audit_sha256=hashlib.sha256(audit_path.read_bytes()).hexdigest(),
            workbook_sha256={k:v for k,v in audit['source_sha256'].items() if k.endswith('.xlsx')},
            price_changes=changes,groups=groups,current_material_corrections=prior['current_material_corrections'],
            product_versions={str(p['id']):p['version'] for p in products if p['id'] in
                ({identity for g in groups for identity in g['product_ids']}|{3884,3885})},
            alternative_molds=dict(parent_id=3581,peer_id=3524,default_child_id=3885,
                expected_children=[3884,3885],evidence='老板确认82021154同一成品，3模/4模两种模具；并非A+B各一片组套'),
            order_id=9864,order_number='TM20261021003',exceptions=exceptions,
            boundary='仅当前主档、已明确共用资格和原订单真实缺口补占；库存总量/成本/货位及历史金额不改')
    plan['sha256']=hashlib.sha256(json.dumps(plan,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return plan


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--database',type=Path,required=True);p.add_argument('--evidence-dir',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();result=prepare(args.database.resolve(),args.evidence_dir/'price-source-audit-20261021.json',args.evidence_dir/'customer-sharing-repair-review.json')
    with args.output.open('x') as f:json.dump(result,f,ensure_ascii=False,indent=2)
    print(json.dumps(dict(price_changes=len(result['price_changes']),groups=len(result['groups']),exceptions=len(result['exceptions']),sha256=result['sha256'])))
