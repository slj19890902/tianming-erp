from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MOBILE = (ROOT / "static" / "mobile_erp.html").read_text(encoding="utf-8")
DASHBOARD_API = (ROOT / "app" / "api" / "dashboard.py").read_text(encoding="utf-8")
DEPS = (ROOT / "app" / "api" / "deps.py").read_text(encoding="utf-8")


def test_mobile_home_uses_effective_dashboard_permission_not_role_name() -> None:
    assert 'const canHome = permissions.includes("dashboard.view");' in MOBILE
    assert '&& ["admin", "boss"].includes(user.role)' not in MOBILE
    assert 'if (!(state.allowedPages.has("home")))' not in MOBILE
    assert "按当前账号权限只读显示" in MOBILE


def test_mobile_home_shows_all_authorized_workflow_cards_without_recalculation() -> None:
    assert 'apiGet("/api/dashboard/overview"' in MOBILE
    assert "Array.isArray(state.dashboard.cards) ? state.dashboard.cards : []" in MOBILE
    assert "state.dashboard.cards.slice" not in MOBILE
    assert "pending_payment" not in MOBILE
    assert "pending_invoice" not in MOBILE
    assert "pending_reconciliation" not in MOBILE


def test_dashboard_backend_remains_permission_and_customer_scoped() -> None:
    assert 'can_read = PermissionChecker("dashboard.view")' in DASHBOARD_API
    assert 'can_view_finance = has_permission(user, "finance.view")' in DASHBOARD_API
    assert "visible_customer_ids" in DASHBOARD_API
    assert '"finance": frozenset(' in DEPS
    assert '"dashboard.view"' in DEPS
    assert '"finance.view"' in DEPS

    from app.api.deps import has_permission
    from app.models.user import User

    finance_user = User(username="mobile-finance", role="finance")
    assert has_permission(finance_user, "dashboard.view")
    assert has_permission(finance_user, "finance.view")
    assert not has_permission(finance_user, "warehouse.view")


def test_mobile_finance_scope_stays_read_only() -> None:
    assert 'method: "POST"' not in MOBILE
    assert 'method: "PUT"' not in MOBILE
    assert 'method: "PATCH"' not in MOBILE
    assert 'method: "DELETE"' not in MOBILE
