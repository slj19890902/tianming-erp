import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
import zipfile

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from desktop_assistant.release_request import prepare_request, sign_request, finalize_request, inspect_package
from desktop_assistant.signing import public_bytes
from desktop_assistant.storage import pack_tree, extract_verified


class ReleaseRequestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.key = Ed25519PrivateKey.generate()  # Test identity only; never factory identity.
        self.public = public_bytes(self.key)
        self.identity = self.root / 'synthetic-key.pem'
        self.identity.write_bytes(self.key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        tree = self.root / 'payload'
        (tree / 'runtime/bin').mkdir(parents=True)
        (tree / 'runtime/bin/python3.12').write_bytes(b'not an executable; synthetic archive fixture')
        self.package = self.root / 'release.unsigned.zip'
        self.git_sha = 'a' * 40
        pack_tree(tree, self.package, {'type': 'tianming.release.v1', 'version': 'synthetic',
            'revision': 'synthetic-r1', 'git_sha': self.git_sha, 'runtime_platform': 'macos-arm64',
            'executable_files': ['runtime/bin/python3.12']})
        self.request = self.root / 'request.json'
        self.record = prepare_request(self.package, self.public, self.request)
        self.signature = self.root / 'manifest.sig'

    def sign(self, **overrides):
        args = dict(expected_git_sha=self.git_sha, expected_manifest_sha256=self.record['manifest_sha256'])
        args.update(overrides)
        return sign_request(self.package, self.request, self.identity, self.signature, **args)

    def test_unsigned_refused_then_original_identity_signature_installs(self):
        with self.assertRaises(KeyError):
            extract_verified(self.package, self.root / 'not-installed', self.public)
        self.assertFalse((self.root / 'not-installed').exists())
        self.sign()
        output = self.root / 'release.zip'
        result = finalize_request(self.package, self.signature, self.public, output)
        self.assertTrue(result['signed'])
        manifest = extract_verified(output, self.root / 'installed', self.public)
        self.assertEqual(manifest['git_sha'], self.git_sha)
        if os.name != 'nt':
            self.assertEqual((self.root / 'installed/runtime/bin/python3.12').stat().st_mode & 0o7777, 0o755)
        with self.assertRaises(ValueError):
            finalize_request(self.package, self.signature, self.public, output)

    def test_reviewed_commit_and_manifest_must_match(self):
        for args in ({'expected_git_sha': 'b' * 40}, {'expected_manifest_sha256': 'b' * 64}):
            with self.assertRaisesRegex(ValueError, '已审阅'):
                self.sign(**args)
        self.assertFalse(self.signature.exists())

    def test_other_publisher_is_not_accepted(self):
        other = Ed25519PrivateKey.generate()
        self.identity.write_bytes(other.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        with self.assertRaisesRegex(ValueError, '既有发布身份'):
            self.sign()
        self.assertFalse(self.signature.exists())

    def test_corrupt_member_refused_before_signing(self):
        replacement = self.root / 'changed.zip'
        with zipfile.ZipFile(self.package) as source, zipfile.ZipFile(replacement, 'x') as output:
            for name in source.namelist():
                output.writestr(name, b'changed' if name.startswith('runtime/') else source.read(name))
        self.package = replacement
        with self.assertRaisesRegex(ValueError, '哈希'):
            self.sign()
        self.assertFalse(self.signature.exists())

    def test_bad_signature_never_produces_release(self):
        self.signature.write_bytes(b'0' * 64)
        with self.assertRaises(InvalidSignature):
            finalize_request(self.package, self.signature, self.public, self.root / 'release.zip')
        self.assertFalse((self.root / 'release.zip').exists())
        self.assertFalse((self.root / 'release.zip.pending').exists())

    def test_unsigned_permission_metadata_cannot_grant_execute(self):
        with self.assertRaisesRegex(ValueError, '执行权限'):
            extract_verified(self.package, self.root / 'untrusted')
        self.assertFalse((self.root / 'untrusted').exists())

    def test_zip_mode_bits_do_not_grant_extra_privilege(self):
        raw, _ = inspect_package(self.package)
        changed = self.root / 'mode-changed.zip'
        with zipfile.ZipFile(self.package) as source, zipfile.ZipFile(changed, 'x') as output:
            for info in source.infolist():
                info.external_attr = (0o104777 << 16)
                output.writestr(info, source.read(info.filename))
        # ZIP metadata is outside signature coverage: the installer uses only
        # ordinary permissions from the signed executable list, not these bits.
        self.signature.write_bytes(self.key.sign(raw))
        final = self.root / 'mode-signed.zip'
        finalize_request(changed, self.signature, self.public, final)
        extract_verified(final, self.root / 'mode-installed', self.public)
        if os.name != 'nt':
            self.assertEqual((self.root / 'mode-installed/runtime/bin/python3.12').stat().st_mode & 0o7777, 0o755)


if __name__ == '__main__':
    unittest.main()
