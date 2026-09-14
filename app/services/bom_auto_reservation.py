"""Reserve exact customer-owned finished BOM stock; never assert assembly."""
from app.services.bom_transactions import atomic_bom
from app.services.multilevel_bom_requirements import read_graph_requirements
from app.services.warehouse_inventory import (
    finished_inventory_candidates_for_bom_component,
    reserve_finished_inventory_for_bom_component,
)


def reserve_new_order_stock(db, *, order_item_id, operator_id):
    with atomic_bom(db):
        requirements = read_graph_requirements(db, order_item_id)
        if requirements is None:
            return []
        _, _, topology = requirements.compiled.graph.validated()
        snapshots = {s.component_product_id: s.id for s in requirements.compiled.snapshots}
        reserved = []
        for pid in topology:
            # Parent stock reduces assembly-child demand, never accompanying goods.
            requirements = read_graph_requirements(db, order_item_id)
            need = next(p.make_units for p in requirements.plan.products if p.product_id == pid)
            if not need:
                continue
            for lot in finished_inventory_candidates_for_bom_component(
                    db, order_item_id=order_item_id, bom_snapshot_id=snapshots[pid]):
                detail = lot.finished_detail
                if detail.is_general or detail.owner_customer_id != requirements.compiled.graph.customer_id:
                    continue
                from app.services.bom_subkits import active_subkit_order
                if active_subkit_order(db, lot) is not None:
                    continue
                take = min(need, lot.quantity_available)
                reservation = reserve_finished_inventory_for_bom_component(db,
                    order_item_id=order_item_id, bom_snapshot_id=snapshots[pid],
                    inventory_lot_id=lot.id, quantity=take, expected_version=lot.version,
                    operator_id=operator_id,
                    idempotency_key=f"bom-auto:{order_item_id}:{snapshots[pid]}:{lot.id}",
                    warning_acknowledged_codes=[])
                reserved.append(reservation.id)
                need -= take
                if not need:
                    break
        return reserved
