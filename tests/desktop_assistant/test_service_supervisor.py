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


def test_backup_lock_prevents_restart_and_shutdown_does_not_force_kill(tmp_path):
    manager = Manager(tmp_path)
    manager.busy = True
    assert service.tick(manager) == 'maintenance-busy'
    with pytest.raises(ValueError, match=BUSY):
        service.stop_owned(manager, timeout=0)
    assert manager.starts == manager.stops == 0


def test_supervisor_shutdown_and_exclusive_ownership(tmp_path):
    manager = Manager(tmp_path)
    stopping = threading.Event()
    def start():
        manager.starts += 1
        stopping.set()
    manager.start = start
    service.supervise(manager, stopping, interval=0)
    assert manager.starts == manager.stops == 1
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
