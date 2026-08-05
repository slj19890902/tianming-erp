"""Static frontend contract checks for N041 customer contracts."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
PRINT = (ROOT / "static" / "contract-print.html").read_text(encoding="utf-8")


def _inline_scripts(source: str) -> list[str]:
    return [script for script in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", source, re.DOTALL) if script.strip()]


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        raise AssertionError("Node.js is required for N041 frontend checks")
    return node


def _check_js(source: str, tmp_path: Path, name: str) -> None:
    scripts = _inline_scripts(source)
    assert len(scripts) == 1
    target = tmp_path / name
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run([_node(), "--check", str(target)], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr


def test_customer_contract_entry_and_hidden_work_page() -> None:
    assert 'openCustomerContracts(row)' in INDEX
    assert 'activePage === \'contracts\'' in INDEX
    assert '返回客户管理' in INDEX
    assert '客户合同' in INDEX
    assert 'v-if="canViewContracts"' in INDEX
    assert 'contracts:"contracts.view"' in INDEX or 'contracts: "contracts.view"' in INDEX
    assert 'roleMenus' in INDEX and 'contracts' not in INDEX.split('const roleMenus =', 1)[1].split('const bossOverviewCardOrder', 1)[0]


def test_contract_form_uses_customer_products_and_payload_contract() -> None:
    for marker in (
        'axios.get("/api/contracts"',
        'axios.post("/api/contracts"',
        'axios.put(`/api/contracts/${this.contractDraft.id}`',
        'axios.delete(`/api/contracts/${target.id}`',
        'confirm_text:"我确认删除合同"',
        'expected_version:target.version',
        'product_id: Number(line.product_id)',
        'contractProductOptions',
        '@search="searchContractProducts"',
        'keyword:String(keyword || "").trim()',
        'selectContractProduct(line,$event)',
        '默认单价',
    ):
        assert marker in INDEX
    assert 'customer_id:this.contractCustomer.id' in INDEX
    assert 'contract_date: this.contractDraft.contract_date' in INDEX
    assert 'delivery_date: this.contractDraft.delivery_date' in INDEX


def test_contract_form_defaults_and_compact_layout() -> None:
    header = INDEX.split('<div class="contract-header-grid">', 1)[1].split('<div class="contract-secondary-row">', 1)[0]
    assert header.index('客户</label>') < header.index('客户单号 / PO')
    assert header.index('客户单号 / PO') < header.index('合同日期')
    assert header.index('合同日期') < header.index('交期</label>')
    assert 'const addWorkingDays = (startDate, workingDays=7)' in INDEX
    assert 'delivery_date: addWorkingDays(today(), 7)' in INDEX
    assert 'onContractDateChanged' in INDEX
    assert 'markContractDeliveryDateManual' in INDEX
    assert "保存后自动使用天明合同号" in INDEX
    assert '.contract-edit-table { width: 100%; min-width: 0; table-layout: fixed' in INDEX
    assert '.contract-edit-table { min-width: 1560px' not in INDEX
    assert '.contract-remark-input { width: 6em' in INDEX
    assert '.contract-line-remark { width: 6em' in INDEX
    assert 'class="table-wrap contract-table-wrap"' in INDEX
    assert '.contract-product-select .search-select-list' in INDEX
    assert 'width: min(560px,70vw)' in INDEX
    assert '金额</th>' in INDEX


def test_contract_status_actions_are_locked_and_idempotent() -> None:
    assert "contractDraft.status === 'draft'" in INDEX
    assert "contractDraft.status === 'confirmed'" in INDEX
    assert "contractDraft.status === 'draft'" in INDEX and "contractDraft.status === 'confirmed'" in INDEX
    assert 'convert-order' in INDEX
    assert 'idempotencyKey:this.contractIdempotencyKey()' in INDEX
    assert 'idempotency_key:target.idempotencyKey' in INDEX
    assert 'typeof globalThis.crypto.randomUUID === "function"' in INDEX
    assert 'contract-${Date.now()}-${Math.random().toString(36)' in INDEX
    assert 'contractStatusText(status)' in INDEX
    assert 'contract_converted:"已转订单"' in INDEX
    assert 'if (!this.contractEditable) return null;' in INDEX
    assert 'if (this.loading || !this.contractEditable) return null;' not in INDEX


def test_contract_permissions_have_sales_edit_convert_and_finance_view() -> None:
    assert '"contracts.view","contracts.edit","contracts.convert"' in INDEX
    assert 'finance: ["customers.view","quotations.view","contracts.view"' in INDEX
    assert '["contracts", "合同查看", "contracts.view"]' in INDEX
    assert '["contracts", "合同编辑", "contracts.edit"]' in INDEX
    assert '["contracts", "合同转订单", "contracts.convert"]' in INDEX


def test_contract_print_is_safe_customer_facing_document() -> None:
    assert '/api/contracts/${encodeURIComponent(id)}/print' in PRINT
    assert 'window.print()' in PRINT
    assert '双方签章' not in PRINT  # signature blocks are rendered as customer/ERP sign areas
    assert '甲方（供方）' in PRINT
    assert '乙方（需方）' in PRINT
    assert 'escapeHtml' in PRINT
    assert 'estimated_unit_cost' not in PRINT
    assert 'gross_profit' not in PRINT
    assert 'margin_rate' not in PRINT
    assert '购货合同' in PRINT
    assert '存货编码</th>' not in PRINT
    assert 'item.product_code' not in PRINT
    assert '规格(mm)' not in PRINT
    assert '.replace(/mm/gi, "")' in PRINT
    assert '金额</th>' in PRINT
    assert 'buildContractPages(items)' in PRINT
    assert 'items.length <= 4' in PRINT
    assert 'pages.push({kind:"terms",items:[],offset:items.length})' in PRINT
    assert 'page.kind !== "terms"' in PRINT
    assert 'page.kind !== "items"' in PRINT
    assert 'class="contract-page page-${page.kind}"' in PRINT
    assert '第 ${pageIndex + 1} / ${pages.length} 页' in PRINT
    assert '第二条　交货' in PRINT
    assert '第七条　生效、附件与争议解决' in PRINT
    assert '甲方（供方）' in PRINT
    assert '乙方（需方）' in PRINT
    assert '本合同打印件仅显示业务条款' not in PRINT


def test_contract_print_page_route_is_registered() -> None:
    from app.main import create_app

    with TestClient(create_app()) as client:
        response = client.get("/contract-print.html")
    assert response.status_code == 200
    assert "客户合同" in response.text


def test_contract_work_page_deep_link_is_registered() -> None:
    from app.main import create_app

    with TestClient(create_app()) as client:
        response = client.get("/contracts")
    assert response.status_code == 200
    assert 'activePage === \'contracts\'' in response.text


def test_n041_inline_javascript_is_syntactically_valid(tmp_path: Path) -> None:
    _check_js(INDEX, tmp_path, "index-inline.js")
    _check_js(PRINT, tmp_path, "contract-print-inline.js")
