"""Reviewed exact-scope maintenance. Caller owns cold backup and release lock.

No command-line write entry. A reviewed plan made on the isolated copy must
match byte-for-byte before each transaction action. No inventory is enrolled.
"""
import hashlib
import json
import os
from pathlib import Path
from sqlalchemy import select
from app.models.product import Product
from app.models.product_bom import ProductBomComponent
from app.models.shared_finished_stock import SharedFinishedPolicy
from app.services import shared_finished_stock as shared

PAIRS=((3833,3836),(3834,3837),(3660,3594))
EVIDENCE='老板2026-10-09明确启用研光/光洋80011946跨客户BOM共用；长片、短片和整套分别共用；每套11长片+6短片，原子件只/片为1:1名称。保持原归属、配方、数量、成本及权限。'
KEY='bom-shared-80011946-20261009'


def signature(value):
    return hashlib.sha256(shared._json(value).encode()).hexdigest()


def apply(db,expected=None):
    """Execute inside caller transaction; expected=None is isolated planning only."""
    if expected is None:
        assert os.environ.get('ERP_ENVIRONMENT')=='test'
        assert Path(db.get_bind().url.database).resolve().is_relative_to(Path(os.environ['ERP_UAT_ROOT']).resolve())
    products={pid:db.get(Product,pid) for pair in PAIRS for pid in pair}
    assert all(p and p.product_code=='80011946' for p in products.values())
    assert all(products[a].customer_id==137 and products[b].customer_id==138 for a,b in PAIRS)
    for parent,long,short in ((3660,3833,3834),(3594,3836,3837)):
        assert {r.component_product_id:int(r.quantity_per_set) for r in db.scalars(select(ProductBomComponent).where(ProductBomComponent.parent_product_id==parent))}=={long:11,short:6}
    result=[]
    for index,pair in enumerate(PAIRS):
        value=shared.preview(db,product_ids=pair,lot_ids=[])
        if expected is not None:
            assert signature(value)==expected[index]['preview_sha256'],'正式BOM资料已变化，停止启用'
        saved=shared._apply_confirmed(db,product_ids=pair,lot_ids=[],preview_hash=value['preview_hash'],
            operation_key='bom-shared-80011946-20261009-'+str(index),evidence=EVIDENCE,actor=None,source='script')
        db.add(SharedFinishedPolicy(group_id=saved['group_id'],auto_enroll=True));db.flush()
        result.append(dict(product_ids=list(pair),group_id=saved['group_id'],preview_sha256=signature(value),
            product_versions={str(r['product_id']):r['version'] for r in value['products']},auto_enroll=True))
    if expected is not None:assert result==expected
    return result


def apply_reviewed(db,*,expected,backup_receipt):
    import re
    from app.services.bom_transactions import atomic_bom
    from app.services.audit_log import append_audit_event
    from app.models.shared_finished_stock import SharedFinishedMutation
    if not (backup_receipt.get('verified') is True and re.fullmatch('[0-9a-f]{64}',backup_receipt.get('sha256',''))):
        raise shared._error('缺少已验证的本次停服备份')
    request=shared._json(expected)
    with atomic_bom(db):
        previous=db.scalar(select(SharedFinishedMutation).where(SharedFinishedMutation.operation_key==KEY))
        if previous:
            if previous.request_json!=request:raise shared._error('BOM共用维护编号与固定清单不一致')
            return json.loads(previous.result_json)
        groups=apply(db,expected)
        result=dict(groups=groups,backup_sha256=backup_receipt['sha256'],inventory_changed=False)
        db.add(SharedFinishedMutation(operation_key=KEY,group_id=groups[0]['group_id'],request_json=request,result_json=shared._json(result)))
        append_audit_event(db,event_category='business',result='success',source='script',module_code='warehouse',
            action_code='enable_shared_bom',resource='products',entity_type='shared_finished_group',entity_id=groups[0]['group_id'],
            actor=None,operator_name='老板明确授权的发布维护任务',
            details=dict(plan_sha256=signature(expected),backup_sha256=backup_receipt['sha256'],auto_enroll=True,groups=groups,evidence=EVIDENCE))
        db.flush();return result
