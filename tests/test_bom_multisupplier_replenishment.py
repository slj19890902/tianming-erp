from copy import deepcopy
from decimal import Decimal
from uuid import uuid4
from datetime import date, datetime

import pytest
import json
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from tests.test_stock_replenishment_flow import stock_replenishment_app, _login


def seed_bom(factory):
    from app.models.material import Material
    from app.models.product import Product
    from app.models.product_bom import ProductBomComponent
    from app.models.stock_replenishment import InventoryStockPolicy
    from app.models.supplier import Supplier
    from app.services.supplier_master import normalize_supplier_identity
    with factory() as db:
        db.add(Supplier(standard_name='第二供应商', normalized_name=normalize_supplier_identity('第二供应商'),
                        display_name='第二供应商', is_active=True))
        material = Material(code='SECOND', layer_count=5, flute_type='AB', supplier_name='第二供应商',
                            quote_price=Decimal('3'), price_unit='元/㎡', purchase_currency='CNY',
                            purchase_tax_included=True, purchase_tax_rate=Decimal('0.13'), is_active=True)
        db.add(material); db.flush()
        children = [db.get(Product, 1), Product(customer_id=1, product_code='BODY-2',
                    customer_material_code='BODY-2', product_name='补强板', material_id=material.id,
                    layer_count=5, flute_type='AB', report_length_mm=500, report_width_mm=300,
                    crease_type='净料', box_style='模切内盒')]
        parent = Product(customer_id=1, product_code='KIT', customer_material_code='KIT', product_name='组合套装',
                         is_composite=True, is_virtual_composite_parent=True, unit='套')
        db.add_all([children[1], parent]); db.flush()
        for index, (child, ratio) in enumerate(zip(children, [1, 2])):
            db.add(ProductBomComponent(parent_product_id=parent.id, component_product_id=child.id,
                    quantity_per_set=ratio, display_order=index, internal_component_code=str(index), is_required=True))
        policy = InventoryStockPolicy(policy_name='组合补库', target_inventory_type='finished', customer_id=1,
                    product_id=parent.id, warning_quantity=100, target_quantity=200, active=True)
        db.add(policy); db.commit()
        return policy.id, [child.id for child in children], material.id


def saved(client, policy_id):
    draft = client.get(f'/api/requisition/stock-policies/{policy_id}/replenishment-draft',
                       params={'finished_quantity': 20})
    assert draft.status_code == 200, draft.text
    data = draft.json()
    assert data['draft_ready'], data['missing_fields']
    assert [row['quantity'] for row in data['items']] == [20, 40]
    assert [row['material_supplier_name'] for row in data['items']] == ['苏州佳丰', '第二供应商']
    payload = {**data, 'idempotency_key': 'multi-bom-save'}
    response = client.post('/api/requisition/stock-replenishment/orders', json=payload)
    assert response.status_code == 201, response.text
    replay = client.post('/api/requisition/stock-replenishment/orders', json=payload)
    assert replay.status_code == 201 and replay.json()['id'] == response.json()['id']
    changed = deepcopy(payload); changed['items'][0]['quantity'] += 1
    assert client.post('/api/requisition/stock-replenishment/orders', json=changed).status_code == 409
    order = response.json()
    rows = [row for row in client.get('/api/requisition/pending').json()['items']
            if row.get('source_id') == order['id'] and row['source_type'] == 'stock_replenishment']
    assert [row['supplier_name'] for row in rows] == ['苏州佳丰', '第二供应商']
    return order, rows


def groups(rows):
    return {'supplier_groups': [dict(supplier_name=row['supplier_name'], request_key=str(uuid4()),
        stock_sources=[dict(stock_replenishment_item_id=row['stock_replenishment_item_id'],
                            source_fingerprint=row['source_fingerprint'])]) for index, row in enumerate(rows)]}


def test_two_suppliers_save_purchase_replay_and_keep_frozen_identity(stock_replenishment_app):
    from app.models.material import Material
    from app.models.supplier_requisition_order import SupplierRequisitionOrder, SupplierRequisitionOrderItem
    from app.models.procurement_source import ProcurementSourceLink
    app, factory = stock_replenishment_app
    policy_id, _, material_id = seed_bom(factory)
    with TestClient(app) as client:
        _login(client)
        order, rows = saved(client, policy_id)
        # A later master edit cannot redirect an already frozen source.
        with factory() as db:
            db.get(Material, material_id).supplier_name = '苏州佳丰'; db.commit()
        payload = groups(rows)
        purchase = client.post('/api/requisition/supplier-orders/from-pending-selection', json=payload)
        assert purchase.status_code == 201, purchase.text
        assert len(purchase.json()['created_orders']) == 2
        replay = client.post('/api/requisition/supplier-orders/from-pending-selection', json=payload)
        assert replay.status_code == 201 and replay.json()['idempotent_replay']
        assert not any(row.get('source_id') == order['id'] for row in client.get('/api/requisition/pending').json()['items'])
    with factory() as db:
        assert db.scalar(select(func.count(SupplierRequisitionOrder.id))) == 2
        assert db.scalar(select(func.count(ProcurementSourceLink.id))) == 2
        lines = list(db.scalars(select(SupplierRequisitionOrderItem).order_by(SupplierRequisitionOrderItem.id)))
        assert [(row.supplier_name_snapshot, row.quantity) for row in lines] == [('苏州佳丰', 20), ('第二供应商', 40)]


def test_wrong_supplier_second_group_rolls_back_first_group(stock_replenishment_app):
    from app.models.supplier_requisition_order import SupplierRequisitionOrder
    from app.models.stock_replenishment import StockReplenishmentOrder
    app, factory = stock_replenishment_app
    policy_id, _, _ = seed_bom(factory)
    with TestClient(app) as client:
        _login(client)
        order, rows = saved(client, policy_id)
        payload = groups(rows); payload['supplier_groups'][1]['supplier_name'] = '佳丰'
        rejected = client.post('/api/requisition/supplier-orders/from-pending-selection', json=payload)
        assert rejected.status_code == 409, rejected.text
    with factory() as db:
        assert db.scalar(select(func.count(SupplierRequisitionOrder.id))) == 0
        assert db.get(StockReplenishmentOrder, order['id']).status == 'draft'


def test_missing_supplier_names_the_component(stock_replenishment_app):
    from app.models.material import Material
    app, factory = stock_replenishment_app
    policy_id, _, material_id = seed_bom(factory)
    with factory() as db:
        db.get(Material, material_id).supplier_name = ''; db.commit()
    with TestClient(app) as client:
        _login(client)
        response = client.get(f'/api/requisition/stock-policies/{policy_id}/replenishment-draft')
        assert response.status_code == 200, response.text
        assert not response.json()['draft_ready']
        assert '补强板：材质供应商' in response.json()['missing_fields']


def test_partial_purchase_pending_coverage_and_receipts_keep_supplier_cost(stock_replenishment_app):
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.supplier_settlement import SupplierReceiptSettlementPriceFact
    from app.models.warehouse_inventory import SemiFinishedInventoryDetail
    from app.services.stock_replenishment import customer_board_preparation_coverage
    from app.models.product import Product
    app, factory = stock_replenishment_app
    policy_id, children, _ = seed_bom(factory)
    with TestClient(app) as client:
        _login(client)
        order, rows = saved(client, policy_id)
        payload = groups(rows)
        first = client.post('/api/requisition/supplier-orders/from-pending-selection',
                            json={'supplier_groups': payload['supplier_groups'][:1]})
        assert first.status_code == 201, first.text
        with factory() as db:
            body = customer_board_preparation_coverage(db, product=db.get(Product, children[0]))
            reinforcement = customer_board_preparation_coverage(db, product=db.get(Product, children[1]))
            assert body['pending_board_preparation_sheet_quantity'] == 0
            assert body['incoming_board_preparation_sheet_quantity'] == 20
            assert reinforcement['pending_board_preparation_sheet_quantity'] == 40
            assert reinforcement['incoming_board_preparation_sheet_quantity'] == 0
        second = client.post('/api/requisition/supplier-orders/from-pending-selection',
                             json={'supplier_groups': payload['supplier_groups'][1:]})
        assert second.status_code == 201, second.text
        pending = client.get('/api/incoming/pending')
        assert pending.status_code == 200, pending.text
        incoming_rows = {row['item_id']: row for row in pending.json()['items']}
        for index, row in enumerate(rows):
            source_id = row['stock_replenishment_item_id']
            assert incoming_rows[f'sr{source_id}']['snapshot_supplier_name'] == row['supplier_name']
            received = client.put(f'/api/incoming/receive/sr{source_id}', json={
                'received_quantity': row['quantity'], 'idempotency_key': f'multi-bom-receipt-{index}'})
            assert received.status_code == 200, received.text
    with factory() as db:
        facts = list(db.scalars(select(SupplierReceiptSettlementPriceFact).order_by(SupplierReceiptSettlementPriceFact.id)))
        assert [(fact.supplier_name_snapshot, fact.received_quantity_snapshot) for fact in facts] == [('佳丰', 20), ('第二供应商', 40)]
        details = list(db.scalars(select(SemiFinishedInventoryDetail).order_by(SemiFinishedInventoryDetail.inventory_lot_id)))
        assert [row.supplier_name for row in details] == ['苏州佳丰', '第二供应商']
        assert db.scalar(select(func.count(IncomingReceiptItem.id))) == 2


def test_automatic_bom_warning_keeps_supplier_contract_and_replays(stock_replenishment_app):
    from app.api.deliveries import router
    from app.models.delivery import Delivery
    from app.models.product import Product
    from app.models.stock_replenishment import InventoryStockPolicy, StockReplenishmentOrder
    from app.models.warehouse_inventory import InventoryLot, InventoryMovement, WarehouseLocation
    from tests.test_common_box_low_stock_alert import _add_finished_lot
    app, factory = stock_replenishment_app
    app.include_router(router, prefix='/api/deliveries')
    policy_id, _, _ = seed_bom(factory)
    # A completed dispatch fixture isolates warning confirmation from printing.
    with factory() as db:
        policy = db.get(InventoryStockPolicy, policy_id)
        policy.updated_at = datetime(2020, 1, 1)
        delivery = Delivery(delivery_number='MULTI-BOM-DISPATCH', customer_id=1,
                            delivery_date=date.today(), status='dispatched', source_mode='unordered_finished')
        db.add(delivery); db.flush()
        _add_finished_lot(db, lot_number='MULTI-BOM-CONSUMED', product=db.get(Product, policy.product_id),
                          location=db.get(WarehouseLocation, 2), quantity_available=0)
        db.flush()
        lot = db.scalar(select(InventoryLot).where(InventoryLot.lot_number == 'MULTI-BOM-CONSUMED'))
        lot.quantity_consumed = 1
        db.add(InventoryMovement(movement_number='MULTI-BOM-CONSUME', inventory_lot_id=lot.id,
            movement_type='consume', quantity=1, unit='套', before_available=1, after_available=0,
            before_reserved=0, after_reserved=0, before_consumed=0, after_consumed=1,
            before_damaged=0, after_damaged=0, before_scrapped=0, after_scrapped=0,
            related_delivery_id=delivery.id))
        db.commit(); delivery_id = delivery.id
    with TestClient(app) as client:
        _login(client)
        url = f'/api/deliveries/{delivery_id}/stock-warnings'
        warning = client.get(url)
        assert warning.status_code == 200, warning.text
        row = warning.json()['items'][0]
        payload = {'items': [{'policy_id': policy_id, 'plan_hash': row['plan_hash']}]}
        first = client.post(url + '/confirm', json=payload)
        assert first.status_code == 200, first.text
        second = client.post(url + '/confirm', json=payload)
        assert second.status_code == 200 and not second.json()['orders'][0]['created'], second.text
        pending = client.get('/api/requisition/pending').json()['items']
        assert [r['supplier_name'] for r in pending if r.get('source_type') == 'stock_replenishment'] == ['苏州佳丰', '第二供应商']
    with factory() as db:
        assert db.scalar(select(func.count(StockReplenishmentOrder.id))) == 1
        assert db.get(InventoryStockPolicy, policy_id).updated_at == datetime(2020, 1, 1)
        source = db.scalar(select(StockReplenishmentOrder))
        assert all(json.loads(item.production_snapshot_json)['bom_parent_basis'] for item in source.items)


def test_purchase_audit_failure_rolls_back_both_supplier_orders(stock_replenishment_app, monkeypatch):
    from app.api import requisition
    from app.models.supplier_requisition_order import SupplierRequisitionOrder
    from app.models.procurement_source import ProcurementSourceLink
    from app.models.stock_replenishment import StockReplenishmentOrder
    app, factory = stock_replenishment_app
    policy_id, _, _ = seed_bom(factory)
    with TestClient(app) as client:
        _login(client)
        order, rows = saved(client, policy_id)
        def fail(*args, **kwargs):
            raise RuntimeError('injected supplier audit failure')
        monkeypatch.setattr(requisition, 'append_audit_event', fail)
        with pytest.raises(RuntimeError, match='injected supplier audit failure'):
            client.post('/api/requisition/supplier-orders/from-pending-selection', json=groups(rows))
    with factory() as db:
        assert db.scalar(select(func.count(SupplierRequisitionOrder.id))) == 0
        assert db.scalar(select(func.count(ProcurementSourceLink.id))) == 0
        assert db.get(StockReplenishmentOrder, order['id']).status == 'draft'


def test_real_multisupplier_save_receipt_passes_frontend_recovery(stock_replenishment_app, tmp_path):
    app, factory = stock_replenishment_app
    policy_id, _, _ = seed_bom(factory)
    with TestClient(app) as client:
        _login(client)
        draft = client.get(f'/api/requisition/stock-policies/{policy_id}/replenishment-draft',
                           params={'finished_quantity': 20}).json()
        request = {**draft, 'idempotency_key': 'multi-bom-recovery', 'expected_actor_id': 1}
        response = client.post('/api/requisition/stock-replenishment/orders', json=request)
        assert response.status_code == 201, response.text
        resolved = client.post('/api/requisition/stock-replenishment/save-results/multi-bom-recovery/resolve',
                               json={'expected_actor_id': 1, 'original_request': request})
        assert resolved.status_code == 200, resolved.text
    vector = tmp_path / 'receipt.json'
    vector.write_text(json.dumps({'request': request, 'saved': response.json(), 'resolved': resolved.json()}))
    script = r"""
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const box={TextEncoder};vm.createContext(box);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),box);
const api=box.TmStockReplenishmentRecovery,data=JSON.parse(fs.readFileSync(process.argv[2],'utf8')),m=new Map();
const storage={getItem:k=>m.get(k)??null,setItem:(k,v)=>m.set(k,v),removeItem:k=>m.delete(k),key:i=>[...m.keys()][i],get length(){return m.size}};
const record=api.createStore(storage,1).prepare(data.request);
assert.equal(api.validateComplete(data.saved,record).lines.length,2);
assert.equal(api.validateResolve(data.resolved,record).lines.length,2);
"""
    node = '/Users/kimiyf/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node'
    if not Path(node).exists():
        import shutil
        node = shutil.which('node')
    assert node, 'Node.js is required for the real saved receipt regression'
    subprocess.run([node, '-e', script, str(Path(__file__).resolve().parents[1] / 'static/ui/stock-replenishment-save-recovery.js'),
                    str(vector)], check=True, capture_output=True, text=True)
