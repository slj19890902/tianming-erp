from contextlib import contextmanager
from copy import deepcopy
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from app.api.deps import get_db
from app.api.orders import _encode_pdf_preview_safety_token
from app.models.user import User
from app.models.order import Order
from app.models.email_intake import EmailIntakeMessage, EmailIntakeAttachment, EmailIntakeOrderLink
from app.models.order_import_source import OrderImportSource
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
        from app.models.material import Material
        from app.models.product import Product
        from app.models.supplier import Supplier
        from app.services.supplier_master import normalize_supplier_identity

        user = db.scalar(select(User).where(User.username == 'admin'))
        supplier = db.scalar(select(Supplier).where(Supplier.is_active.is_(True)))
        if supplier is None:
            supplier = Supplier(
                standard_name='Fixture supplier',
                normalized_name=normalize_supplier_identity('Fixture supplier'),
                is_active=True,
            )
            db.add(supplier);db.flush()
        material = db.scalar(select(Material).where(Material.code == 'A6A'))
        if material is None:
            material = Material(
                code='A6A', supplier_name=supplier.standard_name, is_active=True,
                layer_count=3, flute_type='A',
            )
            db.add(material);db.flush()
        product = db.get(Product, 1)
        product.material_id=material.id
        product.report_length_mm=800;product.report_width_mm=600
        product.layer_count=3;product.flute_type='A'
        mail = EmailIntakeMessage(mailbox_key='test', uid_validity='1', uid=1)
        db.add(mail); db.flush()
        attachment = EmailIntakeAttachment(message_id=mail.id, part_number=1,
            filename='test.pdf', sha256='a'*64, content=b'%PDF-fixture')
        db.add(attachment); db.commit()
        token = _encode_pdf_preview_safety_token({'source_name':'test.pdf', 'file_hash':'a'*64,
            'recognition_status':'needs_confirmation', 'customer_route':{'status':'needs_confirmation'},
            'customer_match_status':'matched', 'integrity_check':{'integrity_status':'passed'},
            'matched_customer_id':1, 'items':[{
                'line_no':1, 'product_code':'21312009', 'product_name':'中性内箱',
                'quantity':30, 'unit_price':'1.79',
            }]}, user)
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


def test_distinct_email_attachments_with_same_bytes_are_distinct_sources(tmp_path):
    app = _order_import_app(tmp_path)
    payload = fixture(app)
    with session(app) as db:
        second_mail = EmailIntakeMessage(mailbox_key='test', uid_validity='1', uid=2)
        db.add(second_mail);db.flush()
        second_attachment = EmailIntakeAttachment(
            message_id=second_mail.id,
            part_number=1,
            filename='same-bytes-new-occurrence.pdf',
            sha256='a'*64,
            content=b'%PDF-fixture',
        )
        db.add(second_attachment);db.commit()
        second_attachment_id = second_attachment.id
    second_payload = deepcopy(payload)
    second_payload['email_attachment_id'] = second_attachment_id
    with TestClient(app) as client:
        client.post('/api/auth/login', json={'username':'admin','password':'RolePass123!'})
        first = client.post('/api/orders', json=payload)
        second = client.post('/api/orders', json=second_payload)
    assert first.status_code == second.status_code == 201
    assert first.json()['id'] != second.json()['id']
    with session(app) as db:
        assert db.scalar(select(func.count()).select_from(Order)) == 2
        assert db.scalar(select(func.count()).select_from(EmailIntakeOrderLink)) == 2
        assert db.scalar(select(func.count()).select_from(OrderImportSource)) == 2


def test_pdf_mail_list_and_source_follow_live_orders_and_duplicate_content(tmp_path):
    app = _order_import_app(tmp_path)
    from app.api.email_intake import router
    app.include_router(router, prefix='/api/email-intake')
    payload = fixture(app)
    with session(app) as db:
        for number in (2, 3):
            mail = EmailIntakeMessage(mailbox_key='test', uid_validity='1', uid=number)
            db.add(mail); db.flush()
            db.add(EmailIntakeAttachment(message_id=mail.id, part_number=1,
                filename='copy.pdf', sha256=('a' if number == 2 else 'b')*64, content=b'%PDF-fixture'))
        # Two matching attachments in one mail still count the order once.
        db.add(EmailIntakeAttachment(message_id=1, part_number=2,
            filename='again.pdf', sha256='a'*64, content=b'%PDF-fixture'))
        db.commit()
    with TestClient(app) as client:
        assert client.get('/api/email-intake/orders/1/source').status_code == 401
        client.post('/api/auth/login', json={'username':'admin','password':'RolePass123!'})
        created = client.post('/api/orders', json=payload)
        assert created.status_code == 201, created.text
        order_id = created.json()['id']
        linked = client.get('/api/email-intake?association=linked').json()
        assert linked['total'] == 2
        assert {row['id']: row['linked_order_count'] for row in linked['items']} == {1: 1, 2: 1}
        unlinked = client.get('/api/email-intake?association=unlinked').json()
        assert unlinked['total'] == 1 and unlinked['items'][0]['id'] == 3
        source = client.get(f'/api/email-intake/orders/{order_id}/source')
        assert source.headers['cache-control'] == 'private, no-store'
        assert source.json() == {'sources':[{'id':1,'message_id':1,'filename':'test.pdf'}]}
        assert client.get('/api/email-intake/orders/999999/source').status_code == 404
        client.post('/api/auth/login', json={'username':'sales','password':'RolePass123!'})
        assert client.get(f'/api/email-intake/orders/{order_id}/source').status_code == 403
        assert client.get('/api/email-intake?association=linked').status_code == 403
        with session(app) as db:
            db.scalar(select(EmailIntakeOrderLink)).order_id = None
            db.commit()
        client.post('/api/auth/login', json={'username':'admin','password':'RolePass123!'})
        assert client.get('/api/email-intake?association=linked').json()['total'] == 0
        assert client.get('/api/email-intake?association=unlinked').json()['total'] == 3
        assert client.get('/api/email-intake/2').json()['attachments'][0]['orders'][0]['id'] is None
        assert client.get(f'/api/email-intake/orders/{order_id}/source').json()['sources'] == []
