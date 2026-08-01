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
    assert 'axios.get("/api/finance/invoice-sellers")' in methods
    assert 'axios.post("/api/finance/invoice-sellers",payload)' in methods
    assert "/api/finance/invoice-sellers/${form.id}" in methods
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
    assert 'axios.post(`/api/finance/statements/${row.id}/confirm`' in methods
    assert 'axios.post(`/api/finance/statements/${row.id}/invoice-tasks`' in methods
    assert 'axios.get("/api/finance/invoice-tasks"' in methods
    assert "tax-template.xlsx" in methods
    assert "idempotency_key:createIdempotencyKey()" in methods
    assert "税局网页" not in finance


def test_inline_javascript_is_syntax_valid() -> None:
    node = shutil.which("node")
    assert node
    scripts = [item for item in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, flags=re.DOTALL) if item.strip()]
    assert scripts
    for script in scripts:
        completed = subprocess.run([node, "--check"], input=script, text=True, encoding="utf-8", capture_output=True, env=os.environ.copy(), check=False)
        assert completed.returncode == 0, completed.stderr
