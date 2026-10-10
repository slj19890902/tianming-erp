import copy

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from tests.test_stock_replenishment_flow import stock_replenishment_app, _login


@pytest.mark.parametrize('crease_type', ['净料', '净', '毛片', '毛', '其他', '压线'])
def test_warning_draft_save_round_trip_with_legacy_crease_segments(stock_replenishment_app, crease_type):
    from app.models.product import Product
    from app.models.stock_replenishment import InventoryStockPolicy, StockReplenishmentOrder
    from app.models.warehouse_inventory import InventoryLot
    app, factory = stock_replenishment_app
    with factory() as db:
        product = db.get(Product, 1)
        product.crease_type = crease_type
        # An unchanged old product can retain these values after changing sheet type.
        product.crease_left_mm, product.crease_middle_mm, product.crease_right_mm = 335, 160, 335
        policy = InventoryStockPolicy(policy_name='匿名补库预警', target_inventory_type='finished',
            product_id=product.id, customer_id=product.customer_id, warning_quantity=100, target_quantity=200)
        db.add(policy); db.commit(); policy_id = policy.id
        quantities = list(db.execute(select(InventoryLot.id, InventoryLot.quantity_available)))
    with TestClient(app) as client:
        _login(client)
        response = client.get(f'/api/requisition/stock-policies/{policy_id}/replenishment-draft')
        assert response.status_code == 200, response.text
        draft = response.json(); line = draft['items'][0]
        assert draft['draft_ready'] and line['quantity'] > 0
        payload = dict(source_type='stock_warning', idempotency_key='legacy-crease-round-trip',
            supplier_name=draft['supplier_name'], customer_id=draft['customer_id'], stock_now=False, items=[line])
        accepted = client.post('/api/requisition/stock-replenishment/orders', json=payload)
        assert accepted.status_code == 201, accepted.text
        saved = accepted.json()
        assert saved['status'] == 'draft' and saved['stocked_quantity'] == 0
        expected = (335, 160, 335) if crease_type == '压线' else (None, None, None)
        fields = ('crease_left_mm', 'crease_middle_mm', 'crease_right_mm')
        assert tuple(line[k] for k in fields) == expected
        readback = client.get(f"/api/requisition/stock-replenishment/orders/{saved['id']}")
        assert readback.status_code == 200, readback.text
        assert tuple(readback.json()['items'][0][k] for k in fields) == expected
        replay = client.post('/api/requisition/stock-replenishment/orders', json=payload)
        assert replay.status_code == 201 and replay.json()['id'] == saved['id']
        changed = copy.deepcopy(payload); changed['items'][0]['quantity'] += 1
        assert client.post('/api/requisition/stock-replenishment/orders', json=changed).status_code == 409
        stale = copy.deepcopy(payload); stale['idempotency_key'] = 'changed-master-must-fail'
        with factory() as db:
            product = db.get(Product, 1)
            if crease_type == '压线': product.crease_middle_mm += 1
            else: product.report_length_mm += 1
            db.commit()
        rejected = client.post('/api/requisition/stock-replenishment/orders', json=stale)
        assert rejected.status_code == 400 and '主数据一致' in rejected.text
    with factory() as db:
        assert db.scalar(select(func.count(StockReplenishmentOrder.id))) == 1
        assert list(db.execute(select(InventoryLot.id, InventoryLot.quantity_available))) == quantities
        assert db.get(Product, 1).crease_left_mm == 335
