from __future__ import annotations

from collections.abc import Generator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture()
def n028_customer_scope_app(tmp_path: Path):
    from app.api.auth import router as auth_router
    from app.api.customers import router as customers_router
    from app.api.deps import get_db
    from app.api.orders import router as orders_router
    from app.api.products import router as products_router
    from app.api.requisition import router as requisition_router
    from app.api.warehouse import router as warehouse_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.access_control import UserCustomerScope, UserPermissionOverride
    from app.models.customer import Customer
    from app.models.order import Order
    from app.models.product import Product
    from app.models.product_drawing import ProductDrawing
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "n028-customer-scopes.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        admin = User(
            username="n028-admin",
            password_hash=hash_password("AdminPass123!"),
            role="admin",
            real_name="Admin",
            must_change_password=False,
        )
        boss = User(
            username="n028-boss",
            password_hash=hash_password("BossPass123!"),
            role="boss",
            real_name="Boss",
            must_change_password=False,
        )
        sales = User(
            username="n028-sales",
            password_hash=hash_password("SalesPass123!"),
            role="sales",
            real_name="Sales",
            must_change_password=False,
            customer_access_mode="selected",
        )
        empty_sales = User(
            username="n028-empty-sales",
            password_hash=hash_password("EmptySalesPass123!"),
            role="sales",
            real_name="Empty Sales",
            must_change_password=False,
            customer_access_mode="selected",
        )
        customer = Customer(
            customer_number=1,
            customer_code="N028-A",
            name="N028 Customer",
        )
        other_customer = Customer(
            customer_number=2,
            customer_code="N028-B",
            name="N028 Other Customer",
        )
        db.add_all([admin, boss, sales, empty_sales, customer, other_customer])
        db.flush()
        product = Product(
            customer_id=customer.id,
            product_code="N028-BASE",
            customer_material_code="N028-BASE",
            product_name="N028 base carton",
            box_category="normal",
            sale_unit_price=Decimal("8.00"),
            cost_unit_price=Decimal("2.00"),
            board_price=Decimal("3.00"),
            suggested_price=Decimal("4.00"),
        )
        db.add_all(
            [
                product,
                Product(
                    customer_id=other_customer.id,
                    product_code="N028-OTHER",
                    customer_material_code="N028-OTHER",
                    product_name="N028 other carton",
                    box_category="normal",
                ),
                UserCustomerScope(user_id=sales.id, customer_id=customer.id),
                UserPermissionOverride(
                    user_id=sales.id,
                    permission_code="products.create",
                    is_allowed=True,
                    granted_by=admin.id,
                ),
                UserPermissionOverride(
                    user_id=sales.id,
                    permission_code="customers.create",
                    is_allowed=True,
                    granted_by=admin.id,
                ),
                UserPermissionOverride(
                    user_id=sales.id,
                    permission_code="requisition.view",
                    is_allowed=True,
                    granted_by=admin.id,
                ),
                UserPermissionOverride(
                    user_id=sales.id,
                    permission_code="requisition.execute",
                    is_allowed=True,
                    granted_by=admin.id,
                ),
                UserPermissionOverride(
                    user_id=sales.id,
                    permission_code="warehouse.view",
                    is_allowed=True,
                    granted_by=admin.id,
                ),
                UserPermissionOverride(
                    user_id=sales.id,
                    permission_code="warehouse.execute",
                    is_allowed=True,
                    granted_by=admin.id,
                ),
                UserPermissionOverride(
                    user_id=sales.id,
                    permission_code="warehouse.reserve",
                    is_allowed=True,
                    granted_by=admin.id,
                ),
                UserPermissionOverride(
                    user_id=empty_sales.id,
                    permission_code="requisition.view",
                    is_allowed=True,
                    granted_by=admin.id,
                ),
                UserPermissionOverride(
                    user_id=empty_sales.id,
                    permission_code="warehouse.view",
                    is_allowed=True,
                    granted_by=admin.id,
                ),
                Order(
                    order_number="N028-SCOPE-ORDER",
                    customer_id=customer.id,
                    order_date=date(2026, 7, 13),
                    total_amount=Decimal("0"),
                    created_by=admin.id,
                ),
                Order(
                    order_number="N028-OTHER-ORDER",
                    customer_id=other_customer.id,
                    order_date=date(2026, 7, 13),
                    total_amount=Decimal("0"),
                    created_by=admin.id,
                ),
            ]
        )
        db.flush()
        db.add(
            ProductDrawing(
                product_id=product.id,
                image_path="/static/uploads/drawings/legacy.png",
                thumbnail_path="/static/uploads/drawings/legacy.png",
                uploaded_by=admin.id,
            )
        )
        db.commit()
        ids = {
            "customer": customer.id,
            "other_customer": other_customer.id,
            "product": product.id,
        }

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(customers_router, prefix="/api/master/customers")
    app.include_router(products_router, prefix="/api/master/products")
    app.include_router(orders_router, prefix="/api/orders")
    app.include_router(requisition_router, prefix="/api/requisition")
    app.include_router(warehouse_router, prefix="/api/warehouse")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield app, ids, factory
    finally:
        engine.dispose()


def _login(client: TestClient, username: str, password: str) -> None:
    response = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200


def _product_payload(customer_id: int, *, code: str, name: str) -> dict:
    return {
        "customer_id": customer_id,
        "product_code": code,
        "customer_material_code": code,
        "product_name": name,
        "box_category": "normal",
        "sale_unit_price": "9.00",
        "cost_unit_price": "20.00",
        "board_price": "21.00",
        "suggested_price": "22.00",
    }


def _overwrite_order_payload(customer_id: int, product_id: int) -> dict:
    return {
        "customer_id": customer_id,
        "items": [
            {
                "product_id": product_id,
                "quantity": 1,
                "unit_price": "9.00",
                "temp_drawing_file": "/static/uploads/drawings/replacement.png",
                "drawing_save_option": "overwrite_product",
            }
        ],
    }


def test_empty_selected_scope_returns_no_customer_product_or_order_rows(
    n028_customer_scope_app,
) -> None:
    app, ids, _factory = n028_customer_scope_app
    with TestClient(app) as client:
        _login(client, "n028-empty-sales", "EmptySalesPass123!")
        customers = client.get("/api/master/customers")
        products = client.get("/api/master/products")
        orders = client.get("/api/orders")
        pending = client.get("/api/requisition/pending")
        documents = client.get("/api/requisition/reported-documents")
        insights = client.get("/api/warehouse/insights")
        directed_product_list = client.get(
            "/api/master/products", params={"customer_id": ids["customer"]}
        )

    assert customers.json()["items"] == []
    assert products.json()["items"] == []
    assert orders.json()["items"] == []
    assert pending.json()["items"] == []
    assert documents.json()["items"] == []
    assert insights.json()["summary"]["recorded_lots"] == 0
    assert insights.json()["action_items"] == []
    assert directed_product_list.status_code == 403

    with TestClient(app) as client:
        _login(client, "n028-sales", "SalesPass123!")
        selected_customers = client.get("/api/master/customers")
        selected_products = client.get("/api/master/products")
        selected_orders = client.get("/api/orders")
        assert client.get(
            "/api/master/products", params={"customer_id": ids["other_customer"]}
        ).status_code == 403
        client.post("/api/auth/logout")

        _login(client, "n028-admin", "AdminPass123!")
        all_customers = client.get("/api/master/customers")
        all_products = client.get("/api/master/products")
        all_orders = client.get("/api/orders")

    assert [item["id"] for item in selected_customers.json()["items"]] == [
        ids["customer"]
    ]
    assert [item["customer_id"] for item in selected_products.json()["items"]] == [
        ids["customer"]
    ]
    assert [item["customer_id"] for item in selected_orders.json()["items"]] == [
        ids["customer"]
    ]
    assert {item["id"] for item in all_customers.json()["items"]} == {
        ids["customer"],
        ids["other_customer"],
    }
    assert {item["customer_id"] for item in all_products.json()["items"]} == {
        ids["customer"],
        ids["other_customer"],
    }
    assert {item["customer_id"] for item in all_orders.json()["items"]} == {
        ids["customer"],
        ids["other_customer"],
    }


def test_sales_created_customer_is_scoped_in_the_same_transaction(
    n028_customer_scope_app,
) -> None:
    app, _ids, factory = n028_customer_scope_app
    with TestClient(app) as client:
        _login(client, "n028-sales", "SalesPass123!")
        response = client.post(
            "/api/master/customers",
            json={
                "customer_number": 3,
                "customer_code": "N028-NEW",
                "name": "N028 newly assigned customer",
            },
        )
        assert response.status_code == 201, response.text
        created_id = response.json()["id"]
        assert created_id in [item["id"] for item in client.get("/api/master/customers").json()["items"]]

    from app.models.access_control import UserCustomerScope
    from app.models.user import User

    with factory() as db:
        sales = db.scalar(select(User).where(User.username == "n028-sales"))
        assert sales is not None
        assert db.scalar(
            select(UserCustomerScope.id).where(
                UserCustomerScope.user_id == sales.id,
                UserCustomerScope.customer_id == created_id,
            )
        ) is not None


def test_customer_create_rollback_does_not_leave_customer_or_scope(
    n028_customer_scope_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api import customers as customers_api
    from app.models.access_control import UserCustomerScope
    from app.models.customer import Customer

    app, _ids, factory = n028_customer_scope_app

    def fail_after_scope(*_args, **_kwargs):
        raise RuntimeError("forced customer create rollback")

    monkeypatch.setattr(customers_api, "record_versioned_create", fail_after_scope)
    with TestClient(app, raise_server_exceptions=False) as client:
        _login(client, "n028-sales", "SalesPass123!")
        response = client.post(
            "/api/master/customers",
            json={
                "customer_number": 30,
                "customer_code": "N028-ROLLBACK",
                "name": "N028 rollback customer",
            },
        )
        assert response.status_code == 500

    with factory() as db:
        assert db.scalar(
            select(Customer.id).where(Customer.customer_code == "N028-ROLLBACK")
        ) is None
        assert db.scalar(select(func.count()).select_from(UserCustomerScope)) == 1


def test_admin_and_boss_customer_create_does_not_add_scope(
    n028_customer_scope_app,
) -> None:
    from app.models.access_control import UserCustomerScope

    app, _ids, factory = n028_customer_scope_app
    created_ids: list[int] = []
    with TestClient(app) as client:
        for username, password, number, code in (
            ("n028-admin", "AdminPass123!", 31, "N028-ADMIN-CUSTOMER"),
            ("n028-boss", "BossPass123!", 32, "N028-BOSS-CUSTOMER"),
        ):
            _login(client, username, password)
            response = client.post(
                "/api/master/customers",
                json={
                    "customer_number": number,
                    "customer_code": code,
                    "name": f"{code} name",
                },
            )
            assert response.status_code == 201, response.text
            created_ids.append(response.json()["id"])
            client.post("/api/auth/logout")

    with factory() as db:
        assert db.scalar(
            select(func.count()).select_from(UserCustomerScope).where(
                UserCustomerScope.customer_id.in_(created_ids)
            )
        ) == 0


def test_requisition_supplier_orders_and_documents_fail_closed_by_customer_scope(
    n028_customer_scope_app,
) -> None:
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.supplier_requisition_order import (
        SupplierRequisitionOrder,
        SupplierRequisitionOrderItem,
    )

    app, ids, factory = n028_customer_scope_app
    with factory() as db:
        orders = {row.customer_id: row for row in db.scalars(select(Order)).all()}
        products = {
            row.customer_id: row for row in db.scalars(select(Product)).all()
        }
        source_items = {}
        supplier_orders = {}
        for customer_id, label in ((ids["customer"], "A"), (ids["other_customer"], "B")):
            item = OrderItem(
                order_id=orders[customer_id].id,
                product_id=products[customer_id].id,
                quantity=10,
                unit_price=Decimal("1.00"),
                subtotal=Decimal("10.00"),
                material_status="pending",
                requisition_status="未报料",
                snapshot_product_name=f"N028 scope carton {label}",
            )
            db.add(item)
            db.flush()
            source_items[label] = item
            supplier = SupplierRequisitionOrder(
                order_number=f"N028-SRO-{label}",
                total_quantity=10,
                requisition_qty=10,
                stock_deduction_qty=0,
            )
            db.add(supplier)
            db.flush()
            supplier.items.append(
                SupplierRequisitionOrderItem(
                    order_item_id=item.id,
                    quantity=10,
                    requisition_qty=10,
                    stock_deduction_qty=0,
                    customer_name=f"Customer {label}",
                )
            )
            supplier_orders[label] = supplier.id
        db.commit()

    with TestClient(app) as client:
        _login(client, "n028-sales", "SalesPass123!")
        pending = client.get("/api/requisition/pending")
        listed = client.get("/api/requisition/supplier-orders")
        documents = client.get("/api/requisition/reported-documents")
        assert pending.status_code == listed.status_code == documents.status_code == 200
        assert "scope carton B" not in pending.text
        assert "N028-SRO-B" not in listed.text
        assert "Customer B" not in documents.text
        assert client.get(f"/api/requisition/supplier-orders/{supplier_orders['B']}").status_code == 403
        assert client.put(f"/api/requisition/supplier-orders/{supplier_orders['B']}/void").status_code == 403
        assert client.get(f"/api/requisition/supplier-orders/{supplier_orders['A']}").status_code == 200


def test_warehouse_insights_and_direct_lot_access_are_customer_scoped(
    n028_customer_scope_app,
) -> None:
    from app.models.product import Product
    from app.models.warehouse_inventory import WarehouseLocation
    from app.services.warehouse_inventory import manual_finished_in

    app, ids, factory = n028_customer_scope_app
    with factory() as db:
        products = {
            row.customer_id: row for row in db.scalars(select(Product)).all()
        }
        location = WarehouseLocation(
            location_code="N028-INSIGHT",
            location_name="N028 insight location",
            warehouse_type="finished",
        )
        db.add(location)
        db.flush()
        lots = {}
        for customer_id, label in ((ids["customer"], "A"), (ids["other_customer"], "B")):
            lots[label] = manual_finished_in(
                db,
                customer_id=customer_id,
                product_id=products[customer_id].id,
                location_id=location.id,
                quantity=10,
                stock_date=date.today(),
                source_type="manual",
                remarks=f"Customer {label} inventory",
                operator_id=None,
                idempotency_key=f"n028-insight-{label}",
            ).id
        db.commit()

    with TestClient(app) as client:
        _login(client, "n028-sales", "SalesPass123!")
        scoped = client.get("/api/warehouse/insights")
        assert scoped.status_code == 200
        assert scoped.json()["summary"]["recorded_lots"] == 1
        assert "Customer B" not in scoped.text
        assert client.get(f"/api/warehouse/lots/{lots['B']}").status_code == 403
        client.post("/api/auth/logout")

        _login(client, "n028-boss", "BossPass123!")
        assert client.get("/api/warehouse/insights").json()["summary"]["recorded_lots"] == 2


def test_cross_customer_finished_lot_binding_fails_closed_for_scoped_users(
    n028_customer_scope_app,
) -> None:
    from app.models.customer import Customer
    from app.models.product import Product
    from app.models.warehouse_inventory import (
        FinishedGoodsInventoryDetail,
        InventoryLot,
        WarehouseLocation,
    )

    app, ids, factory = n028_customer_scope_app
    with factory() as db:
        customer_a = db.get(Customer, ids["customer"])
        product_b = db.scalar(
            select(Product).where(Product.customer_id == ids["other_customer"])
        )
        assert customer_a is not None and product_b is not None
        product_b.cost_unit_price = Decimal("9876.54")
        location = WarehouseLocation(
            location_code="N028-CROSS-FG",
            location_name="N028 cross customer finished",
            warehouse_type="finished",
        )
        db.add(location)
        db.flush()
        lot = InventoryLot(
            lot_number="N028-CROSS-FINISHED-LOT",
            inventory_type="finished",
            warehouse_location_id=location.id,
            quantity_available=3,
            quantity_reserved=0,
            quantity_consumed=0,
            quantity_damaged=0,
            quantity_scrapped=0,
            unit="boxes",
            status="active",
            source_type="manual",
            stock_date=date(2026, 7, 17),
            last_movement_at=datetime(2026, 7, 17, 8, 0, 0),
        )
        lot.finished_detail = FinishedGoodsInventoryDetail(
            owner_customer_id=customer_a.id,
            owner_customer_name_snapshot=customer_a.name,
            is_general=False,
            product_id=product_b.id,
            inventory_code_snapshot=product_b.product_code,
            product_name_snapshot=product_b.product_name,
        )
        db.add(lot)
        db.commit()
        lot_id = lot.id
        product_b_code = product_b.product_code
        product_b_name = product_b.product_name

    with TestClient(app) as client:
        _login(client, "n028-sales", "SalesPass123!")
        listed = client.get("/api/warehouse/lots")
        insights = client.get("/api/warehouse/insights")
        assert listed.status_code == insights.status_code == 200
        assert listed.json()["total"] == 0
        assert insights.json()["summary"]["recorded_lots"] == 0
        assert insights.json()["action_items"] == []
        assert client.get(f"/api/warehouse/lots/{lot_id}").status_code == 403
        for secret in (product_b_code, product_b_name, "9876.54"):
            assert secret not in listed.text
            assert secret not in insights.text
        client.post("/api/auth/logout")

        for username, password in (
            ("n028-boss", "BossPass123!"),
            ("n028-admin", "AdminPass123!"),
        ):
            _login(client, username, password)
            unrestricted_lots = client.get("/api/warehouse/lots")
            unrestricted_insights = client.get("/api/warehouse/insights")
            assert unrestricted_lots.json()["total"] == 1
            assert unrestricted_insights.json()["summary"]["recorded_lots"] == 1
            assert product_b_code in unrestricted_lots.text
            assert product_b_name in unrestricted_insights.text
            assert "9876.54" in unrestricted_insights.text
            client.post("/api/auth/logout")


def test_cross_customer_finished_pallet_binding_blocks_scoped_floor3_access(
    n028_customer_scope_app,
) -> None:
    from app.models.customer import Customer
    from app.models.product import Product
    from app.models.warehouse_inventory import (
        FinishedGoodsInventoryDetail,
        InventoryLot,
        InventoryPallet,
        InventoryPalletItem,
        WarehouseLocation,
    )

    app, ids, factory = n028_customer_scope_app
    with factory() as db:
        customer_a = db.get(Customer, ids["customer"])
        product_b = db.scalar(
            select(Product).where(Product.customer_id == ids["other_customer"])
        )
        assert customer_a is not None and product_b is not None

        source = WarehouseLocation(
            location_code="N028-CROSS-PALLET-SOURCE",
            location_name="N028 cross pallet source",
            warehouse_type="finished",
            warehouse_floor=3,
            area_code="A1",
            storage_type="ground",
            sort_order=1,
            source_version="V11",
        )
        target = WarehouseLocation(
            location_code="N028-CROSS-PALLET-TARGET",
            location_name="N028 cross pallet target",
            warehouse_type="finished",
            warehouse_floor=3,
            area_code="A1",
            storage_type="ground",
            sort_order=2,
            source_version="V11",
        )
        db.add_all([source, target])
        db.flush()

        lot = InventoryLot(
            lot_number="N028-CROSS-PALLET-LOT",
            inventory_type="finished",
            warehouse_location_id=source.id,
            quantity_available=3,
            quantity_reserved=0,
            quantity_consumed=0,
            quantity_damaged=0,
            quantity_scrapped=0,
            unit="boxes",
            status="active",
            source_type="manual",
            stock_date=date(2026, 7, 17),
            last_movement_at=datetime(2026, 7, 17, 8, 0, 0),
        )
        lot.finished_detail = FinishedGoodsInventoryDetail(
            owner_customer_id=customer_a.id,
            owner_customer_name_snapshot=customer_a.name,
            is_general=False,
            product_id=product_b.id,
            inventory_code_snapshot=product_b.product_code,
            product_name_snapshot=product_b.product_name,
        )
        pallet = InventoryPallet(
            pallet_code="N028-CROSS-PALLET",
            location_id=source.id,
            status="active",
            is_current=True,
            version=1,
        )
        pallet.items.append(
            InventoryPalletItem(
                inventory_lot=lot,
                customer_id=customer_a.id,
                product_id=product_b.id,
                inventory_code=product_b.product_code,
                product_name=product_b.product_name,
                item_type="finished",
                quantity=Decimal("3"),
                unit="boxes",
                match_status="matched",
            )
        )
        db.add_all([lot, pallet])
        db.commit()
        pallet_id = pallet.id
        source_id = source.id
        target_id = target.id
        product_b_code = product_b.product_code
        product_b_name = product_b.product_name

    with TestClient(app) as client:
        _login(client, "n028-sales", "SalesPass123!")
        listing = client.get("/api/warehouse/floor3/locations")
        assert listing.status_code == 200
        source_payload = next(
            row for row in listing.json()["items"] if row["id"] == source_id
        )
        assert source_payload["current_pallet"] == {"access_restricted": True}
        assert product_b_code not in listing.text
        assert product_b_name not in listing.text
        blocked_move = client.post(
            f"/api/warehouse/pallets/{pallet_id}/move",
            json={
                "expected_version": 1,
                "to_location_id": target_id,
                "confirmed": True,
                "idempotency_key": "n028-cross-pallet-scoped-move",
            },
        )
        assert blocked_move.status_code == 403
        client.post("/api/auth/logout")

        _login(client, "n028-admin", "AdminPass123!")
        admin_listing = client.get("/api/warehouse/floor3/locations")
        assert product_b_code in admin_listing.text
        assert product_b_name in admin_listing.text
        allowed_move = client.post(
            f"/api/warehouse/pallets/{pallet_id}/move",
            json={
                "expected_version": 1,
                "to_location_id": target_id,
                "confirmed": True,
                "idempotency_key": "n028-cross-pallet-admin-move",
            },
        )
        assert allowed_move.status_code == 200
        client.post("/api/auth/logout")

        _login(client, "n028-boss", "BossPass123!")
        boss_listing = client.get("/api/warehouse/floor3/locations")
        assert product_b_code in boss_listing.text
        assert product_b_name in boss_listing.text

    with factory() as db:
        moved = db.get(InventoryPallet, pallet_id)
        assert moved is not None
        assert moved.location_id == target_id
        assert moved.version == 2


def test_scoped_supplier_order_creation_requires_an_authorized_order_item(
    n028_customer_scope_app,
) -> None:
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.supplier_requisition_order import SupplierRequisitionOrder

    app, ids, factory = n028_customer_scope_app
    with factory() as db:
        order_b = db.scalar(
            select(Order).where(Order.customer_id == ids["other_customer"])
        )
        product_b = db.scalar(
            select(Product).where(Product.customer_id == ids["other_customer"])
        )
        assert order_b is not None and product_b is not None
        item_b = OrderItem(
            order_id=order_b.id,
            product_id=product_b.id,
            quantity=2,
            unit_price=Decimal("1.00"),
            subtotal=Decimal("2.00"),
            snapshot_product_code="N028-MANUAL-B-SECRET",
            snapshot_product_name="N028 manual B secret",
        )
        db.add(item_b)
        db.commit()
        item_b_id = item_b.id

    manual_payload = {
        "supplier_name": "N028 manual supplier",
        "members": [
            {
                "quantity": 1,
                "product_code": "N028-MANUAL-NULL",
                "product_name": "N028 manual unlinked secret",
                "customer_name": "N028 manual customer secret",
            }
        ],
    }
    with TestClient(app) as client:
        _login(client, "n028-sales", "SalesPass123!")
        assert client.post(
            "/api/requisition/supplier-orders", json=manual_payload
        ).status_code == 403
        assert client.post(
            "/api/requisition/supplier-orders",
            json={
                "members": [
                    {
                        "item_id": item_b_id,
                        "quantity": 2,
                        "product_name": "N028 manual B secret",
                    }
                ]
            },
        ).status_code == 403
        client.post("/api/auth/logout")

        for username, password in (
            ("n028-admin", "AdminPass123!"),
            ("n028-boss", "BossPass123!"),
        ):
            _login(client, username, password)
            created = client.post(
                "/api/requisition/supplier-orders", json=manual_payload
            )
            assert created.status_code == 201, created.text
            client.post("/api/auth/logout")

        _login(client, "n028-sales", "SalesPass123!")
        assert "manual unlinked secret" not in client.get(
            "/api/requisition/supplier-orders"
        ).text
        assert "manual customer secret" not in client.get(
            "/api/requisition/reported-documents"
        ).text

    with factory() as db:
        assert db.scalar(select(func.count()).select_from(SupplierRequisitionOrder)) == 2


def test_stock_replenishment_full_chain_is_customer_scoped(
    n028_customer_scope_app,
) -> None:
    from app.models.product import Product
    from app.models.stock_replenishment import StockReplenishmentOrder
    from app.models.warehouse_inventory import WarehouseLocation

    app, ids, factory = n028_customer_scope_app
    with factory() as db:
        products = {
            row.customer_id: row for row in db.scalars(select(Product)).all()
        }
        finished_location = WarehouseLocation(
            location_code="N028-STOCK-FG",
            location_name="N028 stock finished",
            warehouse_type="finished",
        )
        semi_location = WarehouseLocation(
            location_code="N028-STOCK-SI",
            location_name="N028 stock semi",
            warehouse_type="semi_finished",
        )
        db.add_all([finished_location, semi_location])
        db.commit()
        product_ids = {
            customer_id: product.id for customer_id, product in products.items()
        }
        finished_location_id = finished_location.id
        semi_location_id = semi_location.id

    def policy_payload(customer_id: int, label: str) -> dict:
        return {
            "policy_name": f"N028 policy {label}",
            "target_inventory_type": "finished",
            "product_id": product_ids[customer_id],
            "warning_quantity": 0,
            "target_quantity": 5,
            "default_location_id": finished_location_id,
        }

    def finished_item(customer_id: int, quantity: int = 2) -> dict:
        return {
            "target_inventory_type": "finished",
            "product_id": product_ids[customer_id],
            "quantity": quantity,
            "location_id": finished_location_id,
        }

    with TestClient(app) as client:
        _login(client, "n028-admin", "AdminPass123!")
        policy_a = client.post(
            "/api/requisition/stock-policies",
            json=policy_payload(ids["customer"], "A"),
        )
        policy_b = client.post(
            "/api/requisition/stock-policies",
            json=policy_payload(ids["other_customer"], "B-SECRET"),
        )
        assert policy_a.status_code == policy_b.status_code == 201

        order_payloads = {
            "A": {
                "source_type": "customer_request",
                "customer_id": ids["customer"],
                "stock_now": False,
                "items": [finished_item(ids["customer"])],
            },
            "B": {
                "source_type": "customer_request",
                "customer_id": ids["other_customer"],
                "stock_now": False,
                "items": [finished_item(ids["other_customer"])],
            },
            "MIXED": {
                "source_type": "customer_request",
                "stock_now": False,
                "items": [
                    finished_item(ids["customer"]),
                    finished_item(ids["other_customer"]),
                ],
            },
            "UNLINKED": {
                "source_type": "manual_history",
                "stock_now": False,
                "items": [
                    {
                        "target_inventory_type": "semi_finished",
                        "product_name": "N028 GLOBAL UNLINKED SECRET",
                        "material_code": "A416D",
                        "layer_count": 5,
                        "flute_type": "AB",
                        "report_length_mm": 1000,
                        "report_width_mm": 800,
                        "quantity": 1,
                        "location_id": semi_location_id,
                    }
                ],
            },
        }
        order_ids = {}
        for label, payload in order_payloads.items():
            response = client.post(
                "/api/requisition/stock-replenishment/orders", json=payload
            )
            assert response.status_code == 201, response.text
            order_ids[label] = response.json()["id"]
        client.post("/api/auth/logout")

        _login(client, "n028-sales", "SalesPass123!")
        products_response = client.get(
            "/api/requisition/stock-replenishment/products"
        )
        assert products_response.status_code == 200
        assert "N028-OTHER" not in products_response.text
        assert client.get(
            "/api/requisition/stock-replenishment/products",
            params={"customer_id": ids["other_customer"]},
        ).status_code == 403

        policies = client.get("/api/requisition/stock-policies")
        assert policies.status_code == 200
        assert {row["id"] for row in policies.json()["items"]} == {
            policy_a.json()["id"]
        }
        assert client.get(
            f"/api/requisition/stock-policies/{policy_b.json()['id']}/replenishment-draft"
        ).status_code == 403
        assert client.get(
            f"/api/requisition/stock-policies/{policy_a.json()['id']}/replenishment-draft"
        ).status_code == 200
        assert client.post(
            "/api/requisition/stock-policies",
            json=policy_payload(ids["other_customer"], "B-DENIED"),
        ).status_code == 403
        assert client.put(
            f"/api/requisition/stock-policies/{policy_b.json()['id']}",
            json=policy_payload(ids["other_customer"], "B-UPDATE-DENIED"),
        ).status_code == 403

        listed = client.get("/api/requisition/stock-replenishment/orders")
        documents = client.get("/api/requisition/reported-documents")
        assert {row["id"] for row in listed.json()["items"]} == {order_ids["A"]}
        assert "N028-OTHER" not in listed.text
        assert "GLOBAL UNLINKED SECRET" not in documents.text
        assert client.get(
            f"/api/requisition/stock-replenishment/orders/{order_ids['A']}"
        ).status_code == 200
        assert client.get(
            f"/api/requisition/stock-replenishment/orders/{order_ids['A']}/print"
        ).status_code == 200
        for label in ("B", "MIXED", "UNLINKED"):
            assert client.get(
                f"/api/requisition/stock-replenishment/orders/{order_ids[label]}"
            ).status_code == 403
            assert client.get(
                f"/api/requisition/stock-replenishment/orders/{order_ids[label]}/print"
            ).status_code == 403
            assert client.post(
                f"/api/requisition/stock-replenishment/orders/{order_ids[label]}/stock"
            ).status_code == 403
        stocked_a = client.post(
            f"/api/requisition/stock-replenishment/orders/{order_ids['A']}/stock"
        )
        assert stocked_a.status_code == 200, stocked_a.text
        assert stocked_a.json()["status"] == "stocked"
        assert client.post(
            "/api/requisition/stock-replenishment/orders",
            json=order_payloads["B"],
        ).status_code == 403
        assert client.post(
            "/api/requisition/stock-replenishment/orders",
            json=order_payloads["MIXED"],
        ).status_code == 403
        assert client.post(
            "/api/requisition/stock-replenishment/orders",
            json=order_payloads["UNLINKED"],
        ).status_code == 403
        top_level_mismatch = {
            **order_payloads["A"],
            "customer_id": ids["other_customer"],
        }
        assert client.post(
            "/api/requisition/stock-replenishment/orders",
            json=top_level_mismatch,
        ).status_code == 403
        client.post("/api/auth/logout")

        _login(client, "n028-boss", "BossPass123!")
        boss_rows = client.get(
            "/api/requisition/stock-replenishment/orders"
        ).json()["items"]
        assert {row["id"] for row in boss_rows} == set(order_ids.values())
        assert client.get(
            f"/api/requisition/stock-replenishment/orders/{order_ids['B']}/print"
        ).status_code == 200

    with factory() as db:
        assert db.scalar(
            select(func.count()).select_from(StockReplenishmentOrder)
        ) == 4


def test_null_owner_inventory_and_cross_customer_semi_binding_fail_closed(
    n028_customer_scope_app,
) -> None:
    from app.models.product import Product
    from app.models.warehouse_inventory import (
        SemiFinishedLotAllowedProduct,
        WarehouseLocation,
    )
    from app.services.warehouse_inventory import manual_semi_finished_in

    app, ids, factory = n028_customer_scope_app
    with factory() as db:
        product_b = db.scalar(
            select(Product).where(Product.customer_id == ids["other_customer"])
        )
        assert product_b is not None
        location = WarehouseLocation(
            location_code="N028-NULL-SI",
            location_name="N028 null owner semi",
            warehouse_type="semi_finished",
        )
        db.add(location)
        db.flush()

        def add_lot(customer_id: int | None, key: str, remarks: str):
            return manual_semi_finished_in(
                db,
                location_id=location.id,
                quantity=5,
                stock_date=date.today(),
                source_type="manual",
                material_code="A416D",
                layer_count=5,
                flute_type="AB",
                board_length_mm=1000,
                board_width_mm=800,
                sheet_type="raw_board",
                supplier_name=None,
                customer_id=customer_id,
                crease_type=None,
                crease_left_mm=None,
                crease_middle_mm=None,
                crease_right_mm=None,
                cutting_note=None,
                remarks=remarks,
                operator_id=None,
                idempotency_key=key,
            )

        general_lot = add_lot(None, "n028-null-general", "GENERAL-NULL-SECRET")
        customer_lot = add_lot(
            ids["customer"], "n028-null-customer-a", "CUSTOMER-A-LOT"
        )
        db.add(
            SemiFinishedLotAllowedProduct(
                inventory_lot_id=customer_lot.id,
                product_id=product_b.id,
                confirmed_at=datetime(2026, 7, 17, 8, 0, 0),
            )
        )
        db.commit()
        general_lot_id = general_lot.id
        customer_lot_id = customer_lot.id
        product_b_code = product_b.product_code
        product_b_name = product_b.product_name

    with TestClient(app) as client:
        _login(client, "n028-sales", "SalesPass123!")
        listed = client.get("/api/warehouse/lots")
        insights = client.get("/api/warehouse/insights")
        assert listed.status_code == insights.status_code == 200
        assert listed.json()["total"] == 1
        assert "GENERAL-NULL-SECRET" not in listed.text
        assert client.get(
            f"/api/warehouse/lots/{general_lot_id}"
        ).status_code == 403
        assert client.post(
            f"/api/warehouse/lots/{general_lot_id}/freeze",
            json={"expected_version": 1, "reason": "scope test"},
        ).status_code == 403
        assert insights.json()["summary"]["recorded_lots"] == 1
        action = next(
            row
            for row in insights.json()["action_items"]
            if row["lot_id"] == customer_lot_id
        )
        assert action["detail"]["assigned_product_count"] == 0
        assert product_b_code not in insights.text
        assert product_b_name not in insights.text
        client.post("/api/auth/logout")

        _login(client, "n028-boss", "BossPass123!")
        assert client.get("/api/warehouse/lots").json()["total"] == 2
        assert client.get(f"/api/warehouse/lots/{general_lot_id}").status_code == 200
        boss_insights = client.get("/api/warehouse/insights")
        assert boss_insights.json()["summary"]["recorded_lots"] == 2
        assert product_b_code in boss_insights.text
        client.post("/api/auth/logout")

        _login(client, "n028-admin", "AdminPass123!")
        assert client.post(
            f"/api/warehouse/lots/{general_lot_id}/freeze",
            json={"expected_version": 1, "reason": "admin scope test"},
        ).status_code == 200


def test_scoped_inventory_insights_filter_before_global_action_limit(
    n028_customer_scope_app,
) -> None:
    from app.models.product import Product
    from app.models.warehouse_inventory import (
        FinishedGoodsInventoryDetail,
        InventoryLot,
        WarehouseLocation,
    )

    app, ids, factory = n028_customer_scope_app
    with factory() as db:
        products = {
            row.customer_id: row for row in db.scalars(select(Product)).all()
        }
        location = WarehouseLocation(
            location_code="N028-LIMIT-FG",
            location_name="N028 action limit",
            warehouse_type="finished",
        )
        db.add(location)
        db.flush()

        def add_finished_row(sequence: int, customer_id: int, marker: str) -> None:
            product = products[customer_id]
            lot = InventoryLot(
                lot_number=f"N028-LIMIT-{sequence:04d}",
                inventory_type="finished",
                warehouse_location_id=location.id,
                quantity_available=1,
                quantity_reserved=0,
                quantity_consumed=0,
                quantity_damaged=0,
                quantity_scrapped=0,
                unit="boxes",
                status="active",
                source_type="manual",
                stock_date=date(2025, 1, 1),
                last_movement_at=datetime(2025, 1, 1, 8, 0, 0),
                estimated_unit_cost_snapshot=Decimal("1"),
                estimated_square_price_snapshot=Decimal("1"),
                estimated_cost_area_m2_snapshot=Decimal("1"),
                cost_snapshot_source="n028-test",
            )
            lot.finished_detail = FinishedGoodsInventoryDetail(
                owner_customer_id=customer_id,
                owner_customer_name_snapshot=marker,
                is_general=False,
                product_id=product.id,
                inventory_code_snapshot=product.product_code,
                product_name_snapshot=marker,
            )
            db.add(lot)

        for index in range(201):
            add_finished_row(
                index + 1,
                ids["other_customer"],
                f"N028 B GLOBAL ACTION {index:03d}",
            )
        add_finished_row(9999, ids["customer"], "N028 A AUTHORIZED ACTION")
        db.commit()

    with TestClient(app) as client:
        _login(client, "n028-sales", "SalesPass123!")
        scoped = client.get("/api/warehouse/insights")
        assert scoped.status_code == 200
        assert scoped.json()["summary"]["recorded_lots"] == 1
        assert scoped.json()["action_item_count"] == 1
        assert len(scoped.json()["action_items"]) == 1
        assert "N028 A AUTHORIZED ACTION" in scoped.text
        assert "N028 B GLOBAL ACTION" not in scoped.text
        client.post("/api/auth/logout")

        _login(client, "n028-boss", "BossPass123!")
        unrestricted = client.get("/api/warehouse/insights")
        assert unrestricted.json()["summary"]["recorded_lots"] == 202
        assert unrestricted.json()["action_item_count"] == 202
        assert len(unrestricted.json()["action_items"]) == 200


def test_historical_purchase_scope_is_applied_before_group_limit(
    n028_customer_scope_app,
) -> None:
    from app.models.historical_purchase import HistoricalPurchaseEntry
    from app.models.product import Product
    from app.services.historical_purchase_lookup import normalize_lookup_text

    app, ids, factory = n028_customer_scope_app
    with factory() as db:
        products = {
            row.customer_id: row for row in db.scalars(select(Product)).all()
        }

        def history_row(
            source_row: int,
            customer_id: int | None,
            label: str,
        ) -> HistoricalPurchaseEntry:
            text = f"N028-HISTORY {label}"
            return HistoricalPurchaseEntry(
                source_workbook="n028-history.xlsx",
                source_sheet="history",
                source_row=source_row,
                source_file_sha256=f"{source_row:064x}"[-64:],
                source_fingerprint=f"{source_row + 1000:064x}"[-64:],
                record_date=date(2026, 7, 17),
                product_reference=text,
                search_text=text,
                normalized_search_text=normalize_lookup_text(text),
                material_code="A416D",
                historical_quantity=1,
                report_length_mm=1000,
                report_width_mm=800,
                product_id=(
                    products[customer_id].id if customer_id is not None else None
                ),
                customer_id=customer_id,
            )

        db.add_all(
            [
                history_row(
                    100 + index,
                    ids["other_customer"],
                    f"B-SECRET-{index:02d}",
                )
                for index in range(25)
            ]
            + [
                history_row(2, None, "NULL-SECRET"),
                history_row(1, ids["customer"], "A-AUTHORIZED"),
            ]
        )
        db.commit()

    with TestClient(app) as client:
        _login(client, "n028-sales", "SalesPass123!")
        scoped = client.get(
            "/api/requisition/historical-purchases/search",
            params={"q": "N028-HISTORY", "limit": 20},
        )
        assert scoped.status_code == 200
        assert scoped.json()["total_matches"] == 1
        assert scoped.json()["source_record_matches"] == 1
        assert scoped.json()["items"][0]["customer_id"] == ids["customer"]
        assert "A-AUTHORIZED" in scoped.text
        assert "B-SECRET" not in scoped.text
        assert "NULL-SECRET" not in scoped.text
        client.post("/api/auth/logout")

        _login(client, "n028-empty-sales", "EmptySalesPass123!")
        empty = client.get(
            "/api/requisition/historical-purchases/search",
            params={"q": "N028-HISTORY"},
        )
        assert empty.json()["items"] == []
        assert empty.json()["indexed_records"] == 0
        client.post("/api/auth/logout")

        _login(client, "n028-admin", "AdminPass123!")
        unrestricted = client.get(
            "/api/requisition/historical-purchases/search",
            params={"q": "N028-HISTORY", "limit": 100},
        )
        assert unrestricted.json()["total_matches"] == 27


def test_sales_cost_payload_is_ignored_while_admin_and_boss_can_write_costs(
    n028_customer_scope_app,
) -> None:
    from app.models.product import Product

    app, ids, factory = n028_customer_scope_app
    with TestClient(app) as client:
        _login(client, "n028-sales", "SalesPass123!")
        created = client.post(
            "/api/master/products",
            json=_product_payload(ids["customer"], code="N028-SALES", name="Sales carton"),
        )
        assert created.status_code == 201
        updated = client.put(
            f"/api/master/products/{ids['product']}",
            json={
                **_product_payload(ids["customer"], code="N028-BASE", name="Renamed carton"),
                "expected_version": 1,
                "change_reason": "业务员更新常用箱",
            },
        )
        assert updated.status_code == 200
        synced = client.post(
            f"/api/master/products/{ids['product']}/sync-fields",
            json={
                "fields": {
                    "product_name": "Synced carton",
                    "cost_unit_price": "99.00",
                    "board_price": "98.00",
                    "suggested_price": "97.00",
                },
                "expected_version": 2,
                "change_reason": "业务员同步常用箱",
            },
        )
        assert synced.status_code == 200
        client.post("/api/auth/logout")

        _login(client, "n028-admin", "AdminPass123!")
        admin_created = client.post(
            "/api/master/products",
            json=_product_payload(ids["customer"], code="N028-ADMIN", name="Admin carton"),
        )
        assert admin_created.status_code == 201
        client.post("/api/auth/logout")

        _login(client, "n028-boss", "BossPass123!")
        boss_payload = _product_payload(
            ids["customer"], code="N028-ADMIN", name="Boss edited carton"
        )
        boss_payload["cost_unit_price"] = "30.00"
        boss_updated = client.put(
            f"/api/master/products/{admin_created.json()['id']}",
            json={**boss_payload, "expected_version": 1, "change_reason": "老板更新常用箱"},
        )
        assert boss_updated.status_code == 200

    with factory() as db:
        sales_created = db.scalar(
            select(Product).where(Product.product_code == "N028-SALES")
        )
        original = db.get(Product, ids["product"])
        admin_product = db.get(Product, admin_created.json()["id"])
        assert sales_created is not None
        assert (
            sales_created.cost_unit_price,
            sales_created.board_price,
            sales_created.suggested_price,
        ) == (None, None, None)
        assert sales_created.sale_unit_price == Decimal("9.00")
        assert (
            original.cost_unit_price,
            original.board_price,
            original.suggested_price,
            original.product_name,
        ) == (
            Decimal("2.00"),
            Decimal("3.00"),
            Decimal("4.00"),
            "Synced carton",
        )
        assert (
            admin_product.cost_unit_price,
            admin_product.board_price,
            admin_product.suggested_price,
        ) == (Decimal("30.00"), Decimal("21.00"), Decimal("22.00"))


def test_sales_cannot_overwrite_product_drawings_but_admin_can(
    n028_customer_scope_app,
) -> None:
    from app.models.product_drawing import ProductDrawing

    app, ids, factory = n028_customer_scope_app
    with TestClient(app) as client:
        _login(client, "n028-sales", "SalesPass123!")
        denied = client.post(
            "/api/orders", json=_overwrite_order_payload(ids["customer"], ids["product"])
        )
        assert denied.status_code == 403
        client.post("/api/auth/logout")

        _login(client, "n028-admin", "AdminPass123!")
        allowed = client.post(
            "/api/orders", json=_overwrite_order_payload(ids["customer"], ids["product"])
        )
        assert allowed.status_code == 201

    with factory() as db:
        drawings = db.scalars(
            select(ProductDrawing).where(ProductDrawing.product_id == ids["product"])
        ).all()
        assert len(drawings) == 1
        assert drawings[0].image_path.endswith("replacement.png")
        assert db.scalar(select(func.count()).select_from(ProductDrawing)) == 1
