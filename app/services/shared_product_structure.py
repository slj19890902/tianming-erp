"""One existing product+BOM transaction owns all enabled shared counterparts.

The private session owner is installed only after scope, membership and versions
are checked. It routes nested master/BOM writes through this coordinator rather
than recursively broadcasting half of a structural conversion. All ordinary
validation, per-product CAS, history, audit and the outer transaction remain.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select, update

from app.models.product import Product
from app.models.multilevel_bom import ProductBomProfile
from app.models.shared_finished_stock import SharedFinishedGroup, SharedFinishedMember, SharedFinishedMutation, SharedBomMember
from app.services import shared_product_master as master, shared_finished_stock as shared

_KEY = 'shared_structure_write_owner'


@dataclass(frozen=True)
class _Owner:
    product_ids: frozenset


def owns_product_write(db, product_id):
    owner = db.info.get(_KEY)
    return isinstance(owner, _Owner) and product_id in owner.product_ids


@contextmanager
def _owner(db, products):
    if _KEY in db.info:
        raise HTTPException(409, '共用结构保存已在执行，请勿重复提交')
    db.info[_KEY] = _Owner(frozenset(p.id for p in products))
    try:
        yield
    finally:
        db.info.pop(_KEY, None)


def _check_members(db, products, actor):
    from app.api.deps import require_customer_access
    from app.services.shared_member_policy import enabled
    for p in products:
        require_customer_access(p.customer_id, current_user=actor, db=db)
        member = db.get(SharedFinishedMember, p.id)
        if (not p.is_active or p.deleted_at or p.purged_at or not member
                or not db.get(SharedFinishedGroup, member.group_id).enabled
                or not enabled(db, member.group_id, p.id)):
            raise HTTPException(409, f'{p.product_code} 的 {p.customer.name} 已停用产品或共用，本次全部未保存')
    if not master._same_role(products):
        raise HTTPException(409, '共用产品原有角色或单位不一致，需先核对具体差异，本次全部未保存')


def _refresh(db, plan, actor):
    # New roles gain an immutable initial contract; existing contracts remain
    # available to frozen reservations and consumed-material evidence.
    from app.services.shared_bom_stock import persist_contract, role
    for p in plan['products']:
        if role(db, p) and db.get(SharedBomMember, p.id) is None:
            persist_contract(db, p.id, shared.member_identity(p))
    db.flush()
    master.refresh_identity(db, plan, actor, 'shared_structure_edit')


def _unlinked_component_products(db, child, customers, actor):
    """Build exact references; their BOM membership is confirmed after edges exist."""
    from app.services.shared_catalog_bom import clone_product, physical
    products = []
    for cid in customers:
        matches = list(db.scalars(select(Product).where(Product.customer_id == cid,
            Product.product_code == child.product_code, Product.deleted_at.is_(None), Product.purged_at.is_(None))))
        if len(matches) > 1:
            raise HTTPException(409, f'{child.product_code} 在对应客户有多份资料，请明确保留的子件，本次全部未保存')
        peer = matches[0] if matches else None
        if peer and (not peer.is_active or peer.is_composite or peer.is_virtual_composite_parent
                     or physical(peer) != physical(child)):
            raise HTTPException(409, f'{child.product_code} 对应子件的角色或实物规格不同，本次全部未保存')
        if peer and db.get(SharedFinishedMember, peer.id):
            raise HTTPException(409, f'{child.product_code} 对应子件已属于另一共用关系，本次全部未保存')
        products.append(peer or clone_product(db, child, cid, actor))
    return products


def save_if_shared(db, product_id, payload, actor):
    from app.api import products as api
    from app.services.shared_catalog_bom import edges
    from app.services import customer_sharing as sharing
    from app.services.master_data_versioning import _apply_versioned_update, preview_versioned_update
    source = db.get(Product, product_id)
    pair = master._pair(db, source) if source else None
    if not pair:
        return None
    gid, products = pair
    policy = master.receipt(db, gid)
    if not policy or not policy.get('full_catalog'):
        return None
    profile = db.get(ProductBomProfile, source.id)
    target_bom = payload.bom.inventory_mode == 'assembled'
    target_plain = (not payload.bom.components and payload.bom.inventory_mode is None
                    and bool(payload.product.box_style) and payload.product.box_style != 'BOM组合')
    if not target_bom and not (target_plain and profile and profile.source == 'assembled'):
        return None
    if payload.product.customer_id != source.customer_id or payload.product.product_code != source.product_code:
        raise HTTPException(409, '共同修改结构不改变客户或存货编码，请保留原身份后保存')
    _check_members(db, products, actor)
    if source.is_internal_component or any(p.is_internal_component for p in products):
        raise HTTPException(409, '该产品仍是其他BOM的子件，请先核对父件关系，不能直接改为成套父件')
    if payload.bom.subkit is not None and payload.bom.subkit.enabled:
        raise HTTPException(409, '旧收料组套不能混入共同结构转换，请按原入口核对，本次全部未保存')
    if source.version != payload.product.expected_version:
        raise HTTPException(409, '产品版本已变化，请刷新后重试')
    updates = api._validated_product_versioned_updates(db, product=source, payload=payload.product, user=actor)
    preview = preview_versioned_update(db, object_type='product', entity=source, updates=updates,
        expected_version=source.version, user=actor)
    if preview['warnings'] and not payload.product.confirmation_token:
        raise HTTPException(409, {**preview, 'code': 'MASTER_CHANGE_CONFIRMATION_REQUIRED'})
    for p in products:
        from app.models.bom_subkit import ProductSubkit
        subkit = db.get(ProductSubkit, p.id)
        if subkit and subkit.enabled:
            raise HTTPException(409, f'{p.customer.name} 仍启用旧收料组套，需先按原入口核对，本次全部未保存')
        current = db.get(ProductBomProfile, p.id)
        if current and (current.source != 'assembled' or current.delivery_mode != 'parent'):
            raise HTTPException(409, f'{p.customer.name} 当前含其他BOM来源或配套方式，不能自动转换')
        claimed = db.execute(update(Product).where(Product.id == p.id, Product.version == p.version)
                             .values(version=p.version, updated_at=Product.updated_at))
        if claimed.rowcount != 1:
            raise HTTPException(409, '共用产品版本已变化，本次全部未保存')

    root_plan = master.identity_plan(db, gid, actor)
    customers = sorted(p.customer_id for p in products)
    child_groups = {db.get(SharedFinishedMember, e.component_product_id).group_id
                    for p in products for e in edges(db, p.id)
                    if db.get(SharedFinishedMember, e.component_product_id)}
    mapping = {}
    unlinked = []
    for component in payload.bom.components:
        child = db.get(Product, component.component_product_id)
        if (child is None or child.customer_id != source.customer_id or child.is_composite
                or child.is_virtual_composite_parent or child.id in {p.id for p in products}
                or component.inventory_relation != 'assembly'):
            raise HTTPException(409, '请选择本客户真实单片子件及组装用量，不能自动转换其他BOM或配套关系')
        if child.is_internal_component and db.get(SharedFinishedMember, child.id) is None:
            counterparts = _unlinked_component_products(db, child, customers, actor)
            mapping[child.id] = {p.customer_id: p.id for p in counterparts}
            unlinked.append((child, counterparts))
            continue
        action = sharing._sku_plan(db, child, customers)
        if action['kind'] == 'bom':
            raise HTTPException(409, '多级组合子件请按原多级BOM入口核对，本次全部未保存')
        existing_gid = action.get('group_id') if action['kind'] == 'link' else None
        existing_members = (list(db.scalars(select(SharedFinishedMember).where(
            SharedFinishedMember.group_id == existing_gid))) if existing_gid else [])
        existing_policy = master.receipt(db, existing_gid) if existing_gid else None
        if (existing_policy and existing_policy.get('full_catalog')
                and {m.product_id for m in existing_members} == set(action['ids'])):
            # Editing an already approved recipe is a product write, not a
            # new membership registration. Keep the original editor access;
            # adding references still goes through administrator-only enrolment.
            child_gid = existing_gid
        else:
            child_gid = sharing._apply_sku(db, action, actor,
                include_existing_stock=False, preserve_existing_prices=True)
        child_groups.add(child_gid)
        members = list(db.scalars(select(SharedFinishedMember).where(SharedFinishedMember.group_id == child_gid)))
        mapping[child.id] = {m.customer_id: m.product_id for m in members}

    children = [master.identity_plan(db, child_gid, actor) for child_gid in sorted(child_groups)]
    for plan in children:
        _check_members(db, plan['products'], actor)
    role_before = {p.id: (p, p.version, p.is_internal_component)
                   for plan in children for p in plan['products']}
    role_before.update({p.id: (p, p.version, p.is_internal_component)
                        for _, counterparts in unlinked for p in counterparts})
    ancestors = master.ancestor_plans(db, [p.id for p in products] + list(role_before), actor)
    sync = master.prepare_sync(db, source, updates, actor, allow_structure=True)
    peer_rows = {row['product'].id: row for row in sync['peers']} if sync else {}
    before_prices = {str(p.id): {key: getattr(p, key) for key in ('sale_unit_price', 'sale_unit_price_no_tax')}
                     for p in products}
    with _owner(db, products):
        api._update_product(source.id, payload.product, db, actor, commit=False)
        for p in products:
            if p.id != source.id and p.id in peer_rows:
                row = peer_rows[p.id]
                _apply_versioned_update(db, object_type='product', entity=p, updates=row['updates'],
                    expected_version=row['version'], user=actor, reason='共用结构共同修改',
                    source='api.products.shared_structure', confirmation_token=row['token'])
            bom = payload.bom.model_copy(update={
                'expected_version': p.version,
                'inventory_mode': 'assembled' if target_bom else 'legacy',
                'material_mode': 'expand_children' if target_bom else None,
                'delivery_mode': 'parent' if target_bom else None,
                'components': [c.model_copy(update={'component_product_id': mapping[c.component_product_id][p.customer_id]})
                               for c in payload.bom.components]})
            api._update_product_bom(p.id, bom, db, actor, commit=False, preserve_parent_unit=True)
        for child, version, old_role in role_before.values():
            new_role = child.is_internal_component
            if new_role != old_role:
                # The existing BOM writer derives child roles. Record that
                # derived change through the original child CAS/history too.
                child.is_internal_component = old_role
                db.flush()
                change = {'is_internal_component': new_role}
                preview = preview_versioned_update(db, object_type='product', entity=child,
                    updates=change, expected_version=version, user=actor)
                _apply_versioned_update(db, object_type='product', entity=child, updates=change,
                    expected_version=version, user=actor, reason='共同BOM修改关联子件角色',
                    source='api.products.shared_structure.child', confirmation_token=preview['confirmation_token'])
    for plan in children:
        _check_members(db, plan['products'], actor)
        _refresh(db, plan, actor)
    for child, _ in unlinked:
        action = sharing._sku_plan(db, child, customers)
        child_groups.add(sharing._apply_sku(db, action, actor,
            include_existing_stock=False, preserve_existing_prices=True))
    _refresh(db, root_plan, actor)
    refreshed = child_groups | {gid}
    for plan in ancestors:
        if plan['group_id'] not in refreshed:
            _refresh(db, plan, actor)
            refreshed.add(plan['group_id'])
    # Reuse the full existing identity comparison; equal printed codes alone
    # never make different recipes, units or components interchangeable.
    shared.preview(db, product_ids=[p.id for p in products], lot_ids=[], group_id=gid)
    from app.services.shared_customer_access import canonical
    from app.services.audit_log import append_audit_event
    evidence = {'source_product_id': source.id, 'product_ids': [p.id for p in products],
                'target': 'assembled' if target_bom else 'ordinary', 'before_prices': before_prices,
                'prices_policy': 'preserve_each_customer', 'child_mapping': mapping}
    db.add(SharedFinishedMutation(group_id=gid, operation_key='shared-structure-'+uuid4().hex,
        request_json=canonical(evidence), result_json=canonical({'shared_structure_edit_v1': evidence})))
    append_audit_event(db, event_category='business', result='success', source='web', module_code='products',
        action_code='shared_structure_edit', resource='products', actor=actor,
        entity_type='product', entity_id=source.id, details=evidence)
    db.flush()
    from app.services.bom_subkits import read_subkit
    bom = api.get_product_bom(db, source.id)
    bom['subkit'] = read_subkit(db, source.id)
    return {'product': api._response(source, actor), 'bom': bom}
