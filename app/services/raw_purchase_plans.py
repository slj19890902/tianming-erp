"""Explicit common-sheet purchases; final product requirements are never overwritten."""
import json
import hashlib
from types import SimpleNamespace
from sqlalchemy import select
from fastapi import HTTPException
from app.models.order import Order,OrderItem
from app.models.product import Product
from app.models.material import Material
from app.models.raw_purchase_plan import RawPurchasePlan,RawPurchaseDemand,RawPurchaseReceiptAllocation
from app.services.bom_transactions import atomic_bom
from app.core.time_contract import utc_now_naive


def encoded(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,default=str,separators=(',',':'))


def fingerprint(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def fail(message):
    raise HTTPException(409,message)


def active_order_items():
    return select(RawPurchaseDemand.order_item_id).where(RawPurchaseDemand.status=='active')


def assert_no_plan(db,item_ids):
    if db.scalar(select(RawPurchaseDemand.id).where(RawPurchaseDemand.order_item_id.in_(item_ids),RawPurchaseDemand.status=='active').limit(1)):
        fail('订单已有统一原片采购方案，请先核对并撤销未收料方案，不能重复采购或改写冻结需求')


def requirements(db,item_ids,actor):
    from app.api import requisition as req
    from app.api.deps import require_customer_access
    from app.services.multilevel_bom_cutover_review import _row
    from app.models.production import ProductionCompletion
    from app.models.multilevel_bom import OrderBomGraph
    if not item_ids or len(set(item_ids))!=len(item_ids): fail('请选择不重复的待报料订单')
    assert_no_plan(db,item_ids)
    rows=[];proof=[]
    for item_id in sorted(item_ids):
        item=db.get(OrderItem,item_id);order=db.get(Order,item.order_id) if item else None
        if not order: fail('原订单明细不存在')
        require_customer_access(order.customer_id,actor,db)
        if (order.status not in req.ORDER_ITEM_ACTIVE_ORDER_STATUSES or item.is_force_closed or item.delivered_quantity
                or item.material_status!='pending' or item.supply_mode_snapshot=='external_purchase'):
            fail('统一原片方案仅适用于有效、未收料未发货的自制需求')
        if db.scalar(select(ProductionCompletion.id).where(ProductionCompletion.order_item_id==item_id).limit(1)):
            fail('该订单已有加工历史，请保留原采购来源')
        product=db.get(Product,item.product_id)
        snapshots=req._bom_snapshots_for_order_item(db,item_id)
        if snapshots and not db.get(OrderBomGraph,item_id):
            fail('旧式组合订单请先完成已有受控BOM转换，不能猜测原片分配')
        component_rows=req._bom_pending_component_requirements(db,item,snapshots=snapshots) if snapshots else []
        if not req._composite_parent_requisition_is_suppressed(item,snapshots):
            component_rows=[] if not snapshots else component_rows
            for spec in req._semi_component_specs_for_requisition(item,product):
                route=spec['component_type']
                need=req._current_requisition_requirements(db,item,component_type=route)
                existing=req._active_supplier_requisition_facts(db,item=item,component_type=route)
                crease=req._component_crease(item,route)
                component_rows.append(dict(need,product_id=item.product_id,snapshot_id=None,
                    material_id=item.material_id,material=item.snapshot_material,layer_count=item.layer_count,
                    flute_type=item.flute_type,report_length_mm=spec['board_length_mm'],report_width_mm=spec['board_width_mm'],
                    product_code=item.snapshot_product_code,product_name=item.snapshot_product_name,
                    crease_type=crease[0],crease_left_mm=crease[1],crease_middle_mm=crease[2],crease_right_mm=crease[3],
                    already_requisitioned=existing['quantity']))
        for row in component_rows:
            if row.get('already_requisitioned'): fail('来源已经采购，请撤销原未收料采购后重新选择')
            quantity=int(row.get('remaining_required_piece_qty') or 0)
            if not quantity: continue
            route=row['component_type'];sid=row.get('snapshot_id')
            row.update(order_item_id=item_id,customer_id=order.customer_id,
                source_key=f'raw:{item_id}:{sid or 0}:{route}',piece_quantity=quantity,
                pieces_per_box=int(row.get('physical_pieces_per_component') or row.get('pieces_per_box') or 1))
            if any(not row.get(key) for key in ('material_id','flute_type','report_length_mm','report_width_mm')):
                fail('原需求的材质、楞型或最终尺寸未完整冻结，请先核对')
            rows.append(row)
        # The shared order lock touches updated_at even when no business field changes.
        proof.append(dict(item=_row(item),order={k:v for k,v in _row(order).items() if k!='updated_at'},snapshots=[_row(s) for s in snapshots]))
    if not rows: fail('所选订单没有需要采购的物理部件')
    return rows,proof


def preview(db,payload,actor):
    from app.api.requisition import _require_active_material_supplier
    rows,proof=requirements(db,payload['order_item_ids'],actor)
    customers={r['customer_id'] for r in rows};materials={r['material_id'] for r in rows};flutes={r['flute_type'] for r in rows}
    if len(customers)!=1 or len(materials)!=1 or len(flutes)!=1:
        fail('统一原片须同客户、同材质、同楞型；不同范围请分别生成方案')
    material=db.get(Material,next(iter(materials)))
    if not material or not material.is_active: fail('材质已停用或不存在')
    _require_active_material_supplier(db,material)
    # Supplier authority is still the validated material's real supplier.
    supplier_name=material.supplier_name
    if not supplier_name: fail('材质缺少有效供应商')
    selections={s['source_key']:s for s in payload['allocations']}
    if len(selections)!=len(payload['allocations']) or set(selections)!={r['source_key'] for r in rows}:
        fail('必须逐项确认当前全部需求分配，不能遗漏、重复或引入其他来源')
    allocated=0
    for row in rows:
        selection=selections[row['source_key']];factor=selection['yield_factor']
        length,width=row['report_length_mm'],row['report_width_mm']
        if selection['rotate']: length,width=width,length
        expected_direction=({'length':'width','width':'length'}[payload['flute_direction']] if selection['rotate'] else payload['flute_direction'])
        if selection['piece_flute_direction']!=expected_direction:
            fail('原片方向、旋转和最终部件方向不一致')
        trim=payload.get('trim_mm',0);kerf=payload.get('kerf_mm',0)
        capacity=max((payload['length_mm']-2*trim+kerf)//(length+kerf),0)*max((payload['width_mm']-2*trim+kerf)//(width+kerf),0)
        if factor<=0 or factor>capacity: fail('实际原片尺寸不足以完成所选方向和出数')
        if row['piece_quantity']%factor: fail('需求片数不能按该出数整张分配；请选择可整除的实际出数，余片须另行明确')
        sheets=row['piece_quantity']//factor;allocated+=sheets
        row.update(raw_quantity=sheets,yield_factor=factor,rotate=selection['rotate'],piece_flute_direction=selection['piece_flute_direction'])
    reserve=payload['quantity']-allocated
    if reserve<0: fail('采购张数小于分配所需原片张数')
    document=dict(request=payload,requirements=rows,source_proof=proof,supplier_name=supplier_name,customer_id=next(iter(customers)),material_id=material.id,
        material_code=material.code,layer_count=material.layer_count,flute_type=next(iter(flutes)),allocated_quantity=allocated,reserve_quantity=reserve)
    return dict(document,reviewed_hash=fingerprint(document))


def choices(db,item_ids,actor):
    rows,_=requirements(db,item_ids,actor)
    return dict(requirements=rows)


def create(db,payload,reviewed_hash,operation_key,actor):
    from app.models.stock_replenishment import StockReplenishmentOrder,StockReplenishmentOrderItem
    from app.models.supplier_requisition_order import SupplierRequisitionOrder
    from app.services.unified_procurement import attach_stock_sources,stock_snapshot,fingerprint as stock_fingerprint
    from app.services.semi_finished_inventory import save_order_item_semi_requirement
    from app.services.production_workflow import lock_order_rows_for_production_transition
    from app.api.requisition import _supplier_order_number
    from app.services.audit_log import append_audit_event
    request_hash=fingerprint(dict(payload=payload,reviewed_hash=reviewed_hash,actor=actor.id))
    with atomic_bom(db):
        previous=db.scalar(select(RawPurchasePlan).where(RawPurchasePlan.operation_key==operation_key))
        if previous:
            if previous.status!='active' or previous.request_hash!=request_hash: fail('方案提交标识已用于不同内容或已撤销')
            return describe(db,previous)
        items=list(db.scalars(select(OrderItem).where(OrderItem.id.in_(payload['order_item_ids']))))
        lock_order_rows_for_production_transition(db,sorted({i.order_id for i in items}))
        reviewed=preview(db,payload,actor)
        if reviewed['reviewed_hash']!=reviewed_hash: fail('订单、库存或采购方案已变化，请重新预览')
        now=utc_now_naive();pid=reviewed['requirements'][0]['product_id'];product=db.get(Product,pid)
        key=fingerprint(dict(key=operation_key))[:20]
        stock=StockReplenishmentOrder(order_number='RAW-'+key,request_hash=request_hash,customer_id=reviewed['customer_id'],
            supplier_name=reviewed['supplier_name'],source_type='customer_request',status='draft',created_by=actor.id,remark='统一原片实际采购；需求分配另行冻结')
        db.add(stock);db.flush()
        material=db.get(Material,reviewed['material_id'])
        from app.services.warehouse_inventory import normalize_material_code
        raw=StockReplenishmentOrderItem(replenishment_order_id=stock.id,target_inventory_type='semi_finished',procurement_route_snapshot='paperboard',
            reference_product_id=pid,customer_id=stock.customer_id,material_id=material.id,material_code_snapshot=material.code,
            normalized_material_code=normalize_material_code(material.code),product_code_snapshot=product.product_code,product_name_snapshot='统一原片（厂内加工）',
            layer_count=material.layer_count,flute_type=reviewed['flute_type'],report_length_mm=payload['length_mm'],report_width_mm=payload['width_mm'],
            crease_type='净料',sheet_type=payload['sheet_type'],component_type='whole',pieces_per_box=1,stock_yield_per_sheet=1,quantity=payload['quantity'],stocked_quantity=0,
            remark='供应商不压线；厂内按原订单压线、开槽等工艺加工')
        db.add(raw);db.flush()
        purchase=SupplierRequisitionOrder(order_number=_supplier_order_number(db),supplier_name=stock.supplier_name,status='confirmed',created_by=actor.id,
            request_key='raw:'+key,request_hash=request_hash,request_actor_id=actor.id,total_quantity=0,requisition_qty=0,stock_deduction_qty=0,required_piece_qty=0)
        db.add(purchase);db.flush()
        attach_stock_sources(db,purchase=purchase,selections=[SimpleNamespace(stock_replenishment_item_id=raw.id,source_fingerprint=stock_fingerprint(stock_snapshot(raw,stock)))],user=actor)
        plan=RawPurchasePlan(stock_item_id=raw.id,supplier_order_id=purchase.id,customer_id=stock.customer_id,operation_key=operation_key,
            request_hash=request_hash,snapshot_json=encoded(reviewed),created_by=actor.id)
        db.add(plan);db.flush()
        for row in reviewed['requirements']:
            db.get(OrderItem,row['order_item_id']).requisition_status='已报料'
            requirement=save_order_item_semi_requirement(db,order_item_id=row['order_item_id'],component_type=row['component_type'],
                board_length_mm=row['report_length_mm'],board_width_mm=row['report_width_mm'],material_code=material.code,flute_type=row['flute_type'],
                pieces_per_box=row['pieces_per_box'],stock_yield_per_sheet=int(row.get('cutting_factor') or row.get('yield_per_sheet') or 1),
                required_piece_quantity=int(row.get('required_piece_quantity') or row.get('required_piece_qty') or row['piece_quantity']),operator_id=actor.id,
                sales_order_item_bom_component_id=row.get('snapshot_id'))
            db.add(RawPurchaseDemand(plan_id=plan.id,order_item_id=row['order_item_id'],product_id=row['product_id'],requirement_id=requirement.id,
                source_key=row['source_key'],snapshot_json=encoded(row),raw_quantity=row['raw_quantity'],piece_quantity=row['piece_quantity'],yield_factor=row['yield_factor']))
        append_audit_event(db,event_category='business',result='success',source='web',module_code='requisition',action_code='raw_purchase.create',resource='purchase',
            actor=actor,entity_type='raw_purchase_plan',entity_id=plan.id,customer_id=plan.customer_id,details=reviewed)
        db.flush()
        return describe(db,plan)


def describe(db,plan):
    from app.models.supplier_requisition_order import SupplierRequisitionOrder
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.warehouse_inventory import InventoryLot,WarehouseLocation
    purchase=db.get(SupplierRequisitionOrder,plan.supplier_order_id)
    public={k:v for k,v in json.loads(plan.snapshot_json).items() if k!='source_proof'}
    receipts=[]
    for receipt in db.scalars(select(IncomingReceiptItem).where(IncomingReceiptItem.stock_replenishment_item_id==plan.stock_item_id).order_by(IncomingReceiptItem.id)):
        lot=db.get(InventoryLot,receipt.received_inventory_lot_id) if receipt.received_inventory_lot_id else None
        if not lot:continue
        cost=json.loads(lot.cost_snapshot_detail_json or '{}')
        location=db.get(WarehouseLocation,lot.warehouse_location_id)
        receipts.append(dict(receipt_item_id=receipt.id,status=receipt.status,quantity=receipt.received_quantity,lot_id=lot.id,
            available=lot.quantity_available,reserved=lot.quantity_reserved,consumed=lot.quantity_consumed,
            total_cost=cost.get('total_cost'),currency=cost.get('currency'),location_name=location.location_name if location else '未登记'))
    return dict(id=plan.id,status=plan.status,supplier_order_id=purchase.id,supplier_order_number=purchase.order_number,
        stock_item_id=plan.stock_item_id,receipts=receipts,**public)


def on_receipt(db,receipt,lot,price,actor):
    """Allocate only this newly received physical lot, without recording any processing."""
    from decimal import Decimal,ROUND_HALF_UP
    from sqlalchemy import update,func
    from app.models.warehouse_inventory import InventoryReservation,InventoryLot,OrderItemSemiRequirement,SemiFinishedLotAllowedProduct
    from app.models.warehouse_goods import WarehouseGoodsProfile
    from app.services.warehouse_inventory import _balances,_movement
    from app.services.bom_subkit_costs import cost_slice
    from app.services.production_workflow import lock_order_rows_for_production_transition
    from app.services.paper_color import material_face
    plan=db.scalar(select(RawPurchasePlan).where(RawPurchasePlan.stock_item_id==receipt.stock_replenishment_item_id))
    if not plan: return
    if plan.status!='active': fail('原片采购方案已撤销')
    frozen=json.loads(plan.snapshot_json);request=frozen['request']
    demands=list(db.scalars(select(RawPurchaseDemand).where(RawPurchaseDemand.plan_id==plan.id).order_by(RawPurchaseDemand.id)))
    items={d.order_item_id:db.get(OrderItem,d.order_item_id) for d in demands}
    lock_order_rows_for_production_transition(db,sorted({item.order_id for item in items.values()}))
    if any(d.status!='active' for d in demands): fail('原片采购分配已撤销')
    detail=lot.semi_finished_detail;detail.customer_generic_eligible=False
    for pid in sorted({d.product_id for d in demands}):
        db.add(SemiFinishedLotAllowedProduct(inventory_lot_id=lot.id,product_id=pid,confirmed_at=utc_now_naive()))
    profile=dict(processing='raw',scope='customers',customer_ids=[plan.customer_id],product_ids=sorted({d.product_id for d in demands}),
        verified_material_id=detail.material_id,material_code=detail.material_code_snapshot,material_confidence='confirmed',
        face_paper=material_face(db,db.get(Material,detail.material_id)),mold_tool_id=None,mold_version=None,usage_confirmed=True,
        allow_material_substitution=False,blank_unprinted=True,flute_direction=request['flute_direction'],raw_purchase_plan_id=plan.id,
        completed_processes=[],remaining_processes=['按原订单裁切','压线/开槽等原要求','实际成型'],note='统一原片；保留原客户/产品范围；尚未厂内加工')
    db.add(WarehouseGoodsProfile(lot_id=lot.id,data_json=encoded(profile)))
    sheet_cost=price.unit_price
    if price.price_unit=='per_square_meter':sheet_cost*=price.report_length_mm*price.report_width_mm/Decimal(1000000)
    total=(sheet_cost*receipt.received_quantity).quantize(Decimal('.0001'),rounding=ROUND_HALF_UP)
    lot.estimated_unit_cost_snapshot=sheet_cost.quantize(Decimal('.0001'),rounding=ROUND_HALF_UP)
    lot.cost_snapshot_source='raw_purchase_receipt'
    lot.cost_snapshot_at=utc_now_naive()
    lot.cost_snapshot_detail_json=encoded(dict(raw_purchase_plan_id=plan.id,supplier_receipt_price_fact_id=price.id,
        total_cost=str(total),quantity=receipt.received_quantity,currency=price.currency,price_unit=price.price_unit,unit_price=str(price.unit_price)))
    available=receipt.received_quantity;offset=0
    for demand in demands:
        already=db.scalar(select(func.coalesce(func.sum(RawPurchaseReceiptAllocation.raw_quantity),0)).where(RawPurchaseReceiptAllocation.demand_id==demand.id))
        take=min(available,demand.raw_quantity-already)
        if take<=0:continue
        requirement=db.get(OrderItemSemiRequirement,demand.requirement_id);row=json.loads(demand.snapshot_json);item=items[demand.order_item_id]
        cut=dict(method='reviewed_raw_purchase',raw_purchase_plan_id=plan.id,lot_id=lot.id,product_id=demand.product_id,
            source_length_mm=detail.board_length_mm,source_width_mm=detail.board_width_mm,
            target_length_mm=requirement.board_length_mm,target_width_mm=requirement.board_width_mm,yield_factor=demand.yield_factor,
            rotated=row['rotate'],piece_flute_direction=row['piece_flute_direction'],requires_production=True)
        key=f'raw:{receipt.id}:{demand.id}'
        reservation=InventoryReservation(reservation_number='RAW-'+fingerprint(key)[:20],inventory_lot_id=lot.id,
            order_id=item.order_id,order_item_id=item.id,semi_requirement_id=requirement.id,sales_order_item_bom_component_id=requirement.sales_order_item_bom_component_id,
            reservation_type='semi_order',reserved_stock_quantity=take,credited_requirement_quantity=take*demand.yield_factor,yield_factor=demand.yield_factor,
            status='active',idempotency_key=key,reservation_group_key=key,reservation_group_requested_quantity=take*demand.yield_factor,
            reserved_by=actor.id,reserved_at=utc_now_naive(),cut_plan_json=encoded(cut),warning_codes='raw_purchase_processing_required')
        db.add(reservation);db.flush();before=_balances(lot)
        changed=db.execute(update(InventoryLot).where(InventoryLot.id==lot.id,InventoryLot.version==lot.version,InventoryLot.status=='active',InventoryLot.quantity_available>=take)
            .values(quantity_available=InventoryLot.quantity_available-take,quantity_reserved=InventoryLot.quantity_reserved+take,version=InventoryLot.version+1))
        if changed.rowcount!=1:fail('原片批次余额或版本已变化')
        db.refresh(lot)
        _movement(db,lot=lot,movement_type='reserve',quantity=take,before=before,operator_id=actor.id,reservation_id=reservation.id,
            related_order_id=item.order_id,related_order_item_id=item.id,reason='实收统一原片按已确认需求分配，尚未加工',idempotency_key='raw-reserve:'+key)
        db.add(RawPurchaseReceiptAllocation(demand_id=demand.id,receipt_item_id=receipt.id,reservation_id=reservation.id,
            raw_quantity=take,piece_quantity=take*demand.yield_factor,raw_offset=offset,total_cost=cost_slice(total,receipt.received_quantity,offset,take)))
        available-=take;offset+=take
    db.flush()
    from app.services.production_workflow import ensure_receipt_auto_main_task,PENDING,WAITING_MATERIAL,COMPLETED
    for item_id,item in items.items():
        relevant=[d for d in demands if d.order_item_id==item_id]
        required=sum(d.raw_quantity for d in relevant)
        allocated=db.scalar(select(func.coalesce(func.sum(RawPurchaseReceiptAllocation.raw_quantity),0))
            .where(RawPurchaseReceiptAllocation.demand_id.in_([d.id for d in relevant])))
        item.material_status='received' if allocated==required else 'pending'
        task=ensure_receipt_auto_main_task(db,order_item_id=item_id)
        if task.status!=COMPLETED:
            task.status=PENDING if allocated==required else WAITING_MATERIAL
            task.planned_quantity=item.quantity if allocated==required else 0
        task.readiness_basis='raw_purchase_processing'
    db.flush()


def validate_reservation(db,reservation,lot,requirement,*,delivery=False):
    """A typed receipt allocation authorizes only its frozen final requirement."""
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.services.warehouse_goods import goods_profile,qualification_issues
    allocation=db.scalar(select(RawPurchaseReceiptAllocation).where(RawPurchaseReceiptAllocation.reservation_id==reservation.id))
    if not allocation:return False
    demand=db.get(RawPurchaseDemand,allocation.demand_id);plan=db.get(RawPurchasePlan,demand.plan_id)
    receipt=db.get(IncomingReceiptItem,allocation.receipt_item_id);row=json.loads(demand.snapshot_json)
    frozen=json.loads(plan.snapshot_json);request=frozen['request'];detail=lot.semi_finished_detail
    profile=goods_profile(db,lot) or {}
    if (delivery or plan.status!='active' or demand.status!='active' or receipt.status!='posted' or lot.status!='active'
            or receipt.received_inventory_lot_id!=lot.id or receipt.stock_replenishment_item_id!=plan.stock_item_id
            or reservation.semi_requirement_id!=demand.requirement_id or reservation.order_item_id!=demand.order_item_id
            or reservation.yield_factor!=demand.yield_factor or reservation.reserved_stock_quantity!=allocation.raw_quantity
            or reservation.credited_requirement_quantity!=allocation.piece_quantity
            or requirement.component_type!=row['component_type'] or requirement.customer_id!=plan.customer_id
            or (requirement.board_length_mm,requirement.board_width_mm)!=(row['report_length_mm'],row['report_width_mm'])
            or not detail or detail.owner_customer_id!=plan.customer_id or detail.material_id!=frozen['material_id']
            or (detail.board_length_mm,detail.board_width_mm,detail.flute_type)!=(request['length_mm'],request['width_mm'],frozen['flute_type'])
            or profile.get('raw_purchase_plan_id')!=plan.id or profile.get('processing')!='raw'
            or profile.get('flute_direction')!=request['flute_direction']):
        fail('原片预占与冻结采购、实收或最终需求不一致，不能消耗或直接发货')
    issues=qualification_issues(db,lot,db.get(Product,demand.product_id),profile,expected_material_code=requirement.normalized_material_code)
    if issues:fail('原片加工资格已变化：'+'；'.join(issues))
    return True


def reservation_cost(db,reservation,default):
    allocation=db.scalar(select(RawPurchaseReceiptAllocation).where(RawPurchaseReceiptAllocation.reservation_id==reservation.id))
    return allocation.total_cost if allocation else default


def raw_cost_source(db,reservation,entry,amount,product_id):
    from decimal import Decimal
    from app.models.supplier_settlement import SupplierReceiptSettlementPriceFact
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.services.bom_subkit_costs import cost_slice
    allocation=db.scalar(select(RawPurchaseReceiptAllocation).where(RawPurchaseReceiptAllocation.reservation_id==reservation.id))
    if not allocation:return None
    demand=db.get(RawPurchaseDemand,allocation.demand_id)
    receipt=db.get(IncomingReceiptItem,allocation.receipt_item_id)
    price=db.scalar(select(SupplierReceiptSettlementPriceFact).where(SupplierReceiptSettlementPriceFact.incoming_receipt_item_id==receipt.id))
    if (not price or price.fact_origin!='receipt_frozen' or receipt.status!='posted' or demand.product_id!=product_id
            or demand.order_item_id!=reservation.order_item_id or receipt.received_inventory_lot_id!=reservation.inventory_lot_id
            or cost_slice(allocation.total_cost,allocation.piece_quantity,entry['before'],entry['quantity'])!=amount):
        fail('原片加工实际成本与冻结收料、分配或产品不一致')
    unit=price.unit_price if price.price_unit=='per_sheet' else price.unit_price*price.report_length_mm*price.report_width_mm/Decimal(1000000)
    from decimal import ROUND_HALF_UP
    total=(unit*receipt.received_quantity).quantize(Decimal('.0001'),rounding=ROUND_HALF_UP)
    if cost_slice(total,receipt.received_quantity,allocation.raw_offset,allocation.raw_quantity)!=allocation.total_cost:
        fail('原片采购成本分配不守恒')
    return dict(raw_receipt_allocation_id=allocation.id,amount=amount,currency=price.currency,tax_included=price.tax_included,tax_rate=price.tax_rate)


def void_unreceived_plan(db,stock_item_id,actor):
    from app.models.stock_replenishment import StockReplenishmentOrder,StockReplenishmentOrderItem
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.services.audit_log import append_audit_event
    plan=db.scalar(select(RawPurchasePlan).where(RawPurchasePlan.stock_item_id==stock_item_id))
    if not plan:return
    if plan.status!='active':fail('原片方案已撤销')
    if db.scalar(select(IncomingReceiptItem.id).where(IncomingReceiptItem.stock_replenishment_item_id==stock_item_id).limit(1)):
        fail('原片已有实收历史，不能删除或重新采购原计划')
    plan.status='voided'
    for row in db.scalars(select(RawPurchaseDemand).where(RawPurchaseDemand.plan_id==plan.id)):row.status='voided'
    item_ids=set(db.scalars(select(RawPurchaseDemand.order_item_id).where(RawPurchaseDemand.plan_id==plan.id)))
    for item_id in item_ids:db.get(OrderItem,item_id).requisition_status='未报料'
    stock=db.get(StockReplenishmentOrderItem,stock_item_id)
    order=db.get(StockReplenishmentOrder,stock.replenishment_order_id);order.status='voided';order.voided_at=utc_now_naive()
    append_audit_event(db,event_category='business',result='success',source='web',module_code='requisition',action_code='raw_purchase.void',resource='purchase',
        actor=actor,entity_type='raw_purchase_plan',entity_id=plan.id,customer_id=plan.customer_id,details={'supplier_order_id':plan.supplier_order_id})


def production_summaries(db,item_ids):
    from collections import defaultdict
    from decimal import Decimal
    from app.models.warehouse_inventory import InventoryReservation,OrderItemSemiRequirement
    from app.models.multilevel_bom import OrderBomGraph
    from app.services.receipt_managed_production import _empty_summary
    from app.services.component_processing import processing_completions
    grouped=defaultdict(list)
    for demand in db.scalars(select(RawPurchaseDemand).where(RawPurchaseDemand.order_item_id.in_(item_ids),RawPurchaseDemand.status=='active')):
        grouped[demand.order_item_id].append(demand)
    result={}
    for item_id,demands in grouped.items():
        item=db.get(OrderItem,item_id);summary=_empty_summary();states=[];credits={};capacities=[]
        completions=[c for c,d in processing_completions(db,item_id) if d['processing_product_id']==item.product_id]
        manual=sum(c.actual_output_quantity for c in completions)
        summary.update(manual_processing_output_qty=manual,manual_processing_reserved_qty=sum(c.order_reserved_quantity for c in completions),
            manual_processing_surplus_qty=sum(c.surplus_finished_quantity for c in completions),requires_component_processing=True,raw_purchase_processing=True)
        for demand in demands:
            requirement=db.get(OrderItemSemiRequirement,demand.requirement_id);row=json.loads(demand.snapshot_json)
            allocations=list(db.scalars(select(RawPurchaseReceiptAllocation).where(RawPurchaseReceiptAllocation.demand_id==demand.id)))
            received_raw=sum(a.raw_quantity for a in allocations);received_pieces=sum(a.piece_quantity for a in allocations)
            reservations=list(db.scalars(select(InventoryReservation).where(InventoryReservation.semi_requirement_id==requirement.id,InventoryReservation.status!='cancelled')))
            credit=sum(r.credited_requirement_quantity-r.released_requirement_quantity for r in reservations)
            key=f'bom:{requirement.sales_order_item_bom_component_id}:{requirement.component_type}' if requirement.sales_order_item_bom_component_id else requirement.component_type
            credits[item_id,key]=credit
            capacity=credit//requirement.pieces_per_box;capacities.append(capacity)
            states.append(dict(component_key=key,component_label=f"{row['product_name']} {requirement.component_type}",component_type=requirement.component_type,
                planned_order_sheet_qty=demand.raw_quantity,received_order_sheet_qty=received_raw,reserve_received_sheet_qty=0,
                pieces_per_finished=1,received_capacity=Decimal(0),planned_capacity=Decimal(max(demand.piece_quantity-received_pieces,0)),
                current_finished_capacity_qty=capacity,waiting_for_pairing=received_pieces<demand.piece_quantity))
        summary['order_purpose_received_sheet_qty']=sum(s['received_order_sheet_qty'] for s in states)
        summary['remaining_order_purpose_sheet_qty']=sum(s['planned_order_sheet_qty']-s['received_order_sheet_qty'] for s in states)
        if db.get(OrderBomGraph,item_id):
            from app.services.multilevel_bom_receipt_projection import project_graph_receipts
            project_graph_receipts(db,item_id,summary,states,credits)
            from app.services.unfinished_components import unfinished_reservations
            summary['requires_component_processing']=bool(summary['remaining_order_purpose_sheet_qty'] or unfinished_reservations(db,item_id))
        else:
            capacity=min(capacities,default=0)
            pending=max(capacity-manual,0)
            summary.update(component_progress=states,current_theoretical_finished_capacity_qty=capacity,pending_processing_quantity=pending,
                currently_unposted_finished_capacity_qty=0,requires_component_processing=bool(pending or summary['remaining_order_purpose_sheet_qty']),
                projection_inconsistent=manual>capacity)
        result[item_id]=summary
    return result
