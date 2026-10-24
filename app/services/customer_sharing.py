"""Small customer-level entry point over the existing shared SKU protocol.

No stock balance or old document is copied. Catalog membership and SKU
eligibility are separate: a customer may keep private products in the same group.
"""
from uuid import uuid4
import json
from sqlalchemy import select, update
from fastapi import HTTPException
from app.models.customer import Customer
from app.models.product import Product
from app.models.customer_sharing import CustomerSharingGroup, CustomerSharingMember
from app.models.shared_finished_stock import SharedFinishedGroup, SharedFinishedMember, SharedFinishedMutation, SharedFinishedLot
from app.models.warehouse_inventory import InventoryLot, FinishedGoodsInventoryDetail
from app.services.warehouse_inventory import WarehouseInventoryError
from app.services import shared_finished_stock as shared, shared_finished_management as management
from app.services import shared_customer_access as access, shared_product_master as master
from app.services.bom_transactions import atomic_bom
from app.services.audit_log import append_audit_event


def _customer(db, cid, actor):
    from app.api.deps import require_customer_access
    require_customer_access(cid,current_user=actor,db=db)
    row=db.get(Customer,cid)
    if not row or not row.is_active:
        raise HTTPException(409,'共用客户不存在或已停用')
    return row


def settings(db,cid,actor):
    _customer(db,cid,actor)
    member=db.get(CustomerSharingMember,cid)
    if not member:return dict(group_id=None,version=None,members=[],new_products_shared=False,
        delivery_heading_customer_id=None,delivery_address_customer_id=None)
    group=db.get(CustomerSharingGroup,member.group_id)
    members=list(db.scalars(select(CustomerSharingMember).where(CustomerSharingMember.group_id==group.id)))
    linked_groups=select(SharedFinishedMember.group_id).join(SharedFinishedGroup,
        SharedFinishedGroup.id==SharedFinishedMember.group_id).where(
        SharedFinishedMember.customer_id==cid,SharedFinishedGroup.enabled.is_(True))
    selected={str(m.customer_id):[] for m in members}
    for row in db.scalars(select(SharedFinishedMember).where(SharedFinishedMember.group_id.in_(linked_groups),
            SharedFinishedMember.customer_id.in_([m.customer_id for m in members]))):
        selected[str(row.customer_id)].append(row.product_id)
    return dict(group_id=group.id,version=group.version,name=group.name,new_products_shared=group.new_products_shared,
        shared_source_product_ids=selected,
        members=[dict(id=m.customer_id,name=_customer(db,m.customer_id,actor).name) for m in members],
        delivery_heading_customer_id=member.delivery_heading_customer_id,
        delivery_address_customer_id=member.delivery_address_customer_id)


def _role(p):
    return p.is_composite,p.is_internal_component,p.is_virtual_composite_parent


def _sku_plan_base(db,p,customer_ids):
    if not shared._ordinary(db,p):
        from app.services.shared_bom_stock import role
        if not role(db,p):raise shared._error('当前生产类型尚不符合原共用库存规则，请在常用箱核对生产类型和单位')
    member=db.get(SharedFinishedMember,p.id)
    gid=member.group_id if member else None
    ids=management.member_ids(db,gid) if gid else [p.id]
    current=[db.get(Product,pid) for pid in ids]
    if any(q.customer_id not in customer_ids for q in current):
        raise shared._error('该产品已与本次范围外的客户共用；请先把该客户纳入共用组，不能扩大资料同步范围')
    missing=[]
    for cid in customer_ids:
        if any(q.customer_id==cid for q in current):continue
        matches=list(db.scalars(select(Product).where(Product.customer_id==cid,Product.product_code==p.product_code,
            Product.deleted_at.is_(None),Product.purged_at.is_(None))))
        matches=[q for q in matches if _role(q)==_role(p)]
        if len(matches)>1:raise shared._error('同客户同编码同角色有多份资料，请在常用箱明确保留的产品')
        if matches:current.append(matches[0])
        else:missing.append(cid)
    # Manufacturing cost conflicts still need review. Customer selling-price
    # differences are intentional and never block physical catalog sharing.
    for peer in current:
        if peer.id == p.id:
            continue
        for field in master.COMMERCIAL:
            source_value, target_value = getattr(p, field), getattr(peer, field)
            if target_value is not None and target_value != source_value:
                raise shared._error(f'{p.product_code} 的{field}不同：来源{source_value}，目标{target_value}；请先核对当前报价')
    from app.models.processing_cost import ProductProcessingProfile
    profiles=list(db.scalars(select(ProductProcessingProfile).where(ProductProcessingProfile.product_id.in_([q.id for q in current]))))
    if len({(r.printer_mode,r.die_cut_mode,r.assembly_worker_days_per_1000) for r in profiles})>1:
        raise shared._error(f'{p.product_code} 的加工成本例外参数不同，请先在加工成本维护中核对')
    ids=sorted(q.id for q in current)
    group=db.get(SharedFinishedGroup,gid) if gid else None
    from app.services.shared_catalog_bom import edges
    bom_children=edges(db,p.id) if p.is_composite else []
    if p.is_composite and (missing or any(not db.get(SharedFinishedMember,e.component_product_id) for e in bom_children)):
        from app.services.shared_catalog_bom import preview as bom_preview
        return bom_preview(db,p,customer_ids)
    if missing:
        args=dict(source_product_id=p.id,product_ids=ids,new_customer_ids=missing,
            expected_group_version=group.version if group else None,auto_enroll=True)
        value=access.preview(db,**args)
        return dict(kind='create',args=args,hash=value['preview_hash'],source_id=p.id,code=p.product_code)
    if len(ids)<2:raise shared._error('请至少选择两个客户')
    value=shared.preview(db,product_ids=ids,lot_ids=[],group_id=gid,allow_new_members=bool(gid),auto_enroll=True)
    return dict(kind='link',ids=ids,group_id=gid,version=group.version if group else None,
        hash=value['preview_hash'],source_id=p.id,code=p.product_code)


def _sku_plan(db,p,customer_ids):
    plan=_sku_plan_base(db,p,customer_ids)
    # Tax profile changes must invalidate the user's price-inheritance preview,
    # even when they do not increment a product/customer version.
    plan['sale_price_initializations']={str(cid):access.sale_price_initialization(
        db,p,target_customer_id=cid,target_unit=p.unit) for cid in customer_ids}
    ids=plan.get('ids') or plan.get('args',{}).get('product_ids') or [p.id]
    lots=list(db.scalars(select(InventoryLot).join(InventoryLot.finished_detail).where(
        FinishedGoodsInventoryDetail.product_id.in_(ids),InventoryLot.status=='active',InventoryLot.quantity_available>0)))
    accepted=[];rejected=[]
    for lot in lots:
        if db.get(SharedFinishedLot,lot.id):continue
        try:
            accepted.extend(shared._preview_lots(db,[db.get(Product,pid) for pid in ids],[lot.id],automatic=True))
        except WarehouseInventoryError as exc:
            rejected.append(dict(lot_id=lot.id,reason=str(exc)))
    plan['stock']=accepted;plan['stock_pending']=rejected
    return plan


def _attach_stock(db,gid,plan,actor):
    if not plan.get('stock'):return
    for row in plan['stock']:
        lot=db.get(InventoryLot,row['lot_id'])
        if db.get(SharedFinishedLot,lot.id):continue
        products=[db.get(Product,pid) for pid in management.member_ids(db,gid)]
        current=shared._preview_lots(db,products,[lot.id],automatic=True)[0]
        if access.canonical(current)!=access.canonical(row):raise shared._error('库存已变化，请重新核对共用范围')
        claimed=db.execute(update(InventoryLot).where(InventoryLot.id==lot.id,InventoryLot.version==row['version'])
            .values(version=InventoryLot.version+1,updated_at=InventoryLot.updated_at))
        if claimed.rowcount!=1:raise shared._error('库存已变化，共用设置未保存')
        db.add(SharedFinishedLot(lot_id=lot.id,group_id=gid,identity_json=row['identity_json']))
    append_audit_event(db,event_category='business',result='success',source='web',module_code='warehouse',
        action_code='share_qualified_catalog_stock',resource='shared_finished_group',actor=actor,
        entity_type='shared_finished_group',entity_id=gid,details=dict(lots=plan['stock'],pending=plan['stock_pending']))
    db.flush()


def preview(db,*,customer_id,source_customer_id,product_ids=None,new_products_shared=False,
            delivery_heading_customer_id=None,delivery_address_customer_id=None,actor):
    current=_customer(db,customer_id,actor);source=_customer(db,source_customer_id,actor)
    if current.id==source.id:raise HTTPException(422,'请选择另一个共用客户')
    memberships=[db.get(CustomerSharingMember,cid) for cid in (customer_id,source_customer_id)]
    gids={m.group_id for m in memberships if m}
    if len(gids)>1:raise HTTPException(409,'两个客户已有不同共用组，请先逐项核对现有产品关系；不能自动合并')
    gid=next(iter(gids),None);group=db.get(CustomerSharingGroup,gid) if gid else None
    cids={customer_id,source_customer_id}
    if gid:cids.update(db.scalars(select(CustomerSharingMember.customer_id).where(CustomerSharingMember.group_id==gid)))
    if len(cids)>20:raise HTTPException(422,'一个共用组最多20个客户')
    customers=[_customer(db,cid,actor) for cid in sorted(cids)]
    for cid in (delivery_heading_customer_id,delivery_address_customer_id):
        if cid is not None and cid not in cids:raise HTTPException(422,'送货资料须选择本客户或当前共用组的客户')
    products=list(db.scalars(select(Product).where(Product.customer_id==source_customer_id,
        Product.is_active.is_(True),Product.deleted_at.is_(None),Product.purged_at.is_(None)).order_by(Product.id)))
    if product_ids is not None:
        if not set(product_ids)<={p.id for p in products}:raise HTTPException(409,'所选产品已变化或不属于来源客户')
        products=[p for p in products if p.id in product_ids]
    from app.services.shared_catalog_bom import edges
    included_children={e.component_product_id for p in products if p.is_composite for e in edges(db,p.id)}
    products=[p for p in products if p.id not in included_children]
    actions=[];blocked=[];seen=set()
    for p in products:
        member=db.get(SharedFinishedMember,p.id)
        key=('group',member.group_id) if member else ('product',p.id)
        if key in seen:continue
        seen.add(key)
        try:actions.append(_sku_plan(db,p,sorted(cids)))
        except (HTTPException,WarehouseInventoryError) as exc:
            blocked.append(dict(product_id=p.id,code=p.product_code,name=p.product_name,reason=str(getattr(exc,'detail',exc))))
    result=dict(customer_id=customer_id,source_customer_id=source_customer_id,group_id=gid,
        version=group.version if group else None,customers=[dict(id=c.id,name=c.name,version=c.version) for c in customers],
        product_ids=product_ids,new_products_shared=new_products_shared,
        delivery_heading_customer_id=delivery_heading_customer_id,delivery_address_customer_id=delivery_address_customer_id,
        actions=actions,blocked=blocked)
    result['preview_hash']=access.digest(result)
    return result


def activate_master(db,gid,actor,source_product_id=None,*,preserve_existing_prices=False):
    """Enable one canonical manufacturing version, without approving old drift."""
    products=[db.get(Product,pid) for pid in management.member_ids(db,gid)]
    source = next((p for p in products if p.id == source_product_id), products[0])
    prior=master.receipt(db,gid) or dict(lots={},legacy_lot_ids=[])
    price_initializations=prior.setdefault('customer_sale_price_initializations',{})
    from app.services.master_data_versioning import preview_versioned_update, _apply_versioned_update
    for p in products:
        updates={field:getattr(source,field) for field in master.COMMERCIAL
                 if getattr(p,field) is None and getattr(source,field) is not None}
        # Treat gross/net fields as a pair: a saved customer price must not gain
        # the other customer's unrelated net price. A prior marker also protects
        # intentional later clearing when this sharing group is re-enabled.
        if str(p.id) not in price_initializations:
            initial=access.sale_price_initialization(db,source,target_customer_id=p.customer_id,target_unit=p.unit)
            existing={field:getattr(p,field) for field in access.SALE_PRICE_FIELDS}
            if preserve_existing_prices or any(value is not None for value in existing.values()):
                initial.update(status='preserved',values=json.loads(access.canonical(existing)))
            else:
                updates.update(access.initial_sale_price_values(initial))
            price_initializations[str(p.id)]=dict(initial,target_product_id=p.id,target_version=p.version)
        if updates:
            preview=preview_versioned_update(db,object_type='product',entity=p,updates=updates,
                expected_version=p.version,user=actor)
            _apply_versioned_update(db,object_type='product',entity=p,updates=updates,
                expected_version=p.version,user=actor,reason='管理员确认整套产品共用，补齐当前报价',
                source='customer_sharing',confirmation_token=preview['confirmation_token'])
        if str(p.id) in price_initializations:
            price_initializations[str(p.id)].setdefault('applied_target_version',p.version)
    for p in products:
        _customer(db,p.customer_id,actor)
        member=master.effective_member(db,db.get(SharedFinishedMember,p.id))
        if member.identity_json!=shared.member_identity(p):raise shared._error('共用成员资料已变化，请核对历史实物')
    old_bases={str(p.id):shared.product_basis(p) for p in products}
    # The reviewed source is the current master. Keep old lot identities and
    # historical order snapshots; unify only the current customer references.
    fields=set(access.PHYSICAL_FIELDS)|set(master.MATERIAL)|set(master.REPORT)|set(master.PRINTING)
    from copy import deepcopy
    for p in products:
        if p.id==source.id:continue
        updates={key:deepcopy(getattr(source,key)) for key in fields
                 if getattr(p,key)!=getattr(source,key)}
        if updates:
            check=preview_versioned_update(db,object_type='product',entity=p,updates=updates,
                expected_version=p.version,user=actor)
            _apply_versioned_update(db,object_type='product',entity=p,updates=updates,
                expected_version=p.version,user=actor,reason='管理员确认同一产品共用，以已选来源统一当前生产资料',
                source='customer_sharing',confirmation_token=check['confirmation_token'])
    new_members={str(p.id):dict(identity_json=shared.member_identity(p),product_basis_json=shared.product_basis(p)) for p in products}
    # A newly added sales reference may use only old stock already approved
    # for an equivalent current member; a material-changed batch stays blocked.
    for frozen in prior['lots'].values():
        for pid,basis in list(frozen['bases'].items()):
            if basis==old_bases.get(pid):
                frozen['bases'][pid]=new_members[pid]['product_basis_json']
        for pid,value in new_members.items():
            if pid in frozen['bases']:continue
            for old_pid,basis in list(frozen['bases'].items()):
                existing=new_members.get(old_pid)
                if (existing and existing['product_basis_json']==basis and
                        shared._interchangeability_key(existing['identity_json'])==shared._interchangeability_key(value['identity_json'])):
                    frozen['bases'][pid]=value['product_basis_json'];break
    prior['members']=new_members
    prior['full_catalog']=True
    prior['source_product_id']=source.id
    prior['manufacturing_fields']=json.loads(access.canonical({key:getattr(source,key)
        for key in set(access.PHYSICAL_FIELDS)|set(master.MATERIAL)|set(master.REPORT)}))
    db.add(SharedFinishedMutation(group_id=gid,operation_key='catalog-master-'+uuid4().hex,
        request_json=access.canonical(dict(actor_id=actor.id,action='enable_shared_master')),
        result_json=access.canonical({master.CAPABILITY:prior})))
    db.flush()
    from app.models.processing_cost import ProductProcessingProfile
    profiles=list(db.scalars(select(ProductProcessingProfile).where(ProductProcessingProfile.product_id.in_([p.id for p in products]))))
    if profiles:
        profile=next((r for r in profiles if r.product_id==source.id),profiles[0])
        master.sync_processing_profile(db,db.get(Product,profile.product_id),profile,actor)


def _apply_sku(db,action,actor,*,include_existing_stock=True,preserve_existing_prices=False):
    key='customer-catalog-'+uuid4().hex
    evidence='管理员确认客户共用同一产品、生产报料资料和合法库存'
    if action['kind']=='bom':
        from app.services.shared_catalog_bom import apply as apply_bom
        gid=apply_bom(db,action,actor)
        _attach_stock(db,gid,action,actor)
        return gid
    if action['kind']=='create':
        result=access.confirm(db,**action['args'],actor=actor,preview_hash=action['hash'],operation_key=key,evidence=evidence)
        gid=result['group_id']
    elif not action['group_id']:
        result=shared.confirm(db,product_ids=action['ids'],lot_ids=[],auto_enroll=True,actor=actor,
            preview_hash=action['hash'],operation_key=key,evidence=evidence)
        gid=result['group_id']
    else:
        gid=action['group_id']
        from app.services.shared_member_policy import states
        disabled=[int(pid) for pid,enabled in states(db,gid).items() if not enabled]
        args=dict(group_id=gid,action='members',expected_version=action['version'],product_ids=action['ids'],disabled_product_ids=disabled)
        value=management.preview_change(db,**args)
        management.change(db,**args,actor=actor,preview_hash=value['preview_hash'],operation_key=key,evidence=evidence)
    from app.models.shared_finished_stock import SharedFinishedPolicy
    policy=db.get(SharedFinishedPolicy,gid)
    if policy:policy.auto_enroll=True
    else:db.add(SharedFinishedPolicy(group_id=gid,auto_enroll=True))
    activate_master(db,gid,actor,action['source_id'],preserve_existing_prices=preserve_existing_prices)
    if include_existing_stock:
        _attach_stock(db,gid,action,actor)
    return gid


def confirm(db,*,preview_hash,actor,**args):
    if not actor or not actor.is_active or actor.role!='admin':raise HTTPException(403,'仅管理员可设置客户共用')
    with atomic_bom(db):
        plan=preview(db,actor=actor,**args)
        if plan['preview_hash']!=preview_hash:raise HTTPException(409,'客户或产品已变化，请重新预览')
        group=db.get(CustomerSharingGroup,plan['group_id']) if plan['group_id'] else None
        if group:
            changed=db.execute(update(CustomerSharingGroup).where(CustomerSharingGroup.id==group.id,
                CustomerSharingGroup.version==plan['version']).values(version=plan['version']+1,new_products_shared=args['new_products_shared']))
            if changed.rowcount!=1:raise HTTPException(409,'共用设置已被修改，请刷新')
        else:
            group=CustomerSharingGroup(name=' / '.join(c['name'] for c in plan['customers'])[:200],new_products_shared=args['new_products_shared'])
            db.add(group);db.flush()
        for row in plan['customers']:
            if not db.get(CustomerSharingMember,row['id']):db.add(CustomerSharingMember(customer_id=row['id'],group_id=group.id))
        db.flush()
        member=db.get(CustomerSharingMember,args['customer_id'])
        member.delivery_heading_customer_id=args.get('delivery_heading_customer_id')
        member.delivery_address_customer_id=args.get('delivery_address_customer_id')
        results=[]
        for action in plan['actions']:
            # A previous action may have linked this SKU's BOM children.
            fresh=_sku_plan(db,db.get(Product,action['source_id']),[c['id'] for c in plan['customers']])
            results.append(dict(product_id=action['source_id'],group_id=_apply_sku(db,fresh,actor),stock_pending=fresh.get('stock_pending',[])))
        append_audit_event(db,event_category='business',result='success',source='web',module_code='customers',
            action_code='configure_customer_sharing',resource='customers',actor=actor,entity_type='customer',entity_id=args['customer_id'],
            details=dict(preview_hash=preview_hash,settings=args,linked=results,blocked=plan['blocked']))
        db.flush()
        return dict(group_id=group.id,linked=results,blocked=plan['blocked'])


def enroll_new_product(db,product,actor,choice=None):
    membership=db.get(CustomerSharingMember,product.customer_id)
    if not membership:
        if choice is True:raise HTTPException(422,'请先在客户资料设置产品与库存共用，或选择仅本客户')
        return
    group=db.get(CustomerSharingGroup,membership.group_id)
    if not (choice if choice is not None else group.new_products_shared):return
    if not actor or not actor.is_active or actor.role!='admin':
        raise HTTPException(403,'新增共用产品需管理员维护；仅本客户的新品可选择“仅本客户”后保存')
    customers=list(db.scalars(select(CustomerSharingMember.customer_id).where(CustomerSharingMember.group_id==group.id)))
    try:
        action=_sku_plan(db,product,customers)
        _apply_sku(db,action,actor)
    except WarehouseInventoryError as exc:
        raise HTTPException(exc.status_code,str(exc)) from exc


def delivery_defaults(db,customer):
    member=db.get(CustomerSharingMember,customer.id)
    heading=db.get(Customer,member.delivery_heading_customer_id) if member and member.delivery_heading_customer_id else customer
    address=db.get(Customer,member.delivery_address_customer_id) if member and member.delivery_address_customer_id else customer
    if not heading or not address or not heading.is_active or not address.is_active:
        raise HTTPException(409,'共用送货资料的客户已停用，请在客户资料重新选择')
    return dict(customer_name_snapshot=heading.name,customer_contact_snapshot=address.contact_person,
        customer_phone_snapshot=address.phone,customer_address_snapshot=address.address)
