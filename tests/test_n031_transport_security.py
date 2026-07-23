from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _set_production_urls(monkeypatch, origin: str = "https://erp.example.com") -> None:
    monkeypatch.delenv("ERP_PRODUCTION_TRANSPORT", raising=False)
    monkeypatch.setenv("ERP_HEALTH_URL", f"{origin}/api/health")
    monkeypatch.setenv("ERP_BROWSER_URL", f"{origin}/")
    if not os.environ.get("ERP_TRUSTED_HOSTS"):
        monkeypatch.setenv("ERP_TRUSTED_HOSTS", origin.split("//", 1)[1])
    if not os.environ.get("ERP_TRUSTED_PROXY_IPS"):
        monkeypatch.setenv("ERP_TRUSTED_PROXY_IPS", "127.0.0.1")


def _set_lan_http_production(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ERP_ENVIRONMENT", "production")
    monkeypatch.setenv("ERP_PRODUCTION_TRANSPORT", "lan_http")
    monkeypatch.setenv(
        "ERP_SECRET_KEY",
        "n031-lan-http-secret-that-is-longer-than-32-characters",
    )
    monkeypatch.setenv("ERP_DATABASE_PATH", str(tmp_path / "missing.sqlite3"))
    monkeypatch.setenv("ERP_BIND_HOST", "0.0.0.0")
    monkeypatch.setenv("ERP_PORT", "8000")
    monkeypatch.setenv("ERP_ALLOWED_ORIGINS", "http://192.168.3.80:8000")
    monkeypatch.setenv(
        "ERP_TRUSTED_HOSTS",
        "192.168.3.80,PC-20250926DZYH,127.0.0.1,localhost",
    )
    monkeypatch.delenv("ERP_TRUSTED_PROXY_IPS", raising=False)
    monkeypatch.setenv(
        "ERP_HEALTH_URL",
        "http://192.168.3.80:8000/api/health",
    )
    monkeypatch.setenv("ERP_BROWSER_URL", "http://192.168.3.80:8000/")
    monkeypatch.delenv("ERP_SESSION_COOKIE_SECURE", raising=False)


def _production_app(monkeypatch, tmp_path):
    monkeypatch.setenv("ERP_ENVIRONMENT", "production")
    monkeypatch.setenv("ERP_SECRET_KEY", "n031-test-secret-that-is-longer-than-32-characters")
    monkeypatch.setenv("ERP_DATABASE_PATH", str(tmp_path / "missing.sqlite3"))
    monkeypatch.setenv("ERP_TRUSTED_HOSTS", "testserver")
    monkeypatch.setenv("ERP_ALLOWED_ORIGINS", "https://testserver")
    _set_production_urls(monkeypatch, "https://testserver")
    from app.main import create_app

    return create_app()


def test_health_failure_is_503_and_never_leaks_storage_details(monkeypatch, tmp_path):
    app = _production_app(monkeypatch, tmp_path)
    with TestClient(app, base_url="https://testserver") as client:
        response = client.get("/api/health")

    assert response.status_code == 503
    assert response.json() == {"ok": False}
    for forbidden in ("database", "sqlite", "table", "exception", str(tmp_path)):
        assert forbidden not in response.text.lower()


def test_production_enforces_https_hsts_and_trusted_host(monkeypatch, tmp_path):
    app = _production_app(monkeypatch, tmp_path)
    with TestClient(app, base_url="http://testserver", follow_redirects=False) as client:
        redirected = client.get("/api/health")
    with TestClient(app, base_url="https://untrusted.example", follow_redirects=False) as client:
        rejected = client.get("/api/health")
    with TestClient(app, base_url="http://untrusted.example", follow_redirects=False) as client:
        rejected_before_redirect = client.get("/api/health")

    assert redirected.status_code in {301, 307, 308}
    assert redirected.headers["location"].startswith("https://")
    assert redirected.headers["strict-transport-security"] == "max-age=63072000"
    assert redirected.headers["x-content-type-options"] == "nosniff"
    assert redirected.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert redirected.headers["x-frame-options"] == "DENY"
    assert redirected.headers["permissions-policy"] == (
        "camera=(), microphone=(), geolocation=()"
    )
    assert rejected.status_code == 400
    assert rejected_before_redirect.status_code == 400
    assert "location" not in rejected_before_redirect.headers


def test_production_cors_requires_explicit_origins_and_proxy_trust(monkeypatch, tmp_path):
    from app.core.config import load_settings

    monkeypatch.setenv("ERP_ENVIRONMENT", "production")
    monkeypatch.setenv("ERP_SECRET_KEY", "n031-test-secret-that-is-longer-than-32-characters")
    monkeypatch.setenv("ERP_ALLOWED_ORIGINS", "https://erp.example.com")
    monkeypatch.setenv("ERP_TRUSTED_PROXY_IPS", "127.0.0.1")
    _set_production_urls(monkeypatch)
    settings = load_settings()

    assert settings.allowed_origins == ("https://erp.example.com",)
    assert settings.allowed_origin_regex is None
    assert settings.trusted_proxy_ips == ("127.0.0.1",)

    monkeypatch.setenv("ERP_TRUSTED_HOSTS", "testserver")
    monkeypatch.setenv("ERP_DATABASE_PATH", str(tmp_path / "missing.sqlite3"))
    from app.main import create_app

    app = create_app()
    with TestClient(app, base_url="https://testserver") as client:
        allowed = client.get("/api/health", headers={"Origin": "https://erp.example.com"})
        blocked = client.get("/api/health", headers={"Origin": "http://192.168.1.20:8000"})

    assert allowed.headers["access-control-allow-origin"] == "https://erp.example.com"
    assert "access-control-allow-origin" not in blocked.headers

    monkeypatch.setenv("ERP_ALLOWED_ORIGINS", "*")
    try:
        load_settings()
    except ValueError as error:
        assert "ERP_ALLOWED_ORIGINS" in str(error)
    else:
        raise AssertionError("production wildcard origin was accepted")

    monkeypatch.setenv("ERP_ALLOWED_ORIGINS", "https://erp.example.com")
    monkeypatch.setenv("ERP_TRUSTED_PROXY_IPS", "*")
    try:
        load_settings()
    except ValueError as error:
        assert "ERP_TRUSTED_PROXY_IPS" in str(error)
    else:
        raise AssertionError("wildcard proxy trust was accepted")


def test_production_cookie_writes_require_an_allowed_origin(monkeypatch, tmp_path):
    app = _production_app(monkeypatch, tmp_path)
    with TestClient(app, base_url="https://testserver") as client:
        client.cookies.set("erp_session", "invalid-session-token")
        missing = client.post("/api/auth/logout")
        blocked = client.post(
            "/api/auth/logout",
            headers={"Origin": "https://attacker.example"},
        )
        null_origin = client.post(
            "/api/auth/logout",
            headers={"Origin": "null"},
        )
        allowed = client.post(
            "/api/auth/logout",
            headers={"Origin": "https://testserver"},
        )
        safe_read = client.get("/api/auth/me")

    assert missing.status_code == 403
    assert blocked.status_code == 403
    assert null_origin.status_code == 403
    assert missing.json()["detail"].startswith("安全校验失败")
    assert allowed.status_code == 401
    assert safe_read.status_code == 401


def test_production_unauthenticated_write_is_not_treated_as_csrf(
    monkeypatch,
    tmp_path,
):
    app = _production_app(monkeypatch, tmp_path)
    with TestClient(app, base_url="https://testserver") as client:
        response = client.post("/api/n031-not-found")

    assert response.status_code == 404


def test_production_rejects_non_loopback_trusted_proxy(monkeypatch):
    from app.core.config import load_settings

    monkeypatch.setenv("ERP_ENVIRONMENT", "production")
    monkeypatch.setenv(
        "ERP_SECRET_KEY",
        "n031-test-secret-that-is-longer-than-32-characters",
    )
    monkeypatch.setenv("ERP_ALLOWED_ORIGINS", "https://erp.example.com")
    monkeypatch.setenv("ERP_TRUSTED_PROXY_IPS", "10.0.0.10")
    _set_production_urls(monkeypatch)

    with pytest.raises(ValueError, match="loopback"):
        load_settings()


def test_lan_http_production_is_explicit_private_and_does_not_trust_proxy(
    monkeypatch,
    tmp_path,
):
    from app.core.config import load_settings

    _set_lan_http_production(monkeypatch, tmp_path)
    current = load_settings()

    assert current.production_transport == "lan_http"
    assert current.uses_https_proxy is False
    assert current.bind_host == "0.0.0.0"
    assert current.allowed_origins == ("http://192.168.3.80:8000",)
    assert current.trusted_proxy_ips == ()
    assert current.session_cookie_secure is False


def test_lan_http_production_keeps_host_origin_and_security_header_gates(
    monkeypatch,
    tmp_path,
):
    _set_lan_http_production(monkeypatch, tmp_path)
    from app.main import create_app

    app = create_app()
    middleware_names = {item.cls.__name__ for item in app.user_middleware}
    assert "HTTPSRedirectMiddleware" not in middleware_names
    assert "ProxyHeadersMiddleware" not in middleware_names
    assert {
        "CookieOriginCSRFMiddleware",
        "TrustedHostMiddleware",
        "HSTSMiddleware",
    } <= middleware_names

    with TestClient(app, base_url="http://192.168.3.80:8000") as client:
        allowed = client.get(
            "/api/health",
            headers={"Origin": "http://192.168.3.80:8000"},
        )
        client.cookies.set("erp_session", "invalid-session-token")
        blocked_write = client.post(
            "/api/auth/logout",
            headers={"Origin": "http://192.168.3.81:8000"},
        )
    with TestClient(app, base_url="http://192.168.3.81:8000") as client:
        untrusted_host = client.get("/api/health")

    assert allowed.status_code == 503
    assert "location" not in allowed.headers
    assert "strict-transport-security" not in allowed.headers
    assert allowed.headers["x-content-type-options"] == "nosniff"
    assert allowed.headers["x-frame-options"] == "DENY"
    assert allowed.headers["access-control-allow-origin"] == (
        "http://192.168.3.80:8000"
    )
    assert blocked_write.status_code == 403
    assert untrusted_host.status_code == 400


def test_production_only_allows_same_origin_embedded_warehouse_frame(
    monkeypatch,
    tmp_path,
):
    app = _production_app(monkeypatch, tmp_path)
    with TestClient(app, base_url="https://testserver") as client:
        embedded = client.get("/warehouse.html?embedded=1")
        standalone = client.get("/warehouse.html")
        dashboard = client.get("/")

    assert embedded.status_code == 200
    assert embedded.headers["x-frame-options"] == "SAMEORIGIN"
    assert embedded.headers["content-security-policy"] == "frame-ancestors 'self'"
    assert standalone.headers["x-frame-options"] == "DENY"
    assert "content-security-policy" not in standalone.headers
    assert dashboard.headers["x-frame-options"] == "DENY"


@pytest.mark.parametrize(
    ("name", "value", "match"),
    (
        (
            "ERP_PRODUCTION_TRANSPORT",
            "public_http",
            "ERP_PRODUCTION_TRANSPORT",
        ),
        ("ERP_ALLOWED_ORIGINS", "http://8.8.8.8:8000", "私网"),
        ("ERP_TRUSTED_HOSTS", "*.factory.local", "explicit hosts"),
        ("ERP_TRUSTED_PROXY_IPS", "127.0.0.1", "禁止"),
        ("ERP_BIND_HOST", "8.8.8.8", "私网"),
        ("ERP_BROWSER_URL", "http://192.168.3.80:8001/", "ERP_PORT"),
        (
            "ERP_HEALTH_URL",
            "http://192.168.3.81:8000/api/health",
            "ERP_ALLOWED_ORIGINS",
        ),
        ("ERP_SESSION_COOKIE_SECURE", "true", "必须关闭"),
    ),
)
def test_lan_http_production_rejects_unsafe_or_inconsistent_config(
    monkeypatch,
    tmp_path,
    name,
    value,
    match,
):
    from app.core.config import load_settings

    _set_lan_http_production(monkeypatch, tmp_path)
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=match):
        load_settings()


def test_proxy_headers_only_trust_loopback_tcp_peer() -> None:
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    async def capture(peer: str) -> tuple[str, tuple[str, int]]:
        observed: dict[str, object] = {}

        async def downstream(scope, _receive, send):
            observed["scheme"] = scope["scheme"]
            observed["client"] = scope["client"]
            await send(
                {
                    "type": "http.response.start",
                    "status": 204,
                    "headers": [],
                }
            )
            await send({"type": "http.response.body", "body": b""})

        middleware = ProxyHeadersMiddleware(
            downstream,
            trusted_hosts=["127.0.0.1", "::1"],
        )
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/probe",
            "raw_path": b"/probe",
            "query_string": b"",
            "headers": [
                (b"x-forwarded-proto", b"https"),
                (b"x-forwarded-for", b"203.0.113.50"),
            ],
            "client": (peer, 43210),
            "server": ("127.0.0.1", 8000),
        }

        async def receive():
            return {"type": "http.disconnect"}

        async def send(_message):
            return None

        await middleware(scope, receive, send)
        return observed["scheme"], observed["client"]

    assert asyncio.run(capture("10.0.0.20")) == (
        "http",
        ("10.0.0.20", 43210),
    )
    assert asyncio.run(capture("127.0.0.1")) == (
        "https",
        ("203.0.113.50", 0),
    )


def test_loopback_http_health_bypasses_only_public_https_redirect() -> None:
    from app.main import HTTPSRedirectMiddleware

    async def invoke(peer: str, host: str, path: str) -> int:
        async def downstream(_scope, _receive, send):
            await send(
                {
                    "type": "http.response.start",
                    "status": 204,
                    "headers": [],
                }
            )
            await send({"type": "http.response.body", "body": b""})

        middleware = HTTPSRedirectMiddleware(downstream)
        status_code = 0
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "root_path": "",
            "query_string": b"",
            "headers": [(b"host", host.encode())],
            "client": (peer, 43210),
            "server": ("127.0.0.1", 8000),
        }

        async def receive():
            return {"type": "http.disconnect"}

        async def send(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]

        await middleware(scope, receive, send)
        return status_code

    assert asyncio.run(invoke("127.0.0.1", "127.0.0.1:8000", "/api/health")) == 204
    assert asyncio.run(invoke("10.0.0.20", "erp.example.com", "/api/health")) in {
        307,
        308,
    }


def test_production_defaults_to_loopback_and_fails_closed_without_secret(
    monkeypatch,
    tmp_path,
):
    from app.core.config import load_settings

    monkeypatch.setenv("ERP_ENVIRONMENT", "production")
    monkeypatch.setenv("ERP_SECRET_KEY", "n031-test-secret-that-is-longer-than-32-characters")
    monkeypatch.setenv("ERP_ALLOWED_ORIGINS", "https://erp.example.com")
    monkeypatch.delenv("ERP_BIND_HOST", raising=False)
    _set_production_urls(monkeypatch)
    settings = load_settings()
    assert settings.bind_host == "127.0.0.1"

    monkeypatch.delenv("ERP_SECRET_KEY", raising=False)
    monkeypatch.setenv("ERP_SECRET_KEY_FILE", str(tmp_path / "missing-session-secret.key"))
    try:
        load_settings()
    except RuntimeError as error:
        assert "ERP_SECRET_KEY" in str(error)
    else:
        raise AssertionError("production started without an explicit or provisioned secret")


def test_production_rejects_plain_http_cors_origin(monkeypatch):
    from app.core.config import load_settings

    monkeypatch.setenv("ERP_ENVIRONMENT", "production")
    monkeypatch.setenv("ERP_SECRET_KEY", "n031-test-secret-that-is-longer-than-32-characters")
    monkeypatch.setenv("ERP_ALLOWED_ORIGINS", "http://erp.example.com")
    _set_production_urls(monkeypatch)
    try:
        load_settings()
    except ValueError as error:
        assert "HTTPS" in str(error)
    else:
        raise AssertionError("production accepted a plain HTTP CORS origin")


def test_unknown_environment_and_non_loopback_production_bind_fail_closed(monkeypatch):
    from app.core.config import load_settings

    monkeypatch.setenv("ERP_ENVIRONMENT", "staging")
    with pytest.raises(ValueError, match="ERP_ENVIRONMENT"):
        load_settings()

    monkeypatch.setenv("ERP_ENVIRONMENT", "production")
    monkeypatch.setenv("ERP_SECRET_KEY", "n031-test-secret-that-is-longer-than-32-characters")
    monkeypatch.setenv("ERP_ALLOWED_ORIGINS", "https://erp.example.com")
    monkeypatch.setenv("ERP_BIND_HOST", "0.0.0.0")
    _set_production_urls(monkeypatch)
    with pytest.raises(ValueError, match="loopback"):
        load_settings()


def test_development_keeps_lan_http_runtime_defaults(monkeypatch):
    from app.core.config import load_settings

    monkeypatch.setenv("ERP_ENVIRONMENT", "development")
    monkeypatch.setenv("ERP_ALLOWED_ORIGINS", "http://192.168.10.20:8000")
    monkeypatch.setenv("ERP_PORT", "8000")
    for name in (
        "ERP_BIND_HOST",
        "ERP_HEALTH_URL",
        "ERP_BROWSER_URL",
        "ERP_TRUSTED_HOSTS",
        "ERP_TRUSTED_PROXY_IPS",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = load_settings()

    assert settings.bind_host == "0.0.0.0"
    assert settings.workers == 1
    assert settings.allowed_origins == ("http://192.168.10.20:8000",)
    assert settings.health_url == "http://127.0.0.1:8000/api/health"
    assert settings.browser_url == "http://127.0.0.1:8000/"
    assert settings.session_cookie_secure is False


def test_login_throttle_worker_contract_fails_closed(monkeypatch, tmp_path):
    from app.core.config import load_settings

    monkeypatch.setenv("ERP_ENVIRONMENT", "production")
    monkeypatch.setenv(
        "ERP_SECRET_KEY",
        "worker-contract-production-secret-longer-than-32-characters",
    )
    monkeypatch.setenv("ERP_DATABASE_PATH", str(tmp_path / "production.sqlite3"))
    monkeypatch.setenv("ERP_ALLOWED_ORIGINS", "https://erp.example.com")
    monkeypatch.setenv("ERP_TRUSTED_HOSTS", "erp.example.com")
    monkeypatch.setenv("ERP_TRUSTED_PROXY_IPS", "127.0.0.1")
    monkeypatch.setenv("ERP_HEALTH_URL", "https://erp.example.com/api/health")
    monkeypatch.setenv("ERP_BROWSER_URL", "https://erp.example.com/")
    monkeypatch.setenv("ERP_WORKERS", "2")

    with pytest.raises(ValueError, match="ERP_WORKERS"):
        load_settings()


@pytest.mark.parametrize(
    "missing_name",
    (
        "ERP_ALLOWED_ORIGINS",
        "ERP_TRUSTED_HOSTS",
        "ERP_TRUSTED_PROXY_IPS",
        "ERP_HEALTH_URL",
        "ERP_BROWSER_URL",
    ),
)
def test_production_required_transport_config_is_fail_closed(monkeypatch, missing_name):
    from app.core.config import load_settings

    monkeypatch.setenv("ERP_ENVIRONMENT", "production")
    monkeypatch.setenv("ERP_SECRET_KEY", "n031-test-secret-that-is-longer-than-32-characters")
    monkeypatch.setenv("ERP_ALLOWED_ORIGINS", "https://erp.example.com")
    monkeypatch.setenv("ERP_TRUSTED_HOSTS", "erp.example.com")
    monkeypatch.setenv("ERP_TRUSTED_PROXY_IPS", "127.0.0.1")
    monkeypatch.setenv("ERP_HEALTH_URL", "https://erp.example.com/api/health")
    monkeypatch.setenv("ERP_BROWSER_URL", "https://erp.example.com/")
    monkeypatch.delenv(missing_name)

    with pytest.raises(ValueError, match=missing_name):
        load_settings()


def test_root_main_exports_the_same_hardened_application(monkeypatch, tmp_path):
    project_root = Path(__file__).resolve().parents[1]
    environment = {
        **os.environ,
        "ERP_ENVIRONMENT": "production",
        "ERP_PRODUCTION_TRANSPORT": "https_proxy",
        "ERP_SECRET_KEY": "n031-root-entrypoint-secret-that-is-longer-than-32-characters",
        "ERP_DATABASE_PATH": str(tmp_path / "missing.sqlite3"),
        "ERP_TRUSTED_HOSTS": "testserver",
        "ERP_ALLOWED_ORIGINS": "https://testserver",
        "ERP_TRUSTED_PROXY_IPS": "127.0.0.1",
        "ERP_HEALTH_URL": "https://testserver/api/health",
        "ERP_BROWSER_URL": "https://testserver/",
    }
    check = (
        "import main; "
        "names = {item.cls.__name__ for item in main.app.user_middleware}; "
        "assert {'CORSMiddleware', 'TrustedHostMiddleware', "
        "'HTTPSRedirectMiddleware', 'HSTSMiddleware', "
        "'CookieOriginCSRFMiddleware'} <= names"
    )

    result = subprocess.run(
        [sys.executable, "-c", check],
        cwd=project_root,
        env=environment,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_handoff_documents_proxy_header_overwrite_contract() -> None:
    handoff = (
        Path(__file__).resolve().parents[1] / "docs" / "CODEX_HANDOFF.md"
    ).read_text(encoding="utf-8")

    for marker in (
        "覆盖（overwrite）而不是追加",
        "Host",
        "X-Forwarded-Proto",
        "X-Forwarded-For",
        "127.0.0.1",
        "::1",
    ):
        assert marker in handoff
