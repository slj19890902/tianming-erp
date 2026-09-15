import json
from datetime import datetime
from email.message import EmailMessage
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from app.models.email_intake import (EmailIntakeSettings, EmailIntakeMessage, EmailIntakeAttachment,
    EmailPdfRecognition, EmailPdfDisposition, EmailIntakeOrderLink)
from app.models.order import Order
from app.models.customer import Customer
from app.models.user import User
from app.services import email_intake as service
from app.services.email_pdf_queue import pending_attachments, queue_entries, settle_queue, before_start, record_disposition
from app.services.order_pdf_import import mark_order_duplicate
from tests.test_email_intake import mail_bytes
from tests.test_p1_76_mobile_portal import mobile_portal_app, mobile_erp_app, _login


def configure(db):
    db.add(EmailIntakeSettings(id=1,encrypted_secret='fixture',sender_addresses_json=json.dumps(['sample@example.invalid'])))
    db.commit()


def add_pdf(db, uid, payload=b'fixture', received='', raw=None):
    message=EmailMessage()
    message['From']='sample@example.invalid'
    if received: message['Date']=received
    message.set_content('fixture')
    message.add_attachment(b'%PDF-'+payload,maintype='application',subtype='pdf',filename='fixture.pdf')
    service.store_message(db,None,'terminal-test',uid,message.as_bytes())
    attachment=db.scalar(select(EmailIntakeAttachment).order_by(EmailIntakeAttachment.id.desc()))
    if raw is not None:
        db.add(EmailPdfRecognition(sha256=attachment.sha256,attachment_id=attachment.id,status='improve',draft_json=json.dumps(raw),recognized_at=datetime.now()))
        db.commit()
    return attachment


def test_cutoff_mail_and_order_dates_unknown_stay_visible(mobile_portal_app):
    _,_,factory=mobile_portal_app
    with factory() as db:
        configure(db)
        add_pdf(db,1,b'oldmail','Mon, 31 Aug 2026 12:00:00 +0800')
        add_pdf(db,2,b'oldorder','Tue, 01 Sep 2026 12:00:00 +0800',{'order_date':'2026-08-31'})
        at_start=add_pdf(db,3,b'start','Tue, 01 Sep 2026 00:00:00 +0800',{'order_date':'2026-09-01'})
        unknown=add_pdf(db,4,b'unknown',raw={'order_date':'unrecognized'})
        assert [a.id for a in pending_attachments(db)]==[at_start.id,unknown.id]
        assert before_start('Mon, 31 Aug 2026 16:00:00 +0000',mail_date=True) is False


@pytest.mark.parametrize('action',['processed','deleted','duplicate'])
def test_terminal_action_replay_new_uid_permissions_and_no_sales_change(mobile_portal_app,action):
    app,_,factory=mobile_portal_app
    from app.api.email_intake import router
    app.include_router(router,prefix='/api/email-intake')
    with factory() as db:
        configure(db)
        attachment=add_pdf(db,1)
        aid,sha=attachment.id,attachment.sha256
        baseline=db.scalar(select(func.count()).select_from(Order))
    with TestClient(app,base_url='https://testserver') as client:
        url=f'/api/email-intake/queue/{aid}/disposition'
        body={'sha256':sha,'action':action}
        assert client.post(url,json=body).status_code==401
        _login(client,'mobile-scoped')
        assert client.post(url,json=body).status_code==403
        _login(client,'mobile-admin')
        assert client.post(url,json={**body,'sha256':'0'*64}).status_code==409
        assert client.post(url,json=body).status_code==200
        assert client.post(url,json=body).status_code==200
        assert client.post(url,json={**body,'action': 'deleted' if action!='deleted' else 'processed'}).status_code==409
        with factory() as db:
            add_pdf(db,2)
            assert len(list(db.scalars(select(EmailPdfDisposition))))==1
            assert db.scalar(select(func.count()).select_from(Order))==baseline
        assert client.get('/api/email-intake/queue/summary').json()['pending']==0
        preview=client.post('/api/email-intake/queue/preview').json()
        assert preview['drafts']==[] and len(preview['excluded'])==1


def test_customer_po_duplicate_ignores_spec_price_format_and_stays_after_deletion(mobile_portal_app):
    _,_,factory=mobile_portal_app
    with factory() as db:
        configure(db)
        order=db.scalar(select(Order).where(Order.customer_po=='PO-MOBILE-001'))
        customer=db.get(Customer,order.customer_id)
        raw={'customer_name_raw':customer.name,'customer_po':order.customer_po,'order_date':'2026-09-01','items':[{'product_code':'same','quantity':10,'specification':'42*31*26cm','unit_price':'2.50'}]}
        a=add_pdf(db,1,raw=raw)
        marked=mark_order_duplicate(db,{**raw,'matched_customer_id':customer.id})
        assert marked['duplicate_status']=='duplicate_skipped'
        assert '明细不同' not in marked['duplicate_reason']
        assert order.order_number in marked['duplicate_reason']
        other=db.scalar(select(Order).where(Order.customer_po=='PO-OTHER-MOBILE'))
        cross=mark_order_duplicate(db,{**raw,'matched_customer_id':other.customer_id})
        assert not cross.get('duplicate_status')
        assert pending_attachments(db)==[]
        settle_queue(db)
        assert db.get(EmailPdfDisposition,a.sha256).action=='duplicate'
        # Order changes/removal cannot resurrect the same source PDF.
        order.customer_po='fixture-renamed'
        db.commit()
        add_pdf(db,2)
        assert pending_attachments(db)==[]


def test_deleted_link_and_ignored_duplicate_never_return(mobile_portal_app):
    _,_,factory=mobile_portal_app
    with factory() as db:
        configure(db)
        a=add_pdf(db,1,b'linked')
        user=db.scalar(select(User).where(User.username=='mobile-admin'))
        db.add(EmailIntakeOrderLink(import_key='1'*64,payload_hash='2'*64,attachment_id=a.id,order_id=None,actor_id=user.id))
        b=add_pdf(db,2,b'ignored')
        db.get(EmailIntakeMessage,b.message_id).status='ignored'
        db.commit()
        add_pdf(db,3,b'linked');add_pdf(db,4,b'ignored')
        assert pending_attachments(db)==[]
        settle_queue(db)
        assert db.get(EmailPdfDisposition,a.sha256).action=='processed'
        assert db.get(EmailPdfDisposition,b.sha256).action=='deleted'


def test_disposition_audit_failure_rolls_back(mobile_portal_app,monkeypatch):
    _,_,factory=mobile_portal_app
    with factory() as db:
        configure(db);a=add_pdf(db,1)
        def fail(*args): raise RuntimeError('audit unavailable')
        monkeypatch.setattr(service,'audit',fail)
        with pytest.raises(RuntimeError): record_disposition(db,a,'processed')
        db.rollback()
        assert db.get(EmailPdfDisposition,a.sha256) is None
        assert len(pending_attachments(db))==1


def test_direct_pdf_same_po_cannot_create_again_with_changed_details(tmp_path):
    from tests.test_email_order_link import session, fixture
    from tests.test_phase16_pdf_order_import import _order_import_app
    app=_order_import_app(tmp_path)
    payload=fixture(app)
    payload.pop('email_attachment_id')
    # Recognized orders require complete current master data since v418.
    from app.models.product import Product
    from app.models.material import Material
    from app.models.supplier import Supplier
    from app.services.supplier_master import normalize_supplier_identity
    with session(app) as db:
        db.add(Supplier(standard_name='Fixture supplier', normalized_name=normalize_supplier_identity('Fixture supplier'), is_active=True))
        material=Material(code='A6A',supplier_name='Fixture supplier',is_active=True,layer_count=3,flute_type='A')
        db.add(material);db.flush()
        product=db.get(Product,payload['items'][0]['product_id'])
        product.material_id=material.id
        product.report_length_mm=800;product.report_width_mm=600
        product.layer_count=3;product.flute_type='A'
        db.commit()
    with TestClient(app) as client:
        client.post('/api/auth/login',json={'username':'admin','password':'RolePass123!'})
        first=client.post('/api/orders',json=payload)
        assert first.status_code==201,first.text
        payload['items'][0]['quantity']+=1
        second=client.post('/api/orders',json=payload)
        assert second.status_code==409 and '已录入' in second.text,second.text
    with session(app) as db:
        assert db.scalar(select(func.count()).select_from(Order))==1
        configure(db)
        db.get(EmailIntakeMessage,1).sender='sample@example.invalid'
        # Source hash from direct PDF import also suppresses email, even after PO editing.
        db.scalar(select(Order)).customer_po='edited-after-import'
        db.commit()
        assert pending_attachments(db)==[]


def test_processed_pdf_rejects_stale_unsaved_order(tmp_path):
    from tests.test_email_order_link import session, fixture
    from tests.test_phase16_pdf_order_import import _order_import_app
    app=_order_import_app(tmp_path);payload=fixture(app)
    with session(app) as db:
        attachment=db.get(EmailIntakeAttachment,payload['email_attachment_id'])
        record_disposition(db,attachment,'deleted');db.commit()
    with TestClient(app) as client:
        client.post('/api/auth/login',json={'username':'admin','password':'RolePass123!'})
        reply=client.post('/api/orders',json=payload)
        assert reply.status_code==409 and '已处理或删除' in reply.text
    with session(app) as db:
        assert db.scalar(select(func.count()).select_from(Order))==0
