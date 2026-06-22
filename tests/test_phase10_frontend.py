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


def test_phase10_frontend_enforces_auth_and_workshop_finance_masking() -> None:
    source = INDEX.read_text(encoding="utf-8")

    assert "axios.interceptors.response.use" in source
    assert "status === 401" in source
    assert "status === 403" in source
    assert "isWorkshop" in source
    assert 'v-if="!isWorkshop"' in source
    assert "sensitive-price" in source
    assert 'finance: ["dashboard", "customers", "orders", "finance"]' in source
    assert 'sales: ["dashboard", "customers", "orders", "requisition", "deliveries"]' in source
    assert 'workshop: ["dashboard", "orders", "deliveries"]' in source
    assert 'v-if="isWorkshop || canAdmin"' in source


def test_initial_session_probe_does_not_report_expired_login() -> None:
    source = INDEX.read_text(encoding="utf-8")

    assert "window.erpCheckingSession" in source
    assert "if (!window.erpCheckingSession)" in source


def test_role_switch_resets_active_page_to_dashboard() -> None:
    source = INDEX.read_text(encoding="utf-8")

    assert source.count('this.activePage = "dashboard";') >= 3


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
