"""Read real graph procurement demand using authoritative reserved stock.

Free warehouse stock is never silently credited. Finished units propagate
through assembly edges; physical semi stock stays scoped to its own route.
"""
from dataclasses import dataclass

from sqlalchemy import select

from app.models.order import OrderItem
from app.models.warehouse_inventory import InventoryReservation, InventoryLot
from app.services.composite_bom_workflow import effective_component_demands
from app.services.multilevel_bom_compile import CompiledMasterBom
from app.services.multilevel_bom_orders import read_compiled_order_bom
from app.services.multilevel_bom_plan import BomPlan, BomPlanError, plan_bom
from app.services.warehouse_inventory import component_inventory_coverage


@dataclass(frozen=True)
class GraphRequirements:
    order_quantity: int
    compiled: CompiledMasterBom
    plan: BomPlan
    finished_units: dict[int, int]
    physical_credits: dict[tuple[int, str], int]

    def source(self, product_id):
        return next(n.source for n in self.compiled.graph.nodes if n.product_id == product_id)


def read_graph_requirements(db, order_item_id):
    compiled = read_compiled_order_bom(db, order_item_id)
    if compiled is None:
        return None
    item = db.get(OrderItem, order_item_id)
    graph = compiled.graph
    current_ids = {row.id for row in compiled.snapshots}
    demands = {d.component_product_id: d for d in effective_component_demands(db, order_item_id) if d.snapshot_id in current_ids}
    quantity = compiled.execution_window.execution_quantity if compiled.execution_window else item.quantity
    delivered = compiled.execution_window.delivered_since if compiled.execution_window else int(item.delivered_quantity or 0)
    multipliers = {d.product_id: d.required_units for d in plan_bom(graph, 1).products}
    if any(d.effective_sets != quantity or d.required_piece_quantity != quantity * multipliers[pid]
           for pid, d in demands.items()):
        raise BomPlanError("多级BOM数量调整必须保持冻结组套关系，请先核对订单数量")
    snapshots = {s.component_product_id: s for s in compiled.snapshots}
    from app.services.multilevel_bom_receipts import own_output_lots
    own_lots = own_output_lots(db, item.id)
    from app.services.multilevel_bom_execution_boundary import handoff_assembly_ids
    handoffs = handoff_assembly_ids(db, compiled)
    own_lots = [lot for lot in own_lots if not (lot.source_ref_type == "bom_assembly" and lot.source_ref_id in handoffs)]
    own_ids = {lot.id for lot in own_lots}
    own_root_used = sum(lot.quantity_consumed for lot in own_lots
                       if lot.finished_detail and lot.finished_detail.product_id == graph.root_id)
    from app.services.multilevel_bom_output_history import current_finished_reservation_condition
    reserves = list(db.scalars(select(InventoryReservation).where(
        InventoryReservation.order_item_id == item.id, InventoryReservation.reservation_type == "finished_order",
        current_finished_reservation_condition(db, compiled),
        InventoryReservation.status != "cancelled")))
    # This order's newly manufactured outputs are already represented by its
    # purchased material. Crediting them AND subtracting existing purchases
    # would hide the unreported balance on a partially procured order.
    reserves = [r for r in reserves if r.inventory_lot_id not in own_ids]
    finished, pieces = {}, {}
    for node in graph.nodes:
        row = snapshots[node.product_id]
        if node.source == "separate":
            if any(r.sales_order_item_bom_component_id == row.id
                   or (node.product_id == graph.root_id and r.sales_order_item_bom_component_id is None)
                   for r in reserves):
                raise BomPlanError("组合需求父件存在实体预占，请核对库存身份后再报料")
            finished[node.product_id] = 0
        elif node.product_id == graph.root_id:
            # Root delivery already appears in the canonical order coverage;
            # add only still-unconsumed root-component reservations, not their
            # historical consumed credit a second time.
            root_reservations = [r for r in reserves if r.sales_order_item_bom_component_id in (None, row.id)]
            finished[node.product_id] = max(delivered - own_root_used, 0) + sum(
                max(int(r.credited_requirement_quantity or 0) - int(r.consumed_requirement_quantity or 0)
                    - int(r.released_requirement_quantity or 0), 0) for r in root_reservations)
        else:
            finished[node.product_id] = sum(max(int(r.credited_requirement_quantity or 0)
                - int(r.released_requirement_quantity or 0), 0) for r in reserves
                if r.sales_order_item_bom_component_id == row.id)
        for route in node.routes:
            coverage = component_inventory_coverage(db, row.id, component_type=route.key)
            pieces[node.product_id, route.key] = coverage["semi_piece_quantity"]
    return GraphRequirements(quantity, compiled, plan_bom(graph, quantity,
        eligible_stock=finished, eligible_pieces=pieces), finished, pieces)
