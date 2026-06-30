from decimal import Decimal

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker


def test_quotation_baseline_create_generate_accept_and_print(tmp_path):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.quotations import router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.customer import Customer
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "quotation.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        customer = Customer(
            customer_number=1,
            customer_code="Q-CUSTOMER",
            name="报价测试客户",
            credit_limit=Decimal("0"),
        )
        sales = User(
            username="quote-sales",
            password_hash=hash_password("QuotePass123!"),
            role="sales",
            real_name="报价业务",
            must_change_password=False,
        )
        workshop = User(
            username="quote-workshop",
            password_hash=hash_password("QuotePass123!"),
            role="workshop",
            real_name="报价无权限",
            must_change_password=False,
        )
        db.add_all([customer, sales, workshop])
        db.commit()
        customer_id = customer.id

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(router, prefix="/api/quotations")

    def override_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    payload = {
        "quotation_date": "2026-06-30",
        "remarks": "baseline",
        "items": [
            {
                "product_name": "A1 测试纸箱",
                "box_type": "A1",
                "length_mm": 300,
                "width_mm": 200,
                "height_mm": 150,
                "quantity": 100,
                "margin_rate": 20,
                "final_unit_price": 2.5,
            }
        ],
    }

    with TestClient(app) as client:
        assert client.post(
            "/api/auth/login",
            json={"username": "quote-sales", "password": "QuotePass123!"},
        ).status_code == 200
        created = client.post(
            f"/api/quotations?customer_id={customer_id}",
            json=payload,
        )
        assert created.status_code == 201
        quotation_id = created.json()["id"]
        assert created.json()["quotation_no"] == "QT-20260630-001"
        assert created.json()["total_amount"] == "250.00"

        generated = client.post(
            f"/api/quotations/{quotation_id}/generate"
        )
        assert generated.status_code == 200
        assert generated.json()["status"] == "quoted"
        accepted = client.post(
            f"/api/quotations/{quotation_id}/accept"
        )
        assert accepted.status_code == 200
        assert accepted.json()["status"] == "accepted"
        printed = client.get(
            f"/api/quotations/{quotation_id}/print"
        )
        assert printed.status_code == 200
        assert printed.json()["items"][0]["product_name"] == "A1 测试纸箱"

    with TestClient(app) as client:
        assert client.post(
            "/api/auth/login",
            json={"username": "quote-workshop", "password": "QuotePass123!"},
        ).status_code == 200
        assert client.get("/api/quotations").status_code == 403


def test_quotation_print_page_calls_api():
    from pathlib import Path

    html = (
        Path(__file__).resolve().parents[1]
        / "static"
        / "quotation-print.html"
    ).read_text(encoding="utf-8")
    assert "客户报价单" in html
    assert "/api/quotations/" in html
