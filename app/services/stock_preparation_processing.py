"""Receipt-owned processing and explicit batches over the existing ledger.

GET projections never call these mutations. Splitting a task records the old
reservation release and fresh batch reservations in the caller's transaction.
"""
import json
from decimal import Decimal
from math import ceil
from sqlalchemy import func, select, update
from app.core.time_contract import beijing_today, utc_now_naive
from app.models.incoming_receipt import IncomingReceiptItem
from app.models.product import Product
from app.models.raw_purchase_plan import RawPurchasePlan
from app.models.stock_preparation import StockPreparationJob as Job, StockPreparationCommand as Command
from app.models.warehouse_inventory import InventoryLot, InventoryReservation
from app.services import stock_preparation as prep
from app.services.stock_preparation_groups import encode, digest


def processing_block(db, item, lot):
    if getattr(item, 'frozen_order_reserve', False):
        return '通用多送备料沿原用途保留，需要时另行安排'
    if not lot or lot.inventory_type != 'semi_finished':
        return '这批来料不属于待加工原纸'
    from app.services.stock_preparation_read import has_raw_plan
    if has_raw_plan(db, item.id):
        return '统一原片已按订单分配，请使用订单加工入口'
    if not (item.reference_product_id or item.product_id):
        return '通用备料未指定加工产品'
    from app.services.warehouse_goods import goods_profile
    profile = goods_profile(db, lot) or {}
    if profile.get('output_piece') or profile.get('dimension_basis') == 'source_board':
        return '这是已加工子件，不能再次作为原纸加工'
    try:
        product = prep.plan_product(db, item, lot)
        from app.services.stock_replenishment_plan import frozen_bom_plan
        contract = frozen_bom_plan(item)
        if contract and not item.production_snapshot_json:
            child = next((c for c in contract['components'] if c['product_id'] == product.id), None)
            parent = db.get(Product, contract['parent_product_id'])
            if (not child or child['product_version'] != product.version or not parent
                    or parent.version != contract['parent_product_version']):
                return '采购后产品或BOM版本已变化，请核对冻结加工身份'
        if item.sheet_cutting_snapshot:
            from app.services.sheet_cutting_contract import SheetCuttingContract
            cutting = SheetCuttingContract.from_snapshot(item.sheet_cutting_snapshot)
            if cutting.yield_per_supplier_sheet != item.stock_yield_per_sheet:
                return '冻结开料与每张产出不一致，请核对'
    except prep.WarehouseInventoryError as error:
        return str(error)
    except (ValueError, KeyError, TypeError) as error:
        return '冻结加工合同不完整，请核对'
    return None


def freeze_snapshot(db, item, lot):
    block = processing_block(db, item, lot)
    if block:
        prep.fail(block)
    product = db.get(Product, item.reference_product_id or item.product_id)
    from app.services.finished_stock_identity import product_basis
    from app.services.stock_replenishment_plan import frozen_bom_plan
    contract = frozen_bom_plan(item)
    from app.services.stock_purchase_identity import resolve
    identity = resolve(db, item, product)
    from app.services.stock_completion_basis import completion_basis
    output_basis, basis_proof = completion_basis(db, item, dict(
        physical_basis=identity['physical_basis'], product_id=product.id, purchase_identity=identity))
    return dict(product_id=product.id, code=item.product_code_snapshot or product.product_code,
        name=item.product_name_snapshot or product.product_name, factor=item.stock_yield_per_sheet,
        pieces_per_box=item.pieces_per_box, physical_basis=output_basis, completion_basis_proof=basis_proof,
        purchase_identity=identity, product_version=identity['product_version'], planned_location=None, preparation_group=None,
        frozen_bom_plan=contract, sheet_cutting_snapshot=item.sheet_cutting_snapshot,
        bom_parent_basis=(identity.get('bom_parent_basis') or product_basis(db.get(Product,contract['parent_product_id']))) if contract else None,
        auto_planned=True)


def auto_plan_receipt(db, receipt, lot, actor):
    """A newly posted explicit-product receipt owns one deterministic task."""
    _, item, _ = prep.source(db, receipt.id)
    if processing_block(db, item, lot):
        return None
    key = 'auto-stock-receipt:' + str(receipt.id)
    snapshot = freeze_snapshot(db, item, lot)
    before = max(int(receipt.cumulative_received_quantity)-int(receipt.received_quantity),0)
    planned_input = min(lot.quantity_available,max(int(receipt.planned_quantity)-before,0))
    # A supplier over-receipt proves physical raw stock, not a new processing
    # purpose. Cancelled earlier tasks do not refill this receipt-plan allowance.
    if planned_input*snapshot['factor']//snapshot['pieces_per_box']<=0:
        return None
    return prep.mutate(db, receipt_id=receipt.id, actor=actor, frozen_snapshot=snapshot,
        payload=dict(action='plan', operation_key=key, lot_version=lot.version, quantity=planned_input))


def auto_complete_receipt(db, receipt, lot, actor):
    """Consume cumulative replenishment inputs and post only the new output delta."""
    _, item, _ = prep.source(db, receipt.id)
    block = processing_block(db, item, lot)
    if block:
        if (item.reference_product_id or item.product_id) and not getattr(item, 'frozen_order_reserve', False):
            prep.fail(f'明确产品用途的收料未能自动成品：{block}')
        return None
    key = 'auto-stock-complete:' + str(receipt.id)
    previous = db.get(Command, key)
    if previous:
        return json.loads(previous.result_json)
    snapshot = freeze_snapshot(db, item, lot)
    factor = int(snapshot['factor']); pieces = int(snapshot['pieces_per_box'])
    planned = int(receipt.planned_quantity)
    cumulative = min(int(receipt.cumulative_received_quantity), planned)
    target_output = cumulative * factor // pieces
    receipt_ids = list(db.scalars(select(IncomingReceiptItem.id).where(
        IncomingReceiptItem.stock_replenishment_item_id == item.id,
        IncomingReceiptItem.status == 'posted').order_by(IncomingReceiptItem.id)))
    completed = list(db.scalars(select(Job).where(
        Job.receipt_item_id.in_(receipt_ids), Job.status == 'completed')))
    produced = sum(int(row.actual_output or 0) for row in completed)
    output_delta = target_output - produced
    if output_delta < 0:
        prep.fail('累计已产数量超过冻结实收应产数量，请核对原任务，不能重复收料')
    if output_delta == 0:
        return None
    target_consumed = min(ceil(target_output * pieces / factor), planned)
    consumed = sum(int(row.input_quantity or 0) for row in completed)
    input_delta = target_consumed - consumed
    if input_delta <= 0:
        prep.fail('累计实收产出增加但冻结投入没有增加，请核对数量换算')
    candidates = list(db.scalars(select(InventoryLot).join(IncomingReceiptItem,
        IncomingReceiptItem.received_inventory_lot_id == InventoryLot.id).where(
        IncomingReceiptItem.stock_replenishment_item_id == item.id,
        IncomingReceiptItem.status == 'posted', InventoryLot.status == 'active',
        InventoryLot.inventory_type == 'semi_finished', InventoryLot.quantity_available > 0)
        .order_by(IncomingReceiptItem.id, InventoryLot.id)))
    remaining=input_delta;sources=[]
    for source in candidates:
        if remaining<=0:break
        take=min(remaining,int(source.quantity_available or 0))
        source_receipt=db.scalar(select(IncomingReceiptItem).where(
            IncomingReceiptItem.received_inventory_lot_id==source.id,
            IncomingReceiptItem.stock_replenishment_item_id==item.id,
            IncomingReceiptItem.status=='posted').order_by(IncomingReceiptItem.id).limit(1))
        if take<=0 or source_receipt is None:continue
        if processing_block(db,item,source):
            prep.fail(f'来源批次 {source.lot_number} 已不满足冻结加工条件')
        sources.append((source_receipt,source,take));remaining-=take
    if remaining:
        prep.fail('累计实收原料可用量不足，不能自动形成应产成品')
    total=Decimal(0);input_rows=[];jobs=[]
    for source_receipt,source,take in sources:
        from app.services.inventory_valuation import require_inherited_entry_cost
        require_inherited_entry_cost(db,source)
        reservation=InventoryReservation(reservation_number='SPA-'+digest([key,source.id])[:20],
            inventory_lot_id=source.id,reservation_type='semi_order',reserved_stock_quantity=take,
            consumed_stock_quantity=take,status='consumed',reserved_by=actor.id,reserved_at=utc_now_naive(),
            consumed_by=actor.id,consumed_at=utc_now_naive(),idempotency_key='prep:'+digest([key,source.id])[:60],
            warning_codes='stock_preparation_auto',reservation_group_key=key)
        db.add(reservation);db.flush()
        expected=max(take*factor//pieces,1)
        job=Job(receipt_item_id=source_receipt.id,reservation_id=reservation.id,
            product_id=snapshot['product_id'],product_snapshot=encode(dict(snapshot,automatic_completion=True,
                cumulative_received_quantity=cumulative,cumulative_target_output=target_output)),
            input_quantity=take,expected_output=expected,actual_output=0,status='completed')
        db.add(job);db.flush();before=prep._balances(source)
        changed=db.execute(update(InventoryLot).where(InventoryLot.id==source.id,
            InventoryLot.version==source.version,InventoryLot.status=='active',
            InventoryLot.quantity_available>=take).values(
                quantity_available=InventoryLot.quantity_available-take,
                quantity_consumed=InventoryLot.quantity_consumed+take,
                version=InventoryLot.version+1,last_movement_at=utc_now_naive()))
        if changed.rowcount!=1:prep.fail('自动加工来源库存已变化，请刷新重试')
        db.refresh(source);movement_key='prep-auto:'+digest([key,source.id])[:50]
        prep._movement(db,lot=source,movement_type='consume',quantity=take,before=before,
            operator_id=actor.id,reservation_id=reservation.id,reason='补库实收自动形成产品',
            idempotency_key=movement_key)
        unit=Decimal(str(source.estimated_unit_cost_snapshot));total+=unit*take
        input_rows.append(dict(lot_id=source.id,receipt_item_id=source_receipt.id,
            job_id=job.id,reservation_id=reservation.id,product_id=snapshot['product_id'],
            quantity=take,unit_cost=str(unit),movement_key=movement_key))
        jobs.append(job)
    anchor=jobs[-1];anchor.actual_output=output_delta
    contract=snapshot.get('frozen_bom_plan')
    output_kind='semi' if contract and contract.get('parent_product_id')!=snapshot['product_id'] else 'finished'
    from app.services.production_workflow import _receipt_auto_finished_ground_target
    if output_kind=='semi':
        # A BOM child is interim stock, not a finished parent. Keep it in the
        # physically confirmed semi-capable source cell without saving that
        # raw-material cell as the product's future finished destination.
        from types import SimpleNamespace
        from app.models.warehouse_inventory import WarehouseLocation
        from app.services.receipt_putaway import location_issue
        from app.services.location_candidates import claim_active_placed_location
        source_location=db.get(WarehouseLocation,sources[-1][1].warehouse_location_id)
        issue=location_issue(db,source_location,'semi_finished')
        if issue or not source_location.floor3_layout or not claim_active_placed_location(
                db,source_location.id,expected_layout_version=source_location.floor3_layout.version):
            prep.fail(f'冻结BOM子件的暂存货位不可用：{issue or "位置版本已变化"}')
        target=SimpleNamespace(location=source_location,layout_version=source_location.floor3_layout.version)
    else:
        target=_receipt_auto_finished_ground_target(db,claim=True,customer_id=item.customer_id,
            product_id=snapshot['product_id'],inventory_type='finished')
    payload=dict(location_id=target.location.id,layout_version=target.layout_version)
    if output_kind=='semi':
        from app.services.stock_preparation_disposition import semi_output
        output=semi_output(db,anchor,sources[-1][1],item,output_delta,target.location,payload,actor)
    else:
        from app.services.warehouse_inventory import manual_finished_in
        output=manual_finished_in(db,customer_id=item.customer_id,product_id=snapshot['product_id'],
            location_id=target.location.id,quantity=output_delta,stock_date=beijing_today(),source_type='transfer',
            remarks='补库实收自动成品',operator_id=actor.id,idempotency_key='prep-auto-out:'+str(receipt.id),
            source_ref_type='stock_preparation',source_ref_id=anchor.id,
            expected_layout_version=target.layout_version,movement_reason='补库实收自动形成成品',
            physical_basis_json=snapshot['physical_basis'])
    anchor.output_lot_id=output.id
    output.estimated_unit_cost_snapshot=total/output_delta
    output.cost_snapshot_source='stock_preparation'
    output.cost_snapshot_detail_json=encode(dict(automatic_batch=True,source_lot_id=input_rows[-1]['lot_id'],
        input_quantity=input_delta,output_quantity=output_delta,total_cost=str(total),inputs=input_rows,
        cumulative_received_quantity=cumulative,cumulative_target_output=target_output))
    result=dict(action='automatic_complete',receipt_item_id=receipt.id,input_quantity=input_delta,
        output_quantity=output_delta,output_lot_id=output.id,output_kind=output_kind,
        location_id=target.location.id,job_ids=[row.id for row in jobs])
    db.flush()
    # Automatic receipt completion must honor the same explicitly confirmed
    # cross-customer physical-stock policy as the former manual completion.
    # Enrol only after the output identity and frozen cost evidence are final.
    from app.services.shared_bom_stock import enroll_completed_bom
    enroll_completed_bom(db,output,operator_id=actor.id)
    assembly=_auto_assemble_frozen_parent(db,receipt,item,snapshot,actor)
    if assembly is not None:
        result['assembly']=assembly
    if receipt.stock_replenishment_item_id is not None:
        from app.services.order_replenishment_supply import reconcile_outputs
        reconcile_outputs(db,operator_id=actor.id,source_item_ids=[receipt.stock_replenishment_item_id])
    db.add(Command(operation_key=key,receipt_item_id=receipt.id,request_json=encode(dict(
        action='automatic_complete',receipt_item_id=receipt.id,
        cumulative_received_quantity=cumulative,target_output=target_output)),
        result_json=encode(result),actor_id=actor.id))
    from app.services.audit_log import append_audit_event
    append_audit_event(db,event_category='business',result='success',source='system',module_code='incoming',
        action_code='stock_preparation.automatic_complete',resource='incoming_receipt',actor=actor,
        entity_type='incoming_receipt_item',entity_id=receipt.id,details=dict(result,inputs=input_rows))
    return result


def _auto_assemble_frozen_parent(db, receipt, item, snapshot, actor):
    contract=snapshot.get('frozen_bom_plan')
    if not contract or contract.get('parent_product_id')==snapshot.get('product_id'):
        return None
    parent_id=int(contract['parent_product_id']);target_sets=int(contract.get('finished_quantity') or 0)
    if target_sets<=0:prep.fail('冻结BOM缺少目标套数')
    from app.services.stock_preparation_assembly import _plan_identity, preview, assemble_stock
    from app.models.stock_replenishment import StockReplenishmentOrderItem
    from app.services.stock_replenishment_plan import frozen_bom_plan
    scope=dict(order_id=int(item.replenishment_order_id),parent_id=parent_id,
        plan_hash=_plan_identity(contract),policy_id=int(contract.get('policy_id') or 0),
        target_sets=target_sets)
    completed=0
    for command in db.scalars(select(Command).where(Command.operation_key.like('auto-stock-assembly:%'))):
        try:
            value=json.loads(command.result_json)
        except (TypeError,ValueError):
            continue
        if value.get('action')!='assemble' or value.get('recipe',{}).get('parent_id')!=parent_id:
            continue
        source_items=[]
        for row in value.get('inputs',[]):
            input_lot=db.get(InventoryLot,row.get('lot_id'))
            job=(db.get(Job,input_lot.source_ref_id) if input_lot and
                input_lot.source_ref_type=='stock_preparation' else None)
            source_receipt=db.get(IncomingReceiptItem,job.receipt_item_id) if job else None
            source_items.append(db.get(StockReplenishmentOrderItem,source_receipt.stock_replenishment_item_id)
                if source_receipt else None)
        if source_items and all(source and source.replenishment_order_id==scope['order_id']
                and (source_contract:=frozen_bom_plan(source))
                and _plan_identity(source_contract)==scope['plan_hash']
                and int(source_contract.get('policy_id') or 0)==scope['policy_id']
                and int(source_contract.get('finished_quantity') or 0)==scope['target_sets']
                for source in source_items):
            completed+=int(value.get('sets') or 0)
    remaining=target_sets-completed
    if remaining<=0:return None
    first=preview(db,parent_id,1,scope=scope)
    if first['shortages'] or first['available_sets']<=0:
        return None
    sets=min(remaining,int(first['available_sets']))
    plan=preview(db,parent_id,sets,scope=scope)
    if plan['shortages'] or not plan['sources']:
        return None
    from app.services.production_workflow import _receipt_auto_finished_ground_target
    target=_receipt_auto_finished_ground_target(db,claim=True,customer_id=item.customer_id,product_id=parent_id)
    return assemble_stock(db,dict(action='assemble_stock',parent_id=parent_id,sets=sets,
        basis_hash=plan['basis_hash'],jobs=plan['sources'],scope=scope,location_id=target.location.id,
        layout_version=target.layout_version,operation_key=f'auto-stock-assembly:{receipt.id}:{parent_id}'),actor)


def process(db, receipt_id, payload, actor, *, result_builder=None):
    """One submit records this batch and leaves unprocessed input reserved."""
    request = encode(dict(payload, receipt_id=receipt_id))
    key = payload['operation_key']
    old = db.get(Command, key)
    if old:
        if old.request_json != request or old.actor_id != actor.id:
            prep.fail('操作标识已用于其他内容')
        return json.loads(old.result_json)
    receipt, item, lot = prep.source(db, receipt_id)
    if not lot or lot.version != payload['lot_version']:
        prep.fail('库存已变化，请刷新')
    if payload['action'] == 'process':
        snapshot = freeze_snapshot(db, item, lot)
        amount = payload.get('actual_input_quantity')
        if amount is None:
            prep.fail('请填写本次实际投入张数')
        if amount <= 0 or amount > lot.quantity_available:
            prep.fail('本次投入必须大于0且不能超过可用材料')
        original = None
        remaining = lot.quantity_available - amount
    else:
        original = db.get(Job, payload.get('job_id'))
        if (not original or original.receipt_item_id != receipt_id or original.status != 'pending'
                or original.version != payload.get('job_version')):
            prep.fail('待加工任务已变化，请刷新')
        snapshot = json.loads(original.product_snapshot)
        group = snapshot.get('preparation_group')
        if group and not group.get('independent'):
            prep.fail('原整组任务请从整组入口处理')
        amount = payload.get('actual_input_quantity')
        amount = original.input_quantity if amount is None else amount
        if amount <= 0 or amount > original.input_quantity:
            prep.fail('本次投入必须大于0且不能超过任务未加工张数')
        remaining = original.input_quantity - amount
        # Full completion retains the original task ID and legacy contract.
        if not remaining:
            child_payload = dict(payload)
            child_payload.pop('actual_input_quantity', None)
            child_payload['operation_key'] = digest([key, 'complete'])[:60]
            result = prep.mutate(db, receipt_id=receipt_id, payload=child_payload, actor=actor,
                                 output_kind=payload['output_kind'])
            result.update(completed_job_id=original.id, continuation_job_id=None, remaining_input_quantity=0)
            if result_builder is not None:
                result = result_builder(db, receipt_id, payload, actor, result)
            db.add(Command(operation_key=key,receipt_item_id=receipt_id,request_json=request,result_json=encode(result),actor_id=actor.id))
            db.flush()
            return result
        prep.mutate(db, receipt_id=receipt_id, actor=actor, payload=dict(action='cancel',
            operation_key=digest([key, 'cancel'])[:60], lot_version=lot.version,
            job_id=original.id, job_version=original.version))
        snapshot = dict(snapshot, batch_parent_job_id=original.id)
    planned = prep.mutate(db, receipt_id=receipt_id, actor=actor, frozen_snapshot=snapshot,
        payload=dict(action='plan',operation_key=digest([key,'batch'])[:60],lot_version=lot.version,quantity=amount))
    batch = db.get(Job, planned['job_id'])
    complete = dict(payload,action='complete',operation_key=digest([key,'output'])[:60],
        lot_version=lot.version,job_id=batch.id,job_version=batch.version)
    complete.pop('actual_input_quantity',None)
    prep.mutate(db,receipt_id=receipt_id,payload=complete,actor=actor,output_kind=payload['output_kind'])
    continuation = None
    if remaining and remaining*snapshot['factor']//snapshot['pieces_per_box']>0:
        continuation = prep.mutate(db,receipt_id=receipt_id,actor=actor,frozen_snapshot=snapshot,
            payload=dict(action='plan',operation_key=digest([key,'remaining'])[:60],lot_version=lot.version,quantity=remaining))['job_id']
    result = dict(action='complete',job_id=batch.id,completed_job_id=batch.id,
        continuation_job_id=continuation,remaining_input_quantity=remaining,
        original_job_id=original.id if original else None)
    if result_builder is not None:
        result = result_builder(db, receipt_id, payload, actor, result)
    db.add(Command(operation_key=key,receipt_item_id=receipt_id,request_json=request,result_json=encode(result),actor_id=actor.id))
    db.flush()
    return result
