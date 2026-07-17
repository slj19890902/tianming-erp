from __future__ import annotations

import json
from collections import Counter
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import func, select
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

    @app.middleware("http")
    async def override_test_client_ip(request: Request, call_next):
        client_ip = request.headers.get("x-test-client-ip")
        if client_ip:
            request.scope["client"] = (client_ip, 50_000)
        return await call_next(request)

    app.include_router(router, prefix="/api/auth")

    def override_get_db() -> Generator[Session, None, None]:
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    return app, session_factory


def _login(
    client: TestClient,
    username: str,
    password: str,
    *,
    client_ip: str | None = None,
):
    headers = {"user-agent": USER_AGENT}
    if client_ip is not None:
        headers["x-test-client-ip"] = client_ip
    return client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
        headers=headers,
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


def test_throttled_requests_have_bounded_audit_growth(login_rate_limit_context) -> None:
    from app.models.audit import OperationLog

    app, session_factory = login_rate_limit_context
    with TestClient(app) as client:
        for _ in range(5):
            assert _login(client, "limited-user", "wrong-password").status_code == 401
        throttled = [
            _login(client, "limited-user", "wrong-password")
            for _ in range(120)
        ]

    assert {response.status_code for response in throttled} == {429}
    with session_factory() as session:
        logs = session.scalars(
            select(OperationLog).where(OperationLog.username == "limited-user")
        ).all()
    assert [log.action for log in logs].count("LOGIN_FAILED") == 5
    assert [log.action for log in logs].count("LOGIN_THROTTLED") == 1
    assert len(logs) == 6


def test_login_username_is_normalized_and_oversize_is_not_audited(
    login_rate_limit_context,
) -> None:
    from app.models.audit import OperationLog

    app, session_factory = login_rate_limit_context
    with TestClient(app) as client:
        normalized = _login(client, "　ｌｉｍｉｔｅｄ－ｕｓｅｒ　", PASSWORD)
        oversized = _login(client, "x" * 10_000, "wrong-password")
        expanded_oversized = _login(client, "a" * 49 + "ﬃ", "wrong-password")
        normalized_empty = _login(client, "　\t ", "wrong-password")

    assert normalized.status_code == 200
    assert oversized.status_code == 422
    assert expanded_oversized.status_code == 422
    assert normalized_empty.status_code == 422
    with session_factory() as session:
        assert session.scalars(
            select(OperationLog).where(OperationLog.username == "x" * 10_000)
        ).all() == []


def test_oversize_login_password_never_verifies_or_grows_audit_log(
    login_rate_limit_context,
    monkeypatch,
) -> None:
    from app.models.audit import OperationLog

    app, session_factory = login_rate_limit_context
    verify_calls = 0

    def forbidden_verify(*_args, **_kwargs):
        nonlocal verify_calls
        verify_calls += 1
        raise AssertionError("oversize password reached verify_password")

    monkeypatch.setattr("app.api.auth.verify_password", forbidden_verify)
    with TestClient(app) as client:
        responses = [
            _login(client, "limited-user", "x" * 10_000)
            for _ in range(120)
        ]
        multibyte = _login(client, "limited-user", "密" * 25)

    assert {response.status_code for response in responses} == {422}
    assert multibyte.status_code == 422
    assert verify_calls == 0
    with session_factory() as session:
        assert session.scalars(select(OperationLog)).all() == []


def test_failed_login_caps_user_agent_in_audit_log(login_rate_limit_context) -> None:
    from app.models.audit import OperationLog

    app, session_factory = login_rate_limit_context
    with TestClient(app) as client:
        response = client.post(
            "/api/auth/login",
            json={"username": "limited-user", "password": "wrong-password"},
            headers={"user-agent": "A" * 10_000},
        )

    assert response.status_code == 401
    with session_factory() as session:
        log = session.scalar(
            select(OperationLog).where(OperationLog.action == "LOGIN_FAILED")
        )
    assert log is not None
    assert log.user_agent == "A" * 256
    assert json.loads(log.details or "{}")["user_agent"] == "A" * 256


def test_concurrent_failed_logins_are_atomically_throttled(
    login_rate_limit_context,
) -> None:
    from app.models.audit import OperationLog

    app, session_factory = login_rate_limit_context
    worker_count = 12
    barrier = Barrier(worker_count)

    with TestClient(app) as client:
        def attempt() -> int:
            barrier.wait(timeout=10)
            return _login(client, "limited-user", "concurrent-wrong-password").status_code

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            statuses = list(executor.map(lambda _index: attempt(), range(worker_count)))

    assert Counter(statuses) == Counter({401: 5, 429: 7})
    with session_factory() as session:
        actions = session.scalars(
            select(OperationLog.action).where(
                OperationLog.username == "limited-user"
            )
        ).all()
    assert actions.count("LOGIN_FAILED") == 5
    assert actions.count("LOGIN_THROTTLED") == 1


def test_48_distinct_login_keys_verify_concurrently_without_database_lock(
    login_rate_limit_context,
) -> None:
    from app.models.audit import OperationLog
    from app.models.user import User

    app, session_factory = login_rate_limit_context
    worker_count = 48
    usernames = [f"parallel-user-{index:02d}" for index in range(worker_count)]
    with session_factory() as session:
        shared_hash = session.scalar(
            select(User.password_hash).where(User.username == "limited-user")
        )
        assert shared_hash is not None
        session.add_all(
            User(
                username=username,
                password_hash=shared_hash,
                role="workshop",
                real_name=username,
                must_change_password=False,
            )
            for username in usernames
        )
        session.commit()

    barrier = Barrier(worker_count)
    with TestClient(app) as client:
        def attempt(username: str) -> int:
            barrier.wait(timeout=20)
            return _login(client, username, PASSWORD).status_code

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            statuses = list(executor.map(attempt, usernames))

    assert Counter(statuses) == Counter({200: worker_count})
    with session_factory() as session:
        successful_audits = session.scalar(
            select(func.count(OperationLog.id)).where(
                OperationLog.action == "LOGIN",
                OperationLog.username.in_(usernames),
            )
        )
    assert successful_audits == worker_count


def test_48_distinct_usernames_same_ip_reserve_exact_failure_capacity(
    login_rate_limit_context,
) -> None:
    from app.models.audit import OperationLog
    from app.models.user import User

    app, session_factory = login_rate_limit_context
    worker_count = 48
    usernames = [f"parallel-failure-{index:02d}" for index in range(worker_count)]
    with session_factory() as session:
        shared_hash = session.scalar(
            select(User.password_hash).where(User.username == "limited-user")
        )
        assert shared_hash is not None
        session.add_all(
            User(
                username=username,
                password_hash=shared_hash,
                role="workshop",
                real_name=username,
                must_change_password=False,
            )
            for username in usernames
        )
        session.commit()

    barrier = Barrier(worker_count)
    with TestClient(app) as client:
        def attempt(username: str) -> int:
            barrier.wait(timeout=20)
            return _login(client, username, "parallel-wrong-password").status_code

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            statuses = list(executor.map(attempt, usernames))

    assert Counter(statuses) == Counter({401: 30, 429: 18})
    with session_factory() as session:
        actions = session.scalars(
            select(OperationLog.action).where(
                OperationLog.username.in_(usernames),
            )
        ).all()
    assert actions.count("LOGIN_FAILED") == 30
    assert actions.count("LOGIN_THROTTLED") == 1


def test_different_ip_keys_do_not_block_each_other_during_password_verify(
    login_rate_limit_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _ = login_rate_limit_context
    verify_barrier = Barrier(2)

    def concurrent_verify(_password: str, _password_hash: str) -> bool:
        verify_barrier.wait(timeout=5)
        return False

    monkeypatch.setattr("app.api.auth.verify_password", concurrent_verify)
    client_ips = ["192.0.2.10", "192.0.2.11"]
    with TestClient(app) as client:
        def attempt(client_ip: str) -> int:
            return _login(
                client,
                "limited-user",
                "wrong-password",
                client_ip=client_ip,
            ).status_code

        with ThreadPoolExecutor(max_workers=2) as executor:
            statuses = list(executor.map(attempt, client_ips))

    assert statuses == [401, 401]
