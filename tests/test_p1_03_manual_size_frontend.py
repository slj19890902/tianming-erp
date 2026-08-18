from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _order_template() -> str:
    start = INDEX.index('<div v-else-if="modal.type === \'order\'">')
    end = INDEX.index('<div v-else-if="modal.type === \'orderEdit\'">', start)
    return INDEX[start:end]


def test_p1_03_manual_size_entry_is_a_clear_compact_order_action() -> None:
    block = _order_template()
    assert "手工尺寸" in block
    assert "addManualSizeOrderItem" in block
    assert "manual-size-entry" in block
    assert "manual-size-line" in INDEX
    assert "manual-size-dimensions" in INDEX
    assert "manual-size-inline" in INDEX
    assert 'v-model="item.box_type"' in block
    for field in ("item.length_mm", "item.width_mm", "item.height_mm", "item.quantity"):
        assert field in block
    assert "reuseBlankLine" in INDEX
    assert "this.orderLineIsBlank(this.orderForm.items[0])" in INDEX


def test_p1_03_manual_size_payload_is_explicit_and_does_not_fake_common_box() -> None:
    assert "manual_size_entry: !!item.manual_size_entry" in INDEX
    assert "box_type: item.manual_size_entry" in INDEX
    assert "length_mm: item.manual_size_entry" in INDEX
    assert "width_mm: item.manual_size_entry" in INDEX
    assert "height_mm: item.manual_size_entry" in INDEX
    assert "手工尺寸报价不得自动生成或猜测" not in INDEX  # policy is enforced by payload, not a stale hint
    assert 'product_code: ""' in INDEX
    assert "manual_size_entry: true" in INDEX


def test_p1_03_quote_preview_preserves_manual_unit_price_and_uses_customer_endpoint() -> None:
    assert "/api/master/customers/${this.orderForm.customer_id}/quote-preferences/estimate" in INDEX
    assert "manual_unit_price: item._manual_unit_price ? Number(item.unit_price) : null" in INDEX
    assert "if (!item._manual_unit_price && data?.estimated_unit_price != null)" in INDEX
    assert "markManualSizeUnitPrice(item)" in INDEX
    assert "客户平方价" in INDEX
    assert "manualSizeQuoteText(item)" in INDEX
    assert "输入材质代码筛选已保存偏好" in INDEX
    assert "直接选择材质" not in _order_template()
    assert "selectManualSizeMaterial(item)" not in INDEX
    assert 'min="0.0001"' in INDEX


def test_p1_03_customer_quote_preference_compact_maintenance_uses_versioned_api() -> None:
    assert "客户报价偏好" in INDEX
    assert "/api/master/customers/${customerId}/quote-preferences" in INDEX
    assert "tax_included_square_price" in INDEX
    assert "expected_version: Number(preference.version)" in INDEX
    assert "createCustomerQuotePreference" in INDEX
    assert "updateCustomerQuotePreference" in INDEX
    preference_block = INDEX[INDEX.index("customer-pricing-preferences"):INDEX.index("customer-pricing-preferences") + 7000]
    assert "修改原因" not in preference_block
    assert "is_default" not in preference_block


def test_quote_preference_uses_required_cascade_and_searchable_saved_material() -> None:
    start = INDEX.index('<section class="customer-pricing-preferences">')
    preference_block = INDEX[start:start + 12000]
    fields = [
        "customerQuotePreferenceDraft.box_type",
        "customerQuotePreferenceDraft.layer_count",
        "customerQuotePreferenceDraft.flute_type",
        "customerQuotePreferenceDraft.supplier_name",
        "customerQuotePreferenceDraft.material_id",
        "customerQuotePreferenceDraft.tax_included_square_price",
    ]
    positions = [preference_block.index(field) for field in fields]
    assert positions == sorted(positions)
    assert 'placeholder="输入材质代码筛选已有码"' in preference_block
    assert 'v-model.trim="customerQuotePreferenceDraft.flute_type"' not in preference_block
    assert "quotePreferenceSupplierOptions(customerQuotePreferenceDraft)" in preference_block
    assert "quotePreferenceMaterialOptions(customerQuotePreferenceDraft)" in preference_block
    assert "同一箱型可以新增多个供应商、多个材质代码作对比" in preference_block
    assert "除含税平方价外均从已建档数据下拉选择" in preference_block


def test_manual_size_only_uses_active_saved_customer_quote_preferences() -> None:
    block = _order_template()
    assert "manualSizeLayerOptions(item)" in block
    assert "manualSizeFluteOptions(item)" in block
    assert "manualSizeSupplierOptions(item)" in block
    assert "manualSizePreferenceSelectOptions(item)" in block
    assert "该客户没有已保存并启用的箱型报价偏好" in INDEX
    assert "必须从该客户已保存并启用的报价偏好中选择" in INDEX
    assert ':options="materialSelectOptions(\'\',null,null)"' not in block


def test_customer_editor_loads_material_options_on_demand_and_filters_inactive() -> None:
    assert "ensureCustomerQuotePreferenceOptions()" in INDEX
    assert "if (!m || m.is_active === false) return false;" in INDEX
    assert "正在读取供应商材质代码" in INDEX


def test_p1_03_new_quotation_uses_customer_pricing_preview_without_repricing_history() -> None:
    assert "customer_id:this.quotationCustomer?.id || null" in INDEX
    assert "line.customer_square_price = data.customer_square_price ?? null;" in INDEX
    assert "line.price_source = data.price_source || \"\";" in INDEX
    assert "customer_square_price:line.customer_square_price ?? null" in INDEX
    assert "this.quotationDraft = this.hydrateQuotation(response.data);" in INDEX


def test_p1_03_order_entry_keeps_p1_02_no_horizontal_scroll_contract() -> None:
    assert "overflow-x: hidden; overflow-y: auto;" in INDEX
    assert ".order-entry-modal.order-entry-large .manual-size-quote" in INDEX
    assert "grid-template-columns:repeat(3,minmax(0,1fr))" in INDEX


def test_p1_03_inline_javascript_is_syntactically_valid(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        raise AssertionError("Node.js is required for the frontend contract test")
    scripts = [
        script
        for script in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", INDEX, re.DOTALL)
        if script.strip()
    ]
    assert len(scripts) == 1
    target = tmp_path / "p1-03-index-inline.js"
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run([node, "--check", str(target)], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr
