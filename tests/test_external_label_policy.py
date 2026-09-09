from pathlib import Path

from fastapi.testclient import TestClient
from test_p1_40a_packaging_masterdata import p1_40a_app, _external_payload, _login
from test_p1_106_composite_delivery_labels import delivery_label_api


def test_external_label_create_edit_reread_and_legacy_preservation(p1_40a_app):
    with TestClient(p1_40a_app) as client:
        _login(client)
        payload = _external_payload(p1_40a_app.state.fixture)
        created = client.post('/api/master/products', json=payload)
        assert created.status_code == 201, created.text
        first = created.json()
        assert first['production_label_enabled'] is True
        assert first['production_label_units_per_label'] == 50
        url = f"/api/master/products/{first['id']}"
        payload.update(expected_version=first['version'], production_label_units_per_label=20)
        saved = client.put(url, json=payload)
        assert saved.status_code == 200, saved.text
        current = client.get(url).json()
        assert current['production_label_units_per_label'] == 20
        assert current['version'] == first['version'] + 1
        for key in ('supply_mode', 'external_packaging_default_order_quantity_basis', 'external_packaging_default_purchase_quantity_basis', 'external_packaging_specification_summary'):
            assert current[key] == first[key]
        stale = client.put(url, json=payload)
        assert stale.status_code == 409
        legacy = {key: value for key,value in payload.items() if key not in ('production_label_enabled','production_label_units_per_label')}
        legacy['expected_version'] = current['version']
        preserved = client.put(url,json=legacy)
        assert preserved.status_code == 200, preserved.text
        assert preserved.json()['production_label_units_per_label'] == 20
        payload.update(expected_version=preserved.json()['version'], production_label_units_per_label=0)
        assert client.put(url,json=payload).status_code == 422
        payload.update(production_label_enabled=False,production_label_units_per_label=None)
        preview=client.post(url+'/update-preview',json=payload)
        assert preview.status_code == 200,preview.text
        payload['confirmation_token']=preview.json()['confirmation_token']
        disabled=client.put(url,json=payload)
        assert disabled.status_code == 200,disabled.text
        assert disabled.json()['production_label_enabled'] is False
        assert disabled.json()['production_label_units_per_label'] is None


def test_external_delivery_labels_follow_current_units_without_rewriting_goods(delivery_label_api):
    from app.models.product import Product
    from app.models.delivery import Delivery
    from app.services.production_packaging_label import build_delivery_packaging_label_package
    fixture=delivery_label_api
    with fixture['session_factory']() as db:
        product=db.get(Product,fixture['product_id'])
        delivery=db.get(Delivery,fixture['delivery_id'])
        product.supply_mode='external_purchase'
        product.box_style='其他'
        product.external_packaging_category_code='other_packaging'
        product.external_packaging_specification_summary='外购材料规格'
        product.external_packaging_purchase_unit='片'
        product.external_packaging_specification_json='{"summary":"外购材料规格"}'
        product.external_packaging_candidate_snapshot_json='[]'
        product.external_packaging_default_order_quantity_basis=1
        product.external_packaging_default_purchase_quantity_basis=1
        product.production_label_enabled=True
        product.production_label_units_per_label=50
        db.flush()
        old=build_delivery_packaging_label_package(db,delivery)
        product.production_label_units_per_label=20
        product.version+=1
        db.flush()
        new=build_delivery_packaging_label_package(db,delivery)
        assert not new['review_required']
        assert old['plans'][0]['units_per_label']==50
        assert new['plans'][0]['units_per_label']==20
        assert new['plans'][0]['total_quantity']==old['plans'][0]['total_quantity']
        assert sum(new['plans'][0]['label_quantities'])==old['plans'][0]['total_quantity']
        assert new['plans'][0]['label_count']>old['plans'][0]['label_count']


def test_external_label_controls_are_visible_and_payload_does_not_clear_them():
    source=(Path(__file__).resolve().parents[1]/'static/index.html').read_text(encoding='utf8')
    assert '<div class="product-production-label-config">' in source
    assert 'payload.production_label_enabled=false; payload.production_label_units_per_label=null;' not in source
