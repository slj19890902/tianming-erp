from email.message import EmailMessage
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from app.models.email_intake import EmailIntakeSettings, EmailIntakeMessage, EmailIntakeAttachment
from app.services import email_intake as service
from tests.test_p1_76_mobile_portal import mobile_portal_app, mobile_erp_app, _login


def mail_bytes():
    message = EmailMessage()
    message['Subject'] = '订单测试 <script>alert(1)</script>'
    message['From'] = 'sample@example.invalid'
    message['Message-ID'] = '<same-source@example.invalid>'
    message.set_content('请核对订单，不得自动创建')
    message.add_attachment(b'%PDF-1.4\nfixture', maintype='application', subtype='pdf', filename='../订单.pdf')
    message.add_attachment(b'never-run', maintype='application', subtype='octet-stream', filename='unsafe.exe')
    return message.as_bytes()


def test_mail_permissions_config_encryption_and_version(mobile_portal_app):
    app, ids, factory = mobile_portal_app
    from app.api.email_intake import router
    app.include_router(router, prefix='/api/email-intake')
    with TestClient(app, base_url='https://testserver') as client:
        assert client.get('/api/email-intake/settings').status_code == 401
        _login(client, 'mobile-admin')
        assert client.get('/api/email-intake/settings').json()['configured'] is False
        assert client.post('/api/email-intake/sync').status_code == 422
        payload = {'authorization_code': 'fake-uat-secret-only', 'expected_version': 0}
        response = client.put('/api/email-intake/settings', json=payload)
        assert response.status_code == 200, response.text
        public = client.get('/api/email-intake/settings')
        assert public.json()['version'] == 1 and 'fake-uat' not in public.text
        assert client.put('/api/email-intake/settings', json=payload).status_code == 409
        rejected = client.put('http://192.168.3.80/api/email-intake/settings', json=payload)
        assert rejected.status_code in (400, 401)
        with factory() as db:
            assert 'fake-uat' not in db.get(EmailIntakeSettings, 1).encrypted_secret
            assert service.secret(db) == payload['authorization_code']
        _login(client, 'mobile-scoped')
        for path in ('', '/settings', '/1', '/attachments/1/download'):
            assert client.get('/api/email-intake' + path).status_code == 403


def test_imap_readonly_replay_attachments_and_state(mobile_portal_app, monkeypatch):
    app, ids, factory = mobile_portal_app
    from app.api.email_intake import router
    app.include_router(router, prefix='/api/email-intake')
    calls = []
    raw = mail_bytes()

    class FakeIMAP:
        capabilities = ()
        def __init__(self, host, port, **kwargs):
            assert host == 'imap.126.com' and port == 993
            assert kwargs['ssl_context'].check_hostname
        def login(self, account, password):
            assert account == service.ACCOUNT
        def select(self, folder, readonly):
            assert folder == 'INBOX' and readonly is True
            return 'OK', []
        def response(self, name):
            return name, [b'123']
        def uid(self, command, *args):
            calls.append((command, args))
            if command == 'search':
                return 'OK', [b'1 2']
            if args[1] == '(RFC822.SIZE)':
                return 'OK', [b'1 (RFC822.SIZE ' + str(len(raw)).encode() + b')']
            assert args[1] == '(BODY.PEEK[])'
            return 'OK', [(b'header', raw)]
        def logout(self):
            calls.append(('logout', ()))

    with TestClient(app, base_url='https://testserver') as client:
        _login(client, 'mobile-admin')
        assert client.put('/api/email-intake/settings', json={'authorization_code': 'fake-secret', 'expected_version': 0}).status_code == 200
        with factory() as db:
            from app.models.user import User
            user = db.scalar(select(User).where(User.username == 'mobile-admin'))
            assert service.sync_inbox(db, user, FakeIMAP)['received'] == 2
            assert service.sync_inbox(db, user, FakeIMAP)['received'] == 0
            assert db.scalar(select(func.count()).select_from(EmailIntakeMessage)) == 2
            attachments = list(db.scalars(select(EmailIntakeAttachment).order_by(EmailIntakeAttachment.id)))
            assert len(attachments) == 2
            assert attachments[0].filename == '订单.pdf'
            assert attachments[1].duplicate_of == attachments[0].id
            message_id = attachments[0].message_id
        row = client.get(f'/api/email-intake/{message_id}').json()
        assert 'unsafe.exe' in row['notice']
        change = {'status': 'ignored', 'expected_version': row['version']}
        assert client.put(f'/api/email-intake/{message_id}/state', json=change).status_code == 200
        assert client.put(f'/api/email-intake/{message_id}/state', json=change).status_code == 409
        response = client.get('/api/email-intake/attachments/1/download')
        assert response.status_code == 200 and response.content.startswith(b'%PDF')
        assert response.headers['content-disposition'].startswith('attachment;')
        from app.api import orders
        parsed = []
        def fake_parser(*args):
            parsed.append(True)
            raise orders.PdfParseError('测试文件无明细')
        monkeypatch.setattr(orders, '_parse_order_pdf_preview', fake_parser)
        response = client.post('/api/email-intake/attachments/1/preview')
        assert response.status_code == 200, response.text
        assert parsed == [True]
    assert not any(command in ('store', 'copy', 'expunge') for command, args in calls)


def test_oversize_and_encrypted_secret_failure(mobile_portal_app):
    import pytest
    app, ids, factory = mobile_portal_app
    with pytest.raises(ValueError, match='16MB'):
        service.parse_message(b'x' * (service.MAX_MESSAGE + 1))
    with pytest.raises(ValueError):
        service.protect('bad secret', decrypt=True)
    # No HTML is exposed as executable markup, even for HTML-only mail.
    message = EmailMessage()
    message.set_content('<script>steal()</script><img src="https://example.invalid/pixel">', subtype='html')
    parsed, attachments = service.parse_message(message.as_bytes())
    assert parsed['body'] == '' and 'HTML' in parsed['notice']
