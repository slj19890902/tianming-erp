from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VIEW = (ROOT / "tm_frontend" / "src" / "views" / "MasterDataView.vue").read_text(
    encoding="utf-8"
)
API = (ROOT / "tm_frontend" / "src" / "api" / "masterData.ts").read_text(
    encoding="utf-8"
)


def test_customer_table_uses_server_side_pagination() -> None:
    assert "<vxe-pager" in VIEW
    assert "v-model:current-page=\"customerPage.page\"" in VIEW
    assert ":total=\"totals.customers\"" in VIEW
    assert "@page-change=\"handleCustomerPageChange\"" in VIEW
    assert "page: customerPage.page" in VIEW
    assert "pageSize: customerPage.pageSize" in VIEW


def test_customer_inactive_visibility_and_actions_match_acceptance() -> None:
    assert "显示已停用客户" in VIEW
    assert "v-model=\"showInactiveCustomers\"" in VIEW
    assert ":row-class-name=\"customerRowClassName\"" in VIEW
    assert "inactive-customer-row" in VIEW
    assert "toggleCustomerStatus(row)" in VIEW
    assert "{{ row.is_active ? '停用' : '启用' }}" in VIEW
    assert 'field="is_active" title="状态"' not in VIEW
    assert "updateCustomerStatus" in API


def test_customer_default_columns_hide_payment_term_and_size_name_dynamically() -> None:
    assert ':width="customerNameColumnWidth"' in VIEW
    assert 'field="payment_term_days" title="账期"' not in VIEW
    assert "longestCustomerNameLength" in VIEW


def test_customer_api_accepts_paging_and_inactive_parameters() -> None:
    assert "includeInactive?: boolean" in API
    assert "page?: number" in API
    assert "pageSize?: number" in API
    assert "query.set('include_inactive'" in API
    assert "query.set('page'" in API
    assert "query.set('page_size'" in API
