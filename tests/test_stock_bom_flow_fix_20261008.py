from copy import deepcopy
from pathlib import Path
import shutil
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.services.production_route_contract import production_route_contract
from app.services.sheet_cutting_contract import SheetCuttingContract


@pytest.fixture
def stock_flow_snapshot(tmp_path):
    # Never connect to or write the source evidence; every test gets a new copy.
    source = Path('D:/.codex/workspace_artifacts/cutting-mold-followup-20261008/formal-readonly-snapshot.sqlite3')
    if not source.exists():
        pytest.skip('Optional isolated factory snapshot is unavailable')
    target = tmp_path / 'stock-flow.sqlite3'
    shutil.copy2(source, target)
    from app.core.database import create_sqlite_engine
    from app.api.deps import get_db
    from app.api.requisition import router, can_read, can_operate
    from app.models.user import User
    engine = create_sqlite_engine(target)
    sessions = sessionmaker(engine)
    app = FastAPI()
    app.include_router(router, prefix='/api/requisition')
    def database():
        with sessions() as db:
            yield db
    def admin():
        with sessions() as db:
            user = db.scalar(select(User).where(User.role == 'admin'))
            db.expunge(user)
            return user
    app.dependency_overrides[get_db] = database
    app.dependency_overrides[can_read] = admin
    app.dependency_overrides[can_operate] = admin
    yield app, sessions, admin
    engine.dispose()


def _parent_policy(db):
    from app.models.stock_replenishment import InventoryStockPolicy
    return db.scalar(select(InventoryStockPolicy).where(InventoryStockPolicy.product_id == 3799,
        InventoryStockPolicy.active.is_(True)))


def _remove_coverage(db):
    from app.models.stock_replenishment import StockReplenishmentOrder
    # Fixture-only baseline; this does not modify the source or formal system.
    for row in db.scalars(select(StockReplenishmentOrder)):
        row.status = 'voided'
    db.commit()


def test_manual_bom_plan_quantity_freezing_tamper_stale_and_replay(stock_flow_snapshot):
    from app.models.product import Product
    from app.models.stock_replenishment import StockReplenishmentOrderItem
    app, sessions, _ = stock_flow_snapshot
    with sessions() as db:
        policy_id = _parent_policy(db).id
        _remove_coverage(db)
    with TestClient(app) as client:
        preview = client.get(f'/api/requisition/stock-policies/{policy_id}/replenishment-draft',
            params={'finished_quantity': 1800})
        assert preview.status_code == 200, preview.text
        plan = preview.json()
        assert [row['quantity'] for row in plan['items']] == [1350, 1800]
        assert plan['replenishment_plan']['finished_quantity'] == 1800
        payload = {**plan, 'idempotency_key': str(uuid4())}
        wrong = deepcopy(payload)
        wrong['items'][0]['quantity'] += 1
        assert client.post('/api/requisition/stock-replenishment/orders', json=wrong).status_code == 409
        response = client.post('/api/requisition/stock-replenishment/orders', json=payload)
        assert response.status_code == 201, response.text
        pending = client.get('/api/requisition/pending').json()
        saved_ids = [row['id'] for row in response.json()['items']]
        assert [row['stock_replenishment_item_id'] for row in pending['items'][:2]] == saved_ids
        assert pending['total'] == len(pending['items'])
        assert sum(row['count'] for row in pending['supplier_counts']) == pending['total']
        page = client.get('/api/requisition/pending', params={'page': 1, 'page_size': 1}).json()
        assert page['items'][0]['stock_replenishment_item_id'] == saved_ids[0]
        replay = client.post('/api/requisition/stock-replenishment/orders', json=payload)
        assert replay.status_code == 201 and replay.json()['id'] == response.json()['id']
        changed = deepcopy(payload)
        changed['items'][0]['quantity'] += 1
        assert client.post('/api/requisition/stock-replenishment/orders', json=changed).status_code == 409
        with sessions() as db:
            item = db.get(StockReplenishmentOrderItem, response.json()['items'][0]['id'])
            assert item.quantity_contract_json and '1800' in item.quantity_contract_json
            product = db.get(Product, 3771)
            product.version += 1
            db.commit()
        payload['idempotency_key'] = str(uuid4())
        assert client.post('/api/requisition/stock-replenishment/orders', json=payload).status_code == 409


def test_manual_bom_c2_quantity_and_incoming_signature(stock_flow_snapshot):
    from app.api.requisition import stock_policy_replenishment_draft
    from app.models.product import Product
    from app.services.stock_replenishment import customer_board_preparation_coverage
    app, sessions, admin = stock_flow_snapshot
    with sessions() as db:
        coverage = customer_board_preparation_coverage(db, product=db.get(Product, 3771))
        assert coverage['incoming_board_preparation_sheet_quantity'] == 1080
        policy_id = _parent_policy(db).id
        preview = stock_policy_replenishment_draft(policy_id, db, admin(), finished_quantity=1800)
        assert [row['quantity'] for row in preview['items']] == [270, 360]
        _remove_coverage(db)
        for product_id in (3771, 3783):
            product = db.get(Product, product_id)
            settings = deepcopy(product.sheet_cutting_settings)
            settings['whole']['width_parts'] = 2
            product.sheet_cutting_settings = settings
        db.commit()
        preview = stock_policy_replenishment_draft(policy_id, db, admin(), finished_quantity=1800)
        assert [row['quantity'] for row in preview['items']] == [675, 900]


def test_supplier_stock_source_print_and_fingerprint_cas(stock_flow_snapshot):
    from app.models.supplier_requisition_order import SupplierRequisitionOrder
    from app.models.stock_replenishment import StockReplenishmentOrderItem
    from app.models.supplier_requisition_order import SupplierRequisitionOrderItem
    from app.models.production import ProductionTask
    from app.services.requisition_production_print import build_supplier_requisition_production_package
    from app.services.requisition_production_print_batch import build_selected_production_print_package, ProductionPrintBatchError
    _, sessions, _ = stock_flow_snapshot
    with sessions() as db:
        order = db.get(SupplierRequisitionOrder, 186)
        package = build_supplier_requisition_production_package(db, order)
        assert len(package['cards']) == 2
        assert [row['stock_replenishment_item_id'] for row in package['cards']] == [20, 21]
        assert all(row['selection_eligible'] and not row['production_task_versions'] for row in package['cards'])
        selections = [{'supplier_order_id': 186, 'source_identity': row['source_identity'],
            'selection_fingerprint': row['selection_fingerprint'], 'task_versions': []} for row in package['cards']]
        selected = build_selected_production_print_package(db, selections=selections, orders={186: order}, batch_id='isolated-stock-test')
        assert selected['card_count'] == 2
        regular = db.scalar(select(SupplierRequisitionOrderItem)
            .join(ProductionTask, ProductionTask.order_item_id == SupplierRequisitionOrderItem.order_item_id)
            .where(SupplierRequisitionOrderItem.status == 'active',
                   SupplierRequisitionOrderItem.supplier_order_id != 186,
                   ProductionTask.sales_order_item_bom_component_id.is_(None)))
        assert regular is not None
        regular.supplier_order_id = 186
        db.flush()
        db.expire(order, ['items'])
        mixed = build_supplier_requisition_production_package(db, order)
        assert len(mixed['cards']) == 3
        assert sum(bool(row['production_task_versions']) for row in mixed['cards']) == 1
        assert all(row['selection_eligible'] for row in mixed['cards'])
        mixed_selections = [{'supplier_order_id': 186, 'source_identity': row['source_identity'],
            'selection_fingerprint': row['selection_fingerprint'],
            'task_versions': row['production_task_versions']} for row in mixed['cards']]
        assert build_selected_production_print_package(db, selections=mixed_selections,
            orders={186: order}, batch_id='isolated-mixed-test')['card_count'] == 3
        db.get(StockReplenishmentOrderItem, 20).quantity += 1
        db.flush()
        with pytest.raises(ProductionPrintBatchError):
            build_selected_production_print_package(db, selections=selections, orders={186: order}, batch_id='isolated-stock-test-2')


def test_route_distinguishes_supplier_split_mold_and_already_cut():
    snapshot = SheetCuttingContract(100, 200, 2, 2, True, 4).to_snapshot()
    route = production_route_contract(snapshot, process=['模具资料，印刷、压线、模切'])
    assert [row['label'] for row in route['steps']] == ['分切', '印刷', '压线', '模切']
    assert '每张报料纸产出16片' in route['cutting_instruction']
    assert '分切' not in [row['label'] for row in production_route_contract(snapshot,
        process=['模切'], already_cut=True)['steps']]
    unsplit = SheetCuttingContract(100, 200, 1, 1, True, 4).to_snapshot()
    assert [row['label'] for row in production_route_contract(unsplit)['steps']] == ['模切']


def test_signature_legacy_null_compatibility_is_unsplit_only():
    from app.services.stock_replenishment import _cutting_signature
    legacy = _cutting_signature(None, 100, 200, 4)
    assert legacy == _cutting_signature(SheetCuttingContract(100, 200, 1, 1, True, 4).to_snapshot(), 100, 200, 4)
    assert legacy != _cutting_signature(SheetCuttingContract(50, 200, 2, 1, True, 2).to_snapshot(), 100, 200, 4)
