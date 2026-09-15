import json
from unittest.mock import Mock, patch
import pytest
from desktop_assistant.gui import App


@pytest.mark.parametrize('lan,expected', [
    ('http://192.168.3.80:8000','http://192.168.3.80:8000/'),
    ('http://192.168.3.80:8000/','http://192.168.3.80:8000/'),
    ('','https://example.com/'),
])
def test_open_uses_lan_without_rewriting_https_service_contract(tmp_path,lan,expected):
    shared=tmp_path/'shared';shared.mkdir()
    path=shared/'environment.json'
    config={'ERP_PORT':'18000','ERP_PRODUCTION_TRANSPORT':'https_proxy',
            'ERP_HEALTH_URL':'https://example.com/api/health','ERP_BROWSER_URL':'https://example.com/',
            'ERP_LAN_HTTP_ORIGIN':lan,'ERP_SESSION_COOKIE_SECURE':'true'}
    path.write_text(json.dumps(config));original=path.read_bytes()
    app=Mock();app.manager.root=tmp_path;app.run.side_effect=lambda label,action:action()
    with patch('desktop_assistant.gui.webbrowser.open') as opened, patch('desktop_assistant.gui.inspect_connectivity', return_value=('后台正常','网页可访问')):
        App.open_erp(app)
    app.manager.resume.assert_called_once()
    opened.assert_called_once_with(expected)
    assert path.read_bytes()==original


def test_inaccessible_page_is_not_opened(tmp_path):
    shared=tmp_path/'shared';shared.mkdir()
    (shared/'environment.json').write_text(json.dumps({'ERP_PORT':'18000'}))
    app=Mock();app.manager.root=tmp_path;app.run.side_effect=lambda label,action:action()
    with patch('desktop_assistant.gui.webbrowser.open') as opened, patch('desktop_assistant.gui.inspect_connectivity', return_value=('后台正常','网页入口不可达')):
        with pytest.raises(ValueError,match='不可达'):App.open_erp(app)
        opened.assert_not_called()


def test_start_failure_does_not_open_browser(tmp_path):
    app=Mock();app.manager.root=tmp_path;app.manager.resume.side_effect=ValueError('start failed')
    app.run.side_effect=lambda label,action:action()
    with patch('desktop_assistant.gui.webbrowser.open') as opened:
        with pytest.raises(ValueError,match='start failed'):App.open_erp(app)
        opened.assert_not_called()
