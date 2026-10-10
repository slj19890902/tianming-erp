from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from app.services.sheet_cutting_contract import SheetCuttingContract, SheetCuttingContractError, cutting_work_instruction
from app.services.sheet_cutting_settings import SheetCuttingSettings, normalize_settings, component_settings, resolve_order_sheet_contract
from tests.test_master_data_versioning_p4_writers import writer_app, _create_customer, _create_product, _product_payload
from tests.test_phase11_requisition import requisition_app, _login, _preview_supplier_order_draft, _save_supplier_order_draft
from tests.test_sheet_cutting_supplier_flow import _source
from tests.test_stock_replenishment_flow import stock_replenishment_app


def settings(mold=1):
    return {'schema_version': 3, 'whole': SheetCuttingSettings(2, 3, mold, mold > 1, '750', '700').to_dict()}


def test_trim_preserves_yield_and_strict_old_and_new_snapshots():
    new = component_settings(settings(4)).contract(375, 226)
    assert new.supplier_size_mm == (750, 700)
    assert new.required_supplier_size_mm == (750, 678)
    assert new.purchase_quantity(required_piece_qty=2400).supplier_sheet_qty == 100
    assert new.yield_per_supplier_sheet == 24
    assert SheetCuttingContract.from_snapshot(new.to_snapshot()) == new
    assert '先修边至750×678mm' in cutting_work_instruction(new.to_snapshot())
    old = SheetCuttingContract(375, 226, 2, 3)
    assert old.to_snapshot()['schema_version'] == 2
    assert SheetCuttingContract.from_snapshot(old.to_snapshot()) == old
    for field, value in [('supplier_width_mm', '701'), ('yield_per_supplier_sheet', 25), ('schema_version', 2), ('actual_supplier_width_mm', None)]:
        bad = new.to_snapshot(); bad[field] = value
        with pytest.raises(SheetCuttingContractError):
            SheetCuttingContract.from_snapshot(bad)
    for length, width in [('749', '700'), ('750', '677'), ('750', None), ('750', 'NaN')]:
        with pytest.raises(SheetCuttingContractError):
            SheetCuttingContract(375, 226, 2, 3, False, 1, length, width)
    invalid = settings(); invalid['schema_version'] = 2
    with pytest.raises(SheetCuttingContractError): normalize_settings(invalid)


def test_common_box_default_roundtrip_cas_and_wrong_net_rejected(writer_app):
    with TestClient(writer_app) as client:
        customer = _create_customer(client, 'TRIM', 381)
        product = _create_product(client, customer['id'], 'TRIM', box_style='A1普通箱',
            report_length_mm=375, report_width_mm=226, crease_type='毛片', sheet_cutting_settings=settings())
        read = client.get(f"/api/master/products/{product['id']}")
        assert read.status_code == 200, read.text
        assert read.json()['sheet_cutting_settings'] == settings()
        assert Decimal(str(read.json()['report_length_mm'])) == 375
        payload = _product_payload(customer['id'], 'TRIM', box_style='A1普通箱', report_length_mm=375,
            report_width_mm=226, crease_type='毛片', expected_version=product['version'], remark='普通编辑保留默认尺寸')
        saved = client.put(f"/api/master/products/{product['id']}", json=payload)
        assert saved.status_code == 200, saved.text
        assert saved.json()['sheet_cutting_settings'] == settings()
        assert client.put(f"/api/master/products/{product['id']}", json=payload).status_code == 409
        payload.update(expected_version=saved.json()['version'], crease_type='净料')
        refused = client.put(f"/api/master/products/{product['id']}", json=payload)
        assert refused.status_code == 422, refused.text
        assert '毛片' in refused.text
        assert client.get(f"/api/master/products/{product['id']}").json()['version'] == saved.json()['version']


def seed_trim(sessions, *, quantity=600):
    from app.models.order import OrderItem
    from app.models.product import Product
    item_id, product_id = _source(sessions, quantity=quantity)
    with sessions() as db:
        item, product = db.get(OrderItem, item_id), db.get(Product, product_id)
        product.report_length_mm = item.snapshot_report_length_mm = 375
        product.report_width_mm = item.snapshot_report_width_mm = 226
        product.crease_type = item.snapshot_crease_type = '毛片'
        product.sheet_cutting_settings = settings(2)
        item.sheet_cutting_settings_snapshot = deepcopy(settings(2))
        item.special_process = '一开12'
        for field in ('left', 'middle', 'right'):
            setattr(item, f'snapshot_crease_{field}_mm', None)
            setattr(product, f'crease_{field}_mm', None)
        db.commit()
    return item_id, product_id


def test_order_uses_saved_default_freezes_history_and_replay(requisition_app):
    from app.models.product import Product
    from app.models.order import OrderItem
    from app.models.supplier_requisition_order import SupplierRequisitionOrderItem
    from sqlalchemy import select
    app, sessions = requisition_app
    item_id, product_id = seed_trim(sessions)
    with TestClient(app) as client:
        _login(client, 'admin')
        draft = _preview_supplier_order_draft(client, [{'type': 'order_item', 'order_item_id': item_id}])
        line = draft['supplier_groups'][0]['lines'][0]
        assert (line['report_length_mm'], line['report_width_mm'], line['requisition_qty']) == (750, 700, 50)
        assert line['crease_type'] == '毛片'
        saved = _save_supplier_order_draft(client, draft)
        assert saved.status_code == 201, saved.text
        replay = _save_supplier_order_draft(client, draft)
        assert replay.status_code == 201 and replay.json()['idempotent_replay'] is True
        assert replay.json()['created_orders'] == saved.json()['created_orders']
    with sessions() as db:
        item = db.get(OrderItem, item_id)
        assert item.sheet_cutting_settings_snapshot == settings(2)
        row = db.scalar(select(SupplierRequisitionOrderItem).where(SupplierRequisitionOrderItem.order_item_id == item_id))
        frozen = deepcopy(row.sheet_cutting_snapshot)
        product = db.get(Product, product_id)
        product.sheet_cutting_settings = {'schema_version': 2, 'whole': SheetCuttingSettings().to_dict()}
        product.report_length_mm = 999; product.report_width_mm = 999
        db.commit()
        assert row.sheet_cutting_snapshot == frozen
        assert component_settings(item.sheet_cutting_settings_snapshot).contract(item.snapshot_report_length_mm, item.snapshot_report_width_mm).supplier_size_mm == (750, 700)


def test_net_order_cannot_receive_a_gross_trim_override():
    item = SimpleNamespace(sheet_cutting_settings_snapshot={'schema_version': 2, 'whole': SheetCuttingSettings(2, 3).to_dict()},
        snapshot_report_length_mm=375, snapshot_report_width_mm=226, snapshot_crease_type='净料')
    with pytest.raises(SheetCuttingContractError, match='毛片'):
        resolve_order_sheet_contract(item, proposal=component_settings(settings()).contract(375, 226).to_snapshot())


def test_stock_draft_uses_actual_dimensions_and_rejects_stale_net_size(stock_replenishment_app):
    from uuid import uuid4
    from app.models.product import Product
    from tests.test_stock_replenishment_flow import _login as stock_login
    app, sessions = stock_replenishment_app
    with sessions() as db:
        product = db.get(Product, 1)
        product.sheet_cutting_settings = settings()
        product.report_length_mm=375; product.report_width_mm=226; product.crease_type='毛片'
        product.crease_left_mm=product.crease_middle_mm=product.crease_right_mm=None
        db.commit()
    payload = {'idempotency_key': str(uuid4()), 'source_type': 'customer_request', 'supplier_name': '佳丰', 'stock_now': False,
        'items': [{'target_inventory_type': 'semi_finished', 'customer_id': 1, 'product_id': 1, 'material_id': 1,
            'layer_count': 5, 'flute_type': 'AB', 'report_length_mm': 750, 'report_width_mm': 700,
            'crease_type': '毛片', 'quantity': 30, 'stock_yield_per_sheet': 6, 'location_id': 2}]}
    with TestClient(app) as client:
        stock_login(client)
        data=client.get('/api/requisition/stock-replenishment/products', params={'customer_id':1}).json()['items'][0]
        assert (data['report_length_mm'],data['report_width_mm'],data['output_per_sheet']) == (750,700,6)
        wrong=deepcopy(payload); wrong['items'][0]['report_width_mm']=678
        assert client.post('/api/requisition/stock-replenishment/orders',json=wrong).status_code==409
        saved=client.post('/api/requisition/stock-replenishment/orders',json=payload)
        assert saved.status_code==201,saved.text
        assert saved.json()['items'][0]['sheet_cutting_snapshot']['supplier_width_mm']=='700'
