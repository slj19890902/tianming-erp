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


def node_receipt_context(db, order_item_id, purpose):
    if db.get(OrderBomGraph, order_item_id) is None:
        return None
    compiled = read_compiled_order_bom(db, order_item_id)
    source = db.get(RequisitionItemBomSource, purpose.source_bom_requisition_source_id) if purpose.source_bom_requisition_source_id else None
    snapshot = next((s for s in compiled.snapshots if source and s.id == source.sales_order_item_bom_component_id), None)
    if (snapshot is None or purpose.source_order_item_id not in (None, order_item_id)
            or purpose.customer_id != compiled.graph.customer_id):
        raise SubkitError("多级BOM收料缺少真实产品材料来源")
    node = next(n for n in compiled.graph.nodes if n.product_id == snapshot.component_product_id)
    if node.source != "manufactured" or source.component_type not in {r.key for r in node.routes}:
        raise SubkitError("该BOM产品不是当前纸板收料的自制物理片组")
    return NodeReceiptContext(compiled, node, snapshot)


def node_purpose_snapshots(db, context, snapshots):
    source_ids = set(db.scalars(select(RequisitionItemBomSource.id).where(
        RequisitionItemBomSource.sales_order_item_bom_component_id == context.snapshot.id)))
    return [s for s in snapshots if s.source_bom_requisition_source_id in source_ids]


def node_completed_quantity(db, context):
    root = context.node.product_id == context.compiled.graph.root_id
    condition = ProductionTask.sales_order_item_bom_component_id.is_(None) if root else (
        ProductionTask.sales_order_item_bom_component_id == context.snapshot.id)
    return int(db.scalar(select(func.coalesce(func.sum(ProductionCompletion.actual_output_quantity), 0))
        .join(ProductionTask, ProductionTask.id == ProductionCompletion.task_id).where(
            ProductionCompletion.order_item_id == context.snapshot.sales_order_item_id,
            ProductionCompletion.status == "posted", condition)) or 0)


def node_receipt_plan(db, context, *, snapshots, allocations, current_snapshot,
                      order_delta, order_cost, currency):
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
            or_(InventoryReservation.sales_order_item_bom_component_id == context.snapshot.id,
                OrderItemSemiRequirement.sales_order_item_bom_component_id == context.snapshot.id))
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
    current_source = db.get(RequisitionItemBomSource, current_snapshot.source_bom_requisition_source_id)
    sources[current_source.component_type].append({"kind": "current_receipt", "id": current_snapshot.id,
        "quantity": order_delta * int(current_snapshot.yield_per_sheet_snapshot),
        "total_cost": order_cost, "currency": currency, "actual": True})
    before = node_completed_quantity(db, context)
    after = min(sum(s["quantity"] for s in sources[r.key]) // r.pieces_per_unit for r in context.node.routes)
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
                "currency": next(iter(currencies), ""), "actual": all(i["actual"] for i in inputs)}}


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
    completions = select(ProductionCompletion.id).where(
        ProductionCompletion.order_item_id == order_item_id, ProductionCompletion.status == "posted")
    assemblies = select(BomAssembly.id).where(
        BomAssembly.order_item_id == order_item_id, BomAssembly.status == "posted")
    from app.models.external_packaging_purchase import ExternalPackagingReceiptItem, ExternalPackagingPurchaseItem
    external = select(ExternalPackagingReceiptItem.id).join(ExternalPackagingPurchaseItem,
        ExternalPackagingPurchaseItem.id == ExternalPackagingReceiptItem.purchase_item_id).where(
            ExternalPackagingPurchaseItem.sales_order_item_id == order_item_id)
    return list(db.scalars(select(InventoryLot).where(or_(
        and_(InventoryLot.source_ref_type == "production_completion", InventoryLot.source_ref_id.in_(completions)),
        and_(InventoryLot.source_ref_type == "bom_external_receipt", InventoryLot.source_ref_id.in_(external)),
        and_(InventoryLot.source_ref_type == "bom_assembly", InventoryLot.source_ref_id.in_(assemblies))))))


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
    pids = {n.product_id for n in compiled.graph.nodes if n.source == "assembled"}
    if not pids:
        return ()
    oid = order_item_id
    own_ids = {lot.id for lot in own_output_lots(db, oid)}
    reserved_ids = set(db.scalars(select(InventoryReservation.inventory_lot_id).where(
        InventoryReservation.order_item_id == oid, InventoryReservation.reservation_type == "finished_order",
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
    for source, row, purpose in db.execute(select(RequisitionItemBomSource, RequisitionItem, PurchasePurposeSourceSnapshot)
        .join(RequisitionItem, RequisitionItem.id == RequisitionItemBomSource.requisition_item_id)
        .join(PurchasePurposeSourceSnapshot, PurchasePurposeSourceSnapshot.material_requisition_item_id == RequisitionItem.id)
        .where(RequisitionItem.order_item_id == item.id)):
        if row.status in ("有效", "supplier_requisition_created"):
            return False
        if row.status == "已入库":
            received[source.sales_order_item_bom_component_id, source.component_type] += purpose.order_purpose_sheet_qty
    snapshots = {s.component_product_id: s for s in requirements.compiled.snapshots}
    return all(received[snapshots[m.product_id].id, m.route_key] >= m.purchase_sheets + (
        int(snapshots[m.product_id].spare_sheet_quantity or 0) if m.purchase_sheets else 0)
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
        quantities[lot.finished_detail.product_id] += max(lot.quantity_available + lot.quantity_reserved
                                                        + lot.quantity_consumed - consumed[lot.id], 0)
    for reserve in db.scalars(select(InventoryReservation).where(InventoryReservation.order_item_id == item.id,
        InventoryReservation.reservation_type == "finished_order", InventoryReservation.status != "cancelled")):
        if reserve.inventory_lot_id in own_ids:
            continue
        lot = db.get(InventoryLot, reserve.inventory_lot_id)
        if lot and lot.finished_detail:
            quantities[lot.finished_detail.product_id] += max(int(reserve.credited_requirement_quantity or 0)
                                                            - reserve.released_requirement_quantity, 0)
    picking = plan_bom(compiled.graph, item.quantity).picking
    covered = min(item.quantity, *(quantities[pid] * item.quantity // qty for pid, qty in picking))
    status = COMPLETED if covered >= item.quantity else PENDING if own_ids else WAITING_MATERIAL
    from app.services.receipt_purpose_distribution import _active_order_item_allocations
    material_received = sum(a.receipt_order_purpose_sheet_qty for a in _active_order_item_allocations(db, item.id))
    semi_consumed = int(db.scalar(select(func.coalesce(func.sum(InventoryReservation.consumed_stock_quantity), 0))
        .where(InventoryReservation.order_item_id == item.id, InventoryReservation.reservation_type == "semi_order",
               InventoryReservation.status != "cancelled")) or 0)
    values = {"status": status, "planned_quantity": item.quantity if status != WAITING_MATERIAL else 0,
              "finished_coverage_snapshot": covered, "ordered_quantity_snapshot": item.quantity,
              "material_received_quantity": material_received,
              "material_input_quantity": material_received + semi_consumed,
              "readiness_basis": "automatic_receipt" if material_received or own_ids else None}
    if any(getattr(task, key) != value for key, value in values.items()):
        for key, value in values.items():
            setattr(task, key, value)
        task.version += 1
    db.flush()
    return task
