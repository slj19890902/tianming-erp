import json
from pathlib import Path
import tempfile
import unittest
from desktop_assistant.signing import initialize, load_key, public_bytes


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
