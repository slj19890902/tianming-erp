from pathlib import Path


INDEX = Path("static/index.html").read_text(encoding="utf-8")


def _section(start: str, end: str) -> str:
    start_index = INDEX.index(start)
    return INDEX[start_index : INDEX.index(end, start_index)]


def test_finance_page_leads_with_four_simple_expense_groups() -> None:
    start = INDEX.index("每月只看四类支出")
    end = INDEX.index("费用明细与高级筛选", start)
    panel = INDEX[start:end]
    for label in ("材料采购", "人员工资", "运费", "固定 / 经营费用"):
        assert label in panel
    assert "生成本月固定草稿" in panel
    assert "导出材料核票" in panel
    assert "纸板" in panel and "外购包材" in panel and "模具/油墨等" in panel
    assert "发票差" in panel
    assert "查看材料账单 / 发票差异具体位置" in panel
    assert "承兑不计作银行现金" not in panel  # the wording is rendered from API
    assert "不冒充银行现金" in panel


def test_fixed_expense_utility_and_acceptance_forms_are_compact_and_actionable() -> None:
    start = INDEX.index("工资、房租和固定月供（只配置一次）")
    end = INDEX.index("费用明细与高级筛选", start)
    panel = INDEX[start:end]
    for text in (
        "基本工资",
        "津贴",
        "公司社保",
        "固定扣款",
        "默认 380000，末月自动消化尾差",
        "上月表数",
        "本月表数",
        "发票金额（优先）",
        "已付 ¥",
        "row.payment_date",
        "客户承兑与供应商背书",
        "背书抵付",
    ):
        assert text in panel
    assert 'v-model="financeRecurringForm.is_active"' in panel
    assert 'v-model.number="row._statementId"' in panel
    assert ':disabled="Number(item.available_payment_amount)<Number(row.amount)"' in panel
    assert "余额不足" in panel


def test_new_cost_entry_categories_hide_outsourcing_but_keep_history_mapping() -> None:
    entry_form = _section("{{ financeCostForm.id ? '编辑费用草稿'", "费用明细")
    assert "financeCostMeta.input_categories" in entry_form
    options = _section("payableCategoryOptions() {", "financePayableAgingRows() {")
    assert 'value:"outsourcing"' not in options
    assert 'label:"人员工资"' in options
    assert 'label:"模具 / 油墨 / 其他材料"' in options
    assert 'outsourcing:"历史外协加工"' in INDEX


def test_simple_finance_calls_are_loaded_with_payables_and_keep_acceptance_non_cash() -> None:
    load_finance = _section("async loadFinance() {", "toggleFinanceCurrentGroup(row) {")
    assert "this.loadSimpleFinance()" in load_finance
    load_simple = _section("async loadSimpleFinance() {", "applyFinanceRecurringTypeDefaults() {")
    for endpoint in (
        "/api/finance/simple-finance/metadata",
        "/api/finance/simple-finance/summary",
        "/api/finance/simple-finance/recurring-rules",
        "/api/finance/simple-finance/utility-readings",
        "/api/finance/simple-finance/acceptances",
    ):
        assert endpoint in load_simple

    endorse = _section("async endorseFinanceAcceptance(row) {", "async transitionFinanceAcceptance")
    assert "不是银行现金" in endorse
    assert "/endorse" in endorse
    assert "supplier_statement_id" in endorse
    assert "expected_version" in endorse
    assert "available_payment_amount" in endorse
    assert "确认应付或已收发票余额不足" in endorse

    export = _section("async exportFinanceMaterialReconciliation() {", "payableCategoryText(value) {")
    assert "/api/finance/simple-finance/material-export" in export
    assert 'responseType:"blob"' in export
