from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker


@pytest.fixture()
def delivery_print_settings_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.system import router as system_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.user import User

    database_path = tmp_path / "print-settings.sqlite3"
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database_path))
    engine = create_sqlite_engine(database_path)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as session:
        session.add_all(
            [
                User(username="admin", password_hash=hash_password("RolePass123!"), role="admin", real_name="管理员"),
                User(username="finance", password_hash=hash_password("RolePass123!"), role="finance", real_name="财务"),
            ]
        )
        session.commit()

    app = FastAPI()

    def override_get_db():
        with session_factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(system_router, prefix="/api/system")
    return app, session_factory, database_path


def _login(client: TestClient, username: str) -> None:
    response = client.post("/api/auth/login", json={"username": username, "password": "RolePass123!"})
    assert response.status_code == 200


DEFAULT_PRINT_PROFILE = {
    "printer_model": "EPSON SK820",
    "orientation_mode": "driver_managed",
    "paper_width_mm": 241.0,
    "paper_height_mm": 139.5,
    "printable_width_mm": 200.0,
    "content_width_mm": 188.0,
    "offset_x_mm": 0.0,
    "offset_y_mm": 0.0,
}


def test_delivery_print_settings_are_public_and_default_without_writing(delivery_print_settings_app):
    app, _, database_path = delivery_print_settings_app
    settings_path = database_path.parent / "delivery_print_settings.json"
    with TestClient(app) as client:
        response = client.get("/api/system/delivery-print-settings")
    assert response.status_code == 200
    assert response.json() == DEFAULT_PRINT_PROFILE
    assert not settings_path.exists()


def test_admin_can_save_paper_dimensions_and_audit(delivery_print_settings_app):
    from app.models.audit import OperationLog

    app, session_factory, database_path = delivery_print_settings_app
    with TestClient(app) as client:
        _login(client, "admin")
        response = client.put(
            "/api/system/delivery-print-settings",
            json={"paper_width_mm": 250, "paper_height_mm": 140.25},
        )
        reread = client.get("/api/system/delivery-print-settings")
    assert response.status_code == 200
    assert response.json() == {
        **DEFAULT_PRINT_PROFILE,
        "paper_width_mm": 250.0,
        "paper_height_mm": 140.25,
    }
    assert reread.json() == response.json()
    assert database_path.parent.joinpath("delivery_print_settings.json").is_file()
    with session_factory() as session:
        audit = session.query(OperationLog).filter_by(action="UPDATE_DELIVERY_PRINT_SETTINGS").one()
    assert "250" in audit.details


def test_existing_dimension_only_file_is_upgraded_in_memory_without_rewrite(
    delivery_print_settings_app,
):
    app, _, database_path = delivery_print_settings_app
    settings_path = database_path.parent / "delivery_print_settings.json"
    settings_path.write_text(
        '{"paper_width_mm": 242, "paper_height_mm": 140}\n',
        encoding="utf-8",
    )
    before = settings_path.read_bytes()

    with TestClient(app) as client:
        response = client.get("/api/system/delivery-print-settings")

    assert response.status_code == 200
    assert response.json() == {
        **DEFAULT_PRINT_PROFILE,
        "paper_width_mm": 242.0,
        "paper_height_mm": 140.0,
    }
    assert settings_path.read_bytes() == before


@pytest.mark.parametrize(
    "payload",
    [
        {"printer_model": "", "paper_width_mm": 241, "paper_height_mm": 139.5},
        {
            "printer_model": "EPSON SK820",
            "orientation_mode": "landscape",
            "paper_width_mm": 241,
            "paper_height_mm": 139.5,
        },
    ],
)
def test_delivery_print_profile_rejects_invalid_model_or_orientation(
    delivery_print_settings_app,
    payload,
):
    app, _, _ = delivery_print_settings_app
    with TestClient(app) as client:
        _login(client, "admin")
        response = client.put("/api/system/delivery-print-settings", json=payload)
    assert response.status_code == 422


@pytest.mark.parametrize(
    "payload",
    [
        {"paper_width_mm": 99.99, "paper_height_mm": 139.5},
        {"paper_width_mm": 241, "paper_height_mm": 400.01},
        {"paper_width_mm": 241.123, "paper_height_mm": 139.5},
    ],
)
def test_delivery_print_settings_reject_invalid_dimensions(delivery_print_settings_app, payload):
    app, _, _ = delivery_print_settings_app
    with TestClient(app) as client:
        _login(client, "admin")
        response = client.put("/api/system/delivery-print-settings", json=payload)
    assert response.status_code == 422


def test_delivery_print_settings_write_is_admin_only(delivery_print_settings_app):
    app, _, _ = delivery_print_settings_app
    with TestClient(app) as client:
        _login(client, "finance")
        response = client.put(
            "/api/system/delivery-print-settings",
            json={"paper_width_mm": 241, "paper_height_mm": 139.5},
        )
    assert response.status_code == 403


def test_calibration_rejects_clipped_content_and_preserves_existing_file(tmp_path, monkeypatch):
    from app.services.delivery_print_settings import save_delivery_print_settings, get_delivery_print_settings
    target = tmp_path / "profile.json"
    monkeypatch.setenv("ERP_DELIVERY_PRINT_SETTINGS_PATH", str(target))
    profile = {**DEFAULT_PRINT_PROFILE, "printable_width_mm": 241, "content_width_mm": 210, "offset_x_mm": 2, "offset_y_mm": 1}
    assert save_delivery_print_settings(profile) == profile
    before = target.read_bytes()
    for invalid in ({"content_width_mm": 240}, {"offset_x_mm": 15}, {"offset_y_mm": 3}):
        with pytest.raises(ValueError):
            save_delivery_print_settings({**profile, **invalid})
        assert target.read_bytes() == before
    assert get_delivery_print_settings() == profile


def test_old_narrow_paper_gets_safe_default_without_rewrite(tmp_path, monkeypatch):
    from app.services.delivery_print_settings import get_delivery_print_settings
    target = tmp_path / "profile.json"
    monkeypatch.setenv("ERP_DELIVERY_PRINT_SETTINGS_PATH", str(target))
    target.write_text('{"paper_width_mm": 200, "paper_height_mm": 130}')
    before = target.read_bytes()
    profile = get_delivery_print_settings()
    assert profile["paper_width_mm"] == 200
    assert profile["content_width_mm"] == 188
    assert target.read_bytes() == before


def test_printable_area_distinct_from_paper_and_legacy_calibration_preserved(tmp_path, monkeypatch):
    from app.services.delivery_print_settings import normalize_delivery_print_settings, get_delivery_print_settings
    narrow = normalize_delivery_print_settings({"paper_width_mm":241,"paper_height_mm":139.5})
    assert narrow["printable_width_mm"] == 200 and narrow["content_width_mm"] == 188
    for bad in ({"printable_width_mm":242}, {"content_width_mm":189}, {"offset_x_mm":4}):
        with pytest.raises(ValueError):
            normalize_delivery_print_settings({**narrow, **bad})
    path=tmp_path/'legacy.json'
    monkeypatch.setenv('ERP_DELIVERY_PRINT_SETTINGS_PATH',str(path))
    path.write_text('{"paper_width_mm":241,"paper_height_mm":139.5,"content_width_mm":210,"offset_x_mm":2}')
    before=path.read_bytes(); profile=get_delivery_print_settings()
    assert profile['printable_width_mm']==241 and profile['content_width_mm']==210
    assert profile['offset_x_mm']==2 and path.read_bytes()==before
