from copy import deepcopy
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from starlette.requests import Request

from app.api import products as api, fixed_shelf
from app.models.product import Product
from app.models.user import User
from app.models.receipt_putaway import ProductStoragePreference
from app.models.warehouse_inventory import WarehouseLocation, WarehouseArea
from app.services import shared_finished_stock as shared, customer_sharing, shared_product_storage as service
from tests.test_fixed_shelf import setup, incoming
from tests.test_p1_123_warehouse_region_rack_labels import rack_factory
from tests.test_warehouse_inventory_foundation import seed_other_customer_product


@pytest.fixture
def storage_case(setup):
    factory, pid, cid, ids = setup
    with factory() as db:
        source = db.get(Product, pid)
        customer, unused = seed_other_customer_product(db, 'SHARED-STORE')
        fields = {key: value for key, value in api._product_payload_snapshot(source).items() if hasattr(Product, key)}
        fields.update(customer_id=customer.id, product_name='乙客户名称', sale_unit_price=Decimal('8'))
        source.sale_unit_price = Decimal('5')
        peer = Product(**fields)
        db.add(peer); db.flush()
        actor = db.get(User, 1)
        args = dict(product_ids=[source.id, peer.id], lot_ids=[])
        plan = shared.preview(db, **args)
        result = shared.confirm(db, **args, actor=actor, preview_hash=plan['preview_hash'],
            operation_key='storage-sharing', evidence='虚构同实物')
        customer_sharing.activate_master(db, result['group_id'], actor)
        db.commit()
        yield db, source, peer, actor, ids


def storage_payload(db, source, lid):
    loc = db.get(WarehouseLocation, lid)
    area = db.scalar(select(WarehouseArea).where(WarehouseArea.area_code == loc.area_code))
    current = service.info(db, source.id)
    return dict(expected_version=current['version'], expected_shared_group_version=current['shared_group_version'],
        area_id=area.id, location_id=lid, address_version=loc.address_version, layout_version=loc.floor3_layout.version)


def local_request(source, **values):
    return api.ProductUpdatePayload(customer_id=source.customer_id, product_code=source.product_code,
        product_name=source.product_name, box_category=source.box_category, expected_version=source.version,
        structure_edit_fields=[], **values)


def test_product_save_updates_both_defaults_but_only_local_price_and_keeps_stock(storage_case):
    db, source, peer, actor, ids = storage_case
    lot = incoming(db, source.id, source.customer_id, ids[0]); db.commit()
    before = (lot.warehouse_location_id, lot.quantity_available, lot.version)
    request = local_request(source, sale_unit_price=Decimal('6'), receipt_storage=storage_payload(db, source, ids[1]))
    saved = api._update_product(source.id, request, db, actor)
    assert saved['receipt_storage']['location_id'] == ids[1]
    assert [db.get(ProductStoragePreference, p.id).location_id for p in (source, peer)] == [ids[1]] * 2
    assert (source.sale_unit_price, peer.sale_unit_price) == (6, 8)
    assert (lot.warehouse_location_id, lot.quantity_available, lot.version) == before
    # Reverse direction and explicit cancellation synchronize as well.
    current = service.info(db, peer.id)
    service.save(db, peer.id, actor=actor, expected_version=current['version'],
        expected_shared_group_version=current['shared_group_version'], area_id=None, location_id=None)
    db.commit()
    assert all(db.get(ProductStoragePreference, p.id).location_id is None for p in (source, peer))


def test_stale_peer_form_rejected_with_no_partial_destination_changes(storage_case):
    db, source, peer, actor, ids = storage_case
    stale = storage_payload(db, peer, ids[0])
    service.save(db, source.id, actor=actor, **storage_payload(db, source, ids[1])); db.commit()
    with pytest.raises(HTTPException) as exc:
        service.save(db, peer.id, actor=actor, **stale)
    assert exc.value.status_code == 409
    db.rollback()
    assert [db.get(ProductStoragePreference, p.id).location_id for p in (source, peer)] == [ids[1]] * 2


def test_late_product_failure_rolls_back_every_saved_preference(storage_case, monkeypatch):
    db, source, peer, actor, ids = storage_case
    before = service.info(db, source.id)['shared_group_version']
    def fail(*args, **kwargs):
        raise RuntimeError('failure after saving preferences')
    monkeypatch.setattr(api, 'apply_versioned_update', fail)
    with pytest.raises(RuntimeError):
        api._update_product(source.id, local_request(source, sale_unit_price=6,
            receipt_storage=storage_payload(db, source, ids[1])), db, actor)
    assert db.query(ProductStoragePreference).count() == 0
    assert service.info(db, source.id)['shared_group_version'] == before
    assert (source.sale_unit_price, peer.sale_unit_price) == (5, 8)


def test_storage_cannot_write_outside_customer_scope(storage_case, monkeypatch):
    db, source, peer, actor, ids = storage_case
    def restricted(customer_id, **kwargs):
        if customer_id == peer.customer_id:
            raise HTTPException(403, 'outside scope')
    monkeypatch.setattr('app.api.deps.require_customer_access', restricted)
    with pytest.raises(HTTPException) as exc:
        service.save(db, source.id, actor=actor, **storage_payload(db, source, ids[1]))
    assert exc.value.status_code == 403
    db.rollback()
    assert db.query(ProductStoragePreference).count() == 0


def test_storage_endpoint_replay_does_not_increment_peers_again(storage_case):
    db, source, peer, actor, ids = storage_case
    payload = fixed_shelf.StoragePayload(**storage_payload(db, source, ids[0]), idempotency_key='shared-storage-once')
    request = Request({'type':'http', 'headers':[], 'client':('127.0.0.1', 1)})
    first = fixed_shelf.save_receipt_storage(source.id, payload, request, db, actor)
    assert fixed_shelf.save_receipt_storage(source.id, payload, request, db, actor) == first
    assert [db.get(ProductStoragePreference, p.id).version for p in (source, peer)] == [1, 1]
    with pytest.raises(HTTPException) as exc:
        fixed_shelf.save_receipt_storage(source.id, payload.model_copy(update={'location_id':ids[1]}), request, db, actor)
    assert exc.value.status_code == 409
