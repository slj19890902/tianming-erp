from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.orm import sessionmaker


PROJECTION_KEYS = (
    "item_id",
    "order_item_id",
    "requisition_item_id",
    "supplier_order_id",
    "supplier_order_item_id",
    "stock_replenishment_item_id",
    "customer_id",
    "customer_name",
    "order_number",
    "product_code",
    "delivery_date",
    "created_at",
)


def _projection(row: dict) -> dict:
    return {key: row.get(key) for key in PROJECTION_KEYS}


def _fixture(tmp_path: Path, *, ordinary_count: int = 1):
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

    engine = create_sqlite_engine(tmp_path / "p1_36j_incoming.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        visible = Customer(
            customer_number=1,
            customer_code="P1-36J-VISIBLE",
            name="P1-36J 可见客户",
            payment_term_days=0,
            credit_limit=Decimal("0"),
        )
        hidden = Customer(
            customer_number=2,
            customer_code="P1-36J-HIDDEN",
            name="P1-36J 隐藏客户",
            payment_term_days=0,
            credit_limit=Decimal("0"),
        )
        user = User(
            username="p1-36j-scoped",
            password_hash="not-used",
            role="workshop",
            real_name="P1-36J scope",
            must_change_password=False,
            customer_access_mode="selected",
        )
        db.add_all([visible, hidden, user])
        db.flush()
        db.add(UserCustomerScope(user_id=user.id, customer_id=visible.id))

        visible_product = Product(
            customer_id=visible.id,
            product_code="P1-36J-PARENT",
            customer_material_code="P1-36J-PARENT",
            product_name="P1-36J 外箱",
            box_category="normal",
        )
        hidden_product = Product(
            customer_id=hidden.id,
            product_code="P1-36J-HIDDEN",
            customer_material_code="P1-36J-HIDDEN",
            product_name="P1-36J 隐藏外箱",
            box_category="normal",
        )
        db.add_all([visible_product, hidden_product])
        db.flush()

        for index in range(ordinary_count):
            order = Order(
                order_number=f"P1-36J-ORD-{index:03d}",
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
            db.add(
                OrderItem(
                    order_id=order.id,
                    product_id=visible_product.id,
                    quantity=100,
                    delivered_quantity=0,
                    unit_price=Decimal("0"),
                    subtotal=Decimal("0"),
                    material_status="pending",
                    requisition_status="已报料",
                    snapshot_product_name="P1-36J 外箱",
                    snapshot_product_code=(
                        "" if index == 0 else f"P1-36J-O-{index:03d}"
                    ),
                    snapshot_material="A=B",
                    cardboard_len=500,
                    cardboard_width=300,
                )
            )

        component_order = Order(
            order_number="P1-36J-COMPONENT",
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
            snapshot_product_name="P1-36J 组合父件",
            snapshot_product_code="P1-36J-PARENT",
            snapshot_material="A=B",
        )
        db.add(component_item)
        db.flush()
        requisition = Requisition(
            requisition_number="P1-36J-REQ-001",
            requisition_date=date(2026, 8, 10),
            status="已报料",
        )
        db.add(requisition)
        db.flush()
        db.add_all(
            [
                RequisitionItem(
                    requisition_id=requisition.id,
                    order_item_id=component_item.id,
                    requisition_qty=300,
                    cardboard_len=Decimal("470"),
                    cardboard_width=Decimal("600"),
                    product_code_snapshot="P1-36J-COMP-A",
                    product_name_snapshot="P1-36J 组件 A",
                    status="有效",
                ),
                RequisitionItem(
                    requisition_id=requisition.id,
                    order_item_id=component_item.id,
                    requisition_qty=150,
                    cardboard_len=Decimal("575"),
                    cardboard_width=Decimal("550"),
                    product_code_snapshot="P1-36J-COMP-B",
                    product_name_snapshot="P1-36J 组件 B",
                    status="有效",
                ),
            ]
        )

        hidden_order = Order(
            order_number="P1-36J-HIDDEN-ORDER",
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
                snapshot_product_name="P1-36J 隐藏外箱",
                snapshot_product_code="P1-36J-HIDDEN",
                snapshot_material="A=B",
            )
        )

        stock_order = StockReplenishmentOrder(
            order_number="P1-36J-STOCK",
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
                product_code_snapshot="P1-36J-STOCK-P",
                product_name_snapshot="P1-36J 补库纸板",
                quantity=50,
                stocked_quantity=0,
            )
        ]
        hidden_stock_order = StockReplenishmentOrder(
            order_number="P1-36J-HIDDEN-STOCK",
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
                product_code_snapshot="P1-36J-HIDDEN-STOCK-P",
                product_name_snapshot="P1-36J 隐藏补库纸板",
                quantity=50,
                stocked_quantity=0,
            )
        ]
        db.add_all([stock_order, hidden_stock_order])
        db.commit()
        return engine, factory, user.id


def test_projection_matches_full_routes_todos_and_customer_scope(
    tmp_path: Path,
) -> None:
    from app.api.incoming import _rows, dashboard_pending_incoming_rows
    from app.models.user import User

    _engine, factory, user_id = _fixture(tmp_path)
    with factory() as db:
        user = db.get(User, user_id)
        assert user is not None
        full = [_projection(row) for row in _rows(db, user=user)]
        narrow_rows = dashboard_pending_incoming_rows(db, user)
        narrow = [_projection(row) for row in narrow_rows]

    assert narrow == full
    assert all(set(row).issubset(PROJECTION_KEYS) for row in narrow_rows)
    assert {row["product_code"] for row in narrow_rows} == {
        "",
        "P1-36J-COMP-A",
        "P1-36J-COMP-B",
        "P1-36J-STOCK-P",
    }
    assert sum(bool(row.get("requisition_item_id")) for row in narrow_rows) == 2
    assert sum(bool(row.get("stock_replenishment_item_id")) for row in narrow_rows) == 1
    assert all(row["customer_name"] == "P1-36J 可见客户" for row in narrow_rows)


def test_projection_skips_full_page_decoration(tmp_path: Path, monkeypatch) -> None:
    import app.api.incoming as incoming_api
    from app.models.user import User

    _engine, factory, user_id = _fixture(tmp_path)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("dashboard projection entered full page decoration")

    monkeypatch.setattr(incoming_api, "_PendingIncomingReadContext", forbidden)
    monkeypatch.setattr(incoming_api, "_source_component_types", forbidden)
    monkeypatch.setattr(incoming_api, "_product_drawing_url", forbidden)
    monkeypatch.setattr(incoming_api, "_order_drawing_url", forbidden)
    monkeypatch.setattr(incoming_api, "_stock_replenishment_pending_rows", forbidden)
    monkeypatch.setattr(incoming_api, "build_display_registry", forbidden)
    with factory() as db:
        user = db.get(User, user_id)
        assert user is not None
        rows = incoming_api.dashboard_pending_incoming_rows(db, user)
    assert len(rows) == 4


def test_projection_loads_only_requested_order_numbers_without_full_registry(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import app.api.incoming as incoming_api
    from app.models.customer import Customer
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.user import User

    _engine, factory, user_id = _fixture(tmp_path)
    with factory() as db:
        customer = db.query(Customer).filter_by(customer_code="P1-36J-VISIBLE").one()
        product = db.query(Product).filter_by(product_code="P1-36J-PARENT").one()
        for suffix, pending in ((2, False), (10, True), (30, False)):
            order = Order(
                order_number=f"TM20260810{suffix:03d}",
                customer_id=customer.id,
                order_date=date(2026, 8, 10),
                status="pending_production",
                payment_status="unpaid",
                total_amount=Decimal("0"),
            )
            db.add(order)
            db.flush()
            db.add(
                OrderItem(
                    order_id=order.id,
                    product_id=product.id,
                    quantity=10,
                    delivered_quantity=0,
                    unit_price=Decimal("0"),
                    subtotal=Decimal("0"),
                    material_status="pending" if pending else "received",
                    requisition_status="已报料",
                    snapshot_product_name="当前纸箱",
                    snapshot_product_code=f"P1-36J-P-{suffix}",
                    snapshot_material="A=B",
                )
            )
        db.commit()

        user = db.get(User, user_id)
        assert user is not None
        full_row = next(
            row
            for row in incoming_api._rows(db, user=user)
            if row.get("product_code") == "P1-36J-P-10"
        )

        def forbidden(*_args, **_kwargs):
            raise AssertionError("首页不得构造全量订单号显示表")

        monkeypatch.setattr(incoming_api, "build_display_registry", forbidden)
        projected_row = next(
            row
            for row in incoming_api.dashboard_pending_incoming_rows(db, user)
            if row.get("product_code") == "P1-36J-P-10"
        )

    assert full_row["order_number"] == "TM20260810010"
    assert projected_row["order_number"] == full_row["order_number"]


def _query_count(factory, user_id: int) -> tuple[int, list[str], int]:
    from app.api.incoming import dashboard_pending_incoming_rows
    from app.models.user import User

    engine = factory.kw["bind"]
    statements: list[str] = []

    def record_sql(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.lstrip().lower())

    event.listen(engine, "before_cursor_execute", record_sql)
    try:
        with factory() as db:
            user = db.get(User, user_id)
            assert user is not None
            rows = dashboard_pending_incoming_rows(db, user)
    finally:
        event.remove(engine, "before_cursor_execute", record_sql)
    return len(rows), statements, sum(row.startswith("select") for row in statements)


def test_projection_queries_are_bounded_and_read_only(tmp_path: Path) -> None:
    results: dict[int, tuple[int, list[str], int]] = {}
    for count in (1, 20, 100):
        _engine, factory, user_id = _fixture(
            tmp_path / f"scale-{count}", ordinary_count=count
        )
        results[count] = _query_count(factory, user_id)

    assert results[1][0] == 4
    assert results[20][0] == 23
    assert results[100][0] == 103
    assert results[20][2] <= results[1][2] + 1
    assert results[100][2] <= results[1][2] + 1
    all_sql = [statement for _rows, statements, _count in results.values() for statement in statements]
    assert all(
        not statement.startswith(("insert", "update", "delete"))
        for statement in all_sql
    )
    assert not any(
        forbidden_table in statement
        for statement in all_sql
        for forbidden_table in (
            "product_drawings",
            "incoming_receipt_items",
            "warehouse_locations",
            "materials ",
        )
    )
