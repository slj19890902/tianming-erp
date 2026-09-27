from datetime import date
from decimal import Decimal
from io import BytesIO

from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy.orm import sessionmaker


def _yanguang_delivery_file() -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "送货单样本"
    sheet.append(["苏州天明包装有限公司送货单"])
    sheet.append(["客户:捷太格特电子(无锡)有限公司---研光 2026.09.28"])
    sheet.append(["N0.", "图号", "品目号", "类别", "数量", "单价", "金额"])
    sheet.append([1, "0632094", "80010631", "A", 600, 2.865, 1719])
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def test_excel_pre_delivery_upload_matches_customer_order_without_dispatch(tmp_path, monkeypatch):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.tianhua_pre_delivery import router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.customer import Customer
    from app.models.delivery import Delivery
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.tianhua_pre_delivery import TianhuaPreDeliveryImportBatch
    from app.models.user import User

    monkeypatch.setenv("ERP_SECRET_KEY", "excel-pre-delivery-test-secret-32b")
    engine = create_sqlite_engine(tmp_path / "excel-pre-delivery.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        user = User(
            username="admin",
            password_hash=hash_password("RolePass123!"),
            role="admin",
            real_name="管理员",
            display_name="管理员",
            must_change_password=False,
        )
        customer = Customer(
            customer_number=137,
            customer_code="YG",
            chinese_short_name="研光",
            name="研光电子（无锡）有限公司",
            credit_limit=Decimal("0"),
        )
        db.add_all([user, customer])
        db.flush()
        product = Product(
            customer_id=customer.id,
            product_code="80010631",
            customer_material_code="80010631",
            product_name="测试纸箱",
            box_category="normal",
        )
        db.add(product)
        db.flush()
        order = Order(
            order_number="YG-TEST-1",
            customer_id=customer.id,
            customer_po="PO-TEST",
            order_date=date(2026, 9, 20),
            delivery_date=date(2026, 9, 28),
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
                quantity=600,
                delivered_quantity=0,
                unit_price=Decimal("0"),
                subtotal=Decimal("0"),
                material_status="pending",
                requisition_status="已报料",
                snapshot_product_name="测试纸箱",
                snapshot_product_code="80010631",
            )
        )
        db.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(router, prefix="/api/deliveries")

    def override():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override
    with TestClient(app) as client:
        assert client.post(
            "/api/auth/login", json={"username": "admin", "password": "RolePass123!"}
        ).status_code == 200
        response = client.post(
            "/api/deliveries/pre-delivery-excel/upload",
            files={
                "file": (
                    "研光送货单.xlsx",
                    _yanguang_delivery_file(),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["source_type"] == "excel_upload"
    assert body["total_rows"] == 1
    assert body["items"][0]["stock_code"] == "80010631"
    assert body["items"][0]["fulfillment_status"] == "incoming"
    assert body["items"][0]["finished_available_qty"] == 0
    assert body["items"][0]["shortage_qty"] == 600
    diagnostic = body["items"][0]["source_payload"]["shortage_diagnostic"]
    assert diagnostic["effective_inbound"] == 600
    assert diagnostic["new_purchase_shortage"] == 0
    with factory() as db:
        assert db.query(TianhuaPreDeliveryImportBatch).count() == 1
        assert db.query(Delivery).count() == 0
