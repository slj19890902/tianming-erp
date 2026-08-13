from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
MOBILE_PICK = (ROOT / "static" / "mobile_delivery_pick.html").read_text(
    encoding="utf-8"
)
PICK_PRINT = (ROOT / "static" / "delivery-pick-print.html").read_text(
    encoding="utf-8"
)


def _method_body(start: str, end: str) -> str:
    return INDEX.split(start, 1)[1].split(end, 1)[0].rsplit("}", 1)[0]


def _run_node(source: str, tmp_path: Path, name: str) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the frontend permission contract"
    target = tmp_path / name
    target.write_text(source, encoding="utf-8")
    result = subprocess.run(
        [node, str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_delivery_picker_has_no_general_order_default_or_menu_fallback() -> None:
    role_menus = INDEX.split("const roleMenus =", 1)[1].split(
        "const bossOverviewCardOrder", 1
    )[0]
    defaults = INDEX.split("permissionDefaultAllowed(item, roleOverride = null)", 1)[1].split(
        "async loadPermissionUsers", 1
    )[0]

    assert "delivery_picker: []" in role_menus
    assert 'delivery_picker: ["deliveries.pick"]' in defaults
    assert not re.search(r"delivery_picker\s*:\s*\[[^\]]*orders\.view", defaults)


def test_picker_redirect_precedes_every_general_startup_resource() -> None:
    redirect = _method_body("redirectAfterLogin() {", "async loadProductBoxTypeRules(")
    check_session = _method_body("async checkSession() {", "async login() {")
    login = _method_body("async login() {", "async logout() {")

    assert 'this.user?.role === "delivery_picker"' in redirect
    assert 'this.hasPermission("deliveries.pick")' in redirect
    assert 'window.location.replace("/mobile/delivery-pick.html")' in redirect
    assert redirect.index("must_change_password") < redirect.index("delivery_picker")

    for block in (check_session, login):
        assert block.index(
            'this.user?.role === "delivery_picker" && this.user?.must_change_password'
        ) < block.index("if (this.redirectAfterLogin()) return;")
        assert block.index("if (this.redirectAfterLogin()) return;") < block.index(
            "loadProductBoxTypeRules()"
        )
        assert block.index("if (this.redirectAfterLogin()) return;") < block.index(
            "loadEffectiveUiLayout()"
        )
        assert block.index("if (this.redirectAfterLogin()) return;") < block.index(
            "loadInitialPageResources()"
        )


def test_unrelated_startup_resources_have_positive_permission_or_role_guards() -> None:
    box_rules = _method_body(
        "async loadProductBoxTypeRules({ force = false } = {}) {",
        "async checkSession() {",
    )
    layout = _method_body(
        "async loadEffectiveUiLayout() {", "pagePermission(page) {"
    )
    suppliers = _method_body(
        "async loadSuppliers(includeInactive = this.canAdmin) {",
        "defaultSupplierName() {",
    )

    assert 'if (!this.hasPermission("products.view"))' in box_rules
    assert 'if (!this.hasPermission("products.view"))' in suppliers
    assert '["admin", "finance", "sales", "workshop", "boss"].includes(this.user.role)' in layout


def test_sales_amount_and_cost_rendering_use_separate_positive_capabilities() -> None:
    amount_capability = _method_body(
        "canViewSalesAmounts() {", "canViewCosts() {"
    )
    assert '["admin", "boss", "sales", "finance"]' in amount_capability
    assert 'this.hasPermission("orders.view")' in amount_capability
    assert 'canViewCosts() { return this.hasPermission("cost.view"); }' in INDEX

    sensitive_tokens = (
        "unit_price",
        "subtotal",
        "total_amount",
        "sale_unit_price",
        "quote_price",
        "金额",
        "单价",
        "成本",
        "毛利",
        "price",
    )
    offenders = [
        f"{line_number}:{line.strip()}"
        for line_number, line in enumerate(INDEX.splitlines(), start=1)
        if ("!isWorkshop" in line or "!this.isWorkshop" in line)
        and any(token in line for token in sensitive_tokens)
    ]
    assert offenders == []

    assert 'v-if="canViewSalesAmounts" class="sensitive-price"' in INDEX
    assert 'v-if="canViewCosts" class="cost-sensitive' in INDEX
    assert 'role-no-sales-amounts' in INDEX

    order_markup = INDEX.split('<template v-else-if="activePage === \'orders\'">', 1)[1].split(
        '<template v-else-if="activePage === \'requisition\'">', 1
    )[0]
    for line_number, line in enumerate(order_markup.splitlines(), start=1):
        if "sensitive-price" in line and any(
            token in line
            for token in ("unit_price", "subtotal", "total_amount", "单价", "金额", "总价")
        ):
            assert 'v-if="canViewSalesAmounts"' in line, (
                f"order sales amount only relies on CSS at block line {line_number}: {line.strip()}"
            )
        if "cost-sensitive" in line and any(
            token in line for token in ("estimated", "成本", "毛利", "利润")
        ):
            assert 'v-if="canViewCosts"' in line, (
                f"order cost only relies on CSS at block line {line_number}: {line.strip()}"
            )


def test_logout_and_401_reset_clear_cached_order_amount_payloads() -> None:
    logout = _method_body("async logout() {", "resetPagePerformanceState() {")
    reset = _method_body("resetPagePerformanceState() {", "pageCacheFresh(page) {")
    mounted_auth = INDEX.split("window.erpAuthRequired = () => {", 1)[1].split(
        "window.erpForbidden", 1
    )[0]

    assert "this.resetPagePerformanceState();" in logout
    assert "this.resetPagePerformanceState();" in mounted_auth
    for clear in (
        "this.orders = [];",
        "this.orderGroupDetails = {};",
        "this.orderDetail = null;",
        "this.orderTrace = null;",
        "this.orderForm = {",
        "this.orderItemForm = {};",
    ):
        assert clear in reset
    plain_detail_line = next(
        line for line in INDEX.splitlines() if 'class="order-detail-row"' in line
    )
    assert '<th v-if="canViewCosts" class="cost-sensitive">预计成本</th>' in plain_detail_line
    assert '<th v-if="canViewCosts" class="cost-sensitive">预计利润</th>' in plain_detail_line
    assert '<td v-if="canViewCosts" class="cost-sensitive"><strong>{{ item.total_estimated_cost' in plain_detail_line
    assert '<td v-if="canViewCosts" class="cost-sensitive"><strong>{{ item.total_estimated_gross_profit' in plain_detail_line


def test_auth_reset_clears_cached_order_amount_payloads() -> None:
    reset = _method_body("resetPagePerformanceState() {", "pageCacheFresh(page) {")
    for statement in (
        "this.orders = [];",
        "this.ordersTotal = 0;",
        "this.ordersUnfinishedTotal = 0;",
        "this.expandedOrders = {};",
        "this.orderGroupDetails = {};",
        "this.orderDetail = null;",
        "this.orderGroupDetail = null;",
        "this.orderTrace = null;",
        "this.orderTraceEventDetail = null;",
    ):
        assert statement in reset


def test_picker_amount_capability_is_false_even_with_orders_override(tmp_path: Path) -> None:
    body = _method_body("canViewSalesAmounts() {", "canViewCosts() {")
    script = f"""
const FunctionCtor = Object.getPrototypeOf(function(){{}}).constructor;
const canViewSalesAmounts = new FunctionCtor({json.dumps(body, ensure_ascii=False)});
const allowed = new Set(["admin", "boss", "sales", "finance"]);
for (const role of allowed) {{
  const vm = {{user:{{role}}, hasPermission:code=>code==="orders.view"}};
  if (!canViewSalesAmounts.call(vm)) throw new Error(`${{role}} lost sales amounts`);
}}
const picker = {{user:{{role:"delivery_picker"}}, hasPermission:()=>true}};
if (canViewSalesAmounts.call(picker)) throw new Error("picker saw sales amounts");
const deniedSales = {{user:{{role:"sales"}}, hasPermission:()=>false}};
if (canViewSalesAmounts.call(deniedSales)) throw new Error("denied sales override was ignored");
"""
    _run_node(script, tmp_path, "candidate-b-sales-amount-capability.js")


def test_picker_page_contains_no_money_text_or_currency_symbol() -> None:
    for forbidden in ("单价", "金额", "成本", "毛利", "人民币", "¥", "￥"):
        assert forbidden not in MOBILE_PICK
        assert forbidden not in PICK_PRINT
    assert "/api/orders" not in MOBILE_PICK
    assert "/api/orders" not in PICK_PRINT
    assert "/api/delivery-picks" in MOBILE_PICK
    assert "/api/delivery-picks" in PICK_PRINT


def test_index_inline_javascript_is_syntactically_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the frontend syntax contract"
    scripts = [
        script
        for script in re.findall(
            r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, re.DOTALL
        )
        if script.strip()
    ]
    target = tmp_path / "candidate-b-index-inline.js"
    target.write_text("\n".join(scripts), encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr
