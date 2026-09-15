from types import SimpleNamespace

from app.services.product_readiness import product_readiness


def test_missing_order_board_fields_are_actionable():
    result = product_readiness(SimpleNamespace(product_code='TEST', product_name='Box'))
    assert '主料报料长未填写' in result['order_save_missing_labels']
    assert '主料报料宽未填写' in result['order_save_missing_labels']
    assert '材质主数据未启用' in result['order_save_missing_labels']


def test_external_and_virtual_do_not_require_board_dimensions():
    for fields in ({'supply_mode': 'external_purchase'}, {'is_virtual_composite_parent': True}):
        assert product_readiness(SimpleNamespace(**fields))['order_save_missing_labels'] == []


def test_telescoping_requires_base_and_supplier():
    product = SimpleNamespace(product_code='TEST', product_name='Box', box_style='天地盖',
        report_length_mm=800, report_width_mm=500,
        material=SimpleNamespace(code='A6A', is_active=True, supplier_name=''))
    missing = product_readiness(product)['order_save_missing_labels']
    assert '底料报料长未填写' in missing and '底料报料宽未填写' in missing
    assert '材质供应商未填写' in missing


def test_assembled_parent_uses_its_bom_not_own_board(tmp_path):
    from tests.test_phase16_pdf_order_import import _order_import_app
    from app.api.deps import get_db
    from app.models.product import Product
    from app.models.multilevel_bom import ProductBomProfile
    app = _order_import_app(tmp_path)
    sessions = app.dependency_overrides[get_db]()
    db = next(sessions)
    db.add(ProductBomProfile(product_id=1, source='assembled'))
    assert product_readiness(db.get(Product, 1))['order_save_missing_labels'] == []
    db.rollback()
    sessions.close()


def test_pdf_api_rejects_all_missing_lines_without_creating_order(tmp_path):
    from fastapi.testclient import TestClient
    from sqlalchemy import select, func
    from app.models.order import Order
    from tests.test_phase16_pdf_order_import import _order_import_app, _signed_pdf_preview_token, _pdf_order_payload, _database_scalar
    app = _order_import_app(tmp_path)
    payload = _pdf_order_payload(confirmed=True, token=_signed_pdf_preview_token(app))
    payload['items'].append({**payload['items'][0], 'product_id': 2, 'product_code': '21308002'})
    with TestClient(app) as client:
        assert client.post('/api/auth/login', json={'username':'sales', 'password':'RolePass123!'}).status_code == 200
        response = client.post('/api/orders', json=payload)
    assert response.status_code == 400, response.text
    message = response.json()['detail']
    assert '第1行【21312009】' in message and '第2行【21308002】' in message
    assert '报料长' in message and '报料宽' in message and '常用箱' in message
    assert _database_scalar(app, select(func.count(Order.id))) == 0
    from app.api.deps import get_db
    from app.models.product import Product
    from app.models.material import Material
    from app.models.supplier import Supplier
    from app.services.supplier_master import normalize_supplier_identity
    sessions = app.dependency_overrides[get_db]()
    db = next(sessions)
    db.add(Supplier(standard_name='Test supplier', normalized_name=normalize_supplier_identity('Test supplier'), is_active=True))
    material = Material(code='A6A', supplier_name='Test supplier', is_active=True, layer_count=3, flute_type='A')
    db.add(material)
    db.flush()
    for product_id in (1, 2):
        product = db.get(Product, product_id)
        product.material_id = material.id
        product.report_length_mm = 800
        product.report_width_mm = 600
        product.layer_count = 3
        product.flute_type = 'A'
    db.commit()
    sessions.close()
    with TestClient(app) as client:
        client.post('/api/auth/login', json={'username':'sales', 'password':'RolePass123!'})
        response = client.post('/api/orders', json=payload)
    assert response.status_code == 201, response.text
    assert _database_scalar(app, select(func.count(Order.id))) == 1
