import json
from types import SimpleNamespace
import pytest
from app.services import customer_document_fields as fields


def test_new_delivery_fills_unresolved_category_from_explicit_master_only():
    original = {'schema_version': 1, 'product_id': 3564, 'customer_category': None,
                'customer_drawing_number': '7942511', 'customer_model': None,
                'missing_labels': ['客户类别'], 'conflicts': ['customer_category', 'customer_model']}
    product = {'id': 3564, 'version': 8, 'customer_category': 'BT'}
    result = fields.supplement_delivery_category(original, product)
    assert result['customer_category'] == 'BT'
    assert result['customer_drawing_number'] == '7942511'
    assert result['customer_model'] is None
    assert result['conflicts'] == ['customer_model']
    assert result['missing_labels'] == []
    assert result['category_source']['product_version'] == 8
    assert original['customer_category'] is None


@pytest.mark.parametrize('category', ['B', '', '   '])
def test_existing_or_explicit_blank_category_is_not_overwritten(category):
    original = {'schema_version': 1, 'product_id': 3564, 'customer_category': category}
    assert fields.supplement_delivery_category(original, {'id': 3564, 'customer_category': 'BT'}) == original


@pytest.mark.parametrize('product', [{'id': 3509, 'customer_category': 'BT'},
                                   {'id': 3564, 'customer_category': None},
                                   {'id': 3564, 'customer_category': ''}])
def test_no_wrong_product_or_legacy_guess(product):
    original = {'schema_version': 1, 'product_id': 3564, 'customer_category': None}
    assert fields.supplement_delivery_category(original, product) == original

from test_n029_production_integration import n029_delivery_app, _login, _prepare_103_finished_stock


def test_order_to_delivery_detail_and_print_share_category_without_rewriting_order(n029_delivery_app):
    from fastapi.testclient import TestClient
    from app.models.product import Product
    from app.models.order import OrderItem
    from app.models.delivery import DeliveryItem
    from app.models.user import User
    from app.services.delivery_print_templates import save_draft, publish_draft
    from app.services.customer_delivery_templates import preset_layout
    app, factory, ids = n029_delivery_app
    _prepare_103_finished_stock(factory, ids)
    with factory() as db:
        order = db.get(OrderItem, ids['task_completed'])
        product = db.get(Product, order.product_id)
        product.customer_category = 'BT'
        product_id = product.id
        old = fields.encode_snapshot({'schema_version': 1, 'product_id': product.id,
             'customer_drawing_number': '7942511', 'customer_category': None,
             'conflicts': ['customer_category'], 'missing_labels': ['客户类别']})
        order.customer_document_snapshot_json = old
        actor = db.query(User).filter_by(username='n029-admin').one()
        saved = save_draft(db, customer_id=ids['customer'], expected_release_version=0,
                          operation_key='category-draft', actor_id=actor.id, layout=preset_layout('yke'))
        publish_draft(db, customer_id=ids['customer'], draft_version=saved['version'],
                      expected_release_version=0, operation_key='category-publish', actor_id=actor.id)
        db.commit()
    with TestClient(app) as client:
        _login(client)
        created = client.post('/api/deliveries', json={'customer_id': ids['customer'],
            'items': [{'order_item_id': ids['task_completed'], 'delivered_quantity': 50}]})
        assert created.status_code == 201, created.text
        did = created.json()['id']
        assert client.get(f'/api/deliveries/{did}').json()['items'][0]['customer_category'] == 'BT'
        with factory() as db:
            assert db.get(OrderItem, ids['task_completed']).customer_document_snapshot_json == old
            db.get(Product, product_id).customer_category = 'LATER'
            db.commit()
        printed = client.get(f'/api/deliveries/{did}/print?show_prices=false').json()
        assert printed['customer_document_rows'][0]['customer_category'] == 'LATER'
        assert printed['customer_document_rows'][0]['customer_drawing_number'] == ''
        updated = client.put(f'/api/deliveries/{did}', json={'items': [
            {'order_item_id': ids['task_completed'], 'delivered_quantity': 49}]})
        assert updated.status_code == 200, updated.text
        assert client.get(f'/api/deliveries/{did}').json()['items'][0]['customer_category'] == 'LATER'
