from pathlib import Path
import shutil
import tempfile
import unittest

from desktop_assistant.native_runtime import safe_copy


class NativeRuntimeCopyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / 'source'
        self.source.mkdir()
        (self.source / 'original').write_text('runtime bytes')

    def test_internal_file_alias_is_materialized(self):
        (self.source / 'alias').symlink_to('original')
        destination = self.root / 'copy'
        safe_copy(self.source, destination, shutil.ignore_patterns('__pycache__'))
        self.assertEqual((destination / 'alias').read_text(), 'runtime bytes')
        self.assertFalse((destination / 'alias').is_symlink())

    def test_external_link_is_rejected_before_copy(self):
        outside = self.root / 'outside'
        outside.write_text('never bundle')
        (self.source / 'escape').symlink_to(outside)
        with self.assertRaisesRegex(ValueError, '外部'):
            safe_copy(self.source, self.root / 'copy', None)
        self.assertFalse((self.root / 'copy').exists())

    def test_directory_loop_is_rejected_before_copy(self):
        (self.source / 'loop').symlink_to(self.source, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, '目录链接'):
            safe_copy(self.source, self.root / 'copy', None)
        self.assertFalse((self.root / 'copy').exists())
