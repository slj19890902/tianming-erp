from copy import deepcopy

from fastapi.testclient import TestClient
from sqlalchemy import select, func

from test_stock_replenishment_flow import stock_replenishment_app, _login
from test_n039_composite_bom_requisition import composite_requisition_app


def stock_payload(key="r05-stock-request-001", quantity=20):
    return {"source_type": "customer_request", "idempotency_key": key,
        "customer_id": 1, "stock_now": False, "items": [{
            "target_inventory_type": "semi_finished", "reference_product_id": 1,
            "customer_id": 1, "material_id": 1, "material_code": "A416D",
            "layer_count": 5, "flute_type": "AB", "report_length_mm": 1865,
            "report_width_mm": 830, "quantity": quantity}]}


def test_stock_pending_purchase_replay_and_stale_request(stock_replenishment_app):
    from app.models.stock_replenishment import StockReplenishmentOrder
    from app.models.procurement_source import ProcurementSourceLink
    from app.models.supplier_requisition_order import SupplierRequisitionOrder
    from app.services.incoming_receipts import _stock_target, IncomingReceiptError
    import pytest
    app, factory = stock_replenishment_app
    with TestClient(app) as client:
        _login(client)
        response = client.post('/api/requisition/stock-replenishment/orders', json=stock_payload())
        assert response.status_code == 201, response.text
        saved = response.json()
        assert saved['status'] == 'draft'
        assert client.post('/api/requisition/stock-replenishment/orders', json=stock_payload()).json()['id'] == saved['id']
        assert client.post('/api/requisition/stock-replenishment/orders', json=stock_payload(quantity=21)).status_code == 409
        with factory() as db:
            assert db.scalar(select(func.count()).select_from(SupplierRequisitionOrder)) == 0
            with pytest.raises(IncomingReceiptError):
                _stock_target(db, f"sr{saved['items'][0]['id']}")
        pending = client.get('/api/requisition/pending', params={'page': 1, 'page_size': 25})
        assert pending.status_code == 200, pending.text
        rows = [row for row in pending.json()['items'] if row.get('source_type') == 'stock_replenishment']
        assert len(rows) == 1
        row = rows[0]
        payload = {'supplier_groups': [{'supplier_name': row['supplier_name'], 'request_key': 'r05-purchase-request-001',
            'stock_sources': [{'stock_replenishment_item_id': row['stock_replenishment_item_id'],
                               'source_fingerprint': row['source_fingerprint']}]}]}
        stale = deepcopy(payload)
        stale['supplier_groups'][0]['stock_sources'][0]['source_fingerprint'] = '0' * 64
        assert client.post('/api/requisition/supplier-orders/from-pending-selection', json=stale).status_code == 409
        purchase = client.post('/api/requisition/supplier-orders/from-pending-selection', json=payload)
        assert purchase.status_code == 201, purchase.text
        replay = client.post('/api/requisition/supplier-orders/from-pending-selection', json=payload)
        assert replay.status_code == 201, replay.text
        assert replay.json()['idempotent_replay'] is True
        duplicate = deepcopy(payload)
        duplicate['supplier_groups'][0]['request_key'] = 'r05-different-request-002'
        assert client.post('/api/requisition/supplier-orders/from-pending-selection', json=duplicate).status_code == 409
        with factory() as db:
            assert db.scalar(select(func.count()).select_from(SupplierRequisitionOrder)) == 1
            assert db.scalar(select(func.count()).select_from(ProcurementSourceLink)) == 1
            assert db.get(StockReplenishmentOrder, saved['id']).status == 'confirmed'
            _stock_target(db, f"sr{saved['items'][0]['id']}")
        assert not [row for row in client.get('/api/requisition/pending').json()['items']
                    if row.get('source_type') == 'stock_replenishment']
        source_id = saved['items'][0]['id']
        receipt_payload = {'received_quantity': 5, 'idempotency_key':'r05-stock-receipt-001',
                           'resolution_action':'await_supplier', 'resolution_reason':'分批到料，继续等补料'}
        receipt = client.put(f'/api/incoming/receive/sr{source_id}', json=receipt_payload)
        assert receipt.status_code == 200, receipt.text
        repeated = client.put(f'/api/incoming/receive/sr{source_id}', json=receipt_payload)
        assert repeated.status_code == 200, repeated.text
        from app.models.incoming_receipt import IncomingReceiptItem
        with factory() as db:
            posted = db.scalars(select(IncomingReceiptItem).where(IncomingReceiptItem.stock_replenishment_item_id == source_id)).all()
            assert len(posted) == 1
            assert posted[0].received_quantity == 5
            purchase_id = db.scalar(select(SupplierRequisitionOrder.id))
        assert client.put(f'/api/requisition/supplier-orders/{purchase_id}/void').status_code == 409


def test_two_stock_sources_one_purchase_projection_and_cancel(stock_replenishment_app):
    from app.models.procurement_source import ProcurementSourceLink
    from app.models.supplier_requisition_order import SupplierRequisitionOrder
    app, factory = stock_replenishment_app
    with TestClient(app) as client:
        _login(client)
        for number in (1, 2):
            response = client.post('/api/requisition/stock-replenishment/orders', json=stock_payload(f'r05-demand-{number}', number * 20))
            assert response.status_code == 201, response.text
        rows = client.get('/api/requisition/pending').json()['items']
        sources = [{'stock_replenishment_item_id': row['stock_replenishment_item_id'], 'source_fingerprint': row['source_fingerprint']} for row in rows]
        payload = {'supplier_groups': [{'supplier_name': rows[0]['supplier_name'], 'request_key': 'r05-multiple-purchase-001', 'stock_sources': sources}]}
        invalid = deepcopy(payload)
        invalid['supplier_groups'][0]['stock_sources'][1]['source_fingerprint'] = '0' * 64
        assert client.post('/api/requisition/supplier-orders/from-pending-selection', json=invalid).status_code == 409
        with factory() as db:
            assert db.scalar(select(func.count()).select_from(SupplierRequisitionOrder)) == 0
            assert db.scalar(select(func.count()).select_from(ProcurementSourceLink)) == 0
        response = client.post('/api/requisition/supplier-orders/from-pending-selection', json=payload)
        assert response.status_code == 201, response.text
        with factory() as db:
            purchase_id = db.scalar(select(SupplierRequisitionOrder.id))
        projection = client.get(f'/api/requisition/supplier-orders/{purchase_id}')
        assert projection.status_code == 200, projection.text
        assert projection.json()['requisition_qty'] == 60
        assert projection.json()['stock_purpose_sheet_qty'] == 60
        assert len(projection.json()['lines']) == 2
        assert all(line['source_items'][0]['stock_replenishment_item_id'] for line in projection.json()['lines'])
        reported = client.get('/api/requisition/reported-documents')
        assert reported.status_code == 200, reported.text
        assert 'stock_replenishment:' not in reported.text
        incoming = client.get('/api/incoming/pending')
        assert incoming.status_code == 200, incoming.text
        assert {row['supplier_order_number'] for row in incoming.json()['items']} == {projection.json()['order_number']}
        cancelled = client.put(f'/api/requisition/supplier-orders/{purchase_id}/void')
        assert cancelled.status_code == 200, cancelled.text
        assert len(client.get('/api/requisition/pending').json()['items']) == 2


def test_bom_purchase_keeps_existing_receipt_sources(composite_requisition_app):
    from test_n039_composite_bom_requisition import _login as bom_login, _parent_payload, _component_payload
    from app.models.procurement_source import ProcurementSourceLink
    from app.models.requisition import RequisitionItem
    from app.models.supplier_requisition_order import SupplierRequisitionOrder
    app, factory = composite_requisition_app
    with TestClient(app) as client:
        bom_login(client)
        payload = {'supplier_groups': [{'supplier_name': 'N039 供应商', 'request_key': 'r05-bom-purchase-001',
            'bom_items': [_parent_payload(), _component_payload(1), _component_payload(2)]}]}
        response = client.post('/api/requisition/supplier-orders/from-pending-selection', json=payload)
        assert response.status_code == 201, response.text
        assert client.post('/api/requisition/supplier-orders/from-pending-selection', json=payload).json()['idempotent_replay']

        with factory() as db:
            assert db.scalar(select(func.count()).select_from(ProcurementSourceLink)) == 3
            assert db.scalar(select(func.count()).select_from(RequisitionItem)) == 3
            purchase_id = db.scalar(select(SupplierRequisitionOrder.id))
        response = client.get(f'/api/requisition/supplier-orders/{purchase_id}')
        assert response.status_code == 200, response.text
        assert response.json()['purchase_total_sheet_qty'] == 60
        assert len(response.json()['lines']) == 3
        cancelled = client.put(f'/api/requisition/supplier-orders/{purchase_id}/void')
        assert cancelled.status_code == 200, cancelled.text
        with factory() as db:
            assert db.scalar(select(func.count()).select_from(ProcurementSourceLink).where(ProcurementSourceLink.status == 'active')) == 0


def test_unified_frontend_preserves_mixed_sources(tmp_path):
    import subprocess
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    html = (root / 'static/index.html').read_text(encoding='utf-8')
    method = html.split('async openUnifiedSupplierDraft(rows) {', 1)[1].split('          compositePhysicalMergeKey(line) {', 1)[0].rsplit('},', 1)[0]
    script = '''
const assert = require('node:assert/strict');
const latestRequestControllers = new Map();
const createIdempotencyKey=()=> 'r05-js-request-00001';
const axios={post:async()=>({data:{supplier_groups:[{supplier_name:'same', request_key:'ordinary-request-0001', lines:[{source_items:[{order_item_id:1}]}]}]}})};
const context={activePage:'requisition',
 beginLatestRequest(key){const controller={signal:{}};latestRequestControllers.set(key,controller);return controller;},
 finishLatestRequest(key){latestRequestControllers.delete(key);},
 pendingSupplierSelectionPayload:row=>({type:'order_item',order_item_id:row.item_id}),
 buildRequisitionFormLines:()=>[{snapshot_supplier_name:'same',order_item_id:2,bom_snapshot_id:3,requisition_qty:20}],
 initializePurchasePurposeLine:()=>{}, initializeSupplierPurchasePurposeDraft:data=>data,
 isCancelledRequest:()=>false, showToast(message){throw new Error(message);}};
const open=async function(rows){''' + method + '''};
(async()=>{
 const result=await open.call(context,[{item_id:1},{is_composite_bom:true},{source_type:'stock_replenishment',supplier_name:'same',stock_replenishment_item_id:4,source_fingerprint:'f'.repeat(64),quantity:5}]);
 assert.equal(result,true);
 assert.equal(context.supplierRequisitionDraft.supplier_groups.length,1);
 const group=context.supplierRequisitionDraft.supplier_groups[0];
 assert.equal(group.lines.length,1);assert.equal(group.bom_lines[0].bom_snapshot_id,3);assert.equal(group.stock_sources[0].stock_replenishment_item_id,4);
 assert.equal(context.modal.type,'supplierRequisitionDraft');assert.equal(context.supplierRequisitionPreviewLoading,false);
})().catch(e=>{console.error(e);process.exit(1);});
'''
    target = tmp_path / 'unified-purchase.cjs'
    target.write_text(script, encoding='utf-8')
    result = subprocess.run(['node', str(target)], capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 0, result.stderr

def test_unified_purchase_migration_is_additive_and_history_is_immutable(tmp_path, monkeypatch):
    import sqlite3
    from pathlib import Path
    from types import SimpleNamespace
    from alembic import command
    from alembic.config import Config
    import pytest
    target = tmp_path / 'isolated-migration.sqlite3'
    with sqlite3.connect(target) as db:
        db.executescript('''
            CREATE TABLE alembic_version(version_num TEXT PRIMARY KEY);
            INSERT INTO alembic_version VALUES ('sc0916');
            CREATE TABLE users(id INTEGER PRIMARY KEY);
            CREATE TABLE supplier_requisition_order_items(id INTEGER PRIMARY KEY);
            CREATE TABLE stock_replenishment_orders(id INTEGER PRIMARY KEY, order_number TEXT);
            CREATE TABLE stock_replenishment_order_items(id INTEGER PRIMARY KEY);
            CREATE TABLE material_requisition_items(id INTEGER PRIMARY KEY);
            INSERT INTO stock_replenishment_orders VALUES (1, 'historical-unchanged');
            INSERT INTO users VALUES(1);
            INSERT INTO supplier_requisition_order_items VALUES(1);
            INSERT INTO stock_replenishment_order_items VALUES(1);
        ''')
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv('ERP_DATABASE_PATH', str(target))
    config = Config(str(root / 'alembic.ini'))
    config.set_main_option('script_location', str(root / 'alembic'))
    config.cmd_opts = SimpleNamespace(x=[f'expected_database_path={target}'])
    command.upgrade(config, 'up0919')
    command.upgrade(config, 'up0919')
    with sqlite3.connect(target) as db:
        db.execute('PRAGMA foreign_keys=ON')
        assert db.execute('SELECT * FROM stock_replenishment_orders').fetchone() == (1, 'historical-unchanged', None)
        db.execute("INSERT INTO procurement_source_links(supplier_item_id,stock_replenishment_item_id,source_quantity,source_snapshot_json,created_by) VALUES(1,1,20,'{}',1)")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute('UPDATE procurement_source_links SET source_quantity=21')
        with pytest.raises(sqlite3.IntegrityError):
            db.execute('DELETE FROM procurement_source_links')
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    with pytest.raises(RuntimeError, match='禁止删除'):
        command.downgrade(config, 'sc0916')


def test_ordinary_bom_and_stock_share_purchase_without_duplicate_demand(composite_requisition_app):
    from test_n039_composite_bom_requisition import _login as bom_login, _parent_payload, _component_payload
    from app.models.material import Material
    from app.models.product import Product
    from app.models.order import OrderItem
    from app.models.stock_replenishment import StockReplenishmentOrder, StockReplenishmentOrderItem
    from decimal import Decimal
    app, factory = composite_requisition_app
    with factory() as db:
        material = Material(code='K616K', layer_count=5, supplier_name='N039 供应商', is_active=True)
        db.add(material)
        db.flush()
        product = Product(customer_id=1, product_code='R05-NORMAL', customer_material_code='R05-NORMAL', product_name='普通箱', material_id=material.id,
            box_category='normal', box_style='对口箱', layer_count=5, flute_type='AB', report_length_mm=900, report_width_mm=600, crease_type='净料')
        db.add(product)
        db.flush()
        item = OrderItem(order_id=1, product_id=product.id, quantity=10, unit_price=Decimal('1'), subtotal=Decimal('10'),
            material_id=material.id, snapshot_product_code=product.product_code, snapshot_product_name=product.product_name,
            snapshot_material=material.code, snapshot_supplier_name='N039 供应商', layer_count=5, flute_type='AB',
            snapshot_report_length_mm=900, snapshot_report_width_mm=600, snapshot_crease_type='净料',
            material_status='pending', requisition_status='未报料', special_process='一开一')
        stock = StockReplenishmentOrder(order_number='R05-STOCK-MIX', request_hash='a'*64, supplier_name='N039 供应商',
            customer_id=1, source_type='customer_request', status='draft', created_by=1)
        stock.items = [StockReplenishmentOrderItem(target_inventory_type='semi_finished', customer_id=1,
            material_id=material.id, material_code_snapshot=material.code, product_name_snapshot='客户备料',
            layer_count=5, flute_type='AB', report_length_mm=900, report_width_mm=600, crease_type='净料', quantity=5)]
        db.add_all([item,stock])
        db.commit()
        ordinary_id = item.id
    with TestClient(app) as client:
        bom_login(client)
        preview = client.post('/api/requisition/supplier-orders/preview-from-pending-selection', json={'selections':[{'type':'order_item','order_item_id':ordinary_id}]})
        assert preview.status_code == 200, preview.text
        payload = preview.json()
        group = payload['supplier_groups'][0]
        group['request_key'] = 'r05-all-source-mixed-001'
        group['bom_items'] = [_parent_payload(), _component_payload(1), _component_payload(2)]
        row = next(row for row in client.get('/api/requisition/pending').json()['items'] if row.get('source_type') == 'stock_replenishment')
        group['stock_sources'] = [{'stock_replenishment_item_id':row['stock_replenishment_item_id'], 'source_fingerprint':row['source_fingerprint']}]
        response = client.post('/api/requisition/supplier-orders/from-pending-selection', json=payload)
        assert response.status_code == 201, response.text
        assert len(response.json()['created_orders']) == 1
        purchase_id = response.json()['created_orders'][0]['supplier_order_id']
        projection = client.get(f'/api/requisition/supplier-orders/{purchase_id}').json()
        assert projection['requisition_qty'] == 75
        assert len(projection['source_items']) == 5
        assert client.post('/api/requisition/supplier-orders/from-pending-selection', json=payload).json()['idempotent_replay']


def test_unified_bom_inventory_refresh_rejects_replaced_draft(tmp_path):
    import subprocess
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1] / 'static/index.html').read_text(encoding='utf-8')
    method = html.split('async autoCoverCompositeDraftLine(line) {', 1)[1].split('          requisitionBatchLinePayload(line) {', 1)[0].rsplit('},', 1)[0]
    script = r"""
const assert=require('node:assert/strict');
const createIdempotencyKey=()=> 'r05-inventory-00001';
const run=async function(line){""" + method + r"""};
(async()=>{
 const line={order_item_id:1,bom_snapshot_id:2};
 const group={bom_lines:[line]}; let refreshed=0;
 const ctx={activePage:'requisition',modal:{type:'supplierRequisitionDraft'},supplierRequisitionDraft:{supplier_groups:[group]},
  recalculateCompositeDraftLine(value){assert.equal(value,line);refreshed++;},loadRequisition:async()=>{},
  executeRequisitionInventoryAction:async options=>options.refresh({data:{finished_reserved_piece_qty:3,semi_finished_reserved_piece_qty:2,remaining_required_piece_qty:15}})};
 assert.equal(await run.call(ctx,line),true);assert.equal(line.remaining_required_piece_qty,15);assert.equal(refreshed,1);assert.equal(line._auto_cover_loading,false);
 group.bom_lines=[];line.remaining_required_piece_qty=20;
 assert.equal(await run.call(ctx,line),false);assert.equal(line.remaining_required_piece_qty,20);assert.equal(refreshed,1);assert.equal(line._auto_cover_loading,false);
})().catch(error=>{console.error(error);process.exit(1);});
"""
    target=tmp_path / 'unified-inventory.cjs'
    target.write_text(script,encoding='utf-8')
    result=subprocess.run(['node',str(target)],capture_output=True,text=True,encoding='utf-8')
    assert result.returncode == 0, result.stderr
