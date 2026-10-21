"""Reviewed correction of a false A+B template into alternative molds.

Only the current template/master changes. Existing batches, reservations and
frozen order graphs retain their original quantities, identity and provenance.
No web endpoint exposes this administrative data correction.
"""
import json
from sqlalchemy import select
from app.models.product import Product
from app.models.product_bom import ProductBomComponent
from app.models.multilevel_bom import ProductBomProfile
from app.models.shared_finished_stock import SharedFinishedMember,SharedFinishedMutation
from app.services.shared_customer_access import canonical,digest,PHYSICAL_FIELDS
from app.services import shared_product_master as master
from app.services.bom_transactions import atomic_bom
from app.services.shared_finished_stock import _error


def preview(db,parent_id,peer_id,default_child_id):
    parent,peer=db.get(Product,parent_id),db.get(Product,peer_id)
    if not parent or not peer or parent.product_code!=peer.product_code or parent.customer_id==peer.customer_id:
        raise _error('须核对两个客户现有同编码产品')
    members=[db.get(SharedFinishedMember,p.id) for p in (parent,peer)]
    if any(m is None for m in members) or members[0].group_id!=members[1].group_id:
        raise _error('须沿用原共用组的稳定产品身份')
    profile=db.get(ProductBomProfile,parent.id)
    if not parent.is_composite or not profile or profile.source!='assembled' or peer.is_composite:
        raise _error('原资料已变化，不符合本次已核实的误建组套情形')
    edges=list(db.scalars(select(ProductBomComponent).where(ProductBomComponent.parent_product_id==parent.id)
        .order_by(ProductBomComponent.display_order)))
    if len(edges)!=2 or any(e.quantity_per_set!=1 for e in edges):raise _error('原配方已变化，不能沿用本次修正依据')
    children=[db.get(Product,e.component_product_id) for e in edges]
    if default_child_id not in {p.id for p in children if p}:raise _error('默认方案不属于原资料')
    variants=[]
    from app.services.sheet_cutting_settings import component_settings
    for child in children:
        if not child or not child.is_active or child.is_composite or not child.is_internal_component or not child.mold_tool_id:
            raise _error('原子件状态或模具已变化，需重新核对')
        settings=component_settings(child.sheet_cutting_settings)
        if settings is None:raise _error('原模具方案缺少模数或分切资料，须补齐明确值')
        variants.append(dict(id='mold-'+str(child.mold_tool_id),name=str(settings.mold_count)+'模',
            mold_tool_id=child.mold_tool_id,mold_count=settings.mold_count,
            length_parts=settings.length_parts,width_parts=settings.width_parts,
            report_length_mm=child.report_length_mm,report_width_mm=child.report_width_mm))
    variants.sort(key=lambda v:v['mold_tool_id']!=db.get(Product,default_child_id).mold_tool_id)
    from app.services.production_variants import normalize_variants,validate_references
    variants=normalize_variants(variants)
    for p in (parent,peer):validate_references(db,p.customer_id,variants)
    default=db.get(Product,default_child_id)
    updates={key:getattr(default,key) for key in PHYSICAL_FIELDS if key not in ('die_cut_path','production_variants')}
    updates.update(is_composite=False,is_internal_component=False,is_virtual_composite_parent=False,
        unit=peer.unit,production_variants=variants,combination_mode='parent_priced_set',composite_fulfillment_mode='parent_delivery')
    result=dict(parent_id=parent_id,peer_id=peer_id,default_child_id=default_child_id,
        group_id=members[0].group_id,parent_version=parent.version,peer_version=peer.version,
        children=[dict(id=p.id,version=p.version,basis=master.shared.product_basis(p)) for p in children],
        current_edges=[dict(id=e.id,child=e.component_product_id,quantity=str(e.quantity_per_set)) for e in edges],
        updates=json.loads(canonical(updates)))
    result['sha256']=digest(result)
    return result


def apply(db,*,plan,actor,operation_key,evidence):
    if not actor or not actor.is_active or actor.role!='admin' or not evidence.strip():raise _error('仅活动管理员可按明确事实修正',403)
    request=canonical(dict(plan=plan,evidence=evidence,actor_id=actor.id))
    with atomic_bom(db):
        old=db.scalar(select(SharedFinishedMutation).where(SharedFinishedMutation.operation_key==operation_key))
        if old:
            if old.request_json!=request:raise _error('修正标识已用于其它内容')
            return json.loads(old.result_json)
        actual=preview(db,plan['parent_id'],plan['peer_id'],plan['default_child_id'])
        if actual['sha256']!=plan['sha256']:raise _error('当前资料已变化，本次修正未执行')
        from app.api.deps import require_customer_access
        from app.services.master_data_versioning import _apply_versioned_update,preview_versioned_update
        for pid in (plan['parent_id'],plan['peer_id']):
            p=db.get(Product,pid);require_customer_access(p.customer_id,current_user=actor,db=db)
            # Use typed master values; the manifest encodes Decimal as strings.
            default=db.get(Product,plan['default_child_id'])
            updates={k:getattr(default,k) for k in PHYSICAL_FIELDS if k not in ('die_cut_path','production_variants')}
            updates.update({k:v for k,v in plan['updates'].items() if k not in updates})
            check=preview_versioned_update(db,object_type='product',entity=p,updates=updates,expected_version=p.version,user=actor)
            _apply_versioned_update(db,object_type='product',entity=p,updates=updates,expected_version=p.version,user=actor,
                reason=evidence,source='reviewed_alternative_molds',confirmation_token=check['confirmation_token'])
        from app.services.composite_bom import _replace_product_bom
        parent=db.get(Product,plan['parent_id'])
        _replace_product_bom(db,parent_product_id=parent.id,components=[],expected_version=parent.version,user=actor,
            change_reason=evidence,inventory_mode='manufactured',material_mode='expand_children',delivery_mode='parent',preserve_parent_unit=True)
        # Legacy product IDs remain available to their frozen order graphs.
        for row in plan['children']:
            child=db.get(Product,row['id'])
            if not child.is_internal_component:
                check=preview_versioned_update(db,object_type='product',entity=child,updates={'is_internal_component':True},expected_version=child.version,user=actor)
                _apply_versioned_update(db,object_type='product',entity=child,updates={'is_internal_component':True},expected_version=child.version,user=actor,
                    reason='保留历史子件身份，仅停用错误的未来必需组套关系',source='reviewed_alternative_molds',confirmation_token=check['confirmation_token'])
        policy=master.preview_reconcile(db,parent.id)
        master.reconcile(db,product_id=parent.id,preview_hash=policy['sha256'],operation_key=operation_key+'-stock',evidence=evidence,actor=actor)
        result=dict(parent_id=parent.id,peer_id=plan['peer_id'],variants=plan['updates']['production_variants'],stock_quantity_writes=False)
        db.add(SharedFinishedMutation(group_id=plan['group_id'],operation_key=operation_key,request_json=request,result_json=canonical(result)))
        db.flush();return result
