from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def test_p5_ui_mode_drives_shell_and_persists_with_backend_contract() -> None:
    assert "'ui-large': isLargeUi" in INDEX
    assert "'ui-standard': !isLargeUi" in INDEX
    assert 'if (["standard", "large"].includes(this.user?.ui_mode)) return this.user.ui_mode;' in INDEX
    assert 'return this.isBoss ? "large" : "standard";' in INDEX
    assert 'axios.put("/api/auth/me/ui-mode", { ui_mode:mode })' in INDEX
    assert "this.user = {...this.user, ...data.user, ui_mode:data.user.ui_mode || mode};" in INDEX
    assert '@click="setUiMode(\'standard\')">标准</button>' in INDEX
    assert '@click="setUiMode(\'large\')">大字</button>' in INDEX
    assert 'role="group" aria-label="界面显示大小"' in INDEX


def test_p5_late_ui_mode_response_cannot_restore_logged_out_user() -> None:
    assert "authGeneration: 0" in INDEX
    assert "const requestGeneration = this.authGeneration;" in INDEX
    assert "const requestUserId = this.user?.id;" in INDEX
    assert "requestGeneration !== this.authGeneration || this.user?.id !== requestUserId" in INDEX
    assert "this.authGeneration += 1;" in INDEX
    logout = re.search(r"async logout\(\) \{(.*?)\n          \},", INDEX, re.DOTALL)
    assert logout is not None
    assert logout.group(1).index("this.authGeneration += 1;") < logout.group(1).index("await axios.post")
    assert logout.group(1).index("this.user = null;") < logout.group(1).index("await axios.post")


def test_p5_large_mode_meets_typography_and_control_size_contract() -> None:
    for marker in (
        ".ui-large { font-size: 18px;",
        ".ui-large .page-head h2, .ui-large .order-head-card h2 { font-size: 28px !important;",
        ".ui-large .page-head p, .ui-large .page-subtitle { font-size: 17px;",
        ".ui-large .field label, .ui-large .card-label, .ui-large .section-title,",
        ".ui-large .btn { min-height: 48px;",
        ".ui-large .input, .ui-large .select, .ui-large .textarea {",
        "min-height: 48px; padding: 10px 14px; font-size: 17px;",
        ".ui-large .ui-mode-button { min-height: 48px;",
    ):
        assert marker in INDEX


def test_p5_large_mode_has_zoom_safe_responsive_shell() -> None:
    assert "body:has(.ui-large) { min-width: 0; }" in INDEX
    assert "@media (max-width: 1400px)" in INDEX
    assert "@media (max-width: 1180px)" in INDEX
    assert "@media (max-width: 760px)" in INDEX
    assert ".ui-large .layout { grid-template-columns: 220px minmax(0, 1fr); }" in INDEX
    assert ".ui-large .layout { display: flex; flex-direction: column; }" in INDEX
    assert "grid-template-columns: repeat(4, minmax(0, 1fr));" in INDEX
    assert ".ui-large .dashboard-cards { grid-template-columns: repeat(2, minmax(0, 1fr)); }" in INDEX
    assert ".ui-large .table-wrap { width: 100%; max-width: 100%; min-width: 0; overflow-x: auto; }" in INDEX


def test_p5_large_mode_keeps_delivery_and_production_history_columns_reachable() -> None:
    for marker in (
        '<div v-else class="panel delivery-list-panel">',
        '<table class="delivery-list-table">',
        'class="toolbar-group delivery-list-actions"',
        ".ui-large .delivery-list-panel .table-wrap {",
        ".ui-large .delivery-list-table { min-width: 1480px; }",
        ".ui-large .delivery-list-actions { min-width: 300px; white-space: normal; }",
        ".delivery-list-table {\n          width: 100%; min-width: 0 !important; table-layout: fixed; white-space: normal;",
        ".delivery-list-panel .table-wrap {\n          width: 100%; max-width: 100%; min-width: 0; overflow-x: hidden;",
        ".delivery-list-table .delivery-col-actions { width: 21%; }",
        'class="btn small delivery-list-detail-toggle"',
        '<th class="production-history-time">完工时间</th>',
        '<td class="production-history-time">{{ formatDateTime(row.completed_at) }}</td>',
        ".ui-large .production-history-time { width: 154px; white-space: nowrap; }",
        '<th class="production-history-actions">操作</th>',
        '<td class="production-history-actions"><button v-if="user?.role===\'admin\' && row.can_revert"',
        ".ui-large .production-history-actions { width: 66px; white-space: nowrap; text-align:center; }",
    ):
        assert marker in INDEX


def test_p5_delivery_list_narrow_desktop_contract_keeps_every_column_and_action() -> None:
    column_classes = re.findall(
        r'<col class="(delivery-col-[^"]+)"\s*/>',
        INDEX,
    )
    assert column_classes == [
        "delivery-col-number",
        "delivery-col-customer",
        "delivery-col-date",
        "delivery-col-vehicle",
        "delivery-col-quantity",
        "delivery-col-status",
        "delivery-col-receipt",
        "delivery-col-detail",
        "delivery-col-actions",
    ]
    widths = {
        name: int(width)
        for name, width in re.findall(
            r"\.delivery-list-table \.(delivery-col-[a-z]+) \{ width: (\d+)%; \}",
            INDEX,
        )
    }
    assert set(widths) == set(column_classes)
    assert sum(widths.values()) == 100
    for action in (
        "拿货",
        "管理员查看",
        "打开手机拿货",
        "打印仓库找货单",
        "发货打印",
        "直接发货打印",
        "编辑",
        "删除",
        "编辑回单",
        "取消回单",
        "确认回单",
        "补打",
        "取消发货",
    ):
        assert action in INDEX
    assert ".delivery-list-table .delivery-list-actions .select { width: min(130px, 100%); }" in INDEX
    assert ".delivery-list-table .delivery-list-actions {\n          min-width: 0 !important; gap: 4px; align-items: flex-start;" in INDEX


def test_p5_incoming_table_wraps_controls_inside_fixed_columns() -> None:
    for marker in (
        ".incoming-table { width: 100%; min-width: 0; table-layout: fixed; white-space: normal; }",
        ".incoming-table .input, .incoming-table .select { min-width: 0 !important;",
        ".incoming-sequence-cell,.incoming-dimension-cell,.incoming-times-cell,.incoming-date-cell,.incoming-quantity-summary { white-space:nowrap !important;",
        ".incoming-compact-table th:nth-child(12) { width:13.1%; }",
        ".incoming-action-cell { width: 9%; }",
        ".incoming-table .btn {\n        max-width: 100%; min-width: 0; padding-left: 6px; padding-right: 6px;",
        "line-height: 1.3; white-space: normal; overflow-wrap: anywhere;",
        ".incoming-table td:last-child .btn { display: block; width: 100%; margin: 0 0 4px; }",
        '<input v-if="incomingTab===\'pending\' && hasPermission(\'incoming.execute\')" class="input compact-input" type="number"',
        '<th>报料长(mm)</th><th class="incoming-times-cell">×</th><th>报料宽(mm)</th>',
        '<td class="incoming-dimension-cell"><strong>{{ incomingDimensionMm(row.cardboard_len) }}</strong></td><td class="incoming-times-cell">×</td><td class="incoming-dimension-cell"><strong>{{ incomingDimensionMm(row.cardboard_width) }}</strong></td>',
        '<td class="incoming-quantity-summary">{{ row.planned_quantity ?? row.requisition_qty ?? row.quantity ?? 0 }} / {{ row.cumulative_received_quantity || 0 }} / {{ row.remaining_quantity ?? 0 }}</td>',
        'class="incoming-action-cell"><template v-if="incomingTab===\'pending\'">',
        '<button v-if="hasPermission(\'incoming.execute\')" class="btn small success" :disabled="incomingReceiveAttempts[row.item_id]?.saving || !canReceiveIncoming(row)" @click="receiveIncoming(row)">',
        '<button v-if="hasPermission(\'incoming.execute\') && row.purpose_status===\'legacy_unset\' && row.pending_receipt_item_id" class="btn small" @click="acceptShortIncoming(row)">短收结单</button>',
    ):
        assert marker in INDEX


def test_p5_customer_list_has_page_scoped_narrow_desktop_layout() -> None:
    customer_columns = re.findall(
        r'<col class="(customer-col-[^"]+)"\s*/>',
        INDEX,
    )
    assert customer_columns == [
        "customer-col-number",
        "customer-col-code",
        "customer-col-label-short",
        "customer-col-name",
        "customer-col-contact",
        "customer-col-phone",
        "customer-col-term",
        "customer-col-delivery",
        "customer-col-status",
        "customer-col-actions",
    ]
    widths = {
        name: int(width)
        for name, width in re.findall(
            r"\.customer-list-table \.(customer-col-[a-z-]+) \{ width: (\d+)%; \}",
            INDEX,
        )
    }
    assert set(widths) == set(customer_columns)
    assert sum(widths.values()) == 100
    for marker in (
        ".table-wrap:has(> .customer-list-table) {",
        "width: 100%; max-width: 100%; min-width: 0; overflow-x: hidden;",
        ".customer-list-table {\n          width: 100%; min-width: 0; table-layout: fixed; white-space: normal;",
        'class="toolbar-group customer-list-actions"',
        ".customer-list-actions .btn {\n          max-width: 100%; min-width: 0; padding-left: 7px; padding-right: 7px;",
    ):
        assert marker in INDEX


def test_p5_boss_dashboard_uses_authoritative_business_cards_in_fixed_order() -> None:
    match = re.search(
        r"const bossOverviewCardOrder = Object\.freeze\(\[(.*?)\]\);",
        INDEX,
        re.DOTALL,
    )
    assert match is not None
    block = match.group(1)
    assert re.findall(r'key: "([^"]+)"', block) == [
        "pending_material",
        "pending_incoming",
        "pending_production",
        "pending_delivery",
        "pending_receipt",
        "pending_reconciliation",
        "pending_invoice",
        "pending_payment",
        "warehouse_capacity",
        "inventory_risk",
        "business_anomaly",
    ]
    assert re.findall(r'title: "([^"]+)"', block) == [
        "待报料",
        "待入库",
        "待生产",
        "待送货",
        "待回单",
        "待对账",
        "待开票",
        "待结款",
        "仓储容量",
        "库存风险",
        "异常",
    ]
    assert "if (this.isBoss) {" in INDEX
    assert "eligible = bossOverviewCardOrder" in INDEX
    assert 'return this.applyEffectiveLayout("dashboard_cards", eligible);' in INDEX
    assert "v-for=\"card in dashboardCards\"" in INDEX
    assert "v-for=\"card in overview.cards\"" not in INDEX


def test_p5_boss_has_no_system_technical_entry() -> None:
    boss_menu = re.search(r'boss: \[(.*?)\]', INDEX)
    assert boss_menu is not None
    assert '"system"' not in boss_menu.group(1)
    assert '"permissions"' not in boss_menu.group(1)
    assert 'if (this.isBoss && ["system", "permissions"].includes(page)) return false;' in INDEX
    assert '.filter(item => item.key !== "system_hub" || this.user?.role !== "boss")' in INDEX


def test_p5_inline_javascript_is_syntactically_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        raise AssertionError("Node.js is required for the frontend contract test")
    scripts = [
        script
        for script in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, re.DOTALL)
        if script.strip()
    ]
    assert len(scripts) == 1
    target = tmp_path / "p5-index-inline.js"
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
