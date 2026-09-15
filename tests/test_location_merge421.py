from datetime import date
from sqlalchemy import select, func
from app.models.warehouse_inventory import InventoryMovement
from app.models.product_bom import ProductBomComponent
from app.services.warehouse_reading_identity import shelf_merge_identity
from app.services.warehouse_twin_dashboard import _lot_payload
from app.api.mobile_erp import _mobile_goods_payload
from test_warehouse_inventory_foundation import db, finished_lot, seed_product_for_customer


def test_reading_identity_uses_frozen_box_not_current_master_and_never_mutates(db):
    lot = finished_lot(db)
    detail = lot.finished_detail
    frozen = detail.box_type_snapshot
    detail.product.box_style = 'changed'
    db.commit()
    before = db.scalar(select(func.count()).select_from(InventoryMovement))
    for payload in (_lot_payload(lot, date.today()), _mobile_goods_payload(lot)):
        assert payload['box_style'] == frozen
        assert payload['is_bom_component'] is False
        assert payload['location_id'] == lot.warehouse_location_id
    assert lot.quantity_available == 20
    assert db.scalar(select(func.count()).select_from(InventoryMovement)) == before
    assert not db.dirty and not db.new


def test_master_bom_child_is_excluded_even_without_internal_flag(db):
    lot = finished_lot(db)
    detail = lot.finished_detail
    parent = seed_product_for_customer(db, detail.customer, 'parent')
    db.add(ProductBomComponent(parent_product_id=parent.id, component_product_id=detail.product_id,
                              quantity_per_set=4, display_order=0, internal_component_code='child'))
    db.commit()
    assert shelf_merge_identity(lot)['is_bom_component'] is True
    assert _mobile_goods_payload(lot)['is_bom_component'] is True


def test_missing_box_and_detached_identity_fail_closed(db):
    lot = finished_lot(db)
    lot.finished_detail.box_type_snapshot = None
    assert shelf_merge_identity(lot)['box_style'] is None
    db.expunge(lot)
    assert shelf_merge_identity(lot)['is_bom_component'] is None


def test_scan_groups_without_changing_old_qr_identity_and_excludes_children(db, monkeypatch):
    from fastapi import Response
    from app.api import warehouse as api
    from app.services.warehouse_inventory import manual_finished_in
    from app.services.mobile_shelf_labels import product_key
    lot = finished_lot(db)
    detail = lot.finished_detail
    second = manual_finished_in(db, customer_id=detail.owner_customer_id, product_id=detail.product_id,
        location_id=lot.warehouse_location_id, quantity=7, stock_date=date.today(), source_type='manual',
        remarks=None, operator_id=None, idempotency_key='merge-second')
    db.commit()
    key = product_key(lot)
    monkeypatch.setattr(api, '_mobile_shelf_location', lambda *args: (None, {}))
    monkeypatch.setattr(api, '_visible_customer_ids', lambda *args: None)
    def read():
        return api.mobile_shelf_scan(lot.warehouse_location_id, Response(), None, db, None)['items']
    rows = read()
    assert len(rows) == 1 and rows[0]['quantity'] == 27
    assert {r['id'] for r in rows[0]['lots']} == {lot.id, second.id}
    second.finished_detail.box_type_snapshot = 'different'
    db.commit()
    assert len(read()) == 2
    second.finished_detail.box_type_snapshot = detail.box_type_snapshot
    detail.product.is_internal_component = True
    db.commit()
    assert len(read()) == 2
    assert product_key(lot) == key
    assert lot.quantity_available == 20 and second.quantity_available == 7
    monkeypatch.setattr(api, '_visible_customer_ids', lambda *args: set())
    assert read() == []
