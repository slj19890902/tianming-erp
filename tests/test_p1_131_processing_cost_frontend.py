from __future__ import annotations

from pathlib import Path


INDEX = Path("static/index.html").read_text(encoding="utf-8")


def _method_body(start: str, end: str) -> str:
    begin = INDEX.index(start)
    return INDEX[begin : INDEX.index(end, begin)]


def test_processing_parameters_use_one_compact_readable_entry() -> None:
    finance_start = INDEX.index('<template v-else-if="financeView===\'payables\'">')
    finance_end = INDEX.index(
        '<template v-else-if="financeView===\'invoice_tasks\'">', finance_start
    )
    payables = INDEX[finance_start:finance_end]

    assert payables.count("@click=\"toggleFinanceProcessingPanel\"") == 1
    assert 'v-if="canViewFinanceCosts" class="btn small"' in payables
    assert 'v-if="canViewFinanceCosts && financeProcessingOpen"' in payables
    for label in ("人工", "印刷", "模切", "组装", "产品例外"):
        assert f"<span>{label}</span>" in payables
    assert "代替日常 Excel 汇总" not in payables
    assert "金额按 Excel 原数保存，不自动转换为正式会计凭证" not in payables


def test_processing_parameters_follow_shared_erp_typography_and_large_mode() -> None:
    assert '<div class="finance-processing-title">加工参数</div>' in INDEX
    assert '<span class="status blue finance-processing-status">' in INDEX

    compact_css_start = INDEX.index(".finance-processing-panel {")
    compact_css_end = INDEX.index(".finance-month-workbench {", compact_css_start)
    compact_css = INDEX[compact_css_start:compact_css_end]

    assert "height:32px" not in compact_css
    assert "font-size:11px" not in compact_css
    assert "font-size:12px" not in compact_css
    assert ".finance-processing-grid .field label" not in compact_css
    assert ".finance-processing-grid .input" in compact_css
    assert "min-width:0;width:100%" in compact_css
    assert ".ui-large .finance-processing-title," in INDEX
    assert ".ui-large .finance-processing-group-title { font-size: 16px; }" in INDEX


def test_default_printer_and_product_override_modes_are_not_mixed() -> None:
    assert 'v-for="item in financeProcessingDefaultPrinterModes"' in INDEX
    assert 'financeProcessingDefaultPrinterModes:[{value:"new"' in INDEX
    assert '{value:"old",label:"旧印刷机"}' in INDEX
    assert 'financeProcessingPrinterModes:[{value:"auto"' in INDEX
    for value in ("none", "small_normal", "small_complex", "large", "oversize"):
        assert f'value:"{value}"' in INDEX


def test_processing_writes_keep_cas_idempotency_and_backend_assembly_recheck() -> None:
    settings = _method_body(
        "async saveFinanceProcessingSettings() {",
        "async loadFinanceProcessingProfiles() {",
    )
    assert 'expected_version = Number(form.version || 0)' in settings
    assert "payload.idempotency_key = this.financeProcessingSettingsAttempt.idempotencyKey" in settings
    assert 'axios.put("/api/finance/processing-settings",payload)' in settings
    assert 'average_worker_monthly_social_cost = String' in settings
    assert '|| "0"' in settings

    profile = _method_body(
        "async saveFinanceProcessingProfile() {",
        "async deleteFinanceProcessingProfile() {",
    )
    for field in (
        "assembly_workers",
        "assembly_days",
        "assembly_output_quantity",
    ):
        assert f"payload.{field}" in profile
    assert "组装换算请填完整" in profile
    assert "expected_version:Number(form.version || 0)" in profile
    assert "payload.idempotency_key" in profile


def test_processing_profile_can_restore_defaults_and_auth_reset_clears_cost_state() -> None:
    delete = _method_body(
        "async deleteFinanceProcessingProfile() {",
        "financeCostStatusAmount(status) {",
    )
    assert "axios.delete(`/api/finance/product-processing-profiles/${Number(form.product_id)}`" in delete
    assert "expected_version:Number(form.version || 0)" in delete
    assert "idempotency_key" in delete
    assert "恢复产品默认加工参数" in delete

    reset = _method_body("resetPagePerformanceState() {", "pageCacheFresh(page) {")
    assert "this.financeProcessingOpen = false" in reset
    assert "this.financeProcessingSettings = blankFinanceProcessingSettings()" in reset
    assert "this.financeProcessingProfiles = []" in reset
    assert "this.financeProcessingSettingsAttempt" in reset
    assert "this.financeProcessingProfileAttempt" in reset


def test_standard_processing_cost_nulls_render_as_pending_not_zero() -> None:
    for field in ("processing_total_cost", "processing_unit_cost"):
        assert (
            f"orderProcessingCostText(item.estimated_cost_breakdown.{field})" in INDEX
        )
        assert f"money(item.estimated_cost_breakdown.{field})" not in INDEX

    formatter = _method_body(
        "orderProcessingCostText(value) {", "orderMaterialCostComponentText(component) {"
    )
    assert 'value === null || value === undefined || value === ""' in formatter
    assert 'return "待完善"' in formatter
    assert "return `¥ ${this.money(value)}`" in formatter
