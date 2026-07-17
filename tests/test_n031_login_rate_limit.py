from __future__ import annotations

import json
from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker


PASSWORD = "RateLimitPass123!"
USER_AGENT = "N031 login-rate-limit test"


@pytest.fixture()
def login_rate_limit_context(tmp_path: Path):
    from app.api.auth import router
    from app.api.deps import get_db
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "login-rate-limit.sqlite3")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as session:
        session.add_all(
            [
                User(
                    username="limited-user",
                    password_hash=hash_password(PASSWORD),
                    role="workshop",
                    real_name="Limited User",
                    must_change_password=False,
                ),
                User(
                    username="other-user",
                    password_hash=hash_password(PASSWORD),
                    role="workshop",
                    real_name="Other User",
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


def _login(client: TestClient, username: str, password: str):
    return client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
        headers={"user-agent": USER_AGENT},
    )


def test_failed_login_is_audited_and_throttled_without_shared_ip_lockout(
    login_rate_limit_context,
) -> None:
    from app.models.audit import OperationLog

    app, session_factory = login_rate_limit_context
    attempted_password = "do-not-store-this-password"
    with TestClient(app) as client:
        failures = [
            _login(client, "limited-user", attempted_password)
            for _ in range(5)
        ]
        throttled = _login(client, "limited-user", attempted_password)
        other_user = _login(client, "other-user", PASSWORD)

    assert [response.status_code for response in failures] == [401] * 5
    assert throttled.status_code == 429
    assert throttled.json()["detail"] == "登录尝试过多，请稍后再试"
    assert other_user.status_code == 200

    with session_factory() as session:
        failed_logs = session.scalars(
            select(OperationLog).where(
                OperationLog.action == "LOGIN_FAILED",
                OperationLog.username == "limited-user",
            )
        ).all()
        throttled_log = session.scalar(
            select(OperationLog).where(
                OperationLog.action == "LOGIN_THROTTLED",
                OperationLog.username == "limited-user",
            )
        )

    assert len(failed_logs) == 5
    assert throttled_log is not None
    for log in [*failed_logs, throttled_log]:
        details = json.loads(log.details or "{}")
        assert details == {
            "attempted_username": "limited-user",
            "ip_address": log.ip_address,
            "user_agent": USER_AGENT,
        }
        assert log.user_agent == USER_AGENT
        assert attempted_password not in " ".join(
            value or ""
            for value in (log.details, log.description, log.extra_json)
        )


def test_successful_login_resets_only_its_username_and_ip_counter(
    login_rate_limit_context,
) -> None:
    app, _ = login_rate_limit_context
    with TestClient(app) as client:
        initial_failures = [
            _login(client, "limited-user", "wrong-before-success")
            for _ in range(3)
        ]
        success = _login(client, "limited-user", PASSWORD)
        later_failures = [
            _login(client, "limited-user", "wrong-after-success")
            for _ in range(5)
        ]
        throttled = _login(client, "limited-user", "wrong-after-success")

    assert [response.status_code for response in initial_failures] == [401] * 3
    assert success.status_code == 200
    assert [response.status_code for response in later_failures] == [401] * 5
    assert throttled.status_code == 429


def test_ip_bucket_blocks_password_spraying_across_many_usernames(
    login_rate_limit_context,
) -> None:
    app, _ = login_rate_limit_context
    with TestClient(app) as client:
        spray = [
            _login(client, f"unknown-user-{index}", "wrong-password")
            for index in range(30)
        ]
        blocked_real_user = _login(client, "other-user", PASSWORD)

    assert [response.status_code for response in spray] == [401] * 30
    assert blocked_real_user.status_code == 429
    assert blocked_real_user.json()["detail"] == "登录尝试过多，请稍后再试"
