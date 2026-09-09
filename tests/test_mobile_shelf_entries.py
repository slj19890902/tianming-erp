import ast
from pathlib import Path
from types import SimpleNamespace
from fastapi import Request
from fastapi.responses import FileResponse, RedirectResponse

ROOT = Path(__file__).resolve().parents[1]


def function(name):
    tree=ast.parse((ROOT/"app/main.py").read_text(encoding="utf-8"))
    node=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name==name)
    code=ast.Module(body=[node],type_ignores=[])
    ns=dict(Request=Request,Path=Path,FileResponse=FileResponse,RedirectResponse=RedirectResponse,
            __file__=str(ROOT/"app/main.py"),warehouse_twin_path=ROOT/"static/factory-twin-assets/warehouse-twin.html")
    exec(compile(ast.fix_missing_locations(code),"<entry>","exec"),ns)
    return ns[name]


def test_short_qr_serves_mobile_shell_without_redirect_or_inventory():
    response=function("shelf_scan_entry")(1884,"a"*24)
    assert response.status_code==200
    assert str(response.path).endswith("shelf-scan.html")
    assert response.headers["cache-control"]=="no-store"
    shell=Path(response.path).read_text(encoding="utf-8")
    assert 'viewport' in shell and '/static/shelf-scan.js' in shell
    assert "warehouseTwin" not in shell


def test_old_phone_position_qr_redirect_but_desktop_editor_stays():
    entry=function("warehouse_entry")
    for agent,tab,expected in [("iPhone","locations",307),("desktop","locations",200),("iPhone","planning",200)]:
        request=SimpleNamespace(query_params=dict(location_id="1884",tab=tab),headers={"user-agent":agent})
        response=entry(request)
        assert response.status_code==expected
        if expected==307:
            assert response.headers["location"]=="/static/shelf-scan.html?location_id=1884"
