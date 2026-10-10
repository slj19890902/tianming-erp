import json

import pytest

from app.core import mac_keychain
from app.services import email_intake
from desktop_assistant import ai_config, credential_store, backup_settings, gui
from tests.test_p1_76_mobile_portal import mobile_portal_app, mobile_erp_app


@pytest.fixture
def local_store(monkeypatch):
    values = {}

    class Store:
        def put(self, account, raw):
            values[account] = raw

        def get(self, account):
            return values[account]

    monkeypatch.setattr(mac_keychain, 'Keychain', Store)
    monkeypatch.setattr('sys.platform', 'darwin')
    return values


def test_mac_mailbox_ai_backup_routes_preserve_only_scoped_refs(tmp_path, local_store):
    secret = 'synthetic-only-备份凭据'
    mailbox = email_intake.protect(secret)
    assert email_intake.protect(mailbox, decrypt=True) == secret
    backup = backup_settings.protect(secret)
    assert gui.unprotect(backup) == secret
    assert backup_settings.unprotect(backup) == secret
    for purpose, save, load, name in [
        ('openai', ai_config.save_openai_api_key, ai_config.load_openai_api_key, 'ai-provider.json'),
        ('deepseek', ai_config.save_deepseek_api_key, ai_config.load_deepseek_api_key, 'deepseek-provider.json'),
    ]:
        key = 'sk-synthetic-only-' + purpose*4
        save(tmp_path, key)
        content = (tmp_path / 'control' / name).read_text()
        assert key not in content
        assert json.loads(content)['protected_api_key'].startswith(f'tm-keychain-v1:{purpose}:')
        assert load(tmp_path) == key
    assert len(local_store) == 4
    with pytest.raises(ValueError, match='安全移交'):
        email_intake.protect(backup, decrypt=True)
    with pytest.raises(ValueError, match='安全移交'):
        backup_settings.unprotect(mailbox)


def test_mac_legacy_ciphertext_never_replaced_or_sent(tmp_path, local_store, monkeypatch):
    path = tmp_path / 'control/ai-provider.json'
    path.parent.mkdir()
    path.write_text(json.dumps({'provider': 'openai', 'protected_api_key': 'old-windows-dpapi'}))
    before = path.read_bytes()
    with pytest.raises(ValueError, match='安全移交'):
        ai_config.load_openai_api_key(tmp_path)
    with pytest.raises(ValueError, match='安全移交'):
        email_intake.protect('old-windows-dpapi', decrypt=True)
    with pytest.raises(ValueError, match='安全移交'):
        gui.unprotect('old-windows-dpapi')
    assert path.read_bytes() == before
    assert local_store == {}


def test_mail_api_permissions_versions_and_secret_hiding(mobile_portal_app, local_store, monkeypatch):
    from tests.test_email_intake import test_mail_permissions_config_encryption_and_version
    monkeypatch.setenv('ERP_HOME_REHEARSAL', '1')
    test_mail_permissions_config_encryption_and_version(mobile_portal_app)
    assert len(local_store) == 1


def test_windows_keeps_existing_dpapi_and_rejects_mac_reference(monkeypatch):
    from desktop_assistant import windows
    monkeypatch.setattr('sys.platform', 'win32')
    calls = []
    monkeypatch.setattr(windows, 'protect', lambda value: calls.append(('save', value)) or 'legacy-cipher')
    monkeypatch.setattr(windows, 'unprotect', lambda value: calls.append(('read', value)) or 'synthetic')
    assert credential_store.protect('synthetic', 'backup') == 'legacy-cipher'
    assert credential_store.unprotect('legacy-cipher', 'backup') == 'synthetic'
    with pytest.raises(ValueError, match='安全移交'):
        credential_store.unprotect('tm-keychain-v1:backup:' + 'a'*32, 'backup')
    assert calls == [('save', 'synthetic'), ('read', 'legacy-cipher')]


def test_unsupported_host_and_publisher_are_not_fallbacks(monkeypatch):
    monkeypatch.setattr('sys.platform', 'linux')
    with pytest.raises(ValueError, match='当前平台'):
        credential_store.protect('synthetic', 'backup')
    with pytest.raises(ValueError, match='用途'):
        credential_store.protect('synthetic', 'publisher')
