from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, ".")

import app.models  # noqa: F401
from app.api.requisition import _format_supplier_material


@pytest.mark.parametrize(
    ("raw_code", "layer_count", "flute_type", "expected"),
    [
        ("A416D", 5, "AB", "A416D / AB"),
        ("BC14C", 5, "AB", "BC14C / AB"),
        ("CCC-B", 3, "E", "CCC / E"),
        ("A6A", 3, "E", "A6A / E"),
        ("A414B-AB/EB", 5, "AB", "A414B / AB"),
        ("CCC-B", 3, None, "CCC"),
    ],
)
def test_supplier_material_display_rules(raw_code, layer_count, flute_type, expected):
    assert _format_supplier_material(raw_code, layer_count, flute_type) == expected


def test_supplier_material_display_rejects_flute_combinations_as_material_code():
    displayed = _format_supplier_material("AB/BE", 5, "E")
    assert displayed == "E"
    assert "AB/BE" not in displayed
    assert "B/E" not in displayed


def test_supplier_order_frontend_uses_clean_material_display():
    html = Path("static/index.html").read_text(encoding="utf-8")
    assert "{{ so.material_display || so.material_code || '-' }}" in html
    assert "{{ modal.data.material_display || modal.data.material_code || '-' }}" in html
    assert "当前报料单存在低于供应商最小切宽/切长的明细" in html
    assert "so.dimension_warnings" in html


@pytest.fixture(scope="module")
def api_app(tmp_path_factory):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.requisition import router as req_router
    from app.core.database import create_sqlite_engine
    from app.models import Base

    db_path = tmp_path_factory.mktemp("suporder") / "so.sqlite3"
    engine = create_sqlite_engine(db_path)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    application = FastAPI()
    application.include_router(auth_router, prefix="/api/auth")
    application.include_router(req_router, prefix="/api/requisition")
    application.dependency_overrides[get_db] = override_get_db
    return application, session_factory


@pytest.fixture(scope="module")
def client(api_app):
    app, _ = api_app
    return TestClient(app)


@pytest.fixture(scope="module")
def session_factory(api_app):
    _, sf = api_app
    return sf


@pytest.fixture(scope="module")
def admin_cookies(client, session_factory):
    from app.core.security import hash_password
    from app.models.user import User

    db = session_factory()
    try:
        admin = db.query(User).filter_by(username="admin_so").first()
        if not admin:
            admin = User(
                username="admin_so",
                password_hash=hash_password("pw123"),
                role="admin",
                display_name="Admin SO",
                real_name="Admin SO",
                is_active=True,
            )
            db.add(admin)
            db.commit()
    finally:
        db.close()

    r = client.post("/api/auth/login", json={"username": "admin_so", "password": "pw123"})
    assert r.status_code in (200, 201), r.text
    return r.cookies


_created_id = None
_created_number = None


class TestSupplierOrders:
    def test_list_empty(self, client, admin_cookies):
        r = client.get("/api/requisition/supplier-orders", cookies=admin_cookies)
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 0
        assert data["items"] == []

    def test_create_requires_members(self, client, admin_cookies):
        r = client.post(
            "/api/requisition/supplier-orders",
            json={"supplier_name": "X", "members": []},
            cookies=admin_cookies,
        )
        assert r.status_code == 400

    def test_create_supplier_order(self, client, admin_cookies, session_factory):
        global _created_id, _created_number
        from app.models.material import Material

        db = session_factory()
        try:
            material = Material(
                code="A416D",
                layer_count=5,
                flute_type="AB",
                supplier_name="天意纸板厂",
            )
            db.add(material)
            db.commit()
            db.refresh(material)
            material_id = material.id
        finally:
            db.close()
        payload = {
            "supplier_name": "天意纸板厂",
            "material_id": material_id,
            "layer_count": 5,
            "flute_type": "AB",
            "report_length_mm": 800,
            "report_width_mm": 200,
            "crease_type": "压线",
            "crease_left_mm": 130,
            "crease_middle_mm": 360,
            "crease_right_mm": 130,
            "cutting_mode": "一开三",
            "pieces_per_box": 2,
            "required_piece_qty": 10,
            "members": [
                {
                    "item_id": None,
                    "order_number": "TM260101-001",
                    "product_code": "BOX001",
                    "product_name": "普通瓦楞箱",
                    "quantity": 5,
                    "pieces_per_box": 2,
                    "required_piece_qty": 10,
                    "stock_deduction_qty": 0,
                    "requisition_qty": 4,
                    "cutting_mode": "一开三",
                    "customer_name": "客户甲",
                    "delivery_date": "2026-07-10",
                },
                {
                    "item_id": None,
                    "order_number": "TM260101-002",
                    "product_code": "BOX001",
                    "product_name": "普通瓦楞箱",
                    "quantity": 3,
                    "pieces_per_box": 2,
                    "required_piece_qty": 6,
                    "stock_deduction_qty": 0,
                    "requisition_qty": 2,
                    "cutting_mode": "一开三",
                    "customer_name": "客户乙",
                    "delivery_date": "2026-07-12",
                },
            ],
        }
        r = client.post("/api/requisition/supplier-orders", json=payload, cookies=admin_cookies)
        assert r.status_code == 201, r.text
        data = r.json()
        assert data["order_number"].startswith("SRO-")
        assert data["supplier_name"] == "天意纸板厂"
        assert data["sender"] == {
            "company_name": "",
            "address": None,
            "phone": None,
        }
        assert data["material_code"] == "A416D"
        assert data["material_display"] == "A416D / AB"
        assert data["total_quantity"] == 8
        assert data["stock_deduction_qty"] == 0
        assert data["requisition_qty"] == 6
        assert data["cutting_mode"] == "一开三"
        assert data["pieces_per_box"] == 2
        assert data["required_piece_qty"] == 16
        assert len(data["items"]) == 2
        assert data["status"] == "confirmed"
        assert data["crease_display"] == "130+360+130"
        assert data["items"][0]["cutting_mode"] == "一开三"
        _created_id = data["id"]
        _created_number = data["order_number"]

    def test_list_after_create(self, client, admin_cookies):
        r = client.get("/api/requisition/supplier-orders", cookies=admin_cookies)
        assert r.status_code == 200
        assert r.json()["total"] == 1
        assert r.json()["items"][0]["material_display"] == "A416D / AB"

    def test_get_by_id(self, client, admin_cookies):
        r = client.get(f"/api/requisition/supplier-orders/{_created_id}", cookies=admin_cookies)
        assert r.status_code == 200
        data = r.json()
        assert data["order_number"] == _created_number
        assert len(data["items"]) == 2
        assert data["cutting_mode"] == "一开三"

    def test_get_404(self, client, admin_cookies):
        r = client.get("/api/requisition/supplier-orders/99999", cookies=admin_cookies)
        assert r.status_code == 404

    def test_void_order(self, client, admin_cookies):
        r = client.put(f"/api/requisition/supplier-orders/{_created_id}/void", cookies=admin_cookies)
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "voided"
        assert data["voided_at"] is not None

    def test_void_again_fails(self, client, admin_cookies):
        r = client.put(f"/api/requisition/supplier-orders/{_created_id}/void", cookies=admin_cookies)
        assert r.status_code == 400

    def test_list_with_status_filter(self, client, admin_cookies):
        r = client.get("/api/requisition/supplier-orders?status=confirmed", cookies=admin_cookies)
        assert r.status_code == 200
        assert r.json()["total"] == 0
