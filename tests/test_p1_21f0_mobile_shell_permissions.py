from __future__ import annotations

from pathlib import Path
import re
import shutil
import subprocess

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from test_p1_21b_mobile_admin_product_search import _login, mobile_erp_app


ROOT = Path(__file__).resolve().parents[1]
MOBILE = (ROOT / "static/mobile_erp.html").read_text(encoding="utf-8")


def _entries(payload: dict) -> dict[str, dict]:
    return {entry["id"]: entry for entry in payload["entries"]}


def test_shell_entries_are_backend_permission_derived_and_read_only(
    mobile_erp_app,
) -> None:
    from app.models.audit import OperationLog

    app, _ids, factory = mobile_erp_app
    with TestClient(app) as client:
        assert client.get("/api/mobile/erp/shell").status_code == 401
        _login(client, "mobile-admin")
        with factory() as db:
            before = db.scalar(select(func.count(OperationLog.id))) or 0
        response = client.get("/api/mobile/erp/shell")
        assert response.status_code == 200
        payload = response.json()
        entries = _entries(payload)
        assert list(entries) == ["lookup", "incoming", "warehouse", "production", "pre_delivery"]
        assert entries["lookup"]["can_execute"] is False
        assert entries["incoming"]["can_execute"] is True
        assert entries["warehouse"]["can_execute"] is True
        assert entries["production"]["stations"] == ["printing", "die_cut"]
        assert entries["production"]["can_execute"] is False
        assert entries["pre_delivery"]["can_execute"] is True
        assert entries["pre_delivery"]["can_manage"] is True
        assert payload["management_summary_allowed"] is True
        assert set(payload["user"]) == {"id", "username", "display_name", "role"}
        assert payload["user"]["username"] == "mobile-admin"
        assert payload["user"]["display_name"] == "手机管理员"
        assert payload["user"]["role"] == "admin"
        assert payload["read_only"] is True
        assert "items" not in payload
        with factory() as db:
            after = db.scalar(select(func.count(OperationLog.id))) or 0
        assert after == before

    with TestClient(app) as client:
        _login(client, "mobile-scoped")
        payload = client.get("/api/mobile/erp/shell").json()
        assert list(_entries(payload)) == ["lookup", "incoming", "warehouse", "production"]

    with TestClient(app) as client:
        _login(client, "mobile-picker")
        payload = client.get("/api/mobile/erp/shell").json()
        entries = _entries(payload)
        assert list(entries) == ["pre_delivery"]
        assert entries["pre_delivery"]["can_execute"] is True
        assert entries["pre_delivery"]["can_manage"] is False
        assert payload["management_summary_allowed"] is False


def test_printing_and_die_cut_station_permissions_are_independent(
    mobile_erp_app,
) -> None:
    from app.models.access_control import UserPermissionOverride
    from app.models.user import User

    app, _ids, factory = mobile_erp_app
    with factory() as db:
        employee = db.scalar(select(User).where(User.username == "mobile-scoped"))
        admin = db.scalar(select(User).where(User.username == "mobile-admin"))
        assert employee is not None and admin is not None
        db.add(
            UserPermissionOverride(
                user_id=employee.id,
                permission_code="production.die_cut.view",
                is_allowed=False,
                granted_by=admin.id,
            )
        )
        db.commit()

    with TestClient(app) as client:
        _login(client, "mobile-scoped")
        production = _entries(client.get("/api/mobile/erp/shell").json())["production"]
        assert production["stations"] == ["printing"]

    with factory() as db:
        employee = db.scalar(select(User).where(User.username == "mobile-scoped"))
        admin = db.scalar(select(User).where(User.username == "mobile-admin"))
        assert employee is not None and admin is not None
        db.add(
            UserPermissionOverride(
                user_id=employee.id,
                permission_code="production.printing.view",
                is_allowed=False,
                granted_by=admin.id,
            )
        )
        db.commit()

    with TestClient(app) as client:
        _login(client, "mobile-scoped")
        assert "production" not in _entries(client.get("/api/mobile/erp/shell").json())
        denied = client.get("/api/mobile/erp/production/recent")
        assert denied.status_code == 403
        assert denied.json()["detail"] == "当前账号没有手机生产工位查看权限"


def test_shared_mobile_shell_is_lazy_and_preserves_entry_state() -> None:
    for marker in (
        'data-page="lookup">查询',
        'data-page="incoming">收料',
        'data-page="warehouse">仓库',
        'data-page="production">生产',
        'data-page="pre_delivery">预送货',
        'state.shell = await apiGet("/api/mobile/erp/shell")',
        'byId("incomingFrame").src = "/incoming.html?embedded=1"',
        'byId("preDeliveryFrame").src = "/mobile/delivery-pick.html?embedded=1"',
        "loadedFrames: new Set()",
        "scrollByPage: new Map()",
        "state.scrollByPage.set(state.activePage, window.scrollY)",
        "state.scrollByPage.get(page) || 0",
    ):
        assert marker in MOBILE
    assert '<iframe id="incomingFrame"' in MOBILE and 'src="about:blank"' in MOBILE
    assert '<iframe id="preDeliveryFrame"' in MOBILE
    initialize = MOBILE.split("async function initialize()", 1)[1].split(
        "document.querySelectorAll", 1
    )[0]
    assert 'apiGet("/api/mobile/erp/shell")' in initialize
    assert 'apiGet("/api/auth/me")' not in initialize
    assert "/api/dashboard/overview" not in initialize
    assert "/api/mobile/erp/products" not in initialize
    assert "/api/mobile/erp/production/recent" not in initialize


def test_shared_mobile_shell_javascript_is_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None
    scripts = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", MOBILE, re.DOTALL)
    assert len(scripts) == 1
    target = tmp_path / "p1-21f0-mobile-shell.js"
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
