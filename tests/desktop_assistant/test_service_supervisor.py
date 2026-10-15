from contextlib import contextmanager
import json
import threading

import pytest

from desktop_assistant import service_supervisor as service
from desktop_assistant.operation_lock import BUSY, operation_lock


class Manager:
    def __init__(self, root):
        self.root = root
        (root / 'control').mkdir()
        self.state = {'current':'signed-release'}
        self.running = None
        self.starts = self.stops = 0
        self.busy = False
        self.reaps = 0

    def reap_owned_children(self):
        self.reaps += 1

    @contextmanager
    def lock(self):
        if self.busy:
            raise ValueError(BUSY)
        yield

    def _process(self):
        return self.running

    def start(self):
        self.starts += 1
        self.running = (object(), {'pid':self.starts,'created':1,'exe':'owned-python'})

    def stop(self):
        self.stops += 1
        self.running = None


def test_restart_after_exit_without_rehashing_running_release_every_tick(tmp_path):
    manager = Manager(tmp_path)
    cache = {}
    assert service.tick(manager, cache) == 'running'
    assert service.tick(manager, cache) == 'running'
    assert manager.starts == 1
    manager.running = None
    assert service.tick(manager, cache) == 'running'
    assert manager.starts == 2
    manager.state['current'] = 'new-release'
    assert service.tick(manager, cache) == 'running'
    assert manager.starts == 3


@pytest.mark.parametrize('state,status', [
    ({'current':None}, 'not-restored'),
    ({'current':'one','manual_stop':True}, 'manually-stopped'),
    ({'current':'one','operation':'migration_running'}, 'maintenance-attention'),
    ({'current':'one','operation':'update_failed_reader_contract'}, 'maintenance-attention'),
    ({'current':'one','onboarding_pending':True}, 'maintenance-attention'),
])
def test_explicit_stop_and_failed_maintenance_are_not_restarted(tmp_path, state, status):
    manager = Manager(tmp_path)
    manager.state = state
    assert service.tick(manager) == status
    assert manager.starts == 0
    assert manager.reaps == 1


def test_backup_lock_prevents_restart_and_shutdown_does_not_force_kill(tmp_path):
    manager = Manager(tmp_path)
    manager.busy = True
    assert service.tick(manager) == 'maintenance-busy'
    with pytest.raises(ValueError, match=BUSY):
        service.stop_owned(manager, timeout=0)
    assert manager.starts == manager.stops == 0
    assert manager.reaps == 1


def test_supervisor_shutdown_and_exclusive_ownership(tmp_path):
    manager = Manager(tmp_path)
    stopping = threading.Event()
    def start():
        manager.starts += 1
        stopping.set()
    manager.start = start
    service.supervise(manager, stopping, interval=0)
    assert manager.starts == manager.stops == 1
    assert manager.reaps == 2
    assert json.loads((tmp_path/'control/service-status.json').read_text())['status'] == 'stopped'
    with operation_lock(tmp_path/'control/service-supervisor.lock'):
        with pytest.raises(ValueError, match=BUSY):
            service.supervise(manager, threading.Event(), interval=0)


def test_unexpected_start_failure_is_visible_without_exception_details(tmp_path):
    manager = Manager(tmp_path)
    def fail():
        raise ValueError('synthetic-sensitive-error')
    manager.start = fail
    with pytest.raises(ValueError):
        service.supervise(manager, threading.Event(), interval=0)
    text = (tmp_path/'control/service-status.json').read_text()
    assert 'attention-required' in text and 'synthetic-sensitive' not in text
    assert manager.reaps == 2


def test_system_supervisor_runs_backup_scheduler_and_joins_before_stop(tmp_path,monkeypatch):
    manager=Manager(tmp_path);stopping=threading.Event();events=[]
    def nightly(manager,stop,**kwargs):
        events.append('nightly');stop.wait(2);events.append('nightly-exited')
    monkeypatch.setattr(service,'nightly_loop',nightly,raising=False)
    def start():
        manager.starts+=1;events.append('started');stopping.set()
    def stop():
        events.append('stop');manager.running=None
    manager.start=start;manager.stop=stop
    service.supervise(manager,stopping,interval=0,system_identity={'uid':499})
    assert 'nightly' in events
    assert events.index('nightly-exited')<events.index('stop')


def test_system_timer_failure_is_sanitized_and_retries_without_stopping_erp(tmp_path,monkeypatch):
    from desktop_assistant import nightly,mac_system_service
    manager=Manager(tmp_path);waits=[];calls=[]
    class Stopping:
        stopped=False
        def is_set(self):return self.stopped
        def wait(self,delay):
            waits.append(delay)
            if len(waits)==2:self.stopped=True
    stopping=Stopping()
    monkeypatch.setattr(mac_system_service,'nightly_enabled',lambda:True)
    def run(manager,**kwargs):
        calls.append(1);assert kwargs['admission']() and kwargs['restart_allowed']()
        if len(calls)==1:raise ValueError('synthetic-secret-error')
        return {'status':'up-to-date','secret-field':'must-not-persist'}
    monkeypatch.setattr(nightly,'run_once',run)
    service.nightly_loop(manager,stopping,system_identity={'uid':499},interval=60,retry_interval=300)
    text=(tmp_path/'control/nightly-status.json').read_text()
    assert waits==[300,60] and manager.stops==manager.starts==0
    assert 'synthetic-secret' not in text and 'secret-field' not in text
    assert json.loads(text)['requires_user_login'] is False


def test_database_snapshot_failure_exposes_stale_alert_without_secret_or_stopping_erp(tmp_path, monkeypatch):
    from desktop_assistant import nightly, mac_system_service
    manager = Manager(tmp_path)
    manager.manifest = lambda: {'runtime_capabilities': {'data_recovery_v2': 1}}
    monkeypatch.setattr(mac_system_service, 'nightly_enabled', lambda: True)

    def failed_snapshot(_manager, **_kwargs):
        raise TimeoutError('synthetic private backup detail')

    monkeypatch.setattr(nightly, 'run_database_snapshot_once', failed_snapshot)
    monkeypatch.setattr(nightly, 'run_once', lambda *_args, **_kwargs: {'status': 'up-to-date'})

    class Stopping:
        stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, delay):
            assert delay == 300
            self.stopped = True

    stopping = Stopping()
    service.nightly_loop(manager, stopping, system_identity={'uid': 499})
    status = json.loads((tmp_path/'control/nightly-status.json').read_text())
    assert status['database_status'] == 'failed'
    assert status['database_backup_health']['stale'] is True
    assert status['database_backup_health']['alert_code'] == 'DATABASE_BACKUP_STALE'
    assert manager.starts == manager.stops == 0
    assert 'synthetic private backup detail' not in json.dumps(status)


def test_personal_supervisor_does_not_create_second_backup_timer(tmp_path,monkeypatch):
    manager=Manager(tmp_path);stopping=threading.Event()
    monkeypatch.setattr(service,'nightly_loop',lambda *a,**kw:pytest.fail('personal timer duplicated'))
    manager.start=lambda:stopping.set()
    service.supervise(manager,stopping,interval=0)
