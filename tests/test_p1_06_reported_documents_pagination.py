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
            chinese_short_name="客户甲",
        )
        customer_b = Customer(
            customer_number=2,
            customer_code="P106-B",
            name="P106 客户 B",
            chinese_short_name="客户乙",
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
                    report_length_mm=500 if suffix == "A" else 300,
                    report_width_mm=300 if suffix == "A" else 500,
                    material_code_snapshot="A+B" if suffix == "A" else "K=K",
                    flute_type_snapshot="B" if suffix == "A" else "BC",
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
                    report_length_mm=500 if suffix == "A" else 300,
                    report_width_mm=300 if suffix == "A" else 500,
                    material_code_snapshot="A+B" if suffix == "A" else "K=K",
                    flute_type="B" if suffix == "A" else "BC",
                    created_at=created_at + timedelta(minutes=2),
                )
            )
            if suffix == "A":
                db.add(
                    StockReplenishmentOrderItem(
                        replenishment_order_id=replenishment.id,
                        target_inventory_type="semi_finished",
                        product_id=product.id,
                        customer_id=customer.id,
                        product_code_snapshot="P106-A-ALT",
                        product_name_snapshot="P106 产品 A 备用",
                        pieces_per_box=1,
                        stock_yield_per_sheet=1,
                        quantity=6,
                        stocked_quantity=0,
                        report_length_mm=700,
                        report_width_mm=400,
                        material_code_snapshot="C+D",
                        flute_type="E",
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
                    cardboard_len=Decimal("500" if suffix == "A" else "300"),
                    cardboard_width=Decimal("300" if suffix == "A" else "500"),
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


def test_reported_document_dimensions_never_swap_or_cross_sibling_lines(
    reported_documents_app,
) -> None:
    with TestClient(reported_documents_app) as client:
        _login(client, "admin", "AdminPass123!")
        exact = client.get(
            "/api/requisition/reported-documents",
            params={"report_length_mm": 500, "report_width_mm": 300},
        )
        swapped = client.get(
            "/api/requisition/reported-documents",
            params={"report_length_mm": 300, "report_width_mm": 500},
        )
        cross_sibling = client.get(
            "/api/requisition/reported-documents",
            params={
                "source_type": "stock_replenishment",
                "report_length_mm": 500,
                "report_width_mm": 400,
            },
        )

    assert exact.status_code == swapped.status_code == cross_sibling.status_code == 200
    assert {row["document_number"] for row in exact.json()["items"]} == {
        "SRO-P106-A",
        "SR-P106-A",
        "REQ-P106-A",
    }
    assert {row["document_number"] for row in swapped.json()["items"]} == {
        "SRO-P106-B",
        "SR-P106-B",
        "REQ-P106-B",
    }
    assert cross_sibling.json()["total"] == 0


def test_reported_document_plus_material_and_grouped_match_metadata(
    reported_documents_app,
) -> None:
    with TestClient(reported_documents_app) as client:
        _login(client, "admin", "AdminPass123!")
        material = client.get(
            "/api/requisition/reported-documents",
            params={"material_code": "A+B"},
        )
        grouped = client.get(
            "/api/requisition/reported-documents",
            params={
                "source_type": "stock_replenishment",
                "product_code": "P106-A-ALT",
            },
        )

    assert material.status_code == grouped.status_code == 200
    assert {row["document_number"] for row in material.json()["items"]} == {
        "SRO-P106-A",
        "SR-P106-A",
    }
    assert grouped.json()["total"] == 1
    document = grouped.json()["items"][0]
    assert document["document_number"] == "SR-P106-A"
    assert len(document["line_items"]) == 2
    assert document["matched_line_count"] == 1
    assert grouped.json()["matched_line_count"] == 1
    matched = [line for line in document["line_items"] if line["matched"]]
    assert matched[0]["product_code"] == "P106-A-ALT"
    assert matched[0]["matched_fields"] == ["product_code"]


def test_reported_customer_options_only_include_visible_reported_customers(
    reported_documents_app,
) -> None:
    with TestClient(reported_documents_app) as client:
        _login(client, "sales-a", "SalesPass123!")
        options = client.get("/api/requisition/reported-customer-options")
        searched = client.get(
            "/api/requisition/reported-customer-options", params={"keyword": "P106-A"}
        )

    assert options.status_code == searched.status_code == 200
    assert [row["customer_code"] for row in options.json()] == ["P106-A"]
    assert searched.json()[0]["label"] == "P106-A｜P106 客户 A"


def test_reported_items_use_one_physical_line_as_the_paging_unit(
    reported_documents_app,
) -> None:
    with TestClient(reported_documents_app) as client:
        _login(client, "admin", "AdminPass123!")
        first = client.get(
            "/api/requisition/reported-items", params={"page": 1, "page_size": 3}
        )
        second = client.get(
            "/api/requisition/reported-items", params={"page": 2, "page_size": 3}
        )
        third = client.get(
            "/api/requisition/reported-items", params={"page": 3, "page_size": 3}
        )

    assert first.status_code == second.status_code == third.status_code == 200
    assert first.json()["total"] == second.json()["total"] == third.json()["total"] == 7
    rows = first.json()["items"] + second.json()["items"] + third.json()["items"]
    assert len(rows) == 7
    assert len({row["stable_id"] for row in rows}) == 7
    assert [row["sequence"] for row in rows] == list(range(1, 8))
    stock_a = [
        row
        for row in rows
        if row["document_number"] == "SR-P106-A"
    ]
    assert len(stock_a) == 2
    assert [row["line_order"] for row in stock_a] == [1, 2]


def test_reported_items_filter_scope_and_short_name_are_line_exact(
    reported_documents_app,
) -> None:
    with TestClient(reported_documents_app) as client:
        _login(client, "sales-a", "SalesPass123!")
        scoped = client.get(
            "/api/requisition/reported-items", params={"page_size": 20}
        )
        material = client.get(
            "/api/requisition/reported-items",
            params={"material_code": "A+B", "page_size": 20},
        )
        short_name = client.get(
            "/api/requisition/reported-items",
            params={"keyword": "客户甲", "page_size": 20},
        )
        cross_sibling = client.get(
            "/api/requisition/reported-items",
            params={
                "source_type": "stock_replenishment",
                "report_length_mm": 500,
                "report_width_mm": 400,
            },
        )

    assert scoped.status_code == material.status_code == short_name.status_code == 200
    assert scoped.json()["total"] == 4
    assert all(row["customer_short_name"] == "客户甲" for row in scoped.json()["items"])
    assert {row["document_number"] for row in material.json()["items"]} == {
        "SRO-P106-A",
        "SR-P106-A",
    }
    assert short_name.json()["total"] == 4
    assert cross_sibling.status_code == 200
    assert cross_sibling.json()["total"] == 0


def test_reported_items_sort_all_results_before_paging(
    reported_documents_app,
) -> None:
    with TestClient(reported_documents_app) as client:
        _login(client, "admin", "AdminPass123!")
        ascending = client.get(
            "/api/requisition/reported-items",
            params={
                "sort_by": "report_width_mm",
                "sort_direction": "asc",
                "page": 1,
                "page_size": 4,
            },
        )
        descending = client.get(
            "/api/requisition/reported-items",
            params={
                "sort_by": "report_width_mm",
                "sort_direction": "desc",
                "page": 1,
                "page_size": 4,
            },
        )

    assert ascending.status_code == descending.status_code == 200
    assert [row["report_width_mm"] for row in ascending.json()["items"]] == [
        300,
        300,
        300,
        400,
    ]
    assert [row["report_width_mm"] for row in descending.json()["items"]] == [
        500,
        500,
        500,
        400,
    ]


def test_reported_items_preserve_source_specific_action_and_print_boundaries(
    reported_documents_app,
) -> None:
    with TestClient(reported_documents_app) as client:
        _login(client, "admin", "AdminPass123!")
        response = client.get(
            "/api/requisition/reported-items", params={"page_size": 20}
        )

    assert response.status_code == 200
    supplier = next(
        row for row in response.json()["items"] if row["source_type"] == "supplier_order"
    )
    legacy = next(
        row
        for row in response.json()["items"]
        if row["source_type"] == "legacy_material_requisition"
    )
    assert supplier["can_view_supplier_order"] is True
    assert supplier["can_print_task"] is True
    assert supplier["can_print_label"] is True
    assert supplier["status"] == "active"
    assert supplier["version"] == 1
    assert supplier["can_void_item"] is True
    same_supplier_order = [
        row
        for row in response.json()["items"]
        if row["source_type"] == "supplier_order"
        and row["document_id"] == supplier["document_id"]
        and row["status"] == "active"
    ]
    assert supplier["active_item_count"] == len(same_supplier_order)
    assert legacy["can_view_supplier_order"] is False
    assert legacy["can_print_task"] is False
    assert legacy["can_print_label"] is False
    assert legacy["can_void_item"] is False
