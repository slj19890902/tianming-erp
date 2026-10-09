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
        from app.services.processed_component_stock import available_outputs, reserve_output
        from app.services.finished_stock_identity import compiled_product_bases
        completed_child_ids = {e.child_id for e in requirements.compiled.graph.edges if e.relation=='assembly'}
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
                from app.services.shared_finished_stock import match as shared_match
                from app.services.finished_stock_identity import compiled_product_bases
                approved=shared_match(db,lot,product_id=pid,customer_id=requirements.compiled.graph.customer_id,
                    expected_basis=compiled_product_bases(requirements.compiled)[pid])
                if detail.is_general or (detail.owner_customer_id != requirements.compiled.graph.customer_id and not approved):
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
            if need and pid in completed_child_ids:
                for lot in available_outputs(db, product_id=pid,
                        customer_id=requirements.compiled.graph.customer_id,
                        expected_basis=compiled_product_bases(requirements.compiled)[pid]):
                    take=min(need,lot.quantity_available)
                    reservation=reserve_output(db,compiled=requirements.compiled,order_item_id=order_item_id,
                        snapshot_id=snapshots[pid],lot=lot,quantity=take,expected_version=lot.version,
                        operator_id=operator_id,key=f'processed-bom-auto:{order_item_id}:{snapshots[pid]}:{lot.id}')
                    reserved.append(reservation.id);need-=take
                    if not need:break
        from app.services.bom_inventory_contract import body_product_ids
        for pid in body_product_ids(requirements.compiled.graph):
            requirements = read_graph_requirements(db, order_item_id)
            row = next(p for p in requirements.plan.products if p.product_id == pid)
            need = row.make_units - row.body_credited_units
            if need <= 0:
                continue
            for lot in finished_inventory_candidates_for_bom_component(db, order_item_id=order_item_id,
                    bom_snapshot_id=snapshots[pid], stock_stage='body'):
                if lot.finished_detail.is_general or lot.finished_detail.owner_customer_id != requirements.compiled.graph.customer_id:
                    continue
                take = min(need, lot.quantity_available)
                reservation = reserve_finished_inventory_for_bom_component(db, order_item_id=order_item_id,
                    bom_snapshot_id=snapshots[pid], inventory_lot_id=lot.id, quantity=take,
                    expected_version=lot.version, operator_id=operator_id, stock_stage='body',
                    idempotency_key=f'bom-body-auto:{order_item_id}:{snapshots[pid]}:{lot.id}',
                    warning_acknowledged_codes=[])
                reserved.append(reservation.id)
                need -= take
                if not need:
                    break
        return reserved
