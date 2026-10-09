from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.core.database import create_sqlite_engine
from app.models import Base
from app.models.audit import OperationLog
from app.models.home_task import HomeTaskPreference, HomeTaskMutation
from app.models.user import User
from app.services.home_task_preferences import annotate_tasks, task_hash, replay_mutation, save_preference
from tests.test_n028_dashboard_permissions import _seed_dashboard_data
from test_fin001_invoice_tasks import fin001_app, _login
from test_finance_unpaid_history import seed


@pytest.fixture
def reminder_db(tmp_path):
    engine = create_sqlite_engine(tmp_path/'actions.sqlite3'); Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    ids = _seed_dashboard_data(factory)
    with factory() as db:
        yield db, db.get(User, ids['sales']), db.get(User, ids['finance'])
    engine.dispose()


TODAY = date(2026, 10, 9)
TASK = dict(id='pending_delivery:42', key='pending_delivery', customer_id=1,
    customer_ids=[1], order_id=10, product_code='8001', due_date='2026-10-12', message='待交100只')


def payload(**changes):
    return dict(task_id=TASK['id'], source_hash=task_hash(TASK), scope='personal',
        state='waiting_customer', reason='customer_delay', remind_on=TODAY+timedelta(days=3),
        expected_version=0, idempotency_key='fixture-request-1', **changes)


def view(db, user, task=None, today=TODAY):
    result={'tasks':[dict(task or TASK)]}
    annotate_tasks(db,user=user,workbench=result,today=today)
    return result['tasks'][0]


def test_personal_defer_expiry_new_business_restore_and_unchanged_money(reminder_db):
    db,user,other=reminder_db
    p=payload(); save_preference(db,user=user,payload=p,task=TASK,today=TODAY);db.commit()
    assert view(db,user)['attention_state']=='waiting_customer'
    assert view(db,other)['attention_state']=='active'
    assert view(db,user,today=TODAY+timedelta(days=3))['attention_state']=='active'
    assert view(db,user,task={**TASK,'message':'待交120只'})['resurface_reason']=='业务已变化，重新提醒'
    assert view(db,user,task={**TASK,'id':'pending_delivery:43'})['attention_state']=='active'
    assert replay_mutation(db,user=user,payload=p,visible_customer_ids={1})['version']==1
    assert db.scalar(select(func.count()).select_from(HomeTaskMutation))==1
    restored={**p,'state':'active','reason':'','remind_on':None,'expected_version':1,'idempotency_key':'fixture-restore-1'}
    save_preference(db,user=user,payload=restored,task=TASK,today=TODAY);db.commit();db.expire_all()
    assert view(db,user)['attention_state']=='active'
    assert db.scalar(select(func.count()).select_from(HomeTaskMutation))==2
    assert db.scalar(select(func.count()).select_from(OperationLog).where(OperationLog.action=='HOME_TASK_PREFERENCE'))==2


def test_replay_scope_payload_version_and_shared_permissions(reminder_db):
    db,user,other=reminder_db;p=payload()
    with pytest.raises(HTTPException) as error:
        save_preference(db,user=user,payload={**p,'scope':'team'},task=TASK,today=TODAY)
    assert error.value.status_code==403
    save_preference(db,user=user,payload=p,task=TASK,today=TODAY);db.commit()
    for changed,visible,status in [(p,set(),403),({**p,'reason':'verify'},{1},409)]:
        with pytest.raises(HTTPException) as error:replay_mutation(db,user=user,payload=changed,visible_customer_ids=visible)
        assert error.value.status_code==status
    with pytest.raises(HTTPException) as error:save_preference(db,user=user,payload={**p,'idempotency_key':'different-key'},task=TASK,today=TODAY)
    assert error.value.status_code==409
    with pytest.raises(HTTPException) as error:save_preference(db,user=user,payload={**p,'source_hash':'a'*64,'expected_version':1},task=TASK,today=TODAY)
    assert error.value.status_code==409
    other.role='boss';save_preference(db,user=other,payload={**p,'scope':'team'},task=TASK,today=TODAY);db.commit()
    assert view(db,other)['attention_state']=='waiting_customer'
    assert view(db,other)['attention_scope']=='team'


def test_hidden_can_restore_and_transaction_rolls_back_all_reminder_writes(reminder_db):
    db,user,other=reminder_db;p={**payload(),'state':'hidden','remind_on':None}
    save_preference(db,user=user,payload=p,task=TASK,today=TODAY);db.rollback()
    assert db.scalar(select(func.count()).select_from(HomeTaskPreference))==0
    assert db.scalar(select(func.count()).select_from(HomeTaskMutation))==0
    save_preference(db,user=user,payload=p,task=TASK,today=TODAY);db.commit()
    assert view(db,user,today=TODAY+timedelta(days=10))['attention_state']=='hidden'
    assert view(db,other)['attention_state']=='active'


def test_home_payment_matches_all_month_collections_and_excludes_unissued(fin001_app):
    from app.models.finance import Statement
    from app.api.finance import project_customer_months
    from app.api.dashboard import _authoritative_dashboard_data
    app,factory=fin001_app
    with TestClient(app) as client:
        _login(client);cid,ids=seed(client,factory)
        with factory() as db:
            user=db.scalar(select(User).where(User.username=='fin001-finance'))
            user.role='admin'
            # Partial invoice: only issued portion is collectible; a draft isn't.
            db.get(Statement,ids[0]).invoiced_amount=60
            db.get(Statement,ids[1]).confirmation_status='draft'
            db.commit()
            finance=project_customer_months(db=db,user=user,all_open=True,balance_type='pending_payment',workspace='collections',paginate=False)
            result=_authoritative_dashboard_data(db=db,user=user,visible_customer_ids=None,statement_month="2026-10",as_of="2026-10-09T12:00:00",can_view_requisition=False,can_view_incoming=False,can_view_orders=False,can_view_deliveries=False,can_view_finance=True,show_reconciliation_reminder=False)
            rows=result['rows']['pending_payment']
            mine=[r for r in rows if r['customer_id']==cid]
            assert {r['statement_month'] for r in mine}=={'2026-08','2026-10'}
            assert sum(Decimal(str(r['amount'])) for r in mine)==340
            assert sum(Decimal(str(r['amount'])) for r in rows)==Decimal(finance['summary']['pending_payment_action_amount'])
            assert not db.new and not db.dirty


def test_stock_evidence_empty_history_preserves_policy_and_scope(reminder_db,monkeypatch):
    from app.services.home_stock_advice import build_stock_advice
    from app.api import dashboard
    db,user,_=reminder_db
    warning=dict(policy_id=1,customer_id=1,product_id=999999,unit_label='只',warning_quantity=100,target_quantity=200)
    result=build_stock_advice(db,warning=warning,today=TODAY)
    assert result['shipment_count_180']==0 and result['complete_cycles']==0
    assert result['suggested_warning_quantity'] is None and result['ai_status']=='not_invoked'
    assert result['warning_quantity']==100 and not db.new and not db.dirty
    monkeypatch.setattr(dashboard,'_common_box_low_stock_warnings',lambda db,visible: [])
    monkeypatch.setattr(dashboard,'has_permission',lambda *a: True)
    with pytest.raises(HTTPException) as error:dashboard.stock_advice(999,db,user)
    assert error.value.status_code==404
