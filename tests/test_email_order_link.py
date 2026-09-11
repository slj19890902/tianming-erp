from contextlib import contextmanager
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from app.api.deps import get_db
from app.api.orders import _encode_pdf_preview_safety_token
from app.models.user import User
from app.models.order import Order
from app.models.email_intake import EmailIntakeMessage, EmailIntakeAttachment, EmailIntakeOrderLink
from tests.test_phase16_pdf_order_import import _order_import_app, _pdf_order_payload


@contextmanager
def session(app):
    generator = app.dependency_overrides[get_db]()
    try:
        yield next(generator)
    finally:
        generator.close()


def fixture(app):
    with session(app) as db:
        user = db.scalar(select(User).where(User.username == 'admin'))
        mail = EmailIntakeMessage(mailbox_key='test', uid_validity='1', uid=1)
        db.add(mail); db.flush()
        attachment = EmailIntakeAttachment(message_id=mail.id, part_number=1,
            filename='test.pdf', sha256='a'*64, content=b'%PDF-fixture')
        db.add(attachment); db.commit()
        token = _encode_pdf_preview_safety_token({'source_name':'test.pdf', 'file_hash':'a'*64,
            'recognition_status':'needs_confirmation', 'customer_route':{'status':'needs_confirmation'},
            'customer_match_status':'matched', 'integrity_check':{'integrity_status':'passed'},
            'matched_customer_id':1}, user)
        payload = _pdf_order_payload(confirmed=True, token=token)
        payload['email_attachment_id'] = attachment.id
        return payload


def test_email_order_save_replay_and_changed_payload(tmp_path):
    app = _order_import_app(tmp_path)
    from app.api.email_intake import router
    app.include_router(router, prefix='/api/email-intake')
    payload = fixture(app)
    with TestClient(app) as client:
        client.post('/api/auth/login', json={'username':'admin','password':'RolePass123!'})
        result = client.post('/api/orders', json=payload)
        assert result.status_code == 201, result.text
        replay = client.post('/api/orders', json=payload)
        assert replay.status_code == 201, replay.text
        assert replay.json()['id'] == result.json()['id']
        detail = client.get('/api/email-intake/1').json()
        assert detail['attachments'][0]['orders'][0]['id'] == result.json()['id']
        payload['items'][0]['quantity'] += 1
        changed = client.post('/api/orders', json=payload)
        assert changed.status_code == 409 and '改单' in changed.text
    with session(app) as db:
        assert db.scalar(select(func.count()).select_from(Order)) == 1
        assert db.scalar(select(func.count()).select_from(EmailIntakeOrderLink)) == 1


def test_email_source_failure_rolls_back_order(tmp_path, monkeypatch):
    from app.services import email_order_link
    app = _order_import_app(tmp_path)
    payload = fixture(app)
    def fail(*args):
        raise RuntimeError('injected source audit failure')
    monkeypatch.setattr(email_order_link, 'attach', fail)
    with TestClient(app, raise_server_exceptions=False) as client:
        client.post('/api/auth/login', json={'username':'admin','password':'RolePass123!'})
        result = client.post('/api/orders', json=payload)
        assert result.status_code == 500, result.text
    with session(app) as db:
        assert db.scalar(select(func.count()).select_from(Order)) == 0
        assert db.scalar(select(func.count()).select_from(EmailIntakeOrderLink)) == 0
        from app.models.production import ProductionTask
        from app.models.order import OrderItem
        assert db.scalar(select(func.count()).select_from(ProductionTask)) == 0
        assert db.scalar(select(func.count()).select_from(OrderItem)) == 0


def test_email_source_scope_and_hash_cannot_be_forged(tmp_path):
    from tests.test_phase16_pdf_order_import import _signed_pdf_preview_token
    app = _order_import_app(tmp_path)
    payload = fixture(app)
    with session(app) as db:
        db.get(EmailIntakeAttachment, payload['email_attachment_id']).sha256 = 'b'*64
        db.commit()
    with TestClient(app) as client:
        client.post('/api/auth/login', json={'username':'admin','password':'RolePass123!'})
        response = client.post('/api/orders', json=payload)
        assert response.status_code == 409 and '不一致' in response.text
        client.post('/api/auth/login', json={'username':'sales','password':'RolePass123!'})
        payload['pdf_import_confirmation']['preview_safety_token'] = _signed_pdf_preview_token(app)
        response = client.post('/api/orders', json=payload)
        assert response.status_code == 403
    with session(app) as db:
        assert db.scalar(select(func.count()).select_from(Order)) == 0
