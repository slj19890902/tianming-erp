"""Targeted guards for FINUI001's invoice-task customer picker."""

from pathlib import Path


HTML_PATH = Path("static/index.html")


def _invoice_tasks_template() -> str:
    html = HTML_PATH.read_text(encoding="utf-8")
    return html.split("<template v-else-if=\"financeView==='invoice_tasks'\">", 1)[1].split(
        "<template v-else-if=\"financeView==='statements'\">", 1
    )[0]


def test_invoice_customer_filter_escapes_panel_clipping_without_changing_binding() -> None:
    html = HTML_PATH.read_text(encoding="utf-8")
    invoice = _invoice_tasks_template()

    # The filter panel must opt into the existing finance overflow layer.  A
    # plain .panel clips the absolutely positioned search-select list.
    assert '<div class="panel finance-filter-panel"' in invoice
    assert ".finance-filter-panel { overflow:visible; position:relative; z-index:25; }" in html
    assert ".finance-filter-panel .search-select-list { z-index:500; }" in html

    # Preserve the existing customer state, loading gate, and query action.
    assert 'v-model="invoiceTaskFilters.customer_id"' in invoice
    assert ':options="customerOptions"' in invoice
    assert ':disabled="invoiceTaskState.loading"' in invoice
    assert '@click="loadInvoiceTasks"' in invoice


def test_invoice_customer_filter_keeps_permission_and_api_controls() -> None:
    invoice = _invoice_tasks_template()

    assert 'v-if="canManageInvoiceProfiles"' in invoice
    assert "openInvoiceSellers" in invoice
    assert "invoiceTaskFilters.statement_month" in invoice
    assert "invoiceTaskFilters.status" in invoice
