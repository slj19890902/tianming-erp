from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def test_order_chain_menu_keeps_one_visible_count_summary() -> None:
    menu = INDEX[
        INDEX.index('<div class="menu-title">业务中心</div>') :
        INDEX.index("</aside>")
    ]
    assert ':title="item.countDetails' in menu
    assert '<span v-if="item.countDetails" class="menu-count-details">' not in menu
    assert ".erp-enterprise-ui .menu-count-summary { flex: 0 0 auto; }" in INDEX


def test_enterprise_primary_buttons_keep_white_text_on_blue() -> None:
    assert (
        ".erp-enterprise-ui .btn.primary { background: #155eef; "
        "border-color: #155eef; color: #fff; box-shadow: none; }"
    ) in INDEX
    assert (
        ".erp-enterprise-ui .btn.primary:hover { background: #004eeb; "
        "border-color: #004eeb; color: #fff; }"
    ) in INDEX


def test_standard_reported_and_incoming_tables_remain_compact() -> None:
    assert ".erp-enterprise-ui.ui-standard .reported-item-table { font-size: 13px; }" in INDEX
    assert INDEX.count('class="reported-item-multiply">×') == 2
    assert "text-overflow: clip; text-align: center;" in INDEX
    assert ".erp-enterprise-ui.ui-standard .incoming-compact-table th" in INDEX


def test_finance_customer_filter_uses_current_month_options_and_all_first() -> None:
    finance = INDEX[
        INDEX.index("<template v-if=\"financeView==='current'\">") :
        INDEX.index("<template v-else-if=\"financeView==='settled_history'\">")
    ]
    assert ':options="financeCurrentCustomerOptions"' in finance
    assert 'placeholder="全部"' in finance
    assert ':allow-empty="true"' in finance
    assert '@change="onFinanceMonthChange"' in finance
    assert "this.financeCurrentCustomerOptions = data.customer_options || [];" in INDEX


def test_customer_tax_profile_explains_and_opens_seller_maintenance() -> None:
    customer_modal = INDEX[
        INDEX.index("<div v-else-if=\"modal.type === 'customer'\">") :
        INDEX.index("<div v-else-if=\"modal.type === 'supplier'\">")
    ]
    assert "尚未维护销方主体，请先录入两个公司开票抬头" in customer_modal
    assert '@click="openInvoiceSellersFromCustomer"' in customer_modal
    assert "async openInvoiceSellersFromCustomer()" in INDEX
    assert 'this.modal?.type === "invoiceSellers"' in INDEX
