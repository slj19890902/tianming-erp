"""Read component addresses from the current warehouse map, without moving stock."""
from sqlalchemy import select

from app.models.warehouse_inventory import WarehouseLocation
from app.services.location_candidates import (
    load_warehouse_location_projection_contexts, warehouse_location_projection,
)
from app.services.warehouse_location_address import employee_location_name


def pending_source_locations(db, lots):
    location_ids = {lot.warehouse_location_id for lot in lots}
    locations = {location.id: location for location in db.scalars(
        select(WarehouseLocation).where(WarehouseLocation.id.in_(location_ids)))}
    contexts = load_warehouse_location_projection_contexts(db, locations.values())
    result = {}
    for location_id in location_ids:
        location = locations.get(location_id)
        context = contexts.get(location_id, {})
        name, issue, status = '位置待核对', '当前库存缺少正式库位，请核对实际位置', 'unplaced'
        if location is not None:
            retired = (not location.is_active
                       or getattr(context.get('area'), 'construction_status', None) == 'archived'
                       or getattr(context.get('policy'), 'status', None) == 'archived')
            if retired:
                name, issue = '历史位置已退役', '该批仍有库存，请核对实际位置后走正常移库'
            else:
                projection = warehouse_location_projection(location, **context)
                name = employee_location_name(location, area=context.get('area'),
                    floor=context.get('floor'), area_sequence=context.get('area_sequence'))
                status = projection['position_status']
                issue = projection.get('map_issue')
        result[location_id] = dict(location=name, location_issue=issue,
            location_position_status=status, location_id=location_id if status == 'mapped' else None)
    return result
