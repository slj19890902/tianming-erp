from __future__ import annotations

from pathlib import Path
import re
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static/index.html").read_text(encoding="utf-8")
MOBILE = (ROOT / "static/mobile_erp.html").read_text(encoding="utf-8")


def test_admin_layout_ui_is_simple_and_lazy_loaded() -> None:
    for marker in (
        "界面布局",
        "保存草稿",
        "预览草稿",
        "发布给该角色",
        "恢复系统默认",
        "回滚上一版",
        'section === "layout"',
        "loadUiLayoutAdmin",
        "moveUiLayoutItem",
    ):
        assert marker in INDEX
    assert "uiLayoutAdminPreview" in INDEX
    assert "uiLayoutAdminDirty" in INDEX
    assert "uiLayoutAdminLoadGeneration" in INDEX
    assert "requestGeneration !== this.uiLayoutAdminLoadGeneration" in INDEX
    assert "async loadUiLayoutAdmin({throwOnError=false}={})" in INDEX
    assert "loadUiLayoutAdmin({throwOnError:true})" in INDEX
    assert "user.role === 'admin' && systemSection === 'layout'" in INDEX
    assert "请先保存草稿再发布" in INDEX
    assert "drag" not in INDEX.lower()
    assert "arbitrary_html" not in INDEX


def test_layout_only_filters_already_permitted_local_components() -> None:
    assert 'return this.applyEffectiveLayout("menus", eligible);' in INDEX
    assert 'return this.applyEffectiveLayout("dashboard_cards", eligible);' in INDEX
    assert 'return this.applyEffectiveLayout("quick_actions", eligible);' in INDEX
    assert "const byKey = new Map((eligible || []).map(item => [item.key,item]));" in INDEX
    assert "configured.map(item => byKey.get(item.id)).filter(Boolean)" in INDEX
    assert 'hasPermission(code) {' in INDEX
    assert 'pageAllowed(page)' in INDEX
    assert 'window.erpAuthRequired' in INDEX and "this.uiLayoutEffective = null" in INDEX


def test_layout_loads_on_login_and_mode_switch_without_blocking_login() -> None:
    assert INDEX.count("this.loadEffectiveUiLayout()") >= 4
    assert 'axios.get("/api/system/ui-layout/effective"' in INDEX
    assert "catch (_error)" in INDEX
    assert "this.uiLayoutEffective = null" in INDEX
    assert 'option value="mobile">手机</option>' in INDEX
    assert 'setUiMode("mobile")' not in INDEX
    assert 'state.shell = await apiGet("/api/mobile/erp/shell")' in MOBILE
    assert '"layout_version": layout_version' in (ROOT / "app/api/mobile_erp.py").read_text(encoding="utf-8")
    assert '["home", "lookup", "incoming", "warehouse", "production", "pre_delivery"].forEach(page =>' in MOBILE
    assert "navContainer.append(button)" in MOBILE


def test_index_inline_javascript_is_syntactically_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the frontend contract test"
    scripts = [
        script
        for script in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, re.DOTALL)
        if script.strip()
    ]
    assert len(scripts) == 1
    target = tmp_path / "index-inline.js"
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    mobile_scripts = [
        script
        for script in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", MOBILE, re.DOTALL)
        if script.strip()
    ]
    mobile_target = tmp_path / "mobile-inline.js"
    mobile_target.write_text("\n".join(mobile_scripts), encoding="utf-8")
    mobile_result = subprocess.run(
        [node, "--check", str(mobile_target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert mobile_result.returncode == 0, mobile_result.stderr
