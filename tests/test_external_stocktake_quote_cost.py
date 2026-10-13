import json
from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.core.time_contract import beijing_today
from app.models.external_packaging_price import ExternalPackagingPriceVersion
from app.models.external_packaging_purchase import ExternalPackagingPurchaseItem, ExternalPackagingPurchaseOrder
from app.models.product import Product
from app.models.supplier import ExternalPackagingProduct
from app.models.user import User
from app.models.warehouse_inventory import InventoryLot, WarehouseLocation, InventoryMovement
from app.services.inventory_valuation import resolve_product_cost
from app.services.warehouse_inventory import manual_finished_in
from test_p1_40a_packaging_masterdata import p1_40a_app, _login, _other_packaging_payload
from test_p1_140_external_stock_replenishment import external_stock_app, _seed_external_warning, _login as external_login


def seed_quote(app):
    with TestClient(app) as client:
        _login(client)
        payload = _other_packaging_payload(app.state.fixture)
        payload.update(product_code='EXT-MOLDED', product_name='匿名纸塑产品',
                       external_packaging_default_order_quantity_basis='2',
                       external_packaging_default_purchase_quantity_basis='3')
        created = client.post('/api/master/products', json=payload)
        assert created.status_code == 201, created.text
        product_id = created.json()['id']
    with app.state.factory() as db:
        external = db.get(ExternalPackagingProduct, app.state.fixture['OTHER-GENERAL'])
        admin = db.scalar(select(User).where(User.username == 'p1-40a-admin'))
        price = ExternalPackagingPriceVersion(external_product_id=external.id,
            version_number=1, product_version=external.version,
            specification_snapshot_json=external.specification_json, quote_unit=external.purchase_unit,
            currency='CNY', tax_mode='tax_inclusive', tax_rate=Decimal('.13'), unit_price=Decimal('2.5'),
            effective_from=beijing_today()-timedelta(days=1), tier_prices_json='[]',
            shipping_fee_mode='not_provided', evidence_reference='isolated quotation fixture',
            quote_fingerprint='isolated-stocktake-quote', created_by=admin.id)
        db.add(price); db.commit()
        return product_id, price.id


@pytest.mark.parametrize('tax_mode,tax_amount,expected', [
    ('tax_inclusive', None, '3.7500'),
    ('tax_exclusive', None, '4.2375'),
    ('tax_exclusive', Decimal('.1'), '3.9000'),
])
def test_quoted_external_product_needs_no_purchase_to_resolve_entry_cost(p1_40a_app, tax_mode, tax_amount, expected):
    product_id, price_id = seed_quote(p1_40a_app)
    with p1_40a_app.state.factory() as db:
        price = db.get(ExternalPackagingPriceVersion, price_id)
        price.tax_mode, price.tax_amount_per_unit = tax_mode, tax_amount
        db.flush()
        result = resolve_product_cost(db, db.get(Product, product_id), for_entry=True)
        assert result.estimate is not None, result.missing
        assert result.estimate.unit_cost == Decimal(expected)
        assert result.estimate.detail['price_version_id'] == price_id
        assert result.estimate.detail['estimate_basis'] == 'current_supplier_quote_reference'
        assert db.scalar(select(func.count(ExternalPackagingPurchaseItem.id))) == 0
        assert db.scalar(select(func.count(ExternalPackagingPurchaseOrder.id))) == 0


def test_stocktake_freezes_quote_and_ratio_replay_does_not_reprice_or_create_purchase(p1_40a_app):
    product_id, price_id = seed_quote(p1_40a_app)
    with p1_40a_app.state.factory() as db:
        product = db.get(Product, product_id)
        location = WarehouseLocation(location_code='ISOLATED-EXT', location_name='合成盘点位置', warehouse_type='finished')
        db.add(location); db.flush()
        args = dict(customer_id=product.customer_id, product_id=product.id, location_id=location.id,
            quantity=100, stock_date=beijing_today(), source_type='stocktake', remarks=None,
            operator_id=None, idempotency_key='external-quoted-stocktake')
        lot = manual_finished_in(db, **args)
        assert lot.estimated_unit_cost_snapshot == Decimal('3.7500')
        original = lot.cost_snapshot_detail_json
        price = db.get(ExternalPackagingPriceVersion, price_id)
        price.unit_price = Decimal('20')
        product.external_packaging_default_purchase_quantity_basis = Decimal('5')
        db.flush()
        replay = manual_finished_in(db, **args)
        assert replay.id == lot.id and replay.cost_snapshot_detail_json == original
        assert replay.quantity_available == 100
        assert db.scalar(select(func.count(InventoryLot.id))) == 1
        assert db.scalar(select(func.count(InventoryMovement.id))) == 1
        assert db.scalar(select(func.count(ExternalPackagingPurchaseOrder.id))) == 0
        assert db.scalar(select(func.count(ExternalPackagingPurchaseItem.id))) == 0
        revised = resolve_product_cost(db, product, for_entry=True)
        assert revised.estimate.unit_cost == Decimal('50.0000')


def test_stocktake_reference_uses_base_quote_without_purchase_moq_or_bulk_discount(p1_40a_app):
    product_id, price_id = seed_quote(p1_40a_app)
    with p1_40a_app.state.factory() as db:
        price = db.get(ExternalPackagingPriceVersion, price_id)
        price.moq_quantity, price.packaging_multiple = Decimal('1000'), Decimal('100')
        price.tier_prices_json = json.dumps([{'min_quantity':'1', 'unit_price':'1'}])
        db.flush()
        result = resolve_product_cost(db, db.get(Product, product_id), for_entry=True)
        assert result.estimate is not None, result.missing
        assert result.estimate.unit_cost == Decimal('3.7500')


@pytest.mark.parametrize('issue,expected', [
    ('unlinked', '常用箱'), ('stale', '变化'), ('inactive', '停用'),
    ('other_customer', '其他客户'), ('expired', '有效价格'), ('future', '有效价格'),
    ('currency', '人民币'), ('ratio', '数量基数'), ('unit', '采购单位'),
    ('supplier_inactive', '停用'), ('supplier_category', '供货类别'),
    ('ambiguous', '默认'), ('precision', '精度'),
])
def test_missing_contract_reports_the_actual_step_without_guessing(p1_40a_app, issue, expected):
    product_id, price_id = seed_quote(p1_40a_app)
    with p1_40a_app.state.factory() as db:
        product = db.get(Product, product_id)
        external = db.get(ExternalPackagingProduct, p1_40a_app.state.fixture['OTHER-GENERAL'])
        price = db.get(ExternalPackagingPriceVersion, price_id)
        if issue == 'unlinked': product.external_packaging_candidate_snapshot_json = '[]'
        elif issue == 'stale': external.version += 1
        elif issue == 'inactive': external.is_active = False
        elif issue == 'other_customer': external.customer_scope_id = p1_40a_app.state.fixture['customer_b']
        elif issue == 'expired': price.effective_to = beijing_today()-timedelta(days=1)
        elif issue == 'future': price.effective_from = beijing_today()+timedelta(days=1)
        elif issue == 'currency': price.currency = 'USD'
        elif issue == 'ratio': product.external_packaging_default_purchase_quantity_basis = None
        elif issue == 'unit': product.external_packaging_purchase_unit = '公斤'
        elif issue == 'supplier_inactive': external.supplier.is_active = False
        elif issue == 'supplier_category':
            for category in external.supplier.supply_categories:
                if category.category_code == external.category_code: category.is_active = False
        elif issue == 'ambiguous':
            candidates = json.loads(product.external_packaging_candidate_snapshot_json)
            candidates.append(dict(candidates[0]))
            product.external_packaging_candidate_snapshot_json = json.dumps(candidates)
        elif issue == 'precision': price.unit_price = Decimal('.000001')
        db.flush()
        result = resolve_product_cost(db, product, for_entry=True)
        assert result.estimate is None
        assert expected in '；'.join(result.missing)
        assert db.scalar(select(func.count(InventoryLot.id))) == 0


def test_actual_purchase_reference_and_old_batch_snapshots_remain_authoritative(external_stock_app):
    policy_id, product_id = _seed_external_warning(external_stock_app)
    with TestClient(external_stock_app) as client:
        external_login(client)
        draft = client.get(f'/api/requisition/stock-policies/{policy_id}/replenishment-draft').json()
        created = client.post('/api/requisition/stock-replenishment/orders', json=dict(
            source_type='stock_warning', idempotency_key='legacy-cost-purchase-fixture',
            customer_id=draft['customer_id'], supplier_name=draft['supplier_name'],
            stock_now=False, items=draft['items']))
        assert created.status_code == 201, created.text
    with external_stock_app.state.factory() as db:
        purchase = db.scalar(select(ExternalPackagingPurchaseItem).where(ExternalPackagingPurchaseItem.customer_product_id_snapshot == product_id))
        assert purchase is not None
        product = db.get(Product, product_id)
        original = resolve_product_cost(db, product, for_entry=True)
        assert original.estimate.unit_cost == Decimal('17.0000')
        assert original.estimate.detail['purchase_item_id'] == purchase.id
        db.get(ExternalPackagingPriceVersion, purchase.price_version_id).unit_price = Decimal('100')
        product.external_packaging_default_purchase_quantity_basis = Decimal('7')
        db.flush()
        result = resolve_product_cost(db, product, for_entry=True)
        assert result.estimate.unit_cost == original.estimate.unit_cost
        assert result.estimate.detail['purchase_quantity_basis'] == original.estimate.detail['purchase_quantity_basis']


def test_corner_guard_quote_uses_current_per_meter_price_and_customer_length(p1_40a_app):
    from test_p1_43b_corner_guard_length_pricing import _product_payload
    ids = p1_40a_app.state.fixture
    with TestClient(p1_40a_app) as client:
        _login(client)
        created = client.post('/api/master/products', json=_product_payload(ids,
            customer_id=ids['customer_a'], code='CG-STOCKTAKE', external_product_id=ids['CG-870-G'],
            length_mm=780, sale_unit_price='9'))
        assert created.status_code == 201, created.text
        product_id = created.json()['id']
    with p1_40a_app.state.factory() as db:
        external = db.get(ExternalPackagingProduct, ids['CG-870-G'])
        admin = db.scalar(select(User).where(User.username == 'p1-40a-admin'))
        db.add(ExternalPackagingPriceVersion(external_product_id=external.id,
            version_number=1, product_version=external.version, specification_snapshot_json=external.specification_json,
            quote_unit='米', currency='CNY', tax_mode='tax_exclusive', tax_rate=Decimal('.13'),
            tax_amount_per_unit=Decimal('.1'), unit_price=Decimal('1.1'), effective_from=beijing_today(),
            tier_prices_json='[]', shipping_fee_mode='not_provided', evidence_reference='isolated meter quote',
            quote_fingerprint='stocktake-meter-fixture', created_by=admin.id))
        db.flush()
        result = resolve_product_cost(db, db.get(Product, product_id), for_entry=True)
        assert result.estimate is not None, result.missing
        assert result.estimate.unit_cost == Decimal('.9360')  # (1.1 + .1) × .78
        assert result.estimate.detail['purchase_unit'] == '根'
        assert result.estimate.detail['price_snapshot']['quote_unit'] == '米'
