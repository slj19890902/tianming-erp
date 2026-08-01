from pathlib import Path


INDEX = (
    Path(__file__).resolve().parents[1] / "static" / "index.html"
).read_text(encoding="utf-8")


def _history_template() -> str:
    start = INDEX.index('<template v-else-if="financeView===\'settled_history\'">')
    end = INDEX.index('<template v-else>', start)
    return INDEX[start:end]


def test_finance_keeps_current_history_and_all_document_views() -> None:
    assert "@click=\"setFinanceView('current')\">当前处理" in INDEX
    assert (
        "@click=\"setFinanceView('settled_history')\">已结清历史" in INDEX
    )
    assert "@click=\"setFinanceView('statements')\">全部单据" in INDEX
    assert "全部对账单（历史分区将在下一阶段完善）" not in INDEX
    assert '<div class="section-title">开票记录</div>' in INDEX
    assert 'axios.get("/api/finance/current-customer-months"' in INDEX
    assert 'axios.get("/api/finance/statements"' in INDEX
    assert 'axios.get("/api/finance/invoices"' in INDEX


def test_settled_history_has_independent_state_filters_and_paging() -> None:
    assert "financeSettledFilters:" in INDEX
    assert "financeSettledRows: []" in INDEX
    assert "financeSettledState: { loading:false, error:\"\", loaded:false }" in INDEX
    assert "financeSettledExpanded: {}" in INDEX
    assert "financeSettled: 1" in INDEX
    assert 'axios.get("/api/finance/settled-customer-months"' in INDEX
    for parameter in (
        "year:filters.year",
        "statement_month:filters.statement_month",
        "customer_id:filters.customer_id",
        "statement_number:filters.statement_number",
        "invoice_number:filters.invoice_number",
        "settlement_date_from:filters.settlement_date_from",
        "settlement_date_to:filters.settlement_date_to",
    ):
        assert parameter in INDEX
    assert "pages.financeSettled = 1" in INDEX


def test_settled_history_groups_by_stable_year_month_customer_key() -> None:
    history = _history_template()
    assert "`${row.year}:${row.statement_month}:${row.customer_id}`" in INDEX
    assert 'v-for="row in financeSettledRows"' in history
    assert "年度 → 月份 → 客户" in history
    assert 'v-for="statement in row.statements"' in history
    assert 'v-for="invoice in statement.invoices"' in history
    assert 'v-for="payment in statement.settlements"' in history
    assert '@click="openStatementDetail(statement)"' in history
    assert '@click="exportStatement(statement)"' in history
    assert "openStatementEdit(statement)" not in history
    assert "cancelStatement(statement)" not in history
    assert "registerInvoice(statement)" not in history
    assert "settle(statement)" not in history


def test_settled_history_distinguishes_loading_error_and_empty_states() -> None:
    history = _history_template()
    assert "正在读取已结清历史" in history
    assert "已结清历史加载失败" in history
    assert "请检查连接或筛选条件后重新查询" in history
    assert "当前条件下没有已结清记录" in history
    assert "financeSettledState.loaded && !financeSettledRows.length" in history


def test_finance_view_switch_keeps_dashboard_on_current_workbench() -> None:
    assert "['current','settled_history','reports','statements'].includes(view)" in INDEX
    assert 'this.financeView = "current";' in INDEX
    assert "this.pages.financeCurrent = 1" in INDEX
