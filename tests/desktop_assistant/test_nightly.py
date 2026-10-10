from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import threading
from unittest.mock import Mock

import pytest

from desktop_assistant import nightly
from desktop_assistant.manager import CN
from desktop_assistant.operation_lock import operation_lock
from desktop_assistant.storage import read_json, write_json
from tests.desktop_assistant import test_recovery as recovery

NOW = datetime(2026,10,11,23,5,tzinfo=CN)


class Manager:
    def __init__(self, root):
        self.root = root
        (root/'control').mkdir()
        write_json(root/'state.json',{'current':'one'})
        write_json(root/'preferences.json',{'protected_password':'synthetic-reference','nas':str(root/'nas')})
        self.starts = self.backups = 0

    @property
    def state(self):
        return read_json(self.root/'state.json')

    def lock(self):
        return operation_lock(self.root/'control/operation.lock')

    def start(self):
        self.starts += 1

    def _backup_locked(self, password, nas):
        self.backups += 1
        state=self.state;state['last_backup_at']=NOW.isoformat()
        write_json(self.root/'state.json',state)
        return nas/'synthetic.tmbackup'


@pytest.fixture
def manager(tmp_path,monkeypatch):
    monkeypatch.setattr(nightly,'unprotect_backup',lambda reference:'synthetic-password-only')
    return Manager(tmp_path.resolve())


def test_due_backup_once_without_starting_stopped_mac(manager):
    assert nightly.run_once(manager,now=NOW)['status']=='verified'
    assert nightly.run_once(manager,now=NOW)['status']=='up-to-date'
    assert manager.backups==1 and manager.starts==0


def test_before_23_uses_previous_due_and_windows_resume_remains(manager):
    state=manager.state;state['last_backup_at']='2026-10-10T23:10:00+08:00'
    write_json(manager.root/'state.json',state)
    assert nightly.run_once(manager,now=NOW.replace(hour=22),resume_when_stopped=True)['status']=='up-to-date'
    assert manager.starts==1 and manager.backups==0
    assert nightly.run_once(manager,now=NOW,resume_when_stopped=True)['status']=='verified'
    assert manager.starts==2 and manager.backups==1


@pytest.mark.parametrize('updates', [{'current':None},{'manual_stop':True},{'onboarding_pending':True},
                                    {'operation':'migration_failed'},{'operation':'migration_running'}])
def test_stop_and_pending_states_never_read_credentials(manager,monkeypatch,updates):
    state=manager.state;state.update(updates);write_json(manager.root/'state.json',state)
    monkeypatch.setattr(nightly,'unprotect_backup',lambda value:pytest.fail('must not load credential'))
    assert nightly.run_once(manager,now=NOW)['status'] in {'skipped','maintenance-attention'}
    assert manager.starts==manager.backups==0


def test_credential_failure_before_backup_has_only_safe_public_error(manager,monkeypatch):
    def fail(value): raise ValueError('synthetic-secret-error-details')
    monkeypatch.setattr(nightly,'unprotect_backup',fail)
    with pytest.raises(ValueError): nightly.run_once(manager,now=NOW)
    assert manager.backups==manager.starts==0
    assert 'synthetic-secret' not in manager.state['backup_error']


def test_concurrent_timer_never_duplicates_backup(manager):
    entered,release=threading.Event(),threading.Event()
    original=manager._backup_locked
    def blocked(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)
    manager._backup_locked=blocked
    results=[]
    thread=threading.Thread(target=lambda:results.append(nightly.run_once(manager,now=NOW)))
    thread.start()
    try:
        assert entered.wait(5)
        assert nightly.run_once(manager,now=NOW)['status']=='maintenance-busy'
    finally:
        release.set();thread.join(5)
    assert not thread.is_alive() and results[0]['status']=='verified'
    assert nightly.run_once(manager,now=NOW)['status']=='up-to-date'
    assert manager.backups==1


def test_missing_nas_does_not_stop_running_service(monkeypatch):
    case=recovery.RecoveryTests();case.setUp()
    try:
        write_json(case.manager.root/'preferences.json',{'protected_password':'synthetic','nas':str(case.root/'missing-nas')})
        monkeypatch.setattr(nightly,'unprotect_backup',lambda value:recovery.PASSWORD)
        stop,start=Mock(),Mock()
        monkeypatch.setattr(case.manager,'stop',stop);monkeypatch.setattr(case.manager,'start',start)
        with pytest.raises(ValueError):nightly.run_once(case.manager,now=NOW)
        stop.assert_not_called();start.assert_not_called()
        assert case.manager.state['backup_error']
    finally:case.tearDown()


def test_real_encrypted_nightly_backup_leaves_stopped_service_stopped(monkeypatch):
    from desktop_assistant import credential_transfer
    case=recovery.RecoveryTests();case.setUp()
    try:
        write_json(case.manager.root/'preferences.json',{'protected_password':'synthetic','nas':str(case.nas)})
        monkeypatch.setattr(nightly,'unprotect_backup',lambda value:recovery.PASSWORD)
        monkeypatch.setattr(credential_transfer,'unprotect',lambda *args:recovery.PASSWORD)
        start=Mock();monkeypatch.setattr(case.manager,'start',start)
        result=nightly.run_once(case.manager)
        assert result['status']=='verified' and Path(result['backup']).is_file()
        start.assert_not_called()
        assert nightly.run_once(case.manager)['status']=='up-to-date'
        assert len(list(case.nas.glob('*.tmbackup')))==1
    finally:case.tearDown()
