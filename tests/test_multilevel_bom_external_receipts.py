import json
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func

from app.models.product import Product
from app.models.order import Order, OrderItem
from app.models.user import User
from app.models.supplier import ExternalPackagingProduct
from app.models.multilevel_bom import OrderBomExternalComponent
from app.models.order_external_packaging import SalesOrderItemExternalComponentCandidate
from app.models.external_packaging_purchase import ExternalPackagingPurchaseItem, ExternalPackagingReceipt
from app.services.multilevel_bom_external_freeze import freeze_order_procurement
from tests.test_multilevel_bom_master import save
from test_p1_33c3_external_packaging_purchase_confirmation import purchase_app
from test_p1_33c5_external_packaging_receiving import _login, _confirm


def prepare(app):
    with app.state.session_factory() as db:
        actor = db.scalar(select(User).where(User.username == 'purchase-admin'))
        old = db.get(Order, app.state.fixture['order_id'])
        external = db.get(ExternalPackagingProduct, app.state.fixture['not_frozen_product_id'])
        root = Product(customer_id=old.customer_id, product_code='GRAPH-ROOT', customer_material_code='GRAPH-ROOT', product_name='成套产品', unit='套')
        child = Product(customer_id=old.customer_id, product_code='GRAPH-CHILD', customer_material_code='GRAPH-CHILD', product_name='真实子件', unit='套',
            supply_mode='external_purchase', external_packaging_category_code=external.category_code,
            external_packaging_specification_json=external.specification_json,
            external_packaging_specification_summary='测试规格', external_packaging_purchase_unit=external.purchase_unit,
            external_packaging_default_order_quantity_basis=1, external_packaging_default_purchase_quantity_basis=3,
            external_packaging_candidate_snapshot_json=json.dumps([dict(external_product_id=external.id,
                external_product_version=external.version, supplier_id=external.supplier_id,
                supplier_name='供应商甲', product_name=external.product_name,
                supplier_product_code=external.supplier_product_code, purchase_unit=external.purchase_unit,
                customer_scope_id=old.customer_id, is_default=True)]))
        db.add_all([root, child])
        db.flush()
        save(db, actor, root.id, 'assembled', [(child.id, 2, 'assembly')])
        order = Order(order_number='GRAPH-RECEIPT', customer_id=old.customer_id, order_date=date(2026,9,10))
        db.add(order)
        db.flush()
        item = OrderItem(order_id=order.id, product_id=root.id, quantity=10, unit_price=Decimal(5),
            subtotal=Decimal(50), snapshot_product_name=root.product_name)
        db.add(item)
        db.flush()
        freeze_order_procurement(db, order_item_id=item.id, actor=actor)
        db.commit()
        return order.id, item.id, child.id


def receive(client, purchase_id, line_id, key, quantity):
    return client.post(f'/api/external-packaging-purchases/{purchase_id}/receipts',
        json=dict(idempotency_key=key, lines=[dict(purchase_item_id=line_id, received_quantity=str(quantity))]))


def test_actual_receipt_route_converts_cumulatively_and_replays_frozen_facts(purchase_app):
    order_id, item_id, child_id = prepare(purchase_app)
    with TestClient(purchase_app) as client:
        _login(client, 'purchase-admin')
        _confirm(client, order_id)
        with purchase_app.state.session_factory() as db:
            line = db.scalar(select(ExternalPackagingPurchaseItem).where(ExternalPackagingPurchaseItem.sales_order_item_id == item_id))
            assert line.purchase_quantity == 60
            purchase_id, line_id = line.purchase_order_id, line.id
            child = db.get(Product, child_id)
            child.external_packaging_default_purchase_quantity_basis = 9
            child.version += 1
            db.commit()
        first = receive(client, purchase_id, line_id, 'graph-first', 2)
        assert first.status_code == 200, first.text
        fact = first.json()['receipt']['items'][0]
        assert fact['converted_finished_quantity'] == 0
        assert Decimal(fact['loose_remainder_quantity_after']) == 2
        retry = receive(client, purchase_id, line_id, 'graph-first', 2)
        assert retry.status_code == 200 and retry.json()['created'] is False
        assert retry.json()['receipt'] == first.json()['receipt']
        second = receive(client, purchase_id, line_id, 'graph-second', 1)
        assert second.status_code == 200, second.text
        fact = second.json()['receipt']['items'][0]
        assert fact['converted_finished_quantity'] == 1
        assert Decimal(fact['loose_remainder_quantity_after']) == 0
        assert receive(client, purchase_id, line_id, 'graph-first', 1).status_code == 409


@pytest.mark.parametrize('damage', ['missing-link', 'wrong-unit', 'wrong-candidate'])
def test_graph_receipt_identity_failure_writes_no_receipt(purchase_app, damage):
    order_id, item_id, _ = prepare(purchase_app)
    with TestClient(purchase_app) as client:
        _login(client, 'purchase-admin')
        _confirm(client, order_id)
        with purchase_app.state.session_factory() as db:
            line = db.scalar(select(ExternalPackagingPurchaseItem).where(ExternalPackagingPurchaseItem.sales_order_item_id == item_id))
            purchase_id, line_id = line.purchase_order_id, line.id
            if damage == 'missing-link':
                db.delete(db.get(OrderBomExternalComponent, line.order_component_id))
            elif damage == 'wrong-unit':
                line.purchase_unit = '箱'
            else:
                line.order_candidate_id = db.scalar(select(SalesOrderItemExternalComponentCandidate.id).where(
                    SalesOrderItemExternalComponentCandidate.order_component_id != line.order_component_id))
            db.commit()
        response = receive(client, purchase_id, line_id, 'graph-bad', 1)
        assert response.status_code == 409, response.text
        with purchase_app.state.session_factory() as db:
            assert db.scalar(select(func.count()).select_from(ExternalPackagingReceipt)) == 0
