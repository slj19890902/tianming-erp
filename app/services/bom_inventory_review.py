"""Read frozen BOM stock facts, then explicitly adopt selected existing paper."""
import hashlib
import json
from sqlalchemy import select,func
from app.models.order import Order,OrderItem
from app.models.product_bom import SalesOrderItemBomComponent
from app.models.warehouse_inventory import InventoryLot,FinishedGoodsInventoryDetail
from app.models.audit import OperationLog
from app.services.bom_transactions import atomic_bom
from app.services.multilevel_bom_requirements import read_graph_requirements
from app.services.finished_stock_identity import compiled_product_bases,stock_identity_error
from app.services.warehouse_inventory import WarehouseInventoryError,finished_inventory_candidates_for_bom_component
from app.services.semi_finished_inventory import (semi_finished_candidates_for_bom_component,
    save_order_item_semi_requirement,reserve_semi_finished_inventory,SemiFinishedLotVersion)
from app.services.incoming_receipts import _snapshot_physical_pieces
from app.services.sheet_cutting_settings import component_settings
from app.services.bom_physical_quantities import resolve_bom_sheet_yield
from app.services.multilevel_bom_plan import BomPlanError


def _hash(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),default=str).encode()).hexdigest()


def _location(lot):
    return lot.location.location_name if lot.location else '货位待核对'


def review(db,item_id):
    item=db.get(OrderItem,item_id)
    if item is None:raise WarehouseInventoryError('订单明细不存在',404)
    try:
        requirements=read_graph_requirements(db,item_id)
    except BomPlanError as error:
        raise WarehouseInventoryError(str(error),409) from error
    if requirements is None:raise WarehouseInventoryError('该明细沿用旧BOM，请从原待报料入口核对库存，不自动改写旧BOM',409)
    compiled=requirements.compiled; bases=compiled_product_bases(compiled)
    plans={p.product_id:p for p in requirements.plan.products}
    nodes={n.product_id:n for n in compiled.graph.nodes}
    rows=[]
    for snapshot in compiled.snapshots:
        pid=snapshot.component_product_id;node=nodes[pid];plan=plans[pid]
        lots=finished_inventory_candidates_for_bom_component(db,order_item_id=item_id,bom_snapshot_id=snapshot.id)
        eligible={lot.id for lot in lots}
        row=dict(snapshot_id=snapshot.id,product_id=pid,product_code=snapshot.snapshot_component_product_code,
            product_name=node.name,source=node.source,unit=node.unit,required_units=plan.required_units,
            credited_units=plan.credited_units,make_units=plan.make_units,finished_available=sum(l.quantity_available for l in lots),
            rejected_finished=[],paper=[])
        # Only this customer's own product is inspected; shared-lot identities
        # and foreign owners are never exposed by this diagnostic.
        owned=db.scalars(select(InventoryLot).join(FinishedGoodsInventoryDetail).where(
            FinishedGoodsInventoryDetail.product_id==pid,
            FinishedGoodsInventoryDetail.owner_customer_id==compiled.graph.customer_id,
            FinishedGoodsInventoryDetail.is_general.is_(False),InventoryLot.status=='active',
            InventoryLot.quantity_available>0).order_by(InventoryLot.id).limit(100))
        for lot in owned:
            if lot.id in eligible:continue
            from app.services.finished_stock_identity import matches_component_stock_identity
            if matches_component_stock_identity(lot,bases[pid]):continue
            legacy_mold_omission=False
            if lot.source_ref_type=='preparation_assembly':
                from app.services.legacy_bom_mold_confirmation import preview as mold_preview
                try:
                    mold_preview(db,lot_id=lot.id,order_item_id=item_id)
                    legacy_mold_omission=True
                except WarehouseInventoryError:
                    pass
            row['rejected_finished'].append(dict(lot_id=lot.id,lot_number=lot.lot_number,quantity=lot.quantity_available,
                location=_location(lot),missing_identity=lot.finished_detail.physical_basis_json is None,
                legacy_mold_omission=legacy_mold_omission,
                reason=stock_identity_error(lot,bases[pid])))
        if node.source=='manufactured':
            for material in requirements.plan.materials:
                if material.product_id!=pid:continue
                candidates=semi_finished_candidates_for_bom_component(db,snapshot_id=snapshot.id,component_type=material.route_key)
                paper=dict(component_type=material.route_key,required_pieces=material.required_pieces,
                    credited_pieces=material.credited_pieces,remaining_pieces=max(material.required_pieces-material.credited_pieces,0),
                    purchase_sheets=material.purchase_sheets,candidates=[])
                for candidate in candidates:
                    lot=candidate.lot
                    paper['candidates'].append(dict(lot_id=lot.id,lot_number=lot.lot_number,version=lot.version,
                        location=_location(lot),available_sheets=candidate.available_stock_quantity,
                        deductible_pieces=candidate.deductible_requirement_quantity,
                        warning_codes=list(candidate.warning_codes),warning_messages=list(candidate.warning_messages),
                        signature_differences=list(candidate.signature_differences),selectable=candidate.selectable))
                row['paper'].append(paper)
        row['review_hash']=_hash(dict(item_id=item_id,quantity=item.quantity,revision=compiled.rule_revision_id,
            frozen_basis=bases[pid],cutting=snapshot.sheet_cutting_settings_snapshot,row=row))
        rows.append(row)
    return dict(order_item_id=item_id,order_id=item.order_id,order_quantity=item.quantity,rows=rows)


def requisition_display(db,item,*,finished_reserved,has_bom):
    """Projection only: persisted workflow states and action guards stay intact."""
    if item.requisition_status!='未报料':
        return item.requisition_status,''
    if not has_bom:
        return ('无需报料','成品库存已覆盖本订单数量') if finished_reserved>=item.quantity else ('未报料','')
    if db is None:
        return '未报料','按BOM子件核对库存和材料需求'
    try:
        requirements=read_graph_requirements(db,item.id)
    except BomPlanError:
        return '未报料','BOM需求待核对，请打开库存核对查看原因'
    if requirements is None:
        return '未报料','按原BOM报料流程核对库存'
    sources={n.product_id:n.source for n in requirements.compiled.graph.nodes}
    covered=all(m.purchase_sheets==0 for m in requirements.plan.materials) and all(
        p.make_units==0 for p in requirements.plan.products if sources[p.product_id]=='purchased')
    if not covered:
        return '未报料','存在未覆盖的BOM材料需求，请核对库存后采购差额'
    processing=any(p.make_units>0 for p in requirements.plan.products if sources[p.product_id] in ('assembled','manufactured'))
    return '无需报料',('库存材料已覆盖；仍需按实际完成加工、组套' if processing else '成品库存已覆盖BOM需求')


def adopt_paper(db,*,snapshot_id,payload,actor):
    """One transaction; existing stock, prices and immutable identities stay intact."""
    snapshot=db.get(SalesOrderItemBomComponent,snapshot_id)
    if snapshot is None:raise WarehouseInventoryError('BOM组件不存在',404)
    request=dict(snapshot_id=snapshot_id,actor_id=actor.id,**payload)
    request_hash=_hash(request)
    with atomic_bom(db):
        previous=db.scalar(select(OperationLog).where(OperationLog.action_code=='BOM_PAPER_ADOPT',
            func.json_extract(OperationLog.details,'$.operation_key')==payload['idempotency_key']))
        if previous:
            saved=json.loads(previous.details)
            if saved['request_hash']!=request_hash:raise WarehouseInventoryError('该操作标识已用于不同的片料采用，请核对原操作',409)
            return {**saved['result'],'replayed':True}
        item=db.get(OrderItem,snapshot.sales_order_item_id)
        if item.requisition_status!='未报料' or item.material_status=='received' or item.delivered_quantity:
            raise WarehouseInventoryError('订单已进入后续流程，请从原流程核对，不能新增片料抵扣',409)
        current=review(db,item.id)
        row=next((r for r in current['rows'] if r['snapshot_id']==snapshot_id),None)
        if row is None or row['review_hash']!=payload['review_hash']:
            raise WarehouseInventoryError('BOM需求或库存已变化，已选内容保留，请刷新核对后再采用',409)
        paper=next((p for p in row['paper'] if p['component_type']==payload['component_type']),None)
        if paper is None or not 0<payload['requested_requirement_quantity']<=paper['remaining_pieces']:
            raise WarehouseInventoryError('本次抵扣数量超过当前BOM剩余实物需求，请重新核对',409)
        known={c['lot_id']:c for c in paper['candidates']}
        if any(l['lot_id'] not in known or not known[l['lot_id']]['selectable'] for l in payload['lots']):
            raise WarehouseInventoryError('所选片料不属于当前BOM可采用范围',409)
        prefix='snapshot_component_base_' if payload['component_type']=='base' else 'snapshot_component_'
        setting=component_settings(snapshot.sheet_cutting_settings_snapshot,payload['component_type'])
        sheet_yield=setting.mold_count if setting else resolve_bom_sheet_yield(snapshot,strict=True).yield_per_sheet
        requirement=save_order_item_semi_requirement(db,order_item_id=item.id,sales_order_item_bom_component_id=snapshot.id,
            component_type=payload['component_type'],board_length_mm=getattr(snapshot,prefix+'report_length_mm'),
            board_width_mm=getattr(snapshot,prefix+'report_width_mm'),material_code=snapshot.snapshot_component_material,
            flute_type=snapshot.snapshot_component_flute_type,pieces_per_box=_snapshot_physical_pieces(snapshot,payload['component_type']),
            stock_yield_per_sheet=sheet_yield,required_piece_quantity=paper['required_pieces'],operator_id=actor.id)
        batch=reserve_semi_finished_inventory(db,requirement_id=requirement.id,
            requested_requirement_quantity=payload['requested_requirement_quantity'],
            lots=[SemiFinishedLotVersion(**lot) for lot in payload['lots']],operator_id=actor.id,
            idempotency_key=payload['idempotency_key'],confirmed=payload['confirmed'],override=payload['override'],
            warning_acknowledged_codes=payload['warning_acknowledged_codes'])
        if batch.allocated_requirement_quantity<=0:raise WarehouseInventoryError('没有可分配的片料，已选内容保留，请刷新库存',409)
        result=dict(order_item_id=item.id,snapshot_id=snapshot.id,allocated_pieces=batch.allocated_requirement_quantity,
            allocated_sheets=sum(r.reserved_stock_quantity for r in batch.reservations),
            unallocated_pieces=batch.unallocated_requirement_quantity,reservation_ids=[r.id for r in batch.reservations],
            replayed=False,message='已采用现有片料，采购需求按差额减少；模切加工及组套仍按实际完成确认')
        from app.services.audit_log import append_audit_event
        order=db.get(Order,item.order_id)
        append_audit_event(db,event_category='business',result='success',source='web',module_code='warehouse',
            action_code='BOM_PAPER_ADOPT',resource='order_item',actor=actor,entity_type='order_item',entity_id=item.id,
            customer_id=order.customer_id,details=dict(operation_key=payload['idempotency_key'],request_hash=request_hash,result=result))
        return result
