"""Slow signed startup must not be mistaken for a hung ERP process."""
from types import SimpleNamespace
from unittest.mock import Mock, MagicMock

import pytest

from desktop_assistant import manager as module
from desktop_assistant.storage import write_json


def startup(monkeypatch, tmp_path, *, ready_after=None, exit_after=None):
    manager = module.Manager(tmp_path / 'instance', b'synthetic-unused-public-key')
    write_json(manager.root / 'state.json', {'current': 'synthetic-current'})
    release = tmp_path / 'release'
    release.mkdir()
    clock = {'seconds': 0.0}
    proc = SimpleNamespace(pid=123456, poll=lambda: (
        1 if exit_after is not None and clock['seconds'] >= exit_after else None))
    monkeypatch.setattr(manager, '_runtime_python', lambda _: release / 'python')
    monkeypatch.setattr(manager, '_dispatch_reader', lambda _: False)
    monkeypatch.setattr(manager, '_check_dispatch_reader', lambda *a, **k: None)
    monkeypatch.setattr(manager, '_check_provenance_reader', lambda _: None)
    monkeypatch.setattr(manager, '_process', lambda: None)
    monkeypatch.setattr(manager, '_execution_release', lambda _: release)
    monkeypatch.setattr(manager, '_link_data', lambda _: None)
    monkeypatch.setattr(manager, '_environment', lambda _: {'ERP_PORT': '8000'})
    monkeypatch.setattr(manager, 'compatible', lambda *a: True)
    monkeypatch.setattr(manager, '_track_child', lambda _: None)
    stop = Mock()
    monkeypatch.setattr(manager, 'stop', stop)
    monkeypatch.setattr(module, 'database_info', lambda _: {'revision': 'synthetic'})
    monkeypatch.setattr(module.uuid, 'uuid4', lambda: SimpleNamespace(hex='synthetic-start-nonce'))
    monkeypatch.setattr(module.subprocess, 'Popen', lambda *a, **k: proc)
    monkeypatch.setattr(module.psutil, 'Process', lambda _: SimpleNamespace(create_time=lambda: 1))
    socket = MagicMock()
    socket.__enter__.return_value.connect_ex.return_value = 1
    monkeypatch.setattr(module.socket, 'socket', lambda: socket)
    from desktop_assistant import mac_published_runtime, home_process
    monkeypatch.setattr(mac_published_runtime, 'selected', lambda: False)
    monkeypatch.setattr(home_process, 'command', lambda args, env: args)
    # An unrelated old ready file must never count as this startup's proof.
    (manager.root / 'control' / 'old-nonce.ready').write_text('old')

    def sleep(seconds):
        clock['seconds'] += seconds
        if ready_after is not None and clock['seconds'] >= ready_after:
            (manager.root / 'control' / 'synthetic-start-nonce.ready').write_text('ready')

    monkeypatch.setattr(module.time, 'sleep', sleep)
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock['seconds'])
    return manager, clock, stop


@pytest.mark.parametrize('ready_after', [0.5, 75, 299])
def test_ready_after_signature_initialization_keeps_service(monkeypatch, tmp_path, ready_after):
    manager, clock, stop = startup(monkeypatch, tmp_path, ready_after=ready_after)
    manager.start()
    assert clock['seconds'] == ready_after
    stop.assert_not_called()


def test_hung_startup_still_has_a_finite_deadline(monkeypatch, tmp_path):
    manager, clock, stop = startup(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match='启动超时'):
        manager.start()
    assert clock['seconds'] == 300
    stop.assert_called_once()


def test_early_exit_reports_failure_without_waiting_for_deadline(monkeypatch, tmp_path):
    manager, clock, stop = startup(monkeypatch, tmp_path, exit_after=2)
    with pytest.raises(ValueError, match='启动失败'):
        manager.start()
    assert clock['seconds'] == 2
    stop.assert_not_called()
