"""Product editors keep existing shared-recipe access, without enrolment rights."""
from fastapi import HTTPException
import pytest

from app.api.deps import has_permission
from app.models.product_bom import ProductBomComponent
from tests.test_shared_finished_stock import reservation_db
from tests.test_shared_structure_edit_20261024 import case, payload, state, submit


def test_sales_editor_can_update_existing_shared_recipe_without_resetting_membership(case):
    db, data, source, peer, child, item, lot = case
    submit(db, data, source, payload(source, child))
    actor = data['admin']
    actor.role = 'sales'
    actor.customer_access_mode = 'all'
    db.commit()
    assert has_permission(actor, 'products.edit')
    request = payload(source, child)
    request.bom.components[0].quantity_per_set = 3
    submit(db, data, source, request)
    assert [row.quantity_per_set for row in db.query(ProductBomComponent).all()] == [3, 3]
    assert (source.sale_unit_price, peer.sale_unit_price) == (5, 8)


def test_sales_editor_cannot_enrol_new_child_references(case):
    db, data, source, peer, child, item, lot = case
    actor = data['admin']
    actor.role = 'sales'
    actor.customer_access_mode = 'all'
    db.commit()
    assert has_permission(actor, 'products.edit')
    before = state(db, source, peer, lot)
    with pytest.raises(HTTPException) as exc:
        submit(db, data, source, payload(source, child))
    assert exc.value.status_code == 403
    db.rollback()
    assert state(db, source, peer, lot) == before


def test_conflicting_child_returns_http_conflict_instead_of_500(case):
    from fastapi import FastAPI
    from fastapi.encoders import jsonable_encoder
    from fastapi.testclient import TestClient
    from app.api import products as api
    from app.api.deps import get_current_user, get_db
    from app.models.product import Product

    db, data, source, peer, child, item, lot = case
    fields = {key: value for key, value in api._product_payload_snapshot(child).items()
              if hasattr(Product, key)}
    fields.update(customer_id=peer.customer_id, report_length_mm=999)
    db.add(Product(**fields))
    db.commit()
    before = state(db, source, peer, lot)
    app = FastAPI()
    app.include_router(api.router, prefix='/api/master/products')
    def test_db():
        yield db
    app.dependency_overrides[get_db] = test_db
    app.dependency_overrides[get_current_user] = lambda: data['admin']
    body = jsonable_encoder(payload(source, child).model_dump())
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.put(f'/api/master/products/{source.id}/with-bom', json=body)
        if response.status_code == 409 and isinstance(response.json()['detail'], dict):
            body['product']['confirmation_token'] = response.json()['detail']['confirmation_token']
            response = client.put(f'/api/master/products/{source.id}/with-bom', json=body)
    assert response.status_code == 409, response.text
    assert isinstance(response.json()['detail'], str)
    db.rollback()
    assert state(db, source, peer, lot) == before
