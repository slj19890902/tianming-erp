"""Controlled conversion of fully reserved legacy component stock to real kits.

The administrator endpoint owns the outer commit. Orders with
unfinished procurement, loose/unassigned stock or ambiguous parent reservations
need their own reviewed handoff; this adapter does not guess those facts.
"""
import hashlib
import json
from collections import defaultdict

from sqlalchemy import select, update

from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.user import User
from app.models.multilevel_bom import OrderBomGraph, OrderBomGraphProduct, OrderBomExecutionCutover, OrderBomCutoverSource, BomAssembly
from app.models.warehouse_inventory import InventoryLot, InventoryReservation
from app.services.bom_transactions import atomic_bom
from app.services.multilevel_bom_cutover_review import review_legacy_cutover
from app.services.multilevel_bom_execution_boundary import make_cutover_basis
from app.services.multilevel_bom_orders import read_compiled_order_bom
from app.services.multilevel_bom_snapshot import dump_graph, graph_hash, graph_schema_version
from app.services.multilevel_bom_plan import BomPlanError, plan_bom, plan_assembly
from app.services.multilevel_bom_inventory import assemble_order_inventory, _node_keys
from app.services.audit_log import append_audit_event


def _result(db, item_id, key):
    compiled = read_compiled_order_bom(db, item_id)
    pids = [n.product_id for n in compiled.graph.nodes if n.source == "assembled"]
    keys = _node_keys(key + ":assemble", pids)
    assemblies = tuple(db.scalars(select(BomAssembly).where(BomAssembly.idempotency_key.in_(keys.values()))
        .order_by(BomAssembly.id)))
    if len(assemblies) != len(pids) or any(row.status != "posted" for row in assemblies):
        raise BomPlanError("转换组装流水不完整或已撤销")
    return {"order_item_id": item_id, "execution_quantity": compiled.execution_window.execution_quantity,
            "assembly_ids": [row.id for row in assemblies], "output_lot_ids": [row.output_lot_id for row in assemblies if row.output_lot_id]}


def prepare_reserved_cutover(db, *, review, customer_id, source_lot_versions, target_locations):
    """Read-only eligibility checks shared by the preview and locked writer."""
    item = db.get(OrderItem, json.loads(review.document)["item"]["id"])
    graph = review.compiled.graph
    nodes, children, _ = graph.validated()
    picking = dict(plan_bom(graph, item.quantity - (item.delivered_quantity or 0)).picking)
    assembled = {pid for pid, node in nodes.items() if node.source == "assembled"}
    separate = nodes[graph.root_id].source == "separate" and not assembled
    if ((not separate and (nodes[graph.root_id].source != "assembled" or set(picking) != {graph.root_id}))
            or set(target_locations) != assembled
            or any(node.source == "purchased" or (node.source == "manufactured" and children[pid]) for pid, node in nodes.items())):
        raise BomPlanError("该旧订单还需要本体、配套或采购来源交接，不能直接按全预占原片转换")
    manifest = json.loads(review.document)
    if any(row["document"]["status"] not in {"dispatched", "voided"} for row in manifest["deliveries"]):
        raise BomPlanError("旧订单仍有待送货单，须先处理后再转换")
    history_ids = {row.id for row in review.history}
    reservations = list(db.scalars(select(InventoryReservation).where(InventoryReservation.order_item_id == item.id)))
    tasks = {row["id"]: row for row in manifest["tasks"]}
    if (any(r.reservation_type == "finished_order" and r.sales_order_item_bom_component_id is None for r in reservations)
            or any(row["status"] == "posted" and tasks[row["task_id"]]["sales_order_item_bom_component_id"] is None
                   for row in manifest["completions"])):
        raise BomPlanError("旧父件成品来源未绑定组件快照，须先明确历史交接归属")
    active = [r for r in reservations if r.reserved_stock_quantity > r.consumed_stock_quantity + r.released_stock_quantity]
    if not active or any(r.reservation_type != "finished_order" or r.sales_order_item_bom_component_id not in history_ids
            or r.status not in {"active", "partial"} or r.yield_factor != 1
            or r.credited_requirement_quantity != r.reserved_stock_quantity
            or r.consumed_requirement_quantity != r.consumed_stock_quantity
            or r.released_requirement_quantity != r.released_stock_quantity for r in active):
        raise BomPlanError("旧预占不完整或不是一对一原片，须独立核对")
    if {r.inventory_lot_id for r in active} != set(source_lot_versions):
        raise BomPlanError("必须完整指定该订单所有剩余原片预占")
    eligible = defaultdict(int)
    lots = {}
    for lid, version in source_lot_versions.items():
        lot = db.get(InventoryLot, lid)
        if (lot is None or lot.version != version or lot.status != "active" or lot.inventory_type != "finished"
                or lot.quantity_available != 0 or lot.finished_detail is None
                or lot.finished_detail.is_general or lot.finished_detail.owner_customer_id != customer_id):
            raise BomPlanError("原片版本、客户或可用余额与全预占转换不一致")
        quantity = sum(r.reserved_stock_quantity - r.consumed_stock_quantity - r.released_stock_quantity for r in active if r.inventory_lot_id == lid)
        if quantity > lot.quantity_reserved:
            raise BomPlanError("原片预占余额不足")
        eligible[lot.finished_detail.product_id] += quantity
        lots[lid] = lot
    remaining = item.quantity - (item.delivered_quantity or 0)
    planned = plan_assembly(graph, remaining, eligible_stock=eligible)
    if separate:
        if dict(eligible) != picking:
            raise BomPlanError("子件分存切换须逐子件完整覆盖剩余需求，不能有缺件或多余件")
        from app.services.finished_stock_identity import snapshot_basis
        current = {s.component_product_id: s for s in review.compiled.snapshots}
        history = {s.id: s for s in review.history}
        for reservation in active:
            lot = lots[reservation.inventory_lot_id]
            pid = lot.finished_detail.product_id
            old = history[reservation.sales_order_item_bom_component_id]
            old_basis = json.loads(snapshot_basis(old, nodes[pid].unit))
            new_basis = json.loads(snapshot_basis(current[pid], nodes[pid].unit))
            changed_fields = [key for key in new_basis if old_basis.get(key) != new_basis[key]]
            if (old.component_product_id != pid or old.component_product_version != nodes[pid].version
                    or changed_fields):
                raise BomPlanError(f"产品{pid}旧冻结身份与目标版本不一致（旧/新版本{old.component_product_version}/{nodes[pid].version}，差异{','.join(changed_fields)}），须先核实实物和执行资料")
    elif next((s.produced_units for s in planned.steps if s.product_id == graph.root_id), 0) != remaining:
        raise BomPlanError("原片不足以覆盖全部剩余套数，不能自动完成转换")
    if not separate and any(quantity for pid, quantity in planned.remaining_stock if pid != graph.root_id):
        raise BomPlanError("存在多余原片，须先明确保留或损耗，不能自动处置")
    return item, graph, picking, active, lots, remaining, planned


def convert_reserved_legacy_order(db, *, order_item_id, customer_id, reviewed_hash,
                                  source_lot_versions, target_locations, operation_key, actor):
    if (not isinstance(operation_key, str) or not 1 <= len(operation_key) <= 64
            or not isinstance(reviewed_hash, str) or len(reviewed_hash) != 64
            or not isinstance(source_lot_versions, dict) or not source_lot_versions
            or any(type(k) is not int or k <= 0 or type(v) is not int or v <= 0 for k, v in source_lot_versions.items())
            or not isinstance(target_locations, dict)
            or any(type(k) is not int or k <= 0 or type(v) is not int or v <= 0 for k, v in target_locations.items())):
        raise BomPlanError("转换标识、核对摘要或库存版本无效")
    if db.new or db.dirty or db.deleted:
        raise BomPlanError("请先保存或撤销未提交修改，再执行转换")
    request = json.dumps({"item": order_item_id, "customer": customer_id, "review": reviewed_hash,
        "lots": sorted(source_lot_versions.items()), "targets": sorted(target_locations.items()),
        "actor": getattr(actor, "id", None)}, sort_keys=True, separators=(",", ":"))
    request_hash = hashlib.sha256(request.encode()).hexdigest()
    with atomic_bom(db):
        actor = db.get(User, getattr(actor, "id", None), populate_existing=True)
        if actor is None or actor.role != "admin" or not actor.is_active:
            raise BomPlanError("仅活动管理员可执行旧订单库存转换")
        existing = db.get(OrderBomExecutionCutover, order_item_id)
        if existing is not None:
            if existing.idempotency_key != operation_key or existing.request_hash != request_hash:
                raise BomPlanError("订单已转换或操作标识载荷不一致")
            return _result(db, order_item_id, operation_key)
        if db.scalar(select(OrderBomExecutionCutover.order_item_id).where(
                OrderBomExecutionCutover.idempotency_key == operation_key)) is not None:
            raise BomPlanError("转换操作标识已用于其他订单")
        item = db.get(OrderItem, order_item_id, populate_existing=True)
        if item is None:
            raise BomPlanError("转换订单不存在")
        # Take the same commercial-row write lock without changing its timestamp
        # before comparing the caller's exact review manifest.
        db.execute(update(Order).where(Order.id == item.order_id).values(status=Order.status, updated_at=Order.updated_at))
        db.execute(update(OrderItem).where(OrderItem.id == item.id).values(quantity=OrderItem.quantity))
        review = review_legacy_cutover(db, order_item_id=order_item_id, customer_id=customer_id)
        if review.checksum != reviewed_hash:
            raise BomPlanError("转换核对内容已变化，请重新核对")
        item = db.get(OrderItem, order_item_id)
        order = db.get(Order, item.order_id)
        item, graph, picking, active, lots, remaining, planned = prepare_reserved_cutover(
            db, review=review, customer_id=customer_id,
            source_lot_versions=source_lot_versions, target_locations=target_locations)
        checksum = persist_cutover_review(db, review=review, item=item, customer_id=customer_id,
            operation_key=operation_key, request_hash=request_hash, actor=actor)
        # Dedicated remaining-only handoff: do not run the ordinary manual
        # release API, whose production-task refresh would change old facts.
        from app.services.warehouse_inventory import _balances, _movement, _finished_reservation_status
        from app.core.time_contract import utc_now_naive
        for reservation in active:
            lot = lots[reservation.inventory_lot_id]
            quantity = reservation.reserved_stock_quantity - reservation.consumed_stock_quantity - reservation.released_stock_quantity
            before = _balances(lot)
            updated = db.execute(update(InventoryLot).where(InventoryLot.id == lot.id, InventoryLot.version == lot.version,
                InventoryLot.quantity_reserved >= quantity).values(quantity_reserved=InventoryLot.quantity_reserved-quantity,
                    quantity_available=InventoryLot.quantity_available+quantity, version=InventoryLot.version+1,
                    last_movement_at=utc_now_naive()))
            if updated.rowcount != 1:
                raise BomPlanError("转换原片库存已变化")
            reservation.released_stock_quantity += quantity
            reservation.released_requirement_quantity += quantity
            reservation.status = _finished_reservation_status(reservation)
            reservation.released_by = actor.id
            reservation.released_at = utc_now_naive()
            reservation.release_reason = "已核对的真实BOM转换，历史消耗保留"
            db.flush()
            db.refresh(lot)
            _movement(db, lot=lot, movement_type="release_reserve", quantity=quantity, before=before,
                operator_id=actor.id, reason=reservation.release_reason, idempotency_key=f"{operation_key}:release:{reservation.id}",
                reservation_id=reservation.id, related_order_id=order.id, related_order_item_id=item.id)
        db.flush()
        from app.services.production_workflow import _reserve_component_completion_lot
        sources = {s.component_product_id: s for s in review.compiled.snapshots}
        separate = next(n for n in graph.nodes if n.product_id == graph.root_id).source == "separate"
        results = () if separate else assemble_order_inventory(db, order_item_id=item.id,
            source_lot_versions={lid: lot.version for lid, lot in lots.items()}, target_locations=target_locations,
            operation_key=operation_key+":assemble", operator_id=actor.id, available_lot_ids=sorted(lots))
        if separate:
            _retain_component_reservations(db, item=item, order=order, lots=lots, sources=sources,
                operation_key=operation_key, actor=actor)
        for result in results:
            if result.output_product_id in picking and result.output_lot_id:
                lot = db.get(InventoryLot, result.output_lot_id)
                _reserve_component_completion_lot(db, completion=result, order=order, item=item,
                    snapshot_id=sources[result.output_product_id].id, lot=lot, operator_id=actor.id,
                    idempotency_key=f"bom-output-reserve:{result.id}", reserve_quantity=lot.quantity_available, reservation_number_prefix="BARS")
        append_audit_event(db, event_category="business", result="success", source="system", module_code="warehouse",
            action_code="convert_legacy_bom_execution", resource="order_bom_execution_cutover", actor=actor,
            entity_type="order_item", entity_id=item.id, customer_id=customer_id,
            details={"review_hash": reviewed_hash, "request_hash": request_hash, "basis_hash": checksum,
                     "operation_key": operation_key, "target_locations": target_locations,
                     "source_lot_versions": source_lot_versions, "execution_quantity": remaining,
                     "delivered_before": item.delivered_quantity or 0,
                     "old_reservation_ids": [r.id for r in active], "assembly_ids": [r.id for r in results]})
        db.flush()
        return _result(db, item.id, operation_key)


def _retain_component_reservations(db, *, item, order, lots, sources, operation_key, actor):
    """Rebind only the released remainder; no completion or stock is invented."""
    from app.services.warehouse_inventory import _balances, _movement, _number
    from app.core.time_contract import utc_now_naive
    for lid, lot in sorted(lots.items()):
        quantity = lot.quantity_available  # eligibility required zero free stock before release
        snapshot = sources[lot.finished_detail.product_id]
        before = _balances(lot)
        changed = db.execute(update(InventoryLot).where(InventoryLot.id == lid,
            InventoryLot.version == lot.version, InventoryLot.quantity_available == quantity).values(
                quantity_available=0, quantity_reserved=InventoryLot.quantity_reserved + quantity,
                version=InventoryLot.version + 1, last_movement_at=utc_now_naive()))
        if changed.rowcount != 1:
            raise BomPlanError("子件切换预占时库存发生变化")
        key = f"{operation_key}:retain:{lid}"
        reservation = InventoryReservation(reservation_number=_number("BCR"), inventory_lot_id=lid,
            reservation_type="finished_order", order_id=order.id, order_item_id=item.id,
            sales_order_item_bom_component_id=snapshot.id, reserved_stock_quantity=quantity,
            credited_requirement_quantity=quantity, yield_factor=1, status="active", warning_codes="[]",
            reserved_by=actor.id, reserved_at=utc_now_naive(), idempotency_key=key)
        db.add(reservation)
        db.flush()
        db.refresh(lot)
        _movement(db, lot=lot, movement_type="reserve", quantity=quantity, before=before,
            operator_id=actor.id, reason="已核对的子件分存版本切换，原批次货位不变", idempotency_key=key,
            reservation_id=reservation.id, related_order_id=order.id, related_order_item_id=item.id)


def persist_cutover_review(db, *, review, item, customer_id, operation_key, request_hash, actor):
    """Persist reviewed source epochs; caller owns eligibility, lock and audit."""
    graph = review.compiled.graph
    for node in graph.nodes:
        changed = db.execute(update(Product).where(Product.id == node.product_id, Product.version == node.version,
            Product.customer_id == customer_id, Product.is_active.is_(True), Product.deleted_at.is_(None), Product.purged_at.is_(None))
            .values(version=Product.version, updated_at=Product.updated_at))
        if changed.rowcount != 1:
            raise BomPlanError("转换产品已变化")
    document = dump_graph(graph)
    db.add(OrderBomGraph(order_item_id=item.id, root_product_id=graph.root_id, customer_id=customer_id,
        schema_version=graph_schema_version(graph), document_json=document, content_hash=graph_hash(document), created_by=actor.id))
    db.flush()
    db.add_all([OrderBomGraphProduct(order_item_id=item.id, product_id=n.product_id, product_version=n.version) for n in graph.nodes])
    db.add_all(review.compiled.snapshots)
    db.flush()
    basis, checksum = make_cutover_basis(graph=graph, order_item_id=item.id, order_quantity=item.quantity,
        delivered_before=item.delivered_quantity or 0, history_rows=review.history, current_rows=review.compiled.snapshots)
    db.add(OrderBomExecutionCutover(order_item_id=item.id, order_quantity=item.quantity,
        delivered_before=item.delivered_quantity or 0, basis_json=basis, basis_hash=checksum,
        idempotency_key=operation_key, request_hash=request_hash, created_by=actor.id))
    db.flush()
    db.add_all([OrderBomCutoverSource(order_item_id=item.id, snapshot_id=s.id, role=role)
        for role, rows in [("history", review.history), ("current", review.compiled.snapshots)] for s in rows])
    db.flush()
    return checksum
