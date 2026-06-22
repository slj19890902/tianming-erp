from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture()
def auth_context(tmp_path: Path):
    from app.api.auth import router
    from app.api.deps import get_db
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "auth.sqlite3")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as session:
        session.add_all(
            [
                User(
                    username="admin",
                    password_hash=hash_password("AdminPass123!"),
                    role="admin",
                    real_name="系统管理员",
                    display_name="系统管理员",
                    must_change_password=True,
                ),
                User(
                    username="workshop",
                    password_hash=hash_password("WorkshopPass123!"),
                    role="workshop",
                    real_name="车间仓库",
                    display_name="车间仓库",
                    must_change_password=False,
                ),
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


def test_login_sets_http_only_cookie_returns_me_and_writes_audit(auth_context) -> None:
    from app.models.audit import OperationLog

    app, session_factory = auth_context
    with TestClient(app) as client:
        login = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "AdminPass123!"},
        )

        assert login.status_code == 200
        assert login.json()["user"] == {
            "id": 1,
            "username": "admin",
            "role": "admin",
            "real_name": "系统管理员",
            "display_name": "系统管理员",
            "must_change_password": True,
        }
        set_cookie = login.headers["set-cookie"]
        assert "erp_session=" in set_cookie
        assert "HttpOnly" in set_cookie
        assert "SameSite=lax" in set_cookie
        assert "Max-Age=" not in set_cookie

        me = client.get("/api/auth/me")
        assert me.status_code == 200
        assert me.json()["user"]["role"] == "admin"

    with session_factory() as session:
        log = session.scalar(select(OperationLog))
        assert log is not None
        assert log.user_id == 1
        assert log.action == "LOGIN"
        assert log.resource == "User"
        assert '"username": "admin"' in (log.details or "")


def test_remember_login_sets_30_day_http_only_cookie(auth_context) -> None:
    app, _ = auth_context
    with TestClient(app) as client:
        login = client.post(
            "/api/auth/login",
            json={
                "username": "admin",
                "password": "AdminPass123!",
                "remember_me": True,
            },
        )

    assert login.status_code == 200
    set_cookie = login.headers["set-cookie"]
    assert "HttpOnly" in set_cookie
    assert "Max-Age=2592000" in set_cookie


def test_invalid_login_is_rejected_without_cookie(auth_context) -> None:
    app, _ = auth_context
    with TestClient(app) as client:
        response = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "wrong-password"},
        )

    assert response.status_code == 401
    assert response.json()["detail"] == "用户名或密码错误"
    assert "erp_session=" not in response.headers.get("set-cookie", "")


def test_logout_clears_cookie_and_me_requires_login(auth_context) -> None:
    app, _ = auth_context
    with TestClient(app) as client:
        assert client.get("/api/auth/me").status_code == 401
        assert (
            client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "AdminPass123!"},
            ).status_code
            == 200
        )

        logout = client.post("/api/auth/logout")
        assert logout.status_code == 200
        assert "erp_session=" in logout.headers["set-cookie"]
        assert "Max-Age=0" in logout.headers["set-cookie"]
        assert client.get("/api/auth/me").status_code == 401


def test_user_can_change_own_password_and_must_supply_current_password(
    auth_context,
) -> None:
    app, _ = auth_context
    with TestClient(app) as client:
        client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "AdminPass123!"},
        )
        denied = client.put(
            "/api/auth/password",
            json={
                "current_password": "wrong-password",
                "new_password": "NewAdminPass456!",
            },
        )
        changed = client.put(
            "/api/auth/password",
            json={
                "current_password": "AdminPass123!",
                "new_password": "NewAdminPass456!",
            },
        )
        client.post("/api/auth/logout")
        old_login = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "AdminPass123!"},
        )
        new_login = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "NewAdminPass456!"},
        )

    assert denied.status_code == 400
    assert changed.status_code == 200
    assert changed.json()["user"]["must_change_password"] is False
    assert old_login.status_code == 401
    assert new_login.status_code == 200


def test_admin_can_reset_account_password_and_non_admin_cannot(auth_context) -> None:
    from app.models.audit import OperationLog

    app, session_factory = auth_context
    with TestClient(app) as client:
        client.post(
            "/api/auth/login",
            json={"username": "workshop", "password": "WorkshopPass123!"},
        )
        denied = client.put(
            "/api/auth/users/admin/reset-password",
            json={"new_password": "TemporaryPass789!"},
        )
        client.post("/api/auth/logout")
        client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "AdminPass123!"},
        )
        reset = client.put(
            "/api/auth/users/workshop/reset-password",
            json={"new_password": "TemporaryPass789!"},
        )
        client.post("/api/auth/logout")
        login = client.post(
            "/api/auth/login",
            json={"username": "workshop", "password": "TemporaryPass789!"},
        )

    assert denied.status_code == 403
    assert reset.status_code == 200
    assert reset.json()["user"]["must_change_password"] is True
    assert login.status_code == 200
    with session_factory() as session:
        log = session.scalar(
            select(OperationLog).where(
                OperationLog.action == "RESET_PASSWORD",
                OperationLog.resource == "User",
            )
        )
        assert log is not None
        assert '"username": "workshop"' in (log.details or "")


def test_role_checker_allows_listed_role_and_rejects_other_role(auth_context) -> None:
    from app.api.deps import RoleChecker

    app, _ = auth_context

    @app.get("/finance-only")
    def finance_only(
        _user=Depends(RoleChecker(["admin", "finance"])),
    ) -> dict[str, bool]:
        return {"ok": True}

    with TestClient(app) as client:
        client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "AdminPass123!"},
        )
        assert client.get("/finance-only").status_code == 200
        client.post("/api/auth/logout")
        client.post(
            "/api/auth/login",
            json={"username": "workshop", "password": "WorkshopPass123!"},
        )
        denied = client.get("/finance-only")

    assert denied.status_code == 403
    assert denied.json()["detail"] == "权限不足"


def test_session_token_rejects_tampering() -> None:
    from app.core.security import create_session_token, decode_session_token

    token = create_session_token(7, secret_key="test-secret", expires_minutes=10)

    assert decode_session_token(token, secret_key="test-secret") == 7
    with pytest.raises(ValueError, match="登录凭证无效或已过期"):
        decode_session_token(token + "tampered", secret_key="test-secret")


def test_modular_app_registers_auth_routes_and_configured_cors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "configured.sqlite3"
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database_path))
    monkeypatch.setenv(
        "ERP_ALLOWED_ORIGINS",
        "http://192.168.10.20:8000,http://192.168.10.21:8000",
    )
    import main as legacy
    from app.main import create_app

    application = create_app()
    paths = {route.path for route in application.routes}
    cors = next(
        middleware
        for middleware in application.user_middleware
        if middleware.cls.__name__ == "CORSMiddleware"
    )

    assert {
        "/api/auth/login",
        "/api/auth/logout",
        "/api/auth/me",
        "/api/customers",
    } <= paths
    assert cors.kwargs["allow_credentials"] is True
    assert cors.kwargs["allow_origins"] == [
        "http://192.168.10.20:8000",
        "http://192.168.10.21:8000",
    ]
    assert legacy.db_path() == database_path.resolve()


def test_modular_app_customer_alias_requires_authentication(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "customer-alias.sqlite3"
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database_path))
    from app.main import create_app

    application = create_app()

    with TestClient(application) as client:
        response = client.get("/api/customers")

    assert response.status_code == 401


def test_frontend_uses_real_auth_api_and_has_no_default_password_fallback() -> None:
    source = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text(
        encoding="utf-8"
    )

    assert 'axios.post("/api/auth/login"' in source
    assert 'axios.get("/api/auth/me"' in source
    assert 'axios.post("/api/auth/logout"' in source
    assert "axios.defaults.withCredentials = true" in source
    assert "roleMenus" in source
    assert "123456" not in source
    assert 'axios.post("/api/login"' not in source
