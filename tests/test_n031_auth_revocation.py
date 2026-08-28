from __future__ import annotations

from collections.abc import Generator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture()
def auth_revocation_context(tmp_path: Path):
    from app.api.auth import router
    from app.api.deps import get_db
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.customer import Customer
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "auth-revocation.sqlite3")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as session:
        session.add_all(
            [
                User(
                    username="admin",
                    password_hash=hash_password("AdminPass123!"),
                    role="admin",
                    real_name="Admin",
                    must_change_password=False,
                ),
                User(
                    username="workshop",
                    password_hash=hash_password("WorkshopPass123!"),
                    role="workshop",
                    real_name="Workshop",
                    must_change_password=False,
                ),
                Customer(name="N031 Test Customer"),
            ]
        )
        session.commit()

    app = FastAPI()
    app.include_router(router, prefix="/api/auth")

    def override_get_db() -> Generator[Session, None, None]:
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    return app, session_factory


def _login(client: TestClient, username: str, password: str) -> None:
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200


def _auth_version(session_factory, username: str) -> int:
    from app.models.user import User

    with session_factory() as session:
        user = session.query(User).filter_by(username=username).one()
        return user.auth_version


def test_session_tokens_require_current_auth_version(auth_revocation_context) -> None:
    from app.core.config import load_settings
    from app.core.security import create_session_token, decode_session_token

    app, session_factory = auth_revocation_context
    with TestClient(app) as client:
        _login(client, "workshop", "WorkshopPass123!")
        token = client.cookies["erp_session"]
        assert decode_session_token(token) == (2, 1)

        legacy_token = jwt.encode(
            {
                "sub": "2",
                "iat": datetime.now(timezone.utc),
                "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
                "type": "session",
            },
            load_settings().secret_key,
            algorithm="HS256",
        )
        client.cookies.clear()
        client.cookies.set("erp_session", legacy_token)
        assert client.get("/api/auth/me").status_code == 401

    assert _auth_version(session_factory, "workshop") == 1
    with pytest.raises(ValueError):
        create_session_token(2, auth_version=0)


def test_authenticated_request_reuses_startup_validated_settings(
    auth_revocation_context,
    monkeypatch,
) -> None:
    from app.api import auth as auth_api
    from app.api import deps as deps_api
    from app.core.config import load_settings
    from app.core import security

    app, _ = auth_revocation_context
    app.state.erp_settings = load_settings()

    def reject_reload():
        raise AssertionError("request hot path must reuse startup settings")

    monkeypatch.setattr(auth_api, "load_settings", reject_reload)
    monkeypatch.setattr(deps_api, "load_settings", reject_reload)
    monkeypatch.setattr(security, "load_settings", reject_reload)

    with TestClient(app) as client:
        _login(client, "workshop", "WorkshopPass123!")
        assert client.get("/api/auth/me").status_code == 200
        assert client.post("/api/auth/logout").status_code == 200


def test_logout_revokes_all_browser_sessions_and_clears_cookie(auth_revocation_context) -> None:
    app, session_factory = auth_revocation_context
    with TestClient(app) as first, TestClient(app) as second:
        _login(first, "workshop", "WorkshopPass123!")
        _login(second, "workshop", "WorkshopPass123!")

        logout = first.post("/api/auth/logout")
        assert logout.status_code == 200
        assert "Max-Age=0" in logout.headers["set-cookie"]
        assert second.get("/api/auth/me").status_code == 401

    assert _auth_version(session_factory, "workshop") == 2


def test_production_login_cookie_is_secure(
    auth_revocation_context,
    monkeypatch,
) -> None:
    from app.api import auth as auth_api
    from app.core.config import load_settings
    from app.main import CookieOriginCSRFMiddleware

    monkeypatch.setenv("ERP_ENVIRONMENT", "production")
    monkeypatch.setenv(
        "ERP_SECRET_KEY",
        "n031-secure-cookie-test-secret-longer-than-32-characters",
    )
    monkeypatch.setenv("ERP_ALLOWED_ORIGINS", "https://erp.example.com")
    monkeypatch.setenv("ERP_TRUSTED_HOSTS", "erp.example.com")
    monkeypatch.setenv("ERP_TRUSTED_PROXY_IPS", "127.0.0.1")
    monkeypatch.setenv("ERP_HEALTH_URL", "https://erp.example.com/api/health")
    monkeypatch.setenv("ERP_BROWSER_URL", "https://erp.example.com/")
    current = load_settings()
    monkeypatch.setattr(auth_api, "load_settings", lambda: current)

    app, _ = auth_revocation_context
    app.add_middleware(
        CookieOriginCSRFMiddleware,
        allowed_origins=current.allowed_origins,
        session_cookie_name=current.session_cookie_name,
    )
    with TestClient(app, base_url="https://erp.example.com") as client:
        response = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "AdminPass123!"},
        )
        missing_origin = client.post("/api/auth/logout")
        logout = client.post(
            "/api/auth/logout",
            headers={"Origin": "https://erp.example.com"},
        )

    assert response.status_code == 200
    set_cookie = response.headers["set-cookie"]
    assert "Secure" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie
    assert missing_origin.status_code == 403
    assert logout.status_code == 200


def test_lan_http_production_login_cookie_works_without_secure_attribute(
    auth_revocation_context,
    monkeypatch,
) -> None:
    from app.api import auth as auth_api
    from app.core.config import load_settings
    from app.main import CookieOriginCSRFMiddleware

    monkeypatch.setenv("ERP_ENVIRONMENT", "production")
    monkeypatch.setenv("ERP_PRODUCTION_TRANSPORT", "lan_http")
    monkeypatch.setenv(
        "ERP_SECRET_KEY",
        "n031-lan-cookie-test-secret-longer-than-32-characters",
    )
    monkeypatch.setenv("ERP_BIND_HOST", "0.0.0.0")
    monkeypatch.setenv("ERP_PORT", "8000")
    monkeypatch.setenv("ERP_ALLOWED_ORIGINS", "http://192.168.3.80:8000")
    monkeypatch.setenv("ERP_TRUSTED_HOSTS", "192.168.3.80,127.0.0.1,localhost")
    monkeypatch.delenv("ERP_TRUSTED_PROXY_IPS", raising=False)
    monkeypatch.setenv(
        "ERP_HEALTH_URL",
        "http://192.168.3.80:8000/api/health",
    )
    monkeypatch.setenv("ERP_BROWSER_URL", "http://192.168.3.80:8000/")
    monkeypatch.delenv("ERP_SESSION_COOKIE_SECURE", raising=False)
    current = load_settings()
    monkeypatch.setattr(auth_api, "load_settings", lambda: current)

    app, _ = auth_revocation_context
    app.add_middleware(
        CookieOriginCSRFMiddleware,
        allowed_origins=current.allowed_origins,
        session_cookie_name=current.session_cookie_name,
    )
    with TestClient(app, base_url="http://192.168.3.80:8000") as client:
        response = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "AdminPass123!"},
        )
        logout = client.post(
            "/api/auth/logout",
            headers={"Origin": "http://192.168.3.80:8000"},
        )

    assert response.status_code == 200
    set_cookie = response.headers["set-cookie"]
    assert "Secure" not in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie
    assert logout.status_code == 200


def test_security_relevant_user_updates_revoke_target_sessions(auth_revocation_context) -> None:
    app, session_factory = auth_revocation_context
    with TestClient(app) as admin_client, TestClient(app) as target_client:
        _login(admin_client, "admin", "AdminPass123!")
        _login(target_client, "workshop", "WorkshopPass123!")
        role_update = admin_client.put("/api/auth/users/2", json={"role": "finance"})
        assert role_update.status_code == 200
        assert target_client.get("/api/auth/me").status_code == 401
        responses = [
            admin_client.put(
                "/api/auth/users/2/permission-overrides",
                json={"overrides": {"finance.view": True}},
            ),
            admin_client.put(
                "/api/auth/users/2/customer-scopes",
                json={"mode": "selected", "customer_ids": [1]},
            ),
            admin_client.put(
                "/api/auth/users/2/access",
                json={
                    "mode": "selected",
                    "customer_ids": [1],
                    "overrides": {"orders.view": True},
                },
            ),
            admin_client.put("/api/auth/users/2", json={"is_active": False}),
            admin_client.put("/api/auth/users/2", json={"is_active": True}),
            admin_client.put(
                "/api/auth/users/workshop/reset-password",
                json={"new_password": "ResetPass123!"},
            ),
        ]

    assert [response.status_code for response in responses] == [200] * len(responses)
    assert _auth_version(session_factory, "workshop") == 8


def test_password_change_and_tianhua_tokens_follow_separate_paths(auth_revocation_context) -> None:
    from app.core.security import create_tianhua_pick_token, decode_tianhua_pick_token

    app, session_factory = auth_revocation_context
    with TestClient(app) as client:
        _login(client, "workshop", "WorkshopPass123!")
        changed = client.put(
            "/api/auth/password",
            json={
                "current_password": "WorkshopPass123!",
                "new_password": "ChangedPass123!",
            },
        )
        assert changed.status_code == 200
        assert client.get("/api/auth/me").status_code == 401

    token, _expires_at = create_tianhua_pick_token(11, 22)
    assert decode_tianhua_pick_token(token) == (11, 22)
    assert _auth_version(session_factory, "workshop") == 2
