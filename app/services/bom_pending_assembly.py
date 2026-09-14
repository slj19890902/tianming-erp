"""Read-only ready-to-insert plans and explicit physical assembly confirmation."""
from sqlalchemy import select
from app.models.order import Order, OrderItem
from app.models.customer import Customer
from app.models.multilevel_bom import OrderBomGraph
from app.models.warehouse_inventory import InventoryLot, InventoryReservation, WarehouseLocation
from app.services.multilevel_bom_orders import read_compiled_order_bom
from app.services.multilevel_bom_receipts import own_output_lots, assemble_graph_order_receipt
from app.services.multilevel_bom_inventory import assemble_order_inventory
from app.services.bom_subkits import SubkitError
from app.services.bom_transactions import atomic_bom
from app.services.order_status_policy import ORDER_ITEM_ACTIVE_ORDER_STATUSES


def reserve_pending_parts(db, *, order_item_id, compiled, operator_id):
    """Keep cut parts with their order while waiting; excess remains free."""
    from app.services.multilevel_bom_requirements import read_graph_requirements
    from app.services.production_workflow import _reserve_component_completion_lot
    requirements = read_graph_requirements(db, order_item_id)
    child_ids = {e.child_id for e in compiled.graph.edges if e.relation == 'assembly'}
    own = {lot.id: lot for lot in own_output_lots(db, order_item_id)}
    snapshots = {s.component_product_id: s for s in compiled.snapshots}
    needs = {p.product_id: p.make_units for p in requirements.plan.products if p.product_id in child_ids}
    for reservation in db.scalars(select(InventoryReservation).where(
            InventoryReservation.order_item_id == order_item_id,
            InventoryReservation.inventory_lot_id.in_(own),
            InventoryReservation.reservation_type == 'finished_order',
            InventoryReservation.status != 'cancelled')):
        lot = own[reservation.inventory_lot_id]
        if lot.finished_detail and lot.finished_detail.product_id in needs:
            needs[lot.finished_detail.product_id] -= max(0, reservation.credited_requirement_quantity - reservation.released_requirement_quantity)
    item = db.get(OrderItem, order_item_id)
    order = db.get(Order, item.order_id)
    for lot in sorted(own.values(), key=lambda l: l.id):
        if not lot.finished_detail or lot.status != 'active':
            continue
        pid = lot.finished_detail.product_id
        take = min(lot.quantity_available, max(0, needs.get(pid, 0)))
        if take:
            _reserve_component_completion_lot(db, completion=lot, order=order, item=item,
                snapshot_id=snapshots[pid].id, lot=lot, operator_id=operator_id,
                reserve_quantity=take, idempotency_key=f'bom-wait-assembly:{lot.id}:{lot.version}',
                reservation_number_prefix='BWAS', movement_reason='待组套子件预留')
            needs[pid] -= take


def source_selection(db, item_id, compiled):
    own = {lot.id for lot in own_output_lots(db, item_id)}
    from app.services.multilevel_bom_output_history import current_finished_reservation_condition
    reserved = set(db.scalars(select(InventoryReservation.inventory_lot_id).where(
        InventoryReservation.order_item_id == item_id,
        InventoryReservation.reservation_type == 'finished_order',
        current_finished_reservation_condition(db, compiled),
        InventoryReservation.status.in_(('active', 'partial')))))
    lots = list(db.scalars(select(InventoryLot).where(InventoryLot.id.in_(own | reserved),
        InventoryLot.status == 'active', InventoryLot.quantity_available + InventoryLot.quantity_reserved > 0)))
    return lots, sorted(own.intersection(lot.id for lot in lots))


def preview(db, item_id):
    item = db.get(OrderItem, item_id)
    if not item or item.is_force_closed or item.delivered_quantity >= item.quantity:
        raise SubkitError('订单明细已关闭或送完')
    order = db.get(Order, item.order_id)
    if order.status not in ORDER_ITEM_ACTIVE_ORDER_STATUSES:
        raise SubkitError('当前订单状态不可组套')
    compiled = read_compiled_order_bom(db, item_id)
    if compiled is None:
        raise SubkitError('订单没有冻结组合BOM')
    lots, free = source_selection(db, item_id, compiled)
    versions = {lot.id: lot.version for lot in lots}
    quantities = assemble_order_inventory(db, order_item_id=item_id,
        source_lot_versions=versions, target_locations={}, operation_key='read-only-preview',
        operator_id=None, available_lot_ids=free, preview_only=True)
    nodes = {n.product_id: n for n in compiled.graph.nodes}
    from app.services.production_workflow import _receipt_auto_finished_ground_target, ProductionWorkflowError
    outputs = []
    for pid, quantity in quantities.items():
        try:
            target = _receipt_auto_finished_ground_target(db, claim=False,
                customer_id=order.customer_id, product_id=pid).location
            target_id, target_name = target.id, target.location_name
        except ProductionWorkflowError:
            target_id, target_name = None, '请选择实际成套位置'
        outputs.append(dict(product_id=pid, product_name=nodes[pid].name,
            quantity=quantity, unit=nodes[pid].unit, location_id=target_id, location_name=target_name))
    sources = []
    for lot in lots:
        detail = lot.finished_detail
        location = db.get(WarehouseLocation, lot.warehouse_location_id)
        sources.append(dict(lot_id=lot.id, product_name=detail.product_name_snapshot if detail else '箱体',
            location=location.location_name if location else '位置待核对'))
    customer = db.get(Customer, order.customer_id)
    return dict(order_item_id=item.id, order_number=order.order_number, customer_order_number=order.customer_po,
        customer_id=order.customer_id, customer_name=customer.chinese_short_name or customer.name,
        outputs=outputs, sources=sources, source_lot_versions=versions, available_lot_ids=free,
        expected_outputs=quantities)


def pending(db, scope):
    query = (select(OrderItem.id).join(Order, Order.id == OrderItem.order_id)
        .join(OrderBomGraph, OrderBomGraph.order_item_id == OrderItem.id)
        .where(Order.status.in_(ORDER_ITEM_ACTIVE_ORDER_STATUSES), OrderItem.is_force_closed.is_(False),
               OrderItem.delivered_quantity < OrderItem.quantity).order_by(OrderItem.id.desc()))
    if scope is not None:
        query = query.where(Order.customer_id.in_(scope))
    rows = []
    for item_id in db.scalars(query):
        try:
            compiled = read_compiled_order_bom(db, item_id)
            if compiled is None:
                continue
            if not any(n.source == 'assembled' for n in compiled.graph.nodes) and not any(
                    e.relation == 'assembly' for e in compiled.graph.edges):
                continue
            row = preview(db, item_id)
        except (SubkitError, ValueError) as error:
            # Surface invalid plans, never silently hide a blocked order.
            rows.append(dict(order_item_id=item_id, error=str(error), outputs=[]))
            continue
        if any(o['quantity'] for o in row['outputs']):
            rows.append(row)
    return rows


def confirm(db, *, item_id, command, actor):
    with atomic_bom(db):
        compiled = read_compiled_order_bom(db, item_id)
        if compiled is None:
            raise SubkitError('订单没有冻结组合BOM')
        own = {lot.id for lot in own_output_lots(db, item_id)}
        reserved = set(db.scalars(select(InventoryReservation.inventory_lot_id).where(
            InventoryReservation.order_item_id == item_id,
            InventoryReservation.reservation_type == 'finished_order')))
        if (not set(command['available_lot_ids']).issubset(own)
                or not set(command['source_lot_versions']).issubset(own | reserved)):
            raise SubkitError('组套来源不属于当前订单，不能消耗其他货物')
        if not any(command['expected_outputs'].values()):
            raise SubkitError('没有可确认的成套数量')
        results = assemble_graph_order_receipt(db, compiled=compiled, order_item_id=item_id,
            operation_key=command['operation_key'], operator_id=actor.id, confirmed_command=command)
        from app.services.production_workflow import refresh_production_task, refresh_order_production_status
        item = db.get(OrderItem, item_id)
        refresh_production_task(db, item_id)
        refresh_order_production_status(db, item.order_id)
        from app.services.audit_log import append_audit_event
        append_audit_event(db, event_category='business', result='success', source='web',
            module_code='production', action_code='bom.confirm_assembly', resource='production', actor=actor,
            entity_type='order_item', entity_id=item_id,
            details=dict(operation_key=command['operation_key'], assembly_ids=[r.id for r in results],
                         physical_assembly_confirmed=True))
        return dict(assembly_ids=[r.id for r in results if r.quantity],
                    outputs=[dict(product_id=r.output_product_id, quantity=r.quantity) for r in results if r.quantity])
