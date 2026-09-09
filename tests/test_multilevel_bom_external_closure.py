from decimal import Decimal
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models.order import OrderItem
from app.models.external_packaging_purchase import ExternalPackagingPurchaseItem
from app.models.production import ProductionTask
from app.services.multilevel_bom_receipts import graph_material_receipts_closed
from test_p1_33c3_external_packaging_purchase_confirmation import purchase_app
from tests.test_multilevel_bom_external_receipts import prepare, receive, _login
from tests.test_p1_81_receipt_purpose_flow import _seed_material_and_staging, _p181_published_map_identity


def test_stock_coverage_does_not_close_unreceived_purchase_contract(purchase_app, _p181_published_map_identity):
    factory = purchase_app.state.session_factory
    _seed_material_and_staging(factory)
    order_id, item_id, _ = prepare(purchase_app, stock_basis=3, purchase_basis=1, direct=True, quantity=9)
    with TestClient(purchase_app) as client:
        _login(client, 'purchase-admin')
        preview = client.get(f'/api/orders/{order_id}/external-packaging-purchase').json()
        row = preview['items'][0]
        response = client.post(f'/api/orders/{order_id}/external-packaging-purchase/confirm', json={
            'idempotency_key':'closure-purchase', 'lines':[dict(order_component_id=row['order_component_id'],
                candidate_id=row['default_candidate_id'], purchase_quantity='4')]})
        assert response.status_code == 200, response.text
        with factory() as db:
            purchase = db.scalar(select(ExternalPackagingPurchaseItem).where(ExternalPackagingPurchaseItem.sales_order_item_id == item_id))
            purchase_id, line_id = purchase.purchase_order_id, purchase.id
            assert purchase.purchase_quantity == 4
        response = receive(client, purchase_id, line_id, 'closure-first', 3)
        assert response.status_code == 200, response.text
        with factory() as db:
            item = db.get(OrderItem, item_id)
            assert graph_material_receipts_closed(db, item) is False
            assert item.material_status == 'pending' and item.material_received_at is None
            task = db.scalar(select(ProductionTask).where(ProductionTask.order_item_id == item_id))
            assert task.finished_coverage_snapshot == 9
        response = receive(client, purchase_id, line_id, 'closure-last', 1)
        assert response.status_code == 200, response.text
        with factory() as db:
            item = db.get(OrderItem, item_id)
            assert graph_material_receipts_closed(db, item) is True
            assert item.material_status == 'received' and item.requisition_status == '已入库'
            assert item.material_received_at is not None and item.material_received_by is not None
            confirmed_at = item.material_received_at
        retry = receive(client, purchase_id, line_id, 'closure-last', 1)
        assert retry.status_code == 200 and retry.json()['created'] is False
        with factory() as db:
            assert db.get(OrderItem, item_id).material_received_at == confirmed_at
