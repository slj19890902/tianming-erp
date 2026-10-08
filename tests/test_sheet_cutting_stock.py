from copy import deepcopy
from uuid import uuid4

from fastapi.testclient import TestClient

from tests.test_stock_replenishment_flow import stock_replenishment_app, _login


def test_stock_draft_freezes_supplier_size_yield_and_theoretical_crease(stock_replenishment_app):
    from app.models.product import Product
    from app.models.stock_replenishment import StockReplenishmentOrderItem
    app, sessions = stock_replenishment_app
    with sessions() as db:
        product = db.get(Product, 1)
        product.sheet_cutting_settings = {'schema_version': 2, 'whole': {
            'length_parts': 2, 'width_parts': 2, 'mold_count': 1, 'is_die_cut': False}}
        product.default_cutting_mode = '一开四'
        db.commit()
    payload = {'idempotency_key': str(uuid4()), 'source_type': 'customer_request', 'supplier_name': '佳丰', 'stock_now': False,
        'items': [{'target_inventory_type': 'semi_finished', 'customer_id': 1, 'product_id': 1, 'material_id': 1,
            'layer_count': 5, 'flute_type': 'AB', 'report_length_mm': 3730, 'report_width_mm': 1660,
            'crease_type': '压线', 'crease_left_mm': 335, 'crease_middle_mm': 160, 'crease_right_mm': 335,
            'quantity': 30, 'stock_yield_per_sheet': 4, 'location_id': 2}]}
    with TestClient(app) as client:
        _login(client)
        products = client.get('/api/requisition/stock-replenishment/products', params={'customer_id': 1})
        assert products.status_code == 200, products.text
        row = products.json()['items'][0]
        assert row['report_length_mm'] == 3730 and row['report_width_mm'] == 1660
        assert row['output_per_sheet'] == 4
        wrong = deepcopy(payload)
        wrong['items'][0]['stock_yield_per_sheet'] = 1
        rejected = client.post('/api/requisition/stock-replenishment/orders', json=wrong)
        assert rejected.status_code == 409, rejected.text
        saved = client.post('/api/requisition/stock-replenishment/orders', json=payload)
        assert saved.status_code == 201, saved.text
    with sessions() as db:
        row = db.get(StockReplenishmentOrderItem, saved.json()['items'][0]['id'])
        assert row.sheet_cutting_snapshot['theoretical_width_mm'] == '830'
        assert row.sheet_cutting_snapshot['yield_per_supplier_sheet'] == 4
