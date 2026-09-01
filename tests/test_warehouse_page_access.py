from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker


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


def test_warehouse_page_route_returns_new_digital_twin_and_keeps_ledger_separate() -> None:
    twin, ledger = request_pages("/warehouse.html", "/warehouse-ledger.html")
    assert twin["status"] == 200
    assert "天明智慧仓储 - 天明ERP" in twin["text"]
    assert 'id="warehouse-twin-root"' in twin["text"]
    assert "/factory-twin-assets/assets/" in twin["text"]
    assert twin["content_type"].startswith("text/html")
    assert ledger["status"] == 200
    assert "仓库库存管理 - 天明ERP" in ledger["text"]
    assert 'id="inventorySection"' in ledger["text"]


def test_homepage_keeps_warehouse_inside_lazy_persistent_erp_shell() -> None:
    assert 'key: "workbench", label: "订单主链"' in INDEX_HTML
    assert 'v-if="pageAllowed(\'warehouse\')"' in INDEX_HTML
    assert '@click="goMenu(\'warehouse\')"' in INDEX_HTML
    assert 'v-if="warehouseFrameUrl" v-show="activePage === \'warehouse\'"' in INDEX_HTML
    assert 'warehouseFrameUrl: "", warehouseFrameRevision: 0, warehouseTwinFloor: "3F"' in INDEX_HTML
    assert 'this.warehouseFrameUrl = "/warehouse.html?embedded=1&floor=3F&view=2d"' in INDEX_HTML
    assert "默认先看真实三楼地图；切换其它模块不会丢失当前仓库页面。" not in INDEX_HTML
    assert "warehouse-floor-card" not in INDEX_HTML
    assert 'window.location.href = "/warehouse.html";' not in INDEX_HTML
    assert 'this.warehouseFrameUrl = "";' in INDEX_HTML
    assert '"incoming", "production", "warehouse", "deliveries"' in INDEX_HTML


def test_warehouse_page_reads_nested_auth_user_and_uses_n028_permissions() -> None:
    assert "state.user=authResponse.user;" in WAREHOUSE_HTML
    assert "state.permissions=authResponse.permissions||[];" in WAREHOUSE_HTML
    assert 'hasPermission("warehouse.view")' in WAREHOUSE_HTML
    assert 'hasPermission("warehouse.execute")' in WAREHOUSE_HTML
    assert 'state.user=await api("/api/auth/me")' not in WAREHOUSE_HTML
    assert 'if(!["admin","workshop"].includes(state.user.role)){location.href="/"' not in WAREHOUSE_HTML
    assert "当前账号没有仓库库存管理权限，请联系管理员。" in WAREHOUSE_HTML


def test_warehouse_shell_uses_the_main_topbar_without_a_second_banner() -> None:
    assert 'class="warehouse-top-shortcuts"' in INDEX_HTML
    assert "1F 生产车间" in INDEX_HTML
    assert "3F 成品仓库" in INDEX_HTML
    assert "打印货位编号" not in INDEX_HTML
    assert 'data-tab="location_labels">货架与货位标签' in WAREHOUSE_HTML
    assert 'href="/warehouse-ledger.html?tab=finished">库存台账</a>' in INDEX_HTML
    assert 'class="warehouse-shell-head"' not in INDEX_HTML
    assert "新增或编辑生产模具，请进入模具档案" not in INDEX_HTML
    assert 'requestedTab&&["finished","semi_finished","molds"].includes(requestedTab)' in WAREHOUSE_HTML


def test_warehouse_page_unauthenticated_and_initialization_failures_are_explicit() -> None:
    assert 'const loginTarget=warehouseEmbeddedMode?"/?page=warehouse":"/?redirect=%2Fwarehouse.html";' in WAREHOUSE_HTML
    assert "window.top.location.replace(loginTarget)" in WAREHOUSE_HTML
    assert "window.location.replace(loginTarget)" in WAREHOUSE_HTML
    assert "登录状态检查失败" in WAREHOUSE_HTML
    assert "仓库页面初始化失败" in WAREHOUSE_HTML
    assert "库位加载失败" in WAREHOUSE_HTML
    assert "客户资料加载失败" in WAREHOUSE_HTML
    assert "库存批次加载失败" in WAREHOUSE_HTML
    assert "apiErrorMessage" in WAREHOUSE_HTML
    assert 'id="pageError"' in WAREHOUSE_HTML


def test_warehouse_page_has_required_sections_and_no_missing_assets() -> None:
    for label in ("成品仓", "半成品仓", "库存经营看板", "实测仓库地图", "库存流水"):
        assert label in WAREHOUSE_HTML
    assert '<script src="/static/assets/time-utils.js?v=95c91f9ded41"></script>' in WAREHOUSE_HTML
    assert WAREHOUSE_HTML.count("<script src=") == 1
    assert "<link rel=" not in WAREHOUSE_HTML
    assert 'href="/"' in WAREHOUSE_HTML
    assert 'body.embedded>header{display:none}' in WAREHOUSE_HTML
    assert 'const warehouseEmbeddedMode = warehouseSearchParams.get("embedded") === "1";' in WAREHOUSE_HTML
    for empty_text in ("暂无库存批次", "暂无库位", "暂无库存流水"):
        assert empty_text in WAREHOUSE_HTML
    assert "/api/warehouse/insights" in WAREHOUSE_HTML
    assert "成本待补" in WAREHOUSE_HTML
    assert "当前材料估算" in WAREHOUSE_HTML
    assert "库龄按入库日期，停滞按最后异动" in WAREHOUSE_HTML
    assert "只读展示，不自动抵扣" in WAREHOUSE_HTML
    assert "入库快照估算" in WAREHOUSE_HTML
    assert "当前材料报价估算" in WAREHOUSE_HTML
    assert "产品参考估算" in WAREHOUSE_HTML
    assert "绝非实际现金成本覆盖" in WAREHOUSE_HTML
    assert "建议不自动执行" in WAREHOUSE_HTML


def test_warehouse_initialization_apis_return_empty_structures(tmp_path: Path) -> None:
    from app.api import warehouse
    from app.api.deps import get_db
    from app.core.database import create_sqlite_engine
    from app.models import Base
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "empty-warehouse.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        admin = User(
            username="warehouse-admin",
            password_hash="test",
            role="admin",
            real_name="仓库管理员",
            must_change_password=False,
        )
        db.add(admin)
        db.commit()
        admin_id = admin.id

    app = FastAPI()
    app.include_router(warehouse.router, prefix="/api/warehouse")

    def override_get_db():
        with factory() as db:
            yield db

    def override_can_read():
        with factory() as db:
            return db.get(User, admin_id)

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[warehouse.can_read] = override_can_read
    with TestClient(app) as client:
        locations = client.get("/api/warehouse/locations")
        lots = client.get(
            "/api/warehouse/lots",
            params={"inventory_type": "finished", "page_size": 200},
        )
        movements = client.get(
            "/api/warehouse/movements",
            params={"page_size": 200},
        )
        insights = client.get("/api/warehouse/insights")

    assert locations.status_code == 200
    assert locations.json() == {"items": []}
    assert lots.status_code == 200
    assert lots.json()["items"] == []
    assert lots.json()["total"] == 0
    assert movements.status_code == 200
    assert movements.json()["items"] == []
    assert movements.json()["total"] == 0
    assert insights.status_code == 200
    assert insights.json()["summary"]["recorded_lots"] == 0
    assert insights.json()["summary"]["actual_inventory_value"] is None


def test_warehouse_filters_do_not_send_empty_integer_query_values() -> None:
    assert "function queryString(values)" in WAREHOUSE_HTML
    assert 'location_id:$("locationFilter").value' in WAREHOUSE_HTML
    assert 'if(value!==""&&value!==null&&value!==undefined)' in WAREHOUSE_HTML
    assert "new URLSearchParams({inventory_type:state.tab" not in WAREHOUSE_HTML


def test_existing_home_and_incoming_pages_remain_served() -> None:
    home, incoming, mold_mobile, mold_label, location_label, rack_level_label = request_pages(
        "/",
        "/incoming.html",
        "/mobile/mold-lookup",
        "/mold-label.html",
        "/location-label.html?location_id=1",
        "/warehouse-rack-level-label.html?print_job_id=1",
    )
    assert home["status"] == 200
    assert incoming["status"] == 200
    assert "/api/incoming/pending" in incoming["text"]
    assert mold_mobile["status"] == 200
    assert "模具位置查询 - 天明ERP" in mold_mobile["text"]
    assert mold_label["status"] == 200
    assert "模具标签 - 天明ERP" in mold_label["text"]
    assert location_label["status"] == 200
    assert "仓库位置标签 - 天明ERP" in location_label["text"]
    assert rack_level_label["status"] == 200
    assert "货架层标签 - 天明ERP" in rack_level_label["text"]
