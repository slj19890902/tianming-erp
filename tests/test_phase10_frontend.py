from pathlib import Path


INDEX = Path(__file__).resolve().parents[1] / "static" / "index.html"


def test_phase10_frontend_has_no_seed_or_simulated_business_data() -> None:
    source = INDEX.read_text(encoding="utf-8")

    forbidden = (
        "const seed",
        "clone(seed)",
        "已模拟",
        "前端模拟流程",
        "苏州普明电器配件有限公司",
        "SO20260530001",
    )
    for marker in forbidden:
        assert marker not in source


def test_phase10_frontend_uses_core_real_api_contracts() -> None:
    source = INDEX.read_text(encoding="utf-8")

    required = (
        "/api/dashboard/kpi",
        "/api/dashboard/overview",
        "/api/master/customers",
        "/api/master/products",
        "/api/master/materials",
        "/api/orders",
        "/api/deliveries",
        "/api/finance/return_receipts",
        "/api/finance/pending_statements",
        "/api/finance/statements",
        "/api/finance/invoices",
        "/settle",
        "window.open(`/delivery-print.html?id=${",
    )
    for marker in required:
        assert marker in source


def test_dashboard_frontend_uses_plain_language_workflow_cards() -> None:
    source = INDEX.read_text(encoding="utf-8")

    for marker in (
        "overview.cards",
        "overview.todos",
        "overviewError",
        "dashboard-cards",
        "todo-list",
        "go(card.target)",
        "go(todo.target)",
        "当前没有紧急待办",
    ):
        assert marker in source


def test_dashboard_frontend_shows_grouped_reconciliation_todo_fields() -> None:
    source = INDEX.read_text(encoding="utf-8")

    for marker in (
        "todo.month",
        "todo.count",
        "todo.amount",
        "todo.action_text",
        "todo.first_order_no",
        "todo.first_item_no",
        "overview.remaining_todo_count",
        "todo-meta",
        "todo-type",
    ):
        assert marker in source


def test_phase10_frontend_enforces_auth_and_workshop_finance_masking() -> None:
    source = INDEX.read_text(encoding="utf-8")

    assert "axios.interceptors.response.use" in source
    assert "status === 401" in source
    assert "status === 403" in source
    assert "isWorkshop" in source
    assert 'v-if="!isWorkshop"' in source
    assert "sensitive-price" in source
    assert 'finance: ["dashboard", "customers", "orders", "finance"]' in source
    assert 'sales: ["dashboard", "customers", "products", "orders"]' in source
    assert (
        'workshop: ["dashboard", "orders", "incoming", "production", '
        '"warehouse", "deliveries"]'
    ) in source
    assert (
        'v-else-if="row.source_type!==\'stock_replenishment\' '
        '&& row.incoming_status!==\'已作废\' && (isWorkshop || canAdmin)"'
    ) in source


def test_initial_session_probe_does_not_report_expired_login() -> None:
    source = INDEX.read_text(encoding="utf-8")

    assert "window.erpCheckingSession" in source
    assert "if (!window.erpCheckingSession)" in source


def test_auth_reset_uses_dashboard_but_login_preserves_deep_link() -> None:
    source = INDEX.read_text(encoding="utf-8")

    auth_reset = source.split("window.erpAuthRequired = () => {", 1)[1].split(
        "};", 1
    )[0]
    logout = source.split("async logout() {", 1)[1].split("},", 1)[0]
    assert 'this.activePage = "dashboard";' in auth_reset
    assert 'this.activePage = "dashboard";' in logout
    assert "const initialPage = this.initialPageFromLocation();" in source
    assert "this.activePage = initialPage;" in source


def test_core_lists_keep_server_side_pagination() -> None:
    source = INDEX.read_text(encoding="utf-8")

    assert 'app.component("pager"' in source
    assert "page: this.pages.products" in source
    assert "page: this.pages.orders" in source
    assert "page: this.pages.deliveries" in source


def test_frontend_has_safe_password_recovery_and_forced_change_flow() -> None:
    source = INDEX.read_text(encoding="utf-8")

    assert "忘记密码" in source
    assert "/api/auth/password" in source
    assert "/reset-password" in source
    assert "must_change_password" in source


def test_v0210_entry_efficiency_controls_are_visible() -> None:
    source = INDEX.read_text(encoding="utf-8")

    assert 'placeholder="存货编码 / 款号"' in source
    assert "showProductMoreFilters" in source
    assert "更多筛选" in source
    assert ".report-size-line .input { width: 94px; min-width: 82px; }" in source
    assert "当前为手工尺寸" in source
    assert "批量确认入库" in source
    assert "/api/incoming/batch-receive" in source
    assert "toggleAllIncoming" in source


def test_pdf_import_uses_join_button_without_duplicate_confirmation_checkbox() -> None:
    source = INDEX.read_text(encoding="utf-8")
    footer_start = source.index('<div class="modal-foot">')
    footer_end = source.index("</div>", footer_start)
    footer = source[footer_start:footer_end]

    close_index = footer.index("@click=\"closeModal\"")
    confirm_index = footer.index("加入批量保存")
    save_index = footer.index("批量保存已确认草稿")
    assert close_index < confirm_index < save_index
    assert "我已核对客户、产品、数量、材质、价格等信息" not in source
    assert ':disabled="loading || !confirmedImportDraftCount"' in footer
    assert "已加入批量保存：{{ confirmedImportDraftCount }} 条" in footer
    assert "toggleConfirmableImportDrafts" in source


def test_pdf_import_shows_customer_match_status_and_candidates() -> None:
    source = INDEX.read_text(encoding="utf-8")

    for marker in (
        "识别客户原文",
        "系统匹配客户",
        "候选客户",
        "未匹配到客户，请手工选择",
        "customer_match_status",
        "customer_candidates",
    ):
        assert marker in source
