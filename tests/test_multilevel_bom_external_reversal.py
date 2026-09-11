from decimal import Decimal
from datetime import datetime, date
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func

from app.models.external_packaging_purchase import ExternalPackagingPurchaseItem, ExternalPackagingReceipt, ExternalPackagingReceiptItem, ExternalPackagingReceiptReversal
from app.models.warehouse_inventory import InventoryLot, InventoryMovement
from app.models.production import ProductionTask
from app.models.order import OrderItem
from app.services.bom_subkits import SubkitError
from test_p1_33c3_external_packaging_purchase_confirmation import purchase_app
from tests.test_multilevel_bom_external_receipts import prepare, receive, _login, _confirm
from tests.test_p1_81_receipt_purpose_flow import _seed_material_and_staging, _p181_published_map_identity


def reverse(client, rid, key='reverse', reason='现场纠正本次误收'):
    return client.post(f'/api/external-packaging-receipts/{rid}/reverse',
        json={'idempotency_key':key, 'reason':reason, 'confirmed':True})


def test_bom_history_excludes_ordinary_receipts_and_requires_login(purchase_app):
    with TestClient(purchase_app) as client:
        assert client.get('/api/external-packaging-receipts').status_code == 401
        _login(client,'purchase-admin')
        _confirm(client,purchase_app.state.fixture['order_id'])
        with purchase_app.state.session_factory() as db:
            line = db.scalar(select(ExternalPackagingPurchaseItem))
            purchase_id, line_id = line.purchase_order_id, line.id
        response = receive(client,purchase_id,line_id,'ordinary-history',1)
        assert response.status_code == 200, response.text
        result = client.get('/api/external-packaging-receipts')
        assert result.status_code == 200 and result.json()['total'] == 0
        assert client.get('/api/external-packaging-receipts',params={'page_size':101}).status_code == 422


@pytest.mark.parametrize('direct', [False,True])
@pytest.mark.parametrize('fault', [False,True])
def test_real_reversal_restores_receipt_capacity_and_preserves_facts(purchase_app, _p181_published_map_identity, direct, fault, monkeypatch):
    factory = purchase_app.state.session_factory
    _seed_material_and_staging(factory)
    oid, iid, _ = prepare(purchase_app, direct=direct, quantity=2)
    from app.services.external_packaging_receiving import _received_totals
    from app.services.external_packaging_purchase import _received_totals_by_purchase_item_ids
    from app.services.supplier_monthly_settlement import _scan_external_packaging
    from app.services import multilevel_bom_external_reversal as module
    from app.api.deliveries import _external_packaging_received
    with TestClient(purchase_app) as client:
        _login(client, 'purchase-admin')
        _confirm(client, oid)
        with factory() as db:
            line = db.scalar(select(ExternalPackagingPurchaseItem).where(ExternalPackagingPurchaseItem.sales_order_item_id == iid))
            pid, lid, qty = line.purchase_order_id, line.id, line.purchase_quantity
        first = receive(client,pid,lid,'first',2)
        assert first.status_code == 200, first.text
        first_id = first.json()['receipt']['id']
        last = receive(client,pid,lid,'last',qty-2)
        assert last.status_code == 200, last.text
        last_id = last.json()['receipt']['id']
        history = client.get('/api/external-packaging-receipts',params={'page_size':1})
        assert history.status_code == 200, history.text
        assert history.json()['total'] == 2
        row = history.json()['items'][0]
        assert row['id'] == last_id and row['supports_reversal'] and not row['reversed']
        assert 'unit_price' not in str(row) and 'cost' not in str(row)
        with factory() as db:
            facts_before = [(r.id,r.received_quantity,r.converted_finished_quantity) for r in db.scalars(select(ExternalPackagingReceiptItem))]
            assert _external_packaging_received(db,iid) is True
            moves_before = db.scalar(select(func.count()).select_from(InventoryMovement))
        too_early = reverse(client,first_id,'early')
        assert too_early.status_code == 409 and '后续实收' in too_early.text
        if fault:
            with monkeypatch.context() as patch:
                def fail(*args, **kwargs):
                    raise SubkitError('模拟撤销审计失败')
                patch.setattr(module, 'append_audit_event', fail)
                result = reverse(client,last_id)
                assert result.status_code == 409, result.text
            with factory() as db:
                assert db.get(ExternalPackagingReceiptReversal,last_id) is None
                assert db.scalar(select(func.count()).select_from(InventoryMovement)) == moves_before
                assert sum(l.quantity_reserved for l in db.scalars(select(InventoryLot))) == 2
                assert db.get(OrderItem,iid).material_status == 'received'
        result = reverse(client,last_id)
        assert result.status_code == 200, result.text
        assert reverse(client,last_id).json()['created'] is False
        history = client.get('/api/external-packaging-receipts',params={'q':row['receipt_number']}).json()
        assert history['total'] == 1 and history['items'][0]['reversed'] is True
        assert reverse(client,last_id,reason='不同撤销内容').status_code == 409
        assert receive(client,pid,lid,'last',qty-2).status_code == 409
        with factory() as db:
            assert [(r.id,r.received_quantity,r.converted_finished_quantity) for r in db.scalars(select(ExternalPackagingReceiptItem))] == facts_before
            assert _received_totals(db,{lid}) == {lid:Decimal(2)}
            assert _received_totals_by_purchase_item_ids(db,{lid}) == {lid:Decimal(2)}
            assert _external_packaging_received(db,iid) is False
            assert sum(l.quantity_reserved + l.quantity_available for l in db.scalars(select(InventoryLot))) == 0
            task = db.scalar(select(ProductionTask).where(ProductionTask.order_item_id == iid))
            assert task.finished_coverage_snapshot == 0
            assert db.get(OrderItem,iid).material_status == 'pending'
            assert db.get(OrderItem,iid).material_received_at is None
            assert db.get(OrderItem,iid).material_received_by is None
            candidates, _ = _scan_external_packaging(db,start_utc=datetime(2020,1,1),end_utc=datetime(2030,1,1))
            assert len(candidates) == 1 and candidates[0].received_quantity == 2
        # A new real receipt reuses the original frozen ratio, not the reversed output.
        again = receive(client,pid,lid,'new-receipt',qty-2)
        assert again.status_code == 200, again.text
        with factory() as db:
            assert _received_totals(db,{lid}) == {lid:qty}
            assert _external_packaging_received(db,iid) is True
            assert sum(l.quantity_reserved for l in db.scalars(select(InventoryLot))) == 2
            assert db.scalar(select(ProductionTask).where(ProductionTask.order_item_id == iid)).finished_coverage_snapshot == 2
            assert db.scalar(select(func.count()).select_from(ExternalPackagingReceipt)) == 3


def test_reversal_scope_permissions_and_loose_only(purchase_app, _p181_published_map_identity, monkeypatch):
    factory = purchase_app.state.session_factory
    oid,iid,_ = prepare(purchase_app, direct=True,quantity=1)
    with TestClient(purchase_app) as client:
        _login(client,'purchase-admin')
        _confirm(client,oid)
        with factory() as db:
            row = db.scalar(select(ExternalPackagingPurchaseItem).where(ExternalPackagingPurchaseItem.sales_order_item_id == iid))
            pid,lid = row.purchase_order_id,row.id
        result = receive(client,pid,lid,'loose',2)
        assert result.status_code == 200, result.text
        rid = result.json()['receipt']['id']
        _login(client,'purchase-sales')
        assert reverse(client,rid).status_code == 403
        _login(client,'purchase-admin')
        import app.api.external_packaging_purchases as api
        with monkeypatch.context() as patch:
            patch.setattr(api,'_visible_customer_ids',lambda *args:set())
            assert reverse(client,rid).status_code == 403
            history = client.get('/api/external-packaging-receipts')
            assert history.status_code == 200 and history.json()['total'] == 0
        assert reverse(client,rid).status_code == 200
        with factory() as db:
            assert db.scalar(select(func.count()).select_from(InventoryLot)) == 0
            assert db.get(ExternalPackagingReceiptReversal,rid) is not None


def test_two_children_reverse_latest_assembly_then_earlier_source(purchase_app, _p181_published_map_identity):
    factory = purchase_app.state.session_factory
    _seed_material_and_staging(factory)
    oid,iid,_ = prepare(purchase_app,two=True,quantity=1)
    with TestClient(purchase_app) as client:
        _login(client,'purchase-admin')
        _confirm(client,oid)
        with factory() as db:
            lines = list(db.scalars(select(ExternalPackagingPurchaseItem).where(ExternalPackagingPurchaseItem.sales_order_item_id == iid).order_by(ExternalPackagingPurchaseItem.purchase_quantity)))
            first,second = [(r.purchase_order_id,r.id,r.purchase_quantity) for r in lines]
        a = receive(client,*first[:2],'long',first[2])
        b = receive(client,*second[:2],'short',second[2])
        assert a.status_code == b.status_code == 200, (a.text,b.text)
        aid,bid = a.json()['receipt']['id'],b.json()['receipt']['id']
        failed = reverse(client,aid,'reverse-long')
        assert failed.status_code == 409, failed.text
        with factory() as db:
            assert db.get(ExternalPackagingReceiptReversal,aid) is None
            assert sum(l.quantity_reserved for l in db.scalars(select(InventoryLot))) == 1
        assert reverse(client,bid,'reverse-short').status_code == 200
        with factory() as db:
            assert sum(l.quantity_available for l in db.scalars(select(InventoryLot))) == 2
        done = reverse(client,aid,'reverse-long')
        assert done.status_code == 200, done.text
        with factory() as db:
            assert sum(l.quantity_available+l.quantity_reserved for l in db.scalars(select(InventoryLot))) == 0


def test_real_monthly_statement_blocks_receipt_reversal(purchase_app, _p181_published_map_identity):
    from app.models.user import User
    from app.models.supplier_settlement import SupplierMonthlyStatementLine
    from app.services.supplier_monthly_settlement import generate_or_refresh_settlements
    factory = purchase_app.state.session_factory
    oid,iid,_ = prepare(purchase_app,direct=True,quantity=1)
    with TestClient(purchase_app) as client:
        _login(client,'purchase-admin')
        _confirm(client,oid)
        with factory() as db:
            row = db.scalar(select(ExternalPackagingPurchaseItem).where(ExternalPackagingPurchaseItem.sales_order_item_id == iid))
            pid,lid = row.purchase_order_id,row.id
        posted = receive(client,pid,lid,'monthly-loose',2)
        assert posted.status_code == 200, posted.text
        rid = posted.json()['receipt']['id']
        with factory() as db:
            user = db.scalar(select(User).where(User.username == 'purchase-admin'))
            result = generate_or_refresh_settlements(db,settlement_month='2026-09',user=user,business_date=date(2026,9,21))
            assert db.scalar(select(SupplierMonthlyStatementLine.id)) is not None, result
            db.commit()
        blocked = reverse(client,rid)
        assert blocked.status_code == 409 and '月结' in blocked.text
        with factory() as db:
            assert db.get(ExternalPackagingReceiptReversal,rid) is None


def test_reverse_last_loose_receipt_keeps_prior_whole_stock(purchase_app, _p181_published_map_identity):
    factory = purchase_app.state.session_factory
    _seed_material_and_staging(factory)
    oid,iid,_ = prepare(purchase_app,direct=True,quantity=1)
    with TestClient(purchase_app) as client:
        _login(client,'purchase-admin')
        row = client.get(f'/api/orders/{oid}/external-packaging-purchase').json()['items'][0]
        confirmed = client.post(f'/api/orders/{oid}/external-packaging-purchase/confirm',json={
            'idempotency_key':'extra-loose-purchase','lines':[{'order_component_id':row['order_component_id'],
                'candidate_id':row['default_candidate_id'],'purchase_quantity':'4'}]})
        assert confirmed.status_code == 200, confirmed.text
        with factory() as db:
            line = db.scalar(select(ExternalPackagingPurchaseItem).where(ExternalPackagingPurchaseItem.sales_order_item_id == iid))
            pid,lid = line.purchase_order_id,line.id
        whole = receive(client,pid,lid,'whole',3)
        loose = receive(client,pid,lid,'extra-loose',1)
        assert whole.status_code == loose.status_code == 200, (whole.text,loose.text)
        with factory() as db:
            assert db.get(OrderItem,iid).material_status == 'received'
        result = reverse(client,loose.json()['receipt']['id'])
        assert result.status_code == 200, result.text
        with factory() as db:
            assert sum(l.quantity_reserved for l in db.scalars(select(InventoryLot))) == 1
            assert db.get(OrderItem,iid).material_status == 'pending'
            assert db.get(OrderItem,iid).material_received_at is None
            assert db.scalar(select(ProductionTask).where(ProductionTask.order_item_id == iid)).finished_coverage_snapshot == 1
