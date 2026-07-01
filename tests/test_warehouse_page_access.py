from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
WAREHOUSE_HTML = (ROOT / "static" / "warehouse.html").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def request_pages(*paths: str) -> list[dict]:
    code = """
import json
from fastapi.testclient import TestClient
from app.main import app
paths = json.loads(%r)
with TestClient(app) as client:
    rows = []
    for path in paths:
        response = client.get(path, follow_redirects=False)
        rows.append({
            "path": path,
            "status": response.status_code,
            "content_type": response.headers.get("content-type", ""),
            "text": response.text,
        })
print(json.dumps(rows, ensure_ascii=False))
""" % json.dumps(paths)
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_warehouse_page_route_returns_warehouse_html_not_dashboard() -> None:
    response = request_pages("/warehouse.html")[0]
    assert response["status"] == 200
    assert "仓库库存管理 - 天明ERP" in response["text"]
    assert 'id="inventorySection"' in response["text"]
    assert "首页仪表盘" not in response["text"]
    assert response["content_type"].startswith("text/html")


def test_homepage_has_role_menu_and_direct_link_to_warehouse_page() -> None:
    assert '{ key: "warehouse", label: "仓库库存管理" }' in INDEX_HTML
    assert 'href="/warehouse.html">仓库库存管理</a>' in INDEX_HTML
    assert 'if (page === "warehouse")' in INDEX_HTML
    assert 'window.location.href = "/warehouse.html";' in INDEX_HTML
    assert '"incoming", "warehouse", "deliveries"' in INDEX_HTML


def test_warehouse_page_reads_nested_auth_user_and_allows_admin() -> None:
    assert "state.user=authResponse.user;" in WAREHOUSE_HTML
    assert '["admin","workshop"].includes(state.user.role)' in WAREHOUSE_HTML
    assert 'state.user=await api("/api/auth/me")' not in WAREHOUSE_HTML
    assert 'if(!["admin","workshop"].includes(state.user.role)){location.href="/"' not in WAREHOUSE_HTML
    assert "当前账号没有仓库库存管理权限，请联系管理员。" in WAREHOUSE_HTML


def test_warehouse_page_unauthenticated_and_initialization_failures_are_explicit() -> None:
    assert 'if(error.status===401){location.href="/?next=%2Fwarehouse.html";return}' in WAREHOUSE_HTML
    assert "登录状态检查失败" in WAREHOUSE_HTML
    assert "仓库页面初始化失败" in WAREHOUSE_HTML
    assert 'id="pageError"' in WAREHOUSE_HTML


def test_warehouse_page_has_required_sections_and_no_missing_assets() -> None:
    for label in ("成品仓", "半成品仓", "库位管理", "库存流水"):
        assert label in WAREHOUSE_HTML
    assert "<script src=" not in WAREHOUSE_HTML
    assert "<link rel=" not in WAREHOUSE_HTML
    assert 'href="/"' in WAREHOUSE_HTML


def test_existing_home_and_incoming_pages_remain_served() -> None:
    home, incoming = request_pages("/", "/incoming.html")
    assert home["status"] == 200
    assert incoming["status"] == 200
    assert "/api/incoming/pending" in incoming["text"]
