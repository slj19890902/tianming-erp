from contextlib import contextmanager
import ctypes as C
import os
import sys

import pytest

from app.core import mac_keychain as module


@pytest.fixture
def fake_store(monkeypatch):
    values = {}

    class Store:
        def put(self, account, raw):
            assert account not in values
            values[account] = raw

        def get(self, account):
            return values[account]

    monkeypatch.setattr(module, 'Keychain', Store)
    return values


def test_purpose_scoped_opaque_references_and_unicode_round_trip(fake_store):
    value = 'synthetic-only-合成凭据\x00测试'
    references = [module.protect(value, purpose) for purpose in module.PURPOSES]
    assert len(set(references)) == len(module.PURPOSES)
    for reference in references:
        assert value not in reference
        purpose = reference.split(':')[1]
        assert module.unprotect(reference, purpose) == value


@pytest.mark.parametrize('reference', ['AQAAANCMnd8BFdERjHoAwE/Cl+s=', '',
    'tm-keychain-v1:backup:' + 'a'*32, 'tm-keychain-v1:mailbox:' + 'a'*31,
    'tm-keychain-v1:mailbox:' + 'a'*32 + '\n'])
def test_invalid_or_different_purpose_refused_before_keychain(monkeypatch, reference):
    def forbidden():
        raise AssertionError('must not touch keychain')
    monkeypatch.setattr(module, 'Keychain', forbidden)
    with pytest.raises(module.KeychainError, match='安全移交'):
        module.unprotect(reference, 'mailbox')


def test_invalid_size_and_purpose_never_store(fake_store):
    for value, purpose in [('', 'backup'), ('a'*16385, 'backup'), ('secret', 'publisher'),
                           ('synthetic\ud800', 'backup'), (None, 'backup')]:
        with pytest.raises(module.KeychainError):
            module.protect(value, purpose)
    assert fake_store == {}


@pytest.mark.skipif(sys.platform != 'darwin' or os.getenv('ERP_TEST_MAC_KEYCHAIN') != '1',
                    reason='explicit isolated macOS keychain integration')
def test_real_isolated_keychain_round_trip_duplicate_lock_and_cleanup(tmp_path, monkeypatch):
    """Only a newly created task-owned keychain with synthetic values is used."""
    api = module.Keychain()
    p = C.c_void_p
    declarations = [
        ('SecKeychainCreate', [C.c_char_p, C.c_uint32, p, C.c_bool, p, C.POINTER(p)]),
        ('SecKeychainDelete', [p]), ('SecKeychainLock', [p]),
        ('SecKeychainUnlock', [p, C.c_uint32, p, C.c_bool]),
        ('SecKeychainCopySearchList', [C.POINTER(p)]),
        ('SecKeychainSetUserInteractionAllowed', [C.c_bool]),
        ('SecKeychainGetUserInteractionAllowed', [C.POINTER(C.c_bool)]),
    ]
    for name, arguments in declarations:
        function = getattr(api.sec, name)
        function.argtypes, function.restype = arguments, C.c_int32
    api.cf.CFEqual.argtypes, api.cf.CFEqual.restype = [p, p], C.c_bool
    before, after, keychain = p(), p(), p()
    interaction = C.c_bool()
    api.check(api.sec.SecKeychainGetUserInteractionAllowed(C.byref(interaction)))
    api.check(api.sec.SecKeychainCopySearchList(C.byref(before)))
    directory = tmp_path / 'owned-keychain'
    directory.mkdir(mode=0o700)
    path = directory / 'synthetic.keychain'
    password = os.urandom(32).hex().encode('ascii')
    api.check(api.sec.SecKeychainSetUserInteractionAllowed(False))
    try:
        api.check(api.sec.SecKeychainCreate(os.fsencode(path), len(password), password, False, None,
                                          C.byref(keychain)))

        @contextmanager
        def isolated():
            yield keychain.value

        monkeypatch.setattr(api, 'current_keychain', isolated)
        monkeypatch.setattr(module, 'Keychain', lambda: api)
        reference = module.protect('synthetic-合成-only\x00value', 'mailbox')
        assert module.unprotect(reference, 'mailbox') == 'synthetic-合成-only\x00value'
        with pytest.raises(module.KeychainError, match='-25299'):
            api.put(reference, b'do-not-overwrite')
        assert module.unprotect(reference, 'mailbox') == 'synthetic-合成-only\x00value'
        api.check(api.sec.SecKeychainLock(keychain))
        with pytest.raises(module.KeychainError):
            module.unprotect(reference, 'mailbox')
        api.check(api.sec.SecKeychainUnlock(keychain, len(password), password, True))
        assert module.unprotect(reference, 'mailbox') == 'synthetic-合成-only\x00value'
    finally:
        if keychain.value:
            api.check(api.sec.SecKeychainDelete(keychain))
            api.cf.CFRelease(keychain)
        api.check(api.sec.SecKeychainSetUserInteractionAllowed(interaction.value))
        api.check(api.sec.SecKeychainCopySearchList(C.byref(after)))
        try:
            assert api.cf.CFEqual(before, after), 'keychain search list must remain unchanged'
        finally:
            api.cf.CFRelease(before)
            api.cf.CFRelease(after)
