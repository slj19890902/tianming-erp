from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture()
def template_app(tmp_path: Path):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.system import router as system_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.customer import Customer
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "template.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        db.add_all(
            [
                User(
                    username=role,
                    password_hash=hash_password("RolePass123!"),
                    role=role,
                    real_name=role,
                    display_name=role,
                    must_change_password=False,
                )
                for role in ("admin", "sales")
            ]
        )
        db.add(
            Customer(
                customer_number=1,
                customer_code="C001",
                name="模板客户",
                payment_term_days=30,
                credit_limit=0,
                delivery_method="配送",
            )
        )
        db.commit()

    app = FastAPI()

    def override_get_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(system_router, prefix="/api/system")
    yield app, factory
    engine.dispose()


def _login(client: TestClient, role: str) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": role, "password": "RolePass123!"},
    )
    assert response.status_code == 200


def _changed_layout(layout: dict, *, x_mm: float) -> dict:
    changed = json.loads(json.dumps(layout))
    changed["elements"][0]["x_mm"] = x_mm
    return changed


def test_admin_draft_publish_customer_override_snapshot_and_rollback(template_app):
    from app.models.customer import Customer
    from app.services.delivery_print_templates import (
        decode_snapshot,
        effective_layout,
        snapshot_for_delivery,
    )

    app, factory = template_app
    with TestClient(app) as client:
        assert client.get("/api/system/delivery-print-templates/admin").status_code == 401
        _login(client, "sales")
        assert client.get("/api/system/delivery-print-templates/admin").status_code == 403
        _login(client, "admin")

        initial = client.get("/api/system/delivery-print-templates/admin").json()
        assert initial["published"]["version"] == 0
        layout_v1 = _changed_layout(initial["published"]["layout"], x_mm=2.5)
        saved = client.put(
            "/api/system/delivery-print-templates/admin/draft",
            json={
                "customer_id": None,
                "expected_release_version": 0,
                "operation_key": "default-draft-0001",
                "layout": layout_v1,
            },
        )
        assert saved.status_code == 200
        assert saved.json()["version"] == 1
        replay = client.put(
            "/api/system/delivery-print-templates/admin/draft",
            json={
                "customer_id": None,
                "expected_release_version": 0,
                "operation_key": "default-draft-0001",
                "layout": layout_v1,
            },
        )
        assert replay.json()["replayed"] is True
        published = client.post(
            "/api/system/delivery-print-templates/admin/publish",
            json={
                "customer_id": None,
                "draft_version": 1,
                "expected_release_version": 0,
                "operation_key": "default-publish-01",
            },
        )
        assert published.status_code == 200
        assert published.json()["version"] == 1
        state_after_publish = client.get(
            "/api/system/delivery-print-templates/admin"
        ).json()
        assert state_after_publish["draft"] is None

        with factory() as db:
            customer_id = db.query(Customer.id).scalar()
            inherited = effective_layout(db, customer_id)
            frozen = snapshot_for_delivery(db, customer_id)
        assert inherited["profile_key"] == "default"
        assert inherited["version"] == 1

        customer_state = client.get(
            f"/api/system/delivery-print-templates/admin?customer_id={customer_id}"
        ).json()
        assert customer_state["published"]["source"] == "inherited_default"
        assert customer_state["published"]["version"] == 0
        assert customer_state["published"]["layout"]["elements"][0]["x_mm"] == 2.5
        customer_layout = _changed_layout(customer_state["published"]["layout"], x_mm=-3)
        customer_draft = client.put(
            "/api/system/delivery-print-templates/admin/draft",
            json={
                "customer_id": customer_id,
                "expected_release_version": 0,
                "operation_key": "customer-draft-01",
                "layout": customer_layout,
            },
        ).json()
        client.post(
            "/api/system/delivery-print-templates/admin/publish",
            json={
                "customer_id": customer_id,
                "draft_version": customer_draft["version"],
                "expected_release_version": 0,
                "operation_key": "customer-publish-1",
            },
        ).raise_for_status()

        layout_v2 = _changed_layout(layout_v1, x_mm=5)
        default_draft_v2 = client.put(
            "/api/system/delivery-print-templates/admin/draft",
            json={
                "customer_id": None,
                "expected_release_version": 1,
                "operation_key": "default-draft-0002",
                "layout": layout_v2,
            },
        ).json()
        client.post(
            "/api/system/delivery-print-templates/admin/publish",
            json={
                "customer_id": None,
                "draft_version": default_draft_v2["version"],
                "expected_release_version": 1,
                "operation_key": "default-publish-02",
            },
        ).raise_for_status()
        rolled_back = client.post(
            "/api/system/delivery-print-templates/admin/rollback",
            json={
                "customer_id": None,
                "source_version": 1,
                "expected_release_version": 2,
                "operation_key": "default-rollback-1",
            },
        )
        assert rolled_back.status_code == 200
        assert rolled_back.json()["version"] == 3
        assert rolled_back.json()["layout"]["elements"][0]["x_mm"] == 2.5

        with factory() as db:
            override = effective_layout(db, customer_id)
        assert override["profile_key"] == f"customer:{customer_id}"
        assert override["layout"]["elements"][0]["x_mm"] == -3
        decoded = decode_snapshot(
            profile_key_value=frozen["profile_key"],
            version=frozen["version"],
            payload_json=frozen["payload_json"],
            expected_hash=frozen["payload_hash"],
        )
        assert decoded["version"] == 1
        assert decoded["layout"]["elements"][0]["x_mm"] == 2.5


def test_template_rejects_script_unknown_fields_bad_columns_and_corrupt_snapshot(template_app):
    from app.services.delivery_print_templates import DeliveryPrintTemplateError, decode_snapshot

    app, _factory = template_app
    with TestClient(app) as client:
        _login(client, "admin")
        state = client.get("/api/system/delivery-print-templates/admin").json()
        scripted = state["published"]["layout"]
        scripted["script"] = "fetch('/api/system/backup')"
        response = client.put(
            "/api/system/delivery-print-templates/admin/draft",
            json={
                "expected_release_version": 0,
                "operation_key": "reject-script-001",
                "layout": scripted,
            },
        )
        assert response.status_code == 422

        bad_widths = state["published"]["layout"]
        bad_widths["column_widths"]["quantity"] = 8
        response = client.put(
            "/api/system/delivery-print-templates/admin/draft",
            json={
                "expected_release_version": 0,
                "operation_key": "reject-widths-001",
                "layout": bad_widths,
            },
        )
        assert response.status_code == 422

    with pytest.raises(DeliveryPrintTemplateError, match="哈希"):
        decode_snapshot(
            profile_key_value="default",
            version=1,
            payload_json='{"catalog_version":"delivery-print-v1"}',
            expected_hash="0" * 64,
        )


def test_designer_and_print_pages_expose_safe_frozen_template_controls():
    root = Path(__file__).resolve().parents[1]
    designer = (root / "static" / "delivery-print-designer.html").read_text("utf-8")
    print_page = (root / "static" / "delivery-print.html").read_text("utf-8")
    main = (root / "app" / "main.py").read_text("utf-8")

    assert "保存草稿" in designer and "发布草稿" in designer and "回滚上一版" in designer
    assert "显示备注说明（合计始终保留）" in designer
    assert "跨页、重复表头与页码" in designer and "renderScenario" in designer
    assert "page_size=200" in designer and "while(page<=totalPages)" in designer
    assert "金额公式或数据库查询" in designer
    assert 'data.print_template.layout.show_remarks !== false' in print_page
    assert 'sheet.dataset.templateVersion' in print_page
    assert 'route.path == "/delivery-print-designer.html"' in main
