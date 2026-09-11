import json
from pathlib import Path
import tempfile
import unittest
from desktop_assistant.signing import initialize, load_key, public_bytes, export_identity, restore_identity


class SigningTests(unittest.TestCase):
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
