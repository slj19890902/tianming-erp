"""Finite, reviewed correction of the two confirmed 2026-10-14 emergency lines.

No HTTP route, commit, dispatch, or generic stock override is exposed here.
The operator keeps the outer transaction and can review a new snapshot before
applying. Old delivery revisions, purchase purposes and entry costs remain.
"""
import hashlib
import json
from decimal import Decimal
from sqlalchemy import inspect, select, update, func, or_

from app.api.deps import require_customer_access
from app.models.audit import OperationLog
from app.models.delivery import Delivery, DeliveryItem, DeliveryPickTask
from app.models.finance import FinanceIdempotencyRecord
from app.models.order import Order, OrderItem
from app.models.product_bom import SalesOrderItemBomComponent, RequisitionItemBomSource
from app.models.procurement_source import ProcurementSourceLink
from app.models.requisition import RequisitionItem
from app.models.supplier_requisition_order import (
    SupplierRequisitionOrder, SupplierRequisitionOrderItem, PurchasePurposeSourceSnapshot,
)
from app.models.warehouse_inventory import (
    InventoryLot, InventoryMovement, InventoryReservation, InventoryPallet, InventoryPalletItem,
    WarehouseGroundOccupancy, WarehouseGroundOccupancySlot,
    UnorderedFinishedDeliveryAllocation,
)
from app.services.bom_transactions import atomic_bom
from app.services.warehouse_inventory import WarehouseInventoryError


def require(condition, message):
    if not condition:
        raise WarehouseInventoryError(message, 409)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':'), default=str).encode()).hexdigest()


def row(value):
    return {column.key: getattr(value, column.key) for column in inspect(value).mapper.column_attrs}


def actor_guard(db, actor):
    require(actor is not None and actor.is_active and actor.role == 'admin', '仅活动管理员可执行已核实的应急更正')
    require_customer_access(136, actor, db)


def pending_line_facts(db, line_id):
    require(line_id in (1024, 1027), '本次更正仅支持已核实的两条应急明细')
    delivery = db.get(Delivery, 154)
    line = db.get(DeliveryItem, line_id)
    require(delivery is not None and delivery.status == 'pending' and delivery.customer_id == 136
        and delivery.delivery_number == 'YL-20261014-001' and not delivery.is_historical_backfill,
        '原送货单状态或身份已变化，需重新核账')
    require(line is not None and line.delivery_id == 154 and line.is_current
        and line.source_type == 'unordered_finished' and line.delivered_quantity == 50
        and line.product_id == (3494 if line_id == 1024 else 3496), '原应急明细已变化，不能重复更正')
    require(not db.scalar(select(DeliveryPickTask.id).where(DeliveryPickTask.delivery_id == 154,
        DeliveryPickTask.status != 'discarded').limit(1)), '原单已有拿货任务，请先核对实际拿货结果')
    records = list(db.scalars(select(FinanceIdempotencyRecord).where(
        FinanceIdempotencyRecord.resource_id == 154,
        FinanceIdempotencyRecord.action == 'delivery_dispatch_command').order_by(FinanceIdempotencyRecord.id)))
    require(all(json.loads(record.response_json).get('result') == 'closed' for record in records),
        '原发货已有结果凭据，必须先按原请求核对，不能改写应急明细')
    allocations = list(db.scalars(select(UnorderedFinishedDeliveryAllocation).where(
        UnorderedFinishedDeliveryAllocation.delivery_item_id == line_id).order_by(UnorderedFinishedDeliveryAllocation.id)))
    require(len(allocations) == 1 and allocations[0].inventory_lot_id == (1430 if line_id == 1024 else 666)
        and allocations[0].planned_quantity == 50 and allocations[0].status == 'planned'
        and not allocations[0].consumed_quantity and not allocations[0].restored_quantity
        and allocations[0].consume_movement_id is None, '原明细库存分配已有变化或已发生出库')
    competing = db.scalar(select(UnorderedFinishedDeliveryAllocation.id).join(DeliveryItem,
        DeliveryItem.id == UnorderedFinishedDeliveryAllocation.delivery_item_id).where(
            UnorderedFinishedDeliveryAllocation.inventory_lot_id == allocations[0].inventory_lot_id,
            DeliveryItem.is_current.is_(True), DeliveryItem.id != line_id,
            UnorderedFinishedDeliveryAllocation.status == 'planned').limit(1))
    require(competing is None, '该批次另有待发分配，请重新核对本次更正范围')
    return dict(delivery=row(delivery), line=row(line), allocations=[row(value) for value in allocations],
        dispatch_records=[row(value) for value in records])


def replace_pending_order_line(db, *, line_id, order_item_id, expected_delivery_version, actor):
    """Replace just the authorized line; keep every other frozen row verbatim."""
    actor_guard(db, actor)
    require((line_id, order_item_id) == (1024, 10592), '送货更正范围不符')
    pending_line_facts(db, line_id)
    claimed = db.execute(update(Delivery).where(Delivery.id == 154, Delivery.status == 'pending',
        Delivery.version == expected_delivery_version).values(version=Delivery.version))
    require(claimed.rowcount == 1, '送货单版本已变化，请重新核账')
    from app.api.deliveries import DeliveryLineCreate, _collect_delivery_lines, _prepare_shelf_draft_replacement
    from app.services.delivery_snapshots import build_order_delivery_snapshot
    old = db.get(DeliveryItem, line_id)
    other_rows = list(db.scalars(select(DeliveryItem).where(DeliveryItem.delivery_id == 154,
        DeliveryItem.id != line_id).order_by(DeliveryItem.id)))
    before_others = [row(value) for value in other_rows]
    require(not any(value.is_current and value.order_item_id == order_item_id for value in other_rows),
        '该订单明细已在原送货单内，不能重复增加')
    request = DeliveryLineCreate(source_type='order', order_item_id=order_item_id, delivered_quantity=50,
        customer_po=old.customer_po_snapshot, remarks=old.remarks)
    built, total, warnings = _collect_delivery_lines(db, customer_id=136, lines=[request], user=actor)
    require(total == 50 and not warnings and len(built) == 1, '订单可送数量已变化')
    _prepare_shelf_draft_replacement(db, 154, line_id)
    item = db.get(OrderItem, order_item_id)
    old.is_current = False
    new = DeliveryItem(delivery_id=154, source_type='order', order_item_id=item.id,
        **build_order_delivery_snapshot(db, item, 50), customer_po_snapshot=old.customer_po_snapshot,
        delivered_quantity=50, ordered_quantity_snapshot=item.quantity,
        order_remaining_snapshot=item.quantity-item.delivered_quantity,
        over_delivery_quantity=0, remarks=old.remarks,
        revision_number=1+int(db.scalar(select(func.max(DeliveryItem.revision_number)).where(DeliveryItem.delivery_id==154)) or 0))
    db.add(new)
    delivery = db.get(Delivery,154)
    delivery.version = expected_delivery_version + 1
    kinds = {value.source_type for value in other_rows if value.is_current} | {'order'}
    delivery.source_mode = 'mixed' if len(kinds)>1 else 'order'
    db.flush()
    require([row(value) for value in other_rows] == before_others, '无关送货明细被修改，更正已取消')
    require(db.scalar(select(func.sum(DeliveryItem.delivered_quantity)).where(DeliveryItem.delivery_id==154,
        DeliveryItem.is_current.is_(True))) == delivery.total_quantity, '送货总量不守恒')
    return dict(old_line_id=old.id, new_line_id=new.id, order_item_id=item.id,
        before_version=expected_delivery_version, after_version=delivery.version,
        old_contract=row(old), new_contract=row(new))


def preview_stocktake_production(db, actor):
    actor_guard(db, actor)
    delivery = pending_line_facts(db,1024)
    raw, stocktake = db.get(InventoryLot,680), db.get(InventoryLot,1430)
    require(raw is not None and stocktake is not None, '原库存批次不存在')
    require(raw.status == stocktake.status == 'active'
        and (raw.quantity_available,raw.quantity_reserved,raw.quantity_consumed)==(204,0,0)
        and (stocktake.quantity_available,stocktake.quantity_reserved,stocktake.quantity_consumed)==(50,0,0)
        and not raw.quantity_damaged and not raw.quantity_scrapped
        and not stocktake.quantity_damaged and not stocktake.quantity_scrapped,
        '原投入或应急入库数量已变化，请按最新库存重新核账')
    detail = raw.semi_finished_detail
    require(detail is not None and detail.material_code_snapshot=='X616X'
        and (detail.board_length_mm,detail.board_width_mm,detail.flute_type,detail.layer_count)==(1980,1376,'AB',5),
        '实际100张来源与已确认批次不一致')
    require(stocktake.source_type=='stocktake' and stocktake.finished_detail is not None
        and stocktake.finished_detail.product_id==3494, '原50只不是已核实的应急盘点成品')
    pallet_item=stocktake.pallet_item
    pallet=pallet_item.pallet if pallet_item is not None else None
    require(pallet is not None and pallet.id==229 and pallet.is_current
        and pallet.location_id==stocktake.warehouse_location_id and pallet_item.quantity==50,
        '应急成品的原栈板投影已变化')
    require(stocktake.location is not None and stocktake.location.floor3_layout is not None,
        '原成品位置或布局记录缺失')
    pallet_lots=list(db.scalars(select(InventoryLot).join(InventoryPalletItem,
        InventoryPalletItem.inventory_lot_id==InventoryLot.id).where(
        InventoryPalletItem.pallet_id==pallet.id).order_by(InventoryLot.id)))
    require([value.id for value in pallet_lots
        if value.quantity_available+value.quantity_reserved+value.quantity_damaged>0]==[1430],
        '原栈板还有其他实存，不允许随本次更正释放')
    pallet_items=list(db.scalars(select(InventoryPalletItem).where(
        InventoryPalletItem.pallet_id==pallet.id).order_by(InventoryPalletItem.id)))
    require(not any(value.inventory_lot_id is None and value.quantity>0 for value in pallet_items),
        '原栈板另有未关联批次的实物记录，不允许随本次更正释放')
    occupancies=list(db.scalars(select(WarehouseGroundOccupancy).where(
        WarehouseGroundOccupancy.pallet_id==pallet.id).order_by(WarehouseGroundOccupancy.id)))
    occupancy_slots=list(db.scalars(select(WarehouseGroundOccupancySlot).where(
        WarehouseGroundOccupancySlot.occupancy_id.in_([value.id for value in occupancies])
    ).order_by(WarehouseGroundOccupancySlot.id)))
    item, snapshot = db.get(OrderItem,10592), db.get(SalesOrderItemBomComponent,46)
    require(item is not None and item.product_id==3494 and item.quantity==50 and item.delivered_quantity==0
        and not item.is_force_closed and snapshot is not None and snapshot.sales_order_item_id==item.id
        and snapshot.component_product_id==3494 and snapshot.snapshot_component_pieces_per_box==2,
        '原订单或每只耗片合同已变化')
    purchase = db.get(SupplierRequisitionOrder,209)
    require(purchase is not None and purchase.order_number=='SRO-20261014-0001'
        and purchase.status=='confirmed' and purchase.supplier_name=='欢达', '误采购单身份或状态已变化')
    lines = list(db.scalars(select(SupplierRequisitionOrderItem).where(
        SupplierRequisitionOrderItem.supplier_order_id==209).order_by(SupplierRequisitionOrderItem.id)))
    require(len(lines)==1 and lines[0].id==623 and lines[0].requisition_qty==100
        and lines[0].status=='active', '采购单不再只含获准撤销的100张')
    req, source, link, purpose = db.get(RequisitionItem,206), db.get(RequisitionItemBomSource,21), db.get(ProcurementSourceLink,31), db.get(PurchasePurposeSourceSnapshot,247)
    require(req is not None and req.requisition_qty==100 and req.status=='有效'
        and source is not None and source.requisition_item_id==206 and source.sales_order_item_bom_component_id==46
        and link is not None and link.supplier_item_id==623 and link.material_requisition_item_id==206 and link.status=='active'
        and purpose is not None and purpose.material_requisition_item_id==206 and purpose.order_purpose_sheet_qty==100
        and purpose.reserve_purpose_sheet_qty==0, '采购来源或冻结用途范围已变化')
    # The supplier void also voids the entire parent group in this requisition
    # batch. Freeze that complete write scope, not just the selected typed link.
    active_links = list(db.scalars(select(ProcurementSourceLink).where(
        ProcurementSourceLink.supplier_item_id==623,
        ProcurementSourceLink.status=='active').order_by(ProcurementSourceLink.id)))
    parent_group = list(db.scalars(select(RequisitionItem).where(
        RequisitionItem.requisition_id==req.requisition_id,
        RequisitionItem.order_item_id==req.order_item_id).order_by(RequisitionItem.id)))
    active_sources = list(db.scalars(select(RequisitionItemBomSource).where(
        RequisitionItemBomSource.requisition_item_id==206,
        RequisitionItemBomSource.active_guard==1).order_by(RequisitionItemBomSource.id)))
    purposes = list(db.scalars(select(PurchasePurposeSourceSnapshot).where(or_(
        PurchasePurposeSourceSnapshot.supplier_requisition_order_item_id==623,
        PurchasePurposeSourceSnapshot.material_requisition_item_id==206
    )).order_by(PurchasePurposeSourceSnapshot.id)))
    require([value.id for value in active_links]==[31]
        and [value.id for value in parent_group]==[206]
        and [value.id for value in active_sources]==[21]
        and [value.id for value in purposes]==[247],
        '撤销将影响新增的共享来源或其他报料明细，已停止本次100张更正')
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.purchase_receipt import PurchaseReceiptFact
    from app.models.production import ProductionCompletion
    require(not db.scalar(select(IncomingReceiptItem.id).where(IncomingReceiptItem.supplier_order_id==209).limit(1))
        and not db.scalar(select(PurchaseReceiptFact.id).where(PurchaseReceiptFact.supplier_requisition_order_item_id==623).limit(1))
        and not db.scalar(select(ProductionCompletion.id).where(ProductionCompletion.order_item_id==10592).limit(1)),
        '已有收料或完工历史，不能沿用本次未收料更正')
    reservations = list(db.scalars(select(InventoryReservation).where(or_(
        InventoryReservation.inventory_lot_id.in_([680,1430]), InventoryReservation.order_item_id==10592)).order_by(InventoryReservation.id)))
    require(not any(value.status in ('active','partial','consumed') for value in reservations), '本次来源已有其他预占或耗用')
    from app.services.inventory_valuation import require_inherited_entry_cost
    cost = require_inherited_entry_cost(db,raw)
    old_cost = require_inherited_entry_cost(db,stocktake)
    from app.services.multilevel_bom_orders import read_compiled_order_bom
    compiled = read_compiled_order_bom(db,10592)
    require(compiled is not None and len(compiled.graph.nodes)==1 and not compiled.graph.edges,
        '原生产BOM不再是已核实的单款双拼')
    from app.services.multilevel_bom_semi_production import _map_hash
    document = dict(schema=1,actor_id=actor.id,delivery=delivery,
        raw=row(raw), raw_detail=row(detail), old_finish=row(stocktake), finish_detail=row(stocktake.finished_detail),
        old_pallet=row(pallet), old_pallet_item=row(pallet_item), pallet_lots=[row(value) for value in pallet_lots],
        pallet_items=[row(value) for value in pallet_items],
        occupancies=[row(value) for value in occupancies], occupancy_slots=[row(value) for value in occupancy_slots],
        original_location=row(stocktake.location), original_layout=row(stocktake.location.floor3_layout),
        map_hash=_map_hash(), order=row(db.get(Order,item.order_id)),
        item=row(item), snapshot=row(snapshot), purchase=row(purchase), purchase_lines=[row(value) for value in lines],
        requisition=row(req), source=row(source), link=row(link), purpose=row(purpose),
        void_scope=dict(links=[row(value) for value in active_links],
            parent_group=[row(value) for value in parent_group],
            sources=[row(value) for value in active_sources],
            purposes=[row(value) for value in purposes]),
        reservations=[row(value) for value in reservations],
        impact=dict(raw_sheets=-100,old_stocktake_finished=-50,new_produced_finished=50,
            purchase_cancelled=100,input_cost=str(cost*100),old_finish_value=str(old_cost*50),
            new_finish_unit_cost=str(cost*2),finished_value_delta=str(cost*100-old_cost*50)))
    document['reviewed_hash']=digest(document)
    return document


def correct_stocktake_production(db, *, actor, reviewed_hash, operation_key):
    actor_guard(db, actor)
    require(isinstance(operation_key,str) and 8 <= len(operation_key) <= 70, '更正操作标识无效')
    action='emergency_stocktake_production_20261014'
    request=digest(dict(actor=actor.id,review=reviewed_hash,key=operation_key))
    receipt_key='emergency-204:'+digest(operation_key)
    with atomic_bom(db):
        prior=db.scalar(select(FinanceIdempotencyRecord).where(FinanceIdempotencyRecord.idempotency_key==receipt_key))
        if prior:
            try:
                data=json.loads(prior.response_json)
                result = data['result']; saved_preview = data['preview']
                require(isinstance(result,dict) and saved_preview['reviewed_hash'] == reviewed_hash
                    and digest({k:v for k,v in saved_preview.items() if k!='reviewed_hash'}) == reviewed_hash
                    and result['completion']['quantity'] == 50 and result['completion']['lot_id'] > 0
                    and result['completion']['completion_id'] > 0 and result['delivery']['old_line_id'] == 1024
                    and result['delivery']['order_item_id'] == 10592 and result['delivery']['new_line_id'] > 0
                    and result['delivery']['new_contract']['id'] == result['delivery']['new_line_id']
                    and result['delivery']['new_contract']['order_item_id'] == 10592
                    and result['impact'] == saved_preview['impact'], '更正回执不完整，请核对原结果')
            except (TypeError, ValueError, KeyError):
                raise WarehouseInventoryError('更正回执不完整，请核对原结果',409)
            require(prior.actor_user_id==actor.id and prior.action==action and prior.resource_id==1430
                and prior.resource_type=='inventory_lot'
                and prior.request_hash==request, '更正标识已用于其他内容')
            return {**data['result'],'replayed':True}
        preview=preview_stocktake_production(db,actor)
        require(preview['reviewed_hash']==reviewed_hash,'库存、订单或采购已变化，请重新核对更正预览')
        from app.api.requisition import _void_supplier_order
        from app.api.warehouse import _bom_manual_sheet_state,_reserve_manual_sheet_inventory,ManualSheetReservationPayload
        from app.services.multilevel_bom_semi_production import preview_semi_production,confirm_semi_production
        from app.services.warehouse_inventory import mutate_lot
        _void_supplier_order(209,db=db,user=actor,commit=False)
        state=_bom_manual_sheet_state(db,46,'whole',actor)[3]
        candidate=next((value for value in state['items'] if value['lot_id']==680),None)
        require(candidate is not None and candidate['selectable'] and candidate['max_stock_quantity']==100,
            '原100张片料不再适用于本次生产')
        reserved=_reserve_manual_sheet_inventory(46,ManualSheetReservationPayload(
            lot_id=680,stock_quantity=100,expected_version=candidate['version'],
            expected_order_item_revision=state['expected_order_item_revision'],review_hash=candidate['review_hash'],
            warning_acknowledged_codes=candidate['warning_codes'],idempotency_key=operation_key+':reserve'),
            db,actor,is_bom=True,commit=False)
        mutate_lot(db,lot_id=1430,operation='adjust',quantity=-50,expected_version=preview['old_finish']['version'],
            operator_id=actor.id,reason='应急盘点入账更正：由100张实际耗料生产50只的完整来源记录替代，非实物盘亏',
            idempotency_key=operation_key+':stocktake-reverse')
        from app.services.floor3_locations import clear_pallet
        from app.services.warehouse_inventory import _pallet_has_physical_goods
        require(not _pallet_has_physical_goods(db,229), '原栈板仍有其他实物，不能关闭投影')
        clear_pallet(db,pallet_id=preview['old_pallet']['id'],expected_version=preview['old_pallet']['version'],
            remarks='应急盘点账务更正：关闭已清零旧投影，真实生产批次仍在同一库位；未发生实物搬运',
            operator_id=actor.id,idempotency_key=operation_key+':clear-old-projection')
        _,plan,production=preview_semi_production(db,order_item_id=10592,product_id=3494,
            target_location_id=preview['old_finish']['warehouse_location_id'])
        require(production['ready'] and production['quantity']==50
            and Decimal(production['estimated_cost'])==Decimal(preview['impact']['input_cost'])
            and production['map_hash']==preview['map_hash'],
            '实际生产数量或成本与100张产50只不一致')
        completion=confirm_semi_production(db,order_item_id=10592,product_id=3494,
            reviewed_hash=production['reviewed_hash'],operation_key=operation_key+':produce',actor=actor,
            target_location_id=preview['old_finish']['warehouse_location_id'])
        replacement=replace_pending_order_line(db,line_id=1024,order_item_id=10592,
            expected_delivery_version=preview['delivery']['delivery']['version'],actor=actor)
        raw,old,new=db.get(InventoryLot,680),db.get(InventoryLot,1430),db.get(InventoryLot,completion['lot_id'])
        require((raw.quantity_available,raw.quantity_reserved,raw.quantity_consumed)==(104,0,100)
            and old.quantity_available+old.quantity_reserved==0
            and new.quantity_available+new.quantity_reserved==50, '应急更正后库存数量不守恒')
        require(new.warehouse_location_id==old.warehouse_location_id
            ==preview['old_finish']['warehouse_location_id'], '更正不得改变原实物位置')
        cost_fields=('estimated_unit_cost_snapshot','estimated_square_price_snapshot',
            'estimated_cost_area_m2_snapshot','cost_snapshot_source',
            'cost_snapshot_detail_json','cost_snapshot_at')
        require(all(getattr(lot,field)==preview[label][field]
            for lot,label in ((raw,'raw'),(old,'old_finish')) for field in cost_fields),
            '更正不得重写原投入或应急入库成本')
        from app.services.inventory_valuation import require_inherited_entry_cost
        require(require_inherited_entry_cost(db,new)*50==Decimal(preview['impact']['input_cost']),
            '新成品成本与实际投入不一致')
        current_pallets=list(db.scalars(select(InventoryPallet).where(
            InventoryPallet.location_id==new.warehouse_location_id, InventoryPallet.is_current.is_(True))))
        require(new.pallet_item is not None and len(current_pallets)==1
            and current_pallets[0].id==new.pallet_item.pallet_id
            and not db.get(InventoryPallet,229).is_current
            and [row(value) for value in db.scalars(select(InventoryPalletItem).where(
                InventoryPalletItem.pallet_id==229).order_by(InventoryPalletItem.id))]==preview['pallet_items'],
            '原位新旧栈板投影或历史记录不一致')
        from app.services.multilevel_bom_semi_production import _map_hash
        require(_map_hash()==preview['map_hash'], '更正期间地图已变化，请重新预览')
        result=dict(replayed=False,completion=completion,reservation=reserved,delivery=replacement,
            impact=preview['impact'],production_location=production['production_location'],
            projection=dict(old_pallet_id=229,new_pallet_id=current_pallets[0].id,
                location_id=new.warehouse_location_id,physical_move=False))
        receipt=FinanceIdempotencyRecord(idempotency_key=receipt_key,request_hash=request,action=action,
            actor_user_id=actor.id,resource_type='inventory_lot',resource_id=1430,
            response_json=json.dumps(dict(preview=preview,result=result),ensure_ascii=False,sort_keys=True,default=str))
        db.add(receipt);db.flush()
        from app.services.audit_log import append_audit_event
        append_audit_event(db,event_category='business',result='success',source='web',module_code='warehouse',
            action_code=action,resource='inventory_lot',actor=actor,entity_type='inventory_lot',entity_id=1430,
            customer_id=136,request_id=digest(operation_key),description='按已核实事实撤销误采购并以真实耗料生产替代应急盘点',
            details=dict(request_hash=request,reviewed_hash=reviewed_hash,receipt_id=receipt.id,
                old_lot_id=1430,new_lot_id=completion['lot_id'],completion_id=completion['completion_id'],
                source_lot_id=680,delivery_line_id=replacement['new_line_id'],impact=preview['impact'],
                confirmed_fact='用户确认：100张取自YL0204/X616X批次；误采购未发给供应商，同意撤销'))
        db.flush()
        return result


def preview_unordered_kit(db, actor):
    """Freeze the approved unordered correction without reopening order 9782."""
    actor_guard(db, actor)
    from dataclasses import asdict
    from app.models.product import Product
    from app.services import emergency_kit_correction_20261014 as kit
    from app.services.customer_price_tax import resolve_customer_price_tax_terms
    from app.services.multilevel_bom_semi_production import _map_hash
    delivery = pending_line_facts(db, 1027)
    cancelled = db.get(Order, 9782)
    cancelled_item = db.get(OrderItem, 10552)
    require(cancelled is not None and cancelled.status == 'cancelled'
        and cancelled.order_number == 'TM20261013001'
        and cancelled_item is not None and cancelled_item.order_id == cancelled.id
        and cancelled_item.product_id == 3863, '原取消订单已变化，请重新核账')
    parent = db.get(Product, 3863)
    require(parent is not None and parent.sale_unit_price == Decimal('14.88')
        and parent.unit == '套', '父成品当前单价或单位已变化，请重新核对')
    proposal = kit.preview(db, actor=actor)
    refs = proposal['target_planned_references']
    require(len(refs) == 1 and refs[0]['line_id'] == 1027 and refs[0]['lot_id'] == 666
        and refs[0]['quantity'] == 50, '原下盖待送引用已变化')
    document = dict(schema=1, actor_id=actor.id, delivery=delivery, kit=proposal,
        cancelled_order=row(cancelled), cancelled_item=row(cancelled_item),
        parent=row(parent), price_terms=asdict(resolve_customer_price_tax_terms(db, 136)),
        other_delivery_rows=[row(value) for value in db.scalars(select(DeliveryItem).where(
            DeliveryItem.delivery_id == 154, DeliveryItem.id != 1027).order_by(DeliveryItem.id))],
        map_hash=_map_hash(), impact=dict(lower=-50, upper=-50, parent_sets=50,
            cancelled_order_unchanged=True, source_type='unordered_finished',
            before_price=str(delivery['line']['unit_price_snapshot']),
            after_price=str(parent.sale_unit_price), price_source='parent_product_default',
            before_amount=str(Decimal(delivery['line']['unit_price_snapshot'])*50),
            after_amount=str(parent.sale_unit_price*50)))
    document['reviewed_hash'] = digest(document)
    return document


def _store_corrected_unordered_kit_line(db, *, actor, output_lot_id, preview):
    from app.api.deliveries import (DeliveryLineCreate, _collect_unordered_finished_lines,
        _enforce_unordered_finished_order_priority, _store_unordered_finished_items)
    delivery = db.get(Delivery, 154)
    old = db.get(DeliveryItem, 1027)
    require(not old.is_current, '原目标明细尚未退役')
    request = DeliveryLineCreate(source_type='unordered_finished', product_id=3863,
        delivered_quantity=50, unit_price=Decimal(preview['impact']['after_price']),
        customer_po=old.customer_po_snapshot, remarks=old.remarks,
        allocations=[dict(inventory_lot_id=output_lot_id, quantity=50)])
    built, total = _collect_unordered_finished_lines(db, customer_id=136,
        lines=[request], delivery_id=154)
    require(total == 50 and len(built) == 1, '更正后父成品数量不一致')
    _enforce_unordered_finished_order_priority(db, customer_id=136,
        order_lines=[], unordered_lines=built)
    previous_ids = set(db.scalars(select(DeliveryItem.id).where(DeliveryItem.delivery_id == 154)))
    next_revision = 1 + int(db.scalar(select(func.max(DeliveryItem.revision_number)).where(
        DeliveryItem.delivery_id == 154)) or 0)
    _store_unordered_finished_items(db, delivery=delivery, built=built, user=actor)
    db.flush()
    inserted = list(db.scalars(select(DeliveryItem).where(DeliveryItem.delivery_id == 154,
        DeliveryItem.id.not_in(previous_ids))))
    require(len(inserted) == 1, '更正不能增加其他送货明细')
    new = inserted[0]
    new.revision_number = next_revision
    delivery.version = preview['delivery']['delivery']['version'] + 1
    db.flush()
    require(new.is_current and new.product_id == 3863 and new.order_item_id is None
        and new.delivered_quantity == 50 and new.unit_snapshot == '套'
        and new.price_source == 'product_default'
        and new.unit_price_snapshot == Decimal(preview['impact']['after_price']),
        '新无订单成套明细或价税快照不一致')
    contract = json.loads(new.sales_contract_json)
    terms = preview['price_terms']
    require(contract['unit'] == '套' and Decimal(contract['unit_price']) == Decimal('14.88')
        and contract['currency'] == 'CNY' and contract['tax_mode'] == terms['price_tax_mode']
        and Decimal(contract['tax_rate']) == terms['tax_rate']
        and contract['source'] == dict(kind='unordered_delivery_creation',
            profile_id=terms['profile_id'], profile_version=terms['profile_version']),
        '父成品价税与已核对快照不一致')
    quantity = json.loads(new.quantity_contract_json)
    require(quantity['customer_quantity'] == quantity['physical_quantity'] == 50
        and quantity['customer_unit'] == quantity['physical_unit'] == '套'
        and quantity['product_id'] == 3863 and quantity['customer_id'] == 136,
        '父成品客户套数与实物套数不一致')
    return dict(old_line_id=1027, new_line_id=new.id,
        before_version=preview['delivery']['delivery']['version'], after_version=delivery.version,
        old_contract=row(old), new_contract=row(new))


def correct_unordered_kit(db, *, actor, reviewed_hash, operation_key):
    actor_guard(db, actor)
    require(isinstance(operation_key, str) and 8 <= len(operation_key) <= 50, '更正操作标识无效')
    from app.services import emergency_kit_correction_20261014 as kit
    from app.services.multilevel_bom_semi_production import _map_hash
    action = 'emergency_unordered_kit_20261014'
    request = digest(dict(actor=actor.id, review=reviewed_hash, key=operation_key))
    receipt_key = 'emergency-120:' + digest(operation_key)
    with atomic_bom(db):
        prior = db.scalar(select(FinanceIdempotencyRecord).where(
            FinanceIdempotencyRecord.idempotency_key == receipt_key))
        if prior:
            require(prior.actor_user_id == actor.id and prior.action == action
                and prior.resource_type == 'delivery_item' and prior.resource_id == 1027
                and prior.request_hash == request,
                '更正标识已用于其他内容')
            try:
                data = json.loads(prior.response_json)
                result = data['result']; saved_preview = data['preview']
                require(isinstance(result, dict) and saved_preview['reviewed_hash'] == reviewed_hash
                    and digest({k:v for k,v in saved_preview.items() if k!='reviewed_hash'}) == reviewed_hash
                    and result['delivery']['old_line_id'] == 1027 and result['delivery']['new_line_id'] > 0
                    and result['delivery']['new_contract']['id'] == result['delivery']['new_line_id']
                    and result['delivery']['new_contract']['product_id'] == 3863
                    and result['delivery']['new_contract']['order_item_id'] is None
                    and result['assembly']['product_id'] == 3863 and result['assembly']['quantity'] == 50
                    and result['assembly']['output_lot_id'] > 0 and result['assembly']['source_record_id'] > 0
                    and result['impact'] == saved_preview['impact'], '更正回执不完整')
                assembly_record = db.get(FinanceIdempotencyRecord, result['assembly']['source_record_id'])
                require(assembly_record is not None and assembly_record.action == kit.SOURCE
                    and assembly_record.actor_user_id == actor.id
                    and assembly_record.resource_id == result['assembly']['output_lot_id']
                    and json.loads(assembly_record.response_json)['receipt'] == result['assembly'], '组套原回执不完整')
            except (TypeError, ValueError, KeyError):
                raise WarehouseInventoryError('更正回执不完整，请核对原结果', 409)
            return {**data['result'], 'replayed': True}
        preview = preview_unordered_kit(db, actor)
        require(preview['reviewed_hash'] == reviewed_hash, '库存、送货或取消订单已变化，请重新核对')
        claimed = db.execute(update(Delivery).where(Delivery.id == 154, Delivery.status == 'pending',
            Delivery.version == preview['delivery']['delivery']['version']).values(version=Delivery.version))
        require(claimed.rowcount == 1, '送货单版本已变化，请重新核账')
        from app.api.deliveries import _prepare_shelf_draft_replacement
        _prepare_shelf_draft_replacement(db, 154, 1027)
        old = db.get(DeliveryItem, 1027)
        old.is_current = False
        db.flush()
        ready = kit.preview(db, actor=actor)
        comparable = lambda value: {k: v for k, v in value.items()
            if k not in ('preview_hash', 'target_planned_references')}
        require(not ready['target_planned_references']
            and digest(comparable(ready)) == digest(comparable(preview['kit'])),
            '退役目标明细以外的组套事实发生变化')
        output = kit.execute(db, actor=actor, preview_hash=ready['preview_hash'],
            operation_key=operation_key + ':kit', physical_match_confirmed=True,
            evidence='用户确认：实际交付50套，取自已登记上下盖；保留TM20261013001取消状态，按原无订单送货更正')
        replacement = _store_corrected_unordered_kit_line(db, actor=actor,
            output_lot_id=output['output_lot_id'], preview=preview)
        require([row(value) for value in db.scalars(select(DeliveryItem).where(
            DeliveryItem.delivery_id == 154, DeliveryItem.id.not_in([1027, replacement['new_line_id']])
            ).order_by(DeliveryItem.id))] == preview['other_delivery_rows'], '其他送货明细被修改')
        require(row(db.get(Order, 9782)) == preview['cancelled_order']
            and row(db.get(OrderItem, 10552)) == preview['cancelled_item'], '原取消订单被修改')
        require([row(value) for value in db.scalars(select(UnorderedFinishedDeliveryAllocation).where(
            UnorderedFinishedDeliveryAllocation.delivery_item_id == 1027).order_by(
                UnorderedFinishedDeliveryAllocation.id))] == preview['delivery']['allocations'],
            '原planned分配历史被改写')
        delivery = db.get(Delivery, 154)
        require(delivery.status == 'pending' and delivery.source_mode == preview['delivery']['delivery']['source_mode']
            and db.scalar(select(func.sum(DeliveryItem.delivered_quantity)).where(
                DeliveryItem.delivery_id == 154, DeliveryItem.is_current.is_(True))) == delivery.total_quantity,
            '更正后整单状态或数量不一致')
        require(_map_hash() == preview['map_hash'], '更正期间地图已变化，请重新预览')
        result = dict(replayed=False, assembly=output, delivery=replacement, impact=preview['impact'])
        receipt = FinanceIdempotencyRecord(idempotency_key=receipt_key, request_hash=request, action=action,
            actor_user_id=actor.id, resource_type='delivery_item', resource_id=1027,
            response_json=json.dumps(dict(preview=preview, result=result), ensure_ascii=False, sort_keys=True, default=str))
        db.add(receipt); db.flush()
        from app.services.audit_log import append_audit_event
        append_audit_event(db, event_category='business', result='success', source='script', module_code='warehouse',
            action_code=action, resource='delivery_item', actor=actor, entity_type='delivery_item', entity_id=1027,
            customer_id=136, request_id=digest(operation_key), description='按已核实50套更正原无订单送货，取消订单保持',
            details=dict(receipt_id=receipt.id, assembly_receipt_id=output['source_record_id'],
                new_line_id=replacement['new_line_id'], impact=preview['impact']))
        db.flush()
        return result
