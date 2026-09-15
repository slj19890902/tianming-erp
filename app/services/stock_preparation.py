"""Replenishment trace and administrator-directed stock production.

Lists are projections of purchase/receipt facts: merely viewing old receipts
never creates inventory or infers that production has happened.
"""
import json
from decimal import Decimal
from uuid import uuid4
from sqlalchemy import select, update
from app.models.incoming_receipt import IncomingReceiptItem
from app.models.stock_replenishment import StockReplenishmentOrderItem, StockReplenishmentOrder
from app.models.stock_preparation import StockPreparationJob as Job, StockPreparationCommand as Command
from app.models.product import Product
from app.models.warehouse_inventory import InventoryLot, InventoryReservation, InventoryMovement, WarehouseLocation
from app.services.warehouse_inventory import WarehouseInventoryError, _balances, _movement, manual_finished_in
from app.services.audit_log import append_audit_event
from app.core.time_contract import beijing_today, utc_now_naive, utc_naive_to_api


def fail(message):
    raise WarehouseInventoryError(message, 409)


def source(db, receipt_id):
    receipt = db.get(IncomingReceiptItem, receipt_id)
    item = db.get(StockReplenishmentOrderItem, receipt.stock_replenishment_item_id) if receipt and receipt.stock_replenishment_item_id else None
    if not item:
        fail("备库收料来源不存在")
    return receipt, item, db.get(InventoryLot, receipt.received_inventory_lot_id) if receipt.received_inventory_lot_id else None


def location_name(db, lot):
    place = db.get(WarehouseLocation, lot.warehouse_location_id) if lot else None
    return f"{place.warehouse_floor}楼 · {place.location_name}" if place else "尚未收料"


def job_dict(db, job):
    output = db.get(InventoryLot, job.output_lot_id) if job.output_lot_id else None
    outputs = list(db.scalars(select(InventoryLot).where(InventoryLot.source_ref_type == "stock_preparation", InventoryLot.source_ref_id == job.id))) if output else []
    return dict(id=job.id, receipt_item_id=job.receipt_item_id, version=job.version, status=job.status, input_quantity=job.input_quantity,
        expected_output=job.expected_output, actual_output=job.actual_output,
        product=json.loads(job.product_snapshot), output_lot_number=" / ".join(lot.lot_number for lot in outputs) if output else None,
        output_location=" / ".join(dict.fromkeys(location_name(db,lot) for lot in outputs if lot.quantity_available + lot.quantity_reserved + lot.quantity_damaged)) if output else None,
        output_remaining=sum(lot.quantity_available + lot.quantity_reserved + lot.quantity_damaged for lot in outputs),
        output_available=output.quantity_available if output else 0,output_version=output.version if output else 0,
        output_kind='semi' if output and output.inventory_type=='semi_finished' else 'finished',output_location_id=output.warehouse_location_id if output else None)


def list_rows(db, *, scope=None, query=""):
    stmt = select(StockReplenishmentOrderItem).join(StockReplenishmentOrder).where(StockReplenishmentOrder.status != "draft")
    if scope is not None:
        stmt = stmt.where(StockReplenishmentOrderItem.customer_id.in_(scope))
    rows = []
    for item in db.scalars(stmt.order_by(StockReplenishmentOrderItem.id.desc())):
        receipts = list(db.scalars(select(IncomingReceiptItem).where(IncomingReceiptItem.stock_replenishment_item_id == item.id).order_by(IncomingReceiptItem.id)))
        from app.services.replenishment_receipt_progress import receipt_progress
        pending = receipt_progress(db, item)['remaining_quantity']
        sources = receipts + ([None] if pending or not receipts else [])
        for receipt in sources:
            # Pre-receipt-ledger replenishments already have a real stock lot.
            # Preserve that source; absence of a newer receipt is not waiting for goods.
            legacy_lot = db.get(InventoryLot, item.inventory_lot_id) if not receipts and item.inventory_lot_id else None
            lot = db.get(InventoryLot, receipt.received_inventory_lot_id) if receipt and receipt.received_inventory_lot_id else legacy_lot
            jobs = list(db.scalars(select(Job).where(Job.receipt_item_id == receipt.id).order_by(Job.id))) if receipt else []
            commands = list(db.scalars(select(Command).where(Command.receipt_item_id == receipt.id).order_by(Command.created_at, Command.operation_key))) if receipt else []
            keep = next((json.loads(c.request_json)["action"] for c in reversed(commands) if json.loads(c.request_json)["action"].startswith("keep_")), None)
            lot_family = [lot] if lot else []
            if legacy_lot and legacy_lot.source_ref_type and legacy_lot.source_ref_id:
                lot_family = list(db.scalars(select(InventoryLot).where(
                    InventoryLot.source_ref_type == legacy_lot.source_ref_type,
                    InventoryLot.source_ref_id == legacy_lot.source_ref_id,
                    InventoryLot.inventory_type == legacy_lot.inventory_type)))
                owner = (legacy_lot.finished_detail or legacy_lot.semi_finished_detail)
                lot_family = [l for l in lot_family if (l.finished_detail or l.semi_finished_detail) and owner
                    and (l.finished_detail or l.semi_finished_detail).owner_customer_id == owner.owner_customer_id
                    and (not legacy_lot.finished_detail or l.finished_detail.product_id == owner.product_id)]
            physical = sum(l.quantity_available + l.quantity_reserved + l.quantity_damaged for l in lot_family)
            status = ("stock" if lot.inventory_type == "finished" else "keep") if not receipt and lot and physical else "history" if not receipt and not pending else "waiting" if not receipt else "history" if receipt.status != "posted" else "pending" if any(j.status == "pending" for j in jobs) else "stock" if lot and lot.inventory_type == "finished" and physical else "keep" if physical and keep else "arrange" if physical else "stock" if any(job_dict(db,j)["output_remaining"] for j in jobs) else "history"
            if item.order.status == "voided":
                status = "history"
            row = dict(key=f"receipt:{receipt.id}" if receipt else f"waiting:{item.id}", receipt_item_id=receipt.id if receipt else None,
                customer_name=((item.customer.chinese_short_name or item.customer.name) if item.customer else "通用备料"), code=item.product_code_snapshot,
                name=item.product_name_snapshot, specification=f"{item.report_length_mm or '-'} × {item.report_width_mm or '-'} mm",
                material=item.material_code_snapshot, order_number=item.order.order_number,
                quantity=receipt.received_quantity if receipt else item.stocked_quantity if legacy_lot else pending, unit="个" if (lot and lot.inventory_type == "finished") or (not lot and item.target_inventory_type == "finished") else "张", available=sum(l.quantity_available for l in lot_family),
                reserved=sum(l.quantity_reserved for l in lot_family), physical=physical, lot_version=lot.version if lot else 0,
                lot_number=" / ".join(l.lot_number for l in lot_family if l.quantity_available+l.quantity_reserved+l.quantity_damaged) or (lot.lot_number if lot else None), location=" / ".join(dict.fromkeys(location_name(db,l) for l in lot_family if l.quantity_available+l.quantity_reserved+l.quantity_damaged)) or location_name(db,lot), status=status, keep=keep,
                source_kind="legacy_stock" if legacy_lot else "receipt" if receipt else "purchase",
                source_trace=dict(order_number=item.order.order_number,supplier=item.order.supplier_name,
                    ordered_at=utc_naive_to_api(item.order.confirmed_at or item.order.created_at),
                    received_at=utc_naive_to_api(receipt.created_at) if receipt else None,
                    received_quantity=receipt.received_quantity if receipt else item.stocked_quantity,
                    production_consumed=sum(j.input_quantity for j in jobs if j.status=='completed'),
                    remaining=sum(l.quantity_available for l in lot_family)),
                can_plan=bool(receipt and lot and lot.inventory_type == "semi_finished" and lot.status == "active" and receipt.status == "posted"),
                product_id=item.reference_product_id or item.product_id, factor=item.stock_yield_per_sheet, pieces_per_box=item.pieces_per_box,
                jobs=[job_dict(db,j) for j in jobs],
                history=[dict(at=utc_naive_to_api(c.created_at), action=json.loads(c.request_json)["action"], actor_id=c.actor_id) for c in commands],
                movements=[dict(at=utc_naive_to_api(m.created_at),reason=m.reason,quantity=m.quantity,unit='张' if m.unit=='sheets' else '只',order_item_id=m.related_order_item_id,lot_id=m.inventory_lot_id,available_before=m.before_available,available_after=m.after_available) for m in db.scalars(select(InventoryMovement).where(InventoryMovement.inventory_lot_id.in_([l.id for l in lot_family]+[j.output_lot_id for j in jobs if j.output_lot_id])).order_by(InventoryMovement.id))] if lot else [])
            if query.casefold() in " ".join(str(row[k] or "") for k in ("code","name","customer_name","order_number","lot_number")).casefold():
                rows.append(row)
    priority = {"arrange":0,"pending":1,"waiting":2,"keep":3,"stock":4,"history":5}
    return sorted(rows,key=lambda row:priority[row["status"]])



def plan_product(db, item, lot):
    product = db.get(Product, item.reference_product_id or item.product_id)
    if not product or not product.is_active or product.deleted_at or product.customer_id != item.customer_id:
        fail("备料缺少有效的同客户目标产品，请先完善补货产品资料")
    if product.is_virtual_composite_parent:
        fail("虚拟组合母件不能直接入库，请使用实际子件")
    detail = lot.semi_finished_detail
    if (not detail or detail.flute_type != product.flute_type
            or (product.report_length_mm and product.report_length_mm > detail.board_length_mm)
            or (product.report_width_mm and product.report_width_mm > detail.board_width_mm)):
        fail("现有产品规格或楞型与实收材料不匹配，请核对资料")
    if lot.allowed_products and product.id not in {binding.product_id for binding in lot.allowed_products}:
        fail("该批材料的已确认适用产品范围不包含目标产品")
    from app.services.warehouse_goods import qualification_issues
    issues = qualification_issues(db,lot,product)
    if issues:
        fail("材料不能用于该产品："+"；".join(issues))
    return product

def mutate(db, *, receipt_id, payload, actor, group_snapshot=None, output_kind='finished'):
    if payload['action']=='store_output':
        from app.services.stock_preparation_disposition import store_output
        return store_output(db,receipt_id,payload,actor)
    receipt, item, lot = source(db, receipt_id)
    request = json.dumps(dict(payload, receipt_id=receipt_id), sort_keys=True, ensure_ascii=False)
    previous = db.get(Command, payload["operation_key"])
    if previous:
        if previous.request_json != request or previous.actor_id != actor.id:
            fail("操作标识已用于其他内容，请刷新后重试")
        return json.loads(previous.result_json)
    if receipt.status != "posted" or item.order.status == "voided" or not lot or lot.status != "active":
        fail("收料已撤销或库存不可用，请刷新")
    if lot.version != payload["lot_version"]:
        fail("库存已变化，请刷新后重新安排")
    action = payload["action"]
    before = _balances(lot)
    values = dict(version=InventoryLot.version + 1, last_movement_at=utc_now_naive())
    result = {"action":action}
    reservation = None
    movement_type = "adjust"
    quantity = 0
    if action in ("keep_raw", "keep_semi"):
        if lot.inventory_type != "semi_finished" or lot.quantity_available <= 0:
            fail("没有可保留的未分配材料")
        # This is a usage decision, not a rewrite of receipt material/shape facts.
        if payload.get('location_id'):
            from app.services.stock_preparation_disposition import relocate_material
            relocate_material(db,lot,payload['location_id'],payload.get('layout_version'),actor,payload['operation_key'])
    elif action == "plan":
        quantity = payload["quantity"]
        if lot.inventory_type != "semi_finished" or quantity <= 0 or quantity > lot.quantity_available:
            fail("生产投入必须大于0且不能超过可用材料")
        product = plan_product(db,item,lot)
        from app.services.finished_stock_identity import product_basis
        expected = quantity * item.stock_yield_per_sheet // item.pieces_per_box
        if expected <= 0:
            fail("投入材料不足以产出一个成品")
        planned_location = None
        if payload.get("location_id"):
            from app.services.stock_preparation_groups import destination
            planned_location = destination(db,payload["location_id"],payload.get("layout_version"))
        reservation = InventoryReservation(reservation_number=f"SP-{uuid4().hex[:20]}", inventory_lot_id=lot.id,
            reservation_type="semi_order", reserved_stock_quantity=quantity, status="active",
            reserved_by=actor.id, reserved_at=utc_now_naive(), idempotency_key=f"prep:{payload['operation_key']}",
            warning_codes="stock_preparation", reservation_group_key=f"prep:{payload['operation_key']}")
        db.add(reservation); db.flush()
        job = Job(receipt_item_id=receipt.id, reservation_id=reservation.id, product_id=product.id,
            product_snapshot=json.dumps(dict(product_id=product.id,code=product.product_code,name=product.product_name,
                factor=item.stock_yield_per_sheet,pieces_per_box=item.pieces_per_box,physical_basis=product_basis(product),
                planned_location=planned_location,preparation_group=group_snapshot),ensure_ascii=False),
            input_quantity=quantity, expected_output=expected)
        db.add(job); db.flush()
        result["job_id"] = job.id
        values.update(quantity_available=InventoryLot.quantity_available-quantity, quantity_reserved=InventoryLot.quantity_reserved+quantity)
        movement_type = "reserve"
    else:
        job = db.get(Job,payload["job_id"])
        if not job or job.receipt_item_id != receipt.id or job.status != "pending" or job.version != payload["job_version"]:
            fail("待生产任务已变化，请刷新")
        frozen_group = json.loads(job.product_snapshot).get("preparation_group")
        if frozen_group and (not group_snapshot or group_snapshot.get("key") != frozen_group["key"]):
            fail("该子件属于整组生产，请从整组入口操作")
        reservation = db.get(InventoryReservation,job.reservation_id)
        quantity = job.input_quantity
        if not reservation or reservation.status != "active" or reservation.inventory_lot_id != lot.id or lot.quantity_reserved < quantity:
            fail("生产预占已变化，不能继续")
        if action == "cancel":
            values.update(quantity_available=InventoryLot.quantity_available+quantity, quantity_reserved=InventoryLot.quantity_reserved-quantity)
            reservation.status="released"; reservation.released_stock_quantity=quantity
            reservation.released_by=actor.id; reservation.released_at=utc_now_naive()
            movement_type="release_reserve"; job.status="cancelled"
        elif action == "complete":
            from app.services.inventory_valuation import require_inherited_entry_cost
            require_inherited_entry_cost(db, lot)
            actual = payload["actual_output"]
            if actual <= 0:
                fail("请填写实际成品数量")
            if actual > job.expected_output and not payload["confirm_overproduction"]:
                fail("实际产出超过理论，请核对并确认超产")
            destination = db.get(WarehouseLocation,payload["location_id"])
            if not destination:
                fail("请选择实际成品入库位置")
            if output_kind == 'semi':
                from app.services.stock_preparation_disposition import semi_output
                output = semi_output(db,job,lot,item,actual,destination,payload,actor)
            else:
                output = manual_finished_in(db, customer_id=item.customer_id, product_id=job.product_id,
                location_id=destination.id, quantity=actual, stock_date=beijing_today(), source_type="transfer",
                remarks=f"备库生产；来源 {lot.lot_number}",operator_id=actor.id,
                idempotency_key=f"prep-out:{job.id}",source_ref_type="stock_preparation",source_ref_id=job.id,
                expected_layout_version=payload["layout_version"], movement_reason="备库生产确认入库",
                physical_basis_json=json.loads(job.product_snapshot)["physical_basis"])
            snapshot=json.loads(job.product_snapshot)
            if output.finished_detail:
                output.finished_detail.inventory_code_snapshot=snapshot["code"]
                output.finished_detail.product_name_snapshot=snapshot["name"]
            output.estimated_unit_cost_snapshot=None
            if lot.estimated_unit_cost_snapshot is not None:
                total=Decimal(str(lot.estimated_unit_cost_snapshot))*quantity
                output.estimated_unit_cost_snapshot=total/actual
                output.cost_snapshot_source="stock_preparation"
                output.cost_snapshot_detail_json=json.dumps(dict(source_lot_id=lot.id,input_quantity=quantity,total_cost=str(total),output_quantity=actual))
            job.output_lot_id=output.id; job.actual_output=actual; job.status="completed"
            values.update(quantity_reserved=InventoryLot.quantity_reserved-quantity,quantity_consumed=InventoryLot.quantity_consumed+quantity)
            reservation.status="consumed"; reservation.consumed_stock_quantity=quantity
            reservation.consumed_by=actor.id; reservation.consumed_at=utc_now_naive()
            movement_type="consume"
        else:
            fail("不支持的操作")
        job.version += 1
        result["job_id"]=job.id
    changed=db.execute(update(InventoryLot).where(InventoryLot.id==lot.id,InventoryLot.version==payload["lot_version"],InventoryLot.status=="active").values(**values))
    if changed.rowcount != 1:
        fail("库存已被其他操作修改，请刷新")
    db.flush(); db.refresh(lot)
    _movement(db,lot=lot,movement_type=movement_type,quantity=quantity,before=before,operator_id=actor.id,
        reservation_id=reservation.id if reservation else None, reason={"keep_raw":"保留原料备货","keep_semi":"保留半成品备货","plan":"备库安排生产","cancel":"取消备库生产安排","complete":"备库生产消耗材料"}[action],
        idempotency_key=f"prep-move:{payload['operation_key']}")
    db.add(Command(operation_key=payload["operation_key"], receipt_item_id=receipt.id,request_json=request,
        result_json=json.dumps(result),actor_id=actor.id))
    append_audit_event(db,event_category="business",result="success",source="web",module_code="production",action_code=f"stock_preparation.{action}",
        resource="production",actor=actor,entity_type="incoming_receipt_item",entity_id=receipt.id,details=dict(result,input_quantity=quantity,source_lot_id=lot.id))
    db.flush()
    return result
