import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from desktop_assistant.manager import Manager
from desktop_assistant.runtime_platform import (
    runtime_python, prepare_runtime, runtime_relative, process_options, link_managed_directory,
)
from desktop_assistant.storage import pack_tree, write_json


class RuntimePlatformTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.key = Ed25519PrivateKey.generate()
        self.public = self.key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        self.manager = Manager(self.root / 'managed', self.public)

    def tearDown(self):
        self.temp.cleanup()

    def package(self, kind='macos-arm64'):
        source = self.root / ('payload-' + kind)
        relative = runtime_relative({'runtime_platform': kind})
        for name in (relative, 'main.py', 'app/main.py', 'desktop_assistant/server_entry.py'):
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'synthetic fixture; never executed')
        package = self.root / (kind + '.zip')
        metadata = {'type': 'tianming.release.v1', 'version': 'synthetic', 'revision': 'synthetic'}
        if kind != 'windows':
            metadata['runtime_platform'] = kind
        manifest = pack_tree(source, package, metadata, self.key)
        return package, manifest

    def test_native_stage_permissions_and_authenticated_selection(self):
        package, manifest = self.package()
        staged = self.manager.stage_release(package)
        release = self.manager.root / 'releases' / staged['id']
        python = release / runtime_relative(manifest)
        self.assertEqual(python.stat().st_mode & 0o7777, 0o755)
        # Mutable cached metadata cannot change which executable is selected.
        cached = json.loads((release / 'manifest.json').read_text())
        cached['runtime_platform'] = 'windows'
        write_json(release / 'manifest.json', cached)
        with patch('sys.platform', 'darwin'), patch('platform.machine', return_value='arm64'):
            self.assertEqual(self.manager._runtime_python(staged['id']), python)
        python.write_bytes(b'tampered')
        with self.assertRaisesRegex(ValueError, '哈希'):
            self.manager._runtime_python(staged['id'])

    def test_legacy_windows_archive_can_be_preserved_but_not_started_on_mac(self):
        package, manifest = self.package('windows')
        self.assertNotIn('runtime_platform', manifest)
        staged = self.manager.stage_release(package)
        write_json(self.manager.root / 'state.json', {'current': staged['id']})
        with patch('sys.platform', 'darwin'), patch('desktop_assistant.manager.database_info') as db:
            with self.assertRaisesRegex(ValueError, 'Windows运行器'):
                self.manager.start()
        db.assert_not_called()

    def test_foreign_arch_missing_binary_and_unsafe_metadata_rejected(self):
        package, manifest = self.package()
        staged = self.manager.stage_release(package)
        release = self.manager.root / 'releases' / staged['id']
        with patch('sys.platform', 'darwin'), patch('platform.machine', return_value='x86_64'):
            with self.assertRaisesRegex(ValueError, 'Apple Silicon'):
                runtime_python(release, manifest, runnable=True)
        with self.assertRaisesRegex(ValueError, '平台'):
            runtime_relative({'runtime_platform': '../escape'})
        (release / runtime_relative(manifest)).unlink()
        with self.assertRaisesRegex(ValueError, '缺失'):
            runtime_python(release, manifest)

    def test_symlinked_interpreter_is_not_accepted(self):
        package, manifest = self.package()
        staged = self.manager.stage_release(package)
        release = self.manager.root / 'releases' / staged['id']
        executable = release / runtime_relative(manifest)
        replacement = self.root / 'elsewhere'
        executable.rename(replacement)
        executable.symlink_to(replacement)
        with self.assertRaisesRegex(ValueError, '链接'):
            prepare_runtime(release, manifest)

    @unittest.skipIf(os.name == 'nt', 'POSIX data links only')
    def test_data_link_is_idempotent_and_never_replaces_foreign_data(self):
        link, target = self.root / 'release/data', self.root / 'shared/data'
        link_managed_directory(link, target)
        (target / 'fixture').write_text('preserve')
        link_managed_directory(link, target)
        self.assertEqual((link / 'fixture').read_text(), 'preserve')
        foreign = self.root / 'release/foreign'
        foreign.mkdir()
        (foreign / 'original').write_text('keep')
        with self.assertRaisesRegex(ValueError, '拒绝覆盖'):
            link_managed_directory(foreign, target)
        self.assertEqual((foreign / 'original').read_text(), 'keep')
        dangling = self.root / 'release/dangling'
        dangling.symlink_to(self.root / 'absent')
        with self.assertRaisesRegex(ValueError, '拒绝覆盖'):
            link_managed_directory(dangling, target)
        self.assertEqual(process_options(), {})


if __name__ == '__main__':
    unittest.main()
