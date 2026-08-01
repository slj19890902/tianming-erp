from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _report_section() -> str:
    start = INDEX.index('<template v-else-if="financeView===\'reports\'">')
    end = INDEX.index("<template v-else>", start)
    return INDEX[start:end]


def test_finance_keeps_four_views_and_reports_use_authoritative_api() -> None:
    assert "@click=\"setFinanceView('current')\">当前处理" in INDEX
    assert "@click=\"setFinanceView('settled_history')\">已结清历史" in INDEX
    assert "@click=\"setFinanceView('reports')\">月度年度报表" in INDEX
    assert "@click=\"setFinanceView('statements')\">全部单据" in INDEX
    assert "['current','settled_history','reports','statements'].includes(view)" in INDEX
    assert 'axios.get("/api/finance/reports/monthly-yearly"' in INDEX


def test_report_has_independent_filters_loading_error_and_empty_states() -> None:
    section = _report_section()
    assert "financeReportFilters.year" in section
    assert "financeReportFilters.statement_month" in section
    assert "financeReportFilters.customer_id" in section
    assert "financeReportState.loading" in section
    assert "financeReportState.error" in section
    assert "financeReportState.loaded" in section
    assert "这个月份没有待对账或正式财务记录" in section
    assert "当前没有未结余额" in section
    assert "该年度暂无对账、开票、收款或待对账记录" in section
    assert "included_through_statement_month" in section
    assert "this.financeReportData = { monthly:[]" in INDEX


def test_report_shows_month_drilldown_balance_ranking_and_four_aging_buckets() -> None:
    section = _report_section()
    assert "financeReportData.monthly" in section
    assert "selectFinanceReportMonth(row)" in section
    assert "financeReportData.selected_month_customers" in section
    assert "financeReportData.customer_balances" in section
    assert "selectFinanceReportCustomer(row)" in section
    assert "financeReportData.aging" in section
    assert "当月、1 个月、2 个月、3 个月以上" not in section
    assert "未结账龄按对账月份归类" in section


def test_report_is_read_only_and_does_not_add_chart_dependency() -> None:
    section = _report_section()
    for write_action in (
        "registerInvoice(",
        "settle(",
        "cancelStatement(",
        "openStatementEdit(",
        "openStatement(",
    ):
        assert write_action not in section
    assert "chart.js" not in INDEX.lower()
    assert "echarts" not in INDEX.lower()
    assert "grid-template-columns: repeat(6, minmax(0, 1fr))" in INDEX


def test_dashboard_finance_navigation_still_opens_current_workbench() -> None:
    assert 'this.financeView = "current"' in INDEX
