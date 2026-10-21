"""Exact, audited release repair over the current database; no old DB restore."""
from decimal import Decimal
import json
from sqlalchemy import select
from app.models.product import Product
from app.models.material import Material
from app.models.order import Order,OrderItem
from app.models.shared_finished_stock import SharedFinishedGroup,SharedFinishedMember,SharedFinishedMutation
from app.services.shared_customer_access import canonical,digest
from app.services.bom_transactions import atomic_bom
from app.services.shared_finished_stock import _error


def _apply_current(db,p,updates,actor,reason):
    from app.services.master_data_versioning import preview_versioned_update,_apply_versioned_update
    check=preview_versioned_update(db,object_type='product',entity=p,updates=updates,expected_version=p.version,user=actor)
    return _apply_versioned_update(db,object_type='product',entity=p,updates=updates,expected_version=p.version,
        user=actor,reason=reason,source='shared_catalog_release_repair',confirmation_token=check['confirmation_token'])


def apply(db,*,manifest,actor,backup_sha256,operation_key='shared-catalog-20261021-v2'):
    if not actor or actor.role!='admin' or not actor.is_active or len(backup_sha256)!=64:
        raise _error('须由活动管理员绑定本次已验证恢复点执行',403)
    request=canonical(dict(manifest_sha256=manifest['sha256'],actor_id=actor.id,backup_sha256=backup_sha256))
    from app.services import shared_product_master as master,customer_sharing as catalog
    from app.api.deps import require_customer_access
    with atomic_bom(db):
        old=db.scalar(select(SharedFinishedMutation).where(SharedFinishedMutation.operation_key==operation_key))
        if old:
            if old.request_json!=request:raise _error('该修正事务已绑定其它候选或恢复点')
            return dict(json.loads(old.result_json),replayed=True)
        check={k:v for k,v in manifest.items() if k!='sha256'}
        if digest(check)!=manifest['sha256']:raise _error('修正清单摘要不一致')
        for cid in (137,138):require_customer_access(cid,current_user=actor,db=db)
        for row in manifest['price_changes']:
            p=db.get(Product,row['product_id'])
            if (not p or p.version!=row['version'] or p.product_code!=row['code'] or p.customer_id!=row['customer_id']
                    or not p.is_active or p.is_internal_component or p.deleted_at or p.purged_at):raise _error('当前报价资料已变化，未执行本次清单')
            for field,value in row['before'].items():
                current=getattr(p,field)
                if (current is None)!=(value is None) or (current is not None and Decimal(str(current))!=Decimal(str(value))):
                    raise _error('当前报价数值变化，未执行本次清单')
        for row in manifest['groups']:
            group=db.get(SharedFinishedGroup,row['group_id'])
            ids=sorted(db.scalars(select(SharedFinishedMember.product_id).where(SharedFinishedMember.group_id==row['group_id'])))
            if not group or group.version!=row['version'] or not group.enabled or ids!=row['product_ids']:
                raise _error('共用范围或版本已变化，未执行本次清单')
        for identity, version in manifest['product_versions'].items():
            p=db.get(Product,int(identity))
            if not p or p.version!=version:
                raise _error('共用产品或备用模具资料已变化，须刷新审查清单')
        price_count=0
        for row in manifest['price_changes']:
            p=db.get(Product,row['product_id']);price=Decimal(row['price'])
            _apply_current(db,p,dict(sale_unit_price=price,sale_unit_price_no_tax=price),actor,
                '老板提供原表价格列并明确未税口径；仅当前常用箱报价修正，历史金额不重算')
            price_count+=1
        materials=[]
        for row in manifest['current_material_corrections']:
            p=db.get(Product,row['product_id']);new=db.get(Material,row['new_material_id'])
            if p and new and p.material_id==new.id:continue
            old_material=db.get(Material,p.material_id)
            if (not new or not new.is_active or not old_material or old_material.is_active
                    or old_material.id!=row['old_material_id'] or old_material.code!=new.code):
                raise _error('已核实的停用材质引用发生变化，未执行本次修正')
            _apply_current(db,p,dict(material_id=new.id),actor,row['reason']);materials.append(p.id)
        alt=manifest['alternative_molds']
        from app.services import reviewed_alternative_molds
        plan=reviewed_alternative_molds.preview(db,alt['parent_id'],alt['peer_id'],alt['default_child_id'])
        if sorted(c['id'] for c in plan['children'])!=alt['expected_children']:raise _error('替代模具原资料已变化')
        alternative=reviewed_alternative_molds.apply(db,plan=plan,actor=actor,operation_key=operation_key+'-molds',evidence=alt['evidence'])
        reconciled=[]
        for row in manifest['groups']:
            if row['group_id']==plan['group_id']:continue
            current=master.preview_reconcile(db,row['source_id'])
            master.reconcile(db,product_id=row['source_id'],preview_hash=current['sha256'],
                operation_key=operation_key+'-group-'+str(row['group_id']),
                evidence='老板明确研光、光洋同编码现货实物相同；当前资料差异属录入问题，保留冻结旧批次',actor=actor)
            reconciled.append(row['group_id'])
        # Only already-confirmed same-code roles are connected initially. A
        # private product is not copied merely because it shares a customer.
        source_ids=[r['source_id'] for r in manifest['groups']]
        args=dict(customer_id=138,source_customer_id=137,product_ids=source_ids,new_products_shared=True,
            delivery_heading_customer_id=None,delivery_address_customer_id=None)
        preview=catalog.preview(db,actor=actor,**args)
        settings=catalog.confirm(db,actor=actor,preview_hash=preview['preview_hash'],**args)
        order=db.get(Order,manifest['order_id'])
        if not order or order.order_number!=manifest['order_number'] or order.customer_id!=138:
            raise _error('原光洋订单身份已变化，未补占')
        allocations=[];pending=[]
        if order.status not in ('voided','cancelled','delivered','completed'):
            from app.api.requisition import _current_requisition_requirements
            from app.services.warehouse_inventory import finished_inventory_candidates_for_product,reserve_finished_inventory
            for item in db.scalars(select(OrderItem).where(OrderItem.order_id==order.id).order_by(OrderItem.id)):
                p=db.get(Product,item.product_id)
                if not p or p.is_composite or p.supply_mode!='corrugated_production':
                    pending.append(dict(item_id=item.id,reason='保留原BOM或外购流程'));continue
                requirement=_current_requisition_requirements(db,item)
                need=int(requirement['remaining_required_piece_qty'])//int(requirement['pieces_per_box'])
                for lot in finished_inventory_candidates_for_product(db,customer_id=order.customer_id,product_id=p.id):
                    if need<=0:break
                    if lot.finished_detail.is_general:continue
                    take=min(need,int(lot.quantity_available))
                    if take<=0:continue
                    reservation=reserve_finished_inventory(db,order_item_id=item.id,inventory_lot_id=lot.id,
                        quantity=take,expected_version=lot.version,operator_id=actor.id,
                        idempotency_key=operation_key+'-'+str(item.id)+'-'+str(lot.id),warning_acknowledged_codes=[])
                    allocations.append(dict(item_id=item.id,lot_id=lot.id,quantity=take,reservation_id=reservation.id));need-=take
                if need:pending.append(dict(item_id=item.id,code=p.product_code,remaining_quantity=need))
        result=dict(price_updates=price_count,material_updates=materials,alternative_molds=alternative,
            reconciled_groups=reconciled,customer_settings=settings,new_reservations=allocations,pending=pending,
            source_exceptions=manifest['exceptions'],replayed=False)
        db.add(SharedFinishedMutation(group_id=manifest['groups'][0]['group_id'],operation_key=operation_key,
            request_json=request,result_json=canonical(result)))
        db.flush();return result
