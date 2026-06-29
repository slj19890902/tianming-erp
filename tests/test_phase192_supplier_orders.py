from __future__ import annotations

import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, ".")

import app.models  # noqa: F401


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

    def test_create_supplier_order(self, client, admin_cookies):
        global _created_id, _created_number
        payload = {
            "supplier_name": "天意纸板厂",
            "layer_count": 5,
            "flute_type": "BC",
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
        assert data["total_quantity"] == 8
        assert data["stock_deduction_qty"] == 0
        assert data["requisition_qty"] == 6
        assert data["cutting_mode"] == "一开三"
        assert data["pieces_per_box"] == 2
        assert data["required_piece_qty"] == 10
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
