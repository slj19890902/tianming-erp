from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path


INDEX = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text(encoding="utf-8")


def _block(start: str, end: str) -> str:
    position = INDEX.index(start)
    return INDEX[position : INDEX.index(end, position)]


def test_fin001_reuses_finance_and_customer_permissions_without_tax_customer_entry() -> None:
    finance = _block("<template v-else-if=\"activePage === 'finance'\">", "<template v-else-if=\"activePage === 'audit'\">")
    assert "开票任务" in finance
    assert "canViewInvoiceTasks" in finance
    assert "新增税务客户" not in finance
    assert "finance.invoice_profile.manage" in INDEX
    assert "finance.invoice_task.generate" in INDEX
    assert "finance.invoice_result.register" in INDEX
    assert "开票 / 税务资料" in INDEX
    assert "invoice-profile" in INDEX
    assert "普通联系人地址、电话不会自动带入开票资料" in INDEX


def test_fin001_exposes_minimum_seller_and_customer_invoice_rule_contracts() -> None:
    finance = _block("<template v-else-if=\"activePage === 'finance'\">", "<template v-else-if=\"activePage === 'audit'\">")
    methods = _block("financeConfirmationText(statement)", "async clearFinanceDashboardFilter()")
    assert "维护销方主体" in finance
    assert "默认销方" in INDEX
    assert "客户默认开票项目规则" in INDEX
    for label in ("项目名称", "税收分类编码", "单位", "税率", "确认状态"):
        assert label in INDEX
    assert 'axios.get("/api/finance/invoice-sellers"' in methods
    assert 'axios.post("/api/finance/invoice-sellers", payload)' in methods
    assert "/api/finance/invoice-sellers/${editingId}" in methods
    assert "invoice-item-rules/default" in methods
    assert "canManageInvoiceProfiles" in finance


def test_fin001_has_fail_closed_task_flow_and_no_tax_bureau_automation() -> None:
    finance = _block("<template v-else-if=\"activePage === 'finance'\">", "<template v-else-if=\"activePage === 'audit'\">")
    methods = _block("financeConfirmationText(statement)", "async clearFinanceDashboardFilter()")
    assert "核对并确认" in finance
    assert "生成开票任务" in finance
    assert "缺项 / 操作" in finance
    assert "进入客户税务资料" in finance
    assert "下载税局 Excel" in finance
    assert "登记成功/失败" in finance
    assert "上传原始 PDF" in finance
    assert "开票成功已登记" in INDEX
    assert 'axios.post(`/api/finance/statements/${statementId}/confirm`' in methods
    assert 'axios.post(`/api/finance/statements/${statementId}/invoice-tasks`' in methods
    assert 'axios.get("/api/finance/invoice-tasks"' in methods
    assert "tax-template.xlsx" in methods
    assert "const idempotencyKey = createIdempotencyKey();" in methods
    assert "idempotency_key:idempotencyKey" in methods
    assert "税局网页" not in finance


def test_inline_javascript_is_syntax_valid() -> None:
    node = shutil.which("node")
    assert node
    scripts = [item for item in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, flags=re.DOTALL) if item.strip()]
    assert scripts
    for script in scripts:
        completed = subprocess.run([node, "--check"], input=script, text=True, encoding="utf-8", capture_output=True, env=os.environ.copy(), check=False)
        assert completed.returncode == 0, completed.stderr


def test_p0_16_confirmed_statement_has_reachable_invoice_task_actions() -> None:
    finance = _block("<template v-else-if=\"activePage === 'finance'\">", "<template v-else-if=\"activePage === 'audit'\">")
    methods = _block("financeConfirmationText(statement)", "async clearFinanceDashboardFilter()")
    assert "statement.invoice_task" in finance
    assert "查看开票任务" in finance
    assert 'v-else-if="canGenerateInvoiceTask"' in finance
    assert "row.confirmation_status = data.confirmation_status" in methods
    assert "row.version = data.version" in methods
    assert "下一步请点击“生成开票任务”" in methods
    assert "financeInvoicePrerequisite" in INDEX
    assert "开票资料尚未完善" in INDEX
    assert "进入客户税务资料" in INDEX
    assert "missing_items:[...missing]" in methods
    assert "await this.openInvoiceTask(task)" in methods
    assert "detail.missing_items || detail.missing_fields" in INDEX


def test_p0_16_customer_selector_is_layered_keyboard_operable_and_last_request_wins() -> None:
    assert ".search-select:focus-within { z-index: 140; }" in INDEX
    assert ".search-select-option { width: 100%; min-height: 38px;" in INDEX
    for marker in (
        'role="combobox"',
        'role="listbox"',
        '@keydown.down.prevent="moveActive(1)"',
        '@keydown.up.prevent="moveActive(-1)"',
        '@keydown.enter.prevent="chooseActive"',
        '@keydown.esc="closeList"',
        ':aria-activedescendant=',
    ):
        assert marker in INDEX
    statement_customer_loader = _block(
        "async loadStatementCustomers()",
        "async loadPendingStatements()",
    )
    assert 'const requestKey = "finance:statement-customers"' in statement_customer_loader
    assert "this.beginLatestRequest(requestKey)" in statement_customer_loader
    assert "signal:controller.signal" in statement_customer_loader
    assert "latestRequestControllers.get(requestKey) !== controller" in statement_customer_loader
    assert "this.finishLatestRequest(requestKey, controller)" in statement_customer_loader
