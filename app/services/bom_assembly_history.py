"""Read actual assembly events without inventing production completions."""
from collections import defaultdict
from sqlalchemy import select
from app.models.multilevel_bom import BomAssembly, BomAssemblyInput
from app.models.order import Order, OrderItem
from app.models.customer import Customer
from app.models.user import User
from app.models.warehouse_inventory import InventoryLot, WarehouseLocation
from app.core.time_contract import utc_naive_to_api, utc_naive_to_beijing_date
from app.services.location_candidates import load_warehouse_location_projection_contexts, warehouse_location_projection
from app.services.warehouse_location_address import employee_location_name


def history(db, scope, **filters):
    query = (select(BomAssembly, Order, Customer).join(OrderItem, OrderItem.id == BomAssembly.order_item_id)
             .join(Order, Order.id == OrderItem.order_id).join(Customer, Customer.id == Order.customer_id)
             .where(BomAssembly.quantity > 0))
    if scope is not None:
        query = query.where(Order.customer_id.in_(scope))
    if filters.get('customer_id'):
        query = query.where(Order.customer_id == filters['customer_id'])
    assemblies = list(db.execute(query))
    users = {u.id: u.real_name for u in db.scalars(select(User))}
    ids = [a.id for a, _, _ in assemblies]
    inputs = defaultdict(list)
    for entry in db.scalars(select(BomAssemblyInput).where(BomAssemblyInput.conversion_id.in_(ids))):
        inputs[entry.conversion_id].append(entry)
    consumed = defaultdict(int)
    result = []
    for a, order, customer in assemblies:
        lot = db.get(InventoryLot, a.output_lot_id)
        detail = lot.finished_detail if lot else None
        if not detail:
            continue
        day = utc_naive_to_beijing_date(a.created_at)
        if filters.get('status') and filters['status'] != a.status:
            continue
        if filters.get('completed_date_from') and day < filters['completed_date_from']:
            continue
        if filters.get('completed_date_to') and day > filters['completed_date_to']:
            continue
        terms = {'order_keyword': f'{order.order_number} {order.customer_po or ""}', 'product_code': detail.inventory_code_snapshot,
                 'product_name': detail.product_name_snapshot}
        if any(str(filters.get(k) or '').strip().casefold() not in str(v or '').casefold() for k, v in terms.items()):
            continue
        if a.status == 'posted':
            for entry in inputs[a.id]:
                consumed[entry.lot_id] += entry.quantity
        # Include split/moved descendants, preserving the original assembly identity.
        lots = list(db.scalars(select(InventoryLot).where(InventoryLot.source_ref_type == 'bom_assembly',
            InventoryLot.source_ref_id == a.id, InventoryLot.status == 'active')))
        current = [l for l in lots if l.quantity_available + l.quantity_reserved + l.quantity_damaged > 0]
        locations = []
        issues = []
        for l in current:
            loc = db.get(WarehouseLocation, l.warehouse_location_id)
            if loc:
                context = load_warehouse_location_projection_contexts(db, [loc]).get(loc.id, {})
                projection = warehouse_location_projection(loc, **context)
                if projection.get('map_issue'):
                    issues.append(projection['map_issue'])
                name = employee_location_name(loc, area=context.get('area'), floor=context.get('floor'), area_sequence=context.get('area_sequence'))
                if name not in locations:
                    locations.append(name)
            else:
                issues.append('成套库存缺少正式货位')
        source_rows = []
        for entry in inputs[a.id]:
            source = db.get(InventoryLot, entry.lot_id)
            sd = source.finished_detail if source else None
            source_rows.append(dict(product_name=sd.product_name_snapshot if sd else '本体', quantity=entry.quantity,
                product_code=sd.inventory_code_snapshot if sd else '', lot_id=entry.lot_id,
                lot_number=source.lot_number if source else '',
                remaining_quantity=source.quantity_available + source.quantity_reserved if source else 0))
        result.append(dict(id=f'bom-assembly:{a.id}', bom_assembly_id=a.id, origin='bom_assembly', status=a.status,
            order_item_id=a.order_item_id, order_number=order.order_number,
            customer_order_number=order.customer_po, customer_id=customer.id,
            customer_name=customer.name, customer_short_name=customer.chinese_short_name or customer.name,
            product_code=detail.inventory_code_snapshot, product_name=detail.product_name_snapshot,
            completed_at=utc_naive_to_api(a.created_at), actual_output_quantity=a.quantity,
            completed_by_name=users.get(a.created_by),
            planned_output_quantity=a.quantity, output_unit=lot.unit, assembly_inputs=source_rows,
            current_inventory_quantity=sum(l.quantity_available+l.quantity_reserved+l.quantity_damaged for l in current),
            current_inventory_status='located' if locations else 'drained',
            current_warehouse_location_map_issue=' / '.join(dict.fromkeys(issues)) or None,
            current_warehouse_location_name=' / '.join(locations),
            current_warehouse_location_id=current[0].warehouse_location_id if len(current)==1 else None,
            inventory_lot_id=current[0].id if len(current)==1 else None,
            can_revert=a.status=='posted', can_adjust_actual_quantity=False, can_transfer_to_stock=False,
            is_fully_delivered=False))
    return result, consumed
