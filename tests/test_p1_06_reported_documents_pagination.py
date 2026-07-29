from __future__ import annotations

from collections.abc import Generator
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture()
def reported_documents_app(tmp_path: Path):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.requisition import router as requisition_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.access_control import UserCustomerScope, UserPermissionOverride
    from app.models.customer import Customer
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.requisition import Requisition, RequisitionItem
    from app.models.stock_replenishment import (
        StockReplenishmentOrder,
        StockReplenishmentOrderItem,
    )
    from app.models.supplier_requisition_order import (
        SupplierRequisitionOrder,
        SupplierRequisitionOrderItem,
    )
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "reported-documents.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    with factory() as db:
        admin = User(
            username="admin",
            password_hash=hash_password("AdminPass123!"),
            role="admin",
            real_name="Admin",
            must_change_password=False,
        )
        sales = User(
            username="sales-a",
            password_hash=hash_password("SalesPass123!"),
            role="sales",
            real_name="Sales A",
            must_change_password=False,
            customer_access_mode="selected",
        )
        customer_a = Customer(
            customer_number=1,
            customer_code="P106-A",
            name="P106 客户 A",
        )
        customer_b = Customer(
            customer_number=2,
            customer_code="P106-B",
            name="P106 客户 B",
        )
        db.add_all([admin, sales, customer_a, customer_b])
        db.flush()
        db.add_all(
            [
                UserCustomerScope(user_id=sales.id, customer_id=customer_a.id),
                UserPermissionOverride(
                    user_id=sales.id,
                    permission_code="requisition.view",
                    is_allowed=True,
                    granted_by=admin.id,
                ),
            ]
        )

        base_time = datetime(2026, 7, 29, 9, 0, 0)
        for suffix, customer, created_at in (
            ("A", customer_a, base_time),
            ("B", customer_b, base_time + timedelta(hours=1)),
        ):
            product = Product(
                customer_id=customer.id,
                product_code=f"P106-{suffix}",
                customer_material_code=f"P106-{suffix}",
                product_name=f"P106 产品 {suffix}",
                box_category="normal",
            )
            db.add(product)
            db.flush()
            order = Order(
                order_number=f"TM-P106-{suffix}",
                customer_id=customer.id,
                order_date=date(2026, 7, 29),
                status="pending_production",
                payment_status="unpaid",
                total_amount=Decimal("10"),
                created_at=created_at,
            )
            db.add(order)
            db.flush()
            item = OrderItem(
                order_id=order.id,
                product_id=product.id,
                item_order_number=f"TM-P106-{suffix}-001",
                item_sequence=1,
                quantity=10,
                unit_price=Decimal("1"),
                subtotal=Decimal("10"),
                material_status="pending",
                requisition_status="已报料",
                snapshot_product_code=f"P106-{suffix}",
                snapshot_product_name=f"P106 产品 {suffix}",
            )
            db.add(item)
            db.flush()

            supplier = SupplierRequisitionOrder(
                order_number=f"SRO-P106-{suffix}",
                supplier_name=f"供应商 {suffix}",
                requisition_qty=10,
                status="confirmed",
                created_at=created_at + timedelta(minutes=1),
            )
            db.add(supplier)
            db.flush()
            db.add(
                SupplierRequisitionOrderItem(
                    supplier_order_id=supplier.id,
                    order_item_id=item.id,
                    product_id=product.id,
                    order_number=order.order_number,
                    product_code=product.product_code,
                    product_name=product.product_name,
                    customer_name=customer.name,
                    quantity=10,
                    requisition_qty=10,
                )
            )

            replenishment = StockReplenishmentOrder(
                order_number=f"SR-P106-{suffix}",
                customer_id=customer.id,
                source_type="stock_warning",
                status="confirmed",
                created_at=created_at + timedelta(minutes=2),
            )
            db.add(replenishment)
            db.flush()
            db.add(
                StockReplenishmentOrderItem(
                    replenishment_order_id=replenishment.id,
                    target_inventory_type="semi_finished",
                    product_id=product.id,
                    customer_id=customer.id,
                    product_code_snapshot=product.product_code,
                    product_name_snapshot=product.product_name,
                    pieces_per_box=1,
                    stock_yield_per_sheet=1,
                    quantity=10,
                    stocked_quantity=0,
                    created_at=created_at + timedelta(minutes=2),
                )
            )

            legacy = Requisition(
                requisition_number=f"REQ-P106-{suffix}",
                requisition_date=date(2026, 7, 29),
                supplier_name=f"老供应商 {suffix}",
                status="已报料",
                created_at=created_at + timedelta(minutes=3),
            )
            db.add(legacy)
            db.flush()
            db.add(
                RequisitionItem(
                    requisition_id=legacy.id,
                    order_item_id=item.id,
                    requisition_qty=10,
                    cardboard_len=Decimal("500"),
                    cardboard_width=Decimal("300"),
                    product_code_snapshot=product.product_code,
                    product_name_snapshot=product.product_name,
                )
            )
        db.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(requisition_router, prefix="/api/requisition")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    return app


def _login(client: TestClient, username: str, password: str) -> None:
    response = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text


def test_reported_documents_default_remains_unpaged_and_paged_results_are_stable(
    reported_documents_app,
) -> None:
    with TestClient(reported_documents_app) as client:
        _login(client, "admin", "AdminPass123!")
        legacy = client.get("/api/requisition/reported-documents")
        first = client.get(
            "/api/requisition/reported-documents", params={"page": 1, "page_size": 2}
        )
        second = client.get(
            "/api/requisition/reported-documents", params={"page": 2, "page_size": 2}
        )

    assert legacy.status_code == first.status_code == second.status_code == 200
    assert legacy.json()["total"] == len(legacy.json()["items"]) == 6
    assert first.json()["total"] == second.json()["total"] == 6
    assert first.json()["page"] == 1
    assert first.json()["page_size"] == 2
    assert len(first.json()["items"]) == len(second.json()["items"]) == 2
    first_keys = {(row["source_type"], row["id"]) for row in first.json()["items"]}
    second_keys = {(row["source_type"], row["id"]) for row in second.json()["items"]}
    assert first_keys.isdisjoint(second_keys)


def test_reported_document_filters_apply_after_customer_scope(
    reported_documents_app,
) -> None:
    with TestClient(reported_documents_app) as client:
        _login(client, "sales-a", "SalesPass123!")
        scoped = client.get(
            "/api/requisition/reported-documents", params={"page": 1, "page_size": 20}
        )
        product = client.get(
            "/api/requisition/reported-documents",
            params={"product_code": "P106-A", "page": 1, "page_size": 20},
        )
        supplier = client.get(
            "/api/requisition/reported-documents",
            params={"supplier_name": "老供应商 A", "page": 1, "page_size": 20},
        )
        blocked = client.get(
            "/api/requisition/reported-documents", params={"customer_id": 2}
        )

    assert scoped.status_code == product.status_code == supplier.status_code == 200
    assert scoped.json()["total"] == 3
    assert {row["source_type"] for row in scoped.json()["items"]} == {
        "supplier_order",
        "stock_replenishment",
        "legacy_material_requisition",
    }
    assert all("P106-B" not in str(row) for row in scoped.json()["items"])
    assert product.json()["total"] == 3
    assert supplier.json()["total"] == 1
    assert supplier.json()["items"][0]["document_number"] == "REQ-P106-A"
    assert blocked.status_code == 403
