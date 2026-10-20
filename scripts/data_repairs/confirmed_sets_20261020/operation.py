"""Exact, human-confirmed correction of two legacy inventory captures.

Not an application endpoint. The caller owns BEGIN IMMEDIATE, verified backup,
reviewed plan, active-admin attribution, and rollback/commit. No new stock or
assembly is created; full previous identity remains in adjustment/audit records.
"""
import hashlib
import json
from sqlalchemy import select, update
from app.models.order import OrderItem
from app.models.product import Product
from app.models.user import User
from app.models.warehouse_inventory import InventoryLot, InventoryMovement, InventoryReservation
from app.services.finished_stock_identity import product_basis, order_product_basis
from app.services.multilevel_bom_requirements import read_graph_requirements
from app.services.warehouse_inventory import _balances, _movement, reserve_finished_inventory_for_bom_component
from app.services.audit_log import append_audit_event
from app.services.bom_transactions import atomic_bom

KEY = 'confirmed-82021154-sets-20261020'
CONFIRMATION = '用户确认：两批都是配齐的成品套数，可按套交货'
LOT_IDS = (1148, 1220)
ITEM = 10902
PRODUCT = 3581
CUSTOMER = 137

def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str)

def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()

def row(obj):
    return {c.name: getattr(obj, c.name) for c in obj.__table__.columns}

def preview(db):
    item = db.get(OrderItem, ITEM); product = db.get(Product, PRODUCT)
    assert item and product and item.product_id == PRODUCT and item.order.customer_id == CUSTOMER
    assert item.order.order_number == 'TM20261020002' and item.quantity == 300 and item.delivered_quantity == 0
    assert item.requisition_status == '未报料' and item.material_status != 'received'
    assert product.product_code == '82021154' and product.customer_id == CUSTOMER
    basis = order_product_basis(db, ITEM, PRODUCT)
    assert basis and basis == product_basis(product)
    target = json.loads(basis)
    assert target['unit'] == '套' and [(x['product_id'], x['quantity']) for x in target['assembly']] == [(3884, 1), (3885, 1)]
    graph = read_graph_requirements(db, ITEM)
    assert all(p.make_units == 300 and p.credited_units == 0 for p in graph.plan.products)
    lots = []
    for lid, qty, location in [(1148, 3300, 2513), (1220, 2600, 2181)]:
        lot = db.get(InventoryLot, lid); detail = lot.finished_detail
        assert lot.status == 'active' and lot.inventory_type == 'finished' and lot.unit == 'boxes'
        assert lot.quantity_available == qty and lot.warehouse_location_id == location
        assert lot.quantity_reserved == lot.quantity_consumed == lot.quantity_damaged == lot.quantity_scrapped == 0
        assert detail.product_id == PRODUCT and detail.owner_customer_id == CUSTOMER and not detail.is_general
        old = json.loads(detail.physical_basis_json)
        assert old['unit'] == '只' and old['assembly'] == [] and old['product_id'] == PRODUCT
        assert lot.source_ref_type is None
        lots.append(dict(lot=row(lot), detail=row(detail)))
    assert not list(db.scalars(select(InventoryReservation).where(InventoryReservation.order_item_id == ITEM)))
    result = dict(key=KEY, confirmation=CONFIRMATION, lots=lots, order_item=row(item),
        product_version=product.version, confirmed_basis=basis,
        snapshots=[row(s) for s in graph.compiled.snapshots], reserve_lot_id=1148, reserve_quantity=300)
    return json.loads(canonical(result))

def verify(db, plan):
    requirements = read_graph_requirements(db, ITEM)
    assert all(p.make_units == 0 for p in requirements.plan.products)
    assert sum(m.purchase_sheets for m in requirements.plan.materials) == 0
    lots = []
    for old in plan['lots']:
        lid = old['lot']['id']; lot = db.get(InventoryLot, lid)
        assert lot.finished_detail.physical_basis_json == plan['confirmed_basis']
        take = 300 if lid == 1148 else 0
        assert lot.quantity_available == old['lot']['quantity_available'] - take
        assert lot.quantity_reserved == take and lot.quantity_consumed == lot.quantity_damaged == lot.quantity_scrapped == 0
        assert lot.warehouse_location_id == old['lot']['warehouse_location_id'] and lot.unit == 'boxes'
        # No cost, ownership, source, location or stock-date changes.
        for field in set(old['lot']) - {'quantity_available', 'quantity_reserved', 'version', 'last_movement_at', 'updated_at'}:
            assert json.loads(canonical(getattr(lot,field))) == old['lot'][field], field
        for field in set(old['detail']) - {'physical_basis_json'}:
            assert json.loads(canonical(getattr(lot.finished_detail,field))) == old['detail'][field], field
        movement = db.scalar(select(InventoryMovement).where(InventoryMovement.idempotency_key == f'{KEY}:identity:{lid}'))
        evidence = json.loads(movement.remarks)
        assert evidence['original_basis'] == old['detail']['physical_basis_json']
        assert evidence['confirmed_basis'] == plan['confirmed_basis'] and evidence['plan_hash'] == digest(plan)
        assert movement.quantity == 0
        lots.append(dict(lot_id=lid, location_id=lot.warehouse_location_id, available=lot.quantity_available,
            reserved=lot.quantity_reserved, confirmed_unit='套', identity_movement_id=movement.id))
    reservations = list(db.scalars(select(InventoryReservation).where(InventoryReservation.order_item_id == ITEM)))
    assert len(reservations) == 1
    reservation = reservations[0]
    assert reservation.inventory_lot_id == 1148 and reservation.reserved_stock_quantity == reservation.credited_requirement_quantity == 300
    assert reservation.status == 'active' and reservation.idempotency_key == KEY + ':reserve'
    from app.api.requisition import _bom_pending_component_requirements
    pending = _bom_pending_component_requirements(db, db.get(OrderItem, ITEM))
    assert sum(p['requisition_qty'] for p in pending) == 0
    assert db.get(OrderItem, ITEM).quantity == 300 and db.get(OrderItem, ITEM).delivered_quantity == 0
    return dict(key=KEY, plan_hash=digest(plan), order_item_id=ITEM, reservation_id=reservation.id,
        lots=lots, total_on_hand=5900, total_available=5600, reserved_for_order=300,
        child_requisition_quantity=0, created_stock=0, created_assembly=0)

def execute(db, actor, plan):
    assert actor and actor.id == 1 and actor.is_active and actor.role == 'admin'
    assert plan['key'] == KEY and plan['confirmation'] == CONFIRMATION
    with atomic_bom(db):
        prior = db.scalar(select(InventoryMovement).where(InventoryMovement.idempotency_key == KEY + ':identity:1148'))
        if prior:
            assert json.loads(prior.remarks)['plan_hash'] == digest(plan)
            return verify(db, plan)
        assert preview(db) == plan, 'Reviewed inventory or frozen order changed; abort without writing'
        for original in plan['lots']:
            lid = original['lot']['id']; lot = db.get(InventoryLot, lid); before = _balances(lot)
            changed = db.execute(update(InventoryLot).where(InventoryLot.id == lid,
                InventoryLot.version == original['lot']['version'], InventoryLot.quantity_reserved == 0,
                InventoryLot.quantity_consumed == 0).values(version=InventoryLot.version + 1))
            assert changed.rowcount == 1
            lot.finished_detail.physical_basis_json = plan['confirmed_basis']
            evidence = dict(confirmation=CONFIRMATION, plan_hash=digest(plan), order_item_id=ITEM,
                original_basis=original['detail']['physical_basis_json'], confirmed_basis=plan['confirmed_basis'],
                quantity_unit_correction='原数值逐一确认为整套数量；无换算、无新增入库、无子件消耗', actor_id=actor.id)
            movement = _movement(db, lot=lot, movement_type='adjust', quantity=0, before=before,
                operator_id=actor.id, reason='确认旧盘点批次为成品套数', remarks=canonical(evidence),
                idempotency_key=f'{KEY}:identity:{lid}')
            db.flush()
            append_audit_event(db, event_category='business', result='success', source='script',
                module_code='warehouse', action_code='CONFIRMED_LEGACY_FINISHED_SETS', resource='inventory_lot',
                actor=actor, entity_type='inventory_lot', entity_id=lid, customer_id=CUSTOMER,
                details={**evidence, 'movement_id':movement.id})
        db.flush()
        graph = read_graph_requirements(db, ITEM)
        root = next(s for s in graph.compiled.snapshots if s.component_product_id == PRODUCT)
        lot = db.get(InventoryLot, 1148)
        reserve_finished_inventory_for_bom_component(db, order_item_id=ITEM, bom_snapshot_id=root.id,
            inventory_lot_id=lot.id, quantity=300, expected_version=lot.version, operator_id=actor.id,
            idempotency_key=KEY + ':reserve', warning_acknowledged_codes=[])
        db.flush()
        result = verify(db, plan)
        append_audit_event(db, event_category='business', result='success', source='script',
            module_code='warehouse', action_code='CONFIRMED_SETS_ORDER_RESERVE', resource='order_item',
            actor=actor, entity_type='order_item', entity_id=ITEM, customer_id=CUSTOMER, details=result)
        db.flush()
        return result
