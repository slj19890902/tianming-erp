from fastapi.testclient import TestClient
from test_p1_29_warehouse_twin_dashboard import twin_dashboard_app, _login


def test_pending_receipts_are_separate_current_scoped_and_cancelled_receipts_reappear(twin_dashboard_app):
    from datetime import date
    from sqlalchemy import select
    from app.api.deps import get_db
    from app.models.product import Product
    from app.models.delivery import Delivery, DeliveryItem
    from app.models.finance import ReturnReceipt
    from app.services.warehouse_pending_receipts import pending_receipt_search
    app, ids = twin_dashboard_app
    dependency = app.dependency_overrides[get_db]()
    db = next(dependency)
    try:
        product = db.scalar(select(Product).where(Product.customer_id == ids['owner']))
        for index, (status, receipt, owner) in enumerate([
            ('dispatched', None, ids['owner']), ('dispatched', 'confirmed', ids['owner']),
            ('dispatched', 'cancelled', ids['owner']), ('pending', None, ids['owner']),
            ('voided', None, ids['owner']), ('dispatched', None, ids['hidden_owner'])]):
            delivery = Delivery(delivery_number=f'SEARCH405-{index}', customer_id=owner,
                delivery_date=date.today(), status=status, source_mode='unordered_finished')
            db.add(delivery); db.flush()
            db.add(DeliveryItem(delivery_id=delivery.id, source_type='unordered_finished',
                product_id=product.id, product_code_snapshot='SEARCH405-PRODUCT',
                product_name_snapshot='查货回单测试', unit_snapshot='套', price_source='test',
                delivered_quantity=12))
            if receipt:
                db.add(ReturnReceipt(delivery_id=delivery.id, actual_received_date=date.today(), status=receipt))
        db.flush()
        rows = pending_receipt_search(db, keyword='SEARCH405', visible_customer_ids={ids['owner']})
        assert {row['delivery_number'] for row in rows} == {'SEARCH405-0', 'SEARCH405-2'}
        assert all(row['quantity'] == 12 and row['unit'] == '套' and row['counts_as_factory_stock'] is False for row in rows)
        assert pending_receipt_search(db, keyword='SEARCH405', visible_customer_ids=set()) == []
        assert len(pending_receipt_search(db, keyword='SEARCH405', visible_customer_ids=None)) == 3
    finally:
        db.rollback()
        dependency.close()


def test_inventory_search_includes_materials_but_floor_and_customer_scope_remain(twin_dashboard_app):
    app, _ = twin_dashboard_app
    with TestClient(app) as client:
        _login(client, "twin-admin")
        params = dict(keyword="K=A", search_type="inventory", search_floor="ALL")
        response = client.get("/api/warehouse/twin-operations/locate", params=params)
        assert response.status_code == 200, response.text
        rows = response.json()["items"]
        assert rows and all(row["inventory_usage"] == "raw_material" for row in rows)
        assert all(row["quantity"] == row["available_quantity"] + row["reserved_quantity"] + row["damaged_quantity"] for row in rows)
        response = client.get("/api/warehouse/twin-operations/locate", params={**params, "search_floor": "3F"})
        assert response.status_code == 200
        assert response.json()["items"] == []
        response = client.get("/api/warehouse/twin-operations/locate", params={**params, "search_type": "finished"})
        assert response.json()["items"] == []
        _login(client, "twin-scoped")
        response = client.get("/api/warehouse/twin-operations/locate", params={**params, "keyword": "HIDDEN"})
        assert response.status_code == 200
        assert response.json()["items"] == []
