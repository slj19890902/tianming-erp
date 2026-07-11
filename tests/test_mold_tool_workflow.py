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
def mold_app(tmp_path: Path):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.orders import router as orders_router
    from app.api.products import router as products_router
    from app.api.warehouse import router as warehouse_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.customer import Customer
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "mold-tools.sqlite3")
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
                for role in ("admin", "workshop")
            ]
        )
        db.add(
            Customer(
                customer_number=9901,
                customer_code="MOLD-C",
                name="模具联动测试客户",
                payment_term_days=30,
                credit_limit=Decimal("100000"),
            )
        )
        db.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(products_router, prefix="/api/master/products")
    app.include_router(warehouse_router, prefix="/api/warehouse")
    app.include_router(orders_router, prefix="/api/orders")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    return app, factory


def _login(client: TestClient, role: str) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": role, "password": "RolePass123!"},
    )
    assert response.status_code == 200, response.text


def test_admin_creates_mold_and_common_box_binding_is_searchable(mold_app) -> None:
    app, _factory = mold_app
    with TestClient(app) as client:
        _login(client, "admin")
        created_mold = client.post(
            "/api/warehouse/molds",
            json={
                "mold_code": "MJ-A1-001",
                "mold_name": "21301010 开槽模",
                "rack_location": "二楼模具架 B-12",
                "remarks": "天华常用模具",
            },
        )
        assert created_mold.status_code == 201, created_mold.text
        mold = created_mold.json()

        created_product = client.post(
            "/api/master/products",
            json={
                "customer_id": 1,
                "product_code": "21301010",
                "customer_material_code": "TH-21301010",
                "product_name": "天华测试外箱",
                "box_category": "normal",
                "production_process": "模切",
                "mold_tool_id": mold["id"],
            },
        )
        assert created_product.status_code == 201, created_product.text
        product = created_product.json()
        assert product["mold_tool"]["mold_code"] == "MJ-A1-001"
        assert product["mold_tool"]["rack_location"] == "二楼模具架 B-12"

        by_product = client.get("/api/warehouse/molds", params={"q": "21301010"})
        assert by_product.status_code == 200, by_product.text
        row = by_product.json()["items"][0]
        assert row["mold_code"] == "MJ-A1-001"
        assert row["product_count"] == 1
        assert row["products"][0]["product_code"] == "21301010"

        legacy_lookup = client.get(
            "/api/warehouse/references/template-locations",
            params={"q": "B-12"},
        )
        assert legacy_lookup.status_code == 200, legacy_lookup.text
        assert legacy_lookup.json()["items"][0]["template_location"] == "二楼模具架 B-12"


def test_workshop_can_query_but_cannot_modify_mold(mold_app) -> None:
    app, factory = mold_app
    from app.models.mold_tool import MoldTool
    from app.models.product import Product

    with factory() as db:
        mold = MoldTool(
            mold_code="MJ-W-001",
            mold_name="车间查询模",
            rack_location="一楼模具架 A-03",
        )
        db.add(mold)
        db.flush()
        db.add(
            Product(
                customer_id=1,
                product_code="WORK-001",
                customer_material_code="WORK-M-001",
                product_name="车间查询产品",
                box_category="normal",
                mold_tool_id=mold.id,
            )
        )
        db.commit()

    with TestClient(app) as client:
        _login(client, "workshop")
        listed = client.get("/api/warehouse/molds", params={"q": "WORK-001"})
        assert listed.status_code == 200, listed.text
        assert listed.json()["items"][0]["rack_location"] == "一楼模具架 A-03"
        denied = client.post(
            "/api/warehouse/molds",
            json={
                "mold_code": "DENIED",
                "mold_name": "无权限",
                "rack_location": "无权限",
            },
        )
        assert denied.status_code == 403


def test_order_response_exposes_current_mold_location_to_workshop(mold_app) -> None:
    app, factory = mold_app
    from app.models.mold_tool import MoldTool
    from app.models.order import Order, OrderItem
    from app.models.product import Product

    with factory() as db:
        mold = MoldTool(
            mold_code="MJ-ORDER-001",
            mold_name="订单生产模",
            rack_location="生产模具架 C-08",
        )
        db.add(mold)
        db.flush()
        product = Product(
            customer_id=1,
            product_code="ORDER-MOLD-001",
            customer_material_code="ORDER-MOLD-M1",
            product_name="订单模具联动产品",
            box_category="normal",
            production_process="模切",
            mold_tool_id=mold.id,
        )
        db.add(product)
        db.flush()
        order = Order(
            order_number="TM20990101001",
            customer_id=1,
            customer_po="MOLD-PO-001",
            order_date=date.today(),
            status="pending_production",
            payment_status="unpaid",
            total_amount=Decimal("10.00"),
        )
        db.add(order)
        db.flush()
        db.add(
            OrderItem(
                order_id=order.id,
                product_id=product.id,
                item_order_number="TM20990101001-001",
                item_sequence=1,
                quantity=10,
                delivered_quantity=0,
                unit_price=Decimal("1.0000"),
                subtotal=Decimal("10.00"),
                material_status="pending",
                snapshot_product_name=product.product_name,
                snapshot_product_code=product.product_code,
                requisition_status="未报料",
                special_process="无",
            )
        )
        db.commit()

    with TestClient(app) as client:
        _login(client, "workshop")
        response = client.get("/api/orders", params={"page_size": 25})
        assert response.status_code == 200, response.text
        item = response.json()["items"][0]["items"][0]
        assert item["mold_code"] == "MJ-ORDER-001"
        assert item["mold_name"] == "订单生产模"
        assert item["mold_location"] == "生产模具架 C-08"
        assert "unit_price" not in item


def test_mold_frontend_connects_location_common_box_and_order_display() -> None:
    warehouse = Path("static/warehouse.html").read_text(encoding="utf-8")
    index = Path("static/index.html").read_text(encoding="utf-8")
    for marker in (
        "新增 / 编辑生产模具",
        "固定货架位置",
        "/api/warehouse/molds",
        "已绑定常用箱",
    ):
        assert marker in warehouse
    assert 'v-model="productForm.mold_tool_id"' in index
    assert 'v-if="productUsesMold(productForm)"' in index
    assert "productMoldError" in index
    assert "moldLocationText(item)" in index
    assert "模具：{{ moldLocationText(item) }}" in index


def test_mold_is_required_only_for_die_cut_products(mold_app) -> None:
    app, _factory = mold_app
    with TestClient(app) as client:
        _login(client, "admin")
        missing = client.post(
            "/api/master/products",
            json={
                "customer_id": 1,
                "product_code": "DIE-NO-MOLD",
                "customer_material_code": "DIE-NO-MOLD",
                "product_name": "缺少模具",
                "box_category": "die_cut",
                "production_process": "模切",
            },
        )
        assert missing.status_code == 400
        assert "必须选择已登记的生产模具" in missing.json()["detail"]

        mold = client.post(
            "/api/warehouse/molds",
            json={
                "mold_code": "MJ-NON-DIE",
                "mold_name": "非模切不应绑定",
                "rack_location": "测试架 Z-01",
            },
        ).json()
        ordinary = client.post(
            "/api/master/products",
            json={
                "customer_id": 1,
                "product_code": "NO-DIE-CLEAR",
                "customer_material_code": "NO-DIE-CLEAR",
                "product_name": "普通产品",
                "box_category": "normal",
                "production_process": "粘贴",
                "mold_tool_id": mold["id"],
            },
        )
        assert ordinary.status_code == 201, ordinary.text
        assert ordinary.json()["mold_tool_id"] is None
        assert ordinary.json()["mold_tool"] is None
