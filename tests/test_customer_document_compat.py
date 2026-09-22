import json
from types import SimpleNamespace

import pytest
from test_n029_production_integration import n029_delivery_app, _login, _prepare_103_finished_stock
from test_a0008_delivery_print_template_designer import template_app


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


def test_v2_template_can_be_saved_published_and_rolled_back_through_http(template_app):
    from fastapi.testclient import TestClient
    from app.services.customer_delivery_templates import preset_layout
    from test_a0008_delivery_print_template_designer import _login as login
    app,_=template_app
    with TestClient(app) as client:
        login(client,'admin')
        for version,preset in enumerate(('yke','kew','yl')):
            body={'customer_id':1,'expected_release_version':version,'operation_key':f'v2-draft-{preset}', 'layout':preset_layout(preset)}
            saved=client.put('/api/system/delivery-print-templates/admin/draft',json=body)
            assert saved.status_code==200,saved.text
            published=client.post('/api/system/delivery-print-templates/admin/publish',json={
                'customer_id':1,'expected_release_version':version,'draft_version':saved.json()['version'],'operation_key':f'v2-publish-{preset}'})
            assert published.status_code==200,published.text
            assert published.json()['layout']==preset_layout(preset)
        rollback=client.post('/api/system/delivery-print-templates/admin/rollback',json={
            'customer_id':1,'source_version':1,'expected_release_version':3,'operation_key':'v2-rollback-yke'})
        assert rollback.status_code==200,rollback.text
        assert rollback.json()['layout']['preset']=='yke'
        bad=client.put('/api/system/delivery-print-templates/admin/draft',json={**body,'customer_id':None,'expected_release_version':0,'operation_key':'v2-global-forbidden'})
        assert bad.status_code==422,bad.text


def test_common_box_customer_fields_save_read_search_and_old_payload_compatibility(template_app):
    from fastapi.testclient import TestClient
    from app.api.products import router,ProductPayload,_product_write_data
    from app.models.user import User
    from test_a0008_delivery_print_template_designer import _login as login
    app,factory=template_app
    app.include_router(router,prefix='/api/products')
    payload={'customer_id':1,'product_code':'INTERNAL-001','customer_material_code':'00012139','product_name':'内部纸箱名',
        'customer_drawing_number':'0631965-1','customer_category':'AT','customer_model':'TRD-N',
        'customer_product_name':'客户用途','sale_unit_price':'2.14642','box_category':'normal'}
    with TestClient(app) as client:
        login(client,'admin')
        response=client.post('/api/products',json=payload)
        assert response.status_code==201,response.text
        product=response.json()
        assert product['customer_document']['customer_drawing_number']=='0631965-1'
        assert product['customer_document']['customer_material_code']=='00012139'
        detail=client.get(f'/api/products/{product["id"]}')
        assert detail.status_code==200,detail.text
        assert detail.json()['customer_category']=='AT'
        search=client.get('/api/products?customer_id=1&keyword=0631965-1&response_mode=summary')
        assert search.status_code==200,search.text
        assert [row['id'] for row in search.json()['items']]==[product['id']]
    with factory() as db:
        actor=db.query(User).filter_by(username='admin').one()
        old=ProductPayload(**{key:value for key,value in payload.items() if key not in ('customer_drawing_number','customer_category','customer_model','customer_product_name')})
        write=_product_write_data(old,actor)
        assert 'customer_drawing_number' not in write
        assert 'customer_category' not in write


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


def test_five_decimal_sale_survives_delivery_receipt_and_statement(n029_delivery_app):
    from datetime import date
    from decimal import Decimal
    from fastapi.testclient import TestClient
    from app.models.order import OrderItem
    from app.models.customer import Customer
    from app.models.finance import StatementItem
    from app.models.product import Product
    app,factory,ids=n029_delivery_app
    _prepare_103_finished_stock(factory,ids)
    with factory() as db:
        item=db.get(OrderItem,ids['task_completed'])
        item.unit_price=Decimal('2.14642')
        db.get(Product,item.product_id).sale_unit_price=Decimal('2.14642')
        db.get(Customer,ids['customer']).statement_cycle_start_day=1
        db.commit();db.refresh(item)
        assert item.unit_price==Decimal('2.14642')
    with TestClient(app) as client:
        _login(client)
        created=client.post('/api/deliveries',json={'customer_id':ids['customer'],
            'items':[{'order_item_id':ids['task_completed'],'delivered_quantity':50}]})
        assert created.status_code==201,created.text
        did=created.json()['id'];line_id=created.json()['items'][0]['id']
        response=client.put(f'/api/deliveries/{did}/dispatch');assert response.status_code==200,response.text
        receipt=client.post('/api/finance/return_receipts',json={'delivery_id':did,'actual_received_date':date.today().isoformat(),
            'items':[{'delivery_item_id':line_id,'actual_received_quantity':50}]})
        assert receipt.status_code==201,receipt.text
        statement=client.post('/api/finance/statements',json={'customer_id':ids['customer'],
            'statement_month':date.today().strftime('%Y-%m'),'delivery_ids':[did]})
        assert statement.status_code==201,statement.text
        with factory() as db:
            row=db.query(StatementItem).filter_by(statement_id=statement.json()['id']).one()
            assert row.unit_price_snapshot==Decimal('2.14642')
            assert row.receivable_amount==Decimal('107.32')
