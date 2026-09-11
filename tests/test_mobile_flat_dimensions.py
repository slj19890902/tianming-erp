from fastapi.testclient import TestClient
from sqlalchemy import select
from test_p1_21b_mobile_admin_product_search import mobile_erp_app, _login
from app.models.warehouse_inventory import InventoryLot
from app.models.product import Product


def test_mobile_flat_stock_dimensions_and_erp_fallback(mobile_erp_app):
    from app.api.warehouse import router
    app, ids, factory = mobile_erp_app
    app.include_router(router, prefix='/api/warehouse')
    with factory() as db:
        lot = db.scalar(select(InventoryLot).where(InventoryLot.lot_number == 'FG-MOBILE-001'))
        detail = lot.finished_detail
        detail.length_mm, detail.width_mm, detail.height_mm = 817, 613, None
        product = db.get(Product, detail.product_id)
        product.length_mm, product.width_mm, product.height_mm = 817, 613, None
        lot_id, product_id, customer_id = lot.id, product.id, product.customer_id
        db.commit()
    with TestClient(app) as client:
        _login(client, 'mobile-admin')
        for query in ['817×613', '817*613', '817 x 613']:
            found = client.get('/api/mobile/erp/warehouse/physical-inventory/search', params={
                'customer_id': customer_id, 'inventory_keyword': query})
            assert found.status_code == 200, found.text
            assert lot_id in [row['lot_id'] for row in found.json()['items']], query
            fallback = client.get('/api/warehouse/floor3/product-candidates', params={
                'customer_id': customer_id, 'q': query, 'limit': 10})
            assert fallback.status_code == 200, fallback.text
            assert product_id in [row['product_id'] for row in fallback.json()['items']], query
