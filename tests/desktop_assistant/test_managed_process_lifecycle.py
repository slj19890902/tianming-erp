"""Disposable processes only; never load an ERP release or business database."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import psutil
import pytest

from desktop_assistant.manager import Manager
from desktop_assistant import manager as module
from desktop_assistant.storage import write_json


UNREAPED_PARENT = r'''
import json
from pathlib import Path
import subprocess
import sys
import psutil

root = Path(sys.argv[1])
nonce = 'synthetic-graceful-stop'
child = subprocess.Popen([sys.executable, '-c',
    'import sys,time; from pathlib import Path; marker=Path(sys.argv[1]); '
    'deadline=time.monotonic()+90\n'
    'while not marker.exists() and time.monotonic()<deadline: time.sleep(.01)',
    str(root / 'control' / (nonce + '.stop'))])
identity = psutil.Process(child.pid)
print(json.dumps({'pid': child.pid, 'created': identity.create_time(),
                  'exe': identity.exe(), 'nonce': nonce}), flush=True)
# Retain the Popen without polling until the independent manager has finished.
sys.stdin.readline()
(root / 'control' / (nonce + '.stop')).touch()
print(json.dumps({'reaped_exit': child.wait(timeout=10)}), flush=True)
'''


@contextmanager
def unreaped_child(manager):
    parent = subprocess.Popen([sys.executable, '-c', UNREAPED_PARENT, str(manager.root)],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True)
    try:
        record = json.loads(parent.stdout.readline())
        write_json(manager.root / 'control/process.json', record)
        yield parent, record
    finally:
        output, errors = parent.communicate('reap\n', timeout=15)
        assert parent.returncode == 0, errors
        assert json.loads(output)['reaped_exit'] == 0


@pytest.mark.skipif(os.name == 'nt', reason='POSIX unreaped zombie process semantics')
def test_non_parent_stop_accepts_exit_before_parent_reaps(tmp_path):
    manager = Manager(tmp_path.resolve(), b'synthetic-public-key')
    with unreaped_child(manager) as (parent, record):
        process = psutil.Process(record['pid'])
        assert process.ppid() == parent.pid
        started = time.monotonic()
        try:
            manager.stop()
        except Exception:
            # This reports only the disposable PID state, including on baseline failure.
            print({'synthetic_child_status': process.status(),
                   'parent_still_running': parent.poll() is None,
                   'elapsed_seconds': round(time.monotonic() - started, 2)})
            raise
        assert time.monotonic() - started < 5
        assert process.status() == psutil.STATUS_ZOMBIE
        assert parent.poll() is None
        assert manager._process() is None
    assert not psutil.pid_exists(record['pid'])


@pytest.fixture
def process_case(tmp_path, monkeypatch):
    """Exercise the shared psutil contract on every platform, including Windows."""
    manager = Manager(tmp_path.resolve(), b'synthetic-public-key')
    record = {'pid': 987654, 'created': 10., 'exe': str(tmp_path / 'owned-python'),
              'nonce': 'original-nonce'}
    write_json(manager.root / 'control/process.json', record)
    case = SimpleNamespace(manager=manager, record=record, now=0., present=True,
                           created=record['created'], exe=record['exe'],
                           status=psutil.STATUS_RUNNING, errors={}, waits=[])
    def field(name, value):
        if name in case.errors:
            raise case.errors[name]
        return value
    def wait(timeout):
        assert (manager.root / 'control/original-nonce.stop').is_file()
        case.waits.append(timeout)
        case.now += timeout
        field('wait', None)
        raise psutil.TimeoutExpired(timeout, pid=record['pid'])
    process = SimpleNamespace(
        create_time=lambda: field('create_time', case.created),
        exe=lambda: field('exe', case.exe), status=lambda: field('status', case.status),
        wait=wait,
        kill=lambda: pytest.fail('stop must not kill'),
        terminate=lambda: pytest.fail('stop must not terminate'),
        send_signal=lambda *_: pytest.fail('stop must not send signals'))
    def lookup(pid):
        assert pid == record['pid']
        field('lookup', None)
        if not case.present:
            raise psutil.NoSuchProcess(pid)
        return process
    case.process = process
    monkeypatch.setattr(module.psutil, 'Process', lookup)
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: case.now))
    return case


@pytest.mark.parametrize('status', [psutil.STATUS_RUNNING, psutil.STATUS_STOPPED])
def test_alive_or_suspended_process_keeps_total_sixty_second_deadline(process_case, status):
    case = process_case
    case.status = status
    with pytest.raises(ValueError, match='未强杀'):
        case.manager.stop()
    assert case.now == 60
    assert case.waits and max(case.waits) <= .25
    assert case.manager._process() is not None


@pytest.mark.parametrize('transition', ['zombie', 'disappeared', 'pid-reused'])
def test_stop_waits_for_original_identity_and_accepts_its_exit(process_case, transition):
    case = process_case
    original_wait = case.process.wait
    def wait(timeout):
        if transition == 'zombie':
            case.status = psutil.STATUS_ZOMBIE
        elif transition == 'disappeared':
            case.present = False
        else:
            case.created += 10
        return original_wait(timeout)
    case.process.wait = wait
    case.manager.stop()
    case.manager.stop()  # A stale process record is harmless; it is retained.
    assert case.waits == [.25]
    assert json.loads((case.manager.root / 'control/process.json').read_text()) == case.record


@pytest.mark.parametrize('exit_code', [0, None], ids=['child-or-windows-exit', 'non-child-exit'])
def test_wait_completion_is_success_on_shared_posix_and_windows_path(process_case, exit_code):
    case = process_case
    case.process.wait = lambda timeout: exit_code
    case.manager.stop()
    assert (case.manager.root / 'control/original-nonce.stop').is_file()


@pytest.mark.parametrize('field', ['lookup', 'create_time', 'exe', 'status', 'wait'])
def test_permission_denial_is_never_treated_as_exit(process_case, field):
    case = process_case
    case.errors[field] = psutil.AccessDenied(case.record['pid'])
    with pytest.raises(psutil.AccessDenied):
        case.manager.stop()
    assert case.now < 1


@pytest.mark.parametrize('failure', ['exe-changed', 'permission-denied'])
def test_identity_and_permissions_are_rechecked_during_wait(process_case, failure):
    case = process_case
    original_wait = case.process.wait
    def wait(timeout):
        if failure == 'exe-changed':
            case.exe = str(case.manager.root / 'other-python')
        else:
            case.errors['status'] = psutil.AccessDenied(case.record['pid'])
        return original_wait(timeout)
    case.process.wait = wait
    with pytest.raises(ValueError if failure == 'exe-changed' else psutil.AccessDenied):
        case.manager.stop()
    assert case.waits == [.25]


def test_live_executable_mismatch_refuses_before_stop_marker(process_case):
    case = process_case
    case.exe = str(case.manager.root / 'other-python')
    with pytest.raises(ValueError, match='身份不匹配'):
        case.manager.stop()
    assert not (case.manager.root / 'control/original-nonce.stop').exists()
    assert not case.waits


@pytest.mark.parametrize('state', ['missing', 'pid-reused', 'zombie', 'zombie-exe', 'zombie-empty-exe'])
def test_already_exited_original_does_not_write_a_stop_marker(process_case, state):
    case = process_case
    if state == 'missing':
        case.present = False
    elif state == 'pid-reused':
        case.created += 10
    elif state in ('zombie', 'zombie-empty-exe'):
        case.status = psutil.STATUS_ZOMBIE
        if state == 'zombie-empty-exe':
            case.exe = ''
    else:
        case.errors['exe'] = psutil.ZombieProcess(case.record['pid'])
    assert case.manager._process() is None
    case.manager.stop()
    assert not (case.manager.root / 'control/original-nonce.stop').exists()
    assert not case.waits


@pytest.mark.parametrize('publication_fails', [False, True])
def test_start_tracks_child_even_if_process_record_publication_fails(tmp_path, monkeypatch,
                                                                   publication_fails):
    from desktop_assistant import home_process, mac_published_runtime
    manager = Manager(tmp_path.resolve(), b'synthetic-public-key')
    write_json(manager.root / 'state.json', {'current': 'synthetic-release'})
    monkeypatch.setattr(mac_published_runtime, 'selected', lambda: False)
    monkeypatch.setattr(manager, '_runtime_python', lambda *_: Path(sys.executable))
    monkeypatch.setattr(manager, '_execution_release', lambda *_: manager.root)
    monkeypatch.setattr(manager, '_dispatch_reader', lambda *_: False)
    monkeypatch.setattr(manager, '_check_dispatch_reader', lambda *a, **kw: None)
    monkeypatch.setattr(manager, '_check_provenance_reader', lambda *_: None)
    monkeypatch.setattr(manager, '_link_data', lambda *_: None)
    monkeypatch.setattr(manager, 'compatible', lambda *_: True)
    monkeypatch.setattr(manager, '_environment', lambda *_: {'ERP_PORT': '8000'})
    monkeypatch.setattr(module, 'database_info', lambda *_: {'revision': 'synthetic'})
    monkeypatch.setattr(home_process, 'command', lambda arguments, env: arguments)
    @contextmanager
    def socket():
        yield SimpleNamespace(connect_ex=lambda *_: 1)
    monkeypatch.setattr(module, 'socket', SimpleNamespace(socket=socket))
    child = SimpleNamespace(pid=987654, poll=Mock(return_value=None))
    def popen(*_, **kwargs):
        assert kwargs['env']['TM_ERP_CONTROL'] == str(manager.root / 'control')
        nonce = kwargs['env']['TM_ERP_NONCE']
        (manager.root / 'control' / (nonce + '.ready')).touch()
        return child
    monkeypatch.setattr(module, 'subprocess', SimpleNamespace(Popen=popen))
    monkeypatch.setattr(module.psutil, 'Process',
                        lambda _: SimpleNamespace(create_time=lambda: 10.))
    if publication_fails:
        def fail(*_):
            raise ValueError('synthetic process record publication failure')
        monkeypatch.setattr(module, 'write_json', fail)
        with pytest.raises(ValueError, match='publication failure'):
            manager.start()
    else:
        manager.start()
        assert json.loads((manager.root / 'control/process.json').read_text())['pid'] == child.pid
    assert manager._owned_children == [child]
    child.poll.return_value = 0
    manager.reap_owned_children()
    assert not manager._owned_children


def test_stop_keeps_original_nonce_and_identity_if_record_is_replaced(process_case):
    case = process_case
    original_wait = case.process.wait
    def wait(timeout):
        write_json(case.manager.root / 'control/process.json',
                   {**case.record, 'pid': 123, 'nonce': 'replacement-nonce'})
        case.present = False
        return original_wait(timeout)
    case.process.wait = wait
    case.manager.stop()
    assert case.waits == [.25]
    assert not (case.manager.root / 'control/replacement-nonce.stop').exists()


def test_parent_reaps_only_tracked_popen_and_gracefully_stops_on_all_platforms(tmp_path):
    manager = Manager(tmp_path.resolve(), b'synthetic-public-key')
    marker = manager.root / 'control/synthetic.stop'
    child_code = ('import sys,time; from pathlib import Path; marker=Path(sys.argv[1]); '
                  'deadline=time.monotonic()+15\n'
                  'while not marker.exists() and time.monotonic()<deadline: time.sleep(.01)')
    owned = subprocess.Popen([sys.executable, '-c', child_code, str(marker)])
    unrelated = subprocess.Popen([sys.executable, '-c', child_code, str(marker)])
    try:
        manager._track_child(owned)
        process = psutil.Process(owned.pid)
        write_json(manager.root / 'control/process.json', {
            'pid': owned.pid, 'created': process.create_time(),
            'exe': process.exe(), 'nonce': 'synthetic'})
        manager.stop()
        assert owned.returncode == 0
        assert manager._owned_children == []
        assert unrelated.returncode is None
        assert manager._process() is None
    finally:
        marker.touch()
        assert owned.wait(timeout=10) == 0
        assert unrelated.wait(timeout=10) == 0


def test_tracking_from_nightly_and_supervisor_reaping_cannot_lose_a_child(tmp_path):
    manager = Manager(tmp_path.resolve(), b'synthetic-public-key')
    entered, release = threading.Event(), threading.Event()
    def poll():
        entered.set()
        assert release.wait(5)
        return 0
    exited = SimpleNamespace(poll=poll)
    new_child = SimpleNamespace(poll=Mock(return_value=None))
    manager._track_child(exited)
    reaper = threading.Thread(target=manager.reap_owned_children)
    tracker = threading.Thread(target=manager._track_child, args=(new_child,))
    reaper.start()
    try:
        assert entered.wait(5)
        tracker.start()
    finally:
        release.set()
        reaper.join(5)
        if tracker.ident is not None:
            tracker.join(5)
    assert not reaper.is_alive() and not tracker.is_alive()
    assert manager._owned_children == [new_child]
    manager.reap_owned_children()
    assert manager._owned_children == [new_child]
    new_child.poll.return_value = 0
    manager.reap_owned_children()
    assert manager._owned_children == []
