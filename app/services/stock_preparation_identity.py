"""Audited physical confirmation for old groups missing a parent identity.

Never infer physical compatibility from a later BOM version. This operation
records an administrator's explicit confirmation; it does not assemble stock.
"""
import json
from sqlalchemy import select
from app.models.product import Product
from app.models.product_bom import ProductBomComponent
from app.models.stock_preparation import StockPreparationCommand as Command
from app.services import stock_preparation as prep
from app.services.stock_preparation_groups import digest, encode
from app.services.finished_stock_identity import product_basis
from app.services.audit_log import append_audit_event


def evidence(jobs):
    snapshots = [json.loads(j.product_snapshot) for j in jobs]
    group = snapshots[0]['preparation_group']
    if any(s.get('preparation_group') != group for s in snapshots):
        prep.fail('组内冻结配方不一致，请核对原记录')
    return group, digest([dict(id=j.id, product_id=j.product_id,
                              snapshot=j.product_snapshot) for j in jobs])


def confirmation_key(group_key):
    return 'prep-identity:' + digest(group_key)[:48]


def confirm_legacy_group(db, *, jobs, parent_version, job_versions, actor,
                         confirmed_same_product, reason):
    if actor.role not in ('admin', 'boss'):
        prep.fail('仅管理员或老板可确认旧配方实物')
    if not confirmed_same_product or not reason.strip():
        prep.fail('请明确确认旧子件仍可按原配方组成同款成品')
    group, fingerprint = evidence(jobs)
    recipe = group['recipe']
    key = confirmation_key(group['key'])
    old = db.get(Command, key)
    request = encode(dict(action='confirm_legacy_identity', group_key=group['key'],
                          fingerprint=fingerprint, parent_version=parent_version,
                          job_versions=job_versions, reason=reason))
    if old:
        if old.request_json != request or old.actor_id != actor.id:
            prep.fail('旧配方已确认，请刷新核对确认记录')
        return json.loads(old.result_json)
    parent = db.get(Product, recipe['parent_id'])
    if (not parent or not parent.is_active or parent.deleted_at or parent.purged_at
            or parent.customer_id != recipe['customer_id'] or parent.version != parent_version):
        prep.fail('父件资料已变化，请重新核对')
    if group.get('parent_basis'):
        prep.fail('任务已有冻结身份，不需要补充确认')
    if ({j.id: j.version for j in jobs} != job_versions
            or any(j.status != 'completed' for j in jobs)):
        prep.fail('子件任务已变化或尚未完工')
    edges = list(db.scalars(select(ProductBomComponent).where(
        ProductBomComponent.parent_product_id == parent.id)))
    expected = {c['product_id']: c['per_set'] for c in recipe['children']}
    if (len(edges) != len(expected) or any(not e.is_required for e in edges)
            or {e.component_product_id: e.quantity_per_set for e in edges} != expected
            or {j.product_id for j in jobs} != set(expected)):
        prep.fail('当前子件或每套用量与原配方不同，不能承接为同款成品')
    for job in jobs:
        snapshot = json.loads(job.product_snapshot)
        if not snapshot.get('physical_basis'):
            prep.fail('旧子件缺少实物身份，不能仅凭配比确认')
    result = dict(action='confirm_legacy_identity', group_key=group['key'],
                  fingerprint=fingerprint, parent_basis=product_basis(parent),
                  parent_version=parent_version, recipe=recipe, reason=reason)
    db.add(Command(operation_key=key, receipt_item_id=jobs[0].receipt_item_id,
                   actor_id=actor.id, request_json=request, result_json=encode(result)))
    append_audit_event(db, event_category='business', result='success', source='web',
                      module_code='production', action_code='stock_preparation.confirm_identity',
                      resource='production', actor=actor, entity_type='product',
                      entity_id=parent.id, details=result)
    db.flush()
    return result


def resolve_parent_basis(db, jobs):
    group, fingerprint = evidence(jobs)
    recipe = group['recipe']
    parent = db.get(Product, recipe['parent_id'])
    if not parent or parent.customer_id != recipe['customer_id']:
        prep.fail('组合客户身份已变化，请核对原配方')
    if group.get('parent_basis'):
        return group['parent_basis']
    confirmed = db.get(Command, confirmation_key(group['key']))
    if confirmed:
        result = json.loads(confirmed.result_json)
        if result.get('fingerprint') != fingerprint or result.get('recipe') != recipe:
            prep.fail('旧配方确认与当前任务不一致，请重新核对')
        return result['parent_basis']
    if parent.version != recipe['version']:
        prep.fail('组合资料已变化，请先核对原配方')
    return product_basis(parent)
