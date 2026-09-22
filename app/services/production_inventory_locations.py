"""Read current physical locations without changing completion/action identities."""
from app.services.location_candidates import warehouse_location_projection
from app.services.warehouse_location_address import employee_location_name
from app.services.warehouse_relocation_pending import is_pending_relocation_location


def physical_quantity(lot):
    return max(sum(int(value or 0) for value in (
        lot.quantity_available, lot.quantity_reserved, lot.quantity_damaged)), 0)


def lot_location(lot, locations, contexts):
    location = locations.get(lot.warehouse_location_id)
    pallet = lot.pallet_item.pallet if lot.pallet_item else None
    status, issue = 'located', None
    if lot.status not in {'active', 'frozen'}:
        status, issue = 'inventory_status_mismatch', '库存批次仍有数量但状态不可用，请核对仓库'
    elif location is None:
        status, issue = 'unlocated', '当前库存批次缺少有效库位，请核对仓库'
    elif is_pending_relocation_location(location):
        # The designated logical pending location intentionally has no physical pallet.
        if pallet is not None:
            status, issue = 'pallet_mismatch', '待归位库存不应同时绑定实体栈板，请核对仓库'
    elif location.storage_type == 'rack' and location.address_kind == 'rack_slot' and location.map_rack_id:
        if pallet is not None:
            status, issue = 'pallet_mismatch', '货架库存不应同时绑定实体栈板，请核对仓库'
    elif pallet is None:
        status, issue = 'missing_pallet', '当前库存未关联实体栈板，请核对仓库'
    elif not pallet.is_current or pallet.status != 'active' or pallet.location_id != lot.warehouse_location_id:
        status, issue = 'pallet_mismatch', '库存批次与实体栈板库位不一致，请核对仓库'
    if status != 'located':
        location = None
    context = contexts.get(location.id, {}) if location else {}
    projection = warehouse_location_projection(location, **context) if location else {}
    return dict(
        inventory_lot_id=lot.id,
        inventory_lot_version=lot.version,
        current_inventory_quantity=physical_quantity(lot),
        current_inventory_status=status,
        current_warehouse_location_id=location.id if location else None,
        current_warehouse_location_code=location.location_code if location else None,
        current_warehouse_location_name=employee_location_name(location, area=context.get('area'),
            floor=context.get('floor'), area_sequence=context.get('area_sequence')) if location else None,
        current_warehouse_location_position_status=projection.get('position_status'),
        current_warehouse_location_map_issue=projection.get('map_issue'),
        current_location_issue=issue,
    )


def current_inventory(lots, locations, contexts):
    """A batch may occupy several places; never choose an arbitrary map target."""
    entries = [lot_location(lot, locations, contexts)
               for lot in sorted(lots, key=lambda lot: (lot.warehouse_location_id or 0, lot.id))
               if physical_quantity(lot) > 0]
    location_ids = {entry['current_warehouse_location_id'] for entry in entries}
    single_location = len(location_ids) == 1 and None not in location_ids
    names = list(dict.fromkeys(entry['current_warehouse_location_name'] for entry in entries
                              if entry['current_warehouse_location_name']))
    issues = list(dict.fromkeys(entry['current_location_issue'] for entry in entries if entry['current_location_issue']))
    map_issues = list(dict.fromkeys(entry['current_warehouse_location_map_issue'] for entry in entries
                                   if entry['current_warehouse_location_map_issue']))
    status = ('located' if all(entry['current_inventory_status'] == 'located' for entry in entries)
              else entries[0]['current_inventory_status'] if len(entries) == 1 else 'partial_location')
    return dict(
        current_inventory_locations=entries,
        current_inventory_quantity=sum(entry['current_inventory_quantity'] for entry in entries),
        current_inventory_status=status if entries else 'drained' if lots else 'missing',
        current_inventory_lot_id=entries[0]['inventory_lot_id'] if len(entries) == 1 else None,
        current_warehouse_location_id=entries[0]['current_warehouse_location_id'] if single_location else None,
        current_warehouse_location_code=entries[0]['current_warehouse_location_code'] if single_location else None,
        current_warehouse_location_name=' / '.join(names) or None,
        current_warehouse_location_position_status=entries[0]['current_warehouse_location_position_status'] if single_location else None,
        current_warehouse_location_map_issue=' / '.join(map_issues) or None,
        current_location_issue=' / '.join(issues) or None,
    )
