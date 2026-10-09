"""Completed semi-stock pieces keep their physical type and a frozen 1:1 identity."""
import json
from sqlalchemy import select, update, or_
from app.core.time_contract import utc_now_naive
from app.models.warehouse_inventory import InventoryLot, InventoryReservation, WarehouseLocation
from app.models.warehouse_goods import WarehouseGoodsProfile
from app.models.order import Order, OrderItem
from app.services.finished_stock_identity import matches_stock_identity, compiled_product_bases
from app.services.warehouse_goods import goods_profile
from app.services.warehouse_inventory import WarehouseInventoryError, _balances, _movement, _number, inventory_fifo_order_columns
from app.services.bom_transactions import atomic_bom


def output_identity(db, lot):
    """Return only explicit complete child-product identity; unknown old stock stays unknown."""
    if lot is None or lot.inventory_type != 'semi_finished' or lot.finished_detail is not None:
        return None
    detail = lot.semi_finished_detail
    profile = goods_profile(db, lot)
    if (not detail or not profile or profile.get('output_piece') is not True
            or profile.get('processing') not in {'cut','die_cut','printed'}
            or profile.get('remaining_processes') not in (None, [])
            or profile.get('scope') != 'customers' or detail.component_type != 'whole'
            or detail.stock_yield_per_sheet != 1 or detail.pieces_per_box != 1
            or profile.get('customer_ids') != [detail.owner_customer_id]
            or len(profile.get('product_ids', [])) != 1):
        return None
    try:
        basis = json.loads(profile.get('physical_basis') or '{}')
        pid = profile['product_ids'][0]
        if (type(pid) is not int or pid <= 0 or basis.get('schema') != 1
                or basis.get('product_id') != pid or not basis.get('unit')
                or basis.get('assembly') != []):
            return None
        return pid, detail.owner_customer_id, profile['physical_basis']
    except (TypeError, ValueError, KeyError):
        return None


def matches_output(db, lot, *, product_id, customer_id, expected_basis):
    identity = output_identity(db, lot)
    if identity and expected_basis and identity[:2] == (product_id, customer_id) and matches_stock_identity(identity[2], expected_basis):
        return True
    from app.services.shared_finished_stock import match
    return bool(identity and expected_basis and match(db,lot,product_id=product_id,
        customer_id=customer_id,expected_basis=expected_basis))


def eligible_output(db, lot, *, product_id, customer_id, expected_basis):
    """Shared physical/free-stock eligibility, also usable on fully reserved lots."""
    from app.services.fixed_shelf_staging import staging_owner
    from app.services.bom_subkits import active_subkit_order
    location = db.get(WarehouseLocation, lot.warehouse_location_id) if lot else None
    return bool(matches_output(db,lot,product_id=product_id,customer_id=customer_id,
        expected_basis=expected_basis) and lot.status=='active' and location and location.is_active
        and (location.source_version!='V11' or location.warehouse_floor==3)
        and staging_owner(db,lot.id) is None and active_subkit_order(db,lot) is None)


def available_outputs(db, *, product_id, customer_id, expected_basis):
    lots = db.scalars(select(InventoryLot).join(WarehouseGoodsProfile, WarehouseGoodsProfile.lot_id==InventoryLot.id)
        .join(WarehouseLocation, WarehouseLocation.id==InventoryLot.warehouse_location_id)
        .where(InventoryLot.inventory_type=='semi_finished', InventoryLot.status=='active',
            InventoryLot.quantity_available > 0, WarehouseLocation.is_active.is_(True),
            or_(WarehouseLocation.source_version.is_(None), WarehouseLocation.source_version!='V11',
                WarehouseLocation.warehouse_floor==3))
        .order_by(*inventory_fifo_order_columns())).all()
    return [lot for lot in lots if eligible_output(db,lot,product_id=product_id,customer_id=customer_id,
        expected_basis=expected_basis)]


def processed_reservations(db, compiled, order_item_id):
    """This execution's completed child credit, distinct from raw route material."""
    bases = compiled_product_bases(compiled)
    snapshots = {s.id:s.component_product_id for s in compiled.snapshots}
    child_ids = {e.child_id for e in compiled.graph.edges if e.relation=='assembly'}
    rows = db.scalars(select(InventoryReservation).where(InventoryReservation.order_item_id==order_item_id,
        InventoryReservation.reservation_type=='semi_order', InventoryReservation.semi_requirement_id.is_(None),
        InventoryReservation.sales_order_item_bom_component_id.in_(snapshots),
        InventoryReservation.status!='cancelled')).all()
    valid=[]
    for row in rows:
        pid=snapshots[row.sales_order_item_bom_component_id]
        lot=db.get(InventoryLot,row.inventory_lot_id)
        if (pid not in child_ids or row.yield_factor!=1 or row.requirement_quantity_denominator!=1
                or row.credited_requirement_quantity!=row.reserved_stock_quantity
                or not (matches_output(db,lot,product_id=pid,customer_id=compiled.graph.customer_id,
                    expected_basis=bases[pid]) or _reserved_output(db,row,lot,pid,compiled.graph.customer_id,bases[pid]))):
            raise WarehouseInventoryError('已加工子件预占的冻结身份或1:1数量关系不一致',409)
        valid.append(row)
    return valid


def reserve_output(db, *, compiled, order_item_id, snapshot_id, lot, quantity, expected_version, operator_id, key):
    with atomic_bom(db):
        item=db.get(OrderItem,order_item_id);order=db.get(Order,item.order_id)
        snapshot=next((s for s in compiled.snapshots if s.id==snapshot_id),None)
        child_ids={e.child_id for e in compiled.graph.edges if e.relation=='assembly'}
        if (not snapshot or snapshot.component_product_id not in child_ids
                or order.customer_id!=compiled.graph.customer_id or type(quantity) is not int or quantity<=0):
            raise WarehouseInventoryError('已加工子件预占缺少有效订单冻结组件',409)
        previous=db.scalar(select(InventoryReservation).where(InventoryReservation.idempotency_key==key))
        if previous:
            if (previous.order_item_id!=item.id or previous.inventory_lot_id!=lot.id
                    or previous.sales_order_item_bom_component_id!=snapshot_id
                    or previous.reserved_stock_quantity!=quantity or previous.reservation_type!='semi_order'):
                raise WarehouseInventoryError('子件预占标识已用于不同载荷',409)
            return previous
        from app.services.production_workflow import lock_order_rows_for_production_transition
        lock_order_rows_for_production_transition(db,[order.id])
        if item.is_force_closed or order.status in {'dead','completed','archived','delivered','cancelled','closed'}:
            raise WarehouseInventoryError('已结束订单不能继续预占子件',409)
        eligible_ids={candidate.id for candidate in available_outputs(db,product_id=snapshot.component_product_id,
            customer_id=order.customer_id,expected_basis=compiled_product_bases(compiled)[snapshot.component_product_id])}
        if (lot.version!=expected_version or lot.status!='active' or lot.quantity_available<quantity
                or lot.id not in eligible_ids
                or not matches_output(db,lot,product_id=snapshot.component_product_id,
                    customer_id=order.customer_id,expected_basis=compiled_product_bases(compiled)[snapshot.component_product_id])):
            raise WarehouseInventoryError('已加工子件身份、数量或版本已变化',409)
        # Validate inherited material cost before claiming any physical pieces.
        from app.services.bom_subkit_costs import source_cost
        source_cost(db,lot,quantity)
        before=_balances(lot);now=utc_now_naive()
        changed=db.execute(update(InventoryLot).where(InventoryLot.id==lot.id,InventoryLot.version==expected_version,
            InventoryLot.status=='active',InventoryLot.quantity_available>=quantity).values(
            quantity_available=InventoryLot.quantity_available-quantity,quantity_reserved=InventoryLot.quantity_reserved+quantity,
            version=InventoryLot.version+1,last_movement_at=now))
        if changed.rowcount!=1:raise WarehouseInventoryError('子件库存已被其他操作使用',409)
        reservation=InventoryReservation(reservation_number=_number('PCS'), inventory_lot_id=lot.id,
            reservation_type='semi_order',order_id=order.id,order_item_id=item.id,
            sales_order_item_bom_component_id=snapshot_id, reserved_stock_quantity=quantity,
            credited_requirement_quantity=quantity,yield_factor=1, reserved_by=operator_id,reserved_at=now,
            reservation_group_key=key,reservation_group_requested_quantity=quantity,idempotency_key=key,
            warning_codes='["PROCESSED_COMPONENT_OUTPUT"]',status='active')
        db.add(reservation);db.flush();db.refresh(lot)
        from app.services.shared_finished_stock import match,freeze_reservation
        member=match(db,lot,product_id=snapshot.component_product_id,customer_id=order.customer_id,
            expected_basis=compiled_product_bases(compiled)[snapshot.component_product_id],lock=True)
        freeze_reservation(db,reservation,member,lot)
        _movement(db,lot=lot,movement_type='reserve',quantity=quantity,before=before,operator_id=operator_id,
            reason='已加工子件按冻结BOM预占',idempotency_key=key,reservation_id=reservation.id,
            related_order_id=order.id,related_order_item_id=item.id)
        from app.services.audit_log import append_audit_event
        from app.models.user import User
        append_audit_event(db,event_category='business',result='success',source='system',module_code='warehouse',
            action_code='processed_component.reserve',resource='inventory_reservation',actor=db.get(User,operator_id),
            entity_type='inventory_reservation',entity_id=reservation.id,customer_id=order.customer_id,
            details={'order_item_id':item.id,'snapshot_id':snapshot_id,'lot_id':lot.id,'quantity':quantity,'yield_factor':1})
        return reservation


def _reserved_output(db,reservation,lot,pid,customer_id,basis):
    from app.services.shared_bom_stock import reservation_matches
    return reservation_matches(db,reservation,lot,pid,customer_id,basis)
