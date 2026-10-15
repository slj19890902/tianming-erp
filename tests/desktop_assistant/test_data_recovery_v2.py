from pathlib import Path
from datetime import datetime, timedelta
import threading
import time
import zipfile

import pytest

from desktop_assistant import data_recovery
from desktop_assistant.storage import decrypt_file, extract_verified, read_json, sha, write_json
from tests.desktop_assistant import test_recovery as recovery


@pytest.fixture
def scene():
    case = recovery.RecoveryTests()
    case.setUp()
    try:
        yield case
    finally:
        case.tearDown()


def create_point(case, name='point.tmdata'):
    target = case.root/name
    status = case.root/(name+'.status.json')
    log = case.root/(name+'.log')
    result = data_recovery.create(
        case.manager.root/'shared', case.package, target, recovery.PASSWORD,
        case.public, status_path=status, log_path=log,
    )
    return target, status, log, result


def test_v2_separates_program_and_restores_complete_shared_tree(scene):
    point, status, log, result = create_point(scene)
    raw = scene.root/'inspect.zip'
    decrypt_file(point, raw, recovery.PASSWORD)
    manifest = extract_verified(raw, scene.root/'inspect')
    assert manifest['type'] == data_recovery.TYPE
    assert manifest['complete_business_files'] is True
    assert manifest['program']['sha256'] == sha(scene.package)
    assert 'release.zip' not in manifest['files']
    assert 'compatibility.zip' not in manifest['files']
    assert result['program_embedded'] is False and result['restored_verified'] is True
    assert all(result[name] >= 0 for name in (
        'snapshot_seconds', 'check_seconds', 'compress_seconds',
        'encryption_seconds', 'write_seconds', 'readback_seconds'))
    assert read_json(status)['phase'] == 'complete'
    assert 'password' not in log.read_text(encoding='utf-8').lower()

    restored = recovery.TestManager(scene.root/'v2-restored', scene.public)
    answer = restored.restore_data_point(
        point, recovery.PASSWORD, scene.package, expected_backup_sha256=sha(point))
    assert answer['format'] == data_recovery.TYPE and answer['started'] is False
    assert restored.state['current'] == sha(scene.package)
    assert (restored.root/'shared/data/drawing.pdf').read_bytes() == b'synthetic attachment'
    assert read_json(restored.root/'shared/environment.json')['ERP_PORT'] == '18080'


def test_database_only_point_is_small_and_cannot_masquerade_as_complete(scene):
    point = scene.root/'quarter-hour.tmdb'
    result = data_recovery.create(
        scene.manager.root/'shared', scene.package, point, recovery.PASSWORD,
        scene.public, database_only=True)
    assert result['format'] == data_recovery.DATABASE_ONLY_TYPE
    assert result['business_files'] == 0
    target = scene.root/'must-not-restore'
    with pytest.raises(ValueError, match='完整数据恢复点'):
        data_recovery.restore(point, recovery.PASSWORD, target, scene.package, scene.public)
    assert not target.exists()


def test_wrong_program_and_tampered_point_never_publish_shared(scene):
    point, *_ = create_point(scene)
    wrong = scene.release('wrong-program')
    target = scene.root/'wrong-target'
    with pytest.raises(ValueError, match='签名程序'):
        data_recovery.restore(point, recovery.PASSWORD, target, wrong, scene.public)
    assert not target.exists()
    damaged = scene.root/'damaged.tmdata'
    damaged.write_bytes(point.read_bytes()[:-17] + b'x'*17)
    with pytest.raises(ValueError, match='口令错误|损坏'):
        data_recovery.restore(damaged, recovery.PASSWORD, scene.root/'damaged-target',
                              scene.package, scene.public)
    assert not (scene.root/'damaged-target').exists()


def test_online_sqlite_backup_captures_a_consistent_committed_state(scene):
    database = scene.manager.root/'shared/data/carton_erp.sqlite3'
    stop = threading.Event()

    def writer():
        import sqlite3
        number = 2
        while not stop.is_set():
            with sqlite3.connect(database) as db:
                db.execute('INSERT OR REPLACE INTO products(id,note) VALUES (?,?)',
                           (number, f'synthetic-{number}'))
                db.commit()
            number += 1

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        time.sleep(.02)
        target = scene.root/'online.sqlite3'
        result = data_recovery.sqlite_snapshot(database, target)
    finally:
        stop.set()
        thread.join(5)
    assert not thread.is_alive()
    assert result['revision'] == 'r1'
    assert result['counts']['products'] >= 1
    assert target.is_file()


def test_online_sqlite_backup_is_bounded_under_write_pressure(scene):
    database = scene.manager.root/'shared/data/carton_erp.sqlite3'
    target = scene.root/'bounded.sqlite3'
    with pytest.raises(TimeoutError, match='限定时间'):
        data_recovery.sqlite_snapshot(database, target, max_seconds=1e-12)


def test_business_file_change_during_pack_refuses_publication(scene, monkeypatch):
    original = data_recovery.pack_data_recovery

    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        (scene.manager.root/'shared/data/drawing.pdf').write_bytes(b'changed during backup')
        return result

    monkeypatch.setattr(data_recovery, 'pack_data_recovery', changed)
    output = scene.root/'unstable.tmdata'
    with pytest.raises(ValueError, match='业务文件在备份期间'):
        data_recovery.create(scene.manager.root/'shared', scene.package, output,
                             recovery.PASSWORD, scene.public)
    assert not output.exists()


def test_classification_excludes_program_cache_models_and_old_backups(tmp_path):
    expected = {
        'environment.json': 'configuration',
        'data/carton_erp.sqlite3-wal': 'sqlite_sidecar',
        'runtime/lib/python': 'program_runtime',
        'runtime/ocr/model/a.pth': 'ocr_model',
        'cache/view.bin': 'cache',
        'packages/old.zip': 'old_installer',
        'backups/old.tmbackup': 'historical_backup',
        'data/private_uploads/order.pdf': 'business_file',
    }
    assert {name: data_recovery.classify(Path(name)) for name in expected} == expected


def test_quarter_hour_database_schedule_is_local_online_and_idempotent(scene, monkeypatch):
    from desktop_assistant import nightly
    write_json(scene.manager.root/'preferences.json', {
        'protected_password': 'synthetic-reference',
        'nas': '/must-not-be-used-for-database-snapshot',
    })
    monkeypatch.setattr(nightly, 'unprotect_backup', lambda _reference: recovery.PASSWORD)
    now = datetime(2026, 10, 15, 12, 0, tzinfo=nightly.CN)
    first = nightly.run_database_snapshot_once(scene.manager, now=now)
    second = nightly.run_database_snapshot_once(scene.manager, now=now)
    assert first['status'] == 'verified' and second['status'] == 'up-to-date'
    point = Path(first['backup'])
    assert point.parent == scene.manager.root/'backups/database-points'
    assert point.suffix == '.tmdb' and point.is_file()
    assert scene.manager.state['last_database_snapshot_sha256'] == sha(point)
    status = read_json(scene.manager.root/'control/database-backup-status.json')
    assert status['phase'] == 'complete' and status['error'] is None
    health = nightly.database_backup_health(scene.manager, now=now)
    assert health['last_success_at'] == now.isoformat()
    assert health['consecutive_failures'] == 0 and health['stale'] is False


def test_database_snapshot_failures_preserve_last_success_and_raise_stale_alert(scene, monkeypatch):
    from desktop_assistant import nightly
    write_json(scene.manager.root/'preferences.json', {
        'protected_password': 'synthetic-reference',
    })
    monkeypatch.setattr(nightly, 'unprotect_backup', lambda _reference: recovery.PASSWORD)
    first_at = datetime(2026, 10, 15, 12, 0, tzinfo=nightly.CN)
    first = nightly.run_database_snapshot_once(scene.manager, now=first_at)
    old_point = Path(first['backup'])
    from desktop_assistant import data_recovery as module
    actual_create = module.create

    def timeout(*_args, **_kwargs):
        raise TimeoutError('synthetic write pressure')

    monkeypatch.setattr(module, 'create', timeout)
    for number in (1, 2):
        with pytest.raises(TimeoutError, match='synthetic write pressure'):
            nightly.run_database_snapshot_once(
                scene.manager, now=first_at + timedelta(minutes=15 + number))
        health = nightly.database_backup_health(
            scene.manager, now=first_at + timedelta(minutes=15 + number))
        assert health['last_success_at'] == first_at.isoformat()
        assert health['consecutive_failures'] == number
        assert old_point.is_file()
    stale = nightly.database_backup_health(scene.manager,
                                          now=first_at + timedelta(minutes=31))
    assert stale['stale'] is True and stale['alert_code'] == 'DATABASE_BACKUP_STALE'
    assert stale['age_seconds'] == 31 * 60
    assert 'synthetic write pressure' not in str(stale)

    monkeypatch.setattr(module, 'create', actual_create)
    recovered_at = first_at + timedelta(minutes=32)
    result = nightly.run_database_snapshot_once(scene.manager, now=recovered_at)
    assert result['status'] == 'verified'
    healthy = nightly.database_backup_health(scene.manager, now=recovered_at)
    assert healthy['last_success_at'] == recovered_at.isoformat()
    assert healthy['consecutive_failures'] == 0 and healthy['stale'] is False
    assert old_point.is_file()
