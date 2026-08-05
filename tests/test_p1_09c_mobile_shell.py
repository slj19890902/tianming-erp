from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MOBILE_HTML = (ROOT / "static" / "mobile_erp.html").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def test_mobile_shell_hides_navigation_until_permissions_are_known() -> None:
    assert 'id="bottomNav"' in MOBILE_HTML
    assert '<nav id="bottomNav" class="bottom-nav"' in MOBILE_HTML
    assert 'aria-label="手机主导航" hidden' in MOBILE_HTML
    assert "allowedPages: new Set()" in MOBILE_HTML
    assert 'permissions.includes("warehouse.view")' in MOBILE_HTML
    assert 'permissions.includes("orders.view") && permissions.includes("incoming.view")' in MOBILE_HTML
    assert "state.allowedPages.has(page)" in MOBILE_HTML


def test_mobile_shell_keeps_only_safe_authorized_page_targets() -> None:
    assert "function safeRequestedPage()" in MOBILE_HTML
    assert 'new Set(["home", "search", "production"])' in MOBILE_HTML
    assert "window.location.hash" in MOBILE_HTML
    assert "window.history.replaceState" in MOBILE_HTML
    assert 'window.addEventListener("hashchange"' in MOBILE_HTML
    assert "visibleNav[0].dataset.page" in MOBILE_HTML
    assert 'target === "/mobile/erp.html"' in INDEX_HTML
    assert 'new URLSearchParams(window.location.search).get("mobile_page")' in INDEX_HTML
    assert '["home", "search", "production"].includes(mobilePage)' in INDEX_HTML


def test_mobile_shell_retries_without_parallel_initialization_or_home_requests() -> None:
    assert "if (state.initializing) return;" in MOBILE_HTML
    assert "state.homeController?.abort();" in MOBILE_HTML
    assert "generation !== state.homeGeneration" in MOBILE_HTML
    assert "无法连接 ERP 服务，请检查网络后重试" in MOBILE_HTML
    assert "当前账号没有可用的手机只读入口" in MOBILE_HTML


def test_mobile_shell_remains_read_only() -> None:
    assert 'method: "POST"' not in MOBILE_HTML
    assert 'method: "PUT"' not in MOBILE_HTML
    assert 'method: "PATCH"' not in MOBILE_HTML
    assert 'method: "DELETE"' not in MOBILE_HTML
