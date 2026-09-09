"""Atomic component-to-subkit conversion over the existing stock ledger."""
import hashlib
import json
from decimal import Decimal, ROUND_HALF_UP
from types import SimpleNamespace

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.time_contract import beijing_today, utc_now_naive
from app.models.bom_subkit import OrderSubkit, SubkitConversion, SubkitConversionInput
from app.models.order import Order, OrderItem
from app.models.user import User
from app.models.warehouse_inventory import InventoryLot, InventoryReservation, WarehouseLocation
from app.services.audit_log import append_audit_event
from app.services.bom_transactions import atomic_bom
from app.services.bom_subkit_planning import SubkitMember, plan_receipt_assembly
from app.services.bom_subkits import SubkitError, recipe_rows
from app.services.warehouse_inventory import _balances, _movement, manual_finished_in, inventory_fifo_sort_key


def _hash(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def assemble_subkit_inventory(
    db: Session, *, order_item_id: int, source_lot_versions: dict[int, int],
    target_location_id: int, operation_key: str, operator_id: int,
    available_lot_ids: list[int] | None = None,
    graph_product_id: int | None = None,
    quantity_limit: int | None = None,
) -> SubkitConversion:
    """Use only explicitly supplied, versioned, unreserved component balances.

    Receipt caller holds the outer transaction. No commit or formal-data repair
    is performed here; the savepoint also protects callers that catch failures.
    """
    if not operation_key or len(operation_key) > 100:
        raise SubkitError("组套操作标识无效", 400)
    Conversion, ConversionInput = SubkitConversion, SubkitConversionInput
    source_ref = "subkit_conversion"
    if graph_product_id is not None:
        if type(graph_product_id) is not int or graph_product_id <= 0:
            raise SubkitError("组装输出产品无效")
        from app.models.multilevel_bom import BomAssembly, BomAssemblyInput
        Conversion, ConversionInput = BomAssembly, BomAssemblyInput
        source_ref = "bom_assembly"
    if quantity_limit is not None and (type(quantity_limit) is not int or quantity_limit < 0):
        raise SubkitError("组装数量上限无效")
    if quantity_limit is not None and graph_product_id is None:
        raise SubkitError("数量上限仅用于真实多级组装")
    payload = {"order_item_id": order_item_id, "lots": sorted(source_lot_versions.items()),
               "location_id": target_location_id, "operator_id": operator_id,
               "available_lot_ids": sorted(available_lot_ids) if available_lot_ids is not None else None}
    if graph_product_id is not None:
        payload.update(graph_product_id=graph_product_id, quantity_limit=quantity_limit)
    request_hash = _hash(payload)
    existing = db.scalar(select(Conversion).where(Conversion.idempotency_key == operation_key))
    if existing:
        if existing.request_hash != request_hash or existing.status != "posted":
            raise SubkitError("组套操作标识已使用或该组套已撤销")
        return existing
    with atomic_bom(db):
        item = db.get(OrderItem, order_item_id)
        snapshot = db.get(OrderSubkit, order_item_id)
        recipe = None
        if graph_product_id is not None:
            from app.services.multilevel_bom_orders import read_compiled_order_bom
            from app.services.multilevel_bom_plan import plan_bom
            compiled = read_compiled_order_bom(db, order_item_id)
            if compiled is None or item is None:
                raise SubkitError("订单缺少完整多级BOM快照")
            node = next((n for n in compiled.graph.nodes if n.product_id == graph_product_id), None)
            if node is None or node.source != "assembled":
                raise SubkitError("只能组装冻结BOM中的组套成品")
            sources = {r.component_product_id: r.id for r in compiled.snapshots}
            recipe = [{"product_id": e.child_id, "pieces_per_kit": e.quantity,
                       "bom_snapshot_id": sources[e.child_id]} for e in compiled.graph.edges
                      if e.parent_id == graph_product_id and e.relation == "assembly"]
            multiplier = next(d.required_units for d in plan_bom(compiled.graph, 1).products
                              if d.product_id == graph_product_id)
            snapshot = SimpleNamespace(kit_product_id=node.product_id, kit_name_snapshot=node.name,
                                       kits_per_parent=multiplier)
        if item is None or snapshot is None:
            raise SubkitError("订单缺少冻结的子套件配方")
        order = db.get(Order, item.order_id)
        if graph_product_id is not None:
            actor = db.get(User, operator_id)
            if actor is None or not actor.is_active:
                raise SubkitError("操作人已失效")
            if order.status in ("dead", "completed", "archived", "delivered"):
                raise SubkitError("已结束订单不能继续组套")
        if item.is_force_closed or int(item.delivered_quantity or 0) >= item.quantity or order.status in ("cancelled", "closed", "已作废", "已结单"):
            raise SubkitError("已结束订单不能继续组套")
        from app.services.production_workflow import lock_order_rows_for_production_transition
        lock_order_rows_for_production_transition(db, [order.id])
        recipe = recipe if recipe is not None else recipe_rows(snapshot)
        member_ids = {r["product_id"] for r in recipe}
        snapshot_ids = {r["bom_snapshot_id"] for r in recipe}
        from app.services.composite_bom_workflow import _remaining_reservation_quantity, _reservation_status
        reservations = list(db.scalars(select(InventoryReservation).where(
            InventoryReservation.order_item_id == item.id,
            InventoryReservation.sales_order_item_bom_component_id.in_(snapshot_ids),
            InventoryReservation.status.in_(("active", "partial")),
            InventoryReservation.inventory_lot_id.in_(source_lot_versions)).order_by(InventoryReservation.id)))
        reserved_by_lot = {}
        for reservation in reservations:
            reserved_by_lot.setdefault(reservation.inventory_lot_id, []).append(reservation)
        reserved_qty = {lid: sum(_remaining_reservation_quantity(r) for r in rows)
                        for lid, rows in reserved_by_lot.items()}
        lots = []
        available = {pid: 0 for pid in member_ids}
        free_by_lot = {}
        from app.services.fixed_shelf_staging import staging_owner
        for lot_id, version in source_lot_versions.items():
            lot = db.get(InventoryLot, lot_id)
            if (lot is None or lot.status != "active" or lot.version != version
                    or lot.finished_detail is None or lot.inventory_type != "finished"):
                raise SubkitError("组套原片库存状态或版本已变化")
            detail = lot.finished_detail
            from app.services.bom_subkits import active_subkit_order
            owner = active_subkit_order(db, lot)
            if owner is not None and owner != item.id:
                raise SubkitError("组装库存已保留给其他未完成订单")
            if (detail.product_id not in member_ids or detail.owner_customer_id != order.customer_id
                    or detail.is_general or staging_owner(db, lot.id)):
                raise SubkitError("组套原片产品、客户或集货状态不匹配")
            if reserved_qty.get(lot.id, 0) > lot.quantity_reserved:
                raise SubkitError("原片预占余额不一致")
            free_by_lot[lot.id] = lot.quantity_available if available_lot_ids is None or lot.id in available_lot_ids else 0
            available[detail.product_id] += free_by_lot[lot.id] + reserved_qty.get(lot.id, 0)
            lots.append(lot)
        completed = int(db.scalar(select(func.coalesce(func.sum(
            InventoryLot.quantity_available + InventoryLot.quantity_reserved + InventoryLot.quantity_consumed), 0))
            .join(Conversion, Conversion.id == InventoryLot.source_ref_id).where(
            InventoryLot.source_ref_type == source_ref,
            Conversion.order_item_id == item.id, Conversion.status == "posted",
            Conversion.output_product_id == graph_product_id if graph_product_id is not None else True,
        )) or 0)
        remaining = max(item.quantity * snapshot.kits_per_parent - completed, 0)
        if quantity_limit is not None:
            remaining = min(remaining, quantity_limit)
        plan = plan_receipt_assembly(parent_product_id=item.product_id,
            kit_product_id=snapshot.kit_product_id,
            members=[SubkitMember(r["product_id"], r["pieces_per_kit"]) for r in recipe],
            remaining_kit_demand=remaining, eligible_pieces=available,
            allow_parent_output=graph_product_id is not None)
        conversion = Conversion(order_item_id=item.id, idempotency_key=operation_key,
            request_hash=request_hash, quantity=plan.kit_quantity, total_cost=Decimal(0),
            status="posted", created_by=operator_id)
        if graph_product_id is not None:
            conversion.output_product_id = graph_product_id
        db.add(conversion)
        db.flush()
        to_consume = {row.product_id: row.consumed_pieces for row in plan.components}
        cost = Decimal(0)
        cost_sources = []
        for lot in sorted(lots, key=inventory_fifo_sort_key):
            pid = lot.finished_detail.product_id
            take = min(free_by_lot[lot.id] + reserved_qty.get(lot.id, 0), to_consume[pid])
            if not take:
                continue
            reserved_take = min(take, reserved_qty.get(lot.id, 0))
            available_take = take - reserved_take
            from app.services.bom_subkit_costs import source_cost
            part_cost, lineage = source_cost(db, lot, take)
            cost_sources.append({**lineage, "lot_id": lot.id, "quantity": take, "cost": str(part_cost)})
            currencies = {r["currency"] for r in cost_sources if r["currency"]}
            if len(currencies) > 1:
                raise SubkitError("原片采购币种不同，请先确认换算成本，不能直接合并")
            before = _balances(lot)
            changed = db.execute(update(InventoryLot).where(
                InventoryLot.id == lot.id, InventoryLot.version == source_lot_versions[lot.id],
                InventoryLot.quantity_available >= available_take,
                InventoryLot.quantity_reserved >= reserved_take,
            ).values(quantity_available=InventoryLot.quantity_available - available_take,
                     quantity_reserved=InventoryLot.quantity_reserved - reserved_take,
                     quantity_consumed=InventoryLot.quantity_consumed + take,
                     version=InventoryLot.version + 1))
            if changed.rowcount != 1:
                raise SubkitError("组套原片已被其他操作使用")
            db.refresh(lot)
            movement = _movement(db, lot=lot, movement_type="consume", quantity=take,
                before=before, operator_id=operator_id, reason="原片自动组成子套件",
                idempotency_key=f"{operation_key}:in:{lot.id}", related_order_item_id=item.id)
            db.flush()
            used_reservations = []
            for reservation in reserved_by_lot.get(lot.id, []):
                debit = min(reserved_take, _remaining_reservation_quantity(reservation))
                if debit:
                    reservation.consumed_stock_quantity += debit
                    reservation.consumed_requirement_quantity += debit
                    reservation.consumed_by = operator_id
                    reservation.consumed_at = utc_now_naive()
                    reservation.status = _reservation_status(reservation)
                    used_reservations.append({"id": reservation.id, "quantity": debit})
                    reserved_take -= debit
            db.add(ConversionInput(conversion_id=conversion.id, lot_id=lot.id,
                product_id=pid, quantity=take, total_cost=part_cost, consume_movement_id=movement.id,
                reservations_json=json.dumps(used_reservations)))
            to_consume[pid] -= take
            cost += part_cost
        if any(to_consume.values()):
            raise SubkitError("组套原片扣减不完整")
        if plan.kit_quantity:
            destination = db.get(WarehouseLocation, target_location_id)
            if destination is None:
                raise SubkitError("组套目标库位不存在")
            output = manual_finished_in(db, customer_id=order.customer_id,
                product_id=snapshot.kit_product_id, location_id=target_location_id,
                quantity=plan.kit_quantity, stock_date=beijing_today(), source_type="transfer",
                source_ref_type=source_ref, source_ref_id=conversion.id,
                expected_layout_version=destination.floor3_layout.version if destination.floor3_layout else None,
                remarks="收料自动组套", operator_id=operator_id,
                idempotency_key=f"{operation_key}:out", movement_reason="原片自动组套入库")
            output.finished_detail.product_name_snapshot = snapshot.kit_name_snapshot
            output.estimated_unit_cost_snapshot = (cost / plan.kit_quantity).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            output.cost_snapshot_source = source_ref
            output.cost_snapshot_detail_json = json.dumps({"conversion_id": conversion.id, "total_cost": str(cost), "quantity": plan.kit_quantity})
            output.cost_snapshot_at = utc_now_naive()
            conversion.output_lot_id = output.id
        conversion.total_cost = cost
        conversion.cost_detail_json = json.dumps({"sources": cost_sources,
            "actual": bool(cost_sources) and all(r["actual"] for r in cost_sources),
            "currency": next(iter({r["currency"] for r in cost_sources if r["currency"]}), "")})
        append_audit_event(db, event_category="business", result="success", source="system",
            module_code="warehouse", action_code="assemble_subkit", resource=source_ref,
            actor=db.get(User, operator_id), entity_type=source_ref, entity_id=conversion.id,
            customer_id=order.customer_id, details={"quantity": plan.kit_quantity, "cost": str(cost), "source_lot_ids": sorted(source_lot_versions)})
        db.flush()
        return conversion


def _only_reversed_graph_consumptions(db, output):
    """Allow unwinding a deeper assembly only after each child use reversed.

    Equal balances alone are not proof: moves, counts or arbitrary adjustments
    must still block. Match every intervening movement to real reversed lineage.
    """
    from app.models.multilevel_bom import BomAssembly, BomAssemblyInput
    from app.models.warehouse_inventory import InventoryMovement
    movements = list(db.scalars(select(InventoryMovement).where(
        InventoryMovement.inventory_lot_id == output.id).order_by(InventoryMovement.id)))
    if not movements or output.version != len(movements):
        return False
    later = movements[1:]
    sources = list(db.scalars(select(BomAssemblyInput).where(BomAssemblyInput.lot_id == output.id)))
    consumes = {r.consume_movement_id: r for r in sources}
    if not consumes or len(later) != 2 * len(consumes):
        return False
    reversed_ids = set()
    for movement in later:
        if movement.id in consumes:
            source = consumes[movement.id]
            owner = db.get(BomAssembly, source.conversion_id)
            if owner is None or owner.status != "reversed" or movement.movement_type != "consume" or movement.quantity != source.quantity:
                return False
        elif movement.movement_type == "reverse_consume" and movement.reversal_of_movement_id in consumes:
            source = consumes[movement.reversal_of_movement_id]
            if source.consume_movement_id in reversed_ids or movement.quantity != source.quantity:
                return False
            reversed_ids.add(source.consume_movement_id)
        else:
            return False
    return reversed_ids == set(consumes)


def reverse_subkit_conversion(db: Session, *, conversion_id: int, operator_id: int,
                              graph_assembly: bool = False) -> None:
    Conversion, ConversionInput = SubkitConversion, SubkitConversionInput
    key_prefix = "subkit-reverse"
    if graph_assembly:
        from app.models.multilevel_bom import BomAssembly, BomAssemblyInput
        Conversion, ConversionInput = BomAssembly, BomAssemblyInput
        key_prefix = "bom-reverse"
    with atomic_bom(db):
        conversion = db.get(Conversion, conversion_id)
        if conversion is None:
            raise SubkitError("组套记录不存在", 404)
        if conversion.status == "reversed":
            return
        if conversion.output_lot_id:
            output = db.get(InventoryLot, conversion.output_lot_id)
            if (output is None or output.quantity_available != conversion.quantity or output.quantity_reserved
                    or output.quantity_consumed or output.quantity_damaged or output.quantity_scrapped
                    or (output.version != 1 and not (graph_assembly and _only_reversed_graph_consumptions(db, output)))):
                raise SubkitError("子套件已盘点、移库、预占或送货，不能撤销组套")
            before = _balances(output)
            changed = db.execute(update(InventoryLot).where(InventoryLot.id == output.id, InventoryLot.version == output.version).values(
                quantity_available=0, quantity_consumed=conversion.quantity, version=output.version + 1, status="closed"))
            if changed.rowcount != 1:
                raise SubkitError("子套件库存已变化")
            db.refresh(output)
            _movement(db, lot=output, movement_type="consume", quantity=conversion.quantity, before=before,
                operator_id=operator_id, reason="撤销组套成品", idempotency_key=f"{key_prefix}:{conversion.id}:out")
        inputs = list(db.scalars(select(ConversionInput).where(ConversionInput.conversion_id == conversion.id)))
        for source in inputs:
            lot = db.get(InventoryLot, source.lot_id)
            if lot is None or lot.quantity_consumed < source.quantity:
                raise SubkitError("组套原片消耗记录不完整")
            before = _balances(lot)
            version = lot.version
            restored_reservations = json.loads(source.reservations_json or "[]")
            reserved_restore = sum(r["quantity"] for r in restored_reservations)
            from app.services.composite_bom_workflow import _reservation_status
            for record in restored_reservations:
                reservation = db.get(InventoryReservation, record["id"])
                if reservation is None or reservation.consumed_stock_quantity < record["quantity"]:
                    raise SubkitError("原片预占消耗来源不完整")
                reservation.consumed_stock_quantity -= record["quantity"]
                reservation.consumed_requirement_quantity -= record["quantity"]
                reservation.status = _reservation_status(reservation)
            changed = db.execute(update(InventoryLot).where(InventoryLot.id == lot.id, InventoryLot.version == version).values(
                quantity_available=InventoryLot.quantity_available + source.quantity - reserved_restore,
                quantity_reserved=InventoryLot.quantity_reserved + reserved_restore,
                quantity_consumed=InventoryLot.quantity_consumed - source.quantity, version=InventoryLot.version + 1))
            if changed.rowcount != 1:
                raise SubkitError("原片库存已变化")
            db.refresh(lot)
            _movement(db, lot=lot, movement_type="reverse_consume", quantity=source.quantity, before=before,
                operator_id=operator_id, reason="撤销组套恢复原片", reversal_of_movement_id=source.consume_movement_id,
                idempotency_key=f"{key_prefix}:{conversion.id}:{source.id}")
        conversion.status = "reversed"
        append_audit_event(db, event_category="business", result="success", source="system", module_code="warehouse",
            action_code="reverse_subkit", resource="bom_assembly" if graph_assembly else "subkit_conversion", actor=db.get(User, operator_id),
            entity_type="bom_assembly" if graph_assembly else "subkit_conversion", entity_id=conversion.id, details={"quantity": conversion.quantity})
        db.flush()
