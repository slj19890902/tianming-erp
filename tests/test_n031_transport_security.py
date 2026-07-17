from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient


def _production_app(monkeypatch, tmp_path):
    monkeypatch.setenv("ERP_ENVIRONMENT", "production")
    monkeypatch.setenv("ERP_SECRET_KEY", "n031-test-secret-that-is-longer-than-32-characters")
    monkeypatch.setenv("ERP_DATABASE_PATH", str(tmp_path / "missing.sqlite3"))
    monkeypatch.setenv("ERP_TRUSTED_HOSTS", "testserver")
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
    assert rejected.status_code == 400
    assert rejected_before_redirect.status_code == 400
    assert "location" not in rejected_before_redirect.headers


def test_production_cors_requires_explicit_origins_and_proxy_trust(monkeypatch, tmp_path):
    from app.core.config import load_settings

    monkeypatch.setenv("ERP_ENVIRONMENT", "production")
    monkeypatch.setenv("ERP_SECRET_KEY", "n031-test-secret-that-is-longer-than-32-characters")
    monkeypatch.setenv("ERP_ALLOWED_ORIGINS", "https://erp.example.com")
    monkeypatch.setenv("ERP_TRUSTED_PROXY_IPS", "10.0.0.10")
    settings = load_settings()

    assert settings.allowed_origins == ("https://erp.example.com",)
    assert settings.allowed_origin_regex is None
    assert settings.trusted_proxy_ips == ("10.0.0.10",)

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


def test_production_defaults_to_loopback_and_fails_closed_without_secret(
    monkeypatch,
    tmp_path,
):
    from app.core.config import load_settings

    monkeypatch.setenv("ERP_ENVIRONMENT", "production")
    monkeypatch.setenv("ERP_SECRET_KEY", "n031-test-secret-that-is-longer-than-32-characters")
    monkeypatch.delenv("ERP_BIND_HOST", raising=False)
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
    try:
        load_settings()
    except ValueError as error:
        assert "HTTPS" in str(error)
    else:
        raise AssertionError("production accepted a plain HTTP CORS origin")


def test_root_main_exports_the_same_hardened_application(monkeypatch, tmp_path):
    project_root = Path(__file__).resolve().parents[1]
    environment = {
        **os.environ,
        "ERP_ENVIRONMENT": "production",
        "ERP_SECRET_KEY": "n031-root-entrypoint-secret-that-is-longer-than-32-characters",
        "ERP_DATABASE_PATH": str(tmp_path / "missing.sqlite3"),
        "ERP_TRUSTED_HOSTS": "testserver",
    }
    check = (
        "import main; "
        "names = {item.cls.__name__ for item in main.app.user_middleware}; "
        "assert {'CORSMiddleware', 'TrustedHostMiddleware', "
        "'HTTPSRedirectMiddleware', 'HSTSMiddleware'} <= names"
    )

    result = subprocess.run(
        [sys.executable, "-c", check],
        cwd=project_root,
        env=environment,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
