from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from fastapi import FastAPI, Response
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import sessionmaker


def _fixture(tmp_path: Path, *, ordinary_count: int = 3):
    from app.core.database import create_sqlite_engine
    from app.models import Base
    from app.models.access_control import UserCustomerScope
    from app.models.customer import Customer
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.requisition import Requisition, RequisitionItem
    from app.models.stock_replenishment import (
        StockReplenishmentOrder,
        StockReplenishmentOrderItem,
    )
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "p1_36l.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        visible = Customer(
            customer_number=1,
            customer_code="P1-36L-VISIBLE",
            name="P1-36L 可见客户",
            payment_term_days=0,
            credit_limit=Decimal("0"),
        )
        hidden = Customer(
            customer_number=2,
            customer_code="P1-36L-HIDDEN",
            name="P1-36L 隐藏客户",
            payment_term_days=0,
            credit_limit=Decimal("0"),
        )
        user = User(
            username="p1-36l-scoped",
            password_hash="not-used",
            role="workshop",
            real_name="P1-36L scope",
            must_change_password=False,
            customer_access_mode="selected",
        )
        db.add_all([visible, hidden, user])
        db.flush()
        db.add(UserCustomerScope(user_id=user.id, customer_id=visible.id))

        visible_product = Product(
            customer_id=visible.id,
            product_code="P1-36L-PARENT",
            customer_material_code="P1-36L-PARENT",
            product_name="P1-36L 外箱",
            box_category="normal",
        )
        hidden_product = Product(
            customer_id=hidden.id,
            product_code="P1-36L-HIDDEN",
            customer_material_code="P1-36L-HIDDEN",
            product_name="P1-36L 隐藏外箱",
            box_category="normal",
        )
        db.add_all([visible_product, hidden_product])
        db.flush()

        ordinary_ids: list[int] = []
        for index in range(ordinary_count):
            order = Order(
                order_number=f"P1-36L-ORD-{index:03d}",
                customer_id=visible.id,
                order_date=date(2026, 8, 10),
                delivery_date=date(2026, 8, 12),
                status="pending_production",
                payment_status="unpaid",
                total_amount=Decimal("0"),
                created_at=datetime(2026, 8, 10, 8, index % 60),
            )
            db.add(order)
            db.flush()
            item = OrderItem(
                order_id=order.id,
                product_id=visible_product.id,
                quantity=100,
                delivered_quantity=0,
                unit_price=Decimal("0"),
                subtotal=Decimal("0"),
                material_status="pending",
                requisition_status="已报料",
                snapshot_product_name="P1-36L 外箱",
                snapshot_product_code=f"P1-36L-O-{index:03d}",
                snapshot_material="A=B",
                requisition_qty=100,
                cardboard_len=500,
                cardboard_width=300,
            )
            db.add(item)
            db.flush()
            ordinary_ids.append(item.id)

        component_order = Order(
            order_number="P1-36L-COMPONENT",
            customer_id=visible.id,
            order_date=date(2026, 8, 10),
            delivery_date=date(2026, 8, 11),
            status="pending_production",
            payment_status="unpaid",
            total_amount=Decimal("0"),
            created_at=datetime(2026, 8, 10, 7, 0),
        )
        db.add(component_order)
        db.flush()
        component_item = OrderItem(
            order_id=component_order.id,
            product_id=visible_product.id,
            quantity=300,
            delivered_quantity=0,
            unit_price=Decimal("0"),
            subtotal=Decimal("0"),
            material_status="pending",
            requisition_status="已报料",
            snapshot_product_name="P1-36L 组合父件",
            snapshot_product_code="P1-36L-PARENT",
            snapshot_material="A=B",
        )
        db.add(component_item)
        db.flush()
        requisition = Requisition(
            requisition_number="P1-36L-REQ-001",
            requisition_date=date(2026, 8, 10),
            status="已报料",
        )
        db.add(requisition)
        db.flush()
        components = [
            RequisitionItem(
                requisition_id=requisition.id,
                order_item_id=component_item.id,
                requisition_qty=300,
                cardboard_len=Decimal("470"),
                cardboard_width=Decimal("600"),
                product_code_snapshot="P1-36L-COMP-A",
                product_name_snapshot="P1-36L 组件 A",
                status="有效",
            ),
            RequisitionItem(
                requisition_id=requisition.id,
                order_item_id=component_item.id,
                requisition_qty=150,
                cardboard_len=Decimal("575"),
                cardboard_width=Decimal("550"),
                product_code_snapshot="P1-36L-COMP-B",
                product_name_snapshot="P1-36L 组件 B",
                status="有效",
            ),
        ]
        db.add_all(components)

        hidden_order = Order(
            order_number="P1-36L-HIDDEN-ORDER",
            customer_id=hidden.id,
            order_date=date(2026, 8, 10),
            status="pending_production",
            payment_status="unpaid",
            total_amount=Decimal("0"),
        )
        db.add(hidden_order)
        db.flush()
        db.add(
            OrderItem(
                order_id=hidden_order.id,
                product_id=hidden_product.id,
                quantity=10,
                delivered_quantity=0,
                unit_price=Decimal("0"),
                subtotal=Decimal("0"),
                material_status="pending",
                requisition_status="已报料",
                snapshot_product_name="P1-36L 隐藏外箱",
                snapshot_product_code="P1-36L-HIDDEN",
                snapshot_material="A=B",
            )
        )

        stock_order = StockReplenishmentOrder(
            order_number="P1-36L-STOCK",
            customer_id=visible.id,
            source_type="stock_warning",
            status="confirmed",
            created_by=user.id,
            confirmed_by=user.id,
            created_at=datetime(2026, 8, 10, 6, 0),
        )
        stock_order.items = [
            StockReplenishmentOrderItem(
                target_inventory_type="semi_finished",
                product_id=visible_product.id,
                customer_id=visible.id,
                product_code_snapshot="P1-36L-STOCK-P",
                product_name_snapshot="P1-36L 补库纸板",
                quantity=50,
                stocked_quantity=0,
            )
        ]
        hidden_stock_order = StockReplenishmentOrder(
            order_number="P1-36L-HIDDEN-STOCK",
            customer_id=hidden.id,
            source_type="stock_warning",
            status="confirmed",
            created_by=user.id,
            confirmed_by=user.id,
        )
        hidden_stock_order.items = [
            StockReplenishmentOrderItem(
                target_inventory_type="semi_finished",
                product_id=hidden_product.id,
                customer_id=hidden.id,
                product_code_snapshot="P1-36L-HIDDEN-STOCK-P",
                product_name_snapshot="P1-36L 隐藏补库纸板",
                quantity=50,
                stocked_quantity=0,
            )
        ]
        db.add_all([stock_order, hidden_stock_order])
        db.commit()
        return engine, factory, user.id, ordinary_ids


def _read(factory, user_id: int, **kwargs) -> dict:
    from app.api.incoming import pending_items
    from app.models.user import User

    with factory() as db:
        user = db.get(User, user_id)
        assert user is not None
        return pending_items(Response(), db, user, **kwargs)


def test_pages_reassemble_legacy_routes_with_deep_equal_rows(tmp_path: Path) -> None:
    _engine, factory, user_id, ordinary_ids = _fixture(tmp_path)
    legacy = _read(factory, user_id)
    assert set(legacy) == {"items"}
    assert len(legacy["items"]) == len(ordinary_ids) + 3

    pages: list[dict] = []
    for page in range(1, 4):
        response = _read(factory, user_id, page=page, page_size=2)
        assert set(response) == {"items", "total", "page", "page_size"}
        assert response["total"] == len(legacy["items"])
        assert response["page"] == page
        assert response["page_size"] == 2
        pages.extend(response["items"])

    assert pages == legacy["items"]
    identities = [row["item_id"] for row in pages]
    assert sum(isinstance(value, int) for value in identities) == len(ordinary_ids)
    assert sum(str(value).startswith("r") for value in identities) == 2
    assert sum(str(value).startswith("sr") for value in identities) == 1
    assert all(row["customer_name"] == "P1-36L 可见客户" for row in pages)


def test_optional_defaults_clamp_and_empty_page_contract(tmp_path: Path) -> None:
    from app.models.order import Order
    from app.models.stock_replenishment import StockReplenishmentOrder

    _engine, factory, user_id, _ordinary_ids = _fixture(tmp_path)
    by_page_only = _read(factory, user_id, page=1)
    by_size_only = _read(factory, user_id, page_size=2)
    assert by_page_only["page_size"] == 25
    assert by_size_only["page"] == 1
    assert by_size_only["page_size"] == 2

    clamped = _read(factory, user_id, page=999, page_size=2)
    assert clamped["page"] == 3
    assert len(clamped["items"]) == 2

    with factory() as db:
        for order in db.query(Order).all():
            order.status = "cancelled"
        for order in db.query(StockReplenishmentOrder).all():
            order.status = "voided"
        db.commit()
    empty = _read(factory, user_id, page=999, page_size=20)
    assert empty == {"items": [], "total": 0, "page": 1, "page_size": 20}


def test_http_rejects_invalid_page_bounds(tmp_path: Path) -> None:
    import app.api.incoming as incoming_api
    from app.api.deps import get_db
    from app.models.user import User

    _engine, factory, user_id, _ordinary_ids = _fixture(tmp_path)
    app = FastAPI()
    app.include_router(incoming_api.router, prefix="/api/incoming")

    def override_db():
        with factory() as db:
            yield db

    def override_user():
        with factory() as db:
            user = db.get(User, user_id)
            assert user is not None
            db.expunge(user)
            return user

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[incoming_api.can_read] = override_user
    with TestClient(app) as client:
        with factory() as db:
            user = db.get(User, user_id)
            assert user is not None
            expected_identity = f"{user.id}:{user.auth_version}"
        valid = client.get("/api/incoming/pending?page=1&page_size=20")
        assert valid.status_code == 200
        assert valid.headers["X-ERP-Session-Identity"] == expected_identity
        assert valid.headers["Cache-Control"] == "private, no-store"
        assert valid.headers["Pragma"] == "no-cache"
        assert valid.headers["Vary"] == "Cookie"
        assert client.get("/api/incoming/pending?page=0").status_code == 422
        assert client.get("/api/incoming/pending?page_size=0").status_code == 422
        assert client.get("/api/incoming/pending?page_size=201").status_code == 422


def _read_and_count(factory, user_id: int) -> tuple[dict, list[str]]:
    engine = factory.kw["bind"]
    statements: list[str] = []

    def record_sql(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.lstrip().lower())

    event.listen(engine, "before_cursor_execute", record_sql)
    try:
        response = _read(factory, user_id, page=1, page_size=20)
    finally:
        event.remove(engine, "before_cursor_execute", record_sql)
    return response, statements


def test_paged_queries_are_bounded_read_only_and_skip_full_registry(
    tmp_path: Path, monkeypatch
) -> None:
    import app.api.incoming as incoming_api

    def forbidden(*_args, **_kwargs):
        raise AssertionError("paged pending rows built the full history registry")

    monkeypatch.setattr(incoming_api, "build_display_registry", forbidden)
    results: dict[int, tuple[dict, list[str]]] = {}
    for count in (1, 20, 100):
        _engine, factory, user_id, _ordinary_ids = _fixture(
            tmp_path / f"scale-{count}", ordinary_count=count
        )
        results[count] = _read_and_count(factory, user_id)

    assert len(results[1][0]["items"]) == 4
    assert len(results[20][0]["items"]) == 20
    assert len(results[100][0]["items"]) == 20
    select_counts = {
        count: sum(statement.startswith("select") for statement in statements)
        for count, (_response, statements) in results.items()
    }
    assert select_counts[20] <= select_counts[1] + 1
    assert select_counts[100] <= select_counts[1] + 1
    assert all(
        not statement.startswith(("insert", "update", "delete"))
        for _response, statements in results.values()
        for statement in statements
    )
