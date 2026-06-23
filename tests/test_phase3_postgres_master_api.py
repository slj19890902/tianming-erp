from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
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

    app = create_app(engine=engine, seed_demo_data=False)

    def override_session():
        from sqlalchemy.orm import Session

        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    return TestClient(app)


def test_customers_restful_create_list_update():
    client = make_client()

    created = client.post(
        "/api/master/customers",
        json={
            "customer_code": "TH",
            "name": "天华超净",
            "short_name": "天华",
            "contact_person": "王经理",
            "phone": "0512-88888888",
            "address": "苏州工业园区",
            "payment_term_days": 30,
            "delivery_method": "配送",
            "default_tax_rate": "0.13",
            "note": "长期客户",
            "is_active": True,
        },
    )
    assert created.status_code == 201
    customer_id = created.json()["id"]

    listed = client.get("/api/master/customers", params={"keyword": "天华"})
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert listed.json()["items"][0]["name"] == "天华超净"

    updated = client.put(
        f"/api/master/customers/{customer_id}",
        json={
            "customer_code": "TH",
            "name": "天华超净科技",
            "short_name": "天华",
            "contact_person": "王经理",
            "phone": "0512-88888888",
            "address": "苏州工业园区",
            "payment_term_days": 45,
            "delivery_method": "物流",
            "default_tax_rate": "0.13",
            "note": "账期调整",
            "is_active": True,
        },
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "天华超净科技"
    assert updated.json()["delivery_method"] == "物流"


def test_customer_status_can_be_disabled_hidden_and_reenabled():
    client = make_client()
    customer = client.post(
        "/api/master/customers",
        json={
            "customer_code": "STOP",
            "name": "昆山待停用客户",
            "delivery_method": "配送",
        },
    ).json()

    disabled = client.put(
        f"/api/master/customers/{customer['id']}/status",
        json={"is_active": False},
    )
    default_list = client.get("/api/master/customers").json()
    inclusive_list = client.get(
        "/api/master/customers",
        params={"include_inactive": True},
    ).json()
    enabled = client.put(
        f"/api/master/customers/{customer['id']}/status",
        json={"is_active": True},
    )

    assert disabled.status_code == 200
    assert disabled.json()["is_active"] is False
    assert default_list["total"] == 0
    assert inclusive_list["items"][0]["is_active"] is False
    assert enabled.status_code == 200
    assert enabled.json()["is_active"] is True


def test_materials_flutes_and_products_restful_flow():
    client = make_client()

    customer = client.post(
        "/api/master/customers",
        json={
            "customer_code": "SME",
            "name": "苏州思迈尔包装有限公司",
            "delivery_method": "配送",
        },
    ).json()
    flute = client.post(
        "/api/master/flute-types",
        json={
            "code": "AB",
            "name": "AB楞",
            "add_width_mm": "8",
            "basis_weight_gsm": "120",
            "freight_rate": "0.035",
            "loss_rate": "0.03",
            "note": "五层常用楞型",
            "is_active": True,
        },
    ).json()
    material = client.post(
        "/api/master/materials",
        json={
            "code": "K=A",
            "name": "五层加强纸板",
            "paper_composition": "K纸=A纸",
            "basis_weight_description": "170g/130g/80g/170g/150g",
            "layer_count": 5,
            "flute_type_id": flute["id"],
            "customer_square_price": "3.5000",
            "supplier_square_price": "2.8500",
            "note": "常用材质",
            "is_active": True,
        },
    ).json()

    created = client.post(
        "/api/master/products",
        json={
            "customer_id": customer["id"],
            "product_code": "001A",
            "customer_material_code": "001A",
            "product_name": "001A外箱",
            "material_id": material["id"],
            "flute_type_id": flute["id"],
            "length_mm": "450",
            "width_mm": "340",
            "height_mm": "300",
            "box_category": "normal",
            "box_style": "0201",
            "production_process": "钉箱",
            "default_score_line": "340*110*340",
            "default_cardboard_length_mm": "916",
            "default_cardboard_width_mm": "644",
            "default_unit_price": "3.6500",
            "note": "UAT常用箱",
            "is_active": True,
        },
    )
    assert created.status_code == 201
    product_id = created.json()["id"]

    listed = client.get("/api/master/products", params={"keyword": "001A 外箱"})
    assert listed.status_code == 200
    row = listed.json()["items"][0]
    assert row["customer_name"] == "苏州思迈尔包装有限公司"
    assert row["material_code"] == "K=A"
    assert row["flute_type_code"] == "AB"
    assert Decimal(row["default_unit_price"]) == Decimal("3.6500")

    updated = client.put(
        f"/api/master/products/{product_id}",
        json={
            **created.json(),
            "product_name": "001A外箱-新版",
            "default_unit_price": "3.8000",
        },
    )
    assert updated.status_code == 200
    assert updated.json()["product_name"] == "001A外箱-新版"
    assert updated.json()["default_unit_price"] == "3.8000"


def test_material_and_flute_delete_disable_records():
    client = make_client()

    flute = client.post(
        "/api/master/flute-types",
        json={"code": "BE", "name": "BE楞", "is_active": True},
    ).json()
    material = client.post(
        "/api/master/materials",
        json={"code": "A=B", "name": "三层纸板", "flute_type_id": flute["id"], "is_active": True},
    ).json()

    assert client.delete(f"/api/master/materials/{material['id']}").status_code == 204
    assert client.delete(f"/api/master/flute-types/{flute['id']}").status_code == 204

    materials = client.get("/api/master/materials").json()
    flutes = client.get("/api/master/flute-types").json()
    assert materials["total"] == 0
    assert flutes["total"] == 0

    inactive_materials = client.get("/api/master/materials", params={"include_inactive": True}).json()
    inactive_flutes = client.get("/api/master/flute-types", params={"include_inactive": True}).json()
    assert inactive_materials["total"] == 1
    assert inactive_flutes["total"] == 1
