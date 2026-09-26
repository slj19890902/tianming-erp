from datetime import date
from decimal import Decimal
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from app.api.deps import get_db
from app.models.order import Order, OrderItem
from app.models.warehouse_inventory import InventoryLot
from test_p1_29_warehouse_twin_dashboard import twin_dashboard_app, _login
from test_opt002_warehouse_search_paging import large_search_app


def test_actual_200_order_100_location_totals_scope_and_read_only(twin_dashboard_app):
    app, ids = twin_dashboard_app
    dependency = app.dependency_overrides[get_db]()
    db = next(dependency)
    try:
        lot = db.scalar(select(InventoryLot).where(InventoryLot.lot_number == 'FG-OWNER'))
        lot.quantity_available, lot.quantity_reserved = 80, 100
        for status, quantity in [('pending_production', 100), ('cancelled', 999)]:
            order = Order(order_number='QUANTITY-'+status, customer_id=ids['owner'],
                order_date=date(2026,9,26), status=status)
            db.add(order); db.flush()
            db.add(OrderItem(order_id=order.id, product_id=lot.finished_detail.product_id,
                quantity=quantity, delivered_quantity=0, unit_price=Decimal('1'), subtotal=quantity,
                sales_unit_snapshot='只', snapshot_product_name='测试成品', snapshot_product_code='TM-FG-001'))
        db.commit()
        sqls = []
        def capture(_conn, _cursor, sql, *_):
            sqls.append(sql)
        with TestClient(app) as client:
            _login(client, 'twin-scoped')
            event.listen(db.get_bind(), 'before_cursor_execute', capture)
            try:
                result = client.get(f'/api/warehouse/twin-operations/product-quantities/{lot.id}')
                assert result.status_code == 200, result.text
                data = result.json()
                assert data['total_quantity'] == 200
                assert data['reserved_quantity'] == 100
                assert data['order_totals'] == [dict(unit='只', ordered_quantity=100,
                    delivered_quantity=0, remaining_quantity=100)]
                assert sum(r['quantity'] for r in data['items']) == 200
                assert len(data['items']) == 3
                assert sum(r['quantity'] for r in data['items'] if r['floor_code']=='UNLOCATED') == 15
                hidden = db.scalar(select(InventoryLot.id).where(InventoryLot.lot_number == 'FG-HIDDEN'))
                assert client.get(f'/api/warehouse/twin-operations/product-quantities/{hidden}').status_code == 404
            finally:
                event.remove(db.get_bind(), 'before_cursor_execute', capture)
        assert not any(s.lstrip().upper().startswith(('INSERT','UPDATE','DELETE')) for s in sqls)
    finally:
        dependency.close()


def test_selected_product_total_is_not_first_search_page(large_search_app):
    app, _, _ = large_search_app
    with TestClient(app) as client:
        _login(client, 'twin-scoped')
        page = client.get('/api/warehouse/twin-operations/locate', params={
            'keyword':'LATE-MATCH', 'search_type':'finished', 'page_size':1}).json()
        assert len(page['items']) == 1 and page['pagination']['has_more']
        result = client.get('/api/warehouse/twin-operations/product-quantities/'+str(page['items'][0]['lot_id']))
        assert result.status_code == 200, result.text
        data = result.json()
        assert data['counts_scope'] == 'whole_product'
        assert data['total_quantity'] == 2747
        assert len(data['items']) == 2610
