import json
import os
from pathlib import Path
import tempfile
import unittest
from desktop_assistant.signing import initialize, load_key, public_bytes, export_identity, restore_identity
from desktop_assistant import signing
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import hashlib
from types import SimpleNamespace
import pytest


class SigningTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Real Windows ACL and current-user DPAPI')
    def test_encrypted_identity_is_stable_and_can_sign(self):
        with tempfile.TemporaryDirectory() as root:
            directory=Path(root)/'publisher'
            fingerprint=initialize(directory)
            before=(directory/'publisher.json').read_bytes()
            self.assertNotIn(b'PRIVATE KEY',before)
            self.assertEqual(initialize(directory),fingerprint)
            self.assertEqual((directory/'publisher.json').read_bytes(),before)
            key=load_key(directory/'publisher.json')
            key.public_key().verify(key.sign(b'fixture'),b'fixture')
            record=json.loads(before);record['fingerprint']='0'*64
            (directory/'publisher.json').write_text(json.dumps(record))
            with self.assertRaisesRegex(ValueError,'公私钥'):
                initialize(directory)

    def test_nonempty_directory_never_creates_another_identity(self):
        with tempfile.TemporaryDirectory() as root:
            directory=Path(root);(directory/'existing.txt').write_text('keep')
            with self.assertRaisesRegex(ValueError,'非空'):
                initialize(directory)
            self.assertFalse((directory/'publisher.json').exists())

    @unittest.skipUnless(os.name == 'nt', 'Real Windows ACL and current-user DPAPI')
    def test_portable_backup_restores_same_identity_without_overwrite(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root); original=root/'original'; restored=root/'restored'
            fingerprint=initialize(original); backup=root/'recovery.json'
            password='isolated-test-password-only'
            self.assertEqual(export_identity(original/'publisher.json',backup,password),fingerprint)
            self.assertNotIn(b'protected_key',backup.read_bytes())
            self.assertEqual(restore_identity(backup,restored,password),fingerprint)
            self.assertEqual(public_bytes(load_key(original/'publisher.json')),public_bytes(load_key(restored/'publisher.json')))
            before=(restored/'publisher.json').read_bytes()
            with self.assertRaises(ValueError):restore_identity(backup,restored,password)
            self.assertEqual((restored/'publisher.json').read_bytes(),before)
            with self.assertRaises(FileExistsError):export_identity(original/'publisher.json',backup,password)

    @unittest.skipUnless(os.name == 'nt', 'Real Windows ACL and current-user DPAPI')
    def test_wrong_password_and_tampering_do_not_create_identity(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root); original=root/'original'; initialize(original)
            backup=root/'recovery.json'; password='isolated-test-password-only'
            export_identity(original/'publisher.json',backup,password)
            with self.assertRaisesRegex(ValueError,'口令'):
                restore_identity(backup,root/'bad',password+'wrong')
            self.assertFalse((root/'bad').exists())
            record=json.loads(backup.read_bytes());record['fingerprint']='0'*64
            backup.write_text(json.dumps(record))
            with self.assertRaisesRegex(ValueError,'损坏'):
                restore_identity(backup,root/'bad',password)
            self.assertFalse((root/'bad').exists())


@pytest.fixture
def dpapi_fixture(tmp_path, monkeypatch):
    # Synthetic DPAPI adapter only; never opens a real publisher identity.
    key = Ed25519PrivateKey.generate()
    public = public_bytes(key)
    record = {'type': 'tianming.publisher.dpapi.v1', 'public_key': public.decode('ascii'),
              'fingerprint': hashlib.sha256(public).hexdigest(), 'protected_key': 'synthetic-dpapi'}
    identity = tmp_path / 'publisher.json'
    identity.write_text(json.dumps(record))
    calls = []
    def unwrap(value):
        calls.append(value)
        assert value == 'synthetic-dpapi'
        return key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                 serialization.NoEncryption()).hex()
    monkeypatch.setattr(signing, 'sys', SimpleNamespace(platform='win32'))
    monkeypatch.setattr(signing, 'unprotect', unwrap)
    return identity, key, record, calls


def test_restricted_identity_reads_original_dpapi_record_once(dpapi_fixture, monkeypatch):
    path, key, _, calls = dpapi_fixture
    original = Path.open
    reads = []
    def tracked_open(self, *args, **kwargs):
        if self == path:
            reads.append(args[0])
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', tracked_open)
    loaded = signing.load_dpapi_key(path, public_bytes(key))
    loaded.public_key().verify(loaded.sign(b'synthetic'), b'synthetic')
    assert public_bytes(loaded) == public_bytes(key)
    assert reads == ['rb'] and calls == ['synthetic-dpapi']


@pytest.mark.parametrize('fault', ['pem', 'wrong-type', 'wrong-public', 'wrong-fingerprint',
                                   'oversize', 'corrupt', 'missing', 'extra'])
def test_restricted_identity_rejects_before_unprotect(dpapi_fixture, fault):
    path, key, record, calls = dpapi_fixture
    if fault == 'pem':
        raw = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    elif fault == 'oversize':
        raw = b'x' * 8193
    elif fault == 'corrupt':
        raw = b'{invalid'
    else:
        if fault == 'wrong-type':
            record['type'] = 'tianming.publisher.backup.v1'
        elif fault == 'wrong-public':
            record['public_key'] = public_bytes(Ed25519PrivateKey.generate()).decode('ascii')
        elif fault == 'wrong-fingerprint':
            record['fingerprint'] = '0' * 64
        elif fault == 'missing':
            del record['protected_key']
        else:
            record['unexpected'] = 'extra'
        raw = json.dumps(record).encode()
    path.write_bytes(raw)
    with pytest.raises(ValueError, match='原DPAPI'):
        signing.load_dpapi_key(path, public_bytes(key))
    assert not calls and path.read_bytes() == raw


def test_restricted_identity_rejects_wrong_unwrapped_key_and_hides_error(dpapi_fixture, monkeypatch):
    path, key, _, _ = dpapi_fixture
    monkeypatch.setattr(signing, 'unprotect', lambda _: 'ab' * 32)
    with pytest.raises(ValueError, match='原DPAPI'):
        signing.load_dpapi_key(path, public_bytes(key))
    def fail(_):
        raise ValueError('synthetic-secret-that-must-not-leak')
    monkeypatch.setattr(signing, 'unprotect', fail)
    with pytest.raises(ValueError) as error:
        signing.load_dpapi_key(path, public_bytes(key))
    assert 'synthetic-secret' not in str(error.value)


def test_restricted_identity_refuses_nonwindows_without_reading(tmp_path, monkeypatch):
    monkeypatch.setattr(signing, 'sys', SimpleNamespace(platform='darwin'))
    with pytest.raises(ValueError, match='Windows原用户'):
        signing.load_dpapi_key(tmp_path / 'does-not-exist', b'not read')
    assert not list(tmp_path.iterdir())


def test_general_build_loader_keeps_explicit_synthetic_pem_support(tmp_path):
    key = Ed25519PrivateKey.generate()
    path = tmp_path / 'test-only.pem'
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption()))
    assert public_bytes(load_key(path)) == public_bytes(key)


@pytest.mark.skipif(os.name != 'nt', reason='real Windows original-user DPAPI; synthetic identity only')
def test_restricted_windows_dpapi_identity_roundtrip(tmp_path):
    directory = tmp_path / 'synthetic-publisher'
    initialize(directory)
    path = directory / 'publisher.json'
    before = path.read_bytes()
    public = json.loads(before)['public_key'].encode('ascii')
    key = signing.load_dpapi_key(path, public)
    key.public_key().verify(key.sign(b'synthetic-cutover'), b'synthetic-cutover')
    assert public_bytes(key) == public and path.read_bytes() == before
