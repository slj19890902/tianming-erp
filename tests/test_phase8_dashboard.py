from __future__ import annotations

from collections.abc import Generator
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
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
            resolution_action="continue_delivery",
            difference_reason="拒收2个",
        )
        received_item.delivered_quantity = 78
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
        overview_response = client.get("/api/dashboard/overview")

    assert response.status_code == 200
    assert overview_response.status_code == 200
    body = response.json()
    assert overview_response.json()["kpi"] == body
    assert Decimal(str(body["monthly_revenue"])) == Decimal("280.80")
    assert Decimal(str(body["monthly_gross_profit"])) == Decimal("70.20")
    assert Decimal(str(body["outstanding_receivables"])) == Decimal("280.80")
    assert body["today_pending_delivery_tasks"] == 1
    # P0-04：没有 confirmed 供应商报料单时仍是待报料，不能仅凭
    # material_status=pending 把它算成待收料。
    assert body["today_pending_incoming_tasks"] == 0


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
    assert len(body["cards"]) == 8
    assert {card["count"] for card in body["cards"]} == {0}
    assert body["todos"] == []


def test_dashboard_overview_does_not_hide_actionable_future_delivery_orders(tmp_path: Path) -> None:
    from app.api.auth import router as auth_router
    from app.api.dashboard import router as dashboard_router
    from app.api.deps import get_db
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.customer import Customer
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "dashboard-future-delivery.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    today = date.today()
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
        customer = Customer(
            customer_number=1,
            customer_code="FUT",
            name="交期提醒测试客户",
            payment_term_days=30,
            credit_limit=0,
        )
        session.add(customer)
        session.flush()
        product = Product(
            customer_id=customer.id,
            product_code="FUT-001",
            customer_material_code="FUT-001",
            product_name="提醒测试箱",
            box_category="normal",
        )
        session.add(product)
        session.flush()
        for index, delivery_date in enumerate(
            (today - timedelta(days=1), None, today + timedelta(days=1)),
            start=1,
        ):
            order = Order(
                order_number=f"PO-FUTURE-{index}",
                customer_id=customer.id,
                order_date=today,
                delivery_date=delivery_date,
                status="pending_production",
                payment_status="unpaid",
                total_amount=0,
            )
            session.add(order)
            session.flush()
            session.add(
                OrderItem(
                    order_id=order.id,
                    product_id=product.id,
                    quantity=10,
                    unit_price=Decimal("1"),
                    subtotal=Decimal("10"),
                    material_status="pending",
                    requisition_status="未报料",
                    snapshot_product_name="提醒测试箱",
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
    material_card = next(
        card for card in body["cards"] if card["key"] == "pending_material"
    )
    assert material_card["count"] == 3
    material_todo = next(todo for todo in body["todos"] if todo["type"] == "待报料")
    assert material_todo["count"] == 3


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
    from app.models.supplier_requisition_order import (
        SupplierRequisitionOrder,
        SupplierRequisitionOrderItem,
    )
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
        incoming_item = session.scalar(
            select(OrderItem).where(OrderItem.order_id == incoming_order.id)
        )
        assert incoming_item is not None
        supplier_order = SupplierRequisitionOrder(
            order_number="SRO-DASH-002",
            total_quantity=80,
            requisition_qty=80,
            status="confirmed",
        )
        session.add(supplier_order)
        session.flush()
        session.add(
            SupplierRequisitionOrderItem(
                supplier_order_id=supplier_order.id,
                order_item_id=incoming_item.id,
                product_id=product.id,
                product_name=product.product_name,
                quantity=80,
                requisition_qty=80,
            )
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
            resolution_action="accept_short",
            difference_reason="少收2箱",
        )
        confirmed_delivery_item.delivered_quantity = 58
        confirmed_delivery_item.is_force_closed = True
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
        "pending_production",
        "pending_delivery",
        "pending_receipt",
        "pending_reconciliation",
        "pending_invoice",
        "pending_payment",
    ]
    counts = {card["key"]: card["count"] for card in body["cards"]}
    assert counts["pending_material"] == 1
    assert counts["pending_incoming"] == 1
    assert counts["pending_production"] == 0
    assert counts["pending_delivery"] == 1
    assert counts["pending_receipt"] == 1
    assert counts["pending_reconciliation"] == 0
    assert counts["pending_invoice"] == 1
    assert counts["pending_payment"] == 1
    assert body["todos"]
    todo_types = {todo["type"] for todo in body["todos"]}
    assert {"待报料", "待入库", "待送货", "待回单", "待开票", "待结款"} <= todo_types
    pending_material = next(todo for todo in body["todos"] if todo["type"] == "待报料")
    assert pending_material["target"] == "requisition"
    assert pending_material["count"] == 1
    assert pending_material["first_order_no"] == "PO-DASH-001"


def test_dashboard_overview_groups_reconciliation_todos_by_customer_and_month(
    tmp_path: Path,
) -> None:
    from app.api.auth import router as auth_router
    from app.api.dashboard import router as dashboard_router
    from app.api.deps import get_db
    from app.api.finance import _statement_period
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
    statement_month = date.today().strftime("%Y-%m")
    period_start, _period_end = _statement_period(statement_month, 20)
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
            receipt_date=period_start,
            quantity=10,
        )
        add_confirmed_receipt(
            customer=customer_a,
            product=product_a,
            order_no="PO-RECON-A2",
            receipt_date=period_start + timedelta(days=1),
            quantity=20,
        )
        add_confirmed_receipt(
            customer=customer_a,
            product=product_a,
            order_no="PO-RECON-A3",
            receipt_date=period_start + timedelta(days=2),
            quantity=15,
        )
        add_confirmed_receipt(
            customer=customer_b,
            product=product_b,
            order_no="PO-RECON-B1",
            receipt_date=period_start + timedelta(days=3),
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
    assert counts["pending_reconciliation"] == 2
    recon_todos = [todo for todo in body["todos"] if todo["type"] == "待对账"]
    assert len(recon_todos) == 2
    assert {
        (todo["customer_name"], todo["month"])
        for todo in recon_todos
    } == {
        ("苏州思迈尔包装有限公司", statement_month),
        ("昆山华诚电子有限公司", statement_month),
    }
    june_sme = next(
        todo
        for todo in recon_todos
        if todo["customer_name"] == "苏州思迈尔包装有限公司"
        and todo["month"] == statement_month
    )
    assert june_sme["count"] == 3
    assert Decimal(str(june_sme["amount"])) == Decimal("450.00")
    assert june_sme["action_text"] == "去生成月结对账单"


def test_dashboard_overview_groups_same_customer_same_status_into_one_todo(
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
    from app.models.finance import Statement
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.production import ProductionTask
    from app.models.supplier_requisition_order import (
        SupplierRequisitionOrder,
        SupplierRequisitionOrderItem,
    )
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "dashboard-overview-grouped.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    today = date(2026, 6, 29)
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
            customer_code="A",
            name="苏州天华超净科技股份有限公司",
            payment_term_days=30,
            credit_limit=0,
        )
        customer_b = Customer(
            customer_number=2,
            customer_code="B",
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
        )
        product_b = Product(
            customer_id=customer_b.id,
            product_code="PB",
            customer_material_code="PB",
            product_name="测试纸箱B",
            box_category="normal",
        )
        session.add_all([product_a, product_b])
        session.flush()

        def add_order(
            *,
            customer: Customer,
            product: Product,
            order_no: str,
            requisition_status: str,
            material_status: str,
            delivered_quantity: int = 0,
            quantity: int = 10,
        ) -> OrderItem:
            order = Order(
                order_number=order_no,
                customer_id=customer.id,
                order_date=today,
                delivery_date=today,
                status="pending_delivery",
                payment_status="unpaid",
                total_amount=Decimal("0"),
            )
            session.add(order)
            session.flush()
            item = OrderItem(
                order_id=order.id,
                product_id=product.id,
                quantity=quantity,
                delivered_quantity=delivered_quantity,
                unit_price=Decimal("10.00"),
                subtotal=Decimal("0"),
                material_status=material_status,
                requisition_status=requisition_status,
                snapshot_product_name=product.product_name,
                snapshot_product_code=product.product_code,
            )
            session.add(item)
            session.flush()
            return item

        add_order(customer=customer_a, product=product_a, order_no="PO-MAT-001", requisition_status="未报料", material_status="pending")
        add_order(customer=customer_a, product=product_a, order_no="PO-MAT-002", requisition_status="未报料", material_status="pending")
        incoming_item_1 = add_order(customer=customer_a, product=product_a, order_no="PO-IN-001", requisition_status="已报料", material_status="pending")
        incoming_item_2 = add_order(customer=customer_a, product=product_a, order_no="PO-IN-002", requisition_status="供应商已排单", material_status="pending")
        pending_delivery_item_1 = add_order(customer=customer_a, product=product_a, order_no="PO-DE-001", requisition_status="已入库", material_status="received", delivered_quantity=0)
        pending_delivery_item_2 = add_order(customer=customer_a, product=product_a, order_no="PO-DE-002", requisition_status="已入库", material_status="received", delivered_quantity=4, quantity=10)
        shipped_item_1 = add_order(customer=customer_a, product=product_a, order_no="PO-RC-001", requisition_status="已入库", material_status="received", delivered_quantity=8, quantity=8)
        shipped_item_2 = add_order(customer=customer_a, product=product_a, order_no="PO-RC-002", requisition_status="已入库", material_status="received", delivered_quantity=6, quantity=6)
        add_order(customer=customer_b, product=product_b, order_no="PO-MAT-B1", requisition_status="未报料", material_status="pending")

        for index, incoming_item in enumerate(
            [incoming_item_1, incoming_item_2], start=1
        ):
            supplier_order = SupplierRequisitionOrder(
                order_number=f"SRO-GROUP-{index}",
                total_quantity=10,
                requisition_qty=10,
                status="confirmed",
            )
            session.add(supplier_order)
            session.flush()
            session.add(
                SupplierRequisitionOrderItem(
                    supplier_order_id=supplier_order.id,
                    order_item_id=incoming_item.id,
                    product_id=product_a.id,
                    product_name=product_a.product_name,
                    quantity=10,
                    requisition_qty=10,
                )
            )
        for pending_delivery_item in [
            pending_delivery_item_1,
            pending_delivery_item_2,
        ]:
            session.add(
                ProductionTask(
                    order_item_id=pending_delivery_item.id,
                    status="completed",
                    planned_quantity=10,
                    finished_coverage_snapshot=0,
                    ordered_quantity_snapshot=10,
                    material_received_quantity=10,
                    material_input_quantity=10,
                    output_factor=1,
                    version=1,
                )
            )

        delivery_1 = Delivery(
            delivery_number="DH-RC-001",
            customer_id=customer_a.id,
            delivery_date=today,
            status="dispatched",
            total_quantity=8,
        )
        delivery_2 = Delivery(
            delivery_number="DH-RC-002",
            customer_id=customer_a.id,
            delivery_date=today,
            status="dispatched",
            total_quantity=6,
        )
        partial_delivery = Delivery(
            delivery_number="DH-DE-002",
            customer_id=customer_a.id,
            delivery_date=today,
            status="dispatched",
            total_quantity=4,
        )
        session.add_all([delivery_1, delivery_2, partial_delivery])
        session.flush()
        session.add_all(
            [
                DeliveryItem(delivery_id=delivery_1.id, order_item_id=shipped_item_1.id, delivered_quantity=8),
                DeliveryItem(delivery_id=delivery_2.id, order_item_id=shipped_item_2.id, delivered_quantity=6),
                DeliveryItem(delivery_id=partial_delivery.id, order_item_id=pending_delivery_item_2.id, delivered_quantity=4),
            ]
        )
        session.add_all(
            [
                Statement(
                    statement_number="ST-A-001",
                    customer_id=customer_a.id,
                    statement_month=date.today().strftime("%Y-%m"),
                    total_receivable=Decimal("1000.00"),
                    settled_amount=Decimal("200.00"),
                    total_gross_profit=Decimal("0"),
                    status="unsettled",
                ),
                Statement(
                    statement_number="ST-A-002",
                    customer_id=customer_a.id,
                    statement_month=date.today().strftime("%Y-%m"),
                    total_receivable=Decimal("500.00"),
                    settled_amount=Decimal("0"),
                    total_gross_profit=Decimal("0"),
                    status="unsettled",
                ),
            ]
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

    def find_todo(todo_type: str, customer_name: str) -> dict:
        return next(
            todo
            for todo in body["todos"]
            if todo["type"] == todo_type and todo["customer_name"] == customer_name
        )

    pending_material = find_todo("待报料", "苏州天华超净科技股份有限公司")
    assert pending_material["count"] == 2
    assert pending_material["first_order_no"] in {"PO-MAT-001", "PO-MAT-002"}

    pending_incoming = find_todo("待入库", "苏州天华超净科技股份有限公司")
    assert pending_incoming["count"] == 2
    assert pending_incoming["first_item_no"] == "PA"

    assert not any(
        todo["type"] == "待送货"
        and todo["customer_name"] == "苏州天华超净科技股份有限公司"
        for todo in body["todos"]
    )

    pending_receipt = find_todo("待回单", "苏州天华超净科技股份有限公司")
    assert pending_receipt["count"] == 3

    pending_payment = find_todo("待结款", "苏州天华超净科技股份有限公司")
    assert pending_payment["count"] == 1
    assert Decimal(str(pending_payment["amount"])) == Decimal("1300.00")
