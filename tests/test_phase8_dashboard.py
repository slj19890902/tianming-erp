from __future__ import annotations

from collections.abc import Generator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker


def test_dashboard_frontend_loads_real_kpi_endpoint() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "static" / "index.html"
    ).read_text(encoding="utf-8")

    assert 'axios.get("/api/dashboard/kpi")' in source
    assert "monthly_gross_profit" in source
    assert "含模拟订单" not in source


def test_dashboard_kpi_uses_real_database_aggregates(tmp_path: Path) -> None:
    from app.api.auth import router as auth_router
    from app.api.dashboard import router as dashboard_router
    from app.api.deps import get_db
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.customer import Customer
    from app.models.delivery import Delivery, DeliveryItem
    from app.models.finance import (
        ReturnReceipt,
        ReturnReceiptItem,
        Statement,
        StatementItem,
    )
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "dashboard.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    today = date.today()
    with factory() as session:
        user = User(
            username="admin",
            password_hash=hash_password("RolePass123!"),
            role="admin",
            real_name="管理员",
            must_change_password=False,
        )
        customer = Customer(
            customer_number=1,
            customer_code="SME",
            name="苏州思迈尔包装有限公司",
            payment_term_days=30,
            credit_limit=0,
        )
        session.add_all([user, customer])
        session.flush()
        product = Product(
            customer_id=customer.id,
            product_code="P1",
            customer_material_code="P1",
            product_name="测试纸箱",
            box_category="normal",
            cost_unit_price=Decimal("2.70"),
        )
        session.add(product)
        session.flush()
        received_order = Order(
            order_number="PO-KPI-001",
            customer_id=customer.id,
            order_date=today,
            delivery_date=today,
            status="partially_delivered",
            payment_status="unpaid",
            total_amount=0,
        )
        material_order = Order(
            order_number="PO-KPI-002",
            customer_id=customer.id,
            order_date=today,
            delivery_date=today,
            status="pending_production",
            payment_status="unpaid",
            total_amount=0,
        )
        session.add_all([received_order, material_order])
        session.flush()
        received_item = OrderItem(
            order_id=received_order.id,
            product_id=product.id,
            quantity=100,
            delivered_quantity=80,
            unit_price=Decimal("3.60"),
            subtotal=0,
            material_status="received",
            snapshot_product_name="测试纸箱",
        )
        pending_material_item = OrderItem(
            order_id=material_order.id,
            product_id=product.id,
            quantity=50,
            delivered_quantity=0,
            unit_price=Decimal("2.00"),
            subtotal=0,
            material_status="pending",
            snapshot_product_name="待收料纸箱",
        )
        session.add_all([received_item, pending_material_item])
        session.flush()
        delivery = Delivery(
            delivery_number="DH-KPI-001",
            customer_id=customer.id,
            delivery_date=today,
            status="dispatched",
            total_quantity=80,
        )
        session.add(delivery)
        session.flush()
        delivery_item = DeliveryItem(
            delivery_id=delivery.id,
            order_item_id=received_item.id,
            delivered_quantity=80,
        )
        session.add(delivery_item)
        session.flush()
        receipt = ReturnReceipt(
            delivery_id=delivery.id,
            actual_received_date=today,
            signed_by="王经理",
            status="confirmed",
        )
        session.add(receipt)
        session.flush()
        receipt_item = ReturnReceiptItem(
            return_receipt_id=receipt.id,
            delivery_item_id=delivery_item.id,
            actual_received_quantity=78,
            difference_reason="拒收2个",
        )
        session.add(receipt_item)
        session.flush()
        statement = Statement(
            statement_number="ST-KPI-001",
            customer_id=customer.id,
            statement_month=today.strftime("%Y-%m"),
            total_receivable=Decimal("280.80"),
            total_gross_profit=Decimal("70.20"),
            status="unsettled",
        )
        session.add(statement)
        session.flush()
        session.add(
            StatementItem(
                statement_id=statement.id,
                return_receipt_item_id=receipt_item.id,
                actual_received_quantity=78,
                unit_price_snapshot=Decimal("3.60"),
                unit_cost_snapshot=Decimal("2.70"),
                receivable_amount=Decimal("280.80"),
                gross_profit_amount=Decimal("70.20"),
            )
        )
        session.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(dashboard_router, prefix="/api/dashboard")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as client:
        assert client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "RolePass123!"},
        ).status_code == 200
        response = client.get("/api/dashboard/kpi")

    assert response.status_code == 200
    body = response.json()
    assert Decimal(str(body["monthly_revenue"])) == Decimal("280.80")
    assert Decimal(str(body["monthly_gross_profit"])) == Decimal("70.20")
    assert Decimal(str(body["outstanding_receivables"])) == Decimal("280.80")
    assert body["today_pending_delivery_tasks"] == 1
    assert body["today_pending_incoming_tasks"] == 1


def test_dashboard_overview_returns_safe_empty_defaults(tmp_path: Path) -> None:
    from app.api.auth import router as auth_router
    from app.api.dashboard import router as dashboard_router
    from app.api.deps import get_db
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "dashboard-overview.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        session.add(
            User(
                username="admin",
                password_hash=hash_password("RolePass123!"),
                role="admin",
                real_name="管理员",
                must_change_password=False,
            )
        )
        session.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(dashboard_router, prefix="/api/dashboard")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as client:
        assert client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "RolePass123!"},
        ).status_code == 200
        response = client.get("/api/dashboard/overview")

    assert response.status_code == 200
    body = response.json()
    assert body["summary"]["today_orders"] == 0
    assert body["summary"]["today_deliveries"] == 0
    assert body["summary"]["today_receipts"] == 0
    assert len(body["cards"]) == 6
    assert {card["count"] for card in body["cards"]} == {0}
    assert body["todos"] == []


def test_dashboard_overview_uses_workflow_counts_and_todos(tmp_path: Path) -> None:
    from app.api.auth import router as auth_router
    from app.api.dashboard import router as dashboard_router
    from app.api.deps import get_db
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.customer import Customer
    from app.models.delivery import Delivery, DeliveryItem
    from app.models.finance import (
        ReturnReceipt,
        ReturnReceiptItem,
        Statement,
        StatementItem,
    )
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "dashboard-overview-counts.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    today = date.today()
    with factory() as session:
        user = User(
            username="admin",
            password_hash=hash_password("RolePass123!"),
            role="admin",
            real_name="管理员",
            must_change_password=False,
        )
        customer = Customer(
            customer_number=1,
            customer_code="SME",
            name="苏州思迈尔包装有限公司",
            payment_term_days=30,
            credit_limit=0,
        )
        session.add_all([user, customer])
        session.flush()
        product = Product(
            customer_id=customer.id,
            product_code="P1",
            customer_material_code="P1",
            product_name="测试纸箱",
            box_category="normal",
            cost_unit_price=Decimal("2.70"),
        )
        session.add(product)
        session.flush()
        material_order = Order(
            order_number="PO-DASH-001",
            customer_id=customer.id,
            order_date=today,
            delivery_date=today,
            status="pending_production",
            payment_status="unpaid",
            total_amount=0,
        )
        incoming_order = Order(
            order_number="PO-DASH-002",
            customer_id=customer.id,
            order_date=today,
            delivery_date=today,
            status="pending_production",
            payment_status="unpaid",
            total_amount=0,
        )
        delivery_order = Order(
            order_number="PO-DASH-003",
            customer_id=customer.id,
            order_date=today,
            delivery_date=today,
            status="pending_delivery",
            payment_status="unpaid",
            total_amount=0,
        )
        receipt_order = Order(
            order_number="PO-DASH-004",
            customer_id=customer.id,
            order_date=today,
            delivery_date=today,
            status="pending_delivery",
            payment_status="unpaid",
            total_amount=0,
        )
        session.add_all([material_order, incoming_order, delivery_order, receipt_order])
        session.flush()
        session.add_all(
            [
                OrderItem(
                    order_id=material_order.id,
                    product_id=product.id,
                    quantity=100,
                    delivered_quantity=0,
                    unit_price=Decimal("3.00"),
                    subtotal=0,
                    material_status="pending",
                    requisition_status="未报料",
                    snapshot_product_name="测试纸箱",
                    snapshot_product_code="P1",
                ),
                OrderItem(
                    order_id=incoming_order.id,
                    product_id=product.id,
                    quantity=80,
                    delivered_quantity=0,
                    unit_price=Decimal("3.10"),
                    subtotal=0,
                    material_status="pending",
                    requisition_status="已报料",
                    snapshot_product_name="测试纸箱",
                    snapshot_product_code="P1",
                ),
                OrderItem(
                    order_id=delivery_order.id,
                    product_id=product.id,
                    quantity=70,
                    delivered_quantity=20,
                    unit_price=Decimal("3.20"),
                    subtotal=0,
                    material_status="received",
                    requisition_status="已入库",
                    snapshot_product_name="测试纸箱",
                    snapshot_product_code="P1",
                ),
                OrderItem(
                    order_id=receipt_order.id,
                    product_id=product.id,
                    quantity=60,
                    delivered_quantity=60,
                    unit_price=Decimal("3.30"),
                    subtotal=0,
                    material_status="received",
                    requisition_status="已入库",
                    snapshot_product_name="测试纸箱",
                    snapshot_product_code="P1",
                ),
            ]
        )
        session.flush()
        delivery_pending = Delivery(
            delivery_number="DH-DASH-001",
            customer_id=customer.id,
            delivery_date=today,
            status="dispatched",
            total_quantity=20,
        )
        delivery_confirmed = Delivery(
            delivery_number="DH-DASH-002",
            customer_id=customer.id,
            delivery_date=today,
            status="dispatched",
            total_quantity=60,
        )
        session.add_all([delivery_pending, delivery_confirmed])
        session.flush()
        pending_delivery_item = session.get(OrderItem, 3)
        confirmed_delivery_item = session.get(OrderItem, 4)
        session.add_all(
            [
                DeliveryItem(
                    delivery_id=delivery_pending.id,
                    order_item_id=pending_delivery_item.id,
                    delivered_quantity=20,
                ),
                DeliveryItem(
                    delivery_id=delivery_confirmed.id,
                    order_item_id=confirmed_delivery_item.id,
                    delivered_quantity=60,
                ),
            ]
        )
        session.flush()
        receipt = ReturnReceipt(
            delivery_id=delivery_confirmed.id,
            actual_received_date=today,
            signed_by="客户签收",
            status="confirmed",
        )
        session.add(receipt)
        session.flush()
        receipt_item = ReturnReceiptItem(
            return_receipt_id=receipt.id,
            delivery_item_id=delivery_confirmed.items[0].id,
            actual_received_quantity=58,
            difference_reason="少收2箱",
        )
        session.add(receipt_item)
        session.flush()
        statement = Statement(
            statement_number="ST-DASH-001",
            customer_id=customer.id,
            statement_month=today.strftime("%Y-%m"),
            total_receivable=Decimal("174.00"),
            total_gross_profit=Decimal("58.00"),
            settled_amount=Decimal("0"),
            status="unsettled",
        )
        session.add(statement)
        session.flush()
        session.add(
            StatementItem(
                statement_id=statement.id,
                return_receipt_item_id=receipt_item.id,
                actual_received_quantity=58,
                unit_price_snapshot=Decimal("3.00"),
                unit_cost_snapshot=Decimal("2.00"),
                receivable_amount=Decimal("174.00"),
                gross_profit_amount=Decimal("58.00"),
            )
        )
        session.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(dashboard_router, prefix="/api/dashboard")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as client:
        assert client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "RolePass123!"},
        ).status_code == 200
        response = client.get("/api/dashboard/overview")

    assert response.status_code == 200
    body = response.json()
    assert [card["key"] for card in body["cards"]] == [
        "pending_material",
        "pending_incoming",
        "pending_delivery",
        "pending_receipt",
        "pending_reconciliation",
        "unsettled_statements",
    ]
    counts = {card["key"]: card["count"] for card in body["cards"]}
    assert counts["pending_material"] == 1
    assert counts["pending_incoming"] == 1
    assert counts["pending_delivery"] == 1
    assert counts["pending_receipt"] == 1
    assert counts["pending_reconciliation"] == 0
    assert counts["unsettled_statements"] == 1
    assert body["todos"]
    assert body["todos"][0]["type"] == "待报料"
    assert body["todos"][0]["target"] == "requisition"


def test_dashboard_overview_groups_reconciliation_todos_by_customer_and_month(
    tmp_path: Path,
) -> None:
    from app.api.auth import router as auth_router
    from app.api.dashboard import router as dashboard_router
    from app.api.deps import get_db
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.customer import Customer
    from app.models.delivery import Delivery, DeliveryItem
    from app.models.finance import ReturnReceipt, ReturnReceiptItem
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "dashboard-overview-reconciliation.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        session.add(
            User(
                username="admin",
                password_hash=hash_password("RolePass123!"),
                role="admin",
                real_name="管理员",
                must_change_password=False,
            )
        )
        customer_a = Customer(
            customer_number=1,
            customer_code="CUST-A",
            name="苏州思迈尔包装有限公司",
            payment_term_days=30,
            credit_limit=0,
        )
        customer_b = Customer(
            customer_number=2,
            customer_code="CUST-B",
            name="昆山华诚电子有限公司",
            payment_term_days=30,
            credit_limit=0,
        )
        session.add_all([customer_a, customer_b])
        session.flush()
        product_a = Product(
            customer_id=customer_a.id,
            product_code="PA",
            customer_material_code="PA",
            product_name="测试纸箱A",
            box_category="normal",
            cost_unit_price=Decimal("1.00"),
        )
        product_b = Product(
            customer_id=customer_b.id,
            product_code="PB",
            customer_material_code="PB",
            product_name="测试纸箱B",
            box_category="normal",
            cost_unit_price=Decimal("1.00"),
        )
        session.add_all([product_a, product_b])
        session.flush()

        def add_confirmed_receipt(
            *,
            customer: Customer,
            product: Product,
            order_no: str,
            receipt_date: date,
            quantity: int,
        ) -> None:
            order = Order(
                order_number=order_no,
                customer_id=customer.id,
                order_date=receipt_date,
                delivery_date=receipt_date,
                status="pending_delivery",
                payment_status="unpaid",
                total_amount=Decimal("0"),
            )
            session.add(order)
            session.flush()
            order_item = OrderItem(
                order_id=order.id,
                product_id=product.id,
                quantity=quantity,
                delivered_quantity=quantity,
                unit_price=Decimal("10.00"),
                subtotal=Decimal("0"),
                material_status="received",
                requisition_status="已入库",
                snapshot_product_name=product.product_name,
                snapshot_product_code=product.product_code,
            )
            session.add(order_item)
            session.flush()
            delivery = Delivery(
                delivery_number=f"DH-{order_no}",
                customer_id=customer.id,
                delivery_date=receipt_date,
                status="dispatched",
                total_quantity=quantity,
            )
            session.add(delivery)
            session.flush()
            delivery_item = DeliveryItem(
                delivery_id=delivery.id,
                order_item_id=order_item.id,
                delivered_quantity=quantity,
            )
            session.add(delivery_item)
            session.flush()
            receipt = ReturnReceipt(
                delivery_id=delivery.id,
                actual_received_date=receipt_date,
                signed_by="签收人",
                status="confirmed",
            )
            session.add(receipt)
            session.flush()
            session.add(
                ReturnReceiptItem(
                    return_receipt_id=receipt.id,
                    delivery_item_id=delivery_item.id,
                    actual_received_quantity=quantity,
                    difference_reason="",
                )
            )

        add_confirmed_receipt(
            customer=customer_a,
            product=product_a,
            order_no="PO-RECON-A1",
            receipt_date=date(2026, 6, 1),
            quantity=10,
        )
        add_confirmed_receipt(
            customer=customer_a,
            product=product_a,
            order_no="PO-RECON-A2",
            receipt_date=date(2026, 6, 8),
            quantity=20,
        )
        add_confirmed_receipt(
            customer=customer_a,
            product=product_a,
            order_no="PO-RECON-A3",
            receipt_date=date(2026, 7, 2),
            quantity=15,
        )
        add_confirmed_receipt(
            customer=customer_b,
            product=product_b,
            order_no="PO-RECON-B1",
            receipt_date=date(2026, 6, 3),
            quantity=8,
        )
        session.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(dashboard_router, prefix="/api/dashboard")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as client:
        assert client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "RolePass123!"},
        ).status_code == 200
        response = client.get("/api/dashboard/overview")

    assert response.status_code == 200
    body = response.json()
    counts = {card["key"]: card["count"] for card in body["cards"]}
    assert counts["pending_reconciliation"] == 4
    recon_todos = [todo for todo in body["todos"] if todo["type"] == "待对账"]
    assert len(recon_todos) == 3
    assert {
        (todo["customer_name"], todo["month"])
        for todo in recon_todos
    } == {
        ("苏州思迈尔包装有限公司", "2026-06"),
        ("苏州思迈尔包装有限公司", "2026-07"),
        ("昆山华诚电子有限公司", "2026-06"),
    }
    june_sme = next(
        todo
        for todo in recon_todos
        if todo["customer_name"] == "苏州思迈尔包装有限公司"
        and todo["month"] == "2026-06"
    )
    assert june_sme["item_count"] == 2
    assert Decimal(str(june_sme["amount"])) == Decimal("300.00")
    assert "月结对账单" in june_sme["message"]
    assert june_sme["action_text"] == "去生成月结对账单"
