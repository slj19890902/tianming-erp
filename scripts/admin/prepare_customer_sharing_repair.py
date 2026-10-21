"""Read-only review manifest from the narrowly captured YG/GY evidence.

This does not connect to production or execute the corrections. A protected
maintenance transaction must recheck the exact row versions/identities against
its fresh online copy and verified backup before applying an approved manifest.
"""
import argparse
import hashlib
import json
from pathlib import Path


def prepare(directory):
    paths={name:directory/name for name in ('shared-live.json','bom-live.json','live.json')}
    raw={name:path.read_bytes() for name,path in paths.items()}
    shared,bom,live=(json.loads(raw[name]) for name in paths)
    if any(row['formal_writes'] for row in (shared,bom,live)):
        raise ValueError('证据并非只读捕获')
    customers={c['customer_code']:c['id'] for c in shared['customers']}
    expected={customers['YG'],customers['GY']}
    products={p['id']:p for p in shared['products']}
    members={}
    for m in shared['members']:members.setdefault(m['group_id'],[]).append(m)
    groups=[];exceptions=[]
    for group in shared['groups']:
        rows=members.get(group['id'],[])
        if len(rows)!=2 or {r['customer_id'] for r in rows}!=expected:continue
        pair=[products[r['product_id']] for r in rows]
        if pair[0]['product_code']!=pair[1]['product_code']:
            exceptions.append(dict(group_id=group['id'],reason='编码不同，不能纳入本次全部同编码确认'));continue
        source=next(p for p in pair if p['customer_id']==customers['YG'])
        target=next(p for p in pair if p['customer_id']==customers['GY'])
        groups.append(dict(group_id=group['id'],group_version=group['version'],enabled=group['enabled'],code=source['product_code'],
            source_id=source['id'],source_version=source['version'],target_id=target['id'],target_version=target['version'],
            original_member_sha256={str(m['product_id']):hashlib.sha256(m['identity_json'].encode()).hexdigest() for m in rows},
            action='explicit_current_identity_reconciliation',stock_balance_writes=False))
    materials={m['id']:m for m in shared['materials']}
    fixes=[]
    for code in ('80012251','80010611','80012250'):
        row=next(r for r in groups if r['code']==code)
        a,b=products[row['source_id']],products[row['target_id']]
        old,new=materials[b['material_id']],materials[a['material_id']]
        if not new['is_active'] or old['is_active'] or old['code']!=new['code']:
            raise ValueError(code+'材质有效性或同代码证据变化')
        fixes.append(dict(product_id=b['id'],version=b['version'],code=code,old_material_id=old['id'],new_material_id=new['id'],
            reason='同编码共用确认；沿用研光有效材质，光洋原引用已停用同码材质；历史采购和库存不回填'))
    special=next(r for r in groups if r['code']=='82021154')
    special['action']='align_current_bom_then_reconcile'
    special['approved_recipe']=[dict(child_id=c['component_product_id'],quantity_per_set=c['quantity_per_set'])
        for c in bom['components'] if c['parent_product_id']==special['source_id']]
    if {r['child_id'] for r in special['approved_recipe']}!={3884,3885}:raise ValueError('已确认配方变化')
    order=next(o for o in live['orders'] if o['order_number']=='TM20261021003')
    order_items=[i for i in live['order_items'] if i['order_id']==order['id']]
    return dict(schema='tianming.shared-customer-repair-review.v1',status='review_manifest_only_not_applied',
        source_sha256={name:hashlib.sha256(value).hexdigest() for name,value in raw.items()},
        source_capture_at=shared['at'],package=shared['package'],revision=shared['revision'],groups=groups,
        current_material_corrections=fixes,exceptions=exceptions,
        existing_order=dict(id=order['id'],number=order['order_number'],item_ids=[i['id'] for i in order_items],
            instruction='保留已有订单和预占；不得重复导入两份相同16行需求。正式补占须按当前阶段另行预检、仅补真实缺口'),
        required_before_apply=['最新正式副本与精确版本复核','已验证备份及受保护的唯一维护事务','同一事务中逐组CAS、库存数量成本预占不变核对','82021154现有批次冻结配方及旧订单兼容核对'],
        capture_gaps=bom['errors'],formal_writes=False)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--evidence-dir',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();result=prepare(args.evidence_dir)
    with args.output.open('x') as stream:json.dump(result,stream,ensure_ascii=False,indent=2)
    print(json.dumps(dict(status=result['status'],groups=len(result['groups']),material_corrections=len(result['current_material_corrections']),formal_writes=False),ensure_ascii=False))
