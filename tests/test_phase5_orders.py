from __future__ import annotations

from collections.abc import Generator
from datetime import date, datetime, timedelta
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


def _login(client: TestClient, role: str = "admin") -> None:
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


def test_new_order_rejects_common_box_with_mismatched_crease_width(
    order_api_app,
) -> None:
    from app.models.product import Product

    app, session_factory = order_api_app
    with session_factory() as session:
        product = session.get(Product, 1)
        product.report_length_mm = 1030
        product.report_width_mm = 355
        product.crease_type = "压线"
        product.crease_left_mm = 100
        product.crease_middle_mm = 150
        product.crease_right_mm = 100
        session.commit()

    payload = _payload()
    payload["items"] = [payload["items"][0]]
    with TestClient(app) as client:
        _login(client)
        response = client.post("/api/orders", json=payload)

    assert response.status_code == 400
    assert "第1条明细常用箱报料尺寸不一致" in response.json()["detail"]
    assert "三段合计 350mm" in response.json()["detail"]


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
    from app.models.customer import Customer
    from app.models.product import Product

    app, session_factory = order_api_app
    with session_factory() as session:
        customer_name = session.scalar(select(Customer.name).where(Customer.id == 1))
    assert customer_name

    with TestClient(app) as client:
        _login(client, "admin")
        created = client.post("/api/orders", json=_payload())
        assert created.status_code == 201, created.text

        with session_factory() as session:
            product = session.get(Product, 1)
            assert product is not None
            product.product_code = "CURRENT-BOX-001"
            session.commit()

        by_item_number = client.get(
            "/api/orders",
            params={"keyword": "TM20260613001-002"},
        )
        by_product_code = client.get("/api/orders", params={"keyword": "SME-002"})
        by_snapshot_product_code = client.get(
            "/api/orders", params={"keyword": "SME-001"}
        )
        by_current_product_code = client.get(
            "/api/orders", params={"keyword": "CURRENT-BOX-001"}
        )
        by_product_name = client.get("/api/orders", params={"keyword": "物流周转箱"})
        by_customer_name = client.get(
            "/api/orders", params={"keyword": customer_name}
        )
        by_spec = client.get("/api/orders", params={"keyword": "380"})
        by_material = client.get("/api/orders", params={"keyword": "A=B"})

    assert by_item_number.status_code == 200
    assert by_item_number.json()["total"] == 1
    assert by_item_number.json()["items"][0]["order_number"] == "TM20260613001"
    assert by_product_code.json()["total"] == 1
    assert by_snapshot_product_code.json()["total"] == 1
    assert by_current_product_code.json()["total"] == 1
    assert by_product_name.json()["total"] == 1
    assert by_customer_name.json()["total"] == 1
    assert by_spec.json()["total"] == 1
    assert by_material.json()["total"] == 1


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


def test_sales_can_edit_but_only_authorized_user_can_delete_order_item(
    order_api_app,
) -> None:
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
        denied = client.delete(f"/api/orders/items/{second_id}")
        client.post("/api/auth/logout")
        _login(client, "admin")
        deleted = client.delete(f"/api/orders/items/{second_id}")
        listed = client.get("/api/orders")

    assert edited.status_code == 200, edited.text
    assert edited.json()["snapshot_product_code"] == "SME-001-A"
    assert edited.json()["quantity"] == 210
    assert denied.status_code == 403
    assert deleted.status_code == 204
    refreshed = listed.json()["items"][0]
    assert len(refreshed["items"]) == 1
    assert Decimal(str(refreshed["total_amount"])) == Decimal("777.00")


def test_order_item_edit_syncs_common_box_fields_in_same_save(order_api_app) -> None:
    from app.models.product import Product

    app, session_factory = order_api_app
    with TestClient(app) as client:
        _login(client, "sales")
        created = client.post("/api/orders", json=_payload()).json()
        item_id = created["items"][0]["id"]
        edited = client.put(
            f"/api/orders/items/{item_id}",
            json={
                "quantity": 500,
                "unit_price": "3.92",
                "product_code": "SME-001",
                "product_name": "同步常用箱测试",
                "material": "A6A",
                "specification": "880×670×110mm",
                "box_style": "A1/0201 普通开槽箱",
                "length_mm": 880,
                "width_mm": 670,
                "height_mm": 110,
                "snapshot_splice_mode": "double",
                "snapshot_pieces_per_box": 2,
                "snapshot_flap_mm": 30,
                "snapshot_report_length_mm": 3130,
                "snapshot_report_width_mm": 780,
                "snapshot_crease_type": "压线",
                "snapshot_crease_left_mm": 335,
                "snapshot_crease_middle_mm": 110,
                "snapshot_crease_right_mm": 335,
                "snapshot_base_report_length_mm": 3030,
                "snapshot_base_report_width_mm": 760,
                "snapshot_base_crease_type": "压线",
                "snapshot_base_crease_left_mm": 330,
                "snapshot_base_crease_middle_mm": 100,
                "snapshot_base_crease_right_mm": 330,
                "snapshot_base_report_notes": "底盒单独开料",
                "production_process": "粘贴",
                "print_content": "单色印刷",
                "product_remark": "订单编辑同步",
                "sync_product": True,
            },
        )
        assert edited.status_code == 200, edited.text

    with session_factory() as session:
        product = session.get(Product, 1)
        assert product.box_style == "A1/0201 普通开槽箱"
        assert int(product.length_mm) == 880
        assert int(product.width_mm) == 670
        assert int(product.height_mm) == 110
        assert product.splice_mode == "double"
        assert product.pieces_per_box == 2
        assert product.report_length_mm == 3130
        assert product.crease_middle_mm == 110
        assert product.base_report_length_mm == 3030
        assert product.base_report_width_mm == 760
        assert product.base_crease_type == "压线"
        assert product.base_crease_middle_mm == 100
        assert product.base_report_notes == "底盒单独开料"
        assert product.production_process == "粘贴"
        assert product.print_content == "单色印刷"
        assert product.remark == "订单编辑同步"


def test_legacy_crease_mismatch_allows_unrelated_edit_without_overwriting_product(
    order_api_app,
) -> None:
    from app.models.order import OrderItem
    from app.models.product import Product

    app, session_factory = order_api_app
    with TestClient(app) as client:
        _login(client, "sales")
        created = client.post("/api/orders", json={**_payload(), "items": [_payload()["items"][0]]})
        assert created.status_code == 201, created.text
        item_id = created.json()["items"][0]["id"]

    with session_factory() as session:
        item = session.get(OrderItem, item_id)
        product = session.get(Product, item.product_id)
        item.snapshot_report_length_mm = 1030
        item.snapshot_report_width_mm = 355
        item.snapshot_crease_type = "压线"
        item.snapshot_crease_left_mm = 100
        item.snapshot_crease_middle_mm = 150
        item.snapshot_crease_right_mm = 100
        product.report_length_mm = 1030
        product.report_width_mm = 350
        product.crease_type = "压线"
        product.crease_left_mm = 100
        product.crease_middle_mm = 150
        product.crease_right_mm = 100
        session.commit()

    edit_payload = {
        "quantity": 200,
        "unit_price": "3.75",
        "product_code": "SME-001",
        "product_name": "五层加强纸箱",
        "material": "K=A-BC",
        "specification": "520×350×300mm",
        "snapshot_report_length_mm": 1030,
        "snapshot_report_width_mm": 355,
        "snapshot_crease_type": "压线",
        "snapshot_crease_left_mm": 100,
        "snapshot_crease_middle_mm": 150,
        "snapshot_crease_right_mm": 100,
        "sync_product": True,
    }
    with TestClient(app) as client:
        _login(client, "sales")
        unrelated_edit = client.put(
            f"/api/orders/items/{item_id}",
            json=edit_payload,
        )
        rejected_mismatch = client.put(
            f"/api/orders/items/{item_id}",
            json={**edit_payload, "snapshot_report_width_mm": 356},
        )
        corrected = client.put(
            f"/api/orders/items/{item_id}",
            json={**edit_payload, "snapshot_report_width_mm": 350},
        )

    assert unrelated_edit.status_code == 200, unrelated_edit.text
    assert rejected_mismatch.status_code == 400
    assert "三段合计 350mm" in rejected_mismatch.json()["detail"]
    assert corrected.status_code == 200, corrected.text
    with session_factory() as session:
        item = session.get(OrderItem, item_id)
        product = session.get(Product, item.product_id)
        assert item.snapshot_report_width_mm == 350
        assert product.report_width_mm == 350


def test_order_item_sync_rejects_invalid_common_box_flute_without_partial_save(
    order_api_app,
) -> None:
    from app.models.order import OrderItem
    from app.models.product import Product

    app, session_factory = order_api_app
    base_payload = {
        "quantity": 200,
        "unit_price": "3.60",
        "product_code": "SME-001",
        "product_name": "五层加强纸箱",
        "material": "K=A-BC",
        "specification": "520×350×300mm",
        "layer_count": 3,
        "sync_product": True,
    }
    with TestClient(app) as client:
        _login(client, "sales")
        created = client.post("/api/orders", json=_payload())
        assert created.status_code == 201, created.text
        item_id = created.json()["items"][0]["id"]

        rejected = client.put(
            f"/api/orders/items/{item_id}",
            json={**base_payload, "quantity": 199, "flute_type": "AB"},
        )
        assert rejected.status_code == 400, rejected.text
        assert "三层瓦楞只能是 A / B / E" in rejected.json()["detail"]

        accepted = client.put(
            f"/api/orders/items/{item_id}",
            json={**base_payload, "flute_type": "A"},
        )
        assert accepted.status_code == 200, accepted.text

    with session_factory() as session:
        item = session.get(OrderItem, item_id)
        product = session.get(Product, 1)
        assert item.quantity == 200
        assert (item.layer_count, item.flute_type) == (3, "A")
        assert (product.layer_count, product.flute_type) == (3, "A")


def test_order_models_use_new_tables_and_leave_legacy_name_free() -> None:
    from app.models.order import Order, OrderItem

    assert Order.__tablename__ == "sales_orders"
    assert OrderItem.__tablename__ == "sales_order_items"
    assert date.fromisoformat("2026-06-13").strftime("%Y%m%d") == "20260613"


def test_order_status_closure_requires_remark_and_is_excluded_from_unfinished(
    order_api_app,
) -> None:
    app, _ = order_api_app
    with TestClient(app) as client:
        _login(client)
        created = client.post("/api/orders", json=_payload()).json()
        missing_remark = client.put(
            f"/api/orders/{created['id']}/status",
            json={"status": "dead", "remark": ""},
        )
        closed = client.put(
            f"/api/orders/{created['id']}/status",
            json={"status": "dead", "remark": "客户取消订单"},
        )
        unfinished = client.get("/api/orders", params={"status": "unfinished"})

    assert missing_remark.status_code == 400
    assert closed.status_code == 200
    assert closed.json()["status"] == "dead"
    assert all(item["is_force_closed"] for item in closed.json()["items"])
    assert unfinished.json()["total"] == 0


def test_order_delete_requires_double_confirmation_and_does_not_reuse_number(
    order_api_app,
) -> None:
    app, _ = order_api_app
    with TestClient(app) as client:
        _login(client)
        first = client.post("/api/orders", json=_payload()).json()
        denied = client.delete(f"/api/orders/{first['id']}")
        deleted = client.delete(
            f"/api/orders/{first['id']}",
            params={"confirm": "true"},
        )
        second_payload = _payload()
        second_payload["customer_po"] = "PO-CUSTOMER-AFTER-DELETE"
        second = client.post("/api/orders", json=second_payload).json()

    assert denied.status_code == 400
    assert deleted.status_code == 204
    assert second["order_number"] == "TM20260613002"


def test_pdf_import_order_group_delete_is_atomic_and_returns_chinese_blocker(
    order_api_app,
) -> None:
    from app.models.order import Order
    from app.models.supplier_requisition_order import (
        SupplierRequisitionOrder,
        SupplierRequisitionOrderItem,
    )

    app, session_factory = order_api_app
    first_payload = _payload()
    first_payload["customer_po"] = "PDF-GROUP-001"
    first_payload["remark"] = "PDF识别草稿：first.pdf"
    second_payload = _payload()
    second_payload["customer_po"] = "PDF-GROUP-001"
    second_payload["remark"] = "PDF识别草稿：second.pdf"
    second_payload["items"][0]["quantity"] = 201
    with TestClient(app) as client:
        _login(client)
        first = client.post("/api/orders", json=first_payload).json()
        second = client.post("/api/orders", json=second_payload).json()
        deleted = client.post(
            "/api/orders/group-delete",
            json={"order_ids": [first["id"], second["id"]], "confirm": True},
        )

    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["deleted_count"] == 2
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Order)) == 0

    blocked_payload = _payload()
    blocked_payload["customer_po"] = "PDF-GROUP-BLOCKED"
    blocked_payload["remark"] = "PDF识别草稿：blocked.pdf"
    with TestClient(app) as client:
        _login(client)
        blocked_order = client.post("/api/orders", json=blocked_payload).json()
        blocked_item = blocked_order["items"][0]
        with session_factory() as session:
            supplier_order = SupplierRequisitionOrder(
                order_number="SR-PDF-BLOCK",
                supplier_name="苏州纸板供应商",
                total_quantity=200,
                requisition_qty=200,
                status="confirmed",
            )
            session.add(supplier_order)
            session.flush()
            session.add(
                SupplierRequisitionOrderItem(
                    supplier_order_id=supplier_order.id,
                    order_item_id=blocked_item["id"],
                    order_number=blocked_order["order_number"],
                    product_code=blocked_item["snapshot_product_code"],
                    product_name=blocked_item["snapshot_product_name"],
                    quantity=blocked_item["quantity"],
                    requisition_qty=blocked_item["quantity"],
                )
            )
            session.commit()
        blocked = client.post(
            "/api/orders/group-delete",
            json={"order_ids": [blocked_order["id"]], "confirm": True},
        )

    assert blocked.status_code == 409
    assert "已生成供应商报料单" in blocked.json()["detail"]
    assert "不能直接删除" in blocked.json()["detail"]
    with session_factory() as session:
        assert session.get(Order, blocked_order["id"]) is not None


def test_workflow_rollback_voids_single_source_supplier_order_before_group_delete(
    order_api_app,
) -> None:
    from app.models.order import Order
    from app.models.supplier_requisition_order import (
        SupplierRequisitionOrder,
        SupplierRequisitionOrderItem,
    )

    app, session_factory = order_api_app
    payload = _payload()
    payload["customer_po"] = "ROLLBACK-SUPPLIER-SINGLE"
    payload["items"] = [payload["items"][0]]
    with TestClient(app) as client:
        _login(client, "admin")
        created = client.post("/api/orders", json=payload).json()
        created_item = created["items"][0]
        with session_factory() as session:
            supplier_order = SupplierRequisitionOrder(
                order_number="SR-ROLLBACK-SINGLE",
                supplier_name="Regression Supplier",
                total_quantity=200,
                stock_deduction_qty=20,
                requisition_qty=180,
                required_piece_qty=400,
                status="confirmed",
            )
            session.add(supplier_order)
            session.flush()
            supplier_order_id = supplier_order.id
            session.add(
                SupplierRequisitionOrderItem(
                    supplier_order_id=supplier_order.id,
                    order_item_id=created_item["id"],
                    order_number=created["order_number"],
                    product_code=created_item["snapshot_product_code"],
                    product_name=created_item["snapshot_product_name"],
                    quantity=200,
                    stock_deduction_qty=20,
                    requisition_qty=180,
                    required_piece_qty=400,
                )
            )
            session.commit()

        rolled_back = client.put(
            f"/api/orders/{created['id']}/rollback-workflow",
            json={"reason": "supplier source rollback regression"},
        )

        with session_factory() as session:
            supplier_order = session.get(
                SupplierRequisitionOrder,
                supplier_order_id,
            )
            assert supplier_order is not None
            assert supplier_order.status == "voided"
            assert supplier_order.voided_at is not None

        deleted = client.post(
            "/api/orders/group-delete",
            json={"order_ids": [created["id"]], "confirm": True},
        )

    assert rolled_back.status_code == 200, rolled_back.text
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["deleted_count"] == 1
    with session_factory() as session:
        assert session.get(Order, created["id"]) is None
        supplier_order = session.get(SupplierRequisitionOrder, supplier_order_id)
        assert supplier_order is not None
        assert supplier_order.status == "voided"


def test_workflow_rollback_removes_only_current_order_from_shared_supplier_order(
    order_api_app,
) -> None:
    from app.models.order import Order
    from app.models.supplier_requisition_order import (
        SupplierRequisitionOrder,
        SupplierRequisitionOrderItem,
    )

    app, session_factory = order_api_app
    current_payload = _payload()
    current_payload["customer_po"] = "ROLLBACK-SUPPLIER-CURRENT"
    current_payload["items"] = [
        {"product_id": 1, "quantity": 200, "unit_price": "3.60"}
    ]
    other_payload = _payload()
    other_payload["customer_po"] = "ROLLBACK-SUPPLIER-OTHER"
    other_payload["items"] = [
        {"product_id": 2, "quantity": 120, "unit_price": "2.45"}
    ]

    with TestClient(app) as client:
        _login(client, "admin")
        current_order = client.post("/api/orders", json=current_payload).json()
        other_order = client.post("/api/orders", json=other_payload).json()
        current_item = current_order["items"][0]
        other_item = other_order["items"][0]

        with session_factory() as session:
            supplier_order = SupplierRequisitionOrder(
                order_number="SR-ROLLBACK-SHARED",
                supplier_name="Regression Supplier",
                total_quantity=320,
                stock_deduction_qty=45,
                requisition_qty=275,
                required_piece_qty=640,
                status="confirmed",
            )
            session.add(supplier_order)
            session.flush()
            supplier_order_id = supplier_order.id
            current_source = SupplierRequisitionOrderItem(
                supplier_order_id=supplier_order.id,
                order_item_id=current_item["id"],
                order_number=current_order["order_number"],
                product_code=current_item["snapshot_product_code"],
                product_name=current_item["snapshot_product_name"],
                quantity=200,
                stock_deduction_qty=20,
                requisition_qty=180,
                required_piece_qty=400,
            )
            other_source = SupplierRequisitionOrderItem(
                supplier_order_id=supplier_order.id,
                order_item_id=other_item["id"],
                order_number=other_order["order_number"],
                product_code=other_item["snapshot_product_code"],
                product_name=other_item["snapshot_product_name"],
                quantity=120,
                stock_deduction_qty=25,
                requisition_qty=95,
                required_piece_qty=240,
            )
            session.add_all([current_source, other_source])
            session.flush()
            current_source_id = current_source.id
            other_source_id = other_source.id
            session.commit()

        rolled_back = client.put(
            f"/api/orders/{current_order['id']}/rollback-workflow",
            json={"reason": "shared supplier source rollback regression"},
        )

        with session_factory() as session:
            supplier_order = session.get(
                SupplierRequisitionOrder,
                supplier_order_id,
            )
            remaining_source = session.get(
                SupplierRequisitionOrderItem,
                other_source_id,
            )
            assert session.get(
                SupplierRequisitionOrderItem,
                current_source_id,
            ) is None
            assert remaining_source is not None
            assert remaining_source.order_item_id == other_item["id"]
            assert remaining_source.order_number == other_order["order_number"]
            assert supplier_order is not None
            assert supplier_order.status == "confirmed"
            assert supplier_order.total_quantity == 120
            assert supplier_order.stock_deduction_qty == 25
            assert supplier_order.requisition_qty == 95
            assert supplier_order.required_piece_qty == 240

        current_deleted = client.post(
            "/api/orders/group-delete",
            json={"order_ids": [current_order["id"]], "confirm": True},
        )
        other_protected = client.post(
            "/api/orders/group-delete",
            json={"order_ids": [other_order["id"]], "confirm": True},
        )

    assert rolled_back.status_code == 200, rolled_back.text
    assert current_deleted.status_code == 200, current_deleted.text
    assert current_deleted.json()["deleted_count"] == 1
    assert other_protected.status_code == 409, other_protected.text
    with session_factory() as session:
        assert session.get(Order, current_order["id"]) is None
        assert session.get(Order, other_order["id"]) is not None
        remaining_source = session.get(
            SupplierRequisitionOrderItem,
            other_source_id,
        )
        assert remaining_source is not None
        assert remaining_source.order_item_id == other_item["id"]


def test_workflow_rollback_rejects_active_incoming_before_supplier_unlink(
    order_api_app,
) -> None:
    from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem
    from app.models.order import Order, OrderItem
    from app.models.supplier_requisition_order import (
        SupplierRequisitionOrder,
        SupplierRequisitionOrderItem,
    )

    app, session_factory = order_api_app
    payload = _payload()
    payload["customer_po"] = "ROLLBACK-SUPPLIER-INCOMING-GUARD"
    payload["items"] = [payload["items"][0]]
    with TestClient(app) as client:
        _login(client, "admin")
        created = client.post("/api/orders", json=payload).json()
        created_item = created["items"][0]
        with session_factory() as session:
            order = session.get(Order, created["id"])
            order_item = session.get(OrderItem, created_item["id"])
            assert order is not None
            assert order_item is not None
            supplier_order = SupplierRequisitionOrder(
                order_number="SR-ROLLBACK-INCOMING-GUARD",
                supplier_name="Regression Supplier",
                total_quantity=200,
                stock_deduction_qty=20,
                requisition_qty=180,
                required_piece_qty=400,
                status="confirmed",
            )
            session.add(supplier_order)
            session.flush()
            supplier_source = SupplierRequisitionOrderItem(
                supplier_order_id=supplier_order.id,
                order_item_id=order_item.id,
                order_number=created["order_number"],
                product_code=created_item["snapshot_product_code"],
                product_name=created_item["snapshot_product_name"],
                quantity=200,
                stock_deduction_qty=20,
                requisition_qty=180,
                required_piece_qty=400,
            )
            session.add(supplier_source)
            session.flush()
            receipt = IncomingReceipt(
                receipt_number="IR-ROLLBACK-INCOMING-GUARD",
                status="posted",
                received_at=datetime(2026, 7, 16, 9, 30),
                idempotency_key="rollback-incoming-guard",
            )
            session.add(receipt)
            session.flush()
            receipt_item = IncomingReceiptItem(
                receipt_id=receipt.id,
                order_id=order.id,
                order_item_id=order_item.id,
                supplier_order_id=supplier_order.id,
                supplier_order_item_id=supplier_source.id,
                planned_quantity=180,
                received_quantity=180,
                cumulative_received_quantity=180,
                variance_quantity=0,
                variance_type="matched",
                resolution_status="not_required",
                status="posted",
            )
            session.add(receipt_item)
            order.status = "production"
            order_item.material_status = "received"
            order_item.material_received_at = datetime(2026, 7, 16, 9, 30)
            order_item.requisition_status = "供应商已排单"
            order_item.requisition_qty = 180
            order_item.inventory_deducted_qty = 20
            order_item.supplier_order_number = supplier_order.order_number
            session.flush()
            supplier_order_id = supplier_order.id
            supplier_source_id = supplier_source.id
            receipt_item_id = receipt_item.id
            session.commit()

        rolled_back = client.put(
            f"/api/orders/{created['id']}/rollback-workflow",
            json={"reason": "active incoming must block supplier unlink"},
        )

    assert rolled_back.status_code == 409, rolled_back.text
    assert "有效来料" in rolled_back.json()["detail"]
    with session_factory() as session:
        order = session.get(Order, created["id"])
        order_item = session.get(OrderItem, created_item["id"])
        supplier_order = session.get(SupplierRequisitionOrder, supplier_order_id)
        supplier_source = session.get(
            SupplierRequisitionOrderItem,
            supplier_source_id,
        )
        receipt_item = session.get(IncomingReceiptItem, receipt_item_id)
        assert order is not None
        assert order.status == "production"
        assert order_item is not None
        assert order_item.material_status == "received"
        assert order_item.requisition_status == "供应商已排单"
        assert order_item.requisition_qty == 180
        assert order_item.inventory_deducted_qty == 20
        assert order_item.supplier_order_number == "SR-ROLLBACK-INCOMING-GUARD"
        assert supplier_order is not None
        assert supplier_order.status == "confirmed"
        assert supplier_order.voided_at is None
        assert supplier_source is not None
        assert supplier_source.order_item_id == created_item["id"]
        assert receipt_item is not None
        assert receipt_item.status == "posted"
        assert receipt_item.supplier_order_item_id == supplier_source_id


def test_workflow_rollback_noneditable_supplier_status_is_atomic(
    order_api_app,
) -> None:
    from app.models.order import Order, OrderItem
    from app.models.supplier_requisition_order import (
        SupplierRequisitionOrder,
        SupplierRequisitionOrderItem,
    )

    app, session_factory = order_api_app
    payload = _payload()
    payload["customer_po"] = "ROLLBACK-SUPPLIER-ATOMIC-STATUS"
    with TestClient(app) as client:
        _login(client, "admin")
        created = client.post("/api/orders", json=payload).json()
        with session_factory() as session:
            order = session.get(Order, created["id"])
            assert order is not None
            order.status = "production"
            editable_order = SupplierRequisitionOrder(
                order_number="SR-ROLLBACK-ATOMIC-EDITABLE",
                supplier_name="Regression Supplier",
                total_quantity=200,
                requisition_qty=200,
                status="confirmed",
            )
            locked_order = SupplierRequisitionOrder(
                order_number="SR-ROLLBACK-ATOMIC-LOCKED",
                supplier_name="Regression Supplier",
                total_quantity=100,
                requisition_qty=100,
                status="processing",
            )
            session.add_all([editable_order, locked_order])
            session.flush()
            editable_source = SupplierRequisitionOrderItem(
                supplier_order_id=editable_order.id,
                order_item_id=created["items"][0]["id"],
                order_number=created["order_number"],
                product_code=created["items"][0]["snapshot_product_code"],
                product_name=created["items"][0]["snapshot_product_name"],
                quantity=200,
                requisition_qty=200,
            )
            locked_source = SupplierRequisitionOrderItem(
                supplier_order_id=locked_order.id,
                order_item_id=created["items"][1]["id"],
                order_number=created["order_number"],
                product_code=created["items"][1]["snapshot_product_code"],
                product_name=created["items"][1]["snapshot_product_name"],
                quantity=100,
                requisition_qty=100,
            )
            session.add_all([editable_source, locked_source])
            for item, supplier_number in zip(
                order.items,
                [editable_order.order_number, locked_order.order_number],
                strict=True,
            ):
                item.requisition_status = "供应商已排单"
                item.requisition_qty = item.quantity
                item.supplier_order_number = supplier_number
            session.flush()
            editable_order_id = editable_order.id
            locked_order_id = locked_order.id
            editable_source_id = editable_source.id
            locked_source_id = locked_source.id
            session.commit()

        rolled_back = client.put(
            f"/api/orders/{created['id']}/rollback-workflow",
            json={"reason": "noneditable supplier status must fail atomically"},
        )

    assert rolled_back.status_code == 409, rolled_back.text
    assert "SR-ROLLBACK-ATOMIC-LOCKED" in rolled_back.json()["detail"]
    assert "processing" in rolled_back.json()["detail"]
    with session_factory() as session:
        order = session.get(Order, created["id"])
        editable_order = session.get(SupplierRequisitionOrder, editable_order_id)
        locked_order = session.get(SupplierRequisitionOrder, locked_order_id)
        assert order is not None
        assert order.status == "production"
        assert editable_order is not None
        assert editable_order.status == "confirmed"
        assert editable_order.voided_at is None
        assert locked_order is not None
        assert locked_order.status == "processing"
        assert locked_order.voided_at is None
        assert session.get(SupplierRequisitionOrderItem, editable_source_id) is not None
        assert session.get(SupplierRequisitionOrderItem, locked_source_id) is not None
        order_items = session.scalars(
            select(OrderItem)
            .where(OrderItem.order_id == order.id)
            .order_by(OrderItem.id)
        ).all()
        assert [item.requisition_status for item in order_items] == [
            "供应商已排单",
            "供应商已排单",
        ]
        assert [item.supplier_order_number for item in order_items] == [
            "SR-ROLLBACK-ATOMIC-EDITABLE",
            "SR-ROLLBACK-ATOMIC-LOCKED",
        ]


def test_workflow_rollback_with_supplier_order_is_idempotent(
    order_api_app,
) -> None:
    from app.models.supplier_requisition_order import (
        SupplierRequisitionOrder,
        SupplierRequisitionOrderItem,
    )

    app, session_factory = order_api_app
    payload = _payload()
    payload["customer_po"] = "ROLLBACK-SUPPLIER-IDEMPOTENT"
    payload["items"] = [payload["items"][0]]
    with TestClient(app) as client:
        _login(client, "admin")
        created = client.post("/api/orders", json=payload).json()
        created_item = created["items"][0]
        with session_factory() as session:
            supplier_order = SupplierRequisitionOrder(
                order_number="SR-ROLLBACK-IDEMPOTENT",
                supplier_name="Regression Supplier",
                total_quantity=200,
                stock_deduction_qty=20,
                requisition_qty=180,
                required_piece_qty=400,
                status="confirmed",
            )
            session.add(supplier_order)
            session.flush()
            supplier_order_id = supplier_order.id
            source_item = SupplierRequisitionOrderItem(
                supplier_order_id=supplier_order.id,
                order_item_id=created_item["id"],
                order_number=created["order_number"],
                product_code=created_item["snapshot_product_code"],
                product_name=created_item["snapshot_product_name"],
                quantity=200,
                stock_deduction_qty=20,
                requisition_qty=180,
                required_piece_qty=400,
            )
            session.add(source_item)
            session.flush()
            source_item_id = source_item.id
            session.commit()

        first = client.put(
            f"/api/orders/{created['id']}/rollback-workflow",
            json={"reason": "first supplier rollback"},
        )
        with session_factory() as session:
            supplier_order = session.get(
                SupplierRequisitionOrder,
                supplier_order_id,
            )
            source_item = session.get(
                SupplierRequisitionOrderItem,
                source_item_id,
            )
            assert supplier_order is not None
            assert source_item is not None
            first_state = (
                supplier_order.status,
                supplier_order.voided_at,
                supplier_order.total_quantity,
                supplier_order.stock_deduction_qty,
                supplier_order.requisition_qty,
                supplier_order.required_piece_qty,
                source_item.order_item_id,
                source_item.quantity,
                source_item.stock_deduction_qty,
                source_item.requisition_qty,
                source_item.required_piece_qty,
            )

        second = client.put(
            f"/api/orders/{created['id']}/rollback-workflow",
            json={"reason": "second supplier rollback"},
        )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["status"] == "pending_production"
    assert second.json()["status"] == "pending_production"
    with session_factory() as session:
        supplier_order = session.get(SupplierRequisitionOrder, supplier_order_id)
        source_item = session.get(SupplierRequisitionOrderItem, source_item_id)
        assert supplier_order is not None
        assert source_item is not None
        second_state = (
            supplier_order.status,
            supplier_order.voided_at,
            supplier_order.total_quantity,
            supplier_order.stock_deduction_qty,
            supplier_order.requisition_qty,
            supplier_order.required_piece_qty,
            source_item.order_item_id,
            source_item.quantity,
            source_item.stock_deduction_qty,
            source_item.requisition_qty,
            source_item.required_piece_qty,
        )
    assert first_state == second_state


@pytest.mark.parametrize("supplier_status", ["voided", "cancelled", "withdrawn", "invalid"])
def test_inactive_supplier_order_items_do_not_block_order_group_delete(
    order_api_app,
    supplier_status: str,
) -> None:
    from app.models.supplier_requisition_order import (
        SupplierRequisitionOrder,
        SupplierRequisitionOrderItem,
    )

    app, session_factory = order_api_app
    payload = _payload()
    payload["customer_po"] = f"PDF-GROUP-INACTIVE-SRO-{supplier_status}"
    with TestClient(app) as client:
        _login(client)
        created = client.post("/api/orders", json=payload).json()
        order_item_id = created["items"][0]["id"]
        with session_factory() as session:
            order = SupplierRequisitionOrder(
                order_number=f"SR-INACTIVE-{supplier_status}",
                supplier_name="苏州纸板供应商",
                total_quantity=created["items"][0]["quantity"],
                requisition_qty=created["items"][0]["quantity"],
                status=supplier_status,
            )
            session.add(order)
            session.flush()
            session.add(SupplierRequisitionOrderItem(
                supplier_order_id=order.id,
                order_item_id=order_item_id,
                order_number=created["order_number"],
                product_code=created["items"][0]["snapshot_product_code"],
                product_name=created["items"][0]["snapshot_product_name"],
                quantity=created["items"][0]["quantity"],
                requisition_qty=created["items"][0]["quantity"],
            ))
            session.commit()
        deleted = client.post(
            "/api/orders/group-delete",
            json={"order_ids": [created["id"]], "confirm": True},
        )

    assert deleted.status_code == 200, deleted.text


def test_cancelled_requisition_items_do_not_block_order_group_delete(
    order_api_app,
) -> None:
    from app.models.order import Order, OrderItem
    from app.models.requisition import Requisition, RequisitionItem

    app, session_factory = order_api_app
    payload = _payload()
    payload["customer_po"] = "PDF-GROUP-CANCELLED-REQ"
    with TestClient(app) as client:
        _login(client)
        created = client.post("/api/orders", json=payload).json()
        order_item_id = created["items"][0]["id"]
        with session_factory() as session:
            requisition = Requisition(
                requisition_number="MR-CANCELLED-BEFORE-DELETE",
                requisition_date=date(2026, 6, 13),
                status="已取消",
            )
            session.add(requisition)
            session.flush()
            session.add(
                RequisitionItem(
                    requisition_id=requisition.id,
                    order_item_id=order_item_id,
                    inventory_deducted_qty=0,
                    requisition_qty=200,
                    cardboard_len=Decimal("1000"),
                    cardboard_width=Decimal("500"),
                    special_process="一开一",
                    product_name_snapshot="五层加强纸箱",
                    status="已取消",
                )
            )
            session.commit()
        deleted = client.post(
            "/api/orders/group-delete",
            json={"order_ids": [created["id"]], "confirm": True},
        )

    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["deleted_count"] == 1
    with session_factory() as session:
        assert session.get(Order, created["id"]) is None
        assert session.scalar(
            select(func.count()).select_from(OrderItem).where(OrderItem.order_id == created["id"])
        ) == 0
        assert session.scalar(select(func.count()).select_from(RequisitionItem)) == 0
        assert session.scalar(select(func.count()).select_from(Requisition)) == 0


def _seed_tianhua_predelivery(
    session,
    *,
    order_id: int,
    order_item_id: int,
    order_number: str,
    suffix: str,
    activity_at: datetime | None = None,
) -> int:
    from app.models.tianhua_pre_delivery import (
        TianhuaPreDeliveryDraft,
        TianhuaPreDeliveryDraftItem,
        TianhuaPreDeliveryImportBatch,
        TianhuaPreDeliveryImportItem,
    )

    batch = TianhuaPreDeliveryImportBatch(
        batch_number=f"TH-DELETE-{suffix}",
        filename=f"{suffix}.png",
        customer_id=1,
        customer_name="苏州思迈尔包装有限公司",
        pre_delivery_date=date(2026, 6, 20),
        status="draft_created",
        total_rows=1,
    )
    session.add(batch)
    session.flush()
    imported = TianhuaPreDeliveryImportItem(
        batch_id=batch.id,
        row_no=1,
        raw_text="SME-001 200",
        stock_code="SME-001",
        image_qty=200,
        product_id=1,
        product_name="五层加强纸箱",
        order_item_id=order_item_id,
        order_id=order_id,
        order_number=order_number,
        system_pending_qty=200,
        suggested_qty=200,
        final_delivery_qty=200,
        status="ok",
        selected=True,
    )
    session.add(imported)
    session.flush()
    draft = TianhuaPreDeliveryDraft(
        draft_number=f"THYSH-DELETE-{suffix}",
        batch_id=batch.id,
        customer_id=1,
        status="draft",
        updated_at=activity_at,
    )
    session.add(draft)
    session.flush()
    draft_item = TianhuaPreDeliveryDraftItem(
        draft_id=draft.id,
        import_item_id=imported.id,
        row_no=1,
        stock_code="SME-001",
        product_id=1,
        order_item_id=order_item_id,
        order_id=order_id,
        order_number=order_number,
        delivery_qty=200,
        mobile_pick_status="pending",
    )
    session.add(draft_item)
    session.commit()
    return imported.id


def test_active_predelivery_still_blocks_order_group_delete(order_api_app) -> None:
    from app.models.order import Order

    app, session_factory = order_api_app
    payload = _payload()
    payload["customer_po"] = "PREDELIVERY-ACTIVE"
    with TestClient(app) as client:
        _login(client)
        order = client.post("/api/orders", json=payload).json()
        with session_factory() as session:
            _seed_tianhua_predelivery(
                session,
                order_id=order["id"],
                order_item_id=order["items"][0]["id"],
                order_number=order["order_number"],
                suffix="ACTIVE",
            )
        response = client.post(
            "/api/orders/group-delete",
            json={"order_ids": [order["id"]], "confirm": True},
        )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "该订单仍存在有效预送货流程，请先撤回或作废预送货后再删除。"
    )
    with session_factory() as session:
        assert session.get(Order, order["id"]) is not None


@pytest.mark.parametrize(
    "action",
    ["ROLLBACK_WORKFLOW", "CANCEL_PRE_DELIVERY", "VOID_PRE_DELIVERY"],
)
def test_invalidated_predelivery_does_not_block_order_group_delete(
    order_api_app,
    action: str,
) -> None:
    from app.models.audit import OperationLog
    from app.models.order import Order
    from app.models.tianhua_pre_delivery import (
        TianhuaPreDeliveryDraftItem,
        TianhuaPreDeliveryImportItem,
    )

    app, session_factory = order_api_app
    payload = _payload()
    payload["customer_po"] = f"PREDELIVERY-{action}"
    with TestClient(app) as client:
        _login(client)
        order = client.post("/api/orders", json=payload).json()
        with session_factory() as session:
            import_item_id = _seed_tianhua_predelivery(
                session,
                order_id=order["id"],
                order_item_id=order["items"][0]["id"],
                order_number=order["order_number"],
                suffix=action,
                activity_at=datetime(2026, 6, 19, 10, 0, 0),
            )
            session.add(
                OperationLog(
                    action=action,
                    resource="Order",
                    entity_type="order",
                    entity_id=order["id"],
                    created_at=datetime(2026, 6, 20, 10, 0, 0),
                    description="预送货已撤回或作废",
                )
            )
            session.commit()
        response = client.post(
            "/api/orders/group-delete",
            json={"order_ids": [order["id"]], "confirm": True},
        )

    assert response.status_code == 200, response.text
    with session_factory() as session:
        assert session.get(Order, order["id"]) is None
        imported = session.get(TianhuaPreDeliveryImportItem, import_item_id)
        assert imported is not None
        assert imported.order_id is None
        assert imported.order_item_id is None
        assert imported.selected is False
        assert imported.status == "not_matched"
        assert session.scalar(
            select(func.count()).select_from(TianhuaPreDeliveryDraftItem)
        ) == 0


def test_workflow_rollback_unlinks_predelivery_before_later_delete(
    order_api_app,
) -> None:
    from app.models.tianhua_pre_delivery import TianhuaPreDeliveryImportItem

    app, session_factory = order_api_app
    payload = _payload()
    payload["customer_po"] = "PREDELIVERY-ROLLBACK-ENDPOINT"
    with TestClient(app) as client:
        _login(client, "admin")
        order = client.post("/api/orders", json=payload).json()
        with session_factory() as session:
            import_item_id = _seed_tianhua_predelivery(
                session,
                order_id=order["id"],
                order_item_id=order["items"][0]["id"],
                order_number=order["order_number"],
                suffix="ROLLBACK-ENDPOINT",
            )
        rolled_back = client.put(
            f"/api/orders/{order['id']}/rollback-workflow",
            json={"reason": "预送货取消"},
        )
        deleted = client.post(
            "/api/orders/group-delete",
            json={"order_ids": [order["id"]], "confirm": True},
        )

    assert rolled_back.status_code == 200, rolled_back.text
    assert deleted.status_code == 200, deleted.text
    with session_factory() as session:
        imported = session.get(TianhuaPreDeliveryImportItem, import_item_id)
        assert imported.order_id is None
        assert imported.order_item_id is None


def test_duplicate_formal_order_is_not_generated_twice(order_api_app) -> None:
    app, _ = order_api_app
    with TestClient(app) as client:
        _login(client)
        first = client.post("/api/orders", json=_payload())
        duplicate = client.post("/api/orders", json=_payload())

    assert first.status_code == 201
    assert duplicate.status_code == 409
    assert "未重复生成" in duplicate.json()["detail"]


def test_business_hides_fully_delivered_orders_and_finished_view_lists_them(
    order_api_app,
) -> None:
    from app.models.delivery import Delivery, DeliveryItem
    from app.models.order import Order, OrderItem

    app, session_factory = order_api_app
    with TestClient(app) as client:
        _login(client, "admin")
        created = client.post("/api/orders", json=_payload()).json()
    with session_factory() as session:
        order = session.get(Order, created["id"])
        assert order is not None
        items = list(
            session.scalars(
                select(OrderItem)
                .where(OrderItem.order_id == order.id)
                .order_by(OrderItem.item_sequence)
            ).all()
        )
        order.status = "pending_delivery"
        for item in items:
            item.delivered_quantity = item.quantity
        delivery = Delivery(
            delivery_number="DN-N026-FINISHED",
            customer_id=order.customer_id,
            delivery_date=date(2026, 6, 25),
            status="dispatched",
            total_quantity=sum(item.quantity for item in items),
        )
        session.add(delivery)
        session.flush()
        session.add_all(
            [
                DeliveryItem(
                    delivery_id=delivery.id,
                    order_item_id=item.id,
                    delivered_quantity=item.quantity,
                )
                for item in items
            ]
        )
        session.commit()

    with TestClient(app) as client:
        _login(client, "admin")
        business = client.get("/api/orders", params={"status": "business"})
        business_keyword = client.get(
            "/api/orders",
            params={"status": "business", "keyword": "PO-CUSTOMER-001"},
        )
        unfiltered_keyword = client.get(
            "/api/orders", params={"keyword": "PO-CUSTOMER-001"}
        )
        finished = client.get(
            "/api/orders",
            params={"status": "finished_delivery", "keyword": "PO-CUSTOMER-001"},
        )

    assert business.status_code == 200
    assert business.json()["total"] == 0
    assert business.json()["unfinished_total"] == 0
    assert business_keyword.status_code == 200
    assert business_keyword.json()["total"] == 1
    assert business_keyword.json()["items"][0]["id"] == created["id"]
    assert unfiltered_keyword.status_code == 200
    assert unfiltered_keyword.json()["total"] == 1
    assert finished.status_code == 200
    assert finished.json()["total"] == 1
    assert finished.json()["items"][0]["id"] == created["id"]
    for item in finished.json()["items"][0]["items"]:
        assert item["ordered_quantity"] == item["delivered_quantity"]
        assert item["remaining_quantity"] == 0
        assert item["completion_date"] == "2026-06-25"

    with session_factory() as session:
        order = session.get(Order, created["id"])
        assert order is not None
        order.status = "dead"
        session.commit()
    with TestClient(app) as client:
        _login(client, "admin")
        finished_after_dead = client.get(
            "/api/orders", params={"status": "finished_delivery"}
        )
        business_dead_keyword = client.get(
            "/api/orders",
            params={"status": "business", "keyword": "PO-CUSTOMER-001"},
        )
    assert finished_after_dead.status_code == 200
    assert finished_after_dead.json()["total"] == 0
    assert business_dead_keyword.status_code == 200
    assert business_dead_keyword.json()["total"] == 0


def test_completed_status_with_undelivered_items_stays_discoverable(
    order_api_app,
) -> None:
    app, _ = order_api_app
    with TestClient(app) as client:
        _login(client, "admin")
        created = client.post("/api/orders", json=_payload()).json()
        marked = client.put(
            f"/api/orders/{created['id']}/status",
            json={"status": "completed", "remark": "legacy status mismatch"},
        )
        assert marked.status_code == 200, marked.text
        business = client.get("/api/orders", params={"status": "business"})
        finished = client.get(
            "/api/orders", params={"status": "finished_delivery"}
        )

    assert business.status_code == 200
    assert business.json()["total"] == 1
    assert business.json()["items"][0]["id"] == created["id"]
    assert finished.status_code == 200
    assert finished.json()["total"] == 0


def test_admin_can_rollback_order_to_unreported_state(order_api_app) -> None:
    app, session_factory = order_api_app
    with TestClient(app) as client:
        _login(client, "admin")
        created = client.post("/api/orders", json=_payload()).json()
        response = client.put(
            f"/api/orders/{created['id']}/rollback-workflow",
            json={"reason": "流程录入错误"},
        )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "pending_production"
    assert all(item["requisition_status"] == "未报料" for item in response.json()["items"])


def test_new_product_row_auto_creates_minimal_product_and_saves_item(
    order_api_app,
) -> None:
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.services.product_import import AUTO_CREATE_REMARK

    app, session_factory = order_api_app
    payload = _payload()
    payload["items"] = [
        {
            "product_id": None,
            "is_new_product": True,
            "product_code": "NEW-001",
            "product_name": "全新外箱",
            "specification": "300×200×150mm",
            "quantity": 50,
            "unit_price": "4.20",
        }
    ]
    with TestClient(app) as client:
        _login(client)
        response = client.post("/api/orders", json=payload)

    assert response.status_code == 201, response.text
    item = response.json()["items"][0]
    assert item["snapshot_product_code"] == "NEW-001"
    assert item["snapshot_product_name"] == "全新外箱"

    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Order)) == 1
        assert session.scalar(select(func.count()).select_from(OrderItem)) == 1
        created = session.scalar(
            select(Product).where(Product.product_code == "NEW-001")
        )
        assert created is not None
        assert created.customer_id == 1
        assert created.customer_material_code == "NEW-001"
        assert created.remark == AUTO_CREATE_REMARK
        assert created.length_mm == Decimal("300")
        assert created.width_mm == Decimal("200")
        assert created.height_mm == Decimal("150")
        assert item["product_id"] == created.id


def test_mixed_batch_with_matched_and_new_product_rows_all_save(
    order_api_app,
) -> None:
    from app.models.order import Order, OrderItem
    from app.models.product import Product

    app, session_factory = order_api_app
    payload = _payload()
    payload["items"] = [
        {"product_id": 1, "quantity": 200, "unit_price": "3.60"},
        {
            "product_id": None,
            "is_new_product": True,
            "product_code": "NEW-MIX",
            "product_name": "混合批新箱",
            "specification": "260×180×120mm",
            "quantity": 80,
            "unit_price": "2.10",
        },
    ]
    with TestClient(app) as client:
        _login(client)
        response = client.post("/api/orders", json=payload)

    assert response.status_code == 201, response.text
    assert len(response.json()["items"]) == 2
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Order)) == 1
        assert session.scalar(select(func.count()).select_from(OrderItem)) == 2
        # 2 seeded + 1 auto-created
        assert session.scalar(select(func.count()).select_from(Product)) == 3


def test_new_product_deduplicated_within_single_order(order_api_app) -> None:
    from app.models.product import Product

    app, session_factory = order_api_app
    payload = _payload()
    payload["items"] = [
        {
            "product_id": None,
            "is_new_product": True,
            "product_code": "DUP-CODE",
            "product_name": "重复新箱",
            "specification": "300×200×150mm",
            "quantity": 30,
            "unit_price": "1.50",
        },
        {
            "product_id": None,
            "is_new_product": True,
            "product_code": "DUP-CODE",
            "product_name": "重复新箱",
            "specification": "300×200×150mm",
            "quantity": 40,
            "unit_price": "1.50",
        },
    ]
    with TestClient(app) as client:
        _login(client)
        response = client.post("/api/orders", json=payload)

    assert response.status_code == 201, response.text
    items = response.json()["items"]
    assert items[0]["product_id"] == items[1]["product_id"]
    with session_factory() as session:
        # only one new product created despite two rows
        assert session.scalar(select(func.count()).select_from(Product)) == 3


def test_new_product_reuses_existing_product_by_inventory_code(
    order_api_app,
) -> None:
    from app.models.product import Product

    app, session_factory = order_api_app
    payload = _payload()
    payload["items"] = [
        {
            "product_id": None,
            "is_new_product": True,
            # KH-001 is the seeded product 1's customer_material_code
            "product_code": "KH-001",
            "product_name": "随便填的名字",
            "specification": "999×999×999mm",
            "quantity": 25,
            "unit_price": "5.00",
        }
    ]
    with TestClient(app) as client:
        _login(client)
        response = client.post("/api/orders", json=payload)

    assert response.status_code == 201, response.text
    assert response.json()["items"][0]["product_id"] == 1
    with session_factory() as session:
        # no new product created — reused existing
        assert session.scalar(select(func.count()).select_from(Product)) == 2


def test_new_product_missing_required_field_reports_row_and_rolls_back(
    order_api_app,
) -> None:
    from app.models.order import Order
    from app.models.product import Product

    app, session_factory = order_api_app
    payload = _payload()
    payload["items"] = [
        {"product_id": 1, "quantity": 10, "unit_price": "3.60"},
        {
            "product_id": None,
            "is_new_product": True,
            "product_code": "",
            "product_name": "缺编码的新箱",
            "specification": "260×180×120mm",
            "quantity": 12,
            "unit_price": "2.00",
        },
    ]
    with TestClient(app) as client:
        _login(client)
        response = client.post("/api/orders", json=payload)

    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert "第2条" in detail
    assert "存货编码" in detail
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Order)) == 0
        # no orphan product flushed for the valid earlier row's new sibling
        assert session.scalar(select(func.count()).select_from(Product)) == 2


def test_new_product_row_with_empty_string_product_id_is_coerced_and_saves(
    order_api_app,
) -> None:
    # Reproduces the real-PDF manual failure: a stale frontend payload sends
    # product_id="" (empty string) instead of null for a new-product row.
    # Pydantic int|None rejects "", which used to surface as "必须填写数字".
    from app.models.product import Product

    app, session_factory = order_api_app
    payload = _payload()
    payload["items"] = [
        {
            "product_id": "",
            "is_new_product": True,
            "product_code": "COERCE-EMPTY",
            "product_name": "空串编码新箱",
            "specification": "300×200×150mm",
            "quantity": 20,
            "unit_price": "3.00",
        }
    ]
    with TestClient(app) as client:
        _login(client)
        response = client.post("/api/orders", json=payload)

    assert response.status_code == 201, response.text
    with session_factory() as session:
        created = session.scalar(
            select(Product).where(Product.product_code == "COERCE-EMPTY")
        )
        assert created is not None
        assert response.json()["items"][0]["product_id"] == created.id


def test_matched_row_with_sentinel_string_product_id_is_coerced(
    order_api_app,
) -> None:
    # Sentinel tokens like "new_product"/"null"/"undefined" must coerce to None
    # rather than raising a raw Pydantic validation error.
    from app.models.product import Product

    app, session_factory = order_api_app
    payload = _payload()
    payload["items"] = [
        {
            "product_id": "new_product",
            "is_new_product": True,
            "product_code": "SENTINEL-NEW",
            "product_name": "哨兵令牌新箱",
            "specification": "260×180×120mm",
            "quantity": 15,
            "unit_price": "2.50",
        }
    ]
    with TestClient(app) as client:
        _login(client)
        response = client.post("/api/orders", json=payload)

    assert response.status_code == 201, response.text
    with session_factory() as session:
        assert (
            session.scalar(
                select(Product).where(Product.product_code == "SENTINEL-NEW")
            )
            is not None
        )


def test_unmarked_row_without_product_id_reports_friendly_row_error(
    order_api_app,
) -> None:
    # A row that is neither matched (no product_id) nor flagged as new must
    # produce a human-readable per-row message, not a raw schema error.
    app, session_factory = order_api_app
    payload = _payload()
    payload["items"] = [
        {
            "product_id": "",
            "is_new_product": False,
            "product_code": "",
            "product_name": "",
            "specification": "",
            "quantity": 10,
            "unit_price": "1.00",
        }
    ]
    with TestClient(app) as client:
        _login(client)
        response = client.post("/api/orders", json=payload)

    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert "第1条" in detail


def test_numeric_string_product_id_matches_existing_product(order_api_app) -> None:
    # product_id arriving as the numeric string "1" must still match product 1.
    app, session_factory = order_api_app
    payload = _payload()
    payload["items"] = [
        {"product_id": "1", "quantity": 30, "unit_price": "3.60"}
    ]
    with TestClient(app) as client:
        _login(client)
        response = client.post("/api/orders", json=payload)

    assert response.status_code == 201, response.text
    assert response.json()["items"][0]["product_id"] == 1


def test_invalid_later_row_rolls_back_auto_created_products(order_api_app) -> None:
    from app.models.order import Order
    from app.models.product import Product

    app, session_factory = order_api_app
    payload = _payload()
    payload["items"] = [
        {
            "product_id": None,
            "is_new_product": True,
            "product_code": "ROLLBACK-NEW",
            "product_name": "应被回滚的新箱",
            "specification": "260×180×120mm",
            "quantity": 12,
            "unit_price": "2.00",
        },
        {"product_id": 9999, "quantity": 5, "unit_price": "1.00"},
    ]
    with TestClient(app) as client:
        _login(client)
        response = client.post("/api/orders", json=payload)

    assert response.status_code == 400, response.text
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Order)) == 0
        # the auto-created product from row 1 must be rolled back
        assert (
            session.scalar(
                select(Product).where(Product.product_code == "ROLLBACK-NEW")
            )
            is None
        )
        assert session.scalar(select(func.count()).select_from(Product)) == 2


def _create_order(
    client: TestClient,
    *,
    customer_po: str,
    order_date: str,
    delivery_date: str | None = None,
) -> dict:
    payload = _payload()
    payload["customer_po"] = customer_po
    payload["order_date"] = order_date
    if delivery_date is not None:
        payload["delivery_date"] = delivery_date
    response = client.post("/api/orders", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def test_order_list_supports_whitelisted_header_sorting(order_api_app) -> None:
    from app.models.customer import Customer
    from app.models.product import Product

    app, session_factory = order_api_app
    with session_factory() as session:
        alpha_customer = Customer(
            customer_number=2,
            customer_code="ALPHA",
            name="A Customer",
        )
        session.add(alpha_customer)
        session.flush()
        alpha_product = Product(
            customer_id=alpha_customer.id,
            product_code="ALPHA-001",
            customer_material_code="ALPHA-MAT-001",
            product_name="Alpha Box",
            legacy_material_text="A=B",
            length_mm=Decimal("300"),
            width_mm=Decimal("200"),
            height_mm=Decimal("100"),
            box_category="normal",
        )
        session.add(alpha_product)
        session.flush()
        alpha_customer_id = alpha_customer.id
        alpha_product_id = alpha_product.id
        session.commit()

    with TestClient(app) as client:
        _login(client, "admin")
        first = _create_order(
            client,
            customer_po="PO-SORT-FIRST",
            order_date="2026-06-13",
            delivery_date="2026-06-22",
        )
        second = _create_order(
            client,
            customer_po="PO-SORT-SECOND",
            order_date="2026-06-14",
            delivery_date="2026-06-21",
        )
        alpha_payload = _payload()
        alpha_payload.update(
            {
                "customer_id": alpha_customer_id,
                "customer_po": "PO-SORT-ALPHA",
                "order_date": "2026-06-12",
                "delivery_date": "2026-06-18",
                "items": [
                    {
                        "product_id": alpha_product_id,
                        "quantity": 10,
                        "unit_price": "1.20",
                    }
                ],
            }
        )
        alpha = client.post("/api/orders", json=alpha_payload)
        assert alpha.status_code == 201, alpha.text
        alpha = alpha.json()

        order_date_asc = client.get(
            "/api/orders",
            params={"sort_by": "order_date", "sort_direction": "asc"},
        )
        order_date_desc = client.get(
            "/api/orders",
            params={"sort_by": "order_date", "sort_direction": "desc"},
        )
        delivery_date_asc = client.get(
            "/api/orders",
            params={"sort_by": "delivery_date", "sort_direction": "asc"},
        )
        customer_name_asc = client.get(
            "/api/orders",
            params={"sort_by": "customer_name", "sort_direction": "asc"},
        )
        customer_name_desc = client.get(
            "/api/orders",
            params={"sort_by": "customer_name", "sort_direction": "desc"},
        )
        invalid_sort = client.get("/api/orders", params={"sort_by": "total_amount"})

    assert [row["id"] for row in order_date_asc.json()["items"]] == [
        alpha["id"],
        first["id"],
        second["id"],
    ]
    assert [row["id"] for row in order_date_desc.json()["items"]] == [
        second["id"],
        first["id"],
        alpha["id"],
    ]
    assert [row["id"] for row in delivery_date_asc.json()["items"]] == [
        alpha["id"],
        second["id"],
        first["id"],
    ]
    assert customer_name_asc.json()["items"][0]["id"] == alpha["id"]
    assert customer_name_desc.json()["items"][-1]["id"] == alpha["id"]
    assert invalid_sort.status_code == 422


def test_back_dated_pdf_order_surfaces_at_top_of_business(order_api_app) -> None:
    # Real bug: a PDF-imported order is back-dated to the document date, so the
    # order_date-DESC sort buried it below newer-dated rows and the user thought
    # it "disappeared" from 日常订单. Business must sort by creation time so the
    # just-saved order — even when back-dated — shows at the top.
    app, _ = order_api_app
    with TestClient(app) as client:
        _login(client)
        first = _create_order(client, customer_po="PO-RECENT", order_date="2026-06-13")
        # saved AFTER the first one, but back-dated to an earlier document date
        back_dated = _create_order(
            client, customer_po="PO-PDF-OLD", order_date="2026-05-01"
        )
        business = client.get("/api/orders", params={"status": "business"})

    assert business.status_code == 200, business.text
    data = business.json()
    ids = [row["id"] for row in data["items"]]
    assert back_dated["id"] in ids, "back-dated PDF order must appear in business"
    assert first["id"] in ids
    # the most recently saved order ranks first despite its older order_date
    assert data["items"][0]["id"] == back_dated["id"]


def test_business_orders_sort_by_latest_update(order_api_app) -> None:
    from app.models.order import Order

    app, session_factory = order_api_app
    with TestClient(app) as client:
        _login(client)
        first = _create_order(client, customer_po="PO-UPDATE-FIRST", order_date="2026-06-13")
        second = _create_order(client, customer_po="PO-UPDATE-SECOND", order_date="2026-06-13")
        with session_factory() as session:
            first_order = session.get(Order, first["id"])
            first_order.updated_at = datetime.now() + timedelta(minutes=5)
            session.commit()
        business = client.get("/api/orders", params={"status": "business"})

    assert business.status_code == 200
    assert business.json()["items"][0]["id"] == first["id"]
    assert business.json()["items"][1]["id"] == second["id"]


def test_dead_order_is_excluded_from_business(order_api_app) -> None:
    app, _ = order_api_app
    with TestClient(app) as client:
        _login(client, "admin")
        created = _create_order(
            client, customer_po="PO-DEAD", order_date="2026-06-13"
        )
        marked = client.put(
            f"/api/orders/{created['id']}/status",
            json={"status": "dead", "remark": "客户取消"},
        )
        assert marked.status_code == 200, marked.text
        business = client.get("/api/orders", params={"status": "business"})

    assert business.status_code == 200
    ids = [row["id"] for row in business.json()["items"]]
    assert created["id"] not in ids


def test_closed_order_is_excluded_from_business(order_api_app) -> None:
    app, _ = order_api_app
    with TestClient(app) as client:
        _login(client, "admin")
        created = _create_order(
            client, customer_po="PO-CLOSED", order_date="2026-06-13"
        )
        marked = client.put(
            f"/api/orders/{created['id']}/status",
            json={"status": "closed", "remark": "已结档归档"},
        )
        assert marked.status_code == 200, marked.text
        business = client.get("/api/orders", params={"status": "business"})

    assert business.status_code == 200
    ids = [row["id"] for row in business.json()["items"]]
    assert created["id"] not in ids


def test_frontend_delivery_group_status_uses_aggregate_quantities() -> None:
    index = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text(
        encoding="utf-8"
    )

    assert 'delivered:"已送完"' in index
    assert "deliveryStatusFromQuantities" in index
    assert "group.hasDeliveryStatus" in index
    assert "group.total_delivered_quantity" in index
    assert "group.group_status = this.deliveryStatusFromQuantities" in index


def _expected_delivery_status(quantity: int, delivered_quantity: int) -> str:
    if quantity > 0 and delivered_quantity >= quantity:
        return "delivered"
    if delivered_quantity > 0:
        return "partially_delivered"
    return "pending_delivery"


def test_frontend_detail_item_status_uses_item_quantities() -> None:
    index = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text(
        encoding="utf-8"
    )

    assert 'v-for="item in row.items"' in index
    assert '<status-tag v-else :value="itemDeliveryStatusKey(item)"></status-tag>' in index
    assert "itemDeliveryStatusKey(item)" in index
    assert "item?.quantity" in index
    assert "item?.delivered_quantity" in index

    assert _expected_delivery_status(150, 140) == "partially_delivered"
    assert _expected_delivery_status(150, 0) == "pending_delivery"
    assert _expected_delivery_status(150, 150) == "delivered"
    assert _expected_delivery_status(150, 160) == "delivered"
    assert _expected_delivery_status(300, 140) == "partially_delivered"


def test_order_detail_exposes_item_level_delivery_quantities(order_api_app) -> None:
    from app.models.order import Order, OrderItem

    app, session_factory = order_api_app
    payload = _payload()
    payload["customer_po"] = "PO-ITEM-DELIVERY-STATUS"
    payload["items"][0]["quantity"] = 150
    payload["items"][1]["quantity"] = 150
    with TestClient(app) as client:
        _login(client, "admin")
        created = client.post("/api/orders", json=payload)
        assert created.status_code == 201, created.text
        order_id = created.json()["id"]

    with session_factory() as session:
        order = session.get(Order, order_id)
        assert order is not None
        order.status = "partially_delivered"
        items = (
            session.query(OrderItem)
            .filter(OrderItem.order_id == order_id)
            .order_by(OrderItem.item_sequence)
            .all()
        )
        items[0].delivered_quantity = 140
        items[1].delivered_quantity = 0
        session.commit()

    with TestClient(app) as client:
        _login(client, "admin")
        detail = client.get(f"/api/orders/{order_id}")

    assert detail.status_code == 200, detail.text
    rows = sorted(detail.json()["items"], key=lambda item: item["item_sequence"])
    assert rows[0]["quantity"] == 150
    assert rows[0]["ordered_quantity"] == 150
    assert rows[0]["delivered_quantity"] == 140
    assert rows[0]["remaining_quantity"] == 10
    assert rows[0]["completion_date"] is None
    assert _expected_delivery_status(
        rows[0]["quantity"], rows[0]["delivered_quantity"]
    ) == "partially_delivered"
    assert rows[1]["quantity"] == 150
    assert rows[1]["ordered_quantity"] == 150
    assert rows[1]["delivered_quantity"] == 0
    assert rows[1]["remaining_quantity"] == 150
    assert rows[1]["completion_date"] is None
    assert _expected_delivery_status(
        rows[1]["quantity"], rows[1]["delivered_quantity"]
    ) == "pending_delivery"


def test_order_item_completion_date_uses_latest_dispatched_delivery(
    order_api_app,
) -> None:
    from app.models.delivery import Delivery, DeliveryItem
    from app.models.order import Order, OrderItem

    app, session_factory = order_api_app
    payload = _payload()
    payload["customer_po"] = "PO-N026-COMPLETION-DATE"
    payload["items"] = [
        {"product_id": 1, "quantity": 150, "unit_price": "3.60"}
    ]
    with TestClient(app) as client:
        _login(client, "admin")
        created = client.post("/api/orders", json=payload)
        assert created.status_code == 201, created.text
        order_id = created.json()["id"]

    with session_factory() as session:
        order = session.get(Order, order_id)
        item = session.scalar(select(OrderItem).where(OrderItem.order_id == order_id))
        assert order is not None
        assert item is not None
        order.status = "pending_delivery"
        item.delivered_quantity = 160

        deliveries = [
            Delivery(
                delivery_number="DN-N026-OLD",
                customer_id=order.customer_id,
                delivery_date=date(2026, 6, 24),
                status="dispatched",
                total_quantity=100,
            ),
            Delivery(
                delivery_number="DN-N026-LATEST",
                customer_id=order.customer_id,
                delivery_date=date(2026, 6, 25),
                status="dispatched",
                total_quantity=60,
            ),
            Delivery(
                delivery_number="DN-N026-PENDING",
                customer_id=order.customer_id,
                delivery_date=date(2026, 6, 30),
                status="pending",
                total_quantity=10,
            ),
        ]
        session.add_all(deliveries)
        session.flush()
        session.add_all(
            [
                DeliveryItem(
                    delivery_id=deliveries[0].id,
                    order_item_id=item.id,
                    delivered_quantity=100,
                ),
                DeliveryItem(
                    delivery_id=deliveries[1].id,
                    order_item_id=item.id,
                    delivered_quantity=60,
                ),
                DeliveryItem(
                    delivery_id=deliveries[2].id,
                    order_item_id=item.id,
                    delivered_quantity=10,
                ),
            ]
        )
        session.commit()

    with TestClient(app) as client:
        _login(client, "admin")
        detail = client.get(f"/api/orders/{order_id}")

    assert detail.status_code == 200, detail.text
    item = detail.json()["items"][0]
    assert item["ordered_quantity"] == 150
    assert item["delivered_quantity"] == 160
    assert item["remaining_quantity"] == 0
    assert item["completion_date"] == "2026-06-25"


def test_history_orders_not_mixed_into_business_by_default(order_api_app) -> None:
    from datetime import datetime

    from app.models.order import Order

    app, session_factory = order_api_app
    with session_factory() as session:
        legacy = Order(
            order_number="RUIDA-90001",
            customer_id=1,
            customer_po="LEGACY-PO",
            order_date=date.fromisoformat("2020-01-02"),
            delivery_date=date.fromisoformat("2020-01-10"),
            status="pending_production",
            payment_status="paid",
            total_amount=Decimal("100.00"),
            created_at=datetime(2026, 6, 20, 5, 0, 0),
        )
        session.add(legacy)
        session.commit()
        legacy_id = legacy.id

    with TestClient(app) as client:
        _login(client)
        active = _create_order(
            client, customer_po="PO-ACTIVE", order_date="2026-06-13"
        )
        business = client.get("/api/orders", params={"status": "business"})
        business_keyword = client.get(
            "/api/orders",
            params={"status": "business", "keyword": "LEGACY-PO"},
        )
        history = client.get("/api/orders", params={"status": "history"})

    # The response order_number is display-masked, so assert on stable ids.
    business_ids = [row["id"] for row in business.json()["items"]]
    business_keyword_ids = [row["id"] for row in business_keyword.json()["items"]]
    history_ids = [row["id"] for row in history.json()["items"]]
    # active order is in business, legacy RUIDA order is NOT
    assert active["id"] in business_ids
    assert legacy_id not in business_ids
    assert legacy_id not in business_keyword_ids
    # the legacy RUIDA order only shows under the explicit history view
    assert legacy_id in history_ids


def test_n028_sales_order_scope_blocks_other_customer_and_filters_list(
    order_api_app,
) -> None:
    from app.models.access_control import UserCustomerScope
    from app.models.customer import Customer
    from app.models.product import Product
    from app.models.user import User

    app, session_factory = order_api_app
    with session_factory() as session:
        first_customer = session.query(Customer).order_by(Customer.id).first()
        second_customer = Customer(
            customer_number=2,
            customer_code="OTHER",
            name="Other Customer",
        )
        session.add(second_customer)
        session.flush()
        second_product = Product(
            customer_id=second_customer.id,
            product_code="OTHER-001",
            customer_material_code="OTHER-001",
            product_name="Other carton",
            box_category="normal",
        )
        session.add(second_product)
        session.flush()
        sales = session.query(User).filter(User.username == "sales").one()
        sales.customer_access_mode = "selected"
        session.add(
            UserCustomerScope(user_id=sales.id, customer_id=first_customer.id)
        )
        session.commit()
        second_customer_id = second_customer.id
        second_product_id = second_product.id

    other_payload = {
        "customer_id": second_customer_id,
        "customer_po": "OTHER-PO",
        "order_date": "2026-06-13",
        "items": [
            {"product_id": second_product_id, "quantity": 10, "unit_price": "2.00"}
        ],
    }
    with TestClient(app) as client:
        _login(client, "admin")
        first = client.post("/api/orders", json=_payload())
        second = client.post("/api/orders", json=other_payload)
        assert first.status_code == 201, first.text
        assert second.status_code == 201, second.text
        client.post("/api/auth/logout")

        _login(client, "sales")
        listing = client.get("/api/orders")
        scoped_search = client.get("/api/orders", params={"keyword": "OTHER-PO"})
        forbidden_detail = client.get(f"/api/orders/{second.json()['id']}")
        forbidden_create = client.post("/api/orders", json=other_payload)

    assert listing.status_code == 200
    assert [row["id"] for row in listing.json()["items"]] == [first.json()["id"]]
    assert scoped_search.status_code == 200
    assert scoped_search.json()["total"] == 0
    assert forbidden_detail.status_code == 403
    assert forbidden_create.status_code == 403


def test_n028_sales_order_response_omits_internal_cost_fields(order_api_app) -> None:
    app, _session_factory = order_api_app
    with TestClient(app) as client:
        _login(client, "admin")
        created = client.post("/api/orders", json=_payload())
        assert created.status_code == 201, created.text
        admin_item = created.json()["items"][0]
        assert "unit_estimated_cost" in admin_item
        client.post("/api/auth/logout")

        _login(client, "sales")
        detail = client.get(f"/api/orders/{created.json()['id']}")

    assert detail.status_code == 200
    sales_item = detail.json()["items"][0]
    assert {
        "estimated_cost",
        "cost_status",
        "unit_estimated_cost",
        "unit_estimated_gross_profit",
        "total_estimated_cost",
        "total_estimated_gross_profit",
    }.isdisjoint(sales_item)
