from fastapi.testclient import TestClient
from sqlalchemy import text
from tests.test_p1_76_mobile_portal import mobile_portal_app,mobile_erp_app,_login


def test_inventory_assistant_is_admin_only_scoped_and_readonly(mobile_portal_app):
    app,ids,factory=mobile_portal_app
    from app.api.inventory_assistant import router
    app.include_router(router,prefix="/api/inventory-assistant")
    with factory() as db:
        before=db.execute(text('select * from inventory_lots order by id')).all()
    with TestClient(app) as client:
        assert client.get('/api/inventory-assistant').status_code==401
        _login(client,'mobile-admin')
        r=client.get('/api/inventory-assistant');assert r.status_code==200,r.text
        data=r.json();assert data['mode']=='local_rules' and data['page_size']==25
        with factory() as db: assert data['total']==db.execute(text('select count(*) from inventory_lots where quantity_available>0')).scalar()
        assert 'no-store' in r.headers['cache-control']
        for row in data['items']:
            if row['product_id']:
                result=client.get(f"/api/inventory-assistant/{row['lot_id']}/orders")
                assert result.status_code==200,result.text
                assert not result.json()['has_more']
                assert row['open_quantity']==sum(item['remaining_quantity'] for item in result.json()['items'])
        assert client.get('/api/inventory-assistant?focus=invalid').status_code==422
        assert client.get('/api/inventory-assistant/999999/orders').status_code==404
        _login(client,'mobile-scoped')
        assert client.get('/api/inventory-assistant').status_code==403
        assert client.get('/api/inventory-assistant/999999/orders').status_code==403
    with factory() as db:assert db.execute(text('select * from inventory_lots order by id')).all()==before
