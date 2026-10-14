"""Incremental index/compact evidence tests; all authority/bytes are synthetic."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest

from desktop_assistant import nas_backup_rotation as rotation
from desktop_assistant.nas_full_backup_retention import digest


def pointer(number):
    return {'receipt': f'202610{number:02d}-010203-abcdef01.smb-stream.json', 'receipt_sha256': str(number % 10) * 64}


def report_for(receipt, receipt_sha):
    summary = {'source_sha256': 'a' * 64, 'restored_sha256': 'a' * 64, 'source_count': 1, 'restored_count': 1}
    return {'type': rotation.RECOVERY, 'backup_sha256': receipt['sha256'], 'receipt_sha256': receipt_sha,
            'contract_sha256': receipt['contract_sha256'], 'raw_sha256': receipt['raw_sha256'],
            'manifest_sha256': 'b' * 64, 'source_time': '2026-10-10T01:02:03+08:00',
            'inventory': dict(summary, source_count=4, restored_count=4), 'shared': dict(summary), 'maps': dict(summary),
            'database': {'source': receipt['database'], 'restored': receipt['database']},
            'programs': {'release': 'c' * 64, 'schema_authority': None,
                         'packages': {'release.zip': {'package_sha256': 'c' * 64, 'manifest_sha256': 'd' * 64}}},
            'credentials': {'source_sha256': 'e' * 64, 'status': 'restored', 'restored_metadata_sha256': 'f' * 64},
            'isolated_root': '/synthetic/isolated-task/restored', 'completed_at': '2026-10-13T12:00:00+08:00'}


class Manager:
    def __init__(self): self.locked = False
    @contextmanager
    def lock(self):
        assert not self.locked
        self.locked = True
        try: yield
        finally: self.locked = False


class Adapter:
    """TEST ONLY metadata authority/CAS; production uses MacStreamReceiptAdapter."""
    def __init__(self, versions, *, kept=None, retired=()):
        self.manager = Manager()
        self.versions = {v['pointer']['receipt']: v for v in versions}
        self.index = {'type': rotation.TYPE, 'contract_sha256': versions[0]['contract_sha256'], 'generation': 0,
                      'kept': list(kept or []), 'staged': None, 'retired': list(retired), 'bootstrap': None}
        latest = versions[-1]
        self.running_state = {'current': 'f' * 64, 'previous': 'e' * 64, 'manual_stop': False,
                              'last_backup': latest['receipt']['local_copy']['path'],
                              'last_backup_stream': dict(latest['pointer'], path=latest['receipt']['local_copy']['path'])}
        self.active = {'active_paths': [], 'active_receipts': [], 'active_sha256': [],
                       'rehearsal_paths': [], 'rehearsal_sha256': [], 'uploads': []}
        self.loads = []
        self.fail = None
        self.conflict = False

    def read_index(self): return digest(self.index), deepcopy(self.index)
    def state(self): return deepcopy(self.running_state)
    def active_references(self, state): return deepcopy(self.active)
    def load_receipt(self, p): return self.versions[p['receipt']]['receipt']
    def load_verified(self, p):
        self.loads.append(p['receipt'])
        if p == self.fail: raise ValueError('real recovery evidence missing')
        return deepcopy(self.versions[p['receipt']])
    def cas_index(self, expected, updated):
        assert self.manager.locked
        if self.conflict or expected != digest(self.index): raise ValueError('CAS conflict')
        rotation.validate_index(updated, self.index['contract_sha256'])
        self.index = deepcopy(updated)
        return digest(self.index)


def versions():
    result = []
    for number in (10, 11, 12):
        p = pointer(number)
        filename = p['receipt'].replace('.smb-stream.json', '.tmbackup')
        receipt = {'sha256': str(number % 10) * 64, 'raw_sha256': 'a' * 64, 'contract_sha256': 'b' * 64,
                   'filename': filename, 'size': 100, 'database': {'revision': 'test', 'counts': {'users': 1}},
                   'local_copy': {'path': '/Library/TianmingERP/instance/backups/' + filename}}
        result.append({'pointer': p, 'receipt': receipt, 'source_time': f'2026-10-{number:02d}T01:02:03+08:00',
                       'recovery_sha256': 'c' * 64, 'contract_sha256': 'b' * 64})
    return result


class RotationTests(unittest.TestCase):
    def setUp(self): self.versions = versions()

    def test_bootstrap_requires_two_real_verified_reports(self):
        adapter = Adapter(self.versions)
        with self.assertRaises(ValueError): rotation.advance(adapter, bootstrap=(self.versions[0]['pointer'],))
        self.assertFalse(adapter.index['kept'])
        result = rotation.advance(adapter, bootstrap=tuple(v['pointer'] for v in self.versions[:2]))
        self.assertEqual(result['status'], 'indexed')
        self.assertEqual(adapter.index['kept'], [self.versions[1]['pointer'], self.versions[0]['pointer']])

    def test_rotation_stages_third_and_queues_unique_expired_package(self):
        kept = [self.versions[1]['pointer'], self.versions[0]['pointer']]
        adapter = Adapter(self.versions, kept=kept)
        rotation.stage_candidate(adapter, self.versions[2]['pointer'])
        self.assertEqual(adapter.index['kept'], kept)
        self.assertEqual(adapter.index['staged'], self.versions[2]['pointer'])
        result = rotation.advance(adapter, self.versions[2]['pointer'])
        self.assertEqual(result['retired'], [self.versions[0]['pointer']])
        self.assertEqual(adapter.index['kept'], [self.versions[2]['pointer'], self.versions[1]['pointer']])
        self.assertIsNone(adapter.index['staged'])
        self.assertLessEqual(len(adapter.loads), 3)

    def test_recovery_failure_keeps_original_pair_and_staged_evidence(self):
        adapter = Adapter(self.versions, kept=[self.versions[1]['pointer'], self.versions[0]['pointer']])
        rotation.stage_candidate(adapter, self.versions[2]['pointer'])
        before = deepcopy(adapter.index)
        adapter.fail = self.versions[2]['pointer']
        with self.assertRaises(ValueError): rotation.advance(adapter, self.versions[2]['pointer'])
        self.assertEqual(adapter.index, before)

    def test_cas_conflict_does_not_replace_pair(self):
        adapter = Adapter(self.versions, kept=[self.versions[1]['pointer'], self.versions[0]['pointer']])
        before = deepcopy(adapter.index)
        adapter.conflict = True
        with self.assertRaises(ValueError): rotation.advance(adapter, self.versions[2]['pointer'])
        self.assertEqual(adapter.index, before)

    def test_retry_preserves_retired_queue_and_is_idempotent(self):
        adapter = Adapter(self.versions, kept=[self.versions[1]['pointer'], self.versions[0]['pointer']])
        rotation.advance(adapter, self.versions[2]['pointer'])
        before = deepcopy(adapter.index)
        result = rotation.advance(adapter, self.versions[2]['pointer'])
        self.assertEqual(result['status'], 'unchanged')
        self.assertEqual(adapter.index, before)
        with self.assertRaises(ValueError): rotation.stage_candidate(adapter, self.versions[0]['pointer'])

    def test_runtime_source_or_rehearsal_reference_blocks_retirement(self):
        for field in ('restored_backup_sha256', 'migration_backup_sha256', 'rehearsal'):
            adapter = Adapter(self.versions, kept=[self.versions[1]['pointer'], self.versions[0]['pointer']])
            if field == 'rehearsal': adapter.active['rehearsal_sha256'] = [self.versions[0]['receipt']['sha256']]
            else: adapter.running_state[field] = self.versions[0]['receipt']['sha256']
            with self.assertRaises(ValueError): rotation.advance(adapter, self.versions[2]['pointer'])
            self.assertFalse(adapter.index['retired'])

    def test_last_backup_and_stream_name_hash_aliases_protect_expired_version(self):
        version = self.versions[0]
        for state in ({'last_backup': '/cache/' + version['receipt']['filename'].upper()},
                      {'last_backup_stream': {'receipt': version['pointer']['receipt'].upper()}},
                      {'last_backup_stream': {'receipt_sha256': version['pointer']['receipt_sha256']}},
                      {'last_backup_stream': {'path': '/Library/other/' + version['receipt']['filename']}}):
            self.assertTrue(rotation._protected_version(version, state))

    def test_invalid_receipt_pointer_is_rejected_before_io(self):
        adapter = rotation.MacStreamReceiptAdapter(SimpleNamespace(root=Path('/Library/TianmingERP/instance')))
        with self.assertRaises(ValueError): adapter.load_verified({'receipt': '../state.json', 'receipt_sha256': 'a' * 64})

    def test_compact_report_rejects_missing_or_forged_coverage(self):
        receipt = self.versions[0]['receipt']
        valid = report_for(receipt, self.versions[0]['pointer']['receipt_sha256'])
        rotation.validate_recovery(valid, receipt, valid['receipt_sha256'])
        for field in ('inventory', 'shared', 'maps'):
            bad = deepcopy(valid)
            bad[field]['restored_sha256'] = '0' * 64
            with self.assertRaises(ValueError): rotation.validate_recovery(bad, receipt, valid['receipt_sha256'])
        bad = deepcopy(valid)
        bad['credentials']['status'] = 'not-included'
        with self.assertRaises(ValueError): rotation.validate_recovery(bad, receipt, valid['receipt_sha256'])

    def test_active_reference_report_must_be_fresh_bound_and_complete(self):
        state = Adapter(self.versions).state()
        base = {'type': 'tianming.nas-active-references.v1', 'state_sha256': digest(state),
                'contract_sha256': 'b' * 64, 'observed_at': datetime.now(timezone.utc).isoformat(),
                'active_paths': [], 'active_receipts': [], 'active_sha256': [],
                'rehearsal_paths': [], 'rehearsal_sha256': [], 'uploads': []}
        rotation.validate_references(base, state, 'b' * 64)
        for changed in ({'state_sha256': '0' * 64}, {'observed_at': '2026-10-01T00:00:00+00:00'}, {'uploads': ['active']}):
            with self.assertRaises(ValueError): rotation.validate_references(dict(base, **changed), state, 'b' * 64)

    def test_pre_backup_gate_never_generates_fourth_after_staging_or_retirement_failure(self):
        adapter = Adapter(self.versions, kept=[self.versions[2]['pointer'], self.versions[1]['pointer']])
        self.assertEqual(rotation.pre_backup_gate(adapter)['status'], 'allowed')
        for changed in ({'staged': self.versions[0]['pointer']}, {'retired': [self.versions[0]['pointer']]}):
            adapter.index.update(changed)
            generated = []
            with self.assertRaises(ValueError):
                rotation.pre_backup_gate(adapter)
                generated.append('fourth complete package')
            self.assertFalse(generated)
            adapter.index.update(staged=None, retired=[])
        adapter.running_state['last_backup_stream'] = dict(pointer(13), path='/new/unindexed.tmbackup')
        with self.assertRaises(ValueError): rotation.pre_backup_gate(adapter)

    def test_bootstrap_gate_does_not_initiate_unverified_extra_cold_backups(self):
        adapter = Adapter(self.versions)
        with self.assertRaises(ValueError): rotation.pre_backup_gate(adapter)
        first = self.versions[2]['pointer']
        with adapter.manager.lock():
            self.assertEqual(rotation.pre_backup_gate(adapter, bootstrap_pointer=first)['status'], 'bootstrap_second_allowed')
        with adapter.manager.lock():
            with self.assertRaises(ValueError): rotation.pre_backup_gate(adapter, bootstrap_pointer=first)
        other = Adapter(self.versions)
        other.fail = first
        with other.manager.lock():
            with self.assertRaises(ValueError): rotation.pre_backup_gate(other, bootstrap_pointer=first)

    def test_bootstrap_first_and_second_slot_persist_when_published_advances_a_b_c(self):
        adapter = Adapter(self.versions)
        first = self.versions[0]
        adapter.running_state['last_backup_stream'] = dict(first['pointer'], path=first['receipt']['local_copy']['path'])
        adapter.running_state['last_backup'] = first['receipt']['local_copy']['path']
        with adapter.manager.lock(): rotation.pre_backup_gate(adapter, bootstrap_pointer=first['pointer'])
        reserved = deepcopy(adapter.index)
        self.assertGreater(reserved['generation'], 0)
        self.assertEqual(reserved['bootstrap']['first'], first['pointer'])
        for later in self.versions[1:]:
            adapter.running_state['last_backup_stream'] = dict(later['pointer'], path=later['receipt']['local_copy']['path'])
            adapter.running_state['last_backup'] = later['receipt']['local_copy']['path']
            with adapter.manager.lock():
                with self.assertRaises(ValueError, msg='must not generate a third/fourth bootstrap package'):
                    rotation.pre_backup_gate(adapter, bootstrap_pointer=later['pointer'])
            self.assertEqual(adapter.index, reserved)
        # The fixed reservation can be completed from two real reports; it does
        # not require or authorize another backup-generation call.
        result = rotation.advance(adapter, bootstrap=(first['pointer'], self.versions[1]['pointer']))
        self.assertEqual(result['status'], 'indexed')
        self.assertIsNone(adapter.index['bootstrap'])

    def test_on_demand_collector_refreshes_after_long_remote_read_and_unknown_jobs_block(self):
        from datetime import timedelta
        state = Adapter(self.versions).state()
        registry = {'type': 'tianming.nas-registered-jobs.v1', 'contract_sha256': 'b' * 64, 'jobs': {}}
        old = rotation.collect_registered_references(registry, set(), state, 'b' * 64,
                now=datetime.now(timezone.utc) - timedelta(minutes=2))
        with self.assertRaises(ValueError): rotation.validate_references(old, state, 'b' * 64)
        fresh = rotation.collect_registered_references(registry, set(), state, 'b' * 64)
        self.assertEqual(rotation.validate_references(fresh, state, 'b' * 64)['uploads'], [])
        with self.assertRaises(ValueError): rotation.collect_registered_references(registry, {'unknown-job'}, state, 'b' * 64)

    def test_finite_existing_job_names_can_be_registered_without_arbitrary_paths(self):
        for prefix in ('', 'smb-stream-', 'local-code-update-', 'mounted-checkpoint-', 'import-'):
            job = {'job_id': prefix + 'a' * 32, 'kind': 'restore', 'status': 'completed',
                   'backup_path': '', 'backup_sha256': None, 'receipt': ''}
            rotation.validate_job(job)
        for name in ('../outside', 'arbitrary-job', 'unknown-' + 'a' * 32):
            with self.assertRaises(ValueError): rotation.validate_job(dict(job, job_id=name))


if __name__ == '__main__': unittest.main()
