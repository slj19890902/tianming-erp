"""Real save contracts for older sharing policies and customer-local identity."""
import json
from copy import deepcopy
from decimal import Decimal

import pytest
from fastapi import HTTPException
from app.api import products as api
from app.models.shared_finished_stock import SharedFinishedMember, SharedFinishedMutation
from app.services import shared_product_master as master, shared_finished_stock as shared
from tests.test_shared_finished_stock import reservation_db
from tests.test_shared_structure_edit_20261024 import case, payload, submit
from tests.test_yg_gy_shared_master import edit


def legacy_policy(db, source):
    gid = db.get(SharedFinishedMember, source.id).group_id
    policy = deepcopy(master.receipt(db, gid))
    policy.pop('full_catalog', None)
    db.add(SharedFinishedMutation(group_id=gid, operation_key='legacy-manufacturing-policy',
        request_json='{}', result_json=json.dumps({master.CAPABILITY: policy})))
    db.commit()
    return gid


def test_legacy_shared_manufacturing_converts_both_members_without_changing_scope(case):
    db, data, source, peer, child, item, lot = case
    gid = legacy_policy(db, source)
    ordinary = api._product_payload_snapshot(source)
    ordinary['box_style'] = '开槽箱'
    submit(db, data, source, payload(source, child))
    assert source.is_composite and peer.is_composite
    assert source.box_style == peer.box_style == 'BOM组合'
    assert not master.receipt(db, gid).get('full_catalog')
    back = payload(source, ordinary=ordinary)
    submit(db, data, source, back)
    assert not source.is_composite and not peer.is_composite
    assert (source.sale_unit_price, peer.sale_unit_price) == (5, 8)


def test_local_code_name_price_keep_membership_stock_and_peer_identity(case):
    db, data, source, peer, child, item, lot = case
    peer_before = (peer.product_code, peer.product_name, peer.sale_unit_price, peer.version)
    identity = shared.member_identity(source)
    old_lot = (lot.finished_detail.physical_basis_json, lot.finished_detail.inventory_code_snapshot,
               lot.quantity_available, lot.quantity_reserved)
    edit(db, data, source, {'product_code':'YG-80011641', 'product_name':'研光独立名称',
                          'sale_unit_price':Decimal('6')})
    db.commit()
    assert (peer.product_code, peer.product_name, peer.sale_unit_price, peer.version) == peer_before
    assert shared.member_identity(source) == identity
    assert shared.match(db, lot, product_id=peer.id, customer_id=peer.customer_id)
    assert old_lot == (lot.finished_detail.physical_basis_json, lot.finished_detail.inventory_code_snapshot,
                       lot.quantity_available, lot.quantity_reserved)
    # One request may combine a customer-local rename with a shared structure edit.
    request = payload(source, child)
    request.product.product_code = 'YG-80011641-B'
    request.product.product_name = '研光组合'
    submit(db, data, source, request)
    assert source.product_code == 'YG-80011641-B' and peer.product_code == peer_before[0]
    assert source.is_composite and peer.is_composite
    assert (source.sale_unit_price, peer.sale_unit_price) == (6, 8)


def test_customer_ownership_cannot_change_via_local_identity_edit(case):
    db, data, source, peer, child, item, lot = case
    with pytest.raises(HTTPException):
        edit(db, data, source, {'customer_id': peer.customer_id})
    db.rollback()


def test_dimensionless_die_cut_shared_identity_survives_customer_code_change(case):
    from app.models.mold_tool import MoldTool
    from app.services.product_specification import product_stock_specification
    from app.services.shared_customer_access import canonical
    db, data, source, peer, child, item, lot = case
    mold = MoldTool(mold_code='SAME-DIE', mold_name='同一模具', rack_location='R01')
    db.add(mold); db.flush()
    for p in (source, peer):
        p.length_mm = p.width_mm = p.height_mm = None
        p.box_category = 'die_cut'
        p.mold_tool_id = mold.id
        p.sheet_cutting_settings = {'schema_version':2, 'whole':{
            'length_parts':1, 'width_parts':1, 'mold_count':1, 'is_die_cut':True}}
    db.flush()
    gid = db.get(SharedFinishedMember, source.id).group_id
    policy = deepcopy(master.receipt(db, gid))
    policy['members'] = {str(p.id):dict(identity_json=shared.member_identity(p),
        product_basis_json=shared.product_basis(p)) for p in (source, peer)}
    db.add(SharedFinishedMutation(group_id=gid, operation_key='die-fixture-approved',
        request_json='{}', result_json=canonical({master.CAPABILITY:policy})))
    db.commit()
    before = shared.member_identity(source)
    edit(db, data, source, {'product_code':'YG-NEW-CODE'})
    db.commit()
    assert shared.member_identity(source) == before
    assert 'YG-NEW-CODE' in product_stock_specification(source)
    assert 'YG-NEW-CODE' not in shared.product_basis(source)
    source.sheet_cutting_settings = {'schema_version':2, 'whole':{
        'length_parts':1, 'width_parts':1, 'mold_count':2, 'is_die_cut':True}}
    assert shared.member_identity(source) != before  # A real tool-count change is still physical.


def test_rename_existing_bom_child_uses_stable_member_mapping(case):
    from app.models.product_bom import ProductBomComponent
    db, data, source, peer, child, item, lot = case
    submit(db, data, source, payload(source, child))
    edit(db, data, child, {'product_code':'YG-CHILD-LOCAL', 'product_name':'研光子件'})
    db.commit()
    request = payload(source, child)
    request.bom.components[0].quantity_per_set = Decimal(3)
    submit(db, data, source, request)
    assert [edge.quantity_per_set for edge in db.query(ProductBomComponent).all()] == [3, 3]


def test_desktop_http_preflight_and_combined_save_support_legacy_policy(case):
    from fastapi import FastAPI
    from fastapi.encoders import jsonable_encoder
    from fastapi.testclient import TestClient
    from app.api.deps import get_db, get_current_user
    db, data, source, peer, child, item, lot = case
    legacy_policy(db, source)
    app = FastAPI()
    app.include_router(api.router, prefix='/api/master/products')
    def test_db():
        yield db
    app.dependency_overrides[get_db] = test_db
    app.dependency_overrides[get_current_user] = lambda: data['admin']
    ordinary = api._product_payload_snapshot(source)
    ordinary['box_style'] = '开槽箱'
    with TestClient(app) as client:
        for request in (payload(source, child),):
            body = jsonable_encoder(request.model_dump())
            preview = client.post(f'/api/master/products/{source.id}/update-preview', json=body['product'])
            assert preview.status_code == 200, preview.text
            assert not source.is_composite and not peer.is_composite
            body['product']['confirmation_token'] = preview.json()['confirmation_token']
            saved = client.put(f'/api/master/products/{source.id}/with-bom', json=body)
            assert saved.status_code == 200, saved.text
        assert source.is_composite and peer.is_composite
        back = payload(source, ordinary=ordinary)
        body = jsonable_encoder(back.model_dump())
        preview = client.post(f'/api/master/products/{source.id}/update-preview', json=body['product'])
        assert preview.status_code == 200, preview.text
        body['product']['confirmation_token'] = preview.json()['confirmation_token']
        saved = client.put(f'/api/master/products/{source.id}/with-bom', json=body)
        assert saved.status_code == 200, saved.text
    assert not source.is_composite and not peer.is_composite


def test_combined_bom_failure_rolls_back_shared_location_changes(case, monkeypatch):
    from app.models.receipt_putaway import ProductStoragePreference
    from app.services.shared_product_storage import info
    db, data, source, peer, child, item, lot = case
    before = info(db, source.id)['shared_group_version']
    request = payload(source, child)
    request.product.receipt_storage = api.ProductStorageUpdatePayload(
        expected_version=0, expected_shared_group_version=before, area_id=None, location_id=None)
    original = api._update_product_bom
    def fail(product_id, *a, **kw):
        if product_id == peer.id:
            raise RuntimeError('peer BOM failure')
        return original(product_id, *a, **kw)
    monkeypatch.setattr(api, '_update_product_bom', fail)
    with pytest.raises(RuntimeError):
        submit(db, data, source, request)
    assert db.query(ProductStoragePreference).count() == 0
    assert info(db, source.id)['shared_group_version'] == before
    assert not source.is_composite and not peer.is_composite
