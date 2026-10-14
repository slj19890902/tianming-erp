"""Reject native migration configuration before backup or production stop."""
from unittest.mock import Mock
import pytest
from desktop_assistant import manager as module, migration


@pytest.mark.parametrize('native', [True, False])
def test_migration_prepared_before_backup_without_native_server_environment(tmp_path, monkeypatch, native):
    manager = module.Manager(tmp_path / 'instance', b'synthetic-unused')
    old = {'current': 'a' * 64}
    module.write_json(manager.root / 'state.json', old)
    candidate = {'id': 'b' * 64, 'revision': 'next', 'version': 'candidate',
        'migration': {'policy': 'preserve_existing_facts_v1', 'from_revision': 'before',
                      'rollback_package_sha256': old['current']}}
    manifest = dict(candidate, runtime_platform='macos-arm64' if native else 'win-amd64')
    monkeypatch.setattr(manager, 'stage_release', lambda _: candidate)
    monkeypatch.setattr(manager, '_approve_execution', Mock())
    monkeypatch.setattr(manager, '_check_dispatch_reader', Mock())
    monkeypatch.setattr(manager, '_check_provenance_reader', Mock())
    monkeypatch.setattr(manager, 'compatible', lambda *a: False)
    monkeypatch.setattr(module, 'database_info', lambda _: {'revision': 'before'})
    release = tmp_path / 'approved-migration-tree'
    monkeypatch.setattr(manager, '_execution_release', lambda identity, kind: release)
    monkeypatch.setattr(module, 'signed_release_manifest', lambda *a: manifest)
    environment = Mock(side_effect=AssertionError('native migration must not use server guard')) if native else Mock(return_value={'ERP_ENVIRONMENT': 'production'})
    monkeypatch.setattr(manager, '_environment', environment)
    prepare = Mock(side_effect=ValueError('synthetic preflight rejection'))
    monkeypatch.setattr(migration, 'prepare_migration', prepare, raising=False)
    backup = Mock(side_effect=AssertionError('backup before migration preflight'))
    stop = Mock(side_effect=AssertionError('stop before migration preflight'))
    monkeypatch.setattr(manager, '_backup_preflight', backup)
    monkeypatch.setattr(manager, 'stop', stop)
    with pytest.raises(ValueError, match='synthetic preflight rejection'):
        manager._update_locked(tmp_path / 'candidate.zip', 'synthetic', tmp_path / 'nas')
    prepare.assert_called_once_with(release, manager.root / 'shared', 'next',
        environment=None if native else {'ERP_ENVIRONMENT': 'production'}, release_manifest=manifest)
    backup.assert_not_called(); stop.assert_not_called()
    if native: environment.assert_not_called()
    assert manager.state == old


def test_run_migration_revalidates_at_spawn_boundary(tmp_path, monkeypatch):
    prepare = Mock(side_effect=ValueError('candidate identity changed'))
    monkeypatch.setattr(migration, 'prepare_migration', prepare, raising=False)
    process = Mock(side_effect=AssertionError('must not spawn'))
    monkeypatch.setattr(migration.subprocess, 'run', process)
    monkeypatch.setattr(migration.subprocess, 'Popen', process)
    with pytest.raises(ValueError, match='candidate identity changed'):
        migration.run_migration(tmp_path / 'release', tmp_path / 'shared', 'next', tmp_path / 'log',
            release_manifest={'revision': 'next'})
    process.assert_not_called()
    assert not (tmp_path / 'log').exists()
