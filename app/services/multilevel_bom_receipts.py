"""Frozen physical-node receipt capacity and exact FIFO material cost.

Receipt allocations remain the material facts; ProductionCompletion remains
the finished-output fact. No second inventory ledger or guessed product ID.
"""
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select, func, or_, and_

from app.models.multilevel_bom import OrderBomGraph
from app.models.product_bom import RequisitionItemBomSource, SalesOrderItemBomComponent
from app.models.production import ProductionCompletion, ProductionTask
from app.models.warehouse_inventory import InventoryLot, InventoryReservation, OrderItemSemiRequirement
from app.services.bom_subkit_costs import cost_slice
from app.services.bom_subkits import SubkitError
from app.services.multilevel_bom_orders import read_compiled_order_bom
from app.services.multilevel_bom_compile import CompiledMasterBom
from app.services.multilevel_bom_plan import ProductNode


@dataclass(frozen=True)
class NodeReceiptContext:
    compiled: CompiledMasterBom
    node: ProductNode
    snapshot: SalesOrderItemBomComponent
    material_snapshot: SalesOrderItemBomComponent | None = None

    @property
    def material_source_id(self):
        return (self.material_snapshot or self.snapshot).id


def node_receipt_context(db, order_item_id, purpose):
    if db.get(OrderBomGraph, order_item_id) is None:
        return None
    compiled = read_compiled_order_bom(db, order_item_id)
    source = db.get(RequisitionItemBomSource, purpose.source_bom_requisition_source_id) if purpose.source_bom_requisition_source_id else None
    snapshot = next((s for s in compiled.snapshots if source and s.id == source.sales_order_item_bom_component_id), None)
    material_snapshot = None
    if snapshot is None and source is not None:
        from app.services.multilevel_bom_source_handoffs import current_source_handoffs
        handoff = next((row for row in current_source_handoffs(db, compiled)
            if row.source_snapshot_id == source.sales_order_item_bom_component_id
            and row.source_kind == "manufactured"), None)
        if handoff is not None:
            snapshot = next(row for row in compiled.snapshots if row.id == handoff.target_snapshot_id)
            material_snapshot = db.get(SalesOrderItemBomComponent, handoff.source_snapshot_id)
    if (snapshot is None or purpose.source_order_item_id not in (None, order_item_id)
            or purpose.customer_id != compiled.graph.customer_id):
        raise SubkitError("多级BOM收料缺少真实产品材料来源")
    node = next(n for n in compiled.graph.nodes if n.product_id == snapshot.component_product_id)
    if node.source != "manufactured" or source.component_type not in {r.key for r in node.routes}:
        raise SubkitError("该BOM产品不是当前纸板收料的自制物理片组")
    return NodeReceiptContext(compiled, node, snapshot, material_snapshot)


def node_purpose_snapshots(db, context, snapshots):
    source_ids = set(db.scalars(select(RequisitionItemBomSource.id).where(
        RequisitionItemBomSource.sales_order_item_bom_component_id == context.material_source_id)))
    return [s for s in snapshots if s.source_bom_requisition_source_id in source_ids]


def node_completed_quantity(db, context):
    root = context.node.product_id == context.compiled.graph.root_id
    if context.compiled.rule_revision_id is not None:
        from app.services.multilevel_bom_output_history import completion_material_source_id
        relevant_tasks = (ProductionTask.sales_order_item_bom_component_id.is_(None) if root else
            ProductionTask.sales_order_item_bom_component_id.in_(select(SalesOrderItemBomComponent.id).where(
                SalesOrderItemBomComponent.sales_order_item_id == context.snapshot.sales_order_item_id,
                SalesOrderItemBomComponent.component_product_id == context.node.product_id)))
        completions = db.scalars(select(ProductionCompletion).join(ProductionTask,
            ProductionTask.id == ProductionCompletion.task_id).where(
                ProductionCompletion.order_item_id == context.snapshot.sales_order_item_id,
                ProductionCompletion.status == "posted", relevant_tasks))
        return sum(row.actual_output_quantity for row in completions
                   if completion_material_source_id(db, row) == context.material_source_id)
    condition = ProductionTask.sales_order_item_bom_component_id.is_(None) if root else (
        ProductionTask.sales_order_item_bom_component_id == context.snapshot.id)
    return int(db.scalar(select(func.coalesce(func.sum(ProductionCompletion.actual_output_quantity), 0))
        .join(ProductionTask, ProductionTask.id == ProductionCompletion.task_id).where(
            ProductionCompletion.order_item_id == context.snapshot.sales_order_item_id,
            ProductionCompletion.status == "posted", condition)) or 0)


def node_receipt_plan(db, context, *, snapshots, allocations, current_snapshot,
                      order_delta, order_cost, currency, quantity_limit=None):
    """Return cumulative logical output and cost of ONLY newly used pieces.

    Reserved semi pieces are used before new purchased material. One receipt
    may wait for another A3 route; its unused material/cost stays uncapitalized.
    Extra cut pieces can complete a later increment without buying them again.
    """
    by_id = {s.id: s for s in node_purpose_snapshots(db, context, snapshots)}
    sources = {r.key: [] for r in context.node.routes}
    # A graph reserve always has an unambiguous frozen physical route. Reject
    # old untyped A3 reserves instead of spending them on both lid and base.
    reserves = db.execute(select(InventoryReservation, OrderItemSemiRequirement)
        .outerjoin(OrderItemSemiRequirement, OrderItemSemiRequirement.id == InventoryReservation.semi_requirement_id)
        .where(InventoryReservation.order_item_id == context.snapshot.sales_order_item_id,
            InventoryReservation.reservation_type == "semi_order", InventoryReservation.status != "cancelled",
            or_(InventoryReservation.sales_order_item_bom_component_id == context.material_source_id,
                OrderItemSemiRequirement.sales_order_item_bom_component_id == context.material_source_id))
        .order_by(InventoryReservation.id)).all()
    for reservation, requirement in reserves:
        route = requirement.component_type if requirement else "whole"
        if route not in sources:
            raise SubkitError("多级BOM备料预占缺少明确的盖底物理片组")
        pieces = int(reservation.credited_requirement_quantity or 0) - int(reservation.released_requirement_quantity or 0)
        sheets = reservation.reserved_stock_quantity - reservation.released_stock_quantity
        if pieces <= 0:
            continue
        lot = db.get(InventoryLot, reservation.inventory_lot_id)
        if lot is None or lot.status != "active" or lot.estimated_unit_cost_snapshot is None or lot.estimated_unit_cost_snapshot < 0:
            raise SubkitError("BOM备料预占库存或来源成本无效")
        factor = int(reservation.yield_factor or 0)
        if factor <= 0 or pieces > sheets * factor:
            raise SubkitError("BOM备料预占物理片数与开料出数不一致")
        sources[route].append({"kind": "reservation", "id": reservation.id, "quantity": pieces,
            "total_cost": lot.estimated_unit_cost_snapshot * sheets, "currency": "", "actual": False,
            "yield": factor, "stock_quantity": sheets, "consumed_stock": reservation.consumed_stock_quantity,
            "lot_id": lot.id})
    for allocation in sorted(allocations, key=lambda a: a.id):
        purpose = by_id.get(allocation.purchase_purpose_source_snapshot_id)
        if purpose is None:
            continue
        from app.models.purchase_receipt import PurchaseReceiptFact
        fact = db.get(PurchaseReceiptFact, allocation.purchase_receipt_fact_id)
        if fact is None:
            raise SubkitError("组件收料缺少冻结采购价格事实")
        source = db.get(RequisitionItemBomSource, purpose.source_bom_requisition_source_id)
        sources[source.component_type].append({"kind": "allocation", "id": allocation.id,
            "quantity": allocation.receipt_order_purpose_sheet_qty * int(purpose.yield_per_sheet_snapshot),
            "total_cost": allocation.order_purpose_cost, "currency": fact.currency, "actual": True})
    if current_snapshot is not None:
        current_source = db.get(RequisitionItemBomSource, current_snapshot.source_bom_requisition_source_id)
        sources[current_source.component_type].append({"kind": "current_receipt", "id": current_snapshot.id,
            "quantity": order_delta * int(current_snapshot.yield_per_sheet_snapshot),
            "total_cost": order_cost, "currency": currency, "actual": True})
    elif order_delta or order_cost or currency or allocations or snapshots:
        raise SubkitError("无来料生产不能夹带采购数量、价格或用途分配")
    before = node_completed_quantity(db, context)
    after = min(sum(s["quantity"] for s in sources[r.key]) // r.pieces_per_unit for r in context.node.routes)
    if quantity_limit is not None:
        if type(quantity_limit) is not int or quantity_limit < 0:
            raise SubkitError("半成品生产数量上限无效")
        after = min(after, quantity_limit)
    if after < before:
        raise SubkitError("该产品物理用料不足以支持已有完工，不能继续收料")
    inputs, currencies, total = [], set(), Decimal("0.0000")
    for route in context.node.routes:
        skip, remaining = before * route.pieces_per_unit, (after-before) * route.pieces_per_unit
        for source in sources[route.key]:
            capacity = source["quantity"]
            used = min(skip, capacity)
            skip -= used
            take = min(remaining, capacity-used)
            remaining -= take
            if not take:
                continue
            amount = cost_slice(source["total_cost"], capacity, used, take)
            entry = {"kind": source["kind"], "id": source["id"], "route": route.key,
                     "before": used, "quantity": take, "total_cost": str(amount), "actual": source["actual"]}
            if source["kind"] == "reservation":
                factor = source["yield"]
                stock_before = (used+factor-1)//factor
                stock_after = (used+take+factor-1)//factor
                if source["consumed_stock"] != stock_before:
                    raise SubkitError("组件备料预占已被其他完工消耗，请先核对")
                entry.update(stock_before=stock_before, stock_after=stock_after, lot_id=source["lot_id"])
            inputs.append(entry)
            total += amount
            if source["currency"]:
                currencies.add(source["currency"])
    if len(currencies) > 1:
        raise SubkitError("同一成品包含不同币种材料，需先核对成本币种")
    return {"before": before, "after": after, "total_cost": total,
            "detail": {"bom_material_product_id": context.node.product_id,
                "bom_snapshot_id": context.snapshot.id, "bom_material_inputs": inputs,
                **({"bom_material_source_snapshot_id": context.material_source_id}
                    if context.material_source_id != context.snapshot.id else {}),
                "currency": next(iter(currencies), ""), "actual": all(i["actual"] for i in inputs)}}


def plan_semi_only_production(db, *, order_item_id, product_id):
    """Read actual reserved input for an explicit production confirmation.

    No incoming allocation or purchase fact is synthesized. This is a plan,
    not an inventory writer and not a confirmation of completed processing.
    """
    from app.services.multilevel_bom_requirements import read_graph_requirements
    requirements = read_graph_requirements(db, order_item_id)
    if requirements is None:
        raise SubkitError("订单缺少冻结多级BOM")
    compiled = requirements.compiled
    node = next((node for node in compiled.graph.nodes if node.product_id == product_id), None)
    snapshot = next((row for row in compiled.snapshots if row.component_product_id == product_id), None)
    if node is None or snapshot is None or node.source != "manufactured":
        raise SubkitError("半成品生产必须选择当前冻结的自制产品")
    if any(row.product_id == product_id and row.purchase_sheets > 0 for row in requirements.plan.materials):
        raise SubkitError("该产品尚需其他材料，不能按全额半成品确认生产")
    demand = next(row for row in requirements.plan.products if row.product_id == product_id)
    context = NodeReceiptContext(compiled, node, snapshot)
    plan = node_receipt_plan(db, context, snapshots=[], allocations=[], current_snapshot=None,
        order_delta=0, order_cost=Decimal("0"), currency="", quantity_limit=demand.make_units)
    if plan["after"] < demand.make_units:
        raise SubkitError("当前版本半成品预占未完整覆盖生产需求")
    return context, plan


def consume_node_semi_inputs(db, *, completion, inputs, operator_id):
    from app.services.production_workflow import _stable_key
    from app.services.semi_finished_inventory import consume_semi_finished_reservation
    for entry in inputs:
        if entry["kind"] != "reservation":
            continue
        reservation = db.get(InventoryReservation, entry["id"])
        if reservation is None or reservation.consumed_stock_quantity != entry["stock_before"]:
            raise SubkitError("组件备料预占数量已变化")
        quantity = entry["stock_after"]-entry["stock_before"]
        if quantity:
            lot = db.get(InventoryLot, reservation.inventory_lot_id)
            consume_semi_finished_reservation(db, reservation_id=reservation.id,
                stock_quantity=quantity, expected_version=lot.version, operator_id=operator_id,
                idempotency_key=_stable_key("production-completion", completion.id, "semi", reservation.id),
                delivery_item_id=None, reason="多级BOM物理用料完工消耗预占")


def own_output_lots(db, order_item_id):
    """Include real transfer descendants, not only the original output lot.

    Warehouse transfers preserve source_ref_type/id and frozen cost identity.
    Location and lot IDs may change; names and product codes are not lineage.
    """
    from app.models.multilevel_bom import BomAssembly
    from app.models.multilevel_bom import OrderBomCutoverSource
    historical_tasks = select(ProductionTask.id).join(OrderBomCutoverSource,
        OrderBomCutoverSource.snapshot_id == ProductionTask.sales_order_item_bom_component_id).where(
            OrderBomCutoverSource.order_item_id == order_item_id, OrderBomCutoverSource.role == "history")
    completions = select(ProductionCompletion.id).where(
        ProductionCompletion.order_item_id == order_item_id, ProductionCompletion.status == "posted",
        ProductionCompletion.task_id.not_in(historical_tasks))
    assemblies = select(BomAssembly.id).where(
        BomAssembly.order_item_id == order_item_id, BomAssembly.status == "posted")
    from app.models.external_packaging_purchase import ExternalPackagingReceiptItem, ExternalPackagingPurchaseItem
    from app.services.external_receipt_state import active_receipt_item
    external = select(ExternalPackagingReceiptItem.id).join(ExternalPackagingPurchaseItem,
        ExternalPackagingPurchaseItem.id == ExternalPackagingReceiptItem.purchase_item_id).where(
            ExternalPackagingPurchaseItem.sales_order_item_id == order_item_id, active_receipt_item())
    lots = list(db.scalars(select(InventoryLot).where(or_(
        and_(InventoryLot.source_ref_type == "production_completion", InventoryLot.source_ref_id.in_(completions)),
        and_(InventoryLot.source_ref_type == "bom_external_receipt", InventoryLot.source_ref_id.in_(external)),
        and_(InventoryLot.source_ref_type == "bom_assembly", InventoryLot.source_ref_id.in_(assemblies))))))
    from app.services.multilevel_bom_output_history import current_output_lots
    compiled = read_compiled_order_bom(db, order_item_id)
    from app.services.multilevel_bom_body_inventory import carried_body_quantity
    carried_bodies = [lot for lot in lots if lot.inventory_type == "assembly_body"
        and carried_body_quantity(db, compiled, db.get(ProductionCompletion, lot.source_ref_id)) > 0]
    lots = current_output_lots(db, compiled, lots)
    lots = list({lot.id: lot for lot in lots + carried_bodies}.values())
    from app.services.multilevel_bom_execution_boundary import handoff_assembly_ids
    handoffs = handoff_assembly_ids(db, compiled)
    lots = [lot for lot in lots if not (lot.source_ref_type == "bom_assembly" and lot.source_ref_id in handoffs)]
    # Customer returns move existing output; they are not new free stock that
    # may reduce material demand a second time. Follow the original return
    # facts after source-version qualification, including repeated returns.
    from app.models.warehouse_inventory import OrderedFinishedReceiptReturn
    from app.models.delivery import DeliveryItem
    returns = list(db.scalars(select(OrderedFinishedReceiptReturn).join(DeliveryItem,
        DeliveryItem.id == OrderedFinishedReceiptReturn.delivery_item_id).where(
            DeliveryItem.order_item_id == order_item_id, OrderedFinishedReceiptReturn.status == "active")))
    owned = {lot.id: lot for lot in lots}
    visited = set()
    while True:
        added = False
        for returned in returns:
            if returned.id in visited or returned.source_inventory_lot_id not in owned:
                continue
            source = owned[returned.source_inventory_lot_id]
            original = db.get(InventoryLot, returned.return_inventory_lot_id)
            if (original is None or original.source_ref_type != "return_receipt_item"
                    or original.source_ref_id != returned.return_receipt_item_id
                    or original.finished_detail is None or source.finished_detail is None
                    or original.finished_detail.product_id != source.finished_detail.product_id
                    or original.finished_detail.owner_customer_id != source.finished_detail.owner_customer_id
                    or original.finished_detail.physical_basis_json != source.finished_detail.physical_basis_json):
                raise BomPlanError("自产退回批次与原产品或规格工艺身份不一致")
            descendants = db.scalars(select(InventoryLot).where(
                InventoryLot.source_ref_type == original.source_ref_type,
                InventoryLot.source_ref_id == original.source_ref_id,
                InventoryLot.cost_snapshot_detail_json == original.cost_snapshot_detail_json))
            for descendant in descendants:
                if (descendant.finished_detail is None
                        or descendant.finished_detail.product_id != original.finished_detail.product_id
                        or descendant.finished_detail.owner_customer_id != original.finished_detail.owner_customer_id
                        or descendant.finished_detail.physical_basis_json != original.finished_detail.physical_basis_json):
                    raise BomPlanError("自产退回移位批次身份不一致")
                owned[descendant.id] = descendant
            visited.add(returned.id)
            added = True
        if not added:
            return list(owned.values())


def assemble_graph_receipt(db, *, context, allocation, operator_id):
    return assemble_graph_order_receipt(db, compiled=context.compiled,
        order_item_id=context.snapshot.sales_order_item_id,
        operation_key=f"bom-receipt:{allocation.id}", operator_id=operator_id)


def assemble_graph_order_receipt(db, *, compiled, order_item_id, operation_key, operator_id):
    """Shared assembly path for board and external receipt facts.

    Each adapter supplies its own namespaced receipt key, not a synthetic board
    allocation or production completion. The original caller owns the commit.
    """
    from app.services.multilevel_bom_inventory import assemble_order_inventory
    from app.services.production_workflow import _receipt_auto_finished_ground_target
    pids = {n.product_id for n in compiled.graph.nodes if n.source == "assembled"
            or (n.source == "manufactured" and any(e.parent_id == n.product_id and e.relation == "assembly"
                                                   for e in compiled.graph.edges))}
    if not pids:
        return ()
    oid = order_item_id
    own_ids = {lot.id for lot in own_output_lots(db, oid)}
    from app.services.multilevel_bom_output_history import current_finished_reservation_condition
    reserved_ids = set(db.scalars(select(InventoryReservation.inventory_lot_id).where(
        InventoryReservation.order_item_id == oid, InventoryReservation.reservation_type == "finished_order",
        current_finished_reservation_condition(db, compiled),
        InventoryReservation.status.in_(("active", "partial")))))
    lots = list(db.scalars(select(InventoryLot).where(InventoryLot.id.in_(own_ids | reserved_ids),
        InventoryLot.status == "active", InventoryLot.quantity_available + InventoryLot.quantity_reserved > 0)))
    targets = {pid: _receipt_auto_finished_ground_target(db, claim=True,
        customer_id=compiled.graph.customer_id, product_id=pid).location.id for pid in sorted(pids)}
    results = assemble_order_inventory(db, order_item_id=oid,
        source_lot_versions={lot.id: lot.version for lot in lots}, target_locations=targets,
        operation_key=operation_key, operator_id=operator_id,
        available_lot_ids=sorted(own_ids.intersection(lot.id for lot in lots)))
    from app.models.order import OrderItem, Order
    from app.services.multilevel_bom_plan import plan_bom
    from app.services.production_workflow import _reserve_component_completion_lot
    item = db.get(OrderItem, oid)
    order = db.get(Order, item.order_id)
    picking = dict(plan_bom(compiled.graph, item.quantity).picking)
    snapshots = {s.component_product_id: s for s in compiled.snapshots}
    for result in results:
        if result.output_product_id not in picking or not result.output_lot_id:
            continue
        lot = db.get(InventoryLot, result.output_lot_id)
        # Assembly is itself the immutable output fact; no fake completion row.
        if lot.quantity_available:
            _reserve_component_completion_lot(db, completion=result, order=order, item=item,
                snapshot_id=snapshots[result.output_product_id].id, lot=lot, operator_id=operator_id,
                idempotency_key=f"bom-output-reserve:{result.id}",
                reserve_quantity=lot.quantity_available, reservation_number_prefix="BARS")
    return results


def graph_material_receipts_closed(db, item):
    from collections import defaultdict
    from app.models.requisition import RequisitionItem
    from app.models.supplier_requisition_order import PurchasePurposeSourceSnapshot
    from app.services.multilevel_bom_requirements import read_graph_requirements
    requirements = read_graph_requirements(db, item.id)
    if requirements is None:
        return None
    from app.services.multilevel_bom_external_closure import external_graph_receipts_closed
    if not external_graph_receipts_closed(db, item=item, requirements=requirements):
        return False
    received = defaultdict(int)
    from app.services.multilevel_bom_source_handoffs import current_source_handoffs
    from app.services.multilevel_bom_carried_material import carried_material_pieces
    current_ids = {row.id for row in requirements.compiled.snapshots}
    inherited_ids = {row.source_snapshot_id for row in current_source_handoffs(db, requirements.compiled)
        if row.source_kind == "manufactured"}
    for source, row, purpose in db.execute(select(RequisitionItemBomSource, RequisitionItem, PurchasePurposeSourceSnapshot)
        .join(RequisitionItem, RequisitionItem.id == RequisitionItemBomSource.requisition_item_id)
        .join(PurchasePurposeSourceSnapshot, PurchasePurposeSourceSnapshot.material_requisition_item_id == RequisitionItem.id)
        .where(RequisitionItem.order_item_id == item.id,
               RequisitionItemBomSource.sales_order_item_bom_component_id.in_(
                   current_ids | inherited_ids))):
        if row.status in ("有效", "supplier_requisition_created"):
            return False
        if row.status == "已入库" and source.sales_order_item_bom_component_id in current_ids:
            received[source.sales_order_item_bom_component_id, source.component_type] += purpose.order_purpose_sheet_qty
    snapshots = {s.component_product_id: s for s in requirements.compiled.snapshots}
    inherited = carried_material_pieces(db, requirements.compiled)
    yields = {(node.product_id, route.key): route.pieces_per_sheet
        for node in requirements.compiled.graph.nodes for route in node.routes}
    return all(received[snapshots[m.product_id].id, m.route_key] * yields[m.product_id, m.route_key]
        + inherited.get((m.product_id, m.route_key), 0) >= (m.purchase_sheets + (
        int(snapshots[m.product_id].spare_sheet_quantity or 0) if m.purchase_sheets else 0)) * yields[m.product_id, m.route_key]
        for m in requirements.plan.materials)


def refresh_graph_main_task(db, item, *, create_if_missing):
    from collections import defaultdict
    from app.models.multilevel_bom import BomAssembly, BomAssemblyInput
    from app.services.multilevel_bom_plan import plan_bom
    from app.services.production_workflow import ensure_receipt_auto_main_task, COMPLETED, PENDING, WAITING_MATERIAL
    compiled = read_compiled_order_bom(db, item.id)
    task = db.scalar(select(ProductionTask).where(ProductionTask.order_item_id == item.id,
        ProductionTask.sales_order_item_bom_component_id.is_(None)))
    if task is None:
        if not create_if_missing:
            return None
        task = ensure_receipt_auto_main_task(db, order_item_id=item.id)
    own_lots = own_output_lots(db, item.id)
    own_ids = {lot.id for lot in own_lots}
    consumed = defaultdict(int)
    for source in db.scalars(select(BomAssemblyInput).join(BomAssembly).where(
        BomAssembly.order_item_id == item.id, BomAssembly.status == "posted")):
        consumed[source.lot_id] += source.quantity
    quantities = defaultdict(int)
    for lot in own_lots:
        if lot.inventory_type == "assembly_body":
            from app.services.multilevel_bom_body_inventory import stock_product_identity
            stock_product_identity(db, lot)
            continue
        quantities[lot.finished_detail.product_id] += max(lot.quantity_available + lot.quantity_reserved
                                                        + lot.quantity_consumed - consumed[lot.id], 0)
    from app.services.multilevel_bom_output_history import current_finished_reservation_condition
    for reserve in db.scalars(select(InventoryReservation).where(InventoryReservation.order_item_id == item.id,
        current_finished_reservation_condition(db, compiled),
        InventoryReservation.reservation_type == "finished_order", InventoryReservation.status != "cancelled")):
        if reserve.inventory_lot_id in own_ids:
            continue
        lot = db.get(InventoryLot, reserve.inventory_lot_id)
        if lot and lot.finished_detail:
            quantities[lot.finished_detail.product_id] += max(int(reserve.credited_requirement_quantity or 0)
                                                            - reserve.released_requirement_quantity, 0)
    execution_quantity = compiled.execution_window.execution_quantity if compiled.execution_window else item.quantity
    picking = plan_bom(compiled.graph, execution_quantity).picking
    covered = min(execution_quantity, *(quantities[pid] * execution_quantity // qty for pid, qty in picking))
    status = COMPLETED if covered >= execution_quantity else PENDING if own_ids else WAITING_MATERIAL
    from app.services.receipt_purpose_distribution import _active_order_item_allocations
    allocations = _active_order_item_allocations(db, item.id)
    semi_query = select(func.coalesce(func.sum(InventoryReservation.consumed_stock_quantity), 0)).where(
        InventoryReservation.order_item_id == item.id, InventoryReservation.reservation_type == "semi_order",
        InventoryReservation.status != "cancelled")
    if compiled.rule_revision_id is not None:
        from app.models.supplier_requisition_order import PurchasePurposeSourceSnapshot
        source_ids = [row.id for row in compiled.snapshots]
        purposes = set(db.scalars(select(PurchasePurposeSourceSnapshot.id).join(RequisitionItemBomSource,
            RequisitionItemBomSource.id == PurchasePurposeSourceSnapshot.source_bom_requisition_source_id).where(
                RequisitionItemBomSource.sales_order_item_bom_component_id.in_(source_ids))))
        allocations = [row for row in allocations if row.purchase_purpose_source_snapshot_id in purposes]
        semi_query = semi_query.outerjoin(OrderItemSemiRequirement,
            OrderItemSemiRequirement.id == InventoryReservation.semi_requirement_id).where(or_(
                InventoryReservation.sales_order_item_bom_component_id.in_(source_ids),
                OrderItemSemiRequirement.sales_order_item_bom_component_id.in_(source_ids)))
    material_received = sum(row.receipt_order_purpose_sheet_qty for row in allocations)
    semi_consumed = int(db.scalar(semi_query) or 0)
    values = {"status": status, "planned_quantity": execution_quantity if status != WAITING_MATERIAL else 0,
              "finished_coverage_snapshot": covered, "ordered_quantity_snapshot": execution_quantity,
              "material_received_quantity": material_received,
              "material_input_quantity": material_received + semi_consumed,
              "readiness_basis": "automatic_receipt" if material_received or own_ids else None}
    if any(getattr(task, key) != value for key, value in values.items()):
        for key, value in values.items():
            setattr(task, key, value)
        task.version += 1
    db.flush()
    return task
