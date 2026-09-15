import json
from urllib.request import Request
import pytest
from desktop_assistant.connectivity import inspect, SameOriginRedirect

CONFIG = {'ERP_PORT': '18000', 'ERP_LAN_HTTP_ORIGIN': 'http://192.168.3.80:8000'}


def good(url):
    return (200, 'application/json', json.dumps({'ok': True}).encode()) if url.endswith('/api/health') else (200, 'text/html', b'<!DOCTYPE html><html>ERP</html>')


def test_process_is_not_a_health_check():
    def inaccessible(url):
        if '127.0.0.1' in url:
            return good(url)
        raise OSError('private details must not appear')
    backend, page = inspect(CONFIG, True, inaccessible)
    assert backend == '后台正常' and '不可达' in page
    assert 'private' not in page


def test_normal_and_stopped():
    assert inspect(CONFIG, True, good) == ('后台正常', '网页可访问')
    assert inspect(CONFIG, False, lambda _: pytest.fail('must not request')) == ('后台已停止', '网页不可用')


def test_direct_lan_install_uses_actual_bind_address():
    config = dict(CONFIG, ERP_BIND_HOST='192.168.3.80', ERP_PORT='8000')
    calls = []
    def request(url):
        calls.append(url)
        return good(url)
    assert inspect(config, True, request) == ('后台正常', '网页可访问')
    assert all('127.0.0.1' not in url for url in calls)


@pytest.mark.parametrize('payload', [b'{}', b'not-json', b'{"ok":false}'])
def test_bad_backend_health(payload):
    assert '无响应' in inspect(CONFIG, True, lambda _: (200, 'application/json', payload))[0]


def test_health_success_does_not_hide_broken_page():
    assert '不可达' in inspect(CONFIG, True, lambda url: good(url) if url.endswith('/api/health') else (200, 'text/plain', b'error'))[1]


def test_cross_origin_or_http_to_https_redirect_is_not_success():
    handler = SameOriginRedirect()
    with pytest.raises(ValueError):
        handler.redirect_request(Request('http://192.168.3.80:8000/'), None, 307, '', {}, 'https://192.168.3.80:8000/')


def test_gui_checks_run_on_background_worker():
    from pathlib import Path
    code = Path('desktop_assistant/gui.py').read_text(encoding='utf-8')
    assert 'self.connection_check_running' in code
    assert 'threading.Thread(target=check, daemon=True).start()' in code
    assert "service = ('进程存在'" in code


@pytest.mark.parametrize('url', ['http://0.0.0.0:8000', 'http://8.8.8.8:8000', 'https://192.168.3.80:8000', 'http://127.0.0.1:8000'])
def test_repair_rejects_non_private_or_wildcard_origin(url, monkeypatch):
    import sys
    from unittest.mock import Mock
    from desktop_assistant.connectivity import repair_lan_forward
    monkeypatch.setitem(sys.modules, 'winreg', Mock())
    with pytest.raises(ValueError):
        repair_lan_forward(dict(CONFIG, ERP_LAN_HTTP_ORIGIN=url, ERP_BIND_HOST='127.0.0.1'))


def test_repair_preserves_unrelated_rule(monkeypatch):
    import sys
    from unittest.mock import MagicMock, Mock
    import desktop_assistant.connectivity as module
    registry = MagicMock()
    registry.QueryValueEx.return_value = ('127.0.0.1/9999', 1)
    monkeypatch.setitem(sys.modules, 'winreg', registry)
    monkeypatch.setattr(module.socket, 'socket', MagicMock())
    monkeypatch.setattr(module, 'health', lambda *a: True)
    run = Mock()
    monkeypatch.setattr(module.subprocess, 'run', run)
    with pytest.raises(ValueError, match='未覆盖'):
        module.repair_lan_forward(dict(CONFIG, ERP_BIND_HOST='127.0.0.1'))
    run.assert_not_called()


def test_repair_only_restores_exact_configured_endpoint(monkeypatch):
    import sys
    from unittest.mock import MagicMock, Mock
    import desktop_assistant.connectivity as module
    registry = MagicMock()
    registry.OpenKey.side_effect = FileNotFoundError()
    monkeypatch.setitem(sys.modules, 'winreg', registry)
    monkeypatch.setattr(module.socket, 'socket', MagicMock())
    monkeypatch.setattr(module, 'health', lambda *a: True)
    run = Mock(return_value=Mock(returncode=0))
    monkeypatch.setattr(module.subprocess, 'run', run)
    module.repair_lan_forward(dict(CONFIG, ERP_BIND_HOST='127.0.0.1'))
    command = run.call_args.args[0]
    assert 'listenaddress=192.168.3.80' in command and 'connectport=18000' in command
    assert 'reset' not in command and '0.0.0.0' not in command
