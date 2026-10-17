from decimal import Decimal
import json

from fastapi.testclient import TestClient

from test_n029_production_integration import n029_delivery_app, _login, _prepare_103_finished_stock


def test_old_delivery_refreshes_descriptions_keeps_sale_and_tracks_print(n029_delivery_app, monkeypatch):
    from app.models.product import Product
    from app.models.order import OrderItem
    from app.models.delivery import DeliveryItem
    from app.models.user import User
    from app.models.finance import FinanceIdempotencyRecord
    from app.services.customer_delivery_templates import preset_layout
    from app.services.delivery_print_templates import save_draft, publish_draft
    app, factory, ids = n029_delivery_app
    _prepare_103_finished_stock(factory, ids)
    with factory() as db:
        order = db.get(OrderItem, ids['task_completed'])
        product = db.get(Product, order.product_id)
        pid = product.id
        product.customer_drawing_number = 'OLD-DRAWING'
        product.customer_category = 'A'
        product.customer_model = 'OLD-MODEL'
        from app.services.customer_document_fields import document_snapshot, encode_snapshot
        order.customer_document_snapshot_json = encode_snapshot(document_snapshot(product))
        order.unit_price = Decimal('2.14642')
        actor = db.query(User).filter_by(username='n029-admin').one()
        draft = save_draft(db, customer_id=ids['customer'], expected_release_version=0,
                           operation_key='live-print-draft', actor_id=actor.id, layout=preset_layout('kew'))
        publish_draft(db, customer_id=ids['customer'], draft_version=draft['version'],
                      expected_release_version=0, operation_key='live-print-publish', actor_id=actor.id)
        db.commit()
    with TestClient(app) as client:
        _login(client)
        created = client.post('/api/deliveries', json={'customer_id': ids['customer'],
            'items': [{'order_item_id': ids['task_completed'], 'delivered_quantity': 50}]})
        assert created.status_code == 201, created.text
        did = created.json()['id']
        with factory() as db:
            record = db.query(DeliveryItem).filter_by(delivery_id=did, is_current=True).one()
            frozen = record.customer_document_snapshot_json
            product = db.get(Product, pid)
            product.customer_drawing_number = '0007942511'
            product.customer_category = 'BT'
            product.customer_model = 'NEW-MODEL'
            product.product_name = '当前纸箱名称'
            product.length_mm, product.width_mm, product.height_mm = 400, 300, 200
            product.sale_unit_price = Decimal('99')
            db.commit()
        data = client.get(f'/api/deliveries/{did}/print?show_prices=true').json()
        row = data['customer_document_rows'][0]
        assert (row['customer_drawing_number'], row['customer_category'], row['customer_model']) == ('0007942511', 'BT', 'NEW-MODEL')
        assert (row['product_name'], row['specification']) == ('当前纸箱名称', '400×300×200mm')
        assert (row['quantity'], Decimal(row['unit_price']), data['total_amount']) == (50, Decimal('2.14642'), '107.32')
        assert data['sender']['company_name'] == '苏州天明包装有限公司'
        assert data['print_template']['layout']['show_headers'] is True
        assert any(c['key'] == 'product_code' and c['label'] == '存货编码' for c in data['print_template']['layout']['columns'])
        assert client.get(f'/api/deliveries/{did}').json()['items'][0]['customer_category'] == 'BT'
        original = client.get(f'/api/deliveries/{did}/print?show_prices=false&description_mode=original').json()
        assert original['customer_document_rows'][0]['customer_drawing_number'] == 'OLD-DRAWING'
        assert 'presentation_version' not in original['print_template']['layout']
        event = dict(idempotency_key='live-print-event-000001', document_hash=data['document_hash'],
                     show_prices=True, description_mode='current', order_context='')
        assert client.post(f'/api/deliveries/{did}/customer-print-events', json=event).status_code == 200
        with factory() as db:
            product = db.get(Product, pid)
            product.customer_drawing_number = ''
            product.customer_model = ''
            db.commit()
        stale = {**event, 'idempotency_key': 'live-print-event-000002'}
        assert client.post(f'/api/deliveries/{did}/customer-print-events', json=stale).status_code == 409
        cleared = client.get(f'/api/deliveries/{did}/print?show_prices=false').json()
        assert cleared['customer_document_rows'][0]['customer_drawing_number'] == ''
        assert cleared['customer_document_rows'][0]['customer_model'] == ''
        for view in ('summary', 'full'):
            listed = client.get('/api/deliveries', params={'view': view}).json()['items']
            assert next(x for x in listed if x['id'] == did)['total_amount'] == '107.32'
        with factory() as db:
            assert db.query(DeliveryItem).filter_by(delivery_id=did, is_current=True).one().customer_document_snapshot_json == frozen
            audit = db.query(FinanceIdempotencyRecord).filter_by(idempotency_key=event['idempotency_key']).one()
            saved = json.loads(audit.response_json)['printed_document']
            assert saved['customer_document_rows'][0]['customer_drawing_number'] == '0007942511'
        monkeypatch.setattr('app.api.orders._can_view_order_sales_amount', lambda user: False)
        hidden = client.get('/api/deliveries?view=summary').json()['items']
        assert all('total_amount' not in x for x in hidden)
        assert client.get(f'/api/deliveries/{did}/print?show_prices=true').status_code == 403
        assert 'unit_price' not in client.get(f'/api/deliveries/{did}/print?show_prices=false').json()['customer_document_rows'][0]
        replay = client.post(f'/api/deliveries/{did}/customer-print-events', json=event)
        assert replay.status_code == 200, replay.text
        assert 'unit_price' not in replay.json()['printed_document']['customer_document_rows'][0]
        assert 'amount' not in replay.json()['printed_document']['customer_document_rows'][0]


def test_current_description_never_uses_another_customer_or_legacy_candidates():
    from types import SimpleNamespace as NS
    from unittest.mock import Mock
    from app.services.delivery_current_presentation import current_descriptions
    record = NS(id=1, product_id=2, order_item_id=None)
    db = Mock()
    db.scalars.return_value = [NS(id=2, customer_id=3, deleted_at=None)]
    assert current_descriptions(db, NS(customer_id=4), [record]) == {}


def test_missing_frozen_price_is_unknown_and_zero_is_valid():
    from types import SimpleNamespace as NS
    from unittest.mock import Mock
    from app.services.delivery_current_presentation import delivery_amounts
    db = Mock()
    db.scalars.return_value = [NS(delivery_id=1, sales_contract_json=None,
                                  unit_price_snapshot=None, delivered_quantity=50),
                               NS(delivery_id=2, sales_contract_json=None,
                                  unit_price_snapshot=Decimal('0'), delivered_quantity=50)]
    result = delivery_amounts(db, [1, 2])
    assert result[1]['total_amount'] is None
    assert result[2]['total_amount'] == '0.00'


def test_generic_template_uses_compact_current_view_and_retains_original():
    from copy import deepcopy
    from app.services.delivery_current_presentation import compact_layout
    from app.services.delivery_print_templates import default_layout
    original = {'layout': default_layout(), 'layout_hash': 'a' * 64}
    before = deepcopy(original)
    result = compact_layout(original)
    assert original == before
    assert result['layout']['preset'] == 'generic'
    assert result['layout']['font_size_pt'] == 11
    assert result['source_layout_hash'] == original['layout_hash']
    assert [c['key'] for c in result['layout']['columns']] == list(original['layout']['column_widths'])


def test_old_priced_print_receipt_is_redacted_without_rewriting_stored_fact():
    from copy import deepcopy
    from app.services.customer_delivery_print import print_request_receipt
    old = {'document_hash': 'a'*64, 'printed_document': {'customer_document_rows': [
        {'customer_drawing_number': '0001234', 'quantity': 50, 'unit_price': '99.00', 'amount': '4950.00', 'tax_mode': 'tax_inclusive'}]}}
    before = deepcopy(old)
    result = print_request_receipt(old)
    assert result['printed_document']['customer_document_rows'] == [{'customer_drawing_number': '0001234', 'quantity': 50}]
    assert old == before
