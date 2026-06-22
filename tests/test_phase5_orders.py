from __future__ import annotations

from collections.abc import Generator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture()
def order_api_app(tmp_path: Path):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.orders import router as orders_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.customer import Customer
    from app.models.material import Material
    from app.models.product import Product
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "orders.sqlite3")
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
            customer_code="SME",
            name="苏州思迈尔包装有限公司",
            payment_term_days=30,
            credit_limit=Decimal("100000"),
        )
        material = Material(
            code="K=A-BC",
            paper_composition="K=A",
            layer_count=5,
            flute_type="BC",
        )
        session.add_all([customer, material])
        session.flush()
        session.add_all(
            [
                Product(
                    customer_id=customer.id,
                    product_code="SME-001",
                    customer_material_code="KH-001",
                    product_name="五层加强纸箱",
                    material_id=material.id,
                    length_mm=Decimal("520"),
                    width_mm=Decimal("350"),
                    height_mm=Decimal("300"),
                    box_category="normal",
                ),
                Product(
                    customer_id=customer.id,
                    product_code="SME-002",
                    customer_material_code="KH-002",
                    product_name="物流周转箱",
                    legacy_material_text="A=B",
                    length_mm=Decimal("380"),
                    width_mm=Decimal("260"),
                    height_mm=Decimal("220"),
                    box_category="normal",
                ),
            ]
        )
        session.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(orders_router, prefix="/api/orders")

    def override_get_db() -> Generator[Session, None, None]:
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    return app, session_factory


def _login(client: TestClient, role: str = "sales") -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": role, "password": "RolePass123!"},
    )
    assert response.status_code == 200


def _payload() -> dict:
    return {
        "customer_id": 1,
        "customer_po": "PO-CUSTOMER-001",
        "order_date": "2026-06-13",
        "delivery_date": "2026-06-20",
        "items": [
            {"product_id": 1, "quantity": 200, "unit_price": "3.60"},
            {"product_id": 2, "quantity": 100, "unit_price": "2.45"},
        ],
    }


def test_create_multi_item_order_is_atomic_and_snapshots_products(
    order_api_app,
) -> None:
    from app.models.order import Order, OrderItem

    app, session_factory = order_api_app
    with TestClient(app) as client:
        _login(client)
        response = client.post("/api/orders", json=_payload())

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["order_number"] == "TM20260613001"
    assert Decimal(str(body["total_amount"])) == Decimal("965.00")
    assert len(body["items"]) == 2
    assert body["items"][0]["item_order_number"] == "TM20260613001-001"
    assert body["items"][0]["item_sequence"] == 1
    assert body["items"][1]["item_order_number"] == "TM20260613001-002"
    assert body["items"][1]["item_sequence"] == 2
    assert body["items"][0]["snapshot_product_name"] == "五层加强纸箱"
    assert body["items"][0]["snapshot_spec"] == "520脳350脳300mm"
    assert body["items"][0]["snapshot_material"] == "K=A-BC"
    assert body["items"][0]["material_status"] == "pending"

    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Order)) == 1
        assert session.scalar(select(func.count()).select_from(OrderItem)) == 2


def test_create_order_accepts_editable_product_snapshot(order_api_app) -> None:
    app, _ = order_api_app
    payload = _payload()
    payload["items"] = [
        {
            "product_id": 1,
            "quantity": 20,
            "unit_price": "3.60",
            "product_code": "21301028-TEMP",
            "product_name": "本单临时外箱",
            "material": "K=K-BC",
            "specification": "530脳360脳310mm",
        }
    ]
    with TestClient(app) as client:
        _login(client)
        response = client.post("/api/orders", json=payload)

    assert response.status_code == 201, response.text
    item = response.json()["items"][0]
    assert item["snapshot_product_code"] == "21301028-TEMP"
    assert item["snapshot_product_name"] == "本单临时外箱"
    assert item["snapshot_material"] == "K=K-BC"
    assert item["snapshot_spec"] == "530脳360脳310mm"
    assert item["item_order_number"] == "TM20260613001-001"


def test_missing_product_rolls_back_master_and_all_items(order_api_app) -> None:
    from app.models.order import Order, OrderItem

    app, session_factory = order_api_app
    payload = _payload()
    payload["items"][1]["product_id"] = 9999
    with TestClient(app) as client:
        _login(client)
        response = client.post("/api/orders", json=payload)

    assert response.status_code == 400
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Order)) == 0
        assert session.scalar(select(func.count()).select_from(OrderItem)) == 0


def test_inactive_product_cannot_be_used_for_new_order(order_api_app) -> None:
    from app.models.product import Product

    app, session_factory = order_api_app
    with session_factory() as session:
        session.get(Product, 1).is_active = False
        session.commit()
    payload = _payload()
    payload["items"] = [payload["items"][0]]

    with TestClient(app) as client:
        _login(client)
        response = client.post("/api/orders", json=payload)

    assert response.status_code == 400
    assert response.json()["detail"] == "该纸箱已停用，不能用于新建订单"


def test_invalid_money_rolls_back_order(order_api_app) -> None:
    from app.models.order import Order

    app, session_factory = order_api_app
    payload = _payload()
    payload["items"][1]["unit_price"] = "-0.01"
    with TestClient(app) as client:
        _login(client)
        response = client.post("/api/orders", json=payload)

    assert response.status_code == 400
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Order)) == 0


def test_daily_order_numbers_increment_and_finance_cannot_create(
    order_api_app,
) -> None:
    app, _ = order_api_app
    with TestClient(app) as client:
        _login(client)
        first = client.post("/api/orders", json=_payload())
        second_payload = _payload()
        second_payload["customer_po"] = "PO-CUSTOMER-002"
        second = client.post("/api/orders", json=second_payload)
        client.post("/api/auth/logout")
        _login(client, "finance")
        denied = client.post("/api/orders", json=_payload())

    assert first.json()["order_number"] == "TM20260613001"
    assert second.json()["order_number"] == "TM20260613002"
    assert denied.status_code == 403


def test_order_list_uses_new_multi_item_orders_and_masks_workshop_prices(
    order_api_app,
) -> None:
    app, _ = order_api_app
    with TestClient(app) as client:
        _login(client, "admin")
        created = client.post("/api/orders", json=_payload())
        assert created.status_code == 201, created.text
        admin_list = client.get("/api/orders")
        client.post("/api/auth/logout")
        _login(client, "workshop")
        workshop_list = client.get("/api/orders")

    assert admin_list.status_code == 200, admin_list.text
    assert admin_list.json()["items"][0]["order_number"].startswith("TM")
    assert "total_amount" in admin_list.json()["items"][0]
    assert "unit_price" in admin_list.json()["items"][0]["items"][0]
    assert admin_list.json()["items"][0]["items"][0]["item_order_number"].startswith(
        "TM"
    )
    assert workshop_list.status_code == 200, workshop_list.text
    assert "total_amount" not in workshop_list.json()["items"][0]
    assert "unit_price" not in workshop_list.json()["items"][0]["items"][0]
    assert "subtotal" not in workshop_list.json()["items"][0]["items"][0]


def test_order_list_supports_tm_display_search_and_hides_legacy_raw_number(
    order_api_app,
) -> None:
    from app.models.order import Order

    app, session_factory = order_api_app
    with TestClient(app) as client:
        _login(client, "admin")
        created = client.post("/api/orders", json=_payload())
        order_id = created.json()["id"]
        with session_factory() as session:
            order = session.get(Order, order_id)
            order.order_number = "RUIDA-42838"
            session.commit()

        keyword = client.get("/api/orders", params={"keyword": "TM20260613-0001"})
        exact = client.get(
            "/api/orders",
            params={"order_number": "TM20260613-0001"},
        )
        customer = client.get(
            "/api/orders",
            params={"customer_name": "思迈尔"},
        )
        missing = client.get(
            "/api/orders",
            params={"order_number": "TM20260613-9999"},
        )

    assert keyword.status_code == 200
    assert keyword.json()["total"] == 1
    assert keyword.json()["items"][0]["display_order_number"] == "TM20260613-0001"
    assert keyword.json()["items"][0]["order_number"] == "TM20260613-0001"
    assert "RUIDA" not in str(keyword.json())
    assert exact.json()["total"] == 1
    assert customer.json()["total"] == 1
    assert missing.json()["total"] == 0


def test_order_detail_get_returns_nested_items_and_404_without_writing(
    order_api_app,
) -> None:
    from app.models.order import Order, OrderItem

    app, session_factory = order_api_app
    with TestClient(app) as client:
        _login(client, "admin")
        created = client.post("/api/orders", json=_payload()).json()
        with session_factory() as session:
            before = (
                session.scalar(select(func.count()).select_from(Order)),
                session.scalar(select(func.count()).select_from(OrderItem)),
            )

        detail = client.get(f"/api/orders/{created['id']}")
        missing = client.get("/api/orders/999999")

        with session_factory() as session:
            after = (
                session.scalar(select(func.count()).select_from(Order)),
                session.scalar(select(func.count()).select_from(OrderItem)),
            )

    assert detail.status_code == 200
    assert detail.json()["id"] == created["id"]
    assert detail.json()["display_order_number"].startswith("TM")
    assert detail.json()["customer_name"] == "苏州思迈尔包装有限公司"
    assert len(detail.json()["items"]) == 2
    assert detail.json()["items"][0]["item_order_number"] == "TM20260613001-001"
    assert detail.json()["items"][0]["item_sequence"] == 1
    assert detail.json()["items"][0]["snapshot_product_name"] == "五层加强纸箱"
    assert missing.status_code == 404
    assert before == after == (1, 2)


def test_order_list_supports_search_by_item_number_product_code_and_product_name(
    order_api_app,
) -> None:
    app, _ = order_api_app
    with TestClient(app) as client:
        _login(client, "admin")
        created = client.post("/api/orders", json=_payload())
        assert created.status_code == 201, created.text

        by_item_number = client.get(
            "/api/orders",
            params={"keyword": "TM20260613001-002"},
        )
        by_product_code = client.get("/api/orders", params={"keyword": "SME-002"})
        by_product_name = client.get("/api/orders", params={"keyword": "物流周转箱"})

    assert by_item_number.status_code == 200
    assert by_item_number.json()["total"] == 1
    assert by_item_number.json()["items"][0]["order_number"] == "TM20260613001"
    assert by_product_code.json()["total"] == 1
    assert by_product_name.json()["total"] == 1


def test_sales_can_edit_order_header_customer_po_without_changing_order_number(
    order_api_app,
) -> None:
    app, _ = order_api_app
    with TestClient(app) as client:
        _login(client, "sales")
        created = client.post("/api/orders", json=_payload())
        order = created.json()
        updated = client.put(
            f"/api/orders/{order['id']}",
            json={
                "customer_po": "PO-CUSTOMER-EDIT-002",
                "delivery_date": "2026-06-25",
                "remark": "更新客户单号后重新分组",
            },
        )
        listed = client.get("/api/orders", params={"keyword": "PO-CUSTOMER-EDIT-002"})

    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert body["order_number"] == "TM20260613001"
    assert body["customer_po"] == "PO-CUSTOMER-EDIT-002"
    assert body["delivery_date"] == "2026-06-25"
    assert body["remark"] == "更新客户单号后重新分组"
    assert body["group_key"] == "1::PO-CUSTOMER-EDIT-002"
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert listed.json()["items"][0]["group_key"] == "1::PO-CUSTOMER-EDIT-002"


def test_order_list_and_detail_expose_display_material_without_rewriting_snapshot(
    order_api_app,
) -> None:
    app, _ = order_api_app
    payload = _payload()
    payload["items"] = [
        {
            "product_id": 1,
            "quantity": 12,
            "unit_price": "3.60",
            "material": "045 A113B",
        }
    ]
    with TestClient(app) as client:
        _login(client, "admin")
        created = client.post("/api/orders", json=payload).json()
        listed = client.get("/api/orders")
        detail = client.get(f"/api/orders/{created['id']}")

    assert listed.status_code == 200
    list_item = listed.json()["items"][0]["items"][0]
    detail_item = detail.json()["items"][0]
    assert list_item["snapshot_material"] == "045 A113B"
    assert list_item["display_material"] == "A113B"
    assert detail_item["snapshot_material"] == "045 A113B"
    assert detail_item["display_material"] == "A113B"


def test_sales_can_edit_and_delete_one_order_item(order_api_app) -> None:
    app, _ = order_api_app
    with TestClient(app) as client:
        _login(client, "sales")
        created = client.post("/api/orders", json=_payload())
        order = created.json()
        first_id = order["items"][0]["id"]
        second_id = order["items"][1]["id"]
        edited = client.put(
            f"/api/orders/items/{first_id}",
            json={
                "quantity": 210,
                "unit_price": "3.70",
                "product_code": "SME-001-A",
                "product_name": "中性外箱微调",
                "material": "K=A-BC",
                "specification": "525脳350脳300mm",
            },
        )
        deleted = client.delete(f"/api/orders/items/{second_id}")
        listed = client.get("/api/orders")

    assert edited.status_code == 200, edited.text
    assert edited.json()["snapshot_product_code"] == "SME-001-A"
    assert edited.json()["quantity"] == 210
    assert deleted.status_code == 204
    refreshed = listed.json()["items"][0]
    assert len(refreshed["items"]) == 1
    assert Decimal(str(refreshed["total_amount"])) == Decimal("777.00")


def test_order_models_use_new_tables_and_leave_legacy_name_free() -> None:
    from app.models.order import Order, OrderItem

    assert Order.__tablename__ == "sales_orders"
    assert OrderItem.__tablename__ == "sales_order_items"
    assert date.fromisoformat("2026-06-13").strftime("%Y%m%d") == "20260613"
