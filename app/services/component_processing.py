"""Explicit completion of reserved unfinished parts plus real received materials."""
import hashlib
import json
from decimal import Decimal
from sqlalchemy import select
from app.models.order import OrderItem
from app.models.product import Product
from app.models.production import ProductionCompletion,ProductionCompletionBatch
from app.models.warehouse_inventory import InventoryLot,InventoryReservation,OrderItemSemiRequirement
from app.services.unfinished_components import unfinished_reservations
from app.services.bom_subkits import SubkitError
from app.services.bom_transactions import atomic_bom
from app.services.bom_subkit_costs import cost_slice


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,default=str,separators=(',',':')).encode()).hexdigest()


def processing_completions(db,item_id,posted=True):
    rows=db.execute(select(ProductionCompletion,InventoryLot).join(InventoryLot,InventoryLot.id==ProductionCompletion.inventory_lot_id)
        .where(ProductionCompletion.order_item_id==item_id,ProductionCompletion.origin=='manual')).all()
    return [(row,json.loads(lot.cost_snapshot_detail_json or '{}')) for row,lot in rows
        if (not posted or row.status=='posted') and json.loads(lot.cost_snapshot_detail_json or '{}').get('component_processing_confirmation')]


def completed_units(db,item_id):
    automatic=sum(row.actual_output_quantity for row in db.scalars(select(ProductionCompletion).where(
        ProductionCompletion.order_item_id==item_id,ProductionCompletion.origin=='receipt_auto',ProductionCompletion.status=='posted')))
    return automatic+sum(row.actual_output_quantity for row,_ in processing_completions(db,item_id))


def plan_processing(db,item_id,product_id):
    from app.services.receipt_purpose_distribution import _order_item_snapshots,_active_order_item_allocations
    from app.models.multilevel_bom import OrderBomGraph
    item=db.get(OrderItem,item_id)
    if item is None: raise SubkitError('订单明细不存在')
    pending=unfinished_reservations(db,item_id)
    if not pending: raise SubkitError('当前没有待加工的专用盖/底预占')
    snapshots=_order_item_snapshots(db,item_id)
    allocations=_active_order_item_allocations(db,item_id)
    graph=None
    if db.get(OrderBomGraph,item_id):
        from app.services.multilevel_bom_orders import read_compiled_order_bom
        from app.services.multilevel_bom_receipts import NodeReceiptContext,node_purpose_snapshots,node_receipt_plan
        compiled=read_compiled_order_bom(db,item_id)
        node=next((n for n in compiled.graph.nodes if n.product_id==product_id and n.source=='manufactured'),None)
        snapshot=next((s for s in compiled.snapshots if s.component_product_id==product_id),None)
        if node is None or snapshot is None: raise SubkitError('请选择冻结的自制产品')
        graph=NodeReceiptContext(compiled,node,snapshot)
        pending=[p for p in pending if p[1].sales_order_item_bom_component_id==snapshot.id]
        if not pending: raise SubkitError('该产品没有待加工专用部件')
        snapshots=node_purpose_snapshots(db,graph,snapshots)
        ids={s.id for s in snapshots};allocations=[a for a in allocations if a.purchase_purpose_source_snapshot_id in ids]
        # All actual allocation facts, no synthetic current receipt.
        if snapshots:
            plan=node_receipt_plan(db,graph,snapshots=snapshots,allocations=allocations,current_snapshot=snapshots[0],order_delta=0,order_cost=Decimal(0),currency='')
        else:
            plan=node_receipt_plan(db,graph,snapshots=[],allocations=[],current_snapshot=None,order_delta=0,order_cost=Decimal(0),currency='')
    else:
        from app.services.box_type_rules import get_box_type_rule
        product=db.get(Product,product_id)
        raw_case=any(profile.get('raw_purchase_plan_id') for _,_,profile in pending)
        if product_id!=item.product_id or not product or not (rule:=get_box_type_rule(product.box_style)) or (rule.code!='a3_set' and not raw_case):
            raise SubkitError('专用部件加工请选择本订单天地盖产品')
        if any(s.source_bom_requisition_source_id for s in snapshots):
            raise SubkitError('旧组合订单请先完成既有BOM受控转换，再确认专用部件加工')
        routes=('cover','base') if rule.code=='a3_set' else ('whole',)
        sources={r:[] for r in routes};per={r:1 for r in routes}
        reservations=db.execute(select(InventoryReservation,OrderItemSemiRequirement,InventoryLot)
            .join(OrderItemSemiRequirement,OrderItemSemiRequirement.id==InventoryReservation.semi_requirement_id)
            .join(InventoryLot,InventoryLot.id==InventoryReservation.inventory_lot_id)
            .where(InventoryReservation.order_item_id==item_id,InventoryReservation.reservation_type=='semi_order',InventoryReservation.status!='cancelled')
            .order_by(InventoryReservation.id)).all()
        for reservation,requirement,lot in reservations:
            route=requirement.component_type
            if route not in sources: raise SubkitError('天地盖预占必须保留盖/底身份')
            pieces=reservation.credited_requirement_quantity-reservation.released_requirement_quantity
            sheets=reservation.reserved_stock_quantity-reservation.released_stock_quantity
            if pieces<=0: continue
            if lot.estimated_unit_cost_snapshot is None or lot.estimated_unit_cost_snapshot<0: raise SubkitError('专用部件缺少来源成本，请先核对')
            factor=int(reservation.yield_factor or 0)
            if factor<=0 or pieces>sheets*factor: raise SubkitError('预占片数与出数不一致')
            per[route]=requirement.pieces_per_box
            from app.services.raw_purchase_plans import reservation_cost
            sources[route].append(dict(kind='reservation',id=reservation.id,quantity=pieces,total_cost=reservation_cost(db,reservation,lot.estimated_unit_cost_snapshot*sheets),
                factor=factor,consumed=reservation.consumed_stock_quantity,lot_id=lot.id))
        by_id={s.id:s for s in snapshots}
        for allocation in allocations:
            source=by_id[allocation.purchase_purpose_source_snapshot_id];route=source.component_type
            if route not in sources: raise SubkitError('收料来源不是盖/底物理部件')
            per[route]=source.pieces_per_finished_snapshot
            sources[route].append(dict(kind='allocation',id=allocation.id,quantity=allocation.receipt_order_purpose_sheet_qty*source.yield_per_sheet_snapshot,total_cost=allocation.order_purpose_cost))
        before=completed_units(db,item_id)
        after=min(sum(s['quantity'] for s in sources[r])//per[r] for r in sources)
        if after<before: raise SubkitError('现有材料不足以支持已完工数量')
        inputs=[];total=Decimal('0.0000')
        for route in sources:
            skip=before*per[route];remaining=(after-before)*per[route]
            for source in sources[route]:
                used=min(skip,source['quantity']);skip-=used
                take=min(remaining,source['quantity']-used);remaining-=take
                if not take: continue
                amount=cost_slice(source['total_cost'],source['quantity'],used,take)
                entry=dict(kind=source['kind'],id=source['id'],route=route,before=used,quantity=take,total_cost=str(amount),actual=source['kind']=='allocation')
                if source['kind']=='reservation':
                    factor=source['factor'];stock_before=(used+factor-1)//factor;stock_after=(used+take+factor-1)//factor
                    if stock_before!=source['consumed']: raise SubkitError('原预占已被其他操作消耗，请核对')
                    entry.update(stock_before=stock_before,stock_after=stock_after,lot_id=source['lot_id'])
                inputs.append(entry);total+=amount
        plan=dict(before=before,after=after,total_cost=total,detail={'bom_material_inputs':inputs})
    totals={s.id:sum(a.receipt_order_purpose_sheet_qty for a in allocations if a.purchase_purpose_source_snapshot_id==s.id) for s in snapshots}
    if any(totals[s.id]<s.order_purpose_sheet_qty for s in snapshots): raise SubkitError('待加工材料尚未全部到齐，请先完成实际收料')
    if plan['after']<=plan['before']: raise SubkitError('尚未形成可加工齐套数量')
    evidence, currency = material_sources(db, item, product_id, plan['detail']['bom_material_inputs'])
    plan['detail'].update(actual=evidence is not None, currency=currency)
    plan['detail'].update(component_processing_confirmation=True,processing_product_id=product_id,
        processing_allocation_ids=[a.id for a in allocations],
        processing_allocation_cost=str(sum((Decimal(e['total_cost']) for e in plan['detail']['bom_material_inputs'] if e['kind']=='allocation'),Decimal(0))))
    return item,graph,plan,pending,allocations


def material_sources(db, item, product_id, inputs):
    """Keep every real purchase portion; unknown inventory costs remain estimates."""
    from app.models.purchase_receipt import IncomingReceiptPurposeAllocation, PurchaseReceiptFact
    from app.models.supplier_requisition_order import PurchasePurposeSourceSnapshot
    from app.models.product_bom import RequisitionItemBomSource
    from app.services.material_cost_lineage import resolve_lot_actual_material_cost
    product = db.get(Product, product_id)
    rows, currencies, complete = [], set(), True
    for entry in inputs:
        amount = Decimal(entry['total_cost'])
        if entry['kind'] == 'allocation':
            allocation = db.get(IncomingReceiptPurposeAllocation, entry['id'])
            purpose = db.get(PurchasePurposeSourceSnapshot, allocation.purchase_purpose_source_snapshot_id) if allocation else None
            bom_source = db.get(RequisitionItemBomSource, purpose.source_bom_requisition_source_id) if purpose and purpose.source_bom_requisition_source_id else None
            identity = ((bom_source.sales_order_item_bom_component.sales_order_item_id,
                         bom_source.sales_order_item_bom_component.component_product_id) if bom_source
                        else (purpose.source_order_item_id, item.product_id) if purpose else None)
            if not allocation or allocation.status != 'posted' or identity != (item.id, product_id) or allocation.customer_id != product.customer_id:
                raise SubkitError('加工采购成本来源身份无效')
            expected = cost_slice(allocation.order_purpose_cost, allocation.receipt_order_purpose_sheet_qty * purpose.yield_per_sheet_snapshot, entry['before'], entry['quantity'])
            if expected != amount: raise SubkitError('加工采购成本分摊不一致')
            fact = db.get(PurchaseReceiptFact, allocation.purchase_receipt_fact_id)
        elif entry['kind'] == 'reservation':
            reservation = db.get(InventoryReservation, entry['id'])
            if not reservation or reservation.order_item_id != item.id or reservation.inventory_lot_id != entry['lot_id']:
                raise SubkitError('加工库存成本来源身份无效')
            lot = db.get(InventoryLot, entry['lot_id'])
            from app.services.raw_purchase_plans import raw_cost_source
            raw_source=raw_cost_source(db,reservation,entry,amount,product_id)
            if raw_source:
                rows.append(raw_source);currencies.add(raw_source['currency']);continue
            resolved = resolve_lot_actual_material_cost(db, lot)
            if resolved is None:
                complete = False
                continue
            allocation, fact = resolved.purpose_allocation, resolved.purchase_fact
            sheets = reservation.reserved_stock_quantity - reservation.released_stock_quantity
            pieces = reservation.credited_requirement_quantity - reservation.released_requirement_quantity
            if cost_slice(resolved.unit_material_cost * sheets, pieces, entry['before'], entry['quantity']) != amount:
                raise SubkitError('加工库存成本与原采购来源不一致')
        else:
            raise SubkitError('加工成本来源类型无效')
        if fact is None: raise SubkitError('加工采购成本事实缺失')
        currencies.add(fact.currency)
        rows.append(dict(allocation_id=allocation.id,purchase_receipt_fact_id=fact.id,amount=amount,
                         currency=fact.currency,tax_included=fact.tax_included,tax_rate=fact.tax_rate))
    if len(currencies) > 1: raise SubkitError('同一成品包含不同币种材料，请先核对')
    return (rows if complete else None), next(iter(currencies), '')


def preview_processing(db,*,order_item_id,product_id):
    from app.services.multilevel_bom_cutover_review import _row
    from app.services.multilevel_bom_semi_production import _map_hash
    from app.services.production_workflow import _receipt_auto_finished_ground_target
    item,graph,plan,pending,allocations=plan_processing(db,order_item_id,product_id)
    product=db.get(Product,product_id)
    target=_receipt_auto_finished_ground_target(db,claim=False,customer_id=product.customer_id,product_id=product_id)
    targets={}
    if graph:
        excluded=set() if target.target_kind=='fixed_shelf' else {target.location.id}
        target_products={node.product_id for node in graph.compiled.graph.nodes if node.source=='assembled' or (node.source in ('manufactured','purchased') and any(edge.parent_id==node.product_id and edge.relation=='assembly' for edge in graph.compiled.graph.edges))}
        for pid in sorted(target_products):
            destination=_receipt_auto_finished_ground_target(db,claim=False,excluded_location_ids=excluded,customer_id=product.customer_id,product_id=pid)
            targets[pid]=destination.location
            if destination.target_kind!='fixed_shelf': excluded.add(destination.location.id)
    inputs=plan['detail']['bom_material_inputs']
    reservations=[db.get(InventoryReservation,e['id']) for e in inputs if e['kind']=='reservation']
    lots={r.inventory_lot_id:db.get(InventoryLot,r.inventory_lot_id) for r in reservations}
    document=dict(plan=plan,item=_row(item),reservations=[_row(r) for r in reservations],lots=[_row(l) for l in lots.values()],
        profiles=[p[2] for p in pending],allocations=[_row(a) for a in allocations],target=_row(target.location),targets={pid:_row(location) for pid,location in targets.items()},map_hash=_map_hash())
    return dict(product_id=product_id,quantity=plan['after']-plan['before'],unit='只',estimated_cost=str(plan['total_cost']),ready=True,
        reviewed_hash=digest(document),source_locations={lid:l.warehouse_location_id for lid,l in lots.items()},
        production_location=dict(id=target.location.id,name=target.location.location_name),target_locations={pid:loc.id for pid,loc in targets.items()},target_names={pid:loc.location_name for pid,loc in targets.items()},remaining_processes=['按原订单完成裁切、压线、开槽及成型'] if any(p[2].get('raw_purchase_plan_id') for p in pending) else ['打钉'],map_hash=document['map_hash'])


def confirm_processing(db,*,order_item_id,product_id,reviewed_hash,operation_key,actor):
    from app.services.production_workflow import lock_order_rows_for_production_transition,post_automatic_receipt_completion,refresh_order_production_status
    from app.services.audit_log import append_audit_event
    if not actor.is_active or actor.role!='admin': raise SubkitError('仅活动管理员可确认实际加工完成')
    key='component-processing:'+hashlib.sha256(operation_key.encode()).hexdigest()
    request=digest(dict(item=order_item_id,product=product_id,review=reviewed_hash,key=operation_key,actor=actor.id))
    with atomic_bom(db):
        item=db.get(OrderItem,order_item_id)
        if not item: raise SubkitError('订单明细不存在')
        lock_order_rows_for_production_transition(db,[item.order_id])
        prior=db.scalar(select(ProductionCompletion).join(ProductionCompletionBatch).where(ProductionCompletionBatch.idempotency_key==key))
        if prior:
            lot=db.get(InventoryLot,prior.inventory_lot_id);detail=json.loads(lot.cost_snapshot_detail_json or '{}')
            if prior.status!='posted' or detail.get('processing_request_hash')!=request: raise SubkitError('加工确认标识已用、内容不同或已撤销')
            return dict(completion_id=prior.id,lot_id=lot.id,quantity=prior.quantity)
        preview=preview_processing(db,order_item_id=order_item_id,product_id=product_id)
        if preview['reviewed_hash']!=reviewed_hash: raise SubkitError('来源、库存、订单或位置已变化，请重新预览')
        item,graph,plan,pending,allocations=plan_processing(db,order_item_id,product_id)
        completion=post_automatic_receipt_completion(db,order_item_id=order_item_id,previous_theoretical_quantity=plan['before'],new_theoretical_quantity=plan['after'],
            material_input_delta=max(1,sum(e.get('stock_after',0)-e.get('stock_before',0) for e in plan['detail']['bom_material_inputs'])),
            material_input_cumulative=max(1,sum(a.receipt_order_purpose_sheet_qty for a in allocations),sum(db.get(InventoryReservation,e['id']).reserved_stock_quantity for e in plan['detail']['bom_material_inputs'] if e['kind']=='reservation')),operator_id=actor.id,idempotency_key=key,
            capitalized_material_cost=plan['total_cost'],cost_detail={**plan['detail'],'processing_request_hash':request},
            bom_snapshot_id=graph.snapshot.id if graph else None,processing_manual=True)
        if completion.warehouse_location_id!=preview['production_location']['id']: raise SubkitError('加工入库位置已变化')
        if graph:
            from app.services.multilevel_bom_receipts import assemble_graph_order_receipt,refresh_graph_main_task
            assemblies=assemble_graph_order_receipt(db,compiled=graph.compiled,order_item_id=item.id,operation_key=f'component-processing:{completion.id}',operator_id=actor.id)
            if any(db.get(InventoryLot,row.output_lot_id).warehouse_location_id!=preview['target_locations'][row.output_product_id] for row in assemblies if row.output_lot_id):
                raise SubkitError('后续组装位置与预览不一致')
            refresh_graph_main_task(db,item,create_if_missing=True)
        from app.services.multilevel_bom_semi_production import _map_hash
        if _map_hash()!=preview['map_hash']: raise SubkitError('加工过程中地图发生变化，请重新核对')
        refresh_order_production_status(db,item.order_id)
        append_audit_event(db,event_category='business',result='success',source='web',module_code='orders',action_code='confirm_component_processing',resource='production_completion',actor=actor,entity_type='production_completion',entity_id=completion.id,customer_id=db.get(Product,product_id).customer_id,details=dict(preview=preview,input_plan=plan,request_hash=request))
        return dict(completion_id=completion.id,lot_id=completion.inventory_lot_id,quantity=completion.quantity)


def reverse_processing(db,*,order_item_id,completion_id,actor):
    from app.services.production_workflow import reverse_production_completion
    from app.services.multilevel_bom_inventory import reverse_order_assembly
    if not actor.is_active or actor.role!='admin': raise SubkitError('仅活动管理员可撤销加工确认')
    with atomic_bom(db):
        matches=[(r,d) for r,d in processing_completions(db,order_item_id,posted=False) if r.id==completion_id]
        if not matches: raise SubkitError('该完工不是本订单专用部件加工确认')
        completion,detail=matches[0]
        if completion.status=='reversed': return dict(completion_id=completion.id,status='reversed')
        if detail.get('bom_snapshot_id'):
            reverse_order_assembly(db,order_item_id=order_item_id,operation_key=f'component-processing:{completion.id}',operator_id=actor.id,source_snapshot_id=detail['bom_snapshot_id'])
        reverse_production_completion(db,completion_id=completion.id,operator_id=actor.id,reason='撤销专用盖底实际加工确认')
        if detail.get('bom_snapshot_id'):
            from app.services.multilevel_bom_receipts import refresh_graph_main_task
            refresh_graph_main_task(db,db.get(OrderItem,order_item_id),create_if_missing=False)
        from app.services.audit_log import append_audit_event
        append_audit_event(db,event_category='business',result='success',source='web',module_code='orders',action_code='reverse_component_processing',resource='production_completion',actor=actor,entity_type='production_completion',entity_id=completion.id,customer_id=db.get(Product,detail['processing_product_id']).customer_id,details={'original_request_hash':detail['processing_request_hash']})

        return dict(completion_id=completion.id,status='reversed')
