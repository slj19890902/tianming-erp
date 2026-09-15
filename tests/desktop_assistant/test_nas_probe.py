from unittest.mock import Mock
import subprocess
import pytest
from desktop_assistant.nas_probe import probe, check_before_stop


def test_probe_only_uses_existing_directory(tmp_path):
    assert probe(tmp_path / 'missing') == 2
    assert not (tmp_path / 'missing').exists()
    assert probe(tmp_path) == 0
    assert list(tmp_path.iterdir()) == []


def test_timeout_is_safe_and_does_not_show_secrets(monkeypatch):
    monkeypatch.setattr(subprocess, 'run', Mock(side_effect=subprocess.TimeoutExpired(['private'], 20)))
    with pytest.raises(ValueError, match='保持原运行状态') as result:
        check_before_stop('Z:/backup')
    assert 'private' not in str(result.value)


def test_backup_does_not_stop_when_nas_preflight_fails(monkeypatch):
    from contextlib import nullcontext
    from desktop_assistant.manager import Manager
    monkeypatch.setattr('desktop_assistant.nas_probe.check_before_stop', Mock(side_effect=ValueError('NAS unavailable')))
    manager = Mock()
    manager.lock.return_value = nullcontext()
    with pytest.raises(ValueError):
        Manager.backup(manager, 'unused', 'Z:/backup')
    manager.stop.assert_not_called()
    manager._backup_stopped.assert_not_called()
