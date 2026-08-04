from __future__ import annotations

import inspect
from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.params import Depends as DependsParam
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker


PDF_TRAINING_ROUTE_PERMISSIONS = {
    ("GET", "/api/pdf-training/batches"): "view",
    ("POST", "/api/pdf-training/batches"): "manage",
    ("GET", "/api/pdf-training/samples"): "view",
    ("GET", "/api/pdf-training/samples/list"): "view",
    ("POST", "/api/pdf-training/samples/upload"): "manage",
    ("POST", "/api/pdf-training/samples/submit-correction"): "manage",
    ("GET", "/api/pdf-training/samples/detail/{sample_id}"): "view",
    ("GET", "/api/pdf-training/samples/{sample_id}"): "view",
    ("PUT", "/api/pdf-training/samples/{sample_id}/ground-truth"): "manage",
    ("POST", "/api/pdf-training/samples/{sample_id}/gold-review"): "manage",
    ("POST", "/api/pdf-training/samples/{sample_id}/score"): "manage",
    ("POST", "/api/pdf-training/samples/{sample_id}/reparse"): "manage",
    ("DELETE", "/api/pdf-training/samples/{sample_id}"): "manage",
    ("POST", "/api/pdf-training/samples/{sample_id}/corrections"): "manage",
    ("GET", "/api/pdf-training/samples/{sample_id}/corrections"): "view",
    ("GET", "/api/pdf-training/templates"): "view",
    ("POST", "/api/pdf-training/templates"): "manage",
    ("PUT", "/api/pdf-training/templates/{template_id}"): "manage",
    ("POST", "/api/pdf-training/templates/{template_id}/clone-draft"): "manage",
    ("POST", "/api/pdf-training/templates/{template_id}/activation-dry-run"): "manage",
    ("POST", "/api/pdf-training/templates/{template_id}/activate"): "manage",
    ("POST", "/api/pdf-training/templates/{template_id}/retire"): "manage",
    ("DELETE", "/api/pdf-training/templates/{template_id}"): "manage",
    ("GET", "/api/pdf-training/ocr-status"): "view",
    ("GET", "/api/pdf-training/stats"): "view",
}


@pytest.fixture()
def pdf_training_permissions_app(tmp_path: Path):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.pdf_training import router as pdf_training_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "pdf-training-permissions.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    passwords = {
        "admin": "AdminPass123!",
        "boss": "BossPass123!",
        "sales": "SalesPass123!",
        "finance": "FinancePass123!",
        "workshop": "WorkshopPass123!",
        "delivery_picker": "PickerPass123!",
    }
    with factory() as db:
        db.add_all(
            User(
                username=role,
                password_hash=hash_password(password),
                role=role,
                real_name=role,
                must_change_password=False,
            )
            for role, password in passwords.items()
        )
        db.commit()
        ids = {user.username: user.id for user in db.query(User).all()}

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(pdf_training_router, prefix="/api/pdf-training")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield app, factory, ids, passwords, engine
    finally:
        engine.dispose()


def _login(client: TestClient, username: str, password: str) -> dict:
    response = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200
    return response.json()


def test_pdf_training_permission_catalog_and_role_defaults(
    pdf_training_permissions_app,
) -> None:
    from app.api.deps import ADMIN_ONLY_PERMISSIONS, PERMISSION_CATALOG, has_permission
    from app.models.user import User

    _app, factory, ids, _passwords, _engine = pdf_training_permissions_app
    assert {"pdf_training.view", "pdf_training.manage"}.issubset(PERMISSION_CATALOG)
    assert "pdf_training.manage" in ADMIN_ONLY_PERMISSIONS
    with factory() as db:
        for role, user_id in ids.items():
            user = db.get(User, user_id)
            assert user is not None
            assert has_permission(user, "pdf_training.view") is (role == "admin")
            assert has_permission(user, "pdf_training.manage") is (role == "admin")


def test_every_pdf_training_route_declares_its_required_permission(
    pdf_training_permissions_app,
) -> None:
    from app.api.pdf_training import (
        require_pdf_training_manage,
        require_pdf_training_view,
    )

    app, _factory, _ids, _passwords, _engine = pdf_training_permissions_app
    actual = {}
    for route in app.routes:
        if not route.path.startswith("/api/pdf-training/"):
            continue
        for method in route.methods or ():
            key = (method, route.path)
            dependencies = [
                parameter.default.dependency
                for parameter in inspect.signature(route.endpoint).parameters.values()
                if isinstance(parameter.default, DependsParam)
            ]
            actual[key] = dependencies

    assert set(actual) == set(PDF_TRAINING_ROUTE_PERMISSIONS)
    for key, permission in PDF_TRAINING_ROUTE_PERMISSIONS.items():
        expected = (
            require_pdf_training_view
            if permission == "view"
            else require_pdf_training_manage
        )
        assert expected in actual[key], key


@pytest.mark.parametrize(
    "role",
    ["boss", "sales", "finance", "workshop", "delivery_picker"],
)
def test_default_business_roles_cannot_bypass_pdf_training_urls(
    pdf_training_permissions_app,
    role: str,
) -> None:
    app, _factory, _ids, passwords, _engine = pdf_training_permissions_app
    with TestClient(app) as client:
        _login(client, role, passwords[role])
        assert client.get("/api/pdf-training/stats").status_code == 403
        assert client.post(
            "/api/pdf-training/batches", json={"batch_name": "blocked"}
        ).status_code == 403


def test_pdf_training_requires_login_and_allows_explicit_view_only_override(
    pdf_training_permissions_app,
) -> None:
    app, _factory, ids, passwords, _engine = pdf_training_permissions_app
    with TestClient(app) as client:
        assert client.get("/api/pdf-training/stats").status_code == 401

        _login(client, "admin", passwords["admin"])
        rejected = client.put(
            f"/api/auth/users/{ids['sales']}/permission-overrides",
            json={"overrides": {"pdf_training.manage": True}},
        )
        assert rejected.status_code == 400
        granted = client.put(
            f"/api/auth/users/{ids['sales']}/permission-overrides",
            json={"overrides": {"pdf_training.view": True}},
        )
        assert granted.status_code == 200
        assert "pdf_training.view" in granted.json()["effective_permissions"]

        client.post("/api/auth/logout")
        _login(client, "sales", passwords["sales"])
        assert client.get("/api/pdf-training/stats").status_code == 200
        assert client.post(
            "/api/pdf-training/batches", json={"batch_name": "still-blocked"}
        ).status_code == 403


def test_admin_can_execute_pdf_training_read_and_manage_routes(
    pdf_training_permissions_app,
) -> None:
    app, _factory, _ids, passwords, _engine = pdf_training_permissions_app
    with TestClient(app) as client:
        _login(client, "admin", passwords["admin"])
        assert client.get("/api/pdf-training/stats").status_code == 200
        created = client.post(
            "/api/pdf-training/templates",
            json={"template_name": "permission-gate-template"},
        )
        assert created.status_code == 201
        assert created.json()["template_name"] == "permission-gate-template"
