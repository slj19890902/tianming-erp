"""The shipped release card must remain usable without database access."""
from types import SimpleNamespace
import sqlite3

import pytest

from app import version


@pytest.fixture(autouse=True)
def reject_database_access(monkeypatch):
    """Version metadata and the handler body have no database dependency."""
    from sqlalchemy.engine import Engine

    attempts = []

    def denied(*_args, **_kwargs):
        attempts.append(True)
        raise AssertionError("Release metadata regression must not connect to a database")

    monkeypatch.setattr(sqlite3, "connect", denied)
    monkeypatch.setattr(sqlite3.dbapi2, "connect", denied)
    monkeypatch.setattr(Engine, "connect", denied)
    yield
    assert not attempts, "Even a caught database connection attempt fails this contract"


def test_current_release_card_accepts_actual_shipped_constants():
    metadata = version.current_release_metadata(expected_version=version.APP_VERSION)
    assert metadata["version"] == version.APP_VERSION
    assert metadata["version_name"] == version.APP_VERSION_NAME
    assert metadata["changes"] == version.APP_CHANGES
    assert metadata["verification_steps"] == version.APP_VERIFICATION_STEPS
    assert 1 <= len(metadata["changes"]) <= 5
    assert 1 <= len(metadata["verification_steps"]) <= 5


def test_current_release_card_returns_independent_mutable_copies():
    original = version.current_release_metadata()
    returned = version.current_release_metadata()
    returned["version"] = "v0.0.0"
    returned["changes"].clear()
    returned["verification_steps"].append("Synthetic caller-only instruction")
    assert version.current_release_metadata() == original
    assert version.APP_CHANGES == original["changes"]
    assert version.APP_VERIFICATION_STEPS == original["verification_steps"]


def test_version_handler_returns_the_actual_validated_card():
    # Import/run under the managed test environment. Calling the body avoids
    # authentication/database fixtures; HTTP authentication has separate tests.
    from app.api.system import get_version

    response = get_version(_user=SimpleNamespace(id=-1))
    assert response == version.current_release_metadata()
    assert response["version"] == version.APP_VERSION


def test_version_http_preserves_real_session_authentication_without_a_database():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.deps import get_db
    from app.api.system import router
    from app.core.security import create_session_token
    from app.models.user import User

    # Only storage and startup settings are in memory. The real router,
    # get_current_user, JWT decoder and session-revocation check remain active.
    user = User(id=901, username="synthetic-version-reader", role="admin",
                is_active=True, auth_version=1, permission_overrides=[])
    lookups = []

    class MemoryUsers:
        def get(self, model, user_id):
            assert model is User and user_id == user.id
            lookups.append(user_id)
            return user

    def memory_db():
        yield MemoryUsers()

    settings = SimpleNamespace(session_cookie_name="release-contract-session",
                               secret_key="synthetic-release-contract-secret-not-for-production")
    app = FastAPI()
    app.state.erp_settings = settings
    app.include_router(router, prefix="/api/system")
    app.dependency_overrides[get_db] = memory_db
    with TestClient(app) as client:
        assert client.get("/api/system/version").status_code == 401
        assert client.get("/api/system/version/changelog").status_code == 401
        client.cookies.set(settings.session_cookie_name, "invalid-synthetic-token")
        assert client.get("/api/system/version").status_code == 401
        assert lookups == []
        token = create_session_token(user.id, auth_version=1,
                                     secret_key=settings.secret_key, expires_minutes=1)
        client.cookies.set(settings.session_cookie_name, token)
        response = client.get("/api/system/version")
        assert response.status_code == 200
        assert response.json() == version.current_release_metadata()
        history = client.get("/api/system/version/changelog")
        assert history.status_code == 200
        assert history.json()["changelog"] == version.APP_CHANGELOG
        assert lookups == [user.id, user.id]
        user.auth_version += 1
        assert client.get("/api/system/version").status_code == 401
