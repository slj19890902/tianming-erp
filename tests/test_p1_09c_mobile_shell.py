from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MOBILE_HTML = (ROOT / "static" / "mobile_erp.html").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def test_mobile_shell_hides_navigation_until_permissions_are_known() -> None:
    assert 'id="bottomNav"' in MOBILE_HTML
    assert '<nav id="bottomNav" class="bottom-nav"' in MOBILE_HTML
    assert 'aria-label="手机主导航" hidden' in MOBILE_HTML
    assert "allowedPages: new Set()" in MOBILE_HTML
    assert 'state.shell = await apiGet("/api/mobile/erp/shell")' in MOBILE_HTML
    assert "const allowedEntryIds = new Set(entries.map(entry => entry.id));" in MOBILE_HTML
    assert "state.allowedPages.has(page)" in MOBILE_HTML


def test_mobile_shell_keeps_only_safe_authorized_page_targets() -> None:
    assert "function safeRequestedPage()" in MOBILE_HTML
    assert 'new Set(["home", "incoming", "warehouse", "production", "pre_delivery"])' in MOBILE_HTML
    assert 'const requested = raw === "search" ? "warehouse" : raw;' in MOBILE_HTML
    assert "window.location.hash" in MOBILE_HTML
    assert "window.history.replaceState" in MOBILE_HTML
    assert 'window.addEventListener("hashchange"' in MOBILE_HTML
    assert 'const firstPage = state.allowedPages.has(requestedPage) ? requestedPage : "home";' in MOBILE_HTML
    assert 'target === "/mobile/erp.html"' in INDEX_HTML
    assert 'new URLSearchParams(window.location.search).get("mobile_page")' in INDEX_HTML
    assert '["home", "incoming", "warehouse", "production", "pre_delivery"].includes(safeMobilePage)' in INDEX_HTML


def test_mobile_shell_retries_without_parallel_initialization_or_home_requests() -> None:
    assert "if (state.initializing) return;" in MOBILE_HTML
    assert "state.homeController?.abort();" in MOBILE_HTML
    assert "generation !== state.homeGeneration" in MOBILE_HTML
    assert "无法连接 ERP 服务，请检查网络后重试" in MOBILE_HTML
    assert "当前账号没有可用的手机现场入口" in MOBILE_HTML
    assert 'method: "POST"' in MOBILE_HTML and '"/api/auth/logout"' in MOBILE_HTML


def test_mobile_shell_only_posts_for_explicit_logout() -> None:
    assert MOBILE_HTML.count('method: "POST"') == 2
    assert 'fetch("/api/auth/logout"' in MOBILE_HTML
    assert 'method: "PUT"' not in MOBILE_HTML
    assert 'method: "PATCH"' not in MOBILE_HTML
    assert 'method: "DELETE"' not in MOBILE_HTML
