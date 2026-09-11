import subprocess
import tempfile
import unittest
from pathlib import Path

from desktop_assistant.build import source_snapshot


class BuildSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git('init', '-q')
        self.git('config', 'user.email', 'fixture@example.invalid')
        self.git('config', 'user.name', 'Build fixture')
        self.write('app/version.py', 'APP_VERSION = "v2"\n')
        self.write('alembic/versions/a.py', 'revision = "a"\ndown_revision = None\n')
        self.write('alembic/versions/b.py', 'revision = "b"\ndown_revision = "a"\n')
        self.commit()

    def git(self, *args):
        return subprocess.check_output(['git', *args], cwd=self.root, stderr=subprocess.STDOUT)

    def write(self, name, text):
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8')

    def commit(self):
        self.git('add', '.')
        self.git('commit', '-qm', 'fixture')

    def test_committed_bytes_and_metadata(self):
        commit, sources = source_snapshot(self.root, 'v2', 'b')
        self.assertEqual(commit, self.git('rev-parse', 'HEAD').decode().strip())
        self.assertEqual(sources['app/version.py'], self.git('show', 'HEAD:app/version.py'))

    def test_version_and_head_mismatch(self):
        for version, revision in [('v1', 'b'), ('v2', 'a')]:
            with self.assertRaises(ValueError):
                source_snapshot(self.root, version, revision)

    def test_dirty_tracked_or_untracked_source(self):
        self.write('app/version.py', 'APP_VERSION = "v3"\n')
        with self.assertRaises(ValueError):
            source_snapshot(self.root, 'v3', 'b')
        self.commit()
        self.write('new.py', '# not committed\n')
        with self.assertRaises(ValueError):
            source_snapshot(self.root, 'v3', 'b')

    def test_multiple_heads_or_duplicate_revision(self):
        self.write('alembic/versions/c.py', 'revision = "c"\ndown_revision = "a"\n')
        self.commit()
        with self.assertRaises(ValueError):
            source_snapshot(self.root, 'v2', 'b')
        self.write('alembic/versions/c.py', 'revision = "b"\ndown_revision = "a"\n')
        self.commit()
        with self.assertRaises(ValueError):
            source_snapshot(self.root, 'v2', 'b')
