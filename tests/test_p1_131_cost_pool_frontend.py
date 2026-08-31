from pathlib import Path


INDEX = Path("static/index.html").read_text(encoding="utf-8")


def _method_body(start: str, end: str) -> str:
    return INDEX[INDEX.index(start) : INDEX.index(end, INDEX.index(start))]


def test_finance_keeps_four_primary_views_and_embeds_cost_pool_in_payables() -> None:
    nav_start = INDEX.index('<div class="page-head finance-page-head">')
    nav_end = INDEX.index('<template v-if="financeView===\'overview\'">', nav_start)
    nav = INDEX[nav_start:nav_end]
    assert nav.count("setFinanceView(") == 4
    for label in ("经营概览", "客户对账", "开票任务", "应付支出"):
        assert label in nav

    payables_start = INDEX.index('<template v-else-if="financeView===\'payables\'">')
    payables_end = INDEX.index(
        '<template v-else-if="financeView===\'invoice_tasks\'">', payables_start
    )
    payables = INDEX[payables_start:payables_end]
    assert "月度成本费用" in payables
    assert "供应商应付登记（原应付流程）" in payables
    assert "不会自动重复计入上方成本费用" in payables
    assert 'ref="financeCostFileInput"' in payables
    assert payables.count("下载模板") == 1
    assert payables.count("导入 Excel") == 1
    assert payables.count("导出月表") == 1


def test_cost_pool_ui_keeps_decimal_strings_and_manual_confirmation() -> None:
    save_body = _method_body("async saveFinanceCost() {", "async transitionFinanceCost(row, action) {")
    assert "amount:String(form.amount)" in save_body
    assert "tax_amount:String(form.tax_amount" in save_body
    assert "Number(form.amount||0)<=0" in save_body
    assert 'source_reference:String(form.source_reference).trim()' in save_body
    assert 'axios.post("/api/finance/cost-pool",payload)' in save_body

    transition_body = _method_body(
        "async transitionFinanceCost(row, action) {", "saveFinanceCostWorkbook(response,filename) {"
    )
    assert "expected_version:Number(row.version)" in transition_body
    assert "idempotency_key:attempt.idempotencyKey" in transition_body
    assert "确认后这笔费用将锁定" in transition_body
    assert 'prompt("请输入作废原因")' in transition_body


def test_cost_pool_excel_flow_previews_before_apply_and_has_permissions() -> None:
    import_body = _method_body(
        "async onFinanceCostFileSelected(event) {", "payableCategoryText(value) {"
    )
    assert 'previewForm.append("apply","false")' in import_body
    assert "preview.error_count" in import_body
    assert "if (!confirm(" in import_body
    assert 'applyForm.append("apply","true")' in import_body
    assert 'applyForm.append("idempotency_key",createIdempotencyKey())' in import_body

    for permission in (
        "finance.cost.manage",
        "finance.cost.confirm",
        "finance.cost.export",
    ):
        assert permission in INDEX


def test_cost_pool_permissions_paging_and_latest_request_are_fail_closed() -> None:
    assert 'canViewFinanceCosts() { return this.hasPermission("finance.view")' in INDEX
    assert 'this.hasPermission("cost.view")' in INDEX
    assert 'this.user?.unrestricted_customer_access === true' in INDEX
    assert 'v-if="canViewFinanceCosts" class="panel finance-filter-panel"' in INDEX
    assert 'class="cost-sensitive">¥ {{ money(financeCostStatusAmount' in INDEX
    assert ':page="pages.financeCosts"' in INDEX

    load_body = _method_body("async loadFinanceCosts() {", "editFinanceCost(row) {")
    assert 'const requestKey = "finance:costs:list"' in load_body
    assert "page:Math.max(1,Number(this.pages.financeCosts || 1))" in load_body
    assert "page_size:this.pageSize" in load_body
    assert "signal:controller.signal" in load_body
    assert "financeCostRequestIsCurrent" in load_body
    assert "this.financeCostSummary = {status_totals:{}" in load_body


def test_manual_cost_save_reuses_idempotency_key_after_uncertain_outcome() -> None:
    save_body = _method_body("async saveFinanceCost() {", "async transitionFinanceCost(row, action) {")
    assert "const signature = JSON.stringify" in save_body
    assert "this.financeCostSaveAttempt.signature !== signature" in save_body
    assert "payload.idempotency_key = this.financeCostSaveAttempt.idempotencyKey" in save_body
    assert "outcomeUncertain = !error?.response" in save_body
    assert "按原编号重试" in INDEX

    reset_body = _method_body("resetPagePerformanceState() {", "pageCacheFresh(page) {")
    assert "this.financeCostItems = []" in reset_body
    assert 'this.financeCostSaveAttempt = {signature:"",idempotencyKey:"",outcomeUncertain:false}' in reset_body


def test_standard_mobile_shell_contains_overflow_inside_navigation_and_tables() -> None:
    mobile_start = INDEX.index("/* P1-131: the desktop shell must remain usable")
    mobile_end = INDEX.index("</style>", mobile_start)
    mobile_css = INDEX[mobile_start:mobile_end]

    assert "@media (max-width: 760px)" in mobile_css
    assert "body:has(.erp-enterprise-ui)" in mobile_css
    assert "min-width: 0" in mobile_css
    assert ".erp-enterprise-ui .layout" in mobile_css
    assert "flex-direction: column" in mobile_css
    assert ".erp-enterprise-ui .sidebar" in mobile_css
    assert "overflow-x: auto" in mobile_css
    assert ".erp-enterprise-ui .finance-page-head > .toolbar-group" in mobile_css
    assert "grid-template-columns: repeat(2, minmax(0, 1fr))" in mobile_css
    assert ".erp-enterprise-ui .finance-cost-grid { grid-template-columns: 1fr; }" in mobile_css
    assert ".erp-enterprise-ui .table-wrap" in mobile_css
