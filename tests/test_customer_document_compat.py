import json
from types import SimpleNamespace

import pytest
from test_n029_production_integration import n029_delivery_app, _login, _prepare_103_finished_stock


def test_source_candidates_preserve_identifiers_and_do_not_guess_conflicts():
    from app.services.customer_document_fields import source_candidates, document_snapshot
    raw = [{'字段口径': {'C图号':'0632094','E箱型':'A','B使用型番':'TRD-N'}}]
    p = SimpleNamespace(id=1, customer_material_code='80010631', product_name='内部品名',
        remark='【基础资料原始行20260810】'+json.dumps(raw,ensure_ascii=False)+'【基础资料原始行结束】')
    assert source_candidates(p)['values']['customer_drawing_number'] == ['0632094']
    snapshot = document_snapshot(p)
    assert snapshot['customer_material_code'] == '80010631'
    assert snapshot['customer_drawing_number'] == '0632094'
    assert snapshot['basis'] == 'stored_source_reference'
    raw.append({'字段口径': {'C图号':'0632094-1'}})
    p.remark='【基础资料原始行20260810】'+json.dumps(raw)+'【基础资料原始行结束】'
    assert 'customer_drawing_number' in source_candidates(p)['conflicts']
    assert document_snapshot(p)['customer_drawing_number'] is None


def test_explicit_blank_drawing_does_not_revive_source_value():
    from app.services.customer_document_fields import document_snapshot
    p=SimpleNamespace(id=1,customer_material_code='P1',customer_drawing_number='',
        remark='【基础资料原始行20260810】'+json.dumps([{'字段口径':{'C图号':'old'}}])+'【基础资料原始行结束】')
    assert document_snapshot(p)['customer_drawing_number'] == ''


def test_v1_is_unchanged_and_three_v2_presets_round_trip():
    from app.services.delivery_print_templates import default_layout,canonical_json,normalize_layout
    from app.services.customer_delivery_templates import preset_layout
    legacy=default_layout()
    assert normalize_layout(legacy) == legacy
    for key in ('yke','kew','yl'):
        layout=preset_layout(key)
        assert normalize_layout(json.loads(canonical_json(layout))) == layout
        assert layout['catalog_version']=='delivery-print-v2'
    assert preset_layout('yl')['show_prices'] is False
    with pytest.raises(ValueError): preset_layout('unknown')


def test_customer_print_freezes_identity_price_and_keeps_legacy(n029_delivery_app):
    from decimal import Decimal
    from fastapi.testclient import TestClient
    from app.models.product import Product
    from app.models.order import OrderItem
    from app.models.delivery import DeliveryItem
    from app.models.user import User
    from app.services.delivery_print_templates import save_draft, publish_draft
    from app.services.customer_delivery_templates import preset_layout
    from app.services.customer_document_fields import document_snapshot, encode_snapshot
    app, factory, ids = n029_delivery_app
    _prepare_103_finished_stock(factory, ids)
    with factory() as db:
        order = db.get(OrderItem, ids['task_completed'])
        product = db.get(Product, order.product_id)
        product.customer_drawing_number = '0631965-1'
        product.customer_category = 'AT'
        order.customer_document_snapshot_json = encode_snapshot(document_snapshot(product))
        order.unit_price = Decimal('2.14642')
        product_id=product.id
        actor=db.query(User).filter_by(username='n029-admin').one()
        saved=save_draft(db, customer_id=ids['customer'], expected_release_version=0,
            operation_key='customer-test-draft', actor_id=actor.id, layout=preset_layout('yke'))
        publish_draft(db, customer_id=ids['customer'], draft_version=saved['version'],
            expected_release_version=0, operation_key='customer-test-publish',actor_id=actor.id)
        db.commit()
    with TestClient(app) as client:
        _login(client)
        created=client.post('/api/deliveries',json={'customer_id':ids['customer'],
            'items':[{'order_item_id':ids['task_completed'],'delivered_quantity':50,'customer_po':'PO-001'}]})
        assert created.status_code==201, created.text
        did=created.json()['id']
        with factory() as db:
            db.get(Product,product_id).customer_drawing_number='NEW-DRAWING'
            db.get(OrderItem,ids['task_completed']).unit_price=Decimal('99')
            db.commit()
        printed=client.get(f'/api/deliveries/{did}/print?show_prices=true&order_context=海外订单')
        assert printed.status_code==200, printed.text
        data=printed.json()
        row=data['customer_document_rows'][0]
        assert row['customer_drawing_number']=='0631965-1'
        assert row['customer_category']=='AT'
        assert Decimal(row['unit_price'])==Decimal('2.14642')
        assert Decimal(row['amount'])==Decimal('107.32')
        assert data['commercial_quantity']==50
        assert data['order_context']=='海外订单'
        hidden=client.get(f'/api/deliveries/{did}/print?show_prices=false').json()
        assert not hidden['price_display']['shown']
        assert 'total_amount' not in hidden
        assert all('unit_price' not in row and 'amount' not in row for row in hidden['customer_document_rows'])
        event={'idempotency_key':'test-customer-print-event-001','document_hash':hidden['document_hash'],
            'show_prices':False,'order_context':hidden['order_context']}
        requested=client.post(f'/api/deliveries/{did}/customer-print-events',json=event)
        assert requested.status_code==200, requested.text
        assert client.post(f'/api/deliveries/{did}/customer-print-events',json=event).json()==requested.json()
        assert not client.get(f'/api/deliveries/{did}/print').json()['price_display']['shown']
        assert client.post(f'/api/deliveries/{did}/customer-print-events',json={**event,'show_prices':True}).status_code==409
        with factory() as db:
            original=db.query(DeliveryItem).filter_by(delivery_id=did,is_current=True).one().customer_document_snapshot_json
        updated=client.put(f'/api/deliveries/{did}',json={'items':[{'order_item_id':ids['task_completed'],'delivered_quantity':49}]})
        assert updated.status_code==200, updated.text
        with factory() as db:
            current=db.query(DeliveryItem).filter_by(delivery_id=did,is_current=True).one()
            assert current.customer_document_snapshot_json==original


def test_no_price_permission_is_server_enforced_and_components_not_double_counted():
    from decimal import Decimal
    from unittest.mock import Mock, patch
    from fastapi import HTTPException
    from app.services.customer_delivery_print import enrich_customer_print
    from app.services.customer_delivery_templates import preset_layout
    from app.services.customer_document_fields import encode_snapshot
    from app.services.delivery_snapshots import sales_contract
    record=SimpleNamespace(id=1,customer_document_snapshot_json=encode_snapshot({'schema_version':1,'customer_material_code':'Z.001.000139'}),
        sales_contract_json=sales_contract(unit='套',price=Decimal('2.14642'),tax_mode='tax_inclusive',tax_rate=None,source={}))
    db=Mock();db.scalars.return_value=[record];db.scalar.return_value=None
    base={'id':1,'delivery_number':'TEST','delivery_date':'2026-09-22','vehicle_number':'','sender':{},'customer':{},
        'print_template':{'layout':preset_layout('yl')},'items':[{'delivery_item_id':1,'quantity':50,'product_name':'305风机纸箱(含衬板）1:4',
        'actual_goods_lines':[{'line_type':'component','product_code':'衬板','product_name':'衬板','quantity':200}]}]}
    with patch('app.api.orders._can_view_order_sales_amount',return_value=False):
        with pytest.raises(HTTPException) as error:
            enrich_customer_print(db,SimpleNamespace(id=1),base,SimpleNamespace(role='warehouse'),show_prices=True)
        assert error.value.status_code==403
        result=enrich_customer_print(db,SimpleNamespace(id=1),base,SimpleNamespace(role='warehouse'))
    assert result['commercial_quantity']==50
    assert len(result['customer_document_rows'])==2
    assert not result['customer_document_rows'][1]['pricing_included']
    assert 'total_amount' not in result
    assert 'unit_price' not in result['customer_document_rows'][0]
