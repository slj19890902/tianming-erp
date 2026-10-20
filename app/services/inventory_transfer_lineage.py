"""Verify unchanged entry facts through actual location-transfer ledgers."""

def location_split_source(session, lot):
    from sqlalchemy import select
    from app.models.warehouse_inventory import InventoryLot, InventoryLotTransfer, InventoryMovement
    from app.services.warehouse_inventory import _transfer_key
    if lot.source_type != 'transfer':
        return None
    edges = list(session.scalars(select(InventoryLotTransfer).where(
        InventoryLotTransfer.target_lot_id == lot.id,
        InventoryLotTransfer.source_lot_id != lot.id)))
    if len(edges) != 1:
        return None
    edge = edges[0]
    if (edge.quantity <= 0 or min(edge.available_quantity, edge.reserved_quantity) < 0
            or edge.available_quantity + edge.reserved_quantity != edge.quantity):
        return None
    source = session.get(InventoryLot, edge.source_lot_id)
    # Source-before-target prevents a fabricated circular inheritance chain.
    if source is None or source.id >= lot.id:
        return None
    if (lot.source_ref_type != split_source_ref_type(source)
            or lot.source_ref_id != source.source_ref_id):
        return None
    fields = ('inventory_type', 'unit',
              'stock_date', 'stock_date_accuracy', 'stock_date_original_text',
              'estimated_unit_cost_snapshot', 'estimated_square_price_snapshot',
              'estimated_cost_area_m2_snapshot', 'cost_snapshot_source',
              'cost_snapshot_detail_json', 'cost_snapshot_at')
    if any(getattr(source, key) != getattr(lot, key) for key in fields):
        return None
    for name in ('finished_detail', 'semi_finished_detail'):
        old, new = getattr(source, name), getattr(lot, name)
        if (old is None) != (new is None):
            return None
        if old is not None and any(getattr(old, column.key) != getattr(new, column.key)
                for column in old.__table__.columns if column.key != 'inventory_lot_id'):
            return None
    prefix = 'sheet-move' if lot.inventory_type == 'semi_finished' else 'location-transfer'
    for role, lid, sign in (('source', source.id, -1), ('target', lot.id, 1)):
        movement = session.scalar(select(InventoryMovement).where(
            InventoryMovement.idempotency_key == _transfer_key(prefix, edge.idempotency_key, role)))
        if (movement is None or movement.inventory_lot_id != lid
                or movement.movement_type != 'location_transfer'
                or movement.quantity != edge.quantity or movement.unit != lot.unit
                or movement.after_available - movement.before_available != sign * edge.available_quantity
                or movement.after_reserved - movement.before_reserved != sign * edge.reserved_quantity
                or any(getattr(movement, 'before_' + bucket) != getattr(movement, 'after_' + bucket)
                       for bucket in ('consumed', 'damaged', 'scrapped'))):
            return None
        if role == 'target' and (movement.before_available or movement.before_reserved):
            return None
    return source


def is_original_or_location_split(session, lot, original_id):
    """An output/receipt FK names the first lot; only proven moves may descend from it."""
    from app.models.warehouse_inventory import InventoryLot
    if original_id is None:
        return False
    if lot.id == original_id:
        return True
    # Cost list projections intentionally omit physical columns. Verify the
    # persisted physical identity and both movement ledgers, not projected JSON.
    current = session.get(InventoryLot, lot.id)
    if current is None:
        return False
    for field in ('cost_snapshot_source', 'cost_snapshot_detail_json',
                  'estimated_unit_cost_snapshot', 'cost_snapshot_at', 'source_ref_type', 'source_ref_id'):
        if getattr(current, field) != getattr(lot, field):
            return False
    while current.id != original_id:
        current = location_split_source(session, current)
        if current is None:
            return False
        # Every edge strictly decreases the ID, so cycles cannot be accepted.
        if current.id < original_id:
            return False
    return True


def split_source_ref_type(lot):
    # The original onboarding line is a unique first-entry identity, protected
    # by a database trigger and partial unique index. A split is its descendant,
    # not a second posting of that line. Keep the line ID and actual transfer edge.
    if lot.source_ref_type == 'inventory_onboarding_line':
        return 'inventory_onboarding_transfer'
    return lot.source_ref_type
