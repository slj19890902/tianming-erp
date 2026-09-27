import ast
from pathlib import Path
from types import SimpleNamespace
import re
from urllib.parse import urlencode
from fastapi import Request
from fastapi.responses import FileResponse, RedirectResponse, HTMLResponse

ROOT = Path(__file__).resolve().parents[1]


def function(name):
    tree=ast.parse((ROOT/"app/main.py").read_text(encoding="utf-8"))
    node=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name==name)
    code=ast.Module(body=[node],type_ignores=[])
    ns=dict(Request=Request,Path=Path,FileResponse=FileResponse,RedirectResponse=RedirectResponse,HTMLResponse=HTMLResponse,
            re=re,urlencode=urlencode,
            __file__=str(ROOT/"app/main.py"),warehouse_twin_path=ROOT/"static/factory-twin-assets/warehouse-twin.html")
    exec(compile(ast.fix_missing_locations(code),"<entry>","exec"),ns)
    return ns[name]


def test_short_qr_serves_mobile_shell_without_redirect_or_inventory():
    response=function("shelf_scan_entry")(SimpleNamespace(url=SimpleNamespace(hostname="localhost")),1884,"a"*24)
    assert response.status_code==200
    assert response.headers["cache-control"]=="no-store"
    shell=response.body.decode()
    assert 'viewport' in shell and 'function productCard(' in shell
    assert '<script src=' not in shell
    assert 'setInterval(' not in shell
    assert 'return_scan=1' in shell
    assert "warehouseTwin" not in shell


def test_old_phone_position_qr_redirect_but_desktop_editor_stays():
    entry=function("warehouse_entry")
    for agent,tab,expected in [("iPhone","locations",307),("desktop","locations",200),("iPhone","planning",200)]:
        request=SimpleNamespace(query_params=dict(location_id="1884",tab=tab),headers={"user-agent":agent},url=SimpleNamespace(hostname="localhost"))
        response=entry(request)
        assert response.status_code==expected
        if expected==307:
            assert response.headers["location"]=="/static/shelf-scan.html?location_id=1884"


def test_old_phone_rack_qr_redirects_but_desktop_map_stays():
    entry=function("warehouse_entry")
    mobile=SimpleNamespace(query_params=dict(floor="3F",rack_id="rack-A"),headers={"user-agent":"iPhone MicroMessenger"},url=SimpleNamespace(hostname="localhost"))
    response=entry(mobile)
    assert response.status_code==302
    assert response.headers["location"]=="/scan/rack?floor=3F&rack_id=rack-A"
    desktop=SimpleNamespace(query_params=dict(floor="3F",rack_id="rack-A"),headers={"user-agent":"Windows desktop"},url=SimpleNamespace(hostname="localhost"))
    assert entry(desktop).status_code==200


def test_camera_shell_has_user_gesture_and_local_decoder():
    response=function("mobile_camera_scan_entry")()
    assert response.status_code == 200
    shell=response.body.decode()
    assert 'id="cameraStart"' in shell and 'getUserMedia' in shell
    assert "'/static/vendor/jsqr/jsQR-1.4.0.js'" in shell
    assert 'audio:false' in shell
    assert 'id="cameraPhoto"' in shell
    assert 'URL.revokeObjectURL' in shell
    assert 'camera=(self)' in (ROOT/'app/main.py').read_text(encoding='utf8')


def test_legacy_external_qr_redirect_is_not_cached(monkeypatch):
    monkeypatch.setenv('ERP_SHELF_LABEL_ORIGIN', 'http://172.16.1.26:8000')
    request = SimpleNamespace(url=SimpleNamespace(hostname='tianmingerp0909.share.zrok.io'))
    response = function('shelf_scan_entry')(request,1203,'a'*24)
    assert response.status_code == 302
    assert response.headers['location'] == 'http://172.16.1.26:8000/q/1203/'+'a'*24
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['referrer-policy'] == 'no-referrer'


def test_camera_policy_is_limited_to_scan_entry():
    import asyncio
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import Response
    tree=ast.parse((ROOT/'app/main.py').read_text(encoding='utf8'))
    node=next(n for n in ast.walk(tree) if isinstance(n,ast.ClassDef) and n.name=='HSTSMiddleware')
    ns={'BaseHTTPMiddleware':BaseHTTPMiddleware,'is_lan_http_scope':lambda *a:False}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node],type_ignores=[])),'<policy>','exec'),ns)
    middleware=ns['HSTSMiddleware'](None,include_hsts=False)
    async def response(_):return Response()
    for path,expected in [('/mobile/scan','camera=(self)'),('/mobile/','camera=()'),('/q/1884','camera=()'),('/static/shelf-scan.html','camera=()')]:
        request=SimpleNamespace(url=SimpleNamespace(path=path),query_params={},scope={})
        result=asyncio.run(middleware.dispatch(request,response))
        assert result.headers['permissions-policy']==expected+', microphone=(), geolocation=()'
