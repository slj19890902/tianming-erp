"""Pending component addresses must follow the same published map as the warehouse."""
from types import SimpleNamespace

from tests.test_inventory_cost_snapshot import db
from tests.test_ui_assembly_release_20261015 import finished_children


def current_context(monkeypatch, location):
    from app.services import bom_pending_locations as service
    area = SimpleNamespace(area_name='I1', area_code='EDIT-040', construction_status='enabled')
    floor = SimpleNamespace(floor_number=3)
    context = dict(area=area, floor=floor, area_sequence=3)
    monkeypatch.setattr(service, 'load_warehouse_location_projection_contexts',
                        lambda db, locations: {location.id: context})
    monkeypatch.setattr(service, 'warehouse_location_projection',
                        lambda location, **kw: dict(position_status='mapped', map_issue=None))
    return service, area


def test_pending_stock_uses_current_address_without_moving_stock(db, monkeypatch):
    from app.services.bom_stock_pending import pending_stock
    parent, children, lots, location = finished_children(db)
    location.location_name = '新区域（待设置） 011 号位'
    location.area_code = 'EDIT-040'
    service, _ = current_context(monkeypatch, location)
    db.flush()
    before = [(lot.id, lot.warehouse_location_id, lot.quantity_available, lot.version) for lot in lots]
    row = next(row for row in pending_stock(db, None) if row['parent_product_id'] == parent.id)
    assert row['product_code'] == parent.product_code
    assert {source['location'] for source in row['sources']} == {'三楼 I1-03'}
    assert all(source['location_id'] == location.id for source in row['sources'])
    assert all(source['location_position_status'] == 'mapped' for source in row['sources'])
    assert before == [(lot.id, lot.warehouse_location_id, lot.quantity_available, lot.version) for lot in lots]
    assert location.location_name == '新区域（待设置） 011 号位'
    assert pending_stock(db, set()) == []


def test_unpublished_and_retired_addresses_are_not_map_targets(db, monkeypatch):
    _, _, lots, location = finished_children(db)
    service, area = current_context(monkeypatch, location)
    monkeypatch.setattr(service, 'warehouse_location_projection',
                        lambda location, **kw: dict(position_status='unplaced', map_issue='当前地图未发布此位置'))
    row = service.pending_source_locations(db, lots)[location.id]
    assert row['location_id'] is None
    assert row['location_issue'] == '当前地图未发布此位置'
    area.construction_status = 'archived'
    row = service.pending_source_locations(db, lots)[location.id]
    assert row['location'] == '历史位置已退役'
    assert row['location_id'] is None
    assert '核对实际位置' in row['location_issue']


def test_missing_address_stays_explicit(db, monkeypatch):
    from app.services.bom_pending_locations import pending_source_locations
    row = pending_source_locations(db, [SimpleNamespace(warehouse_location_id=999999)])[999999]
    assert row['location'] == '位置待核对'
    assert row['location_id'] is None
    assert row['location_issue']
