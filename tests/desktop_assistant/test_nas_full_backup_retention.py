"""Small synthetic files only; runnable with unittest without ERP/DB imports."""
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import tempfile
import unittest

from desktop_assistant import nas_full_backup_retention as retention
from scripts.admin.nas_retention_evidence_preview import preview


def sha(value): return hashlib.sha256(value).hexdigest()


class FakeAuthority:
    """TEST ONLY: synthetic verification and exclusive writer lease."""
    def __init__(self, snapshot, callback=None):
        self.snapshot = snapshot
        self.callback = callback
        self.calls = 0
        self.frozen = False

    @contextmanager
    def freeze(self):
        self.frozen = True
        try: yield self.snapshot
        finally: self.frozen = False

    def recheck(self, snapshot):
        assert self.frozen and snapshot == self.snapshot
        self.calls += 1
        if self.callback: self.callback(self.calls)


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='nas-retention-unit-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / 'BoxERP'
        self.root.mkdir()
        self.rows = []
        self.add_file('backups/20991231-old.tmbackup', b'older', 'retired_complete_backup')
        self.add_file('backups/previous.tmbackup', b'previous', 'protected')
        self.add_file('mac-server-backups/new.tmbackup', b'new', 'protected')
        self.add_file('backups/work/job.pending', b'pending', 'stale_pending')
        self.add_file('backups/dependencies/credentials.tmencrypted', b'credentials', 'protected')
        self.add_file('desktop-assistant/releases/current.zip', b'program', 'protected')
        self.add_file('backups/security.key', b'key', 'historical_package')
        self.add_file('product_drawings/formal.pdf', b'drawing', 'historical_package')
        deps = ('backups/dependencies/credentials.tmencrypted',)
        backups = tuple(retention.VerifiedBackup(
            row.path, row.sha256, f'2026-10-{10+i:02d}T12:00:00+08:00',
            sha(('remote-' + row.path).encode()), sha(('restore-' + row.path).encode()),
            tuple(sorted(retention.COVERAGE)), deps) for i, row in enumerate(self.rows[:3]))
        references = {role: () for role in retention.ROLES}
        references.update(current=('desktop-assistant/releases/current.zip',),
                          fixed_backup_contract=('mac-server-backups',),
                          keys=('backups/security.key',), drawings=('product_drawings',))
        self.snapshot = retention.VerifiedSnapshot(str(self.root), sha(b'contract'), sha(b'authority'),
            True, tuple(self.rows), backups, tuple(sorted(references.items())), ())

    def add_file(self, path, content, category):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        row = retention.FileRecord(path, retention.Fingerprint.of(target.lstat()), sha(content),
                                   category, sha(b'authenticated-owner-classification'))
        self.rows.append(row)
        return row

    def test_global_pair_uses_business_time_not_name_or_directory(self):
        result = retention.plan(self.snapshot)
        self.assertFalse(result.blockers)
        self.assertEqual(result.keep_backups, ('mac-server-backups/new.tmbackup', 'backups/previous.tmbackup'))
        self.assertEqual({r.path for r in result.delete}, {'backups/20991231-old.tmbackup', 'backups/work/job.pending'})
        self.assertEqual(result.delete_bytes, len(b'olderpending'))

    def test_keys_drawings_and_backup_dependencies_survive(self):
        result = retention.plan(self.snapshot)
        kept = dict(result.retained)
        for path in ('backups/security.key', 'product_drawings/formal.pdf',
                     'backups/dependencies/credentials.tmencrypted', 'desktop-assistant/releases/current.zip'):
            self.assertIn(path, kept)

    def test_current_reference_case_alias_protects_disposable_package(self):
        path = 'desktop-assistant/releases/current.zip'
        files = tuple(replace(row, category='historical_package') if row.path == path else row
                      for row in self.snapshot.files)
        references = tuple((role, ('DESKTOP-ASSISTANT/releases/CURRENT.zip',) if role == 'current' else paths)
                           for role, paths in self.snapshot.references)
        result = retention.plan(replace(self.snapshot, files=files, references=references))
        self.assertFalse(result.blockers)
        self.assertNotIn(path, {row.path for row in result.delete})
        self.assertEqual(dict(result.retained)[path], 'protected recovery/runtime/business reference')

    def test_previous_reference_case_alias_protects_disposable_package(self):
        row = self.add_file('desktop-assistant/releases/previous.zip', b'previous-program', 'historical_package')
        references = tuple((role, ('desktop-assistant/RELEASES/PREVIOUS.zip',) if role == 'previous' else paths)
                           for role, paths in self.snapshot.references)
        result = retention.plan(replace(self.snapshot, files=tuple(self.rows), references=references))
        self.assertFalse(result.blockers)
        self.assertNotIn(row.path, {entry.path for entry in result.delete})
        self.assertEqual(dict(result.retained)[row.path], 'protected recovery/runtime/business reference')

    def test_knowledge_directory_case_alias_protects_descendants(self):
        row = self.add_file('archives/Knowledge/nested/notes.md', b'knowledge', 'historical_rehearsal')
        references = tuple((role, ('ARCHIVES/knowledge',) if role == 'knowledge' else paths)
                           for role, paths in self.snapshot.references)
        result = retention.plan(replace(self.snapshot, files=tuple(self.rows), references=references))
        self.assertFalse(result.blockers)
        self.assertNotIn(row.path, {entry.path for entry in result.delete})
        self.assertEqual(dict(result.retained)[row.path], 'protected recovery/runtime/business reference')

    def test_knowledge_combining_character_alias_protects_original_path(self):
        row = self.add_file('archives/Caf\u00e9/nested/notes.md', b'knowledge', 'historical_rehearsal')
        references = tuple((role, ('ARCHIVES/cafe\u0301',) if role == 'knowledge' else paths)
                           for role, paths in self.snapshot.references)
        result = retention.plan(replace(self.snapshot, files=tuple(self.rows), references=references))
        self.assertFalse(result.blockers)
        self.assertNotIn(row.path, {entry.path for entry in result.delete})
        self.assertEqual(dict(result.retained)[row.path], 'protected recovery/runtime/business reference')

    def test_inventory_unicode_alias_collision_blocks(self):
        row = self.add_file('archives/Caf\u00e9.zip', b'archive', 'historical_package')
        alias = replace(row, path='ARCHIVES/cafe\u0301.zip')
        result = retention.plan(replace(self.snapshot, files=(*self.rows, alias)))
        self.assertTrue(any('duplicate inventory path' in blocker for blocker in result.blockers))
        self.assertFalse(result.delete)

    def test_only_one_authenticated_backup_blocks_all(self):
        result = retention.plan(replace(self.snapshot, backups=self.snapshot.backups[-1:]))
        self.assertTrue(result.blockers)
        self.assertFalse(result.delete)

    def test_same_bytes_in_two_locations_are_one_version(self):
        duplicate = self.add_file('backups/duplicate.tmbackup', b'new', 'protected')
        verified = replace(self.snapshot.backups[-1], path=duplicate.path)
        result = retention.plan(replace(self.snapshot, files=tuple(self.rows),
                                        backups=(self.snapshot.backups[-1], verified)))
        self.assertTrue(result.blockers)
        self.assertFalse(result.delete)

    def test_remote_readback_without_full_recovery_blocks(self):
        backup = replace(self.snapshot.backups[-1], coverage=('database', 'program'))
        result = retention.plan(replace(self.snapshot, backups=(*self.snapshot.backups[:2], backup)))
        self.assertTrue(result.blockers)
        self.assertFalse(result.delete)

    def test_same_remote_and_restore_report_is_not_independent(self):
        backup = replace(self.snapshot.backups[-1], recovery_report_sha256=self.snapshot.backups[-1].remote_report_sha256)
        self.assertTrue(retention.plan(replace(self.snapshot, backups=(self.snapshot.backups[1], backup))).blockers)

    def test_missing_recovery_dependency_blocks(self):
        backup = replace(self.snapshot.backups[-1], dependencies=('backups/missing.json',))
        self.assertTrue(retention.plan(replace(self.snapshot, backups=(self.snapshot.backups[1], backup))).blockers)

    def test_unknown_or_incomplete_inventory_blocks(self):
        unknown = replace(self.snapshot.files[0], category='unknown')
        for snapshot in (replace(self.snapshot, inventory_complete=False),
                         replace(self.snapshot, files=(unknown, *self.snapshot.files[1:]))):
            result = retention.plan(snapshot)
            self.assertTrue(result.blockers)
            self.assertFalse(result.delete)

    def test_active_upload_blocks_even_if_stale_filename(self):
        result = retention.plan(replace(self.snapshot, active_uploads=('backups/work/job.pending',)))
        self.assertTrue(result.blockers)
        self.assertFalse(result.delete)

    def test_missing_runtime_role_or_current_blocks(self):
        for references in (self.snapshot.references[:-1],
                           tuple((role, () if role == 'current' else paths) for role, paths in self.snapshot.references)):
            result = retention.plan(replace(self.snapshot, references=references))
            self.assertTrue(result.blockers)
            self.assertFalse(result.delete)

    def test_extra_complete_version_with_active_reference_blocks_rotation(self):
        references = tuple((role, ('BACKUPS/20991231-OLD.tmbackup',) if role == 'active_backups' else paths)
                           for role, paths in self.snapshot.references)
        result = retention.plan(replace(self.snapshot, references=references))
        self.assertTrue(any('extra complete' in reason for reason in result.blockers))
        self.assertFalse(result.delete)

    def test_unknown_classification_receipt_blocks(self):
        row = replace(self.snapshot.files[0], classification_report_sha256='')
        self.assertTrue(retention.plan(replace(self.snapshot, files=(row, *self.snapshot.files[1:]))).blockers)

    def test_path_escape_and_alias_collision_block(self):
        for bad in ('../outside', '/tmp/outside', 'backups/../outside', 'backups//outside', 'backups/x:stream'):
            row = replace(self.snapshot.files[0], path=bad)
            self.assertTrue(retention.plan(replace(self.snapshot, files=(row, *self.snapshot.files[1:]))).blockers)
        row = replace(self.snapshot.files[0], path=self.snapshot.files[0].path.upper())
        self.assertTrue(retention.plan(replace(self.snapshot, files=(*self.snapshot.files, row))).blockers)

    def test_symlink_and_hardlink_fingerprints_block(self):
        for identity in (replace(self.snapshot.files[0].identity, mode=0o120777),
                         replace(self.snapshot.files[0].identity, nlink=2)):
            row = replace(self.snapshot.files[0], identity=identity)
            self.assertTrue(retention.plan(replace(self.snapshot, files=(row, *self.snapshot.files[1:]))).blockers)

    def test_apply_deletes_exact_files_keeps_directories_and_two_versions(self):
        authority = FakeAuthority(self.snapshot)
        result = retention.apply_plan(retention.plan(self.snapshot), authority)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['removed_bytes'], len(b'olderpending'))
        self.assertTrue((self.root / 'backups/work').is_dir())
        self.assertTrue((self.root / 'backups/previous.tmbackup').is_file())
        self.assertTrue((self.root / 'mac-server-backups/new.tmbackup').is_file())
        self.assertTrue((self.root / 'backups/security.key').is_file())

    def test_reviewed_plan_tamper_or_fresh_evidence_change_refuses(self):
        original = retention.plan(self.snapshot)
        for reviewed, snapshot in ((replace(original, delete=original.delete[:1]), self.snapshot),
                                   (original, replace(self.snapshot, authority_sha256=sha(b'changed')))):
            with self.assertRaises(ValueError): retention.apply_plan(reviewed, FakeAuthority(snapshot))
        self.assertTrue((self.root / self.snapshot.files[0].path).is_file())

    def test_preflight_checks_all_files_before_any_deletion(self):
        (self.root / 'backups/work/job.pending').write_bytes(b'changed')
        with self.assertRaises(ValueError):
            retention.apply_plan(retention.plan(self.snapshot), FakeAuthority(self.snapshot))
        self.assertTrue((self.root / self.snapshot.files[0].path).is_file())

    def test_same_size_content_mutation_with_mtime_restored_still_refuses(self):
        path = self.root / self.snapshot.files[0].path
        before = path.stat()
        path.write_bytes(b'other')
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        with self.assertRaises(ValueError):
            retention.apply_plan(retention.plan(self.snapshot), FakeAuthority(self.snapshot))
        self.assertTrue(path.is_file())

    def test_symlink_parent_is_not_followed(self):
        work = self.root / 'backups/work'
        other = self.root / 'outside'
        work.rename(other)
        work.symlink_to(other, target_is_directory=True)
        with self.assertRaises(OSError):
            retention.apply_plan(retention.plan(self.snapshot), FakeAuthority(self.snapshot))
        self.assertTrue((other / 'job.pending').is_file())
        self.assertTrue((self.root / self.snapshot.files[0].path).is_file())

    def test_reference_or_upload_change_after_preflight_stops_without_delete(self):
        def changed(call):
            if call == 2: raise ValueError('new active backup reference')
        result = retention.apply_plan(retention.plan(self.snapshot), FakeAuthority(self.snapshot, changed))
        self.assertEqual(result['status'], 'stopped')
        self.assertEqual(result['removed_bytes'], 0)

    def test_file_replacement_after_preflight_stops(self):
        def changed(call):
            if call == 2: (self.root / self.snapshot.files[0].path).write_bytes(b'new content')
        result = retention.apply_plan(retention.plan(self.snapshot), FakeAuthority(self.snapshot, changed))
        self.assertEqual(result['status'], 'stopped')
        self.assertEqual(result['removed'], [])

    def test_partial_failure_reports_only_actual_bytes_and_stops(self):
        def changed(call):
            if call == 4: raise ValueError('lease lost')
        result = retention.apply_plan(retention.plan(self.snapshot), FakeAuthority(self.snapshot, changed))
        self.assertEqual(result['status'], 'stopped')
        self.assertEqual(result['removed'], ['backups/20991231-old.tmbackup'])
        self.assertEqual(result['removed_bytes'], len(b'older'))
        self.assertTrue((self.root / 'backups/work/job.pending').is_file())

    def test_unverified_third_candidate_is_retained(self):
        row = self.add_file('backups/third.tmbackup', b'third', 'awaiting_verification')
        result = retention.plan(replace(self.snapshot, files=tuple(self.rows)))
        self.assertFalse(result.blockers)
        self.assertIn((row.path, 'awaiting_verification'), result.retained)

    def test_evidence_preview_is_metadata_only_and_never_deletes(self):
        source = self.root / 'evidence'
        source.mkdir()
        import json
        values = {'root': str(self.root), 'at': '2026-10-14',
                  'summary': {'backups': {'bytes': 20}},
                  'large': [{'path': str(self.root / 'backups/fake.tmbackup'), 'bytes': 20, 'mtime': '2099-01-01'}]}
        (source / 'nas-sizes.json').write_text(json.dumps(values))
        (source / 'nas-top-level.json').write_text('{}')
        (source / 'NAS容量与两份备份清理方案.txt').write_text('evidence')
        result = preview(source)
        self.assertEqual(result['authorized_delete_bytes'], 0)
        self.assertEqual(result['delete'], [])
        self.assertEqual(result['verified_keep_versions'], [])
        self.assertEqual(result['entries'][0]['action'], 'retain_blocked')


if __name__ == '__main__': unittest.main()
