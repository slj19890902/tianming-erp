from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

LAN = "http://192.168.3.80:8000"
PUBLIC = "https://erp.example.com"


def _client(app, *, base_url, client, **options):
    async def with_peer(scope, receive, send):
        if scope["type"] == "http":
            scope = {**scope, "client": client}
        await app(scope, receive, send)

    return TestClient(with_peer, base_url=base_url, **options)


@pytest.fixture
def dual_settings(monkeypatch, tmp_path):
    from app.core.config import load_settings

    values = {
        "ERP_ENVIRONMENT": "production",
        "ERP_PRODUCTION_TRANSPORT": "https_proxy",
        "ERP_SECRET_KEY": "dual-entry-isolated-test-secret-at-least-32-characters",
        "ERP_DATABASE_PATH": str(tmp_path / "dual.sqlite3"),
        "ERP_BIND_HOST": "127.0.0.1",
        "ERP_PORT": "18000",
        "ERP_ALLOWED_ORIGINS": PUBLIC,
        "ERP_TRUSTED_HOSTS": "erp.example.com",
        "ERP_TRUSTED_PROXY_IPS": "127.0.0.1",
        "ERP_HEALTH_URL": PUBLIC + "/api/health",
        "ERP_BROWSER_URL": PUBLIC + "/",
        "ERP_LAN_HTTP_ORIGIN": LAN,
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return load_settings()


@pytest.mark.parametrize("origin", [
    "http://8.8.8.8:8000", "http://example.com:8000", "http://127.0.0.1:8000",
    "https://192.168.3.80:8000", "http://192.168.3.80", "http://192.168.3.80:0",
    "http://user@192.168.3.80:8000", "http://192.168.3.80:8000/path",
    "http://192.168.3.80:8000?x=1", "http://[::1]:8000",
])
def test_dual_origin_rejects_unsafe_config(dual_settings, monkeypatch, origin):
    from app.core.config import load_settings

    monkeypatch.setenv("ERP_LAN_HTTP_ORIGIN", origin)
    with pytest.raises(ValueError, match="ERP_LAN_HTTP_ORIGIN"):
        load_settings()


def test_dual_transport_and_origin_boundaries(dual_settings):
    from app.main import apply_transport_security

    app = FastAPI()

    @app.api_route("/probe", methods=["GET", "POST"])
    def probe():
        return {"ok": True}

    apply_transport_security(app, dual_settings)
    with _client(app, base_url=LAN, client=("192.168.3.25", 1234), follow_redirects=False) as local:
        response = local.get("/probe")
        assert response.status_code == 200
        assert "strict-transport-security" not in response.headers
        local.cookies.set("erp_session", "test-token")
        assert local.post("/probe", headers={"Origin": LAN}).status_code == 200
        assert local.post("/probe", headers={"Origin": PUBLIC}).status_code == 403
        assert local.post("/probe").status_code == 403
    with _client(app, base_url=PUBLIC, client=("8.8.8.8", 1234)) as public:
        assert "strict-transport-security" in public.get("/probe").headers
        public.cookies.set("erp_session", "test-token")
        assert public.post("/probe", headers={"Origin": PUBLIC}).status_code == 200
        assert public.post("/probe", headers={"Origin": LAN}).status_code == 403
    for origin, peer in [(LAN, "8.8.8.8"), ("http://erp.example.com", "192.168.3.25"),
                         ("http://192.168.3.80:8001", "192.168.3.25")]:
        with _client(app, base_url=origin, client=(peer, 1234), follow_redirects=False) as client:
            response = client.get("/probe", headers={"X-Forwarded-For": "192.168.3.25"})
            assert response.status_code == 307
    with _client(app, base_url="http://attacker.example", client=("192.168.3.25", 1234)) as client:
        assert client.get("/probe").status_code == 400


def test_both_entries_login_same_account_and_revoke_together(dual_settings):
    from sqlalchemy.orm import sessionmaker
    from app.api.auth import router
    from app.api.deps import get_db
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.main import apply_transport_security
    from app.models import Base
    from app.models.user import User

    engine = create_sqlite_engine(dual_settings.database_path)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        db.add(User(username="dual-test", password_hash=hash_password("DualTest123!"),
                    role="workshop", real_name="Isolated test", must_change_password=False,
                    customer_access_mode="selected"))
        db.commit()
    app = FastAPI()
    app.state.erp_settings = dual_settings
    app.include_router(router, prefix="/api/auth")

    def isolated_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = isolated_db
    apply_transport_security(app, dual_settings)
    try:
        with _client(app, base_url=LAN, client=("192.168.3.25", 1234)) as local, \
             _client(app, base_url=PUBLIC, client=("8.8.8.8", 1234)) as public:
            identities = []
            for client, secure in [(local, False), (public, True)]:
                assert client.get("/api/auth/me").status_code == 401
                response = client.post("/api/auth/login", json={"username": "dual-test", "password": "DualTest123!"})
                assert response.status_code == 200, response.text
                cookie = response.headers["set-cookie"].lower()
                assert ("; secure" in cookie) is secure
                assert "httponly" in cookie and "samesite=lax" in cookie
                assert "domain=" not in cookie
                identities.append(client.get("/api/auth/me").json())
                assert client.get("/api/auth/users").status_code == 403
            assert identities[0] == identities[1]
            response = local.post("/api/auth/logout", headers={"Origin": LAN})
            assert response.status_code == 200
            assert "; secure" not in response.headers["set-cookie"].lower()
            assert local.get("/api/auth/me").status_code == 401
            assert public.get("/api/auth/me").status_code == 401
    finally:
        engine.dispose()
