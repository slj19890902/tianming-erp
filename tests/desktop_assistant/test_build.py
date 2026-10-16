import subprocess
import tempfile
import unittest
from pathlib import Path

from desktop_assistant.build import remove_identical_signing_duplicates, remove_transient_build_trees, source_snapshot


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

    def test_exact_commit_excludes_dirty_worktree_and_rejects_mutable_refs(self):
        pinned = self.git('rev-parse', 'HEAD').decode().strip()
        self.write('app/version.py', 'APP_VERSION = "dirty-other-task"\n')
        self.write('another-task.py', 'raise RuntimeError("not published")\n')
        commit, sources = source_snapshot(self.root, 'v2', 'b', commit=pinned)
        self.assertEqual(commit, pinned)
        self.assertEqual(sources['app/version.py'], b'APP_VERSION = "v2"\n')
        self.assertNotIn('another-task.py', sources)
        for value in ('HEAD', pinned[:8], '--all'):
            with self.assertRaises(ValueError):
                source_snapshot(self.root, 'v2', 'b', commit=value)
        with self.assertRaises(ValueError):
            source_snapshot(self.root, 'dirty-other-task', 'b', commit=pinned)

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

    def test_transient_build_trees_are_removed_but_artifacts_remain(self):
        output = self.root / 'output'
        (output / 'payload').mkdir(parents=True)
        (output / 'pyi-work').mkdir()
        (output / 'payload/source.py').write_text('x = 1', encoding='utf-8')
        (output / 'release.zip').write_bytes(b'release')
        remove_transient_build_trees(output)
        self.assertFalse((output / 'payload').exists())
        self.assertFalse((output / 'pyi-work').exists())
        self.assertEqual((output / 'release.zip').read_bytes(), b'release')

    def test_transient_build_cleanup_rejects_junction(self):
        from unittest.mock import patch
        output = self.root / 'output'
        (output / 'payload').mkdir(parents=True)
        with patch.object(Path, 'is_junction', return_value=True):
            with self.assertRaises(ValueError):
                remove_transient_build_trees(output)
        self.assertTrue((output / 'payload').is_dir())

    def test_signing_duplicate_cleanup_keeps_final_and_unique_candidates(self):
        from unittest.mock import patch
        output = self.root / 'builds' / 'mac-arm64-new'
        old = output.parent / 'mac-arm64-old'
        old.mkdir(parents=True)
        output.mkdir()
        (old / 'release.signed.zip').write_bytes(b'finished')
        (old / 'release.signed.zip.pending').write_bytes(b'finished')
        (output / 'release.signed.zip').write_bytes(b'current1')
        (output / 'release.signed.zip.pending').write_bytes(b'current2')
        unsigned = old / 'release.unsigned.zip'
        unsigned.write_bytes(b'unsigned')
        with patch('desktop_assistant.cleanup.assert_idle'):
            report = remove_identical_signing_duplicates(output)
            replay = remove_identical_signing_duplicates(output)
        self.assertEqual(len(report['removed']), 1)
        self.assertEqual(replay['removed'], [])
        self.assertEqual((old / 'release.signed.zip').read_bytes(), b'finished')
        self.assertEqual(unsigned.read_bytes(), b'unsigned')
        self.assertTrue((output / 'release.signed.zip.pending').exists())

    def test_signing_duplicate_cleanup_rejects_links_and_active_file(self):
        from unittest.mock import patch
        output = self.root / 'output'
        output.mkdir()
        final = output / 'release.signed.zip'
        final.write_bytes(b'finished')
        pending = output / 'release.signed.zip.pending'
        pending.symlink_to(final)
        report = remove_identical_signing_duplicates(output)
        self.assertEqual(report['removed'], [])
        self.assertTrue(pending.is_symlink())
        pending.unlink()
        pending.write_bytes(b'finished')
        with patch('desktop_assistant.cleanup.assert_idle', side_effect=ValueError('active')):
            report = remove_identical_signing_duplicates(output)
        self.assertEqual(report['removed'], [])
        self.assertTrue(pending.exists())

    def test_optional_signing_cleanup_dependency_cannot_fail_completed_build(self):
        from unittest.mock import patch
        import json
        output = self.root / 'output'
        output.mkdir()
        artifact = output / 'release.zip'
        artifact.write_bytes(b'finished')
        with patch('desktop_assistant.build.remove_identical_signing_duplicates',
                   side_effect=ImportError('optional process inspection unavailable')):
            remove_transient_build_trees(output)
        report = json.loads((output / 'build-retention.json').read_text())
        self.assertEqual(report['identical_signing_duplicates']['removed'], [])
        self.assertTrue(report['identical_signing_duplicates']['retained'])
        self.assertEqual(artifact.read_bytes(), b'finished')
