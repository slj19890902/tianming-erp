import json
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from app.models.email_intake import EmailIntakeSettings, EmailIntakeAttachment, EmailPdfRecognition
from app.models.order import Order
from app.services import email_intake as service
from app.services.email_pdf_queue import recognize_pending, pending_attachments, classify
from tests.test_email_intake import mail_bytes
from tests.test_p1_76_mobile_portal import mobile_portal_app, mobile_erp_app, _login


def test_queue_dedup_filter_cache_and_no_sales_facts(mobile_portal_app, monkeypatch):
    app, ids, factory = mobile_portal_app
    from app.api.email_intake import router
    from app.api import orders
    app.include_router(router, prefix='/api/email-intake')
    calls=[]
    def parse(*args):
        calls.append(1)
        return {'recognition_status':'failed','items':[], 'warnings':['fixture needs improvement']}
    monkeypatch.setattr(orders, '_parse_order_pdf_preview', parse)
    with factory() as db:
        db.add(EmailIntakeSettings(id=1, encrypted_secret='fixture', sender_addresses_json=json.dumps(['sample@example.invalid'])))
        db.commit()
        service.store_message(db,None,'queue-test',1,mail_bytes())
        service.store_message(db,None,'queue-test',2,mail_bytes())
        baseline=db.scalar(select(func.count()).select_from(Order))
        assert len(pending_attachments(db))==1
        assert recognize_pending(db)==1
        assert recognize_pending(db)==0
        assert len(calls)==1
        assert db.scalar(select(func.count()).select_from(EmailPdfRecognition))==1
        assert db.scalar(select(func.count()).select_from(Order))==baseline
        db.get(EmailIntakeSettings,1).sender_addresses_json='[]'
        db.commit()
        assert pending_attachments(db)==[]
        db.get(EmailIntakeSettings,1).sender_addresses_json=json.dumps(['sample@example.invalid'])
        db.commit()
    with TestClient(app,base_url='https://testserver') as client:
        _login(client,'mobile-admin')
        response=client.get('/api/email-intake/queue/summary')
        assert response.status_code==200,response.text
        assert response.json()=={'pending':1,'ready':0,'improve':1,'recognizing':0}
        assert 'no-store' in response.headers['cache-control']
        opened=client.post('/api/email-intake/queue/preview')
        assert opened.status_code==200,opened.text
        assert len(opened.json()['drafts'])==1
        assert opened.json()['drafts'][0]['email_queue_status']=='improve'
        assert client.get('/api/email-intake/queue/summary').json()['pending']==1
        retried=client.post('/api/email-intake/queue/1/retry')
        assert retried.status_code==200,retried.text
        assert len(calls)==2
        _login(client,'mobile-scoped')
        assert client.get('/api/email-intake/queue/summary').status_code==403
        assert client.post('/api/email-intake/queue/preview').status_code==403


def test_classify_requires_complete_matched_integer_quantities():
    draft={'recognition_status':'recognized','integrity_check':{'integrity_status':'passed'},'matched_customer_id':1,'items':[{'matched_product_id':2,'quantity':3,'unit_price':0}]}
    assert classify(draft)=='ready'
    for field,value in [('quantity',0),('quantity',1.5),('quantity','bad'),('unit_price',None),('matched_product_id',None)]:
        item={**draft['items'][0],field:value}
        assert classify({**draft,'items':[item]})=='improve'
