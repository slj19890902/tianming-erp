"""Read-only demand projection. Never reserves stock or changes product facts."""
from collections import defaultdict
from sqlalchemy import select
from app.models.stock_replenishment import InventoryStockPolicy
from app.services.delivery_quantities import (
    product_basis, require_physical_stock, physical_for,
    available_customer_quantity, QuantityContractError,
)
from app.services.warehouse_inventory import finished_inventory_candidates_for_product
from app.services.warehouse_location_address import location_address_payload
from app.services.location_candidates import load_warehouse_location_projection_contexts, warehouse_location_projection
from app.models.multilevel_bom import ProductBomProfile


def demand_stock_preview(db, customer_id, items, products):
    used = defaultdict(int)
    deficits = defaultdict(int)
    facts = {}
    rows = []
    for item, product in zip(items, products, strict=True):
        row = dict(client_line_id=item.client_line_id, product_id=product.id,
                   product_code=product.product_code, quantity=item.quantity,
                   customer_category=product.customer_category, locations=[])
        try:
            profile = db.get(ProductBomProfile, product.id)
            if product.is_virtual_composite_parent or (profile and profile.source == 'separate'):
                raise QuantityContractError('组件分别存放的组合产品须按配方核对组件库存，不能按父件库存为零计算缺货')
            basis = product_basis(product)
            physical_for(basis, item.quantity)
            if product.id not in facts:
                physical = finished_inventory_candidates_for_product(
                    db, customer_id=customer_id, product_id=product.id,
                    include_reserved=True, include_held=True)
                eligible = finished_inventory_candidates_for_product(
                    db, customer_id=customer_id, product_id=product.id)
                verified = []
                excluded = 0
                for lot in physical:
                    try:
                        require_physical_stock(lot, basis)
                        verified.append(lot)
                    except QuantityContractError:
                        excluded += 1
                ids = {lot.id for lot in verified}
                eligible = [lot for lot in eligible if lot.id in ids]
                policies = db.scalars(select(InventoryStockPolicy).where(
                    InventoryStockPolicy.product_id == product.id,
                    InventoryStockPolicy.active.is_(True),
                    InventoryStockPolicy.target_inventory_type == 'finished')).all()
                facts[product.id] = (verified, eligible, excluded, policies)
            physical, eligible, excluded, policies = facts[product.id]
            on_hand = sum(max(l.quantity_available, 0) + max(l.quantity_reserved, 0) for l in physical)
            reserved = sum(max(l.quantity_reserved, 0) for l in physical)
            available = sum(max(l.quantity_available, 0) for l in eligible)
            free = sum(max(l.quantity_available - used[l.id], 0) for l in eligible)
            capacity = available_customer_quantity(basis, free)
            # Do not round fractional physical demand into an invented stock unit.
            take_customer = min(capacity, item.quantity)
            take_physical = physical_for(basis, take_customer)
            remaining = take_physical
            for lot in eligible:
                take = min(max(lot.quantity_available - used[lot.id], 0), remaining)
                used[lot.id] += take
                remaining -= take
            projected = capacity - item.quantity - deficits[product.id]
            deficits[product.id] += item.quantity - take_customer
            threshold = (available_customer_quantity(basis, policies[0].warning_quantity)
                         if len(policies) == 1 else None)
            status = 'shortage' if projected < 0 else 'empty' if projected == 0 else (
                'low' if threshold is not None and projected < threshold else 'enough')
            row.update(unit=basis['customer_unit'], physical_unit=basis['physical_unit'],
                       on_hand=on_hand, reserved=reserved, available=available,
                       available_customer=available_customer_quantity(basis, available),
                       projected_remaining=projected, shortage=max(-projected, 0),
                       warning_quantity=threshold, status=status,
                       excluded_unverified_lots=excluded,
                       message='历史单位未核实的批次未计入' if excluded else '')
        except QuantityContractError as exc:
            row.update(status='review', message=str(exc))
        rows.append(row)
    # Final batch projection shares every physical lot across all draft lines.
    all_locations = {lot.warehouse_location_id: lot.location for physical, _, _, _ in facts.values()
                     for lot in physical if lot.location}
    contexts = load_warehouse_location_projection_contexts(db, all_locations.values())
    product_map = {p.id: p for p in products}
    location_rows = {}
    for row in rows:
        if row.get('status') == 'review':
            continue
        product = product_map[row['product_id']]
        basis = product_basis(product)
        eligible = facts[product.id][1]
        row['batch_remaining'] = available_customer_quantity(basis, sum(
            max(l.quantity_available - used[l.id], 0) for l in eligible)) - deficits[product.id]
        balance = row['batch_remaining']
        threshold = row['warning_quantity']
        row['status'] = 'shortage' if balance < 0 else 'empty' if balance == 0 else (
            'low' if threshold is not None and balance < threshold else 'enough')
        row['shortage'] = max(-balance, 0)
        if product.id not in location_rows:
            location_rows[product.id] = []
            eligible_ids = {lot.id for lot in eligible}
            for lot in facts[product.id][0]:
                context = contexts.get(lot.warehouse_location_id, {})
                address = location_address_payload(lot.location, **{
                    key: context.get(key) for key in ('area', 'floor', 'area_sequence')}) if lot.location else {}
                if lot.location:
                    address.update(warehouse_location_projection(lot.location, **context))
                location_rows[product.id].append(dict(
                    lot_id=lot.id, lot_number=lot.lot_number,
                    location_id=lot.warehouse_location_id, **address,
                    location_name=(address.get('current_address_name') or
                                   address.get('current_address_code') or '位置待确认'),
                    on_hand=max(lot.quantity_available, 0) + max(lot.quantity_reserved, 0),
                    reserved=lot.quantity_reserved,
                    available=max(lot.quantity_available, 0) if lot.id in eligible_ids else 0))
        row['locations'] = location_rows[product.id]
    return dict(items=rows, read_only=True,
                note='预计结余按本批需求顺序试算，不预占、不扣库；现存/占用/可用为实物单位，结余为客户单位。正式保存仍需核对库存。')
