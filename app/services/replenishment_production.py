"""Replenishment-owned production over the existing inventory ledger.

Demand sheets, planned product coverage and real completed products are separate
facts. No sales order or receipt is manufactured to make stock usable.
"""
import json
from decimal import Decimal
from math import ceil
from sqlalchemy import select, update
from fastapi import HTTPException
from app.models.replenishment_production import ReplenishmentProductionJob as Job
from app.models.scrap_production import ScrapProductionJob as Scrap
from app.models.stock_preparation import StockPreparationCommand as Command
from app.models.stock_replenishment import StockReplenishmentOrderItem as Item, StockReplenishmentOrder as Order
from app.models.warehouse_inventory import InventoryLot, InventoryReservation, SemiFinishedInventoryDetail
from app.models.product import Product
from app.services.shared_customer_access import canonical, digest
from app.services.warehouse_inventory import WarehouseInventoryError, _balances, _movement
from app.services.bom_transactions import atomic_bom
from app.core.time_contract import utc_now_naive, beijing_today


def fail(message):
    raise WarehouseInventoryError(message, 409)


def coverage_map(db, items):
    ids=[i.id for i in items]
    result={i:dict(pending_output=0,completed_output=0,jobs=[]) for i in ids}
    if not ids:return result
    for model,kind in ((Job,'sheet'),(Scrap,'scrap')):
        for j in db.scalars(select(model).where(model.stock_replenishment_item_id.in_(ids), model.status!='cancelled')):
            value=result[j.stock_replenishment_item_id]
            quantity=(j.quantity if kind=='scrap' else j.expected_output if j.status=='pending' else j.actual_output)
            value['pending_output' if j.status=='pending' else 'completed_output']+=quantity
            value['jobs'].append(dict(kind=kind,id=j.id,version=j.version,status=j.status,quantity=quantity))
    return result


def coverage(db,item):
    return coverage_map(db,[item])[item.id]


def purchase_block(db,item):
    from app.models.procurement_source import ProcurementSourceLink
    from app.models.incoming_receipt import IncomingReceiptItem
    if db.scalar(select(ProcurementSourceLink.id).where(ProcurementSourceLink.stock_replenishment_item_id==item.id,
            ProcurementSourceLink.status=='active').limit(1)):
        return '已生成采购单，请先从原采购入口撤销未收料采购，再调整生产来源'
    if item.stocked_quantity or item.inventory_lot_id or db.scalar(select(IncomingReceiptItem.id).where(
            IncomingReceiptItem.stock_replenishment_item_id==item.id,IncomingReceiptItem.status=='posted').limit(1)):
        return '已有实际收料，请保留原收料及生产流程，不能重复覆盖补库需求'
    return None


def source(db,item_id,actor):
    item=db.get(Item,item_id)
    if not item:raise HTTPException(404,'补库明细不存在')
    from app.api.requisition import _require_stock_replenishment_order_access
    _require_stock_replenishment_order_access(db,item.order,actor)
    return item


def production_identity(db,item):
    from app.services.stock_replenishment_plan import frozen_bom_plan
    if (item.order.status=='voided' or not item.order.request_hash or item.order.source_type=='manual_history'
            or item.target_inventory_type!='semi_finished' or item.procurement_route_snapshot!='paperboard' or item.component_type!='whole' or frozen_bom_plan(item)):
        fail('此来源不属于普通产品纸板补库，请使用原对应组件或收料流程')
    product=db.get(Product,item.reference_product_id or item.product_id)
    if (not product or not product.is_active or product.deleted_at or product.purged_at
            or product.customer_id!=item.customer_id):fail('补库目标产品或客户已变化')
    from app.services.bom_inventory_contract import product_has_assembly
    if product.is_composite or product_has_assembly(db,product.id):fail('组合产品须按真实子件用量安排，不能直接生产母件')
    from app.services.stock_purchase_identity import resolve,_view
    identity=resolve(db,item,product);view=_view(identity,item);view.is_active=True
    if view.supply_mode!='corrugated_production':fail('外购包材不能使用片料自制')
    return identity,view


def state(db,item,*,cover=None):
    from app.services.unified_procurement import stock_snapshot
    cover=coverage(db,item) if cover is None else cover
    pieces=max(int(item.pieces_per_box),1);factor=max(int(item.stock_yield_per_sheet),1)
    required=item.quantity*factor//pieces
    used=(cover['pending_output']+cover['completed_output'])*pieces
    remaining_piece=max(item.quantity*factor-used,0)
    return dict(stock_replenishment_item_id=item.id,required_output=required,
        covered_output=cover['pending_output']+cover['completed_output'],**cover,
        remaining_output=max(required-cover['pending_output']-cover['completed_output'],0),
        remaining_sheet_quantity=ceil(remaining_piece/factor),
        can_purchase=not bool(cover['pending_output']),
        purchase_blocked_reason='请先登记片料／废料实际完工或取消安排，再按实际余量报料' if cover['pending_output'] else None,
        source_fingerprint=digest(dict(source=stock_snapshot(item,item.order),coverage=cover)))


def requirement(item,view):
    from app.services.semi_finished_inventory import SemiFinishedSignature
    from app.services.warehouse_inventory import normalize_material_code
    from app.services.sheet_cutting_contract import SheetCuttingContract
    raw=item.sheet_cutting_snapshot
    contract=SheetCuttingContract.from_snapshot(json.loads(raw) if isinstance(raw,str) else raw) if raw else None
    length=contract.theoretical_length_mm if contract else item.report_length_mm
    width=contract.theoretical_width_mm if contract else item.report_width_mm
    factor=contract.mold_count if contract else item.stock_yield_per_sheet
    return SemiFinishedSignature(customer_id=item.customer_id,board_length_mm=length,board_width_mm=width,
        normalized_material_code=normalize_material_code(item.material_code_snapshot),flute_type=item.flute_type,
        component_type=item.component_type,pieces_per_box=item.pieces_per_box,stock_yield_per_sheet=factor)


def candidate(db,item,view,lot):
    from app.services.semi_finished_inventory import ensure_semi_finished_lot_eligibility
    from app.services.sheet_cut_plan import rectangular_cut_plan
    from app.services.processed_sheet_matching import processed_match
    from app.api.deps import require_customer_access
    expected=requirement(item,view)
    ensure_semi_finished_lot_eligibility(db,lot=lot,product_id=view.id,customer_id=item.customer_id,
        expected=expected,reviewed=True,allow_rotation=True,confirm_processed_use=True,frozen_product=view)
    detail=lot.semi_finished_detail
    if detail.flute_type!=expected.flute_type or detail.layer_count!=item.layer_count:
        fail('片料楞型或层数与冻结需求不同')
    plan=rectangular_cut_plan(db,lot,view,expected,reviewed=True,allow_rotation=True)
    processed=processed_match(db,lot,view,expected)
    factor=int(plan['yield_factor']) if plan else int(detail.stock_yield_per_sheet)
    if factor<=0:fail('片料缺少可核对的产出换算')
    from app.services.stock_preparation import location_name
    from app.services.warehouse_goods import goods_profile
    return dict(inventory_lot_id=lot.id,version=lot.version,available_quantity=lot.quantity_available,
        board_length_mm=detail.board_length_mm,board_width_mm=detail.board_width_mm,
        material_code=detail.material_code_snapshot,flute_type=detail.flute_type,
        location=location_name(db,lot),yield_per_sheet=factor,pieces_per_box=item.pieces_per_box,
        cut_plan=plan,recommendation_label='交换方向' if plan and plan.get('rotated') else '大张裁切／模切' if plan and
            (detail.board_length_mm,detail.board_width_mm)!=(expected.board_length_mm,expected.board_width_mm) else '同尺寸',
        warning_messages=([processed['reason']] if processed and processed.get('usage_confirmation_required') else [])+['请按本次片料的尺寸、材质及加工状态确认采用；保留原批次成本。'],
        qualification=dict(profile=goods_profile(db,lot),
            owner_customer_id=detail.owner_customer_id,customer_generic_eligible=detail.customer_generic_eligible,
            allowed_products=sorted(p.product_id for p in lot.allowed_products),sheet_type=detail.sheet_type,
            layer_count=detail.layer_count,processed_match=processed))


def job_row(db,job):
    reservation=db.get(InventoryReservation,job.reservation_id);lot=db.get(InventoryLot,reservation.inventory_lot_id)
    snap=json.loads(job.snapshot_json)
    from app.services.stock_preparation import location_name
    return dict(id=job.id,version=job.version,status=job.status,stock_replenishment_item_id=job.stock_replenishment_item_id,
        product_id=job.product_id,product_code=snap['code'],product_name=snap['name'],source_kind='sheet',
        input_quantity=job.input_quantity,expected_output=job.expected_output,actual_output=job.actual_output,
        inventory_lot_id=lot.id,lot_version=lot.version,location=location_name(db,lot),output_lot_id=job.output_lot_id)


def options(db,item_id,actor):
    item=source(db,item_id,actor);value=state(db,item)
    value.update(product_id=item.reference_product_id or item.product_id,product_code=item.product_code_snapshot,
        product_name=item.product_name_snapshot,order_number=item.order.order_number,candidates=[],variants=[],storage=None,
        jobs=[job_row(db,j) for j in db.scalars(select(Job).where(Job.stock_replenishment_item_id==item.id).order_by(Job.id))])
    from app.services.scrap_production import row,storage
    value['scrap_jobs']=[row(db,j) for j in db.scalars(select(Scrap).where(Scrap.stock_replenishment_item_id==item.id).order_by(Scrap.id))]
    try:
        identity,view=production_identity(db,item)
        value['blocked_reason']=purchase_block(db,item)
        product=db.get(Product,view.id);value['storage']=storage(db,product);value['variants']=product.production_variants or []
        if not value['blocked_reason'] and value['remaining_output']:
            from app.api.deps import customer_scope_ids,has_unrestricted_customer_access
            scope=None if has_unrestricted_customer_access(actor,db) else set(customer_scope_ids(actor,db))
            lots=db.scalars(select(InventoryLot).join(SemiFinishedInventoryDetail).where(
                InventoryLot.status=='active',InventoryLot.quantity_available>0,
                SemiFinishedInventoryDetail.flute_type==item.flute_type,
                SemiFinishedInventoryDetail.component_type==item.component_type).order_by(InventoryLot.id))
            for lot in lots:
                owner=lot.semi_finished_detail.owner_customer_id
                if scope is not None and owner is not None and owner not in scope:continue
                try:value['candidates'].append(candidate(db,item,view,lot))
                except WarehouseInventoryError:continue
            value['candidates'].sort(key=lambda c:({'同尺寸':0,'大张裁切／模切':1,'交换方向':2}[c['recommendation_label']],c['inventory_lot_id']))
    except WarehouseInventoryError as e:value['blocked_reason']=str(e)
    return value


def preview(db,item_id,payload,actor):
    item=source(db,item_id,actor);identity,view=production_identity(db,item)
    blocked=purchase_block(db,item)
    if blocked:fail(blocked)
    current=state(db,item)
    if payload.get('source_fingerprint')!=current['source_fingerprint']:fail('补库需求或生产覆盖已变化，请刷新')
    lot=db.get(InventoryLot,payload.get('inventory_lot_id'))
    if not lot or lot.status!='active' or lot.version!=payload.get('lot_version'):fail('片料库存已变化，请刷新')
    from app.api.deps import require_customer_access
    if lot.semi_finished_detail and lot.semi_finished_detail.owner_customer_id:
        require_customer_access(lot.semi_finished_detail.owner_customer_id,actor,db)
    chosen=candidate(db,item,view,lot)
    quantity=payload['quantity']
    if quantity<=0 or quantity>lot.quantity_available:fail('投入张数必须大于0且不超过可用库存')
    expected=quantity*chosen['yield_per_sheet']//item.pieces_per_box
    # One last sheet may produce physical surplus; never allocate more whole
    # sheets than the uncovered demand needs.
    maximum=ceil(current['remaining_output']*item.pieces_per_box/chosen['yield_per_sheet'])
    if expected<=0 or quantity>maximum:fail('投入张数超过当前补库缺口或不足以产出一个产品')
    from app.services.inventory_valuation import require_inherited_entry_cost
    require_inherited_entry_cost(db,lot)
    value=dict(stock_replenishment_item_id=item.id,source_fingerprint=current['source_fingerprint'],
        input_quantity=quantity,expected_output=expected,product_id=view.id,purchase_identity=identity,
        physical_basis=identity['physical_basis'],candidate=chosen,code=item.product_code_snapshot,name=item.product_name_snapshot,
        unit_cost=str(lot.estimated_unit_cost_snapshot),actor_id=actor.id)
    value['preview_hash']=digest(value)
    return value


def checked_command(db,item_id,payload,actor):
    source(db,item_id,actor)
    old=db.get(Command,payload['operation_key'])
    request=canonical(dict(payload,stock_replenishment_item_id=item_id,actor_id=actor.id))
    if old:
        if old.request_json!=request or old.actor_id!=actor.id:fail('操作标识已用于其他内容或账号')
        return json.loads(old.result_json)
    return None


def refresh(db,item,actor):
    db.flush()
    from app.services.replenishment_receipt_progress import refresh_order_progress
    refresh_order_progress(db,item.order,actor.id)


def mutate(db,item_id,payload,actor):
    with atomic_bom(db):
        old=checked_command(db,item_id,payload,actor)
        if old:return old
        item=source(db,item_id,actor)
        if item.order.status=='voided':fail('补库单已撤销')
        action=payload['action']
        if action=='plan':
            snap=preview(db,item_id,payload,actor)
            if snap['preview_hash']!=payload.get('preview_hash'):fail('片料资格、数量或需求已变化，请重新核对')
            lot=db.get(InventoryLot,snap['candidate']['inventory_lot_id']);quantity=snap['input_quantity'];before=_balances(lot)
            reservation=InventoryReservation(reservation_number='RSP-'+digest(payload['operation_key'])[:20],
                inventory_lot_id=lot.id,reservation_type='semi_order',reserved_stock_quantity=quantity,status='active',
                reserved_by=actor.id,reserved_at=utc_now_naive(),idempotency_key='rsp:'+payload['operation_key'],
                warning_codes='replenishment_production',reservation_group_key='rsp:'+payload['operation_key'])
            db.add(reservation);db.flush()
            job=Job(stock_replenishment_item_id=item.id,reservation_id=reservation.id,product_id=snap['product_id'],
                snapshot_json=canonical(snap),input_quantity=quantity,expected_output=snap['expected_output'],actor_id=actor.id)
            db.add(job);db.flush()
            values=dict(quantity_available=InventoryLot.quantity_available-quantity,quantity_reserved=InventoryLot.quantity_reserved+quantity)
            movement='reserve'
        else:
            job=db.get(Job,payload.get('job_id'))
            if not job or job.stock_replenishment_item_id!=item.id or job.status!='pending' or job.version!=payload.get('job_version'):
                fail('补库生产任务已变化，请刷新')
            if purchase_block(db,item):fail('已有采购或收料，不能改变生产覆盖；请先核对原采购来源')
            reservation=db.get(InventoryReservation,job.reservation_id);lot=db.get(InventoryLot,reservation.inventory_lot_id)
            if (reservation.status!='active' or reservation.reserved_stock_quantity!=job.input_quantity
                    or reservation.consumed_stock_quantity or lot.quantity_reserved<job.input_quantity
                    or lot.version!=payload.get('lot_version') or lot.status!='active'):fail('预占或库存版本已变化，请刷新')
            quantity=job.input_quantity;before=_balances(lot);snap=json.loads(job.snapshot_json)
            if action=='cancel':
                values=dict(quantity_available=InventoryLot.quantity_available+quantity,quantity_reserved=InventoryLot.quantity_reserved-quantity)
                reservation.status='released';reservation.released_stock_quantity=quantity
                reservation.released_by=actor.id;reservation.released_at=utc_now_naive();job.status='cancelled';movement='release_reserve'
            elif action=='complete':
                actual=payload['actual_output']
                if actual<=0:fail('请填写本次实际合格产出')
                if actual>job.expected_output and not payload.get('confirm_overproduction'):fail('实际产出超过理论，请核对并确认超产')
                # Frozen input identity remains binding; current master edits
                # cannot silently rewrite a pending job.
                from app.services.stock_purchase_identity import _view
                frozen=_view(snap['purchase_identity'],item);frozen.is_active=True
                current=candidate(db,item,frozen,lot)
                for field in ('qualification','cut_plan','yield_per_sheet','pieces_per_box'):
                    if canonical(current[field])!=canonical(snap['candidate'][field]):fail('片料用途或加工身份已变化，请取消后重新安排')
                from app.services.inventory_valuation import require_inherited_entry_cost
                require_inherited_entry_cost(db,lot)
                if Decimal(str(lot.estimated_unit_cost_snapshot))!=Decimal(snap['unit_cost']):fail('原片料冻结成本已变化，请核对')
                from app.services.warehouse_inventory import manual_finished_in
                from app.services.stock_completion_basis import completion_basis
                basis,_=completion_basis(db,item,dict(physical_basis=snap['physical_basis'],purchase_identity=snap['purchase_identity'],product_id=job.product_id))
                out=manual_finished_in(db,customer_id=item.customer_id,product_id=job.product_id,
                    location_id=payload['location_id'],quantity=actual,stock_date=beijing_today(),source_type='production_completion',
                    remarks='库存片料补库生产',operator_id=actor.id,idempotency_key='rsp-output:'+str(job.id),
                    source_ref_type='replenishment_production',source_ref_id=job.id,physical_basis_json=basis,
                    expected_layout_version=payload['layout_version'],movement_reason='补库片料实际完工')
                detail=out.finished_detail
                if detail:
                    frozen_basis=json.loads(basis);fields=snap['purchase_identity']['fields']
                    detail.inventory_code_snapshot=snap['code'];detail.product_name_snapshot=snap['name']
                    detail.material_code_snapshot=frozen_basis.get('material')
                    detail.flute_type_snapshot=frozen_basis.get('flute');detail.box_type_snapshot=frozen_basis.get('box_style')
                    for axis in ('length_mm','width_mm','height_mm'):
                        value=fields.get(axis)
                        setattr(detail,axis,round(Decimal(str(value))) if value is not None else None)
                total=Decimal(snap['unit_cost'])*quantity
                out.estimated_unit_cost_snapshot=total/actual;out.cost_snapshot_source='replenishment_production'
                out.cost_snapshot_detail_json=canonical(dict(job_id=job.id,source_lot_id=lot.id,input_quantity=quantity,
                    output_quantity=actual,total_cost=str(total)))
                job.output_lot_id=out.id;job.actual_output=actual;job.status='completed'
                reservation.status='consumed';reservation.consumed_stock_quantity=quantity
                reservation.consumed_by=actor.id;reservation.consumed_at=utc_now_naive()
                values=dict(quantity_reserved=InventoryLot.quantity_reserved-quantity,quantity_consumed=InventoryLot.quantity_consumed+quantity)
                movement='consume'
            else:fail('不支持的补库生产操作')
            job.version+=1
        changed=db.execute(update(InventoryLot).where(InventoryLot.id==lot.id,InventoryLot.version==payload['lot_version'],
            InventoryLot.status=='active').values(**values,version=InventoryLot.version+1,last_movement_at=utc_now_naive()))
        if changed.rowcount!=1:fail('库存已被其他操作修改，请刷新')
        db.flush();db.refresh(lot)
        _movement(db,lot=lot,movement_type=movement,quantity=quantity,before=before,operator_id=actor.id,
            reservation_id=reservation.id,reason={'plan':'补库采用库存片料','cancel':'取消补库片料安排','complete':'补库生产消耗片料'}[action],
            idempotency_key='rsp-move:'+payload['operation_key'])
        refresh(db,item,actor)
        result=job_row(db,job)
        db.add(Command(operation_key=payload['operation_key'],receipt_item_id=None,
            request_json=canonical(dict(payload,stock_replenishment_item_id=item_id,actor_id=actor.id)),
            result_json=canonical(result),actor_id=actor.id))
        from app.services.audit_log import append_audit_event
        append_audit_event(db,event_category='business',result='success',source='web',module_code='production',
            action_code='replenishment_production.'+action,resource='production',actor=actor,
            entity_type='stock_replenishment_item',entity_id=item.id,details=dict(result,action=action))
        db.flush();return result


def jobs(db,actor,include_completed=False):
    from app.services.scrap_production import row
    result=[]
    for model,kind in ((Job,'sheet'),(Scrap,'scrap')):
        query=select(model).where(model.stock_replenishment_item_id.is_not(None))
        if not include_completed:query=query.where(model.status=='pending')
        for job in db.scalars(query.order_by(model.id)):
            try:item=source(db,job.stock_replenishment_item_id,actor)
            except HTTPException as e:
                if e.status_code==403:continue
                raise
            result.append(dict(job_row(db,job) if kind=='sheet' else row(db,job),source_kind=kind,
                stock_replenishment_item_id=item.id,order_number=item.order.order_number))
    return result


def frozen_cost(db,lot,visited):
    from app.services.inventory_transfer_lineage import is_original_or_location_split
    from app.services.inventory_valuation import frozen_cost as cost
    if lot.id in visited:return None,{'validation_issue':'补库生产成本循环'}
    try:
        job=db.get(Job,lot.source_ref_id) if lot.source_ref_type=='replenishment_production' else None
        detail=json.loads(lot.cost_snapshot_detail_json or '{}')
        reservation=db.get(InventoryReservation,job.reservation_id) if job else None
        origin=db.get(InventoryLot,reservation.inventory_lot_id) if reservation else None
        if (not job or job.status!='completed' or not is_original_or_location_split(db,lot,job.output_lot_id)
                or not origin or reservation.status!='consumed' or reservation.consumed_stock_quantity!=job.input_quantity
                or detail.get('job_id')!=job.id or detail.get('source_lot_id')!=origin.id
                or detail.get('input_quantity')!=job.input_quantity or detail.get('output_quantity')!=job.actual_output):
            raise ValueError('补库片料产出来源不一致')
        from app.models.warehouse_inventory import InventoryMovement
        movement=db.scalar(select(InventoryMovement.id).where(InventoryMovement.reservation_id==reservation.id,
            InventoryMovement.inventory_lot_id==origin.id,InventoryMovement.movement_type=='consume',
            InventoryMovement.quantity==job.input_quantity).limit(1))
        snap=json.loads(job.snapshot_json);physical=lot.finished_detail
        if (not movement or not physical or physical.product_id!=job.product_id
                or physical.owner_customer_id!=snap['purchase_identity']['customer_id']):
            raise ValueError('补库产出缺少一致的实际消耗或产品身份')
        unit,evidence=cost(origin,db,set(visited)|{lot.id})
        total=Decimal(detail['total_cost']);quantity=job.actual_output
        if unit is None or quantity<=0 or abs(unit*job.input_quantity-total)>Decimal('.01') or abs(total/quantity-Decimal(str(lot.estimated_unit_cost_snapshot)))>Decimal('.0001'):
            raise ValueError('补库片料产出成本不守恒')
        return total/quantity,dict(currency='CNY',cost_label='补库生产继承片料成本',total_cost=str(total),quantity=quantity,source_evidence=evidence)
    except (TypeError,ValueError,KeyError,ArithmeticError) as e:return None,{'validation_issue':str(e)}


def purchased_sheet_quantity(db,item):
    """The one original supplier link owns actual receipt quantities."""
    from app.models.procurement_source import ProcurementSourceLink
    link=db.scalar(select(ProcurementSourceLink).where(ProcurementSourceLink.stock_replenishment_item_id==item.id,
        ProcurementSourceLink.status=='active'))
    return int(link.source_quantity) if link else int(item.quantity)
