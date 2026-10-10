import json

import pytest
from fastapi.testclient import TestClient

from tests.test_p0_38_stock_replenishment_print import (
    stock_replenishment_print_app, production_print_app, _login, _package_url, _stock_selection,
)


def setup_mold(fixture, monkeypatch, *, item_key='semi_item_id'):
    from app.models.mold_tool import MoldTool
    from app.models.stock_replenishment import StockReplenishmentOrderItem
    from app.services.stock_purchase_identity import capture
    monkeypatch.setattr('app.services.requisition_production_print.describe_mold_location',
                        lambda value: {'prompt': '一楼 · 模具B架 · B7（第3层第1格）', 'short_label': 'B7'}
                        if value == 'TEST-B7' else {'prompt': value, 'short_label': value})
    with fixture['session_factory']() as db:
        item = db.get(StockReplenishmentOrderItem, fixture[item_key])
        product = item.product or item.reference_product
        product.box_category = 'die_cut'
        product.box_style = '模切内盒'
        mold = MoldTool(mold_code='M-PRINT-B7', mold_name='纸盒模具', rack_location='TEST-B7')
        db.add(mold);db.flush()
        product.mold_tool_id = mold.id
        db.flush()
        item.production_snapshot_json = json.dumps(capture(product, item), default=str)
        db.commit()
        return mold.id, product.id, item.order.id


def package(db, fixture, item_key='semi_item_id'):
    from app.models.stock_replenishment import StockReplenishmentOrderItem
    from app.services.requisition_production_print import build_stock_replenishment_production_package
    item = db.get(StockReplenishmentOrderItem, fixture[item_key])
    return build_stock_replenishment_production_package(db, item.order, selected_item_ids={item.id})


@pytest.mark.parametrize('item_key', ['semi_item_id', 'finished_item_id'])
def test_stock_print_uses_purchase_mold_and_current_short_location(stock_replenishment_print_app, monkeypatch, item_key):
    from app.models.mold_tool import MoldTool
    from app.models.product import Product
    fixture = stock_replenishment_print_app
    mold_id, product_id, _ = setup_mold(fixture, monkeypatch, item_key=item_key)
    with fixture['session_factory']() as db:
        before = package(db, fixture, item_key)
        c = before['cards'][0]['components'][0]
        assert c['mold_tool_id'] == mold_id
        assert c['mold_code'] == 'M-PRINT-B7'
        assert c['mold_location_display'] == 'B7'
        assert c['mold_location_version'] == 1
        assert c['mold_binding_basis'] == 'purchase_snapshot'
        original_quantity = before['cards'][0]['planned_finished_quantity']
        old = db.get(MoldTool, mold_id)
        old.rack_location = 'A2'
        old.location_version += 1
        replacement = MoldTool(mold_code='M-NEW-NOT-PURCHASED', mold_name='新版模具', rack_location='C1')
        db.add(replacement);db.flush()
        db.get(Product, product_id).mold_tool_id = replacement.id
        db.commit()
        after = package(db, fixture, item_key)
        updated = after['cards'][0]['components'][0]
        assert updated['mold_tool_id'] == mold_id  # Purchase identity does not follow a new mold binding.
        assert updated['mold_location_display'] == 'A2'  # The same physical mold's move is current.
        assert updated['mold_location_version'] == 2
        assert after['cards'][0]['planned_finished_quantity'] == original_quantity
        assert after['cards'][0]['selection_fingerprint'] != before['cards'][0]['selection_fingerprint']


@pytest.mark.parametrize('condition', ['unbound', 'missing_location', 'inactive', 'bad_snapshot',
                                       'list_identity', 'list_fields', 'list_physical_basis'])
def test_stock_print_does_not_invent_mold_facts(stock_replenishment_print_app, monkeypatch, condition):
    from app.models.mold_tool import MoldTool
    from app.models.stock_replenishment import StockReplenishmentOrderItem
    fixture = stock_replenishment_print_app
    mold_id, _, _ = setup_mold(fixture, monkeypatch)
    with fixture['session_factory']() as db:
        item = db.get(StockReplenishmentOrderItem, fixture['semi_item_id'])
        frozen = json.loads(item.production_snapshot_json)
        if condition == 'unbound':
            frozen['fields']['mold_tool_id'] = None
            item.production_snapshot_json = json.dumps(frozen)
        elif condition == 'bad_snapshot':
            frozen['customer_id'] = 999999
            item.production_snapshot_json = json.dumps(frozen)
        elif condition.startswith('list_'):
            if condition == 'list_identity':
                frozen = []
            elif condition == 'list_fields':
                frozen['fields'] = []
            else:
                frozen['physical_basis'] = '[]'
            item.production_snapshot_json = json.dumps(frozen)
        elif condition == 'missing_location':
            db.get(MoldTool, mold_id).rack_location = ''
        else:
            db.get(MoldTool, mold_id).is_active = False
        db.commit()
        result = package(db, fixture)
        c = result['cards'][0]['components'][0]
        if condition in ('unbound', 'bad_snapshot') or condition.startswith('list_'):
            assert c['mold_tool_id'] is None
            assert c['mold_location_display'] is None
        elif condition == 'missing_location':
            assert c['mold_code'] == 'M-PRINT-B7'
            assert c['mold_location_display'] is None
        else:
            assert c['mold_tool_id'] == mold_id
            assert c['mold_is_active'] is False
        if condition == 'bad_snapshot':
            assert any('冻结身份' in x for x in result['cards'][0]['review_messages'])
        if condition.startswith('list_'):
            assert any('资料格式异常' in x for x in result['cards'][0]['review_messages'])


def test_mold_move_invalidates_prepared_stock_print_selection(stock_replenishment_print_app, monkeypatch):
    from app.models.mold_tool import MoldTool
    fixture = stock_replenishment_print_app
    mold_id, _, order_id = setup_mold(fixture, monkeypatch)
    with TestClient(fixture['app']) as client:
        _login(client, 'p132a2-admin')
        response = client.get(_package_url(order_id, fixture['semi_item_id']))
        assert response.status_code == 200, response.text
        selection = _stock_selection(response.json(), order_id)
        with fixture['session_factory']() as db:
            mold = db.get(MoldTool, mold_id)
            mold.rack_location = 'C2';mold.location_version += 1
            db.commit()
        stale = client.post('/api/requisition/production-print-batches/prepare', json={
            'idempotency_key': 'mold-moved-after-preview', 'confirmed': True, 'items': [selection]})
        assert stale.status_code == 409, stale.text
        fresh = client.get(_package_url(order_id, fixture['semi_item_id']))
        assert fresh.status_code == 200
        prepared = client.post('/api/requisition/production-print-batches/prepare', json={
            'idempotency_key': 'mold-reloaded-after-move', 'confirmed': True,
            'items': [_stock_selection(fresh.json(), order_id)]})
        assert prepared.status_code == 200, prepared.text
        assert prepared.json()['cards'][0]['components'][0]['mold_location_display'] == 'C2'


@pytest.mark.parametrize('history_state', ['verified', 'damaged', 'missing'])
def test_legacy_bom_child_mold_uses_verified_purchase_time_history(stock_replenishment_print_app, monkeypatch, history_state):
    import hashlib
    from datetime import datetime, timedelta
    from app.models.master_data_object_version import MasterDataObjectVersion
    from app.models.product import Product
    from app.models.stock_replenishment import StockReplenishmentOrderItem
    fixture = stock_replenishment_print_app
    mold_id, product_id, _ = setup_mold(fixture, monkeypatch)
    with fixture['session_factory']() as db:
        item = db.get(StockReplenishmentOrderItem, fixture['semi_item_id'])
        fields = json.loads(item.production_snapshot_json)['fields']
        snapshot = json.dumps(fields, ensure_ascii=False)
        item.production_snapshot_json = None
        item.created_at = datetime(2026, 1, 1, 10)
        item.quantity_contract_json = json.dumps({'kind': 'bom_stock_plan', 'components': [{
            'product_id': product_id, 'production_process': '模切', 'printing_colors': None}]})
        if history_state != 'missing':
            db.add(MasterDataObjectVersion(object_type='product', object_id=product_id, version=1,
                action='update', source='test', change_set_id='test-print-mold-history', snapshot_json=snapshot,
                snapshot_sha256=hashlib.sha256(snapshot.encode()).hexdigest() if history_state == 'verified' else '0'*64,
                created_at=item.created_at-timedelta(days=1)))
        # A later master edit cannot become evidence of the old purchase's mold.
        product = db.get(Product, product_id)
        product.mold_tool_id = None
        product.updated_at = item.created_at+timedelta(days=1)
        db.commit()
        result = package(db, fixture)
        c = result['cards'][0]['components'][0]
        if history_state == 'verified':
            assert c['mold_tool_id'] == mold_id
            assert c['mold_binding_basis'] == 'master_history'
            assert c['mold_location_display'] == 'B7'
        else:
            assert c['mold_tool_id'] is None
            assert result['cards'][0]['review_messages']
