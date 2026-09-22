from datetime import date
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_fin001_invoice_tasks import fin001_app, _login
from test_partner_invoice_merge import prepare_partner
import pytest


def test_reconciled_history_is_independent_of_payment(fin001_app):
    app, factory = fin001_app
    with TestClient(app) as c:
        _login(c)
        prepare_partner(c, factory, create_tasks=False)
        result=c.get('/api/finance/current-customer-months',params={'all_open':True,'balance_type':'reconciled','page_size':1})
        assert result.status_code==200,result.text
        data=result.json()
        assert data['total']==1 and data['queue_counts']['reconciled']==1
        assert len(data['items'][0]['statements'])==2
        assert not c.get('/api/finance/current-customer-months',params={'all_open':True,'balance_type':'completed'}).json()['items']


def test_invoice_search_names_periods_and_actual_dates(fin001_app):
    from app.models.finance import Invoice, Statement
    app,factory=fin001_app
    with TestClient(app) as c:
        _login(c)
        tasks=prepare_partner(c,factory)
        with factory() as db:
            db.add(Invoice(statement_id=1,invoice_number='HISTORY-REAL-1',invoice_date=date(2026,9,22),invoice_amount=113))
            db.commit()
        r=c.get('/api/finance/invoice-tasks',params={'customer_keyword':'合作子客户二','month_from':'2026-08','month_to':'2026-08'}).json()
        assert len(r['items'])==1 and r['items'][0]['id']==tasks[1]['id']
        r=c.get('/api/finance/invoice-tasks',params={'customer_keyword':'天美德风','month_from':'2026-09','month_to':'2026-09','date_basis':'invoice'}).json()
        assert len(r['invoice_records'])==1
        assert r['invoice_records'][0]['invoice_date']=='2026-09-22'
        assert r['invoice_records'][0]['invoice_number']=='HISTORY-REAL-1'
        with factory() as db:
            db.add(Invoice(statement_id=1,invoice_number='HISTORY-VOID-1',invoice_date=date(2026,9,22),invoice_amount=1,invoice_status='voided'))
            db.commit()
        assert [r['invoice_number'] for r in c.get('/api/finance/invoice-tasks',params={'status':'issued'}).json()['invoice_records']]==['HISTORY-REAL-1']
        assert not c.get('/api/finance/invoice-tasks',params={'customer_keyword':'不存在'}).json()['items']
        assert c.get('/api/finance/invoice-tasks',params={'month_from':'2026-10','month_to':'2026-08'}).status_code==422


def test_missing_sales_never_becomes_a_known_zero():
    from types import SimpleNamespace as NS
    from app.services.customer_delivery_margin import _metrics,_sales_projection
    sales=_sales_projection(NS(delivered_quantity=2,unit_snapshot=None,unit_price_snapshot=3),None)
    r=_metrics([{'sales':sales,'cost':{'actual_material_cost':0,'supplemental_material_cost':0,'management_material_cost':0,'actual_cost_complete':False,'management_cost_complete':False}}])
    assert r['known_sales_lines']==0
    assert r['known_cost_lines']==0
    assert r['sales_amount'] is None and r['material_cost'] is None


def test_explicit_historical_tax_and_unit_adoption_is_atomic_and_replayable(fin001_app,monkeypatch):
    from app.models.delivery import Delivery,DeliveryItem
    from app.models.user import User
    from app.models.order import OrderItem
    from app.services import historical_sales_contract as service
    from app.services.delivery_snapshots import read_sales_contract
    app,factory=fin001_app
    with factory() as db:
        delivery=Delivery(delivery_number='HISTORY-APPROVED',customer_id=1,delivery_date=date(2026,9,1),status='dispatched',total_quantity=3)
        db.add(delivery);db.flush()
        order=db.get(OrderItem,1);order.unit_price=7;order.sales_unit_snapshot=None;order.price_tax_mode_snapshot=None
        line=DeliveryItem(delivery_id=delivery.id,source_type='order',order_item_id=1,delivered_quantity=3)
        db.add(line);db.commit();line_id=line.id;delivery_id=delivery.id
    with factory() as db:
        actor=db.scalar(select(User).where(User.role=='admin'))
        units={line_id:'只'}
        plan=service.preview(db,['2026-09'],confirmed_tax_inclusive=True,approved_units=units)
        kwargs=dict(months=['2026-09'],actor=actor,expected_preview=plan['preview_fingerprint'],batch_id='test-historical-tax-units',reason='用户核对原单并确认',confirmed_tax_inclusive=True,approved_units=units)
        with monkeypatch.context() as patch:
            def fail(*a,**k):raise RuntimeError('audit failed')
            patch.setattr('app.services.audit_log.append_audit_event',fail)
            with pytest.raises(RuntimeError):service.adopt(db,**kwargs)
            db.rollback()
        assert db.get(DeliveryItem,line_id).sales_contract_json is None
        assert db.get(Delivery,delivery_id).version==1
        service.adopt(db,**kwargs);db.commit()
        line=db.get(DeliveryItem,line_id)
        assert read_sales_contract(line)['unit_price']=='7.0000'
        assert read_sales_contract(line)['tax_mode']=='tax_inclusive'
        assert line.delivered_quantity==3 and line.unit_snapshot=='只'
        assert db.get(Delivery,delivery_id).version==2
        assert service.adopt(db,**kwargs)['replayed']
        with pytest.raises(ValueError):service.adopt(db,**dict(kwargs,approved_units={line_id:'套'}))
        with pytest.raises(ValueError):service.adopt(db,**dict(kwargs,confirmed_tax_inclusive=False))


def test_invoice_history_does_not_leak_merged_invoice_to_partial_scope(fin001_app):
    from app.models.finance import Invoice
    from app.models.user import User
    from app.models.access_control import UserCustomerScope
    from test_partner_invoice_merge import merge
    app,factory=fin001_app
    with TestClient(app) as c:
        _login(c);prepare_partner(c,factory)
        task=merge(c).json()
        with factory() as db:
            db.add(Invoice(statement_id=1,invoice_task_id=task['id'],invoice_number='SCOPE-ONE',invoice_date=date(2026,9,22),invoice_amount=339))
            user=db.scalar(select(User).where(User.username=='fin001-finance'))
            user.customer_access_mode='selected';db.add(UserCustomerScope(user_id=user.id,customer_id=1));db.commit()
        r=c.get('/api/finance/invoice-tasks',params={'customer_keyword':'天美德风'}).json()
        assert not r['invoice_records'] and all(t['id']!=task['id'] for t in r['items'])


def test_gap_paging_includes_more_than_twenty_and_preserves_scope(fin001_app):
    from app.models.delivery import Delivery,DeliveryItem
    from app.services.customer_delivery_margin import build_customer_delivery_margin
    app,factory=fin001_app
    with factory() as db:
        d=Delivery(delivery_number='GAP-21',customer_id=1,delivery_date=date(2026,9,1),status='dispatched',total_quantity=21)
        db.add(d);db.flush()
        db.add_all([DeliveryItem(delivery_id=d.id,source_type='unordered_finished',product_id=1,delivered_quantity=1,product_code_snapshot='BOX',product_name_snapshot='纸箱',unit_snapshot='只',price_source='manual') for i in range(21)]);db.commit()
        kw=dict(date_from=date(2026,9,1),date_to=date(2026,9,1))
        a=build_customer_delivery_margin(db,**kw,gap_page=1);b=build_customer_delivery_margin(db,**kw,gap_page=2)
        assert a['gaps']['total_lines']==21
        assert len(a['gaps']['examples'])==20 and len(b['gaps']['examples'])==1
        assert a['summary']==b['summary']
        assert b['gaps']['examples'][0]['required_information']
        assert not build_customer_delivery_margin(db,**kw,visible_customer_ids=set())['gaps']['examples']
