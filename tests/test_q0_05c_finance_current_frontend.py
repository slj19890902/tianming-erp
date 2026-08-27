from pathlib import Path


INDEX = (
    Path(__file__).resolve().parents[1] / "static" / "index.html"
).read_text(encoding="utf-8")


def test_finance_defaults_to_customer_month_current_workbench() -> None:
    assert 'financeView: "current"' in INDEX
    assert 'financeFilters: { statement_month:month(), balance_type:"", customer_id:"" }' in INDEX
    assert 'axios.get("/api/finance/current-customer-months"' in INDEX
    assert "statement_month:this.financeFilters.statement_month || month()" in INDEX
    assert "balance_type:this.financeFilters.balance_type || undefined" in INDEX
    assert "customer_id:this.financeFilters.customer_id || undefined" in INDEX
    assert ':options="customerOptions" label-key="name" value-key="id" placeholder="全部客户"' in INDEX
    assert "financeCurrentState: { loading:false, error:\"\", loaded:false }" in INDEX
    assert "财务待办加载失败" in INDEX
    assert "本月没有需要处理的对账、开票或收款" in INDEX


def test_finance_customer_month_rows_expand_existing_statement_actions() -> None:
    assert "financeGroupKey(row)" in INDEX
    assert "`${row.statement_month}:${row.customer_id}`" in INDEX
    assert 'v-for="statement in row.statements"' in INDEX
    assert '@click="openStatementDetail(statement)"' in INDEX
    assert '@click="openStatementEdit(statement)"' in INDEX
    assert '@click="cancelStatement(statement)"' in INDEX
    assert '@click="exportStatement(statement)"' in INDEX
    assert 'v-if="canFinance" class="btn small" :disabled="Number(statement.pending_invoice_amount)<=0 || !!financeStatementOperationState.action" @click="registerInvoice(statement)"' in INDEX
    assert 'v-if="canFinance" class="btn small success" :disabled="Number(statement.pending_payment_amount)<=0 || !!financeStatementOperationState.action" @click="settle(statement)"' in INDEX
    assert "@click=\"setFinanceView('statements')\">全部单据" in INDEX
    assert '<div class="section-title">开票记录</div>' in INDEX
    assert 'axios.get("/api/finance/statements"' in INDEX
    assert 'axios.get("/api/finance/invoices"' in INDEX


def test_dashboard_finance_cards_route_to_same_grouped_filters() -> None:
    assert 'balance_type:"pending_reconciliation"' in INDEX
    assert "this.pages.financeCurrent = 1" in INDEX
    assert 'this.financeView = "current"' in INDEX
    finance_load = INDEX.split('if (page === "finance") await Promise.all([', 1)[1].split("]);", 1)[0]
    assert "requirePageLoad(this.loadCustomerOptions(force))" in finance_load
    assert "requirePageLoad(this.loadFinance())" in finance_load
