from datetime import datetime, timedelta
import json
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select
from app.models.delivery import DeliveryItem
from app.models.finance import ReturnReceipt, ReturnReceiptItem, Statement, StatementItem
from app.models.audit import OperationLog
from app.services.delivery_location_retention import policies
from tests.test_delivery_dispatch_commands import delivery_api_app, seed, command, state, _login

NOW = datetime(2026, 10, 17, 10, 0, 0)


def receipt_fixture(client, factory, *, age=0):
    created=seed(client,factory);did=created['id']
    sent=client.put(f'/api/deliveries/{did}/dispatch',json=command(client,did,'retention-dispatch'))
    assert sent.status_code==200,sent.text
    with factory() as db:
        receipt=ReturnReceipt(delivery_id=did,actual_received_date=NOW.date(),status='confirmed',created_by=1,created_at=NOW-timedelta(seconds=age))
        db.add(receipt);db.flush()
        line=db.scalar(select(DeliveryItem).where(DeliveryItem.delivery_id==did,DeliveryItem.is_current.is_(True)))
        item=ReturnReceiptItem(return_receipt_id=receipt.id,delivery_item_id=line.id,actual_received_quantity=line.delivered_quantity)
        db.add(item);db.commit()
        return did,receipt.id,item.id


def test_delivery_position_24h_boundary_cancellation_and_reconfirmation(delivery_api_app):
    app,factory=delivery_api_app
    with TestClient(app) as c:
        _login(c,'admin');did,rid,_=receipt_fixture(c,factory,age=86399)
        with factory() as db:
            r=db.get(ReturnReceipt,rid)
            assert policies(db,{did:object()},{did:r},now=NOW)[did]['visible']
            assert not policies(db,{did:object()},{did:r},now=NOW+timedelta(seconds=1))[did]['visible']
            r.status='cancelled';db.commit()
            assert policies(db,{did:object()},{did:r},now=NOW+timedelta(days=5))[did]['visible']
            r.status='confirmed'
            db.add(OperationLog(user_id=1,resource='ReturnReceipt',entity_id=rid,action='UPDATE_RETURN_RECEIPT',created_at=NOW,details=json.dumps({'before':{'status':'cancelled'}})))
            db.commit()
            p=policies(db,{did:object()},{did:r},now=NOW+timedelta(seconds=86399))[did]
            assert p['visible'] and p['confirmed_at']=='2026-10-17T10:00:00Z'
            assert not policies(db,{did:object()},{did:r},now=NOW+timedelta(days=1))[did]['visible']


def test_ordinary_receipt_edit_does_not_restart_countdown_and_confirmed_statement_hides_immediately(delivery_api_app):
    app,factory=delivery_api_app
    with TestClient(app) as c:
        _login(c,'admin');did,rid,riid=receipt_fixture(c,factory,age=3600)
        with factory() as db:
            r=db.get(ReturnReceipt,rid)
            db.add(OperationLog(user_id=1,resource='ReturnReceipt',entity_id=rid,action='UPDATE_RETURN_RECEIPT',created_at=NOW,details=json.dumps({'before':{'status':'confirmed'}})))
            statement=Statement(statement_number='RETENTION',customer_id=1,statement_month='2026-10',confirmation_status='draft')
            db.add(statement);db.flush()
            db.add(StatementItem(statement_id=statement.id,return_receipt_item_id=riid,actual_received_quantity=50,unit_price_snapshot=1,unit_cost_snapshot=0,receivable_amount=50,gross_profit_amount=0))
            db.commit()
            p=policies(db,{did:object()},{did:r},now=NOW)[did]
            assert p['visible'] and p['confirmed_at']=='2026-10-17T09:00:00Z'
            statement.confirmation_status='confirmed';db.commit()
            p=policies(db,{did:object()},{did:r},now=NOW)[did]
            assert not p['visible'] and p['reason']=='reconciled'


def test_summary_detail_location_policy_is_read_only_and_dispatch_history_kept(delivery_api_app):
    app,factory=delivery_api_app
    with TestClient(app) as c:
        _login(c,'admin');did,rid,_=receipt_fixture(c,factory)
        with factory() as db:
            r=db.get(ReturnReceipt,rid);r.created_at=datetime.utcnow()-timedelta(days=2);db.commit()
        before=state(factory)
        for view in ('summary','full'):
            response=c.get('/api/deliveries',params={'view':view});assert response.status_code==200,response.text
            row=next(r for r in response.json()['items'] if r['id']==did)
            assert row['location_retention']['reason']=='receipt_expired'
        detail=c.get(f'/api/deliveries/{did}');assert detail.status_code==200,detail.text
        assert not detail.json()['location_retention']['visible']
        assert state(factory)==before
        with factory() as db:
            db.get(ReturnReceipt,rid).status='cancelled';db.commit()
        assert c.get(f'/api/deliveries/{did}').json()['location_retention']['visible']
        assert c.get(f'/api/deliveries/{did}').json()['items'][0]['inventory_sources']


def test_retired_features_have_no_routes_or_active_implementation():
    from app.api.deliveries import router
    root=Path(__file__).resolve().parents[1]
    assert '/route-suggestions' not in [getattr(r,'path','') for r in router.routes]
    for path in ('app/api/product_import.py','app/services/product_import_workbook.py','app/services/mixed_sample_import.py'):
        assert not (root/path).exists()
    main=(root/'app/main.py').read_text()
    html=(root/'static/index.html').read_text()
    for text in ('product_import_router','master-product-import'):assert text not in main
    for text in ('deliveryRoute','productWorkbookImport','mixedSampleImport','送货路线辅助','下载样品 Excel','样品 Excel 导入','混合样品整理'):assert text not in html
