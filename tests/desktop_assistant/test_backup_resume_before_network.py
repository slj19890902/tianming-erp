"""A captured recovery point stays valid while ERP resumes during NAS IO."""
from pathlib import Path
import sqlite3
import sys
from unittest.mock import Mock

import pytest

from desktop_assistant import manager as manager_module
from desktop_assistant import mac_smb_stream_receipt as receipt
from desktop_assistant.storage import database_info
from desktop_assistant.migration import facts, files
from tests.desktop_assistant.test_mac_smb_stream_receipt import case
from tests.desktop_assistant.test_credential_restore import recovery_case, host_store
from tests.desktop_assistant.test_recovery import TestManager, PASSWORD

mac_only = pytest.mark.skipif(sys.platform != 'darwin', reason='Mac private receipt and file leases')

@mac_only
def test_resume_precedes_upload_and_restore_keeps_captured_facts(case, host_store, monkeypatch):
    database = case.manager.root / 'shared/data/carton_erp.sqlite3'
    before = facts(database)
    # Recovery rewraps the host-specific encrypted credential, but every other
    # column and business table must match the captured point exactly.
    columns = before['columns']
    columns['email_intake_settings'].remove('encrypted_secret')
    before = facts(database, columns)
    before_files = files(case.manager.root / 'shared')
    running = {'value': False}
    monkeypatch.setattr(case.manager, '_process', lambda: ('owned', {}) if running['value'] else None)
    transfer = receipt.runtime.transfer
    events = []

    def resume():
        events.append('ready')
        running['value'] = True
        return True

    def online_transfer(*args):
        assert running['value']
        events.append('network')
        # Ordinary business may commit after the authenticated point was captured.
        with sqlite3.connect(database) as connection:
            connection.execute("UPDATE sales_orders SET note='after captured point' WHERE id=1")
        (case.manager.root / 'shared/data/drawing.pdf').write_bytes(b'new live attachment')
        return transfer(*args)

    monkeypatch.setattr(receipt.runtime, 'transfer', online_transfer)
    with case.manager.lock():
        result = receipt.archive_and_publish_captured(case.manager, PASSWORD, resume)
    assert events == ['ready', 'network'] and running['value']
    target = TestManager(case.root / 'resumed-restored', case.public)
    target.restore(result, PASSWORD)
    assert facts(target.root / 'shared/data/carton_erp.sqlite3', columns) == before
    assert files(target.root / 'shared') == before_files
    with sqlite3.connect(database) as connection:
        assert connection.execute('SELECT note FROM sales_orders').fetchall() == [('after captured point',)]
    assert (case.manager.root / 'shared/data/drawing.pdf').read_bytes() == b'new live attachment'
    assert receipt.verified_at(case.manager.root, case.manager.state)


@mac_only
def test_failed_upload_keeps_resumed_erp_and_does_not_publish_success(case, monkeypatch):
    before = case.manager.state
    running = {'value': False}
    monkeypatch.setattr(case.manager, '_process', lambda: ('owned', {}) if running['value'] else None)
    def resume():
        running['value'] = True
        return True
    def disconnected(*args):
        assert running['value']
        raise OSError('synthetic NAS timeout')
    monkeypatch.setattr(receipt.runtime, 'transfer', disconnected)
    with case.manager.lock(), pytest.raises(ValueError):
        receipt.archive_and_publish_captured(case.manager, PASSWORD, resume)
    assert running['value'] and case.manager.state == before


@mac_only
def test_resume_failure_does_not_begin_network(case, monkeypatch):
    transfer = Mock()
    monkeypatch.setattr(receipt.runtime, 'transfer', transfer)
    def failed():
        raise ValueError('synthetic readiness failure')
    with case.manager.lock(), pytest.raises(ValueError):
        receipt.archive_and_publish_captured(case.manager, PASSWORD, failed)
    transfer.assert_not_called()


@mac_only
def test_local_cipher_sync_failure_never_resumes_into_transfer(case, monkeypatch):
    payload = case.manager._build_backup_payload
    resume = Mock(return_value=True)
    transfer = Mock()
    monkeypatch.setattr(receipt.runtime, 'transfer', transfer)
    def built(*args):
        result = payload(*args)
        # Fail precisely after encryption/authentication, before the new
        # recovery-point durability barrier and callback.
        def failed(_): raise OSError('synthetic local sync failure')
        monkeypatch.setattr(receipt.builder.os, 'fsync', failed)
        return result
    monkeypatch.setattr(case.manager, '_build_backup_payload', built)
    before = case.manager.state
    with case.manager.lock(), pytest.raises(ValueError):
        receipt.archive_and_publish_captured(case.manager, PASSWORD, resume)
    resume.assert_not_called(); transfer.assert_not_called()
    assert case.manager.state == before


@pytest.mark.parametrize('fail_network', [False, True])
def test_manager_resumes_once_before_transfer_not_in_finally(tmp_path, monkeypatch, fail_network):
    manager = manager_module.Manager(tmp_path / 'instance', b'synthetic-unused')
    events = []; running = {'value': True}
    monkeypatch.setattr(manager, '_process', lambda: ('owned', {}) if running['value'] else None)
    monkeypatch.setattr(manager, '_backup_preflight', lambda _: events.append('preflight'))
    def stop():
        events.append('stop'); running['value'] = False
    def start():
        events.append('ready'); running['value'] = True
    monkeypatch.setattr(manager, 'stop', stop)
    monkeypatch.setattr(manager, 'start', start)
    from desktop_assistant import mac_smb_stream_target as stream
    monkeypatch.setattr(stream, 'selected', lambda _: True)
    def backup(actual, password, path, resume):
        assert not running['value']
        events.append('capture')
        assert resume() is True
        assert running['value']
        events.append('network')
        if fail_network: raise OSError('synthetic NAS timeout')
        return path / 'synthetic.tmbackup'
    monkeypatch.setattr(stream, 'archive_with_resume', backup, raising=False)
    # The old all-stopped path is specifically forbidden for a running service.
    monkeypatch.setattr(manager, '_backup_stopped', lambda *a: pytest.fail('network while ERP stopped'))
    if fail_network:
        with pytest.raises(OSError, match='NAS timeout'):
            manager._backup_locked(PASSWORD, tmp_path / 'nas')
    else:
        assert manager._backup_locked(PASSWORD, tmp_path / 'nas').name == 'synthetic.tmbackup'
    assert events == ['preflight', 'stop', 'capture', 'ready', 'network']
    assert running['value']


@pytest.mark.parametrize('failure', ['capture', 'startup', 'shutdown'])
def test_manager_failure_and_shutdown_never_repeat_start(tmp_path, monkeypatch, failure):
    manager = manager_module.Manager(tmp_path / 'instance', b'synthetic-unused')
    running = {'value': True}; starts = []
    monkeypatch.setattr(manager, '_process', lambda: object() if running['value'] else None)
    monkeypatch.setattr(manager, '_backup_preflight', lambda _: None)
    monkeypatch.setattr(manager, 'stop', lambda: running.update(value=False))
    def start():
        starts.append('start')
        if failure == 'startup': raise ValueError('synthetic readiness failure')
        running['value'] = True
    monkeypatch.setattr(manager, 'start', start)
    from desktop_assistant import mac_smb_stream_target as stream
    monkeypatch.setattr(stream, 'selected', lambda _: True)
    def backup(actual, password, path, resume):
        if failure == 'capture': raise ValueError('synthetic capture failure')
        resumed = resume()
        assert failure == 'shutdown' and resumed is False and not running['value']
        raise ValueError('synthetic network failure')
    monkeypatch.setattr(stream, 'archive_with_resume', backup)
    with pytest.raises(ValueError, match='synthetic'):
        manager._backup_locked(PASSWORD, tmp_path / 'nas', restart_allowed=lambda: failure != 'shutdown')
    assert len(starts) == (0 if failure == 'shutdown' else 1)
    assert running['value'] is (failure == 'capture')


@pytest.mark.parametrize('fault', ['state', 'ciphertext', 'readback'])
@mac_only
def test_resumed_capture_never_publishes_corrupt_or_conflicting_result(case, monkeypatch, fault):
    transfer = receipt.runtime.transfer
    before = case.manager.state
    running = {'value': False}
    monkeypatch.setattr(case.manager, '_process', lambda: object() if running['value'] else None)
    def resume():
        if fault == 'ciphertext':
            encrypted = next((case.manager.root / 'staging').glob('smb-stream-*/*.tmbackup'))
            with encrypted.open('r+b') as stream: stream.write(b'bad')
        running['value'] = True
        return True
    def changed(*args):
        result = transfer(*args)
        if fault == 'state':
            from desktop_assistant.storage import write_json
            write_json(case.manager.root / 'state.json', {**before, 'concurrent_marker': 'preserve'})
            (case.manager.root / 'state.json').chmod(0o600)
        if fault == 'readback':
            import os
            os.pwrite(args[1], b'bad', 0)
        return result
    monkeypatch.setattr(receipt.runtime, 'transfer', changed)
    with case.manager.lock(), pytest.raises(ValueError):
        receipt.archive_and_publish_captured(case.manager, PASSWORD, resume)
    assert running['value'] and 'last_backup' not in case.manager.state
    assert not (case.manager.root / 'control/backup-receipts').exists()
    if fault == 'state': assert case.manager.state['concurrent_marker'] == 'preserve'
    if fault == 'ciphertext': assert not case.calls


@pytest.mark.parametrize('fail_network', [False, True])
def test_shutdown_after_resume_stops_owned_process_on_transfer_return(tmp_path, monkeypatch, fail_network):
    manager = manager_module.Manager(tmp_path / 'instance', b'synthetic-unused')
    running = {'value': True}; shutdown = {'value': False}; events = []
    owned = ('synthetic-process', {'nonce': 'synthetic-only'})
    monkeypatch.setattr(manager, '_process', lambda: owned if running['value'] else None)
    monkeypatch.setattr(manager, '_backup_preflight', lambda _: None)
    def stop(): events.append('stop'); running['value'] = False
    def start(): events.append('ready'); running['value'] = True
    monkeypatch.setattr(manager, 'stop', stop)
    monkeypatch.setattr(manager, 'start', start)
    from desktop_assistant import mac_smb_stream_target as stream
    monkeypatch.setattr(stream, 'selected', lambda _: True)
    def backup(actual, password, path, resume):
        assert resume() is True
        events.append('network'); shutdown['value'] = True
        if fail_network: raise ValueError('synthetic NAS failure')
        return path / 'synthetic.tmbackup'
    monkeypatch.setattr(stream, 'archive_with_resume', backup)
    if fail_network:
        with pytest.raises(ValueError):
            manager._backup_locked(PASSWORD, tmp_path / 'nas', restart_allowed=lambda: not shutdown['value'])
    else:
        manager._backup_locked(PASSWORD, tmp_path / 'nas', restart_allowed=lambda: not shutdown['value'])
    assert not running['value'] and events == ['stop', 'ready', 'network', 'stop']
