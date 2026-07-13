from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
INCOMING = (ROOT / "static" / "incoming.html").read_text(encoding="utf-8")
WAREHOUSE = (ROOT / "static" / "warehouse.html").read_text(encoding="utf-8")


def test_n028_permission_management_uses_atomic_backend_contract() -> None:
    assert 'axios.get("/api/auth/users")' in INDEX
    assert 'axios.post("/api/auth/users"' in INDEX
    assert "/api/auth/users/${encodeURIComponent(userId)}/permission-overrides" in INDEX
    assert "/api/auth/users/${encodeURIComponent(userId)}/customer-scopes" in INDEX
    assert "/api/auth/users/${encodeURIComponent(userId)}/access" in INDEX
    assert "overrides:this.permissionOverridePayload()" in INDEX
    assert "mode:this.permissionDraft.customer_access_mode" in INDEX
    assert "customer_ids:this.permissionDraft.customer_ids" in INDEX


def test_n028_permission_ui_supports_user_selection_checks_and_customer_scope() -> None:
    for marker in (
        'activePage === \'permissions\'',
        "selectedPermissionUserId",
        "permissions:catalog",
        "permissionDraft.permissions[item.code]",
        "togglePermissionCustomer",
        "permissionDraft.customer_ids",
        "保存权限与客户范围",
        "openCreateUserModal",
        "createPermissionUser",
    ):
        assert marker in INDEX


def test_n028_auth_payload_and_visibility_rules_are_wired() -> None:
    assert "permissions:data.permissions || []" in INDEX
    assert "customer_scope:data.customer_scope || []" in INDEX
    assert "unrestricted_customer_access" in INDEX
    assert 'boss:"老板"' in INDEX
    assert ".role-boss" in INDEX
    assert "role-no-costs" in INDEX
    assert 'canViewCosts() { return this.hasPermission("cost.view"); }' in INDEX
    assert 'canRequisition() { return this.hasPermission("requisition.execute"); }' in INDEX
    assert 'if (this.canManagePermissions) items.push({ key: "permissions"' in INDEX
    assert 'roleMenus' in INDEX and 'boss: ["dashboard"' in INDEX
    assert 'system: "system.backup"' in INDEX or 'system:"system.backup"' in INDEX
    assert 'permissions: "users.manage"' in INDEX or 'permissions:"users.manage"' in INDEX


def test_n028_sales_default_menu_and_permissions_are_business_limited() -> None:
    assert 'sales: ["dashboard", "customers", "products", "orders"]' in INDEX
    expected = 'sales: ["customers.view","customers.edit","products.view","products.edit","quotations.view","quotations.edit","orders.view","orders.create","orders.edit","dashboard.view"]'
    assert INDEX.count(expected) == 2
    for forbidden in (
        "warehouse.view",
        "warehouse.reserve",
        "deliveries.view",
        "deliveries.execute",
        "requisition.view",
        "requisition.execute",
        "cost.view",
    ):
        assert forbidden not in expected


def test_n028_boss_responsive_breakpoints_are_scoped_from_regular_roles() -> None:
    assert ".role-boss .btn { min-height: 44px;" in INDEX
    assert ".role-boss .input, .role-boss .select, .role-boss .textarea { min-height: 46px;" in INDEX
    assert "@media (max-width: 1280px)" in INDEX
    assert "@media (max-width: 900px)" in INDEX
    assert "body:has(.role-boss) { min-width: 0; }" in INDEX
    assert ".role-boss .layout { grid-template-columns: 220px minmax(0, 1fr); }" in INDEX
    assert ".role-boss .layout { display: flex; flex-direction: column; }" in INDEX
    assert ".role-boss .top-actions { flex: 1 1 100%;" in INDEX
    assert ".role-boss .page-head { align-items: flex-start; flex-wrap: wrap;" in INDEX
    mobile_match = re.search(r"@media \(max-width: 900px\) \{(.*?)\n      \}", INDEX, re.DOTALL)
    assert mobile_match is not None
    mobile_css = mobile_match.group(1)
    shell_match = re.search(
        r"\.role-boss,\s*\.role-boss \.layout,\s*\.role-boss \.topbar,\s*"
        r"\.role-boss \.sidebar,\s*\.role-boss \.main \{(.*?)\}",
        mobile_css,
        re.DOTALL,
    )
    assert shell_match is not None
    shell_css = shell_match.group(1)
    for declaration in ("width: 100%;", "max-width: 100vw;", "min-width: 0;", "box-sizing: border-box;"):
        assert declaration in shell_css
    assert "body:has(.role-boss) { width: 100%; max-width: 100vw; min-width: 0; }" in mobile_css
    assert ".role-boss .main { padding: 20px 16px 32px; overflow-x: hidden; }" in mobile_css
    assert ".role-boss .table-wrap { width: 100%; max-width: 100%; min-width: 0; overflow-x: auto; }" in mobile_css
    assert ".role-admin .layout" not in INDEX
    assert ".role-sales .layout" not in INDEX


def test_n028_view_only_operational_pages_hide_write_controls() -> None:
    assert "incomingTab==='pending' && hasPermission('incoming.execute')" in INDEX
    assert "v-if=\"hasPermission('incoming.execute')\"" in INDEX
    assert "拥有“来料入库操作”权限才可撤销" in INDEX
    assert 'canFinance() { return this.hasPermission("finance.execute"); }' in INDEX
    assert '<button v-if="canFinance" class="btn primary" @click="openStatement">' in INDEX
    assert '<button v-if="canFinance" class="btn small" @click="registerInvoice(row)">' in INDEX
    assert '<button v-if="canFinance" class="btn small success"' in INDEX
    assert 'quotations:"quotations.view"' in INDEX
    assert 'if (!this.pageAllowed(page))' in INDEX
    assert "当前账号没有访问该功能的权限" in INDEX
    assert "v-if=\"hasPermission('finance.view')\">本月累计营收" in INDEX
    assert "v-if=\"hasPermission('finance.view')\">待收账款" in INDEX
    assert 'hasPermission("incoming.execute")' in INCOMING
    assert 'hasPermission("incoming.view")' in INCOMING
    assert 'hasPermission("warehouse.execute")' in WAREHOUSE
    assert 'hasPermission("warehouse.view")' in WAREHOUSE
    assert 'class="btn primary operate-only"' in WAREHOUSE


def test_n028_inline_javascript_is_syntactically_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        raise AssertionError("Node.js is required for the frontend contract test")
    scripts = [script for script in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, re.DOTALL) if script.strip()]
    assert len(scripts) == 1
    target = tmp_path / "index-inline.js"
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run([node, "--check", str(target)], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr

    for name, source in (("incoming", INCOMING), ("warehouse", WAREHOUSE)):
        scripts = [
            script
            for script in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", source, re.DOTALL)
            if script.strip()
        ]
        target = tmp_path / f"{name}-inline.js"
        target.write_text("\n".join(scripts), encoding="utf-8")
        result = subprocess.run(
            [node, "--check", str(target)], capture_output=True, text=True, encoding="utf-8"
        )
        assert result.returncode == 0, result.stderr
