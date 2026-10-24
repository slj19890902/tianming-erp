import json
from copy import deepcopy
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select, update

from app.api import products as api
from app.models.product import Product
from app.models.product_bom import ProductBomComponent
from app.models.multilevel_bom import ProductBomProfile
from app.models.shared_finished_stock import SharedFinishedMember, SharedFinishedMutation
from app.services import shared_finished_stock as shared
from tests.test_shared_finished_stock import reservation_db, setup_pair, reserve_for
from tests.test_customer_sharing import save, args
from tests.test_yg_gy_shared_master import edit
from tests.test_n039_composite_bom_requisition import composite_requisition_app, _login
from tests.test_p1_81_receipt_purpose_flow import _p181_published_map_identity


@pytest.fixture
def case(reservation_db):
    db, data = reservation_db
    source = data['product']
    peer, item, lot = setup_pair(db, data)
    source.sale_unit_price, peer.sale_unit_price = Decimal('5'), Decimal('8')
    source.sale_unit_price_no_tax, peer.sale_unit_price_no_tax = Decimal('4'), Decimal('7')
    db.commit()
    save(db, data, args(data))
    fields = {k: v for k, v in api._product_payload_snapshot(source).items() if hasattr(Product, k)}
    fields.update(product_code='STRUCTURE-CHILD', customer_material_code='STRUCTURE-CHILD',
                  product_name='虚构共享结构子件', sale_unit_price=Decimal('2'))
    child = Product(**fields)
    db.add(child)
    db.commit()
    return db, data, source, peer, child, item, lot


def payload(source, child=None, *, ordinary=None):
    fields = deepcopy(ordinary or api._product_payload_snapshot(source))
    fields['expected_version'] = source.version
    if ordinary is None:
        fields.update(box_style='BOM组合', unit='套', box_category='normal',
                      is_virtual_composite_parent=False, combination_mode='parent_priced_set',
                      composite_fulfillment_mode='parent_delivery')
    else:
        fields.update(is_composite=False, is_virtual_composite_parent=False)
    return api.ProductWithBOMUpdatePayload.model_validate({'product': fields, 'bom': {
        'expected_version': source.version, 'inventory_mode': 'assembled' if child else None,
        'material_mode': 'expand_children' if child else None,
        'delivery_mode': 'parent' if child else None,
        'components': [{'component_product_id': child.id, 'quantity_per_set': 2,
                        'inventory_relation': 'assembly'}] if child else []}})


def submit(db, data, source, request):
    try:
        return api._save_product_with_bom(request, db, data['admin'], source.id)
    except HTTPException as exc:
        if not isinstance(exc.detail, dict) or exc.detail.get('code') != 'MASTER_CHANGE_CONFIRMATION_REQUIRED':
            raise
        request.product.confirmation_token = exc.detail['confirmation_token']
        return api._save_product_with_bom(request, db, data['admin'], source.id)


def state(db, source, peer, lot):
    return (source.version, peer.version, source.box_style, peer.box_style,
            source.sale_unit_price, peer.sale_unit_price, lot.quantity_available,
            lot.quantity_reserved, lot.finished_detail.physical_basis_json,
            db.query(Product).count(), db.query(ProductBomComponent).count(),
            db.query(SharedFinishedMutation).count())


def test_shared_ordinary_to_bom_and_back_keeps_customer_prices_and_frozen_stock(case):
    db, data, source, peer, child, item, lot = case
    ordinary = api._product_payload_snapshot(source)
    ordinary['box_style'] = '开槽箱'
    reservation = reserve_for(db, data, item, lot, 10, 'before-shared-structure')
    db.commit()
    frozen = (item.unit_price, item.subtotal, item.quantity, lot.quantity_available,
              lot.quantity_reserved, lot.finished_detail.physical_basis_json,
              lot.estimated_unit_cost_snapshot)
    result = submit(db, data, source, payload(source, child))
    assert result['product']['is_composite'] and source.is_composite and peer.is_composite
    assert source.box_style == peer.box_style == 'BOM组合'
    assert source.unit == peer.unit == '套'
    assert [db.get(ProductBomProfile, p.id).source for p in (source, peer)] == ['assembled'] * 2
    peers = list(db.scalars(select(Product).where(Product.product_code == child.product_code)))
    assert len(peers) == 2 and all(p.is_internal_component for p in peers)
    assert len({db.get(SharedFinishedMember, p.id).group_id for p in peers}) == 1
    for parent in (source, peer):
        edge = db.scalar(select(ProductBomComponent).where(ProductBomComponent.parent_product_id == parent.id))
        assert db.get(Product, edge.component_product_id).customer_id == parent.customer_id
        assert edge.quantity_per_set == 2
    assert (source.sale_unit_price, peer.sale_unit_price) == (5, 8)
    assert (source.sale_unit_price_no_tax, peer.sale_unit_price_no_tax) == (4, 7)
    assert frozen == (item.unit_price, item.subtotal, item.quantity, lot.quantity_available,
                     lot.quantity_reserved, lot.finished_detail.physical_basis_json, lot.estimated_unit_cost_snapshot)
    assert shared.reserved_match(db, reservation, lot, product_id=peer.id, customer_id=peer.customer_id)
    assert shared.match(db, lot, product_id=peer.id, customer_id=peer.customer_id) is None
    # The source is now the other customer: structural changes must be bidirectional.
    reverse = deepcopy(ordinary)
    reverse.update(customer_id=peer.customer_id, product_name=peer.product_name,
                   sale_unit_price=peer.sale_unit_price, sale_unit_price_no_tax=peer.sale_unit_price_no_tax)
    submit(db, data, peer, payload(peer, ordinary=reverse))
    assert not source.is_composite and not peer.is_composite
    assert all(db.get(ProductBomProfile, p.id) is None for p in (source, peer))
    assert db.query(ProductBomComponent).count() == 0
    assert all(not p.is_internal_component for p in peers)
    assert (source.sale_unit_price, peer.sale_unit_price) == (5, 8)
    edit(db, data, peer, {'sale_unit_price': Decimal('9')})
    db.commit()
    assert (source.sale_unit_price, peer.sale_unit_price) == (5, 9)
    assert frozen == (item.unit_price, item.subtotal, item.quantity, lot.quantity_available,
                     lot.quantity_reserved, lot.finished_detail.physical_basis_json, lot.estimated_unit_cost_snapshot)


def test_failed_shared_conversion_rolls_back_new_child_customer_and_every_version(case, monkeypatch):
    db, data, source, peer, child, item, lot = case
    before = state(db, source, peer, lot)
    original = api._update_product_bom
    def fail(product_id, *a, **kw):
        value = original(product_id, *a, **kw)
        if product_id == peer.id:
            raise RuntimeError('failure after the second customer BOM')
        return value
    monkeypatch.setattr(api, '_update_product_bom', fail)
    with pytest.raises(RuntimeError):
        submit(db, data, source, payload(source, child))
    db.rollback()
    assert state(db, source, peer, lot) == before


def test_customer_scope_is_not_bypassed(case, monkeypatch):
    db, data, source, peer, child, item, lot = case
    before = state(db, source, peer, lot)
    original = api.require_customer_access
    def scoped(customer_id, **kw):
        if customer_id == peer.customer_id:
            raise HTTPException(403, 'outside customer scope')
        return original(customer_id, **kw)
    monkeypatch.setattr('app.api.deps.require_customer_access', scoped)
    with pytest.raises(HTTPException) as exc:
        submit(db, data, source, payload(source, child))
    assert exc.value.status_code == 403
    db.rollback()
    assert state(db, source, peer, lot) == before


@pytest.mark.parametrize('price', [None, Decimal('17')])
def test_existing_child_reference_keeps_independent_price_including_blank(case, price):
    db, data, source, peer, child, item, lot = case
    values = {k: v for k, v in api._product_payload_snapshot(child).items() if hasattr(Product, k)}
    values.update(customer_id=peer.customer_id, sale_unit_price=price, sale_unit_price_no_tax=None)
    counterpart = Product(**values)
    db.add(counterpart)
    db.commit()
    submit(db, data, source, payload(source, child))
    assert counterpart.sale_unit_price == price and counterpart.sale_unit_price_no_tax is None
    assert child.sale_unit_price == 2


@pytest.mark.parametrize('existing_peer', [False, True])
def test_internal_child_without_shared_scope_is_mapped_after_both_boms_exist(case, existing_peer):
    db, data, source, peer, child, item, lot = case
    child.is_internal_component = True
    if existing_peer:
        values = {k: v for k, v in api._product_payload_snapshot(child).items() if hasattr(Product, k)}
        values.update(customer_id=peer.customer_id, sale_unit_price=Decimal('17'))
        counterpart = Product(**values)
        db.add(counterpart)
    db.commit()
    submit(db, data, source, payload(source, child))
    rows = list(db.scalars(select(Product).where(Product.product_code == child.product_code)))
    assert len(rows) == 2 and all(p.is_internal_component for p in rows)
    assert len({db.get(SharedFinishedMember, p.id).group_id for p in rows}) == 1
    if existing_peer:
        assert counterpart.sale_unit_price == 17
    assert child.sale_unit_price == 2
    assert (source.sale_unit_price, peer.sale_unit_price) == (5, 8)


@pytest.mark.parametrize('invalid', ['stale_source', 'disabled_group', 'different_child'])
def test_invalid_conversion_has_no_partial_writes(case, invalid):
    from app.models.shared_finished_stock import SharedFinishedGroup
    db, data, source, peer, child, item, lot = case
    request = payload(source, child)
    if invalid == 'stale_source':
        request.product.expected_version += 1
        request.bom.expected_version += 1
    elif invalid == 'disabled_group':
        db.get(SharedFinishedGroup, db.get(SharedFinishedMember, source.id).group_id).enabled = False
    else:
        values = {k: v for k, v in api._product_payload_snapshot(child).items() if hasattr(Product, k)}
        values.update(customer_id=peer.customer_id, report_length_mm=999)
        db.add(Product(**values))
    db.commit()
    before = state(db, source, peer, lot)
    with pytest.raises(Exception) as exc:
        submit(db, data, source, request)
    assert getattr(exc.value, 'status_code', None) in (400, 409)
    db.rollback()
    assert state(db, source, peer, lot) == before


def test_peer_version_race_rejects_entire_conversion(case, monkeypatch):
    db, data, source, peer, child, item, lot = case
    before = state(db, source, peer, lot)
    original = api._update_product
    def racing(*a, **kw):
        result = original(*a, **kw)
        db.execute(update(Product).where(Product.id == peer.id).values(version=Product.version + 1))
        return result
    monkeypatch.setattr(api, '_update_product', racing)
    with pytest.raises(HTTPException) as exc:
        submit(db, data, source, payload(source, child))
    assert exc.value.status_code == 409
    db.rollback()
    assert state(db, source, peer, lot) == before


def test_final_audit_failure_rolls_back_all_shared_facts(case, monkeypatch):
    from app.services import audit_log
    db, data, source, peer, child, item, lot = case
    before = state(db, source, peer, lot)
    original = audit_log.append_audit_event
    def fail(*a, **kw):
        if kw.get('action_code') == 'shared_structure_edit':
            raise RuntimeError('final audit failure')
        return original(*a, **kw)
    monkeypatch.setattr(audit_log, 'append_audit_event', fail)
    with pytest.raises(RuntimeError):
        submit(db, data, source, payload(source, child))
    db.rollback()
    assert state(db, source, peer, lot) == before


def test_replayed_original_request_does_not_create_another_customer_reference(case):
    db, data, source, peer, child, item, lot = case
    request = payload(source, child)
    submit(db, data, source, request)
    before = state(db, source, peer, lot)
    with pytest.raises(HTTPException) as exc:
        submit(db, data, source, request)
    assert exc.value.status_code == 409
    db.rollback()
    assert state(db, source, peer, lot) == before


def test_old_editor_cannot_leave_an_active_bom_behind_an_ordinary_box_style(case):
    db, data, source, peer, child, item, lot = case
    ordinary = api._product_payload_snapshot(source)
    submit(db, data, source, payload(source, child))
    before = state(db, source, peer, lot)
    request = payload(source, ordinary=ordinary)
    with pytest.raises(HTTPException) as exc:
        api._update_product(source.id, request.product, db, data['admin'])
    assert exc.value.status_code == 422
    db.rollback()
    assert state(db, source, peer, lot) == before


def test_direct_one_sided_role_write_still_rejected(case):
    db, data, source, peer, child, item, lot = case
    before = state(db, source, peer, lot)
    with pytest.raises(HTTPException, match='BOM'):
        edit(db, data, source, {'is_composite': True})
    db.rollback()
    assert state(db, source, peer, lot) == before


def test_existing_bom_to_ordinary_http_save_keeps_frozen_order_graph(
        composite_requisition_app, _p181_published_map_identity):
    from fastapi.encoders import jsonable_encoder
    from fastapi.testclient import TestClient
    from app.models.user import User
    from app.services import customer_sharing
    from app.services.multilevel_bom_orders import read_compiled_order_bom
    from app.services.multilevel_bom_snapshot import dump_graph
    from tests.test_shared_bom_stock import seed_pair, activate
    app, factory = composite_requisition_app
    app.include_router(api.router, prefix='/api/master/products')
    seed_pair(factory)
    with factory() as db:
        actor = db.get(User, 1)
        for pids in ([2,5],[3,6],[1,4]):
            gid = activate(db, pids)
            customer_sharing.activate_master(db, gid, actor)
        source, peer = db.get(Product, 1), db.get(Product, 4)
        source.sale_unit_price, peer.sale_unit_price = Decimal('5'), Decimal('8')
        ordinary = api._product_payload_snapshot(db.get(Product, 2))
        ordinary.update(customer_id=source.customer_id, product_code=source.product_code,
                        customer_material_code=source.customer_material_code,
                        product_name=source.product_name, sale_unit_price=source.sale_unit_price)
        db.commit()
        graph = dump_graph(read_compiled_order_bom(db, 1).graph)
        body = jsonable_encoder(payload(source, ordinary=ordinary).model_dump())
    with TestClient(app) as client:
        _login(client)
        response = client.put('/api/master/products/1/with-bom', json=body)
        if response.status_code == 409 and isinstance(response.json()['detail'], dict):
            body['product']['confirmation_token'] = response.json()['detail']['confirmation_token']
            response = client.put('/api/master/products/1/with-bom', json=body)
        assert response.status_code == 200, response.text
    with factory() as db:
        assert dump_graph(read_compiled_order_bom(db, 1).graph) == graph
        assert not db.get(Product, 1).is_composite and not db.get(Product, 4).is_composite
        assert (db.get(Product, 1).sale_unit_price, db.get(Product, 4).sale_unit_price) == (5, 8)
