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
from tests.test_p1_81_receipt_purpose_flow import _seed_material_and_staging, _p181_published_map_identity


def prepare(app, *, stock_basis=1, purchase_basis=3, two=False, direct=False, quantity=10, body=False):
    with app.state.session_factory() as db:
        actor = db.scalar(select(User).where(User.username == 'purchase-admin'))
        old = db.get(Order, app.state.fixture['order_id'])
        external = db.get(ExternalPackagingProduct, app.state.fixture['not_frozen_product_id'])
        root = Product(customer_id=old.customer_id, product_code='GRAPH-ROOT', customer_material_code='GRAPH-ROOT', product_name='成套产品', unit='套')
        child = Product(customer_id=old.customer_id, product_code='GRAPH-CHILD', customer_material_code='GRAPH-CHILD', product_name='真实子件', unit='套',
            supply_mode='external_purchase', external_packaging_category_code=external.category_code,
            external_packaging_specification_json=external.specification_json,
            external_packaging_specification_summary='测试规格', external_packaging_purchase_unit=external.purchase_unit,
            external_packaging_default_order_quantity_basis=stock_basis, external_packaging_default_purchase_quantity_basis=purchase_basis,
            external_packaging_candidate_snapshot_json=json.dumps([dict(external_product_id=external.id,
                external_product_version=external.version, supplier_id=external.supplier_id,
                supplier_name='供应商甲', product_name=external.product_name,
                supplier_product_code=external.supplier_product_code, purchase_unit=external.purchase_unit,
                customer_scope_id=old.customer_id, is_default=True)]))
        db.add_all([root, child])
        db.flush()
        components = [(child.id, 2, 'assembly')]
        if two:
            second = Product(customer_id=old.customer_id, product_code='GRAPH-SECOND', customer_material_code='GRAPH-SECOND',
                product_name='第二子件', unit='套', **{key:getattr(child, key) for key in (
                    'supply_mode', 'external_packaging_category_code', 'external_packaging_specification_json',
                    'external_packaging_specification_summary', 'external_packaging_purchase_unit',
                    'external_packaging_default_order_quantity_basis', 'external_packaging_default_purchase_quantity_basis',
                    'external_packaging_candidate_snapshot_json')})
            db.add(second)
            db.flush()
            components.append((second.id, 6, 'assembly'))
        if body:
            for key in ('supply_mode', 'external_packaging_category_code', 'external_packaging_specification_json',
                    'external_packaging_specification_summary', 'external_packaging_purchase_unit',
                    'external_packaging_default_order_quantity_basis', 'external_packaging_default_purchase_quantity_basis',
                    'external_packaging_candidate_snapshot_json'):
                setattr(root, key, getattr(child, key))
            save(db, actor, root.id, 'purchased', components)
        elif direct:
            root = child
            save(db, actor, root.id, 'purchased', [])
        else:
            save(db, actor, root.id, 'assembled', components)
        order = Order(order_number='GRAPH-RECEIPT', customer_id=old.customer_id, order_date=date(2026,9,10))
        db.add(order)
        db.flush()
        item = OrderItem(order_id=order.id, product_id=root.id, quantity=quantity, unit_price=Decimal(5),
            subtotal=Decimal(5)*quantity, snapshot_product_name=root.product_name)
        db.add(item)
        db.flush()
        freeze_order_procurement(db, order_item_id=item.id, actor=actor)
        db.commit()
        return order.id, item.id, child.id


def receive(client, purchase_id, line_id, key, quantity):
    return client.post(f'/api/external-packaging-purchases/{purchase_id}/receipts',
        json=dict(idempotency_key=key, lines=[dict(purchase_item_id=line_id, received_quantity=str(quantity))]))


def test_actual_receipt_route_converts_cumulatively_and_replays_frozen_facts(purchase_app, _p181_published_map_identity):
    _seed_material_and_staging(purchase_app.state.session_factory)
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
            child.product_name = '后续主档名称'
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
        retry = receive(client, purchase_id, line_id, 'graph-second', 1)
        assert retry.status_code == 200 and retry.json()['created'] is False
        from app.services.multilevel_bom_receipts import own_output_lots
        from app.models.warehouse_inventory import InventoryMovement
        with purchase_app.state.session_factory() as db:
            lots = own_output_lots(db, item_id)
            assert len(lots) == 1
            lot = lots[0]
            assert lot.quantity_available == 1 and lot.unit == 'boxes'
            assert json.loads(lot.cost_snapshot_detail_json)['stock_unit'] == '套'
            assert lot.finished_detail.product_id == child_id
            assert lot.finished_detail.product_name_snapshot == '真实子件'
            assert lot.source_ref_type == 'bom_external_receipt'
            assert lot.estimated_unit_cost_snapshot == Decimal('29.7000')
            from app.services.bom_subkit_costs import source_cost
            amount, cost = source_cost(db, lot, 1)
            assert amount == Decimal('29.7000') and cost['actual'] is True
            assert [Decimal(p['amount']) for p in cost['sources']] == [Decimal('19.8000'), Decimal('9.9000')]
            assert db.scalar(select(func.count()).select_from(InventoryMovement).where(
                InventoryMovement.inventory_lot_id == lot.id)) == 1


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


@pytest.mark.parametrize('after_stock', [False, True])
def test_stock_failure_rolls_back_receipt_and_inventory(purchase_app, _p181_published_map_identity, monkeypatch, after_stock):
    if after_stock:
        _seed_material_and_staging(purchase_app.state.session_factory)
        import app.services.warehouse_inventory as inventory
        original = inventory.manual_finished_in
        def fail(*args, **kwargs):
            original(*args, **kwargs)
            raise inventory.WarehouseInventoryError('isolated-post-stock-failure', 409)
        monkeypatch.setattr(inventory, 'manual_finished_in', fail)
    order_id, item_id, _ = prepare(purchase_app)
    with TestClient(purchase_app) as client:
        _login(client, 'purchase-admin')
        _confirm(client, order_id)
        with purchase_app.state.session_factory() as db:
            line = db.scalar(select(ExternalPackagingPurchaseItem).where(ExternalPackagingPurchaseItem.sales_order_item_id == item_id))
            purchase_id, line_id = line.purchase_order_id, line.id
        response = receive(client, purchase_id, line_id, 'graph-stock-failure', 3)
        assert response.status_code == 409, response.text
        from app.models.warehouse_inventory import InventoryLot, InventoryMovement
        with purchase_app.state.session_factory() as db:
            for model in (ExternalPackagingReceipt, InventoryLot, InventoryMovement):
                assert db.scalar(select(func.count()).select_from(model)) == 0
