"""Save current default destinations for explicit shared manufacturing members.

This changes preferences only. Physical lots, reservations and shelf bindings
remain governed by their original inventory operations.
"""
from fastapi import HTTPException
from sqlalchemy import update

from app.models.product import Product
from app.models.receipt_putaway import ProductStoragePreference
from app.models.shared_finished_stock import SharedFinishedGroup
from app.services import shared_product_master as master, receipt_putaway
from app.services.bom_transactions import atomic_bom


def members(db, product_id):
    product = db.get(Product, product_id)
    pair = master._pair(db, product) if product else None
    return pair if pair and master.receipt(db, pair[0]) else (None, [product] if product else [])


def info(db, product_id):
    gid, _ = members(db, product_id)
    return {**receipt_putaway.info(db, product_id),
            'shared_group_version': db.get(SharedFinishedGroup, gid).version if gid else None}


def save(db, product_id, *, actor, expected_shared_group_version=None, **values):
    from app.api.deps import require_customer_access, has_permission
    from app.services.shared_product_structure import _check_members
    from app.services.audit_log import append_audit_event
    if actor.role not in {'admin', 'boss'} or not has_permission(actor, 'warehouse.execute'):
        raise HTTPException(403, '仅具有仓库操作权限的管理员或老板可设置默认货位')
    with atomic_bom(db):
        gid, products = members(db, product_id)
        if not products:
            raise HTTPException(404, '产品不存在')
        if gid:
            _check_members(db, products, actor)
            group = db.get(SharedFinishedGroup, gid)
            expected = expected_shared_group_version if expected_shared_group_version is not None else group.version
            claimed = db.execute(update(SharedFinishedGroup).where(SharedFinishedGroup.id == gid,
                SharedFinishedGroup.version == expected).values(version=SharedFinishedGroup.version + 1))
            if claimed.rowcount != 1:
                raise HTTPException(409, '共用产品或默认位置已变化，请重新打开常用箱；本次全部未保存')
        elif expected_shared_group_version is not None:
            raise HTTPException(409, '共用范围已变化，请重新打开常用箱')
        versions, before = {}, {}
        for product in products:
            require_customer_access(product.customer_id, current_user=actor, db=db)
            row = db.get(ProductStoragePreference, product.id)
            versions[product.id] = row.version if row else 0
            before[product.id] = ({'version': row.version, 'area_id': row.area_id, 'location_id': row.location_id}
                                  if row else None)
        versions[product_id] = values['expected_version']
        for product in products:
            receipt_putaway.save(db, product.id, **{**values, 'expected_version': versions[product.id]})
        append_audit_event(db, event_category='business', result='success', source='web', module_code='warehouse',
            action_code='shared_product_storage_save', resource='products', actor=actor,
            entity_type='product', entity_id=product_id,
            details={'product_ids': [p.id for p in products], 'group_id': gid,
                     'before_preferences': before, 'area_id': values['area_id'], 'location_id': values['location_id']})
        return info(db, product_id)
