"""Off-ledger usable scrap: one work record, zero material and frozen labour.

This command cannot consume or zero an existing material lot. Real BOM parents
still require their original component-consumption command.
"""
from datetime import date
from decimal import Decimal
import json
from sqlalchemy import select, update, func
from fastapi import HTTPException
from app.models.scrap_production import ScrapProductionJob as Job
from app.models.stock_preparation import StockPreparationCommand as Command
from app.models.product import Product
from app.models.order import OrderItem
from app.services.shared_customer_access import canonical, digest
from app.services.bom_transactions import atomic_bom


def pending_quantity(db,item_id,exclude=None):
    if db is None:return 0
    q=select(func.coalesce(func.sum(Job.quantity),0)).where(Job.order_item_id==item_id,Job.status=='pending')
    if exclude:q=q.where(Job.id!=exclude)
    return int(db.scalar(q) or 0)


def row(db,job):
    snap=json.loads(job.snapshot_json)
    return dict(id=job.id,version=job.version,product_id=job.product_id,order_item_id=job.order_item_id,
        stock_replenishment_item_id=job.stock_replenishment_item_id,product_code=snap['code'],product_name=snap['name'],quantity=job.quantity,status=job.status,
        location_id=job.location_id,stock_date=job.stock_date,output_lot_id=job.output_lot_id,
        material_cost='0',labour_cost=snap['labour']['estimated_processing_cost'],variant=snap.get('variant'))


def product_for(db,product_id,actor):
    from app.api.deps import require_customer_access
    p=db.get(Product,product_id)
    if not p or not p.is_active or p.deleted_at or p.purged_at:raise HTTPException(409,'产品已停用或删除')
    require_customer_access(p.customer_id,current_user=actor,db=db)
    from app.services.bom_inventory_contract import product_has_assembly
    if p.is_composite or product_has_assembly(db,p.id):
        raise HTTPException(409,'本产品须真实消耗子件组套；请对实际加工的子件使用废料生产')
    if p.supply_mode!='corrugated_production':raise HTTPException(409,'外购包材不能登记为废料自制生产')
    from app.services.product_readiness import product_readiness
    readiness=product_readiness(p)
    if not readiness.get('ready'):
        raise HTTPException(409,'产品资料待完善：'+'；'.join(readiness.get('missing_labels') or ['请在常用箱核对资料']))
    return p


def storage(db,product):
    from app.services.receipt_putaway import info,resolve
    from app.services.fixed_shelf import ShelfError,location_info
    saved=info(db,product.id)
    if not saved['location_id']:
        try:
            found=resolve(db,product_id=product.id,customer_id=product.customer_id)
            if found:
                location,source,warning=found
                if location:saved.update(location_id=location.id,location=location_info(db,location),source=source,issue=warning)
        except ShelfError as e:saved['issue']=str(e)
    return saved


def options(db,product_id,order_item_id,actor,*,stock_replenishment_item_id=None):
    p=product_for(db,product_id,actor)
    from app.services.receipt_putaway import info
    item=db.get(OrderItem,order_item_id) if order_item_id else None
    remaining=None
    if item:
        if item.product_id!=p.id or item.order.customer_id!=p.customer_id:raise HTTPException(409,'订单与产品不一致')
        from app.api.requisition import _current_requisition_requirements
        requirement=_current_requisition_requirements(db,item)
        remaining=int(requirement['remaining_required_piece_qty'])//int(requirement['pieces_per_box'])
    if stock_replenishment_item_id:
        source=_replenishment_source(db,stock_replenishment_item_id,p,actor)
        if order_item_id:raise HTTPException(409,'订单与补库来源不能同时填写')
        from app.services.replenishment_production import state
        remaining=state(db,source)['remaining_output']
    return dict(product_id=p.id,code=p.product_code,name=p.product_name,version=p.version,
        variants=p.production_variants or [],storage=storage(db,p),remaining_quantity=remaining,
        jobs=[row(db,j) for j in db.scalars(select(Job).where(Job.product_id==p.id,Job.status=='pending',
            Job.stock_replenishment_item_id==stock_replenishment_item_id if stock_replenishment_item_id else Job.stock_replenishment_item_id.is_(None)).order_by(Job.id))])


def _replenishment_source(db,item_id,product,actor):
    from app.services.replenishment_production import source,production_identity,purchase_block
    item=source(db,item_id,actor)
    identity,view=production_identity(db,item)
    if view.id!=product.id:raise HTTPException(409,'补库来源与目标产品不一致')
    blocked=purchase_block(db,item)
    if blocked:raise HTTPException(409,blocked)
    from app.services.finished_stock_identity import product_basis,matches_stock_identity
    if not matches_stock_identity(product_basis(product),identity['physical_basis']):
        raise HTTPException(409,'当前产品与补库冻结身份不同，请核对原补库加工资料')
    return item


def preview(db,payload,actor):
    p=product_for(db,payload['product_id'],actor)
    from app.services.production_variants import resolve_variant
    try:batch,variant=resolve_variant(db,p,payload.get('variant_id'))
    except ValueError as e:raise HTTPException(409,str(e)) from e
    replenishment=None
    if payload.get('stock_replenishment_item_id'):
        if payload.get('order_item_id'):raise HTTPException(409,'订单与补库来源不能同时填写')
        replenishment=_replenishment_source(db,payload['stock_replenishment_item_id'],p,actor)
    quantity=payload['quantity']
    if quantity<=0:raise HTTPException(422,'请输入实际合格数量')
    item=db.get(OrderItem,payload.get('order_item_id')) if payload.get('order_item_id') else None
    if item:
        if item.product_id!=p.id or item.order.customer_id!=p.customer_id or item.order.status in ('voided','cancelled','completed'):
            raise HTTPException(409,'订单或产品状态已变化，请刷新')
        from app.api.requisition import _current_requisition_requirements
        requirement=_current_requisition_requirements(db,item)
        if quantity*requirement['pieces_per_box']>requirement['remaining_required_piece_qty']:
            raise HTTPException(409,'本次数量超过未覆盖缺口；已有采购、领料或生产安排须先从原入口调整。独立备库请不关联订单')
    elif payload.get('order_item_id'):
        raise HTTPException(404,'订单明细不存在')
    if replenishment is not None:
        from app.services.replenishment_production import state
        if quantity>state(db,replenishment)['remaining_output']:
            raise HTTPException(409,'本次数量超过补库未覆盖缺口')
        if variant is not None:
            raise HTTPException(409,'补库加工身份已冻结，不能另选生产方案')
    from app.services.receipt_putaway import info,location_issue
    saved=storage(db,p)
    location_id=payload.get('location_id') or saved['location_id']
    from app.models.warehouse_inventory import WarehouseLocation
    location=db.get(WarehouseLocation,location_id) if location_id else None
    issue=location_issue(db,location)
    if issue:raise HTTPException(409,f"入库位置：{saved.get('issue') or issue}；请在常用箱存放位置修改")
    from app.services.processing_cost import estimate_standard_processing_cost
    try:labour=estimate_standard_processing_cost(db,product=batch,quantity=quantity)
    except ValueError as e:raise HTTPException(409,'人工成本待完善：'+str(e)+'。请在加工成本参数维护') from e
    if labour['calculation_status']!='calculated':
        raise HTTPException(409,'人工成本待完善：'+'；'.join(labour['missing_items'])+'。请在加工成本参数维护')
    if Decimal(labour['estimated_processing_cost'])<=0:
        raise HTTPException(409,'当前工序人工成本为0，请在加工成本参数补齐实际加工工序或费率')
    from app.services.finished_stock_identity import product_basis
    plan=dict(product_id=p.id,product_version=p.version,customer_id=p.customer_id,order_item_id=item.id if item else None,
        quantity=quantity,location_id=location.id,address_version=location.address_version,
        layout_version=location.floor3_layout.version if location.floor3_layout else None,
        code=p.product_code,name=p.product_name,physical_basis=product_basis(p),variant=variant,labour=labour,
        stock_date=str(payload['stock_date']),remarks=payload.get('remarks') or '',material_cost='0')
    if replenishment is not None:
        from app.services.replenishment_production import state
        plan['stock_replenishment_item_id']=replenishment.id
        plan['source_fingerprint']=state(db,replenishment)['source_fingerprint']
    plan['preview_hash']=digest(plan)
    return plan


def mutate(db,payload,actor):
    # Keep the historical request bytes for ordinary/off-source replay.
    payload=dict(payload)
    if payload.get('stock_replenishment_item_id') is None:
        payload.pop('stock_replenishment_item_id',None)
    from app.api.deps import require_customer_access
    from app.services.audit_log import append_audit_event
    request=canonical(dict(payload,actor_id=actor.id))
    with atomic_bom(db):
        old=db.get(Command,payload['operation_key'])
        if old:
            if old.request_json!=request or old.actor_id!=actor.id:raise HTTPException(409,'本次操作标识已用于其他内容')
            return json.loads(old.result_json)
        action=payload['action']
        if action in ('plan','finish_now'):
            plan=preview(db,payload,actor)
            if payload['preview_hash']!=plan['preview_hash']:raise HTTPException(409,'数量、资料、位置或人工参数已变化，请重新核对')
            claimed=db.execute(update(Product).where(Product.id==plan['product_id'],Product.version==plan['product_version'])
                .values(version=Product.version,updated_at=Product.updated_at))
            if claimed.rowcount!=1:raise HTTPException(409,'产品资料已变化，请重新核对')
            job=Job(customer_id=plan['customer_id'],product_id=plan['product_id'],order_item_id=plan['order_item_id'],
                stock_replenishment_item_id=plan.get('stock_replenishment_item_id'),quantity=plan['quantity'],location_id=plan['location_id'],stock_date=date.fromisoformat(plan['stock_date']),
                snapshot_json=canonical(plan),actor_id=actor.id,status='pending',version=1)
            db.add(job);db.flush()
        else:
            job=db.get(Job,payload.get('job_id'))
            if not job:raise HTTPException(404,'废料生产记录不存在')
            require_customer_access(job.customer_id,current_user=actor,db=db)
            if job.status!='pending' or job.version!=payload.get('job_version'):raise HTTPException(409,'生产状态已变化，请刷新')
            claimed=db.execute(update(Job).where(Job.id==job.id,Job.version==job.version,Job.status=='pending')
                .values(version=Job.version+1))
            if claimed.rowcount!=1:raise HTTPException(409,'生产状态已变化，本次未保存')
            plan=json.loads(job.snapshot_json)
        if job.stock_replenishment_item_id:
            if payload.get('stock_replenishment_item_id')!=job.stock_replenishment_item_id or payload.get('order_item_id'):
                raise HTTPException(409,'废料生产与补库来源不一致')
            if action=='cancel':
                from app.services.replenishment_production import source,purchase_block
                origin=source(db,job.stock_replenishment_item_id,actor)
                if origin.order.status=='voided' or purchase_block(db,origin):
                    raise HTTPException(409,'补库状态或采购已变化，不能取消原生产安排')
            else:
                _replenishment_source(db,job.stock_replenishment_item_id,db.get(Product,job.product_id),actor)
        if action=='cancel':job.status='cancelled'
        elif action in ('complete','finish_now'):
            product=product_for(db,job.product_id,actor)
            from app.services.finished_stock_identity import product_basis
            if product_basis(product)!=plan['physical_basis']:raise HTTPException(409,'产品实物资料已变化；请核对本批实物后重新安排')
            from app.services.warehouse_inventory import manual_finished_in
            lot=manual_finished_in(db,customer_id=job.customer_id,product_id=job.product_id,
                location_id=job.location_id,quantity=job.quantity,stock_date=job.stock_date,source_type='production_completion',
                source_ref_type='scrap_production',source_ref_id=job.id,operator_id=actor.id,
                physical_basis_json=plan['physical_basis'],remarks=plan['remarks'],
                idempotency_key='scrap-output:'+str(job.id),movement_reason='废料加工成品入库',
                expected_layout_version=plan['layout_version'])
            lot.estimated_unit_cost_snapshot=Decimal(plan['labour']['estimated_processing_cost'])/Decimal(job.quantity)
            lot.estimated_square_price_snapshot=Decimal('0');lot.estimated_cost_area_m2_snapshot=Decimal('0')
            lot.cost_snapshot_source='scrap_zero_material_frozen_labour'
            lot.cost_snapshot_detail_json=canonical({'schema':1,'source':'off_ledger_usable_scrap',
                'material_cost':'0','labour':plan['labour'],'variant':plan['variant'],'job_id':job.id})
            from app.core.time_contract import utc_now_naive
            lot.cost_snapshot_at=utc_now_naive()
            job.output_lot_id=lot.id;job.status='completed';db.flush()
            if job.order_item_id:
                from app.services.warehouse_inventory import active_finished_reserved_qty,reserve_finished_inventory
                item=db.get(OrderItem,job.order_item_id)
                need=max(int(item.quantity)-active_finished_reserved_qty(db,item.id),0)
                if need:
                    reserve_finished_inventory(db,order_item_id=item.id,inventory_lot_id=lot.id,
                        quantity=min(need,job.quantity),expected_version=lot.version,operator_id=actor.id,
                        idempotency_key='scrap-reserve:'+str(job.id),warning_acknowledged_codes=[])
        if job.stock_replenishment_item_id:
            from app.services.replenishment_production import source,refresh
            refresh(db,source(db,job.stock_replenishment_item_id,actor),actor)
        result=row(db,job)
        db.add(Command(operation_key=payload['operation_key'],receipt_item_id=None,request_json=request,
            result_json=canonical(result),actor_id=actor.id))
        append_audit_event(db,event_category='business',result='success',source='web',module_code='production',
            action_code='scrap_production_'+action,resource='products',actor=actor,
            entity_type='scrap_production_job',entity_id=job.id,details={'request':json.loads(request),'result':json.loads(canonical(result))})
        db.flush();return result


def frozen_cost(db,lot,detail):
    """Verify the exact completed work, not merely a client-supplied cost label."""
    try:
        from app.services.inventory_transfer_lineage import is_original_or_location_split
        job=db.get(Job,lot.source_ref_id) if db and lot.source_ref_type=='scrap_production' else None
        if not job or job.status!='completed' or not is_original_or_location_split(db,lot,job.output_lot_id):
            raise ValueError('废料生产完工来源不一致')
        snap=json.loads(job.snapshot_json)
        expected=dict(schema=1,source='off_ledger_usable_scrap',material_cost='0',labour=snap['labour'],variant=snap['variant'],job_id=job.id)
        if canonical(detail)!=canonical(expected):raise ValueError('废料生产冻结成本与本批依据不一致')
        physical=lot.finished_detail
        if (not physical or physical.product_id!=job.product_id or physical.owner_customer_id!=job.customer_id
                or physical.physical_basis_json!=snap['physical_basis'] or job.quantity<=0):
            raise ValueError('废料产出与本次实物身份不一致')
        total=Decimal(snap['labour']['estimated_processing_cost']);unit=total/Decimal(job.quantity)
        if total<=0 or abs(unit-Decimal(str(lot.estimated_unit_cost_snapshot)))>Decimal('0.0001'):
            raise ValueError('废料产出人工成本不守恒')
        return unit,dict(detail,currency='CNY',cost_label='废料加工：材料0元、冻结人工成本',total_cost=str(total),quantity=job.quantity)
    except (ValueError,TypeError,KeyError,ArithmeticError) as e:
        return None,dict(validation_issue=str(e))
