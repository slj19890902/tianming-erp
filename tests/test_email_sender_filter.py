import pytest
from app.services.email_sender_filter import normalize_senders, sender_matches


def test_normalization_and_complete_mailbox_matching():
    allowed = normalize_senders([' Customer@Example.com ', 'customer@example.com'])
    assert allowed == ['customer@example.com']
    assert sender_matches(['采购 <CUSTOMER@example.com>'], allowed)
    assert not sender_matches(['othercustomer@example.com'], allowed)
    assert not sender_matches(['customer@example.com.evil.test'], allowed)
    assert not sender_matches(['customer@example.com, other@example.com'], allowed)
    assert not sender_matches([], allowed)
    assert not sender_matches(['customer@example.com'], [])


@pytest.mark.parametrize('value', ['*', '@example.com', 'a@example.com\r\nFROM other@example.com', 'Name <a@example.com>', 'a@', ''])
def test_invalid_addresses_are_rejected(value):
    with pytest.raises(ValueError):
        normalize_senders([value])


def test_filtered_sync_downloads_only_exact_sender_and_uses_readonly_protocol(monkeypatch):
    import json
    from types import SimpleNamespace
    from app.services import email_intake as service

    class Database:
        def get(self, *args):
            return SimpleNamespace(sender_addresses_json=json.dumps(['customer@example.com']))
        def scalars(self, *args):
            return []

    calls = []
    class Mailbox:
        capabilities = ()
        def login(self, *args): pass
        def logout(self): pass
        def select(self, folder, readonly):
            assert readonly is True
            return 'OK', []
        def response(self, key): return 'OK', [b'7']
        def uid(self, command, *args):
            calls.append((command, args))
            if command == 'search':
                assert args == (None, 'FROM', '"customer@example.com"')
                return 'OK', [b'1 2']
            if 'HEADER.FIELDS' in args[1]:
                return 'OK', [(b'1 (UID 1)', b'From: customer@example.com\r\n\r\n'),
                              (b'2 (UID 2)', b'From: othercustomer@example.com\r\n\r\n')]
            assert args[0] == '1'
            if args[1] == '(RFC822.SIZE)': return 'OK', [b'1 (RFC822.SIZE 100)']
            assert args[1] == '(BODY.PEEK[])'
            return 'OK', [(b'1', b'From: customer@example.com\r\n\r\norder')]

    stored = []
    monkeypatch.setattr(service, 'secret', lambda db: 'test-only')
    monkeypatch.setattr(service, 'store_message', lambda db, user, validity, uid, raw: stored.append(uid) or True)
    result = service.sync_inbox(Database(), None, factory=lambda *a, **k: Mailbox())
    assert result == {'received': 1, 'remaining': 0}
    assert stored == [1]


def test_saved_empty_allowlist_never_connects_to_mailbox(monkeypatch):
    from types import SimpleNamespace
    from app.services import email_intake as service
    monkeypatch.setattr(service, 'secret', lambda db: 'test-only')
    db = SimpleNamespace(get=lambda *a: SimpleNamespace(sender_addresses_json='[]'))
    def forbidden(*a, **k):
        raise AssertionError('empty sender list must not connect')
    result = service.sync_inbox(db, None, factory=forbidden)
    assert result['received'] == result['remaining'] == 0
