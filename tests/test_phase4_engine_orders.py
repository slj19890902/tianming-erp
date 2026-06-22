from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool


def make_client():
    from phase1_postgres.database import get_session
    from phase1_postgres.main import create_app
    from phase1_postgres.models import Base

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    app = create_app(engine=engine, seed_demo_data=True)

    def override_session():
        from sqlalchemy.orm import Session

        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    return TestClient(app), engine


def test_calculate_carton_returns_paper_score_area_and_prices():
    client, _engine = make_client()

    response = client.post(
        "/api/engine/calculate-carton",
        json={
            "length_mm": "450",
            "width_mm": "340",
            "height_mm": "300",
            "box_category": "normal",
            "length_extra_mm": "8",
            "width_extra_mm": "4",
            "glue_flap_mm": "0",
            "customer_square_price": "3.50",
            "supplier_square_price": "2.85",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["paper_length_mm"] == "1596.00"
    assert body["paper_width_mm"] == "644.00"
    assert body["score_line"] == "340*450*340*450"
    assert body["area_m2"] == "1.0278"
    assert body["sale_unit_price"] == "3.60"
    assert body["purchase_unit_cost"] == "2.93"


def test_create_order_uses_product_snapshot_without_realtime_dependency():
    client, engine = make_client()

    payload = {
        "customer_id": 1,
        "customer_po": "UAT-PO-001",
        "order_date": "2026-06-17",
        "delivery_date": "2026-06-24",
        "items": [
            {
                "product_id": 1,
                "quantity": 100,
            }
        ],
    }
    response = client.post("/api/orders", json=payload)
    assert response.status_code == 201
    body = response.json()
    assert body["order_number"].startswith("PO-20260617-")
    assert body["total_amount"] == "365.00"
    assert body["items"][0]["snapshot_product_name"] == "001A外箱"
    assert body["items"][0]["snapshot_cardboard_length_mm"] == "916.00"
    assert body["items"][0]["snapshot_cardboard_width_mm"] == "644.00"
    assert body["items"][0]["snapshot_score_line"] == "340*110*340"
    assert body["items"][0]["unit_price"] == "3.6500"

    from phase1_postgres.models import OrderItem, Product

    with engine.connect() as connection:
        pass

    from sqlalchemy.orm import Session

    with Session(engine) as session:
        product = session.get(Product, 1)
        product.product_name = "资料库后来改名"
        product.default_unit_price = Decimal("9.9900")
        session.commit()

        item = session.scalar(select(OrderItem).where(OrderItem.id == body["items"][0]["id"]))
        assert item.snapshot_product_name == "001A外箱"
        assert item.unit_price == Decimal("3.6500")


def test_create_order_can_accept_temporary_calculated_carton_snapshot():
    client, engine = make_client()

    response = client.post(
        "/api/orders",
        json={
            "customer_id": 1,
            "customer_po": "TEMP-001",
            "order_date": "2026-06-17",
            "delivery_date": "2026-06-24",
            "items": [
                {
                    "product_id": None,
                    "product_code": "TEMP-BOX",
                    "product_name": "临时新箱",
                    "length_mm": "450",
                    "width_mm": "340",
                    "height_mm": "300",
                    "quantity": 10,
                    "snapshot_material": "K=A",
                    "snapshot_flute_type": "AB",
                    "snapshot_score_line": "340*450*340*450",
                    "snapshot_cardboard_length_mm": "1596.00",
                    "snapshot_cardboard_width_mm": "644.00",
                    "unit_price": "3.60",
                }
            ],
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["items"][0]["snapshot_product_code"] == "TEMP-BOX"
    assert body["items"][0]["snapshot_product_name"] == "临时新箱"
    assert body["items"][0]["subtotal"] == "36.00"

    from sqlalchemy.orm import Session
    from phase1_postgres.models import Product

    with Session(engine) as session:
        persisted = session.scalar(
            select(Product).where(Product.customer_id == 1, Product.historical_search_key.like("%TEMP-BOX%"))
        )
        assert persisted is not None
        assert persisted.default_cardboard_length_mm == Decimal("1596.00")
        assert persisted.default_cardboard_width_mm == Decimal("644.00")
        assert persisted.default_score_line == "340*450*340*450"
        assert persisted.historical_material_code == "K=A"

    lookup = client.get("/api/master/products/history-search", params={"keyword": "TEMP-BOX"})
    assert lookup.status_code == 200
    lookup_body = lookup.json()
    assert lookup_body["matched"] is True
    assert lookup_body["items"][0]["historical_search_key"].find("TEMP-BOX") >= 0
