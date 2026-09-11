import copy
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from app.models.order import Order
from app.models.email_intake import EmailPdfWorkingDraft
from tests.test_email_order_link import fixture, session
from tests.test_phase16_pdf_order_import import _order_import_app


def setup(tmp_path):
    app = _order_import_app(tmp_path)
    from app.api.email_intake import router
    app.include_router(router, prefix='/api/email-intake')
    payload = fixture(app)
    return app, payload


def test_working_draft_cas_replay_atomicity_and_scope(tmp_path, monkeypatch):
    app, _ = setup(tmp_path)
    path = '/api/email-intake/attachments/1/working-draft'
    draft = {'customer_po':'PO-edited','items':[{'quantity':'15','unit_price':'0.25',
        'raw_product_code':'0006','matched_product_id':1,'_inventory':{'selected':True}}],
        'preview_safety_token':'forged','integrity_check':{'integrity_status':'passed'}}
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.put(path,json={'draft':draft,'expected_version':0}).status_code == 401
        client.post('/api/auth/login',json={'username':'admin','password':'RolePass123!'})
        saved = client.put(path,json={'draft':draft,'expected_version':0})
        assert saved.status_code == 200, saved.text
        assert saved.json()['version'] == 1
        assert client.put(path,json={'draft':draft,'expected_version':0}).json()['version'] == 1
        changed = copy.deepcopy(draft);changed['customer_po']='PO-second'
        assert client.put(path,json={'draft':changed,'expected_version':0}).status_code == 409
        from app.services import email_pdf_draft
        def fail(*args): raise RuntimeError('audit failure')
        with monkeypatch.context() as context:
            context.setattr(email_pdf_draft,'audit',fail)
            assert client.put(path,json={'draft':changed,'expected_version':1}).status_code == 500
        assert client.get('/api/email-intake/1').json()['attachments'][0]['working_draft']['version'] == 1
        assert client.put(path,json={'draft':changed,'expected_version':1}).json()['version'] == 2
        client.post('/api/auth/login',json={'username':'sales','password':'RolePass123!'})
        assert client.put(path,json={'draft':changed,'expected_version':2}).status_code == 403
    with session(app) as db:
        assert db.scalar(select(func.count()).select_from(Order)) == 0
        record=db.scalar(select(EmailPdfWorkingDraft))
        assert 'preview_safety_token' not in record.content_json
        assert 'integrity_check' not in record.content_json and '_inventory' not in record.content_json
        assert record.version == 2


def test_resume_uses_fresh_source_and_preserves_edits(tmp_path, monkeypatch):
    app, payload = setup(tmp_path)
    from app.api import orders
    from app.models.product import Product
    from app.models.user import User
    with session(app) as db:
        product=db.get(Product,payload['items'][0]['product_id'])
        product_id=product.id; code=product.product_code; customer_id=product.customer_id
        admin=db.scalar(select(User).where(User.username=='admin'))
        other=db.scalar(select(User).where(User.username=='sales'))
        other.role='admin'; db.commit()
    async def fresh(file, db, user):
        return orders._finalize_pdf_preview_for_user({'source_name':'test.pdf','file_hash':'a'*64,
            'recognition_status':'needs_confirmation','customer_route':{'status':'unmatched'},
            'customer_match_status':'matched','matched_customer_id':customer_id,
            'integrity_check':{'integrity_status':'passed'},
            'items':[{'raw_product_code':code,'quantity':1}]},user)
    monkeypatch.setattr(orders,'preview_order_pdf',fresh)
    draft={'customer_po':'RESUMED-0006','matched_customer_id':customer_id,'order_date':'2026-09-11',
        'items':[{'raw_product_code':code,'matched_product_id':product_id,'product_name':'人工填写名称',
                  'quantity':17,'unit_price':3.25,'manual_product_selected':True}]}
    with TestClient(app) as client:
        client.post('/api/auth/login',json={'username':'admin','password':'RolePass123!'})
        assert client.put('/api/email-intake/attachments/1/working-draft',json={'draft':draft,'expected_version':0}).status_code == 200
        resumed=client.post('/api/email-intake/attachments/1/preview?resume=true')
        assert resumed.status_code == 200,resumed.text
        data=resumed.json();assert data['email_draft_restored'] is True
        assert data['customer_po']=='RESUMED-0006'
        assert data['items'][0]['quantity']==17 and data['items'][0]['unit_price']==3.25
        assert data['items'][0]['product_name']=='人工填写名称'
        claims=orders._decode_pdf_preview_safety_token(data['preview_safety_token'],admin)
        assert claims['source_hash']=='a'*64 and claims['recognition_status']=='needs_confirmation'
        with session(app) as db:
            db.get(Product,product_id).is_active=False;db.commit()
        data=client.post('/api/email-intake/attachments/1/preview?resume=true').json()
        assert data['items'][0]['matched_product_id'] is None
        assert data['items'][0]['quantity']==17 and data['warnings']
        client.post('/api/auth/login',json={'username':'sales','password':'RolePass123!'})
        assert client.get('/api/email-intake/1').json()['attachments'][0]['working_draft'] is None
        assert client.post('/api/email-intake/attachments/1/preview?resume=true').json()['email_draft_restored'] is False
