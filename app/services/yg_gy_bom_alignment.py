"""Reviewed current-master alignment of an ordinary peer to an assembled BOM.

No orders, physical stock, prices or reservations are rewritten. The existing
BOM service validates the recipe and the original sharing protocol qualifies
corresponding components before the parents are reconciled.
"""
import hashlib
from app.services.shared_customer_access import canonical
from sqlalchemy import select
from app.models.product import Product
from app.models.product_bom import ProductBomComponent
from app.models.multilevel_bom import ProductBomProfile, ProductBomInventoryRelation
from app.models.shared_finished_stock import SharedFinishedMember, SharedFinishedMutation
from app.services import shared_finished_stock as shared
from app.services import shared_product_master as policy
from app.services.bom_transactions import atomic_bom


def preview(db, source_id, target_id):
    source,target=db.get(Product,source_id),db.get(Product,target_id)
    if not source or not target: raise shared._error('产品不存在',404)
    pair=policy._pair(db,source)
    if (not pair or {p.id for p in pair[1]} != {source_id,target_id}
            or source.customer.customer_code!='YG' or target.customer.customer_code!='GY'
            or source.product_code!=target.product_code):
        raise shared._error('必须沿用研光和光洋同编码的现有共用组')
    profile=db.get(ProductBomProfile,source_id)
    if (not source.is_active or source.deleted_at or not target.is_active or target.deleted_at
            or source.is_internal_component or target.is_internal_component
            or not profile or profile.source!='assembled' or profile.delivery_mode!='parent'):
        raise shared._error('来源须为有效的组套父件，目标不能是子件')
    if db.scalar(select(ProductBomComponent.id).where(ProductBomComponent.parent_product_id==target_id).limit(1)):
        raise shared._error('目标已有配方，须单独核对，不能覆盖')
    edges=list(db.scalars(select(ProductBomComponent).where(ProductBomComponent.parent_product_id==source_id).order_by(ProductBomComponent.display_order)))
    if not edges:raise shared._error('来源缺少配方')
    children=[]
    for edge in edges:
        child=db.get(Product,edge.component_product_id)
        relation=db.get(ProductBomInventoryRelation,edge.id)
        if (not child.is_active or child.deleted_at or child.customer_id!=source.customer_id or child.is_composite
                or not child.is_internal_component or not relation or relation.relation!='assembly'):
            raise shared._error('仅支持已核实的同客户单层组套配方')
        peers=list(db.scalars(select(Product).where(Product.customer_id==target.customer_id,
            Product.product_code==child.product_code,Product.deleted_at.is_(None),Product.purged_at.is_(None))))
        if peers:raise shared._error(f'{child.product_code}目标客户已有产品，需核对稳定身份，不能重复新建')
        children.append(dict(source_id=child.id,version=child.version,code=child.product_code,
            basis=shared.product_basis(child),edge={c.name:getattr(edge,c.name) for c in edge.__table__.columns
                if c.name not in {'created_at','updated_at'}},relation=relation.relation))
    plan=dict(source_id=source_id,target_id=target_id,source_version=source.version,target_version=target.version,
        group_id=pair[0],source_basis=shared.product_basis(source),target_basis=shared.product_basis(target),children=children)
    plan['sha256']=hashlib.sha256(canonical(plan).encode()).hexdigest()
    return plan


def align(db, *, source_id, target_id, preview_hash, operation_key, evidence, actor):
    if actor is None or actor.role!='admin' or not actor.is_active or not evidence.strip():
        raise shared._error('仅活动管理员可按核实配方修正',403)
    request=shared._json(dict(source_id=source_id,target_id=target_id,preview_hash=preview_hash,evidence=evidence,actor_id=actor.id))
    with atomic_bom(db):
        old=db.scalar(select(SharedFinishedMutation).where(SharedFinishedMutation.operation_key==operation_key))
        if old:
            if old.request_json!=request:raise shared._error('操作标识已被另一修正使用')
            return dict(replayed=True)
        plan=preview(db,source_id,target_id)
        if plan['sha256']!=preview_hash:raise shared._error('配方或产品已变化，请重新核对')
        from app.api.deps import require_customer_access
        from app.services.shared_customer_access import PHYSICAL_FIELDS
        from app.services.master_data_versioning import _apply_versioned_update, preview_versioned_update
        from app.services.composite_bom import replace_product_bom
        source,target=db.get(Product,source_id),db.get(Product,target_id)
        for p in (source,target):require_customer_access(p.customer_id,current_user=actor,db=db)
        components=[];new_pairs=[]
        for row in plan['children']:
            child=db.get(Product,row['source_id'])
            from app.services.shared_catalog_bom import clone_product
            peer=clone_product(db,child,target.customer_id,actor);new_pairs.append([child.id,peer.id])
            edge=row['edge']
            components.append(dict(component_product_id=peer.id,unit=child.unit,inventory_relation='assembly',
                **{key:edge[key] for key in ('quantity_per_set','is_die_cut','mold_tool_id','mold_max_yield_per_sheet',
                    'spare_sheet_quantity','display_mode','show_on_delivery','is_required','remark')}))
        if source.die_cut_path!=target.die_cut_path:
            raise shared._error('旧模切路径不同，须单独核对，不能绕过版本记录')
        updates={key:getattr(source,key) for key in PHYSICAL_FIELDS if key!='die_cut_path'}
        updates.update(is_composite=True,is_virtual_composite_parent=False,combination_mode=source.combination_mode,
                       composite_fulfillment_mode='parent_delivery')
        args=dict(object_type='product',entity=target,updates=updates,expected_version=target.version,user=actor)
        check=preview_versioned_update(db,**args)
        _apply_versioned_update(db,**args,confirmation_token=check['confirmation_token'],reason=evidence,source='yg_gy_bom_alignment')
        replace_product_bom(db,parent_product_id=target.id,components=components,expected_version=target.version,
            user=actor,change_reason=evidence,inventory_mode='assembled',material_mode='expand_children',delivery_mode='parent')
        for ids in new_pairs:
            value=shared.preview(db,product_ids=ids,lot_ids=[],auto_enroll=True)
            shared.confirm(db,product_ids=ids,lot_ids=[],preview_hash=value['preview_hash'],
                operation_key=operation_key+'-child-'+str(ids[0]),evidence=evidence,actor=actor,auto_enroll=True)
        # Parent contract is checked only after each corresponding child group
        # exists. Reconciliation records the new fact without deleting old ones.
        plan=policy.preview_reconcile(db,source_id)
        policy.reconcile(db,product_id=source_id,preview_hash=plan['sha256'],operation_key=operation_key+'-parents',evidence=evidence,actor=actor)
        db.add(SharedFinishedMutation(operation_key=operation_key,group_id=plan['group_id'],request_json=request,
            result_json=shared._json(dict(target_id=target_id,children=new_pairs,formal_stock_writes=False))))
        db.flush()
        return dict(target_id=target_id,children=new_pairs,replayed=False)
