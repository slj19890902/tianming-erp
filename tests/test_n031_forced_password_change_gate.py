from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.api.auth import router as auth_router
from app.api.business_approvals import router as business_approvals_router
from app.api.deps import get_db
from app.core.database import create_sqlite_engine
from app.core.security import hash_password
from app.models import Base
from app.models.user import User


@dataclass(frozen=True)
class _Settings:
    secret_key: str = "isolated-force-change-gate-secret-value"
    session_cookie_name: str = "isolated_force_change_session"
    session_expire_minutes: int = 60

    @staticmethod
    def cookie_secure_for(_scope: object) -> bool:
        return False


def _isolated_app(database: Path) -> tuple[FastAPI, object]:
    engine = create_sqlite_engine(database)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        session.add(
            User(
                username="admin",
                password_hash=hash_password("TemporaryAdmin9"),
                role="admin",
                real_name="Isolated Admin",
                display_name="Isolated Admin",
                is_active=True,
                must_change_password=True,
                customer_access_mode="all",
            )
        )
        session.commit()

    application = FastAPI()
    application.state.erp_settings = _Settings()
    application.include_router(auth_router, prefix="/api/auth")
    application.include_router(
        business_approvals_router,
        prefix="/api/business-approvals",
    )

    def isolated_db():
        with factory() as session:
            yield session

    application.dependency_overrides[get_db] = isolated_db
    return application, engine


def test_forced_password_change_blocks_business_until_normal_change(
    tmp_path: Path,
) -> None:
    application, engine = _isolated_app(tmp_path / "forced-change.sqlite3")
    try:
        with TestClient(application, base_url="http://127.0.0.1:18082") as client:
            login = client.post(
                "/api/auth/login",
                json={
                    "username": "admin",
                    "password": "TemporaryAdmin9",
                    "remember_me": False,
                },
            )
            assert login.status_code == 200
            assert login.json()["user"]["must_change_password"] is True
            assert client.get("/api/auth/me").status_code == 200

            blocked_business = client.get("/api/business-approvals/profile")
            assert blocked_business.status_code == 403
            assert blocked_business.json()["detail"] == "首次登录必须先修改密码"

            blocked_nonessential_auth = client.put(
                "/api/auth/me/ui-mode",
                json={"ui_mode": "large"},
            )
            assert blocked_nonessential_auth.status_code == 403

            old_cookie = client.cookies.get("isolated_force_change_session")
            assert old_cookie
            changed = client.put(
                "/api/auth/password",
                json={
                    "current_password": "TemporaryAdmin9",
                    "new_password": "FinalOwnerSecure9",
                },
            )
            assert changed.status_code == 200
            assert changed.json()["user"]["must_change_password"] is False

        with TestClient(application, base_url="http://127.0.0.1:18082") as stale:
            stale.cookies.set("isolated_force_change_session", old_cookie)
            assert stale.get("/api/auth/me").status_code == 401

        with TestClient(application, base_url="http://127.0.0.1:18082") as final:
            assert final.post(
                "/api/auth/login",
                json={
                    "username": "admin",
                    "password": "TemporaryAdmin9",
                    "remember_me": False,
                },
            ).status_code == 401
            assert final.post(
                "/api/auth/login",
                json={
                    "username": "admin",
                    "password": "FinalOwnerSecure9",
                    "remember_me": False,
                },
            ).status_code == 200
            assert final.get("/api/business-approvals/profile").status_code == 200
    finally:
        engine.dispose()
