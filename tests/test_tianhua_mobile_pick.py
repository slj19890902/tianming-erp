from datetime import date
from decimal import Decimal

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker


def test_mobile_pick_token_expiry_and_invalid_token(monkeypatch):
    from app.core.security import (
        create_tianhua_pick_token,
        decode_tianhua_pick_token,
    )

    monkeypatch.setenv("ERP_SECRET_KEY", "mobile-pick-test-secret")
    valid, _expires = create_tianhua_pick_token(12, 34)
    assert decode_tianhua_pick_token(valid) == (12, 34)

    expired, _expires = create_tianhua_pick_token(12, 34, expires_hours=-1)
    with pytest.raises(ValueError, match="二维码已过期"):
        decode_tianhua_pick_token(expired)
    with pytest.raises(ValueError, match="无权限访问"):
        decode_tianhua_pick_token("not-a-valid-token")


def test_mobile_pick_api_isolated_from_formal_delivery(tmp_path, monkeypatch):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.tianhua_pre_delivery import mobile_router, router
    from app.core.database import create_sqlite_engine
    from app.core.security import create_tianhua_pick_token, hash_password
    from app.models import Base
    from app.models.customer import Customer
    from app.models.delivery import Delivery
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.tianhua_pre_delivery import (
        TianhuaPreDeliveryDraftItem,
        TianhuaPreDeliveryImportItem,
    )
    from app.models.user import User
    from app.services import tianhua_pre_delivery as service

    monkeypatch.setenv("ERP_SECRET_KEY", "mobile-pick-api-secret")
    monkeypatch.setattr(
        service,
        "recognize_tianhua_image",
        lambda _content: [
            service.RecognizedRow(1, "21301877 200", "21301877", 200),
            service.RecognizedRow(2, "21301877 300", "21301877", 300),
        ],
    )
    monkeypatch.setattr(
        "app.api.tianhua_pre_delivery._lan_ip",
        lambda: "192.168.1.88",
    )

    engine = create_sqlite_engine(tmp_path / "mobile.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        user = User(
            username="sales",
            password_hash=hash_password("RolePass123!"),
            role="sales",
            real_name="业务",
            display_name="业务",
            must_change_password=False,
        )
        customer = Customer(
            customer_number=5,
            customer_code="天华",
            name="苏州天华超净科技股份有限公司",
            credit_limit=Decimal("0"),
        )
        db.add_all([user, customer])
        db.flush()
        product = Product(
            customer_id=customer.id,
            product_code="21301877",
            customer_material_code="21301877",
            product_name="测试产品",
            box_category="normal",
        )
        db.add(product)
        db.flush()
        order = Order(
            order_number="TH-MOBILE-1",
            customer_id=customer.id,
            order_date=date.today(),
            status="pending_delivery",
            payment_status="unpaid",
            total_amount=Decimal("0"),
        )
        db.add(order)
        db.flush()
        db.add(
            OrderItem(
                order_id=order.id,
                product_id=product.id,
                quantity=200,
                delivered_quantity=0,
                unit_price=Decimal("0"),
                subtotal=Decimal("0"),
                material_status="received",
                snapshot_product_name="测试产品",
                snapshot_product_code="21301877",
            )
        )
        db.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(router, prefix="/api/deliveries")
    app.include_router(mobile_router, prefix="/api/mobile")

    def override_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as client:
        assert client.post(
            "/api/auth/login",
            json={"username": "sales", "password": "RolePass123!"},
        ).status_code == 200
        uploaded = client.post(
            "/api/deliveries/tianhua-preimport/upload",
            files={"file": ("pick.png", b"x", "image/png")},
        )
        rows = uploaded.json()["items"]
        assert rows[1]["status"] == "stock_shortage"
        draft = client.post(
            f"/api/deliveries/tianhua-preimport/{uploaded.json()['batch_id']}/create-draft",
            json={
                "items": [
                    {
                        "item_id": rows[0]["item_id"],
                        "row_no": 1,
                        "selected": True,
                        "final_delivery_qty": 200,
                    },
                    {
                        "item_id": rows[1]["item_id"],
                        "row_no": 2,
                        "selected": True,
                        "final_delivery_qty": 200,
                    },
                ]
            },
        )
        assert draft.status_code == 201
        token_response = client.post(
            f"/api/deliveries/tianhua-preimport/{uploaded.json()['batch_id']}/mobile-token"
        )
        assert token_response.status_code == 200
        assert "192.168.1.88" in token_response.json()["url"]
        token = token_response.json()["token"]

        mobile = client.get("/api/mobile/tianhua-pick", params={"token": token})
        assert mobile.status_code == 200
        assert len(mobile.json()["items"]) == 2
        shortage = mobile.json()["items"][1]
        assert shortage["status"] == "stock_shortage"
        assert shortage["order_no"] == "TH-MOBILE-1"
        expired_token, _expires = create_tianhua_pick_token(
            uploaded.json()["batch_id"],
            draft.json()["draft_id"],
            expires_hours=-1,
        )
        expired = client.get(
            "/api/mobile/tianhua-pick",
            params={"token": expired_token},
        )
        invalid = client.get(
            "/api/mobile/tianhua-pick",
            params={"token": "invalid-token"},
        )
        assert expired.status_code == 403
        assert "二维码已过期" in expired.json()["detail"]
        assert invalid.status_code == 403

        partial = client.put(
            f"/api/mobile/tianhua-pick/items/{shortage['item_id']}",
            json={
                "token": token,
                "mobile_pick_status": "picked",
                "mobile_picked_qty": 150,
                "mobile_pick_note": "现场只有 150",
            },
        )
        assert partial.status_code == 200
        assert partial.json()["item"]["mobile_pick_status"] == "partial"

        first = mobile.json()["items"][0]
        no_stock = client.put(
            f"/api/mobile/tianhua-pick/items/{first['item_id']}",
            json={
                "token": token,
                "mobile_pick_status": "no_stock",
                "mobile_picked_qty": 0,
                "mobile_pick_note": "现场无货",
            },
        )
        assert no_stock.status_code == 200

        refreshed = client.get(
            f"/api/deliveries/tianhua-preimport/{uploaded.json()['batch_id']}"
        )
        assert refreshed.json()["items"][1]["final_delivery_qty"] == 150
        assert refreshed.json()["items"][1]["mobile_pick_status"] == "partial"
        assert refreshed.json()["items"][0]["final_delivery_qty"] == 0
        saved_again = client.put(
            f"/api/deliveries/tianhua-preimport/{uploaded.json()['batch_id']}/update-draft",
            json={
                "items": [
                    {
                        "item_id": rows[0]["item_id"],
                        "row_no": 1,
                        "selected": True,
                        "final_delivery_qty": 0,
                    },
                    {
                        "item_id": rows[1]["item_id"],
                        "row_no": 2,
                        "selected": True,
                        "final_delivery_qty": 150,
                    },
                ]
            },
        )
        assert saved_again.status_code == 200
        assert [
            item["mobile_pick_status"] for item in saved_again.json()["items"]
        ] == ["no_stock", "partial"]

    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Delivery)) == 0
        assert db.get(OrderItem, 1).delivered_quantity == 0
        picked = db.scalars(
            select(TianhuaPreDeliveryDraftItem).order_by(
                TianhuaPreDeliveryDraftItem.row_no
            )
        ).all()
        imported = db.scalars(
            select(TianhuaPreDeliveryImportItem).order_by(
                TianhuaPreDeliveryImportItem.row_no
            )
        ).all()
        assert [item.delivery_qty for item in picked] == [0, 150]
        assert [item.final_delivery_qty for item in imported] == [0, 150]


def test_delivery_picker_role_is_not_allowed_by_erp_role_checks(tmp_path):
    from app.api.deps import RoleChecker, get_db
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "role.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        db.add(
            User(
                username="picker",
                password_hash=hash_password("PickerPass123!"),
                role="delivery_picker",
                real_name="送货拿货员",
                must_change_password=False,
            )
        )
        db.commit()

    app = FastAPI()
    from app.api.auth import router as auth_router

    app.include_router(auth_router, prefix="/api/auth")

    @app.get("/api/formal-delivery")
    def formal_delivery(_user=Depends(RoleChecker(["admin", "sales"]))):
        return {"ok": True}

    def override_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as client:
        assert client.post(
            "/api/auth/login",
            json={"username": "picker", "password": "PickerPass123!"},
        ).status_code == 200
        assert client.get("/api/formal-delivery").status_code == 403


def test_mobile_page_is_standalone_and_supports_stock_shortage():
    from pathlib import Path

    html = (
        Path(__file__).resolve().parents[1]
        / "static"
        / "mobile_tianhua_pick.html"
    ).read_text(encoding="utf-8")
    for text in (
        "天华预送货拿货",
        "/api/mobile/tianhua-pick",
        "已拿货",
        "没货",
        "部分拿货",
        "此操作不会自动入库或扣库存",
    ):
        assert text in html
    assert "后台菜单" not in html
