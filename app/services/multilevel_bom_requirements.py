"""Read real graph procurement demand using authoritative reserved stock.

Free warehouse stock is never silently credited. Finished units propagate
through assembly edges; physical semi stock stays scoped to its own route.
"""
from dataclasses import dataclass

from sqlalchemy import select, or_, and_

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
    demands = {d.component_product_id: d for d in effective_component_demands(db, order_item_id)}
    multipliers = {d.product_id: d.required_units for d in plan_bom(graph, 1).products}
    if any(d.effective_sets != item.quantity or d.required_piece_quantity != item.quantity * multipliers[pid]
           for pid, d in demands.items()):
        raise BomPlanError("多级BOM数量调整必须保持冻结组套关系，请先核对订单数量")
    snapshots = {s.component_product_id: s for s in compiled.snapshots}
    from app.models.production import ProductionCompletion
    from app.models.multilevel_bom import BomAssembly
    own_completion_ids = select(ProductionCompletion.id).where(ProductionCompletion.order_item_id == item.id)
    own_assembly_ids = select(BomAssembly.id).where(BomAssembly.order_item_id == item.id)
    own_lots = list(db.scalars(select(InventoryLot).where(or_(
        and_(InventoryLot.source_ref_type == "production_completion", InventoryLot.source_ref_id.in_(own_completion_ids)),
        and_(InventoryLot.source_ref_type == "bom_assembly", InventoryLot.source_ref_id.in_(own_assembly_ids))))))
    own_ids = {lot.id for lot in own_lots}
    own_root_used = sum(lot.quantity_consumed for lot in own_lots
                       if lot.finished_detail and lot.finished_detail.product_id == graph.root_id)
    reserves = list(db.scalars(select(InventoryReservation).where(
        InventoryReservation.order_item_id == item.id, InventoryReservation.reservation_type == "finished_order",
        InventoryReservation.status != "cancelled")))
    # This order's newly manufactured outputs are already represented by its
    # purchased material. Crediting them AND subtracting existing purchases
    # would hide the unreported balance on a partially procured order.
    reserves = [r for r in reserves if r.inventory_lot_id not in own_ids]
    finished, pieces = {}, {}
    for node in graph.nodes:
        row = snapshots[node.product_id]
        if node.product_id == graph.root_id:
            # Root delivery already appears in the canonical order coverage;
            # add only still-unconsumed root-component reservations, not their
            # historical consumed credit a second time.
            root_reservations = [r for r in reserves if r.sales_order_item_bom_component_id in (None, row.id)]
            finished[node.product_id] = max(int(item.delivered_quantity or 0) - own_root_used, 0) + sum(
                max(int(r.credited_requirement_quantity or 0) - int(r.consumed_requirement_quantity or 0)
                    - int(r.released_requirement_quantity or 0), 0) for r in root_reservations)
        else:
            finished[node.product_id] = sum(max(int(r.credited_requirement_quantity or 0)
                - int(r.released_requirement_quantity or 0), 0) for r in reserves
                if r.sales_order_item_bom_component_id == row.id)
        for route in node.routes:
            coverage = component_inventory_coverage(db, row.id, component_type=route.key)
            pieces[node.product_id, route.key] = coverage["semi_piece_quantity"]
    return GraphRequirements(item.quantity, compiled, plan_bom(graph, item.quantity,
        eligible_stock=finished, eligible_pieces=pieces), finished, pieces)
