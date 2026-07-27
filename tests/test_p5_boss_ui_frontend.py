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


def test_p5_large_mode_keeps_delivery_and_production_history_columns_readable() -> None:
    for marker in (
        '<data-panel class="delivery-list-panel" :empty="!deliveries.length">',
        '<table class="delivery-list-table">',
        'class="toolbar-group delivery-list-actions"',
        ".ui-large .delivery-list-panel .table-wrap {",
        ".ui-large .delivery-list-table { min-width: 1480px; }",
        ".ui-large .delivery-list-actions { min-width: 300px; white-space: normal; }",
        '<th class="production-history-time">完工时间</th>',
        '<td class="production-history-time">{{ formatDateTime(row.completed_at) }}</td>',
        ".ui-large .production-history-time { width: 176px; white-space: nowrap; }",
        '<th class="production-history-actions">操作</th>',
        '<td class="production-history-actions"><div class="toolbar-group">',
        ".ui-large .production-history-actions { width: 280px; white-space: normal; }",
    ):
        assert marker in INDEX


def test_p5_boss_dashboard_uses_only_the_six_business_cards_in_fixed_order() -> None:
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
        "pending_delivery",
        "unsettled_statements",
        "inventory_risk",
        "business_anomaly",
    ]
    assert re.findall(r'title: "([^"]+)"', block) == [
        "待报料",
        "待入库",
        "待送货",
        "应收",
        "库存风险",
        "异常",
    ]
    assert "if (!this.isBoss) return cards;" in INDEX
    assert "v-for=\"card in dashboardCards\"" in INDEX
    assert "v-for=\"card in overview.cards\"" not in INDEX


def test_p5_boss_has_no_system_technical_entry() -> None:
    boss_menu = re.search(r'boss: \[(.*?)\]', INDEX)
    assert boss_menu is not None
    assert '"system"' not in boss_menu.group(1)
    assert '"permissions"' not in boss_menu.group(1)
    assert 'if (this.isBoss && ["system", "permissions"].includes(page)) return false;' in INDEX
    assert '.filter(item => item.key !== "system" || this.user?.role !== "boss")' in INDEX


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
