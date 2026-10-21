"""Explicit shared-product facts and atomic report/material maintenance.

Immutable mutation receipts retain earlier member and lot identities. This
policy never turns a material change into a change to an existing physical lot.
"""
import hashlib
import json
from copy import deepcopy
from types import SimpleNamespace
from sqlalchemy import select, update
from fastapi import HTTPException
from app.models.customer import Customer
from app.models.product import Product
from app.models.shared_finished_stock import SharedFinishedGroup, SharedFinishedMember, SharedFinishedLot, SharedFinishedMutation
from app.services import shared_finished_stock as shared

CAPABILITY = 'shared_product_master_v1'
MATERIAL = ('material_id', 'legacy_material_text', 'flute_type', 'layer_count', 'default_material_code')
REPORT = ('report_length_mm', 'report_width_mm', 'base_report_length_mm', 'base_report_width_mm',
          'crease_type', 'crease_left_mm', 'crease_middle_mm', 'crease_right_mm',
          'base_crease_type', 'base_crease_left_mm', 'base_crease_middle_mm', 'base_crease_right_mm',
          'default_cutting_mode', 'sheet_cutting_settings', 'production_variants')
COMMERCIAL = ('sale_unit_price', 'sale_unit_price_no_tax', 'cost_unit_price', 'board_price', 'suggested_price')
PRINTING = ('printing_plate_mode', 'printing_plate_1_id', 'printing_plate_2_id', 'printing_plate_3_id',
            'print_content', 'printing_colors', 'plate_alignment_value_mm', 'plate_mount_value_mm',
            'machine_set_length_mm', 'machine_set_width_mm', 'machine_set_height_mm')


def catalog_products(db, product):
    """Only explicitly enabled full-catalog memberships grant shared assets."""
    pair = _pair(db, product)
    if not pair:
        return [product]
    gid, products = pair
    policy = receipt(db, gid)
    group = db.get(SharedFinishedGroup, gid)
    if not policy or not policy.get('full_catalog') or not group.enabled:
        return [product]
    from app.services.shared_member_policy import states
    enabled = states(db, gid)
    if enabled.get(str(product.id), enabled.get(product.id, True)) is False:
        return [product]
    return [p for p in products if p.is_active and not p.deleted_at and not p.purged_at
            and enabled.get(str(p.id), enabled.get(p.id, True)) is not False]


def plate_allowed(db, product, plate):
    if plate.customer_id == product.customer_id:
        return True
    # A foreign plate must already be assigned to this exact shared SKU.
    return any(p.customer_id == plate.customer_id and plate.id in
               (p.printing_plate_1_id, p.printing_plate_2_id, p.printing_plate_3_id)
               for p in catalog_products(db, product))


def sync_processing_profile(db, product, profile, actor):
    """Current labour exceptions share one update transaction and original CAS."""
    from app.models.processing_cost import ProductProcessingProfile
    from app.api.deps import require_customer_access
    from app.services.audit_log import append_audit_event
    from app.services.shared_customer_access import canonical
    fields = ('printer_mode', 'die_cut_mode', 'assembly_worker_days_per_1000')
    values = {k: getattr(profile, k) for k in fields}
    for peer in catalog_products(db, product):
        if peer.id == product.id:
            continue
        require_customer_access(peer.customer_id, current_user=actor, db=db)
        old = db.scalar(select(ProductProcessingProfile).where(ProductProcessingProfile.product_id == peer.id))
        before = {k: getattr(old, k) for k in fields} if old else None
        if old:
            claimed = db.execute(update(ProductProcessingProfile).where(
                ProductProcessingProfile.id == old.id, ProductProcessingProfile.version == old.version)
                .values(**values, version=ProductProcessingProfile.version + 1, updated_by=actor.id))
            if claimed.rowcount != 1:
                raise HTTPException(409, '共用产品加工参数已变化，全部未保存')
        else:
            db.add(ProductProcessingProfile(product_id=peer.id, **values, version=1,
                created_by=actor.id, updated_by=actor.id))
        append_audit_event(db,event_category='business',result='success',source='web',module_code='finance',
            action_code='sync_shared_processing_profile',resource='products',actor=actor,
            entity_type='product',entity_id=peer.id,details={'source_product_id':product.id,
                'before':json.loads(canonical(before)), 'after':json.loads(canonical(values))})
    db.flush()


def receipt(db, group_id):
    raw = db.execute(select(SharedFinishedMutation.result_json).where(
        SharedFinishedMutation.group_id == group_id,
        SharedFinishedMutation.result_json.contains('"shared_product_master_v1":', autoescape=True),
    ).order_by(SharedFinishedMutation.id.desc()).limit(1)).scalar()
    return json.loads(raw)[CAPABILITY] if raw else None


def effective_member(db, member):
    if member is None:
        return None
    original = dict(member) if isinstance(member, dict) else {
        key: getattr(member, key) for key in ('product_id', 'group_id', 'customer_id', 'identity_json', 'product_basis_json')}
    policy = receipt(db, original['group_id'])
    current = policy and policy['members'].get(str(original['product_id']))
    return SimpleNamespace(**{**original, **(current or {})})


def approved_members(db, group_id, products, identities):
    policy = receipt(db, group_id) if group_id else None
    return bool(policy and all(policy['members'].get(str(p.id), {}).get('identity_json') == identity
                              for p, identity in zip(products, identities)))


def lot_allowed(db, member, lot):
    policy = receipt(db, member.group_id)
    if not policy:
        return True
    frozen = policy['lots'].get(str(lot.id))
    # A move/split keeps the original physical version. Its newly allocated
    # row id must not make an older material batch look like a new receipt.
    ancestor=lot.id;seen=set()
    while frozen is None:
        if ancestor in seen:raise shared._error('共用库存移库来源成环，请核对原流水')
        seen.add(ancestor)
        link=db.get(SharedFinishedLot,ancestor)
        if not link or not link.source_lot_id:break
        ancestor=link.source_lot_id
        frozen=policy['lots'].get(str(ancestor))
    if frozen is None:
        # Only new lots admitted by the original receipt qualification may
        # enter after reconciliation; unreviewed old records remain blocked.
        return not seen.intersection(policy['legacy_lot_ids'])
    return (shared.lot_identity(lot) == frozen['identity_json']
            and frozen['bases'].get(str(member.product_id)) == member.product_basis_json)


def _pair(db, product):
    member = db.get(SharedFinishedMember, product.id)
    if not member:
        return None
    ids = list(db.scalars(select(SharedFinishedMember.product_id).where(
        SharedFinishedMember.group_id == member.group_id).order_by(SharedFinishedMember.product_id)))
    products = [db.get(Product, pid) for pid in ids]
    if len(products) < 2 or any(p is None for p in products):
        return None
    return member.group_id, products


def _same_role(products):
    # Same printed code is not enough to collapse a BOM parent/long/short part.
    def unit(p):
        return '片' if p.is_internal_component and not p.is_composite and p.unit in {'只','片'} else p.unit
    return len({(p.supply_mode,p.is_composite,p.is_virtual_composite_parent,
                 p.is_internal_component,unit(p),p.pieces_per_box,p.splice_mode) for p in products}) == 1


def preview_reconcile(db, product_id):
    product = db.get(Product, product_id)
    pair = _pair(db, product) if product else None
    if not pair:
        raise shared._error('需先在原共用组核对同实物、同角色产品')
    gid, products = pair
    if not _same_role(products):
        raise shared._error('同编码的BOM角色或实物单位不同，须先按已核实配方修正当前资料')
    if not all(p.is_active and not p.deleted_at and not p.purged_at for p in products):
        raise shared._error('停用产品不能恢复共用资格')
    from app.models.warehouse_inventory import InventoryLot
    facts = list(db.scalars(select(SharedFinishedLot).where(SharedFinishedLot.group_id == gid)))
    members = {str(p.id): dict(identity_json=shared.member_identity(p), product_basis_json=shared.product_basis(p)) for p in products}
    lots = {}
    for fact in facts:
        lot = db.get(InventoryLot, fact.lot_id)
        if shared.lot_identity(lot) != fact.identity_json:
            raise shared._error(f'批次{lot.id}的冻结身份已变化，须核对原始凭据')
        lots[str(lot.id)] = dict(identity_json=fact.identity_json,
                                bases={pid: value['product_basis_json'] for pid, value in members.items()})
    # Include explicitly confirmed current finished batches, preserving the
    # original owner, frozen identity, cost and every reservation. Unknown or
    # held stock remains outside this reconciliation.
    from app.models.warehouse_inventory import FinishedGoodsInventoryDetail
    all_lots = list(db.scalars(select(InventoryLot).join(InventoryLot.finished_detail).where(
        FinishedGoodsInventoryDetail.product_id.in_([p.id for p in products]),
        InventoryLot.status == 'active', InventoryLot.inventory_type == 'finished')))
    known = {p.id: p for p in products}
    added, skipped = [], []
    for lot in all_lots:
        if str(lot.id) in lots: continue
        detail=lot.finished_detail; owner=known[detail.product_id]
        basis=json.loads(detail.physical_basis_json or '{}')
        other=db.get(SharedFinishedLot,lot.id)
        from app.services.bom_inventory_contract import is_body_lot
        from app.services.fixed_shelf_staging import staging_owner
        from app.services.bom_subkits import require_free_subkit_stock, SubkitError
        from app.services.warehouse_inventory import WarehouseInventoryError
        try:
            require_free_subkit_stock(db, lot)
        except (WarehouseInventoryError, SubkitError):
            skipped.append(lot.id);continue
        if is_body_lot(lot) or staging_owner(db,lot.id):
            skipped.append(lot.id);continue
        if (other or detail.is_general or detail.owner_customer_id != owner.customer_id
                or detail.inventory_code_snapshot not in {owner.product_code,owner.customer_material_code}
                or basis.get('schema') != 1 or basis.get('product_id') != owner.id
                or lot.unit != 'boxes' or basis.get('unit') != owner.unit
                or basis.get('assembly') or basis.get('quantity_basis') or lot.quantity_available <= 0):
            skipped.append(lot.id);continue
        # BOM batches require their original assembled recipe proof. This
        # ordinary receipt repair does not invent missing assembly evidence.
        if owner.is_composite or owner.is_internal_component:
            skipped.append(lot.id);continue
        lots[str(lot.id)] = dict(identity_json=shared.lot_identity(lot),
                                bases={pid:value['product_basis_json'] for pid,value in members.items()})
        added.append(dict(lot_id=lot.id,version=lot.version))
    group = db.get(SharedFinishedGroup, gid)
    result = dict(group_id=gid, version=group.version, enabled=group.enabled,
                  added_lots=added, skipped_lots=skipped,
                  products=[dict(id=p.id, version=p.version, code=p.product_code) for p in products],
                  members=members, lots=lots, legacy_lot_ids=sorted(f.lot_id for f in facts))
    result['sha256'] = hashlib.sha256(shared._json(result).encode()).hexdigest()
    return result


def reconcile(db, *, product_id, preview_hash, operation_key, evidence, actor):
    """Explicit, reviewed clerical correction; does not alter a stock row."""
    if actor is None or not actor.is_active or actor.role != 'admin' or not evidence.strip():
        raise shared._error('仅活动管理员可按已核实事实修复共用关系', 403)
    from app.services.bom_transactions import atomic_bom
    from app.services.audit_log import append_audit_event
    from app.api.deps import require_customer_access
    request = shared._json(dict(product_id=product_id, preview_hash=preview_hash, evidence=evidence, actor_id=actor.id))
    with atomic_bom(db):
        old = db.scalar(select(SharedFinishedMutation).where(SharedFinishedMutation.operation_key == operation_key))
        if old:
            if old.request_json != request: raise shared._error('操作标识已用于其他修复')
            return dict(replayed=True, group_id=old.group_id)
        plan = preview_reconcile(db, product_id)
        if plan['sha256'] != preview_hash: raise shared._error('产品或共用资料已变化，请重新核对')
        gid = plan['group_id']
        for row in plan['products']:
            require_customer_access(db.get(Product,row['id']).customer_id,current_user=actor,db=db)
        locked = db.execute(update(SharedFinishedGroup).where(SharedFinishedGroup.id == gid,
            SharedFinishedGroup.version == plan['version']).values(version=plan['version'] + 1))
        if locked.rowcount != 1: raise shared._error('共用资料已变化，请重新核对')
        for row in plan['products']:
            locked = db.execute(update(Product).where(Product.id == row['id'], Product.version == row['version'])
                                .values(version=Product.version, updated_at=Product.updated_at))
            if locked.rowcount != 1: raise shared._error('产品已变化，请重新核对')
        from app.models.warehouse_inventory import InventoryLot
        for row in plan['added_lots']:
            locked=db.execute(update(InventoryLot).where(InventoryLot.id==row['lot_id'],
                InventoryLot.version==row['version']).values(version=InventoryLot.version+1,updated_at=InventoryLot.updated_at))
            if locked.rowcount != 1: raise shared._error('库存已变化，请重新核对')
            db.add(SharedFinishedLot(lot_id=row['lot_id'],group_id=gid,
                identity_json=plan['lots'][str(row['lot_id'])]['identity_json']))
        from app.models.shared_finished_stock import SharedBomMember
        from app.services.shared_bom_stock import persist_contract
        for pid,current in plan['members'].items():
            if not db.get(SharedBomMember,int(pid)):
                persist_contract(db,int(pid),current['identity_json'])
        db.add(SharedFinishedMutation(operation_key=operation_key, group_id=gid, request_json=request,
                                      result_json=shared._json({CAPABILITY: plan})))
        append_audit_event(db, event_category='business', result='success', source='script', module_code='warehouse',
            action_code='reconcile_shared_product_master', resource='shared_finished_group', actor=actor,
            entity_type='shared_finished_group', entity_id=gid, details=json.loads(request))
        db.flush()
        return dict(group_id=gid, replayed=False)


def prepare_sync(db, product, updates, user):
    pair = _pair(db, product)
    if not pair or not receipt(db, pair[0]):
        return None
    gid, products = pair
    if any(updates.get(key, getattr(product,key)) != getattr(product,key)
           for key in ('customer_id','product_code','is_composite','is_internal_component','is_virtual_composite_parent')):
        raise HTTPException(409, '共用产品的客户、编码和BOM角色不能单边变更，请先调整共用范围')
    from app.services.shared_customer_access import PHYSICAL_FIELDS
    fields = set(PHYSICAL_FIELDS) | set(MATERIAL) | set(REPORT)
    if receipt(db, gid).get('full_catalog'):
        fields |= set(COMMERCIAL) | set(PRINTING)
    changed = {key for key in fields if key in updates and
               (getattr(product,key) != updates[key] if key == 'sheet_cutting_settings' else
                shared._value(getattr(product,key)) != shared._value(updates[key]))}
    if not changed:
        return None
    if not _same_role(products):
        raise HTTPException(409, '共用产品的BOM角色或单位尚未统一，未保存单边资料')
    from app.api.deps import require_customer_access
    from app.services.master_data_versioning import preview_versioned_update
    peers = []
    # Full field families keep derived material/report fields consistent.
    copy_fields = changed | (set(MATERIAL) if changed & set(MATERIAL) else set()) | (set(REPORT) if changed & set(REPORT) else set())
    peer_updates = {key: deepcopy(updates.get(key, getattr(product,key))) for key in copy_fields}
    for peer in products:
        require_customer_access(peer.customer_id, current_user=user, db=db)
        if peer.id == product.id:
            continue
        if peer.deleted_at or peer.purged_at:
            raise HTTPException(409, '共用产品已删除，请先调整共用范围')
        # Validate references for each customer as well as the original source.
        from app.api.products import _validate_references
        _validate_references(db, customer_id=peer.customer_id,
            material_id=peer_updates.get('material_id',peer.material_id),
            mold_tool_id=peer_updates.get('mold_tool_id',peer.mold_tool_id))
        from app.models.printing_plate import PrintingPlate
        for field in ('printing_plate_1_id', 'printing_plate_2_id', 'printing_plate_3_id'):
            plate_id = peer_updates.get(field)
            if plate_id:
                plate = db.get(PrintingPlate, plate_id)
                if plate is None or plate.status != 'active' or not plate_allowed(db, product, plate):
                    raise HTTPException(409, '挂板不适用于本共用产品，未保存任何一方资料')
        preview = preview_versioned_update(db, object_type='product',entity=peer,updates=peer_updates,
                                          expected_version=peer.version,user=user)
        peers.append(dict(product=peer,version=peer.version,token=preview['confirmation_token']))
    group = db.get(SharedFinishedGroup,gid)
    return dict(peers=peers,products=products,group_id=gid,group_version=group.version,
                updates=peer_updates,before_policy=receipt(db,gid),ancestors=ancestor_plans(db,[p.id for p in products],user),
                before_members={str(p.id):dict(identity_json=shared.member_identity(p),product_basis_json=shared.product_basis(p)) for p in products})


def finish_sync(db, product, plan, user, reason, source):
    from uuid import uuid4
    from app.services.master_data_versioning import _apply_versioned_update
    for row in plan['peers']:
        _apply_versioned_update(db,object_type='product',entity=row['product'],updates=plan['updates'],
            expected_version=row['version'],user=user,reason=reason or '共用生产资料同步',
            source=source+'.shared_master',confirmation_token=row['token'])
    for current in plan['products']:
        db.expire(current,['material','mold_tool'])
    gid=plan['group_id']
    locked=db.execute(update(SharedFinishedGroup).where(SharedFinishedGroup.id==gid,
        SharedFinishedGroup.version==plan['group_version']).values(version=plan['group_version']+1))
    if locked.rowcount != 1:
        raise shared._error('共用资料已变化，本次修改全部未保存')
    policy=deepcopy(plan['before_policy'])
    facts=list(db.scalars(select(SharedFinishedLot).where(SharedFinishedLot.group_id==gid)))
    for fact in facts:
        policy['lots'].setdefault(str(fact.lot_id),dict(identity_json=fact.identity_json,
            bases={pid:value['product_basis_json'] for pid,value in plan['before_members'].items()}))
    policy['legacy_lot_ids']=sorted(set(policy['legacy_lot_ids'])|{f.lot_id for f in facts})
    policy['members']={str(p.id):dict(identity_json=shared.member_identity(p),product_basis_json=shared.product_basis(p)) for p in plan['products']}
    policy['source_product_id']=product.id
    from app.services.shared_customer_access import canonical, PHYSICAL_FIELDS
    policy['manufacturing_fields']=json.loads(canonical(
        {key:getattr(product,key) for key in set(PHYSICAL_FIELDS)|set(MATERIAL)|set(REPORT)}))
    db.add(SharedFinishedMutation(operation_key='shared-master-'+uuid4().hex,group_id=gid,
        request_json=shared._json(dict(source_product=product.id,target_products=[r['product'].id for r in plan['peers']],
                                      fields=sorted(plan['updates']),source=source)),
        result_json=shared._json({CAPABILITY:policy})))
    db.flush()
    for parent_plan in plan['ancestors']:
        refresh_identity(db,parent_plan,user,source)


def identity_plan(db,gid,user):
    from app.api.deps import require_customer_access
    from app.services.shared_finished_management import member_ids
    policy=receipt(db,gid)
    if not policy:return None
    products=[db.get(Product,pid) for pid in member_ids(db,gid)]
    for p in products:require_customer_access(p.customer_id,current_user=user,db=db)
    return dict(group_id=gid,version=db.get(SharedFinishedGroup,gid).version,policy=deepcopy(policy),
        products=products,before_members={str(p.id):dict(identity_json=shared.member_identity(p),product_basis_json=shared.product_basis(p)) for p in products})


def ancestor_plans(db,ids,user):
    from app.models.product_bom import ProductBomComponent
    plans=[];seen=set();frontier=set(ids)
    while frontier:
        parents=set(db.scalars(select(ProductBomComponent.parent_product_id).where(ProductBomComponent.component_product_id.in_(frontier))))
        frontier=set()
        for pid in sorted(parents):
            member=db.get(SharedFinishedMember,pid)
            if not member or member.group_id in seen:continue
            seen.add(member.group_id)
            plan=identity_plan(db,member.group_id,user)
            if plan:
                plans.append(plan);frontier.update(p.id for p in plan['products'])
    return plans


def refresh_identity(db,plan,user,source):
    from uuid import uuid4
    gid=plan['group_id'];policy=plan['policy']
    locked=db.execute(update(SharedFinishedGroup).where(SharedFinishedGroup.id==gid,SharedFinishedGroup.version==plan['version'])
        .values(version=plan['version']+1))
    if locked.rowcount!=1:raise shared._error('关联BOM资料已变化，本次修改未保存')
    facts=list(db.scalars(select(SharedFinishedLot).where(SharedFinishedLot.group_id==gid)))
    for fact in facts:policy['lots'].setdefault(str(fact.lot_id),dict(identity_json=fact.identity_json,
        bases={pid:v['product_basis_json'] for pid,v in plan['before_members'].items()}))
    policy['legacy_lot_ids']=sorted(set(policy['legacy_lot_ids'])|{f.lot_id for f in facts})
    policy['members']={str(p.id):dict(identity_json=shared.member_identity(p),product_basis_json=shared.product_basis(p)) for p in plan['products']}
    db.add(SharedFinishedMutation(group_id=gid,operation_key='shared-recipe-'+uuid4().hex,
        request_json=shared._json(dict(source=source,actor_id=user.id)),result_json=shared._json({CAPABILITY:policy})))
    db.flush()


def recipe_sync_plan(db,parent_id,user):
    member=db.get(SharedFinishedMember,parent_id)
    if not member:return None
    plan=identity_plan(db,member.group_id,user)
    if not plan:return None
    from app.services.shared_catalog_bom import edges
    child_gids={m.group_id for p in plan['products'] for e in edges(db,p.id)
        if (m:=db.get(SharedFinishedMember,e.component_product_id))}
    plan['child_plans']=[value for gid in sorted(child_gids) if (value:=identity_plan(db,gid,user))]
    return plan


def finish_recipe_sync(db,parent_id,plan,user,reason):
    from app.services.shared_catalog_bom import edges, EDGE_FIELDS
    from app.services.composite_bom import _replace_product_bom
    from app.models.multilevel_bom import ProductBomProfile,ProductBomInventoryRelation
    from app.api.deps import require_customer_access
    source=db.get(Product,parent_id);profile=db.get(ProductBomProfile,parent_id)
    if not profile or profile.source!='assembled' or profile.delivery_mode!='parent':
        raise shared._error('共用BOM须保持组套父件模式，请先调整共用关系')
    source_edges=edges(db,parent_id)
    for peer in plan['products']:
        if peer.id==parent_id:continue
        require_customer_access(peer.customer_id,current_user=user,db=db)
        components=[]
        for edge in source_edges:
            relation=db.get(ProductBomInventoryRelation,edge.id)
            child=db.get(SharedFinishedMember,edge.component_product_id)
            target=db.scalar(select(SharedFinishedMember).where(SharedFinishedMember.group_id==child.group_id,
                SharedFinishedMember.customer_id==peer.customer_id)) if child else None
            if not target or not relation or relation.relation!='assembly':
                product=db.get(Product,edge.component_product_id)
                raise shared._error(f'{product.product_code}尚未关联{peer.customer.name}的对应子件；请在客户共用设置中加入该子件所属BOM后重试，所有修改均未保存')
            components.append(dict(component_product_id=target.product_id,inventory_relation=relation.relation,
                **{key:getattr(edge,key) for key in EDGE_FIELDS}))
        _replace_product_bom(db,parent_product_id=peer.id,components=components,expected_version=peer.version,user=user,
            change_reason=reason or '共用BOM配方同步',inventory_mode=profile.source,material_mode=profile.material_mode,
            delivery_mode=profile.delivery_mode,preserve_parent_unit=True)
    for child_plan in plan['child_plans']:
        refresh_identity(db,child_plan,user,'shared_bom_recipe')
    refresh_identity(db,plan,user,'shared_bom_recipe')
