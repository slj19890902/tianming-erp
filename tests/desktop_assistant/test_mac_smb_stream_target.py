"""Actual backup/nightly/update routes over synthetic approved SMB peers."""
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import Mock
import sqlite3
import sys
import os

import pytest
from desktop_assistant import mac_smb_stream_target as subject
from desktop_assistant import mac_smb_stream_runtime as runtime, mac_smb_stream_probe_v2 as probe
from desktop_assistant import mac_smb_stream_receipt as receipt, mac_backup_storage as storage
from desktop_assistant import mac_service_manage as manage, nas_probe, nightly
from desktop_assistant.manager import Manager
from desktop_assistant.storage import sha, write_json, read_json, pack_tree, database_info
from test_mac_smb_stream_receipt import case
from tests.desktop_assistant.test_credential_restore import recovery_case, host_store
from test_mac_smb_stream_probe import Peer, ATTEMPT
from tests.desktop_assistant.test_recovery import PASSWORD, TestManager

pytestmark = pytest.mark.skipif(sys.platform != 'darwin', reason='Mac private instance leases')


@pytest.fixture
def route(case, monkeypatch):
    a = case
    # Migration SQL is supplied by each test, not a Windows child on this Mac.
    # The new pre-stop gate still validates all signed synthetic runtime bytes.
    from desktop_assistant import migration
    migration_runtime = migration.runtime_python
    monkeypatch.setattr(migration, 'runtime_python',
        lambda release, manifest, **_: migration_runtime(release, manifest, runnable=False))
    for name in ('releases', 'shared', 'control', 'backups', 'packages', 'staging'):
        (a.manager.root / name).chmod(0o700)
    # Remove the legacy TestManager storage adapter. Exercise production routing,
    # maintenance lock, crypto, complete DB/attachment checks and receipt IO.
    for name in ('lock', '_backup_stopped', '_update_locked'):
        monkeypatch.setattr(a.manager, name, MethodType(getattr(Manager, name), a.manager))
    a.binding.control['publisher_sha256'] = subject.cfg.digest(a.public)
    a.target = subject.destination(a.binding.control)
    a.peer = Peer(); a.preflights = 0; a.events = []; a.running = True; a.transfer_running = []
    monkeypatch.setattr(subject.cfg, 'CONTROL', a.root / 'stream-service.json')
    monkeypatch.setattr(subject.cfg, 'CONTRACT', a.root / 'stream-archive.json')
    def preflight(required):
        a.preflights += 1; a.events.append('preflight')
        assert a.running or 'stop' not in a.events
        return probe.check(a.peer.tree(), a.binding.value, ATTEMPT, required)
    monkeypatch.setattr(runtime, 'preflight', preflight)
    def stop(): a.events.append('stop'); a.running = False
    def start(): a.events.append('start'); a.running = True
    monkeypatch.setattr(a.manager, '_process', lambda: object() if a.running else None)
    monkeypatch.setattr(a.manager, 'stop', stop)
    monkeypatch.setattr(a.manager, 'start', start)
    transfer = runtime.transfer
    def send(*args):
        a.events.append('transfer'); a.transfer_running.append(a.running)
        return transfer(*args)
    monkeypatch.setattr(runtime, 'transfer', send)
    # Match the dedicated service entry's private process umask, including the
    # original Manager state writes between successive migration steps.
    previous_umask = os.umask(0o077)
    try: yield a
    finally: os.umask(previous_umask)


def test_complete_manual_backup_preflights_then_stops_and_restores_running_erp(route, host_store):
    a = route; db = a.manager.root / 'shared/data/carton_erp.sqlite3'; before = sha(db)
    result = a.manager.backup(PASSWORD, a.target)
    assert a.events == ['preflight', 'stop', 'start', 'transfer']
    assert a.transfer_running == [True]
    assert result.parent == a.manager.root / 'backups' and a.running
    assert storage.verified_at(a.manager.root, a.manager.state) == a.manager.state['last_backup_at']
    restored = TestManager(a.root / 'restore-check', a.public)
    assert restored.restore(result, PASSWORD)['credentials']['restored'] == ['backup', 'mailbox', 'openai']
    assert database_info(restored.root / 'shared/data/carton_erp.sqlite3') == database_info(db)
    assert (restored.root / 'shared/data/drawing.pdf').read_bytes() == b'synthetic attachment'
    assert sha(db) == before and not (a.root / str(a.target)).exists()


@pytest.mark.parametrize('reason', ['nas-space', 'local-space', 'network', 'wrong-root', 'wrong-key', 'bad-proof', 'bad-target'])
def test_failure_before_stop_preserves_original_process_state_and_data(route, monkeypatch, reason):
    a = route; state = a.manager.state
    if reason == 'nas-space': a.peer.capacity['caller_available_units'] = 0
    if reason == 'local-space': monkeypatch.setattr(subject.shutil, 'disk_usage', lambda p: SimpleNamespace(free=0))
    if reason == 'network': monkeypatch.setattr(runtime, 'preflight', Mock(side_effect=OSError('private detail')))
    if reason == 'wrong-root':
        other = a.root / 'other'; (other / 'control').mkdir(parents=True)
        monkeypatch.setattr(a.manager, 'root', other)
    if reason == 'wrong-key': monkeypatch.setattr(a.manager, 'public_key', b'other')
    if reason == 'bad-proof': monkeypatch.setattr(runtime, 'preflight', lambda n: {'attempt_id':ATTEMPT})
    if reason == 'bad-target': a.target = Path(subject.PREFIX + 'bad')
    with pytest.raises(ValueError, match=subject.ERROR): a.manager.backup(PASSWORD, a.target)
    assert 'stop' not in a.events and 'transfer' not in a.events and a.running
    assert read_json(a.root / 'instance/state.json') == state


@pytest.mark.parametrize('running,shutdown', [(True, False), (False, False), (True, True)])
def test_transfer_failure_preserves_scene_and_original_restart_rules(route, monkeypatch, running, shutdown):
    a = route; a.running = running
    monkeypatch.setattr(runtime, 'transfer', Mock(side_effect=OSError('synthetic-secret-error')))
    with a.manager.lock(), pytest.raises(ValueError, match=subject.ERROR):
        a.manager._backup_locked(PASSWORD, a.target, restart_allowed=lambda: not shutdown)
    assert a.running is (running and not shutdown)
    assert 'last_backup' not in a.manager.state
    assert a.manager.state['backup_error'] == subject.ERROR
    assert list((a.manager.root / 'staging').glob('smb-stream-*'))


def test_nightly_uses_same_route_and_verified_receipt_prevents_duplicate_run(route, monkeypatch):
    a = route
    write_json(a.manager.root / 'preferences.json', {'nas':str(a.target), 'protected_password':'synthetic-only'})
    monkeypatch.setattr(nightly, 'unprotect_backup', lambda ref: PASSWORD)
    now = datetime.now(timezone.utc)
    assert nightly.run_once(a.manager, now=now)['status'] == 'verified'
    assert nightly.run_once(a.manager, now=now)['status'] == 'up-to-date'
    assert a.preflights == 1 and a.events == ['preflight', 'stop', 'start', 'transfer']
    assert a.transfer_running == [True]


def test_nightly_probe_failure_has_public_warning_without_stopping(route, monkeypatch):
    a = route; a.peer.capacity['caller_available_units'] = 0
    write_json(a.manager.root / 'preferences.json', {'nas':str(a.target), 'protected_password':'synthetic-only'})
    monkeypatch.setattr(nightly, 'unprotect_backup', lambda ref: PASSWORD)
    with pytest.raises(ValueError): nightly.run_once(a.manager)
    assert a.running and 'stop' not in a.events
    assert a.manager.state['backup_error'] and 'last_backup' not in a.manager.state


def test_update_keeps_private_backup_path_and_success_receipt(route, monkeypatch):
    a = route; package = a.release('two')
    from desktop_assistant import retention
    retain = Mock(); monkeypatch.setattr(retention, 'after_update', retain)
    assert a.manager.update(package, PASSWORD, a.target) == 'two'
    state = a.manager.state; backup = Path(state['update_backup'])
    assert backup == Path(state['last_backup']) and backup.is_file()
    assert backup.parent == a.manager.root / 'backups' and a.running
    assert receipt.verified_at(a.manager.root, state) == state['last_backup_at']
    retain.assert_called_once_with(a.manager, package.parent)
    assert a.events == ['preflight', 'stop', 'transfer', 'start']
    assert a.transfer_running == [False]


def test_migration_failure_retains_exact_local_evidence_and_stays_stopped(route, monkeypatch):
    a = route; original = a.manager.state['current']
    a.release('future', 'r2', extra_tables=('feature_r2',))
    package = a.root / 'future-signed.zip'
    contract = dict(policy='preserve_existing_facts_v1', from_revision='r1', rollback_package_sha256=original)
    pack_tree(a.root / 'source-future', package,
        dict(type='tianming.release.v1', version='future', revision='r2', migration=contract), a.key)
    from desktop_assistant import migration
    runs = []
    def migrate(release, shared, revision, log, **kwargs):
        runs.append(shared)
        if shared == a.manager.root / 'shared': raise ValueError('synthetic real-stage failure')
        with closing(sqlite3.connect(shared / 'data/carton_erp.sqlite3')) as db:
            db.execute('CREATE TABLE feature_r2(id INTEGER PRIMARY KEY, note TEXT)')
            db.execute("UPDATE alembic_version SET version_num='r2'"); db.commit()
        log.write_text('synthetic isolated migration')
    monkeypatch.setattr(migration, 'run_migration', migrate)
    with pytest.raises(ValueError, match='数据库升级未完整通过'):
        a.manager.update(package, PASSWORD, a.target)
    state = a.manager.state; backup = Path(state['update_backup'])
    assert len(runs) == 2 and not a.running and state['operation'] == 'migration_failed'
    assert a.transfer_running == [False]
    assert backup.is_file() and sha(backup) == state['migration_backup_sha256']
    assert receipt.verified_at(a.manager.root, state) == state['last_backup_at']
    assert state['current'] == original


def test_feed_chain_does_not_treat_selector_as_directory(route, monkeypatch):
    a = route; package = a.release('feed-two')
    plan = Mock(return_value=[]); monkeypatch.setattr(a.manager, '_plan_update_chain', plan)
    from desktop_assistant import retention
    monkeypatch.setattr(retention, 'after_update', Mock())
    assert a.manager.update_from_feed(package, PASSWORD, a.target) == '已经是该版本'
    plan.assert_called_once_with(package, package.parent)
    assert not a.events


def test_actual_feed_chain_and_migration_keep_stream_backups(route, monkeypatch):
    a = route; feed = a.root / 'explicit-release-feed'; feed.mkdir()
    def package(name, revision, previous=None, origin=None):
        a.release(name, revision, extra_tables=() if previous is None else ('feature_' + revision,))
        path = feed / (name + '.zip')
        migration = (None if previous is None else dict(policy='preserve_existing_facts_v1',
                     from_revision=origin, rollback_package_sha256=sha(previous)))
        pack_tree(a.root / ('source-' + name), path,
            dict(type='tianming.release.v1', version=name, revision=revision, migration=migration), a.key)
        target = feed / (sha(path) + '.zip'); path.rename(target)
        return target
    bridge = package('bridge', 'r1')
    second = package('second', 'r2', bridge, 'r1')
    latest = package('latest', 'r3', second, 'r2')
    from desktop_assistant import migration, retention
    runs = []
    def migrate(release, shared, revision, log, **kwargs):
        runs.append((shared, revision))
        with closing(sqlite3.connect(shared / 'data/carton_erp.sqlite3')) as db:
            db.execute('CREATE TABLE feature_' + revision + '(id INTEGER PRIMARY KEY, note TEXT)')
            db.execute('UPDATE alembic_version SET version_num=?', (revision,)); db.commit()
        log.write_text('synthetic migration')
    monkeypatch.setattr(migration, 'run_migration', migrate)
    retain = Mock(); monkeypatch.setattr(retention, 'after_update', retain)
    assert a.manager.update_from_feed(latest, PASSWORD, a.target) == 'latest'
    state = a.manager.state
    assert state['current'] == sha(latest) and a.running and a.preflights == 3
    assert len(runs) == 4 and len(list((a.manager.root / 'backups').glob('*.tmbackup'))) == 3
    assert a.transfer_running == [False, False, False]
    assert sha(Path(state['update_backup'])) == state['migration_backup_sha256']
    assert receipt.verified_at(a.manager.root, state) == state['last_backup_at']
    retain.assert_called_once_with(a.manager, feed)
    with closing(sqlite3.connect(a.manager.root / 'shared/data/carton_erp.sqlite3')) as db:
        assert db.execute('SELECT * FROM sales_orders').fetchall() == [(1, 'synthetic')]


def test_maintenance_lock_remains_held_during_preflight(route, monkeypatch):
    from desktop_assistant.operation_lock import operation_lock, BUSY
    original = runtime.preflight
    def check(required):
        with pytest.raises(ValueError, match=BUSY):
            with operation_lock(route.manager.root / 'control/operation.lock'): pass
        return original(required)
    monkeypatch.setattr(runtime, 'preflight', check)
    route.manager.backup(PASSWORD, route.target)


def test_root_approval_change_after_preflight_does_not_stop(route, monkeypatch):
    a = route; original = runtime.preflight
    def changed(required):
        proof = original(required)
        a.binding.check = Mock(side_effect=ValueError('changed root record'))
        return proof
    monkeypatch.setattr(runtime, 'preflight', changed)
    with pytest.raises(ValueError, match=subject.ERROR): a.manager.backup(PASSWORD, a.target)
    assert a.running and a.events == ['preflight'] and not a.calls


def test_preparation_and_history_status_never_probe_or_load_credentials(route, monkeypatch):
    a = route
    monkeypatch.setattr(runtime, 'preflight', Mock(side_effect=AssertionError('no network')))
    monkeypatch.setattr(storage, 'before_write', Mock(side_effect=AssertionError('no POSIX mount')))
    assert storage.prepare_destination(a.target) is a.binding
    assert subject.configured(a.target, a.binding.context)
    assert subject.approved_destination(a.binding.context) == a.target


def test_selected_stream_failure_never_uses_legacy_contract(route, monkeypatch):
    a = route
    monkeypatch.setattr(runtime, 'load_binding', Mock(side_effect=ValueError('bad stream approval')))
    old = Mock(side_effect=AssertionError('no fallback')); monkeypatch.setattr(storage, '_contract', old)
    with pytest.raises(ValueError, match=subject.ERROR): storage.prepare_destination(a.target)
    assert not old.called


@pytest.mark.parametrize('which', ['CONTROL', 'CONTRACT'])
def test_partial_stream_install_requires_validation_instead_of_legacy_selection(route, monkeypatch, which):
    a = route; getattr(subject.cfg, which).write_bytes(b'partial')
    monkeypatch.setattr(runtime, 'load_binding', Mock(side_effect=ValueError('incomplete')))
    legacy = Mock(); monkeypatch.setattr(storage, '_contract', legacy)
    with pytest.raises(ValueError): manage.approved_destination(a.binding.context)
    assert not legacy.called


def test_existing_saved_legacy_route_is_not_changed_by_root_stream_records(route, monkeypatch):
    a = route; subject.cfg.CONTROL.write_bytes(b'new installation pending explicit configuration')
    root = object(); context = a.binding.context
    monkeypatch.setattr(manage, 'settings', lambda *args: (b'', {'nas':'/old-approved'}))
    monkeypatch.setattr(storage, '_contract', lambda: ({'destination':'/old-approved'}, 'e'*64, context))
    monkeypatch.setattr(receipt, 'approval', Mock(side_effect=AssertionError('old selection')))
    assert manage.configured(root, context)


def test_configure_switches_only_destination_preserving_preferences(route, monkeypatch):
    a = route; previous = {'nas':'old', 'protected_password':'tm-keychain-v1:backup:'+'d'*32,
                           'nightly_schedule':{'old':True}, 'arbitrary':[1,2]}
    write_json(a.manager.root / 'preferences.json', previous)
    (a.manager.root / 'preferences.json').chmod(0o600)
    subject.cfg.CONTROL.write_bytes(b'synthetic approved record')
    with receipt.root_lease(a.binding.context) as root:
        manage.save_destination(root, a.binding.context, root.check)
        assert manage.configured(root, a.binding.context)
    assert read_json(a.manager.root / 'preferences.json') == {**previous, 'nas':str(a.target)}
    assert a.preflights == 0 and not a.events
