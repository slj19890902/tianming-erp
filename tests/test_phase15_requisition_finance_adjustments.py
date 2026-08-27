from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker


def _login(client: TestClient, role: str = "sales") -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": role, "password": "RolePass123!"},
    )
    assert response.status_code == 200


def _seed_users(session) -> None:
    from app.core.security import hash_password
    from app.models.user import User

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


def test_unrequisitioned_delivered_order_is_not_listed_as_requisition_history(tmp_path: Path) -> None:
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.requisition import router as requisition_router
    from app.core.database import create_sqlite_engine
    from app.models import Base
    from app.models.customer import Customer
    from app.models.order import Order, OrderItem
    from app.models.product import Product

    engine = create_sqlite_engine(tmp_path / "phase15_requisition.sqlite3")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with session_factory() as session:
        _seed_users(session)
        customer = Customer(
            customer_number=1,
            customer_code="HX",
            name="历史客户",
            payment_term_days=30,
            credit_limit=Decimal("0"),
        )
        session.add(customer)
        session.flush()
        product = Product(
            customer_id=customer.id,
            product_code="HX-001",
            customer_material_code="HX-001",
            product_name="历史纸箱",
            legacy_material_text="K=A",
            length_mm=Decimal("380"),
            width_mm=Decimal("260"),
            height_mm=Decimal("220"),
            box_category="normal",
        )
        session.add(product)
        session.flush()
        order = Order(
            order_number="TM20260614001",
            customer_id=customer.id,
            order_date=date(2026, 6, 14),
            delivery_date=date(2026, 6, 21),
            status="delivered",
            payment_status="paid",
            total_amount=Decimal("100"),
        )
        session.add(order)
        session.flush()
        session.add(
            OrderItem(
                order_id=order.id,
                product_id=product.id,
                quantity=10,
                unit_price=Decimal("10"),
                subtotal=Decimal("100"),
                material_status="pending",
                requisition_status="未报料",
                snapshot_product_code="HX-001",
                snapshot_product_name="历史纸箱",
                snapshot_spec="380×260×220mm",
                snapshot_material="K=A",
            )
        )
        session.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(requisition_router, prefix="/api/requisition")

    def override_get_db():
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app) as client:
        _login(client, "admin")
        pending = client.get("/api/requisition/pending")
        archived = client.get(
            "/api/requisition/items",
            params={"include_history": "true"},
        )

    assert pending.status_code == 200
    assert pending.json()["items"] == []
    assert archived.status_code == 200
    assert archived.json()["items"] == []


def test_statement_settlement_allows_missing_account(tmp_path: Path) -> None:
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.finance import router as finance_router
    from app.core.database import create_sqlite_engine
    from app.models import Base
    from app.models.customer import Customer
    from app.models.delivery import Delivery, DeliveryItem
    from app.models.finance import ReturnReceipt, ReturnReceiptItem, SettlementRecord, Statement, StatementItem
    from app.models.order import Order, OrderItem
    from app.models.product import Product

    engine = create_sqlite_engine(tmp_path / "phase15_finance.sqlite3")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with session_factory() as session:
        _seed_users(session)
        customer = Customer(
            customer_number=1,
            customer_code="FIN",
            name="财务客户",
            payment_term_days=30,
            credit_limit=Decimal("0"),
        )
        session.add(customer)
        session.flush()
        product = Product(
            customer_id=customer.id,
            product_code="FIN-001",
            customer_material_code="FIN-001",
            product_name="财务纸箱",
            legacy_material_text="A=B",
            box_category="normal",
        )
        session.add(product)
        session.flush()
        order = Order(
            order_number="TM20260621001",
            customer_id=customer.id,
            order_date=date(2026, 6, 21),
            delivery_date=date(2026, 6, 21),
            status="delivered",
            payment_status="unpaid",
            total_amount=Decimal("280.80"),
        )
        session.add(order)
        session.flush()
        item = OrderItem(
            order_id=order.id,
            product_id=product.id,
            quantity=78,
            delivered_quantity=78,
            unit_price=Decimal("3.60"),
            subtotal=Decimal("280.80"),
            material_status="received",
            requisition_status="已入库",
            snapshot_product_code="FIN-001",
            snapshot_product_name="财务纸箱",
        )
        session.add(item)
        session.flush()
        delivery = Delivery(
            delivery_number="DH-001",
            customer_id=customer.id,
            delivery_date=date(2026, 6, 21),
            status="dispatched",
            total_quantity=78,
        )
        session.add(delivery)
        session.flush()
        delivery_item = DeliveryItem(
            delivery_id=delivery.id,
            order_item_id=item.id,
            delivered_quantity=78,
        )
        session.add(delivery_item)
        session.flush()
        receipt = ReturnReceipt(
            delivery_id=delivery.id,
            actual_received_date=date(2026, 6, 21),
            signed_by="客户",
            status="confirmed",
        )
        session.add(receipt)
        session.flush()
        receipt_item = ReturnReceiptItem(
            return_receipt_id=receipt.id,
            delivery_item_id=delivery_item.id,
            actual_received_quantity=78,
        )
        session.add(receipt_item)
        session.flush()
        statement = Statement(
            statement_number="ST-001",
            customer_id=customer.id,
            statement_month="2026-06",
            total_receivable=Decimal("280.80"),
            total_gross_profit=Decimal("0"),
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
                unit_cost_snapshot=Decimal("0"),
                receivable_amount=Decimal("280.80"),
                gross_profit_amount=Decimal("0"),
            )
        )
        session.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(finance_router, prefix="/api/finance")

    def override_get_db():
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app) as client:
        _login(client, "finance")
        response = client.put(
            "/api/finance/statements/1/settle",
            json={"amount": "100.00", "settlement_date": "2026-06-21"},
        )

    assert response.status_code == 200, response.text
    with session_factory() as session:
        record = session.scalar(select(SettlementRecord))
        assert record is not None
        assert record.account in (None, "")
