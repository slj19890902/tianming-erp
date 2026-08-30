from pathlib import Path


INDEX = (
    Path(__file__).resolve().parents[1] / "static" / "index.html"
).read_text(encoding="utf-8")


def test_finance_defaults_to_customer_month_current_workbench() -> None:
    assert 'financeView: "overview"' in INDEX
    assert 'financeFilters: { statement_month:month(), balance_type:"", customer_id:"" }' in INDEX
    assert 'axios.get("/api/finance/current-customer-months"' in INDEX
    assert "statement_month:this.financeFilters.statement_month || month()" in INDEX
    assert "balance_type:this.financeFilters.balance_type || undefined" in INDEX
    assert "customer_id:this.financeFilters.customer_id || undefined" in INDEX
    assert "经营概览" in INDEX and "客户对账" in INDEX
    assert "financeCurrentState: { loading:false, error:\"\", loaded:false }" in INDEX
    assert "财务待办加载失败" in INDEX
    assert "本月没有需要处理的对账或开票" in INDEX


def test_finance_customer_month_rows_expand_existing_statement_actions() -> None:
    assert "financeGroupKey(row)" in INDEX
    assert "`${row.statement_month}:${row.customer_id}`" in INDEX
    assert 'v-for="statement in row.statements"' in INDEX
    assert '@click="openStatementDetail(statement)"' in INDEX
    assert '@click="confirmFinanceStatement(statement)"' in INDEX
    assert '@click="generateInvoiceTask(statement)"' in INDEX
    assert "@click=\"exportStatement(statement,'xlsx')\"" in INDEX
    assert "@click=\"exportStatement(statement,'pdf')\"" in INDEX
    assert '@click="openStatementDispute(statement)"' in INDEX
    assert "@click=\"setFinanceView('invoice_tasks')\">开票任务" in INDEX
    assert 'axios.get("/api/finance/statements"' in INDEX
    assert 'axios.get("/api/finance/invoice-tasks"' in INDEX


def test_dashboard_finance_cards_route_to_same_grouped_filters() -> None:
    assert 'balance_type:"pending_reconciliation"' in INDEX
    assert "this.pages.financeCurrent = 1" in INDEX
    assert 'this.financeView = "current"' in INDEX
    finance_load = INDEX.split('if (page === "finance") await Promise.all([', 1)[1].split("]);", 1)[0]
    assert "requirePageLoad(this.loadCustomerOptions(force))" in finance_load
    assert "requirePageLoad(this.loadFinance())" in finance_load
