"""Read-only receipt display in real product units, never sums unlike nodes."""
from decimal import Decimal

from sqlalchemy import select

from app.models.multilevel_bom import BomAssembly
from app.models.production import ProductionCompletion, ProductionTask
from app.models.warehouse_inventory import InventoryReservation
from app.services.multilevel_bom_plan import plan_assembly
from app.services.multilevel_bom_requirements import read_graph_requirements


def project_graph_receipts(db, order_item_id, summary, states, semi_credits):
    requirements = read_graph_requirements(db, order_item_id)
    graph = requirements.compiled.graph
    snapshots = {s.component_product_id: s for s in requirements.compiled.snapshots}
    states = {s["component_key"]: s for s in states}
    received, planned, rows = {}, {}, []
    for node in graph.nodes:
        snapshot = snapshots[node.product_id]
        current_routes, planned_routes = [], []
        for route in node.routes:
            key = f"bom:{snapshot.id}:{route.key}"
            state = states.get(key, {})
            # Existing frozen purpose conversion is in procurement units.
            # Recover physical pieces before applying this real node's ratio.
            divisor = int(state.get("pieces_per_finished", 1))
            credit = semi_credits.get((order_item_id, key), 0)
            current = int((state.get("received_capacity", Decimal(0)) * divisor + credit) // route.pieces_per_unit)
            future = int((state.get("planned_capacity", Decimal(0)) * divisor + credit) // route.pieces_per_unit)
            current_routes.append(current)
            planned_routes.append(future)
            pending = max(int(state.get("planned_order_sheet_qty", 0)) - int(state.get("received_order_sheet_qty", 0)), 0)
            rows.append({"component_key": key, "component_label": state.get("component_label", snapshot.snapshot_component_product_name),
                "component_type": route.key, "product_id": node.product_id,
                "planned_order_sheet_qty": int(state.get("planned_order_sheet_qty", 0)),
                "received_order_sheet_qty": int(state.get("received_order_sheet_qty", 0)),
                "reserve_received_sheet_qty": int(state.get("reserve_received_sheet_qty", 0)),
                "semi_reserved_piece_qty": credit, "current_finished_capacity_qty": current,
                "planned_finished_capacity_qty": future, "remaining_order_sheet_qty": pending,
                "over_received_order_sheet_qty": max(-int(state.get("planned_order_sheet_qty", 0)) + int(state.get("received_order_sheet_qty", 0)), 0),
                "waiting_for_pairing": current < future and pending > 0})
        stock = requirements.finished_units.get(node.product_id, 0)
        received[node.product_id] = stock + min(current_routes, default=0)
        planned[node.product_id] = stock + min(planned_routes, default=0)
    # Calculate potential assembly without writing inventory or creating facts.
    for balances in (received, planned):
        result = plan_assembly(graph, requirements.order_quantity, eligible_stock=balances)
        for step in result.steps:
            balances[step.product_id] += step.produced_units
    outputs = {node.product_id: 0 for node in graph.nodes}
    root_reserved = 0
    for completion in db.scalars(select(ProductionCompletion).where(
        ProductionCompletion.order_item_id == order_item_id, ProductionCompletion.status == "posted")):
        task = db.get(ProductionTask, completion.task_id)
        sid = task.sales_order_item_bom_component_id
        pid = graph.root_id if sid is None else next(s.component_product_id for s in snapshots.values() if s.id == sid)
        outputs[pid] += int(completion.actual_output_quantity or 0)
        if pid == graph.root_id:
            root_reserved += int(completion.order_reserved_quantity or 0)
    for assembly in db.scalars(select(BomAssembly).where(
        BomAssembly.order_item_id == order_item_id, BomAssembly.status == "posted")):
        outputs[assembly.output_product_id] += assembly.quantity
        if assembly.output_product_id == graph.root_id:
            reservation = db.scalar(select(InventoryReservation).where(
                InventoryReservation.idempotency_key == f"bom-output-reserve:{assembly.id}",
                InventoryReservation.order_item_id == order_item_id,
                InventoryReservation.inventory_lot_id == assembly.output_lot_id))
            if reservation is not None:
                root_reserved += int(reservation.reserved_stock_quantity)
    output = outputs[graph.root_id]
    capacity = max(received[graph.root_id] - requirements.finished_units.get(graph.root_id, 0), 0)
    future = max(planned[graph.root_id] - requirements.finished_units.get(graph.root_id, 0), 0)
    summary.update(automatic_finished_output_qty=output,
        automatic_order_reserved_quantity=root_reserved,
        automatic_surplus_finished_quantity=max(output-root_reserved, 0),
        current_theoretical_finished_capacity_qty=capacity,
        currently_unposted_finished_capacity_qty=max(capacity-output, 0),
        future_planned_finished_capacity_qty=max(future-output, 0),
        remaining_order_purpose_sheet_qty=sum(r["remaining_order_sheet_qty"] for r in rows),
        waiting_component_labels=[r["component_label"] for r in rows if r["waiting_for_pairing"]],
        waiting_component_gap_quantity=max((r["planned_finished_capacity_qty"]-r["current_finished_capacity_qty"] for r in rows), default=0),
        component_progress=rows, product_output_quantities=outputs,
        projection_inconsistent=bool(summary["projection_inconsistent"] or output != capacity))
