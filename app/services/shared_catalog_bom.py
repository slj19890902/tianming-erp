"""Reuse an explicitly selected, flat assembled BOM for another customer.

The original sharing protocol supports single-piece components and assembled
parents. Other BOM modes remain visible as specific conflicts, never guessed.
"""
from copy import deepcopy
from sqlalchemy import select
from app.models.product import Product
from app.models.product_bom import ProductBomComponent
from app.models.multilevel_bom import ProductBomProfile,ProductBomInventoryRelation
from app.models.shared_finished_stock import SharedFinishedMember
from app.services import shared_finished_stock as shared, shared_customer_access as access

EDGE_FIELDS=('quantity_per_set','is_die_cut','mold_tool_id','mold_max_yield_per_sheet','spare_sheet_quantity','display_mode','show_on_delivery','is_required','remark')


def edges(db,pid):
    return list(db.scalars(select(ProductBomComponent).where(ProductBomComponent.parent_product_id==pid).order_by(ProductBomComponent.display_order)))


def physical(p):
    return access.canonical({f:getattr(p,f) for f in access.PHYSICAL_FIELDS})


def counterpart(db,p,cid):
    member=db.get(SharedFinishedMember,p.id)
    existing=db.scalar(select(SharedFinishedMember).where(SharedFinishedMember.group_id==member.group_id,
        SharedFinishedMember.customer_id==cid)) if member else None
    rows=([db.get(Product,existing.product_id)] if existing else list(db.scalars(select(Product).where(
        Product.customer_id==cid,Product.product_code==p.product_code,
        Product.is_composite==p.is_composite,Product.is_internal_component==p.is_internal_component,
        Product.deleted_at.is_(None),Product.purged_at.is_(None)))))
    if len(rows)>1:raise shared._error(f'{p.product_code}同角色有多份资料，请核对具体产品')
    if rows and (not rows[0] or rows[0].deleted_at or rows[0].purged_at or not rows[0].is_active
                 or rows[0].is_composite!=p.is_composite or rows[0].is_internal_component!=p.is_internal_component
                 or physical(rows[0])!=physical(p)):
        raise shared._error(f'{p.product_code}对应产品规格、材质或报料资料不同，请在常用箱核对')
    return rows[0] if rows else None


def preview(db,p,customer_ids):
    from app.services.shared_bom_stock import role
    if role(db,p)!='assembled':raise shared._error('BOM子件请连同组套父件选择共用，避免缺少配方关系')
    children=edges(db,p.id)
    if not children:raise shared._error('组套父件缺少配方，请在常用箱补齐')
    products=[p]
    for e in children:
        child=db.get(Product,e.component_product_id);relation=db.get(ProductBomInventoryRelation,e.id)
        if (role(db,child)!='component' or child.customer_id!=p.customer_id or not relation or relation.relation!='assembly'):
            raise shared._error(f'{p.product_code}含非单片组套子件，需按原BOM规则核对，不能自动复制')
        products.append(child)
    snapshots=[];targets=[]
    for cid in customer_ids:
        copies=[]
        for source in products:
            peer=source if source.customer_id==cid else counterpart(db,source,cid)
            member=db.get(SharedFinishedMember,source.id)
            other=db.get(SharedFinishedMember,peer.id) if peer else None
            if other and (not member or other.group_id!=member.group_id):raise shared._error(f'{source.product_code}已属于另一共用实物')
            copies.append(dict(source_id=source.id,target_id=peer.id if peer else None,target_version=peer.version if peer else None))
        parent_id=copies[0]['target_id']
        if parent_id and parent_id!=p.id:
            target_edges=edges(db,parent_id);expected=[];actual=[]
            mapping={r['source_id']:r['target_id'] for r in copies}
            for e in children:expected.append((mapping[e.component_product_id],access.canonical({k:getattr(e,k) for k in EDGE_FIELDS})))
            for e in target_edges:
                relation=db.get(ProductBomInventoryRelation,e.id)
                if not relation or relation.relation!='assembly':raise shared._error('对应BOM关系不一致')
                actual.append((e.component_product_id,access.canonical({k:getattr(e,k) for k in EDGE_FIELDS})))
            if expected!=actual:raise shared._error('对应BOM配方不一致，请先核对数量和对应子件')
        targets.append(dict(customer_id=cid,copies=copies))
    for source in products:snapshots.append(dict(id=source.id,version=source.version,fields=physical(source)))
    plan=dict(kind='bom',source_id=p.id,code=p.product_code,targets=targets,products=snapshots,
        edges=[dict(id=e.id,component_product_id=e.component_product_id,**{k:getattr(e,k) for k in EDGE_FIELDS}) for e in children])
    plan['hash']=access.digest(plan)
    return plan


def clone_product(db,source,cid,actor):
    from app.models.mold_tool import MoldToolCustomer
    from app.services.master_data_versioning import record_versioned_create
    if source.mold_tool_id and not db.scalar(select(MoldToolCustomer).where(MoldToolCustomer.mold_tool_id==source.mold_tool_id,MoldToolCustomer.customer_id==cid)):
        db.add(MoldToolCustomer(mold_tool_id=source.mold_tool_id,customer_id=cid,display_order=None,created_by=actor.id))
    fields={f:deepcopy(getattr(source,f)) for f in access.PHYSICAL_FIELDS}
    fields.update(is_composite=source.is_composite,is_internal_component=source.is_internal_component,
        is_virtual_composite_parent=source.is_virtual_composite_parent,combination_mode=source.combination_mode,
        composite_fulfillment_mode=source.composite_fulfillment_mode)
    initial=access.sale_price_initialization(db,source,target_customer_id=cid,target_unit=fields['unit'])
    peer=Product(customer_id=cid,product_code=source.product_code,customer_material_code=source.product_code,
        product_name=source.product_name,**access.initial_sale_price_values(initial),**fields)
    db.add(peer);db.flush()
    record_versioned_create(db,object_type='product',entity=peer,user=actor,reason='客户共用配方销售引用',source='customer_sharing')
    return peer


def apply(db,plan,actor):
    from app.services.composite_bom import replace_product_bom
    from app.services.customer_sharing import _sku_plan,_apply_sku
    source=db.get(Product,plan['source_id']);cids=[row['customer_id'] for row in plan['targets']]
    if preview(db,source,cids)['hash']!=plan['hash']:raise shared._error('BOM资料已变化，请重新核对')
    for target in plan['targets']:
        mapping={}
        for row in target['copies']:
            original=db.get(Product,row['source_id'])
            mapping[original.id]=db.get(Product,row['target_id']) if row['target_id'] else clone_product(db,original,target['customer_id'],actor)
        parent=mapping[source.id]
        if not target['copies'][0]['target_id']:
            profile=db.get(ProductBomProfile,source.id)
            replace_product_bom(db,parent_product_id=parent.id,expected_version=parent.version,user=actor,
                change_reason='沿用共用产品有效组套配方',inventory_mode=profile.source,material_mode=profile.material_mode,
                delivery_mode=profile.delivery_mode,preserve_parent_unit=True,
                components=[dict(component_product_id=mapping[e['component_product_id']].id,inventory_relation='assembly',
                    **{key:e[key] for key in EDGE_FIELDS}) for e in plan['edges']])
    for e in plan['edges']:
        action=_sku_plan(db,db.get(Product,e['component_product_id']),cids)
        _apply_sku(db,action,actor)
    return _apply_sku(db,_sku_plan(db,source,cids),actor)
