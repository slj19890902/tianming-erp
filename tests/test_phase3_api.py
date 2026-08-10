from __future__ import annotations

from collections.abc import Generator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture()
def master_data_app(tmp_path: Path):
    from app.api.auth import router as auth_router
    from app.api.customers import router as customers_router
    from app.api.deps import get_db
    from app.api.materials import router as materials_router
    from app.api.products import router as products_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.customer import Customer
    from app.models.material import Material
    from app.models.product import Product
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "api.sqlite3")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as session:
        session.add_all(
            [
                User(
                    username=role,
                    password_hash=hash_password("RolePass123!"),
                    role=role,
                    real_name=role,
                    display_name=role,
                    must_change_password=False,
                )
                for role in ("admin", "finance", "sales", "workshop")
            ]
        )
        customer = Customer(
            customer_number=1,
            customer_code="TH",
            name="苏州天华超净科技股份有限公司",
            payment_term_days=30,
            credit_limit=100000,
        )
        material = Material(
            code="B416B-AB/EB",
            paper_composition="B416B",
            layer_count=5,
            flute_type="AB/EB",
            basis_weight_description="五层标准材质",
            quote_price=1.95,
            supplier_name="嘉林亿",
        )
        session.add_all([customer, material])
        session.flush()
        session.add(
            Product(
                customer_id=customer.id,
                product_code="TH001",
                customer_material_code="TH001",
                product_name="五层加强纸箱",
                material_id=material.id,
                length_mm=520,
                width_mm=350,
                height_mm=300,
                box_category="normal",
                sale_unit_price=3.6,
                cost_unit_price=2.7,
                suggested_price=3.8,
            )
        )
        session.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(customers_router, prefix="/api/customers")
    app.include_router(customers_router, prefix="/api/master/customers")
    app.include_router(materials_router, prefix="/api/master/materials")
    app.include_router(products_router, prefix="/api/master/products")

    def override_get_db() -> Generator[Session, None, None]:
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    app.state.session_factory = session_factory
    return app


def _login(client: TestClient, role: str) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": role, "password": "RolePass123!"},
    )
    assert response.status_code == 200


def test_finance_is_read_only_and_cannot_open_product_master(master_data_app: FastAPI) -> None:
    with TestClient(master_data_app) as client:
        _login(client, "finance")
        assert client.get("/api/master/customers").status_code == 200
        assert client.get("/api/master/products").status_code == 403
        assert client.get("/api/master/materials").status_code == 403
        denied = client.post(
            "/api/master/materials",
            json={
                "code": "K=A-BC",
                "paper_composition": "K=A",
                "layer_count": 5,
                "flute_type": "BC",
            },
        )

    assert denied.status_code == 403
    assert denied.json()["detail"] == "权限不足"


def test_sales_can_edit_assigned_customer_but_cannot_create_customer(
    master_data_app: FastAPI,
) -> None:
    with TestClient(master_data_app) as client:
        _login(client, "sales")
        customer_list = client.get("/api/master/customers")
        assert customer_list.status_code == 200
        created = client.post(
            "/api/master/customers",
            json={
                "customer_number": 2,
                "customer_code": "HC",
                "name": "昆山华诚电子有限公司",
                "payment_term_days": 45,
                "credit_limit": 50000,
            },
        )
        updated = client.put(
            "/api/master/customers/1",
            json={
                "customer_number": 1,
                "customer_code": "TH",
                "name": "苏州天华超净科技股份有限公司",
                "payment_term_days": 60,
                "credit_limit": 80000,
                "expected_version": 1,
                "change_reason": "更新客户账期",
            },
        )

    assert created.status_code == 403
    assert updated.status_code == 200


def test_n028_sales_scope_filters_customers_and_products_and_hides_cost(
    master_data_app: FastAPI,
) -> None:
    from app.models.access_control import UserCustomerScope
    from app.models.customer import Customer
    from app.models.product import Product
    from app.models.user import User

    factory = master_data_app.state.session_factory
    with factory() as session:
        first_customer = session.get(Customer, 1)
        second_customer = Customer(
            customer_number=2,
            customer_code="OTHER",
            name="Other Customer",
        )
        session.add(second_customer)
        session.flush()
        session.add(
            Product(
                customer_id=second_customer.id,
                product_code="OTHER-001",
                customer_material_code="OTHER-001",
                product_name="Other carton",
                box_category="normal",
                sale_unit_price=4,
                cost_unit_price=3,
                suggested_price=5,
            )
        )
        sales = session.query(User).filter(User.username == "sales").one()
        sales.customer_access_mode = "selected"
        session.add(
            UserCustomerScope(user_id=sales.id, customer_id=first_customer.id)
        )
        session.commit()
        second_customer_id = second_customer.id

    with TestClient(master_data_app) as client:
        _login(client, "sales")
        customers = client.get("/api/master/customers")
        products = client.get("/api/master/products")
        forbidden_customer = client.get(
            f"/api/master/customers/{second_customer_id}"
        )

    assert customers.status_code == 200
    assert [item["id"] for item in customers.json()["items"]] == [1]
    assert products.status_code == 200
    assert [item["product_code"] for item in products.json()["items"]] == ["TH001"]
    product = products.json()["items"][0]
    assert "cost_unit_price" not in product
    assert "suggested_price" not in product
    assert forbidden_customer.status_code == 403


def test_workshop_product_response_hides_prices(master_data_app: FastAPI) -> None:
    with TestClient(master_data_app) as client:
        _login(client, "workshop")
        response = client.get("/api/master/products")
        denied = client.post(
            "/api/master/products",
            json={
                "customer_id": 1,
                "product_code": "NEW001",
                "customer_material_code": "NEW001",
                "product_name": "车间尝试新增",
                "box_category": "normal",
            },
        )

    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["page"] == 1
    item = response.json()["items"][0]
    assert "sale_unit_price" not in item
    assert "cost_unit_price" not in item
    assert "suggested_price" not in item
    assert denied.status_code == 403


def test_product_search_combines_code_name_material_and_dimensions(
    master_data_app: FastAPI,
) -> None:
    with TestClient(master_data_app) as client:
        _login(client, "sales")
        response = client.get(
            "/api/master/products",
            params={"keyword": "TH001 520 350 B416B"},
        )

    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["product_code"] == "TH001"


@pytest.mark.parametrize("spec", ["520×350×300", "520 350 300", "520x350x300"])
def test_product_picker_filters_customer_code_name_and_full_specification(
    master_data_app: FastAPI,
    spec: str,
) -> None:
    with TestClient(master_data_app) as client:
        _login(client, "sales")
        response = client.get(
            "/api/master/products",
            params={
                "customer_id": 1,
                "product_code": "TH0",
                "product_name": "加强",
                "spec": spec,
                "page": 1,
                "page_size": 50,
            },
        )

    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["product_code"] == "TH001"


def test_same_customer_code_allows_distinct_names_but_rejects_same_name(
    master_data_app: FastAPI,
) -> None:
    payload = {
        "customer_id": 1,
        "product_code": "TH001",
        "customer_material_code": "TH001",
        "product_name": "五层加强纸箱内衬",
        "box_category": "normal",
    }
    with TestClient(master_data_app) as client:
        _login(client, "admin")
        created = client.post("/api/master/products", json=payload)
        duplicate = client.post("/api/master/products", json=payload)

    assert created.status_code == 201, created.text
    assert created.json()["product_code"] == "TH001"
    assert created.json()["product_name"] == "五层加强纸箱内衬"
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == "同一客户下，存货编码与产品名称的组合不能重复"


def test_order_product_selection_hides_same_code_child_but_keeps_distinct_code_child(
    master_data_app: FastAPI,
) -> None:
    from app.models.product import Product
    from app.models.product_bom import ProductBomComponent

    factory = master_data_app.state.session_factory
    with factory() as session:
        parent = session.get(Product, 1)
        parent.is_composite = True
        hidden_child = Product(
            customer_id=1,
            product_code="TH001",
            customer_material_code="TH001",
            product_name="五层加强纸箱内衬",
            box_category="normal",
            is_internal_component=True,
        )
        visible_child = Product(
            customer_id=1,
            product_code="TH001-PART",
            customer_material_code="TH001-PART",
            product_name="可独立下单隔板",
            box_category="normal",
            is_internal_component=True,
        )
        session.add_all([hidden_child, visible_child])
        session.flush()
        session.add_all(
            [
                ProductBomComponent(
                    parent_product_id=parent.id,
                    component_product_id=hidden_child.id,
                    quantity_per_set=1,
                    display_order=1,
                    internal_component_code="TH001-S01",
                    display_mode="show_on_all_docs",
                ),
                ProductBomComponent(
                    parent_product_id=parent.id,
                    component_product_id=visible_child.id,
                    quantity_per_set=1,
                    display_order=2,
                    internal_component_code="TH001-S02",
                    display_mode="internal_only",
                ),
            ]
        )
        session.commit()

    with TestClient(master_data_app) as client:
        _login(client, "sales")
        master_data = client.get(
            "/api/master/products",
            params={"customer_id": 1, "page_size": 50},
        )
        order_picker = client.get(
            "/api/master/products",
            params={
                "customer_id": 1,
                "page_size": 50,
                "selection_context": "order",
            },
        )

    assert master_data.status_code == 200
    assert master_data.json()["total"] == 3
    assert order_picker.status_code == 200
    assert {
        (row["product_code"], row["product_name"])
        for row in order_picker.json()["items"]
    } == {
        ("TH001", "五层加强纸箱"),
        ("TH001-PART", "可独立下单隔板"),
    }


def test_public_customer_alias_requires_login_and_hides_legacy_trace_fields(
    master_data_app: FastAPI,
) -> None:
    with TestClient(master_data_app) as client:
        anonymous = client.get("/api/customers")
        _login(client, "sales")
        authed = client.get("/api/customers")

    assert anonymous.status_code == 401
    assert authed.status_code == 200
    assert authed.json()["page_size"] == 25
    first = authed.json()["items"][0]
    assert "billing_note" not in first
    assert "legacy_customer_id" not in first
    assert "ruida" not in str(first).lower()


def test_product_status_toggle_hides_inactive_from_default_search(
    master_data_app: FastAPI,
) -> None:
    with TestClient(master_data_app) as client:
        _login(client, "admin")
        disabled = client.put(
            "/api/master/products/1/status",
            json={"is_active": False, "expected_version": 1, "change_reason": "停用测试产品"},
        )
        default_list = client.get("/api/master/products")
        full_list = client.get(
            "/api/master/products",
            params={"include_inactive": True},
        )
        historical_detail = client.get("/api/master/products/1")

    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["is_active"] is False
    assert default_list.json()["total"] == 0
    assert full_list.json()["total"] == 1
    assert full_list.json()["items"][0]["is_active"] is False
    assert historical_detail.status_code == 200


def test_admin_can_move_new_product_to_trash(master_data_app: FastAPI) -> None:
    from app.models.product import Product

    with TestClient(master_data_app) as client:
        _login(client, "admin")
        response = client.request(
            "DELETE",
            "/api/master/products/1",
            json={"expected_version": 1, "change_reason": "移入测试垃圾站"},
        )
        default_list = client.get("/api/master/products")

    assert response.status_code == 200
    assert response.json()["deleted_at"] is not None
    assert default_list.json()["total"] == 0
    with master_data_app.state.session_factory() as session:
        assert session.get(Product, 1) is not None


def test_delete_used_product_moves_to_trash_and_preserves_order(
    master_data_app: FastAPI,
) -> None:
    from app.models.order import Order, OrderItem
    from app.models.product import Product

    session_factory = master_data_app.state.session_factory
    with session_factory() as session:
        order = Order(
            order_number="PO-20260614-998",
            customer_id=1,
            order_date=date(2026, 6, 14),
            delivery_date=date(2026, 6, 21),
            status="pending_production",
            payment_status="unpaid",
            total_amount=Decimal("360.00"),
        )
        session.add(order)
        session.flush()
        session.add(
            OrderItem(
                order_id=order.id,
                product_id=1,
                quantity=100,
                unit_price=Decimal("3.60"),
                subtotal=Decimal("360.00"),
                snapshot_product_name="五层加强纸箱",
            )
        )
        session.commit()

    with TestClient(
        master_data_app,
        raise_server_exceptions=False,
    ) as client:
        _login(client, "admin")
        response = client.request(
            "DELETE",
            "/api/master/products/1",
            json={"expected_version": 1, "change_reason": "保留订单并移入垃圾站"},
        )

    assert response.status_code == 200
    assert response.json()["deleted_at"] is not None
    with session_factory() as session:
        product = session.get(Product, 1)
        assert product is not None
        assert product.deleted_at is not None
        assert session.query(OrderItem).count() == 1
