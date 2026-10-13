"""Read-only per-child stock plan using the saved order's identity and BOM rules."""
from app.models.order import OrderItem
from app.models.product import Product
from app.services.multilevel_bom_compile import compile_master_order_bom
from app.services.multilevel_bom_plan import plan_bom
from app.services.finished_stock_identity import compiled_product_bases
from app.services.warehouse_inventory import finished_inventory_candidates_for_identity, finished_inventory_candidates_for_product
from app.services.processed_component_stock import available_outputs
from app.services.order_replenishment_supply import output_free_for_order
from app.services.bom_subkits import active_subkit_order
from app.services.shared_finished_stock import match as shared_match
from app.services.warehouse_location_address import employee_location_name


def preview(db, *, product, quantity, used_stock_by_lot, root_allocations=(), future_selected=None):
    future_selected = future_selected or {}
    proposed = OrderItem(id=0, product_id=product.id, quantity=quantity, combination_role='standalone')
    compiled = compile_master_order_bom(db, proposed)
    graph = compiled.graph
    bases = compiled_product_bases(compiled)
    nodes, _, topology = graph.validated()
    complete_children = {e.child_id for e in graph.edges if e.relation == 'assembly'}
    credits, components, root_lots = {}, [], []
    for pid in topology:
        demand = next(row for row in plan_bom(graph, quantity, eligible_stock=credits).products if row.product_id == pid)
        node = nodes[pid]
        lots = [] if node.source == 'separate' else finished_inventory_candidates_for_identity(
            db, product_id=pid, customer_id=graph.customer_id, expected=bases[pid])
        lots = [lot for lot in lots if not lot.finished_detail.is_general
            and (lot.finished_detail.owner_customer_id == graph.customer_id
                 or shared_match(db, lot, product_id=pid, customer_id=graph.customer_id, expected_basis=bases[pid]))
            and active_subkit_order(db, lot) is None]
        if pid == graph.root_id:
            root_lots = list(lots)

        if pid in complete_children:
            lots.extend(available_outputs(db, product_id=pid, customer_id=graph.customer_id, expected_basis=bases[pid]))
        remaining = demand.required_units
        positions = []
        by_id = {lot.id: lot for lot in lots}
        visits = ([(by_id[lid], amount) for lid, amount in root_allocations if lid in by_id]
            if pid == graph.root_id else []) + [(lot, None) for lot in lots]
        for lot, explicit_limit in visits:
            free = max(0, output_free_for_order(db, lot=lot, order_item_id=0) - used_stock_by_lot.get(lot.id, 0) - future_selected.get(lot.id, 0))
            take = min(remaining, free, explicit_limit if explicit_limit is not None else free)
            used_stock_by_lot[lot.id] = used_stock_by_lot.get(lot.id, 0) + take
            remaining -= take
            prior = next((row for row in positions if row['lot_id'] == lot.id), None)
            if prior is not None:
                prior['planned'] += take
                continue
            positions.append(dict(lot_id=lot.id, inventory_type=lot.inventory_type,
                location_id=lot.warehouse_location_id, location_label=employee_location_name(lot.location) if lot.location else '库位待核对',
                on_hand=int(lot.quantity_available + lot.quantity_reserved), reserved=int(lot.quantity_reserved),
                available=free, planned=take, unit=node.unit))
        credits[pid] = demand.required_units - remaining
        from app.services.finished_stock_identity import matches_component_stock_identity, stock_identity_error
        from app.services.bom_inventory_contract import is_body_lot
        review_lots = finished_inventory_candidates_for_product(db, customer_id=graph.customer_id,
            product_id=pid, include_reserved=True, include_held=True, include_ineligible=True)
        issues = [dict(lot_id=lot.id, location_id=lot.warehouse_location_id,
            location_label=employee_location_name(lot.location) if lot.location else '库位待核对',
            quantity_available=lot.quantity_available, reason=stock_identity_error(lot, bases[pid]))
            for lot in review_lots if not is_body_lot(lot) and not matches_component_stock_identity(lot, bases[pid])
            and not shared_match(db, lot, product_id=pid, customer_id=graph.customer_id, expected_basis=bases[pid])]
        master = db.get(Product, pid)
        components.append(dict(product_id=pid, product_code=master.product_code, product_name=node.name,
            unit=node.unit, required_quantity=demand.required_units, planned_quantity=credits[pid],
            shortage_quantity=remaining, positions=positions, identity_issues=issues, source=node.source))
    # Bodies reduce only their own material, never assembled-child demand.
    # Keep this second pass in the same order as reserve_new_order_stock.
    from app.services.bom_inventory_contract import body_product_ids
    bodies = {}
    for pid in body_product_ids(graph):
        demand = next(row for row in plan_bom(graph, quantity, eligible_stock=credits).products if row.product_id == pid)
        remaining = demand.make_units
        positions = []
        for lot in finished_inventory_candidates_for_identity(db, product_id=pid,
                customer_id=graph.customer_id, expected=bases[pid], stock_stage='body'):
            if lot.finished_detail.is_general or lot.finished_detail.owner_customer_id != graph.customer_id:
                continue
            free = max(0, output_free_for_order(db, lot=lot, order_item_id=0) - used_stock_by_lot.get(lot.id, 0) - future_selected.get(lot.id, 0))
            take = min(remaining, free)
            used_stock_by_lot[lot.id] = used_stock_by_lot.get(lot.id, 0) + take
            remaining -= take
            positions.append(dict(lot_id=lot.id, location_id=lot.warehouse_location_id,
                location_label=employee_location_name(lot.location) if lot.location else '库位待核对',
                available=free, planned=take, unit=nodes[pid].unit, stock_stage='body'))
        bodies[pid] = demand.make_units - remaining
        component = next(row for row in components if row['product_id'] == pid)
        component.update(body_planned_quantity=bodies[pid], body_positions=positions)
    final = plan_bom(graph, quantity, eligible_stock=credits, eligible_bodies=bodies)
    materials = [dict(product_id=row.product_id, product_code=db.get(Product, row.product_id).product_code,
        component_type=row.route_key, required_piece_quantity=row.required_pieces,
        semi_planned_requirement_quantity=row.credited_pieces,
        remaining_required_piece_quantity=row.required_pieces-row.credited_pieces,
        requisition_sheet_quantity=row.purchase_sheets, requisition_unit='张') for row in final.materials]
    root = next(row for row in final.products if row.product_id == graph.root_id)
    root_component = next(row for row in components if row['product_id'] == graph.root_id)
    return dict(order_quantity=quantity, coverage_state='full' if all(not r.make_units or nodes[r.product_id].source in ('assembled','separate') for r in final.products) else 'partial' if any(credits.values()) else 'none',
        interaction_state='ready', requisition_calculation_scope='bom',
        message='按子件分别抵扣；有货部分保存时预占，缺少部分正常报料。未加工片料仍须按用途采用。',
        finished_planned_quantity=root.credited_units, production_required_quantity=root.make_units,
        shortage_quantity=root.make_units, finished_stock_unit=nodes[graph.root_id].unit,
        finished_candidate_available_quantity=sum(r['available'] for r in root_component['positions']),
        finished_stock_on_hand_quantity=sum(r['on_hand'] for r in root_component['positions']),
        finished_stock_reserved_quantity=sum(r['reserved'] for r in root_component['positions']),
        finished_stock_available_quantity=sum(r['available'] for r in root_component['positions']),
        finished_stock_unavailable_quantity=0, finished_stock_line_allocated_quantity=root.credited_units,
        finished_stock_allocated_quantity=root.credited_units,
        finished_stock_remaining_quantity=sum(max(lot.quantity_available-used_stock_by_lot.get(lot.id,0),0) for lot in root_lots),
        required_piece_quantity=sum(row.required_pieces for row in final.materials),
        semi_planned_requirement_quantity=sum(row.credited_pieces for row in final.materials),
        remaining_required_piece_quantity=sum(row.required_pieces-row.credited_pieces for row in final.materials),
        requisition_sheet_quantity=sum(row.purchase_sheets for row in final.materials), requisition_unit='张',
        requisition_components=materials, bom_inventory_components=components), root_lots
