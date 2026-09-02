from pathlib import Path


INDEX = Path("static/index.html").read_text(encoding="utf-8")


def _body(start: str, end: str) -> str:
    begin = INDEX.index(start)
    return INDEX[begin : INDEX.index(end, begin)]


def test_supplier_editor_exposes_individual_settlement_day_and_sends_it_to_api() -> None:
    supplier_dialog = _body(
        '<div class="field wide"><label>供应商全称 *',
        '<div v-else-if="modal.type === \'supplierPackagingCatalog\'">',
    )
    assert "对账日" in supplier_dialog
    assert 'min="1"' in supplier_dialog
    assert 'max="31"' in supplier_dialog
    assert 'v-model.number="supplierForm.settlement_day"' in supplier_dialog
    assert "settlement_day:20" in INDEX
    assert "settlement_day:Number(form.settlement_day" in INDEX


def test_supplier_settlement_workspace_shows_supplier_cycle_revision_and_regenerate() -> None:
    payables = _body(
        '<template v-else-if="financeView===\'payables\'">',
        '<template v-else-if="financeView===\'invoice_tasks\'">',
    )
    for text in (
        "供应商月结",
        "对账日",
        "结算周期",
        "版本",
        "重新生成",
        "历史版本",
    ):
        assert text in payables
    assert "row.document_revision" in payables
    assert "row.settlement_day" in payables
    assert "regenerateSupplierSettlement(row)" in payables
    assert "/regenerate" in INDEX
    assert "expected_version" in INDEX
    assert "idempotency_key" in INDEX


def test_supplier_payment_uses_one_combined_credit_acceptance_and_bank_action() -> None:
    payables = _body(
        '<template v-else-if="financeView===\'payables\'">',
        '<template v-else-if="financeView===\'invoice_tasks\'">',
    )
    for text in (
        "可用贷项",
        "承兑票据",
        "本期承兑抵付",
        "银行转账",
        "建议转账",
        "新形成贷项",
        "一次确认付款",
    ):
        assert text in payables
    assert "/payment-batches" in INDEX
    assert "credit_applications" in INDEX
    assert "acceptance_note_id" in INDEX
    assert "bank_amount" in INDEX
    assert "bank_reference" in INDEX
    assert "确认后正负调整" in payables
    assert "row.can_post_adjustment" in payables
    assert "差额自动形成该供应商贷项" in payables


def test_acceptance_customer_picker_explains_and_uses_recent_order_candidates() -> None:
    simple_finance = _body(
        "工资、房租和固定月供（只配置一次）",
        "费用明细与高级筛选",
    )
    assert "最近三个月" in simple_finance
    assert "有效订单" in simple_finance
    assert 'v-for="item in simpleFinanceMeta.customers"' in simple_finance
    assert 'v-model.number="financeAcceptanceForm.customer_id"' in simple_finance
    load = _body("async loadSimpleFinance() {", "applyFinanceRecurringTypeDefaults() {")
    assert "/api/finance/simple-finance/metadata" in load


def test_daily_utility_entry_is_one_combined_monthly_expense_with_legacy_details_only() -> None:
    simple_finance = _body(
        "工资、房租和固定月供（只配置一次）",
        "费用明细与高级筛选",
    )
    assert "水电费（每月一笔）" in simple_finance
    assert 'v-model="financeUtilityExpenseForm.cost_month"' in simple_finance
    assert 'v-model="financeUtilityExpenseForm.total_amount"' in simple_finance
    assert "保存本月水电费" in simple_finance
    assert "历史水费 / 电费明细" in simple_finance
    assert "/api/finance/simple-finance/utility-expenses" in INDEX
    assert "financeUtilityExpenseForm" in INDEX
