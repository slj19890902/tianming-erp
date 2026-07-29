from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy import event, select
from sqlalchemy.orm import Session, sessionmaker


def _fixture(tmp_path: Path, *, visible_count: int):
    from app.core.database import create_sqlite_engine
    from app.models import Base
    from app.models.access_control import UserCustomerScope
    from app.models.customer import Customer
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "p1_09c_requisition_scaling.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        visible = Customer(
            customer_number=1,
            customer_code="VISIBLE",
            name="可见客户",
            payment_term_days=0,
            credit_limit=Decimal("0"),
        )
        hidden = Customer(
            customer_number=2,
            customer_code="HIDDEN",
            name="不可见客户",
            payment_term_days=0,
            credit_limit=Decimal("0"),
        )
        user = User(
            username="scoped-sales",
            password_hash="not-used-by-direct-endpoint-test",
            role="sales",
            real_name="范围测试",
            must_change_password=False,
            customer_access_mode="selected",
        )
        db.add_all([visible, hidden, user])
        db.flush()
        db.add(UserCustomerScope(user_id=user.id, customer_id=visible.id))
        visible_product = Product(
            customer_id=visible.id,
            product_code="P1-09C-V",
            customer_material_code="P1-09C-V",
            product_name="可见待报料纸箱",
            box_category="normal",
        )
        hidden_product = Product(
            customer_id=hidden.id,
            product_code="P1-09C-H",
            customer_material_code="P1-09C-H",
            product_name="不可见待报料纸箱",
            box_category="normal",
        )
        db.add_all([visible_product, hidden_product])
        db.flush()

        for index in range(visible_count):
            order = Order(
                order_number=f"P1-09C-V-{index:03d}",
                customer_id=visible.id,
                customer_po=f"PO-V-{index:03d}",
                order_date=date(2026, 7, 29),
                status="pending_production",
                payment_status="unpaid",
                total_amount=Decimal("0"),
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
                    requisition_status="未报料",
                    snapshot_product_name="可见待报料纸箱",
                    snapshot_product_code="P1-09C-V",
                    snapshot_material="A=B",
                    snapshot_report_length_mm=500,
                    snapshot_report_width_mm=300,
                )
            )

        hidden_order = Order(
            order_number="P1-09C-H-001",
            customer_id=hidden.id,
            customer_po="PO-H-001",
            order_date=date(2026, 7, 29),
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
                quantity=100,
                delivered_quantity=0,
                unit_price=Decimal("0"),
                subtotal=Decimal("0"),
                material_status="pending",
                requisition_status="未报料",
                snapshot_product_name="不可见待报料纸箱",
                snapshot_product_code="P1-09C-H",
                snapshot_material="A=B",
                snapshot_report_length_mm=500,
                snapshot_report_width_mm=300,
            )
        )
        db.commit()
        return engine, factory, user.id


def _read_and_count(
    factory: sessionmaker,
    user_id: int,
    endpoint,
) -> tuple[dict, list[str]]:
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
            response = endpoint(db, user)
    finally:
        event.remove(engine, "before_cursor_execute", record_sql)
    return response, statements


def _select_count(statements: list[str]) -> int:
    return sum(statement.startswith("select") for statement in statements)


def test_pending_and_merge_keep_customer_scope_and_do_not_write(tmp_path: Path) -> None:
    from app.api.requisition import merge_suggestions, pending_requisitions

    _engine, factory, user_id = _fixture(tmp_path, visible_count=2)
    pending, pending_sql = _read_and_count(factory, user_id, pending_requisitions)
    suggestions, merge_sql = _read_and_count(factory, user_id, merge_suggestions)

    assert [row["customer_name"] for row in pending["items"]] == ["可见客户"] * 2
    assert all(
        member["customer_name"] != "不可见客户"
        for row in suggestions["suggestions"]
        for member in row["members"]
    )
    assert {"item_id", "requisition_qty", "inventory_deducted_qty", "component_requirements"} <= set(pending["items"][0])
    assert not any(
        statement.startswith(("insert", "update", "delete"))
        for statement in [*pending_sql, *merge_sql]
    )


def test_pending_and_merge_query_growth_is_bounded(
    tmp_path: Path,
) -> None:
    from app.api.requisition import merge_suggestions, pending_requisitions

    _engine, small_factory, small_user_id = _fixture(tmp_path / "small", visible_count=1)
    _engine, large_factory, large_user_id = _fixture(tmp_path / "large", visible_count=20)
    _small_pending, small_pending_sql = _read_and_count(
        small_factory, small_user_id, pending_requisitions
    )
    _large_pending, large_pending_sql = _read_and_count(
        large_factory, large_user_id, pending_requisitions
    )
    _small_merge, small_merge_sql = _read_and_count(
        small_factory, small_user_id, merge_suggestions
    )
    _large_merge, large_merge_sql = _read_and_count(
        large_factory, large_user_id, merge_suggestions
    )

    # The request-scoped negative-fact context keeps ordinary 20-line reads
    # close to the one-line query count.  The bound intentionally remains
    # tight so a future per-line helper call cannot silently return.
    assert _select_count(large_pending_sql) <= _select_count(small_pending_sql) + 8
    assert _select_count(large_merge_sql) <= _select_count(small_merge_sql) + 8


def test_pending_complex_semi_requirement_uses_existing_calculation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import app.api.requisition as requisition_api
    from app.api.requisition import pending_requisitions
    from app.models.order import Order, OrderItem
    from app.models.warehouse_inventory import OrderItemSemiRequirement
    from app.models.user import User

    _engine, factory, user_id = _fixture(tmp_path, visible_count=1)
    with factory() as db:
        # The fixture has one visible item plus one hidden item; select the
        # scoped user's visible order item explicitly instead of relying on row
        # creation order.
        item = db.scalar(
            select(OrderItem)
            .join(Order, Order.id == OrderItem.order_id)
            .where(Order.order_number == "P1-09C-V-000")
        )
        assert item is not None
        db.add(
            OrderItemSemiRequirement(
                order_item_id=item.id,
                customer_id=item.order.customer_id,
                component_type="whole",
                board_length_mm=500,
                board_width_mm=300,
                material_code_snapshot="A=B",
                normalized_material_code="A=B",
                flute_type="B",
                pieces_per_box=1,
                stock_yield_per_sheet=1,
                required_piece_quantity=100,
            )
        )
        db.commit()

    called_item_ids: list[int] = []
    original = requisition_api._current_requisition_summary

    def spy(db, item, **kwargs):
        called_item_ids.append(item.id)
        return original(db, item, **kwargs)

    monkeypatch.setattr(requisition_api, "_current_requisition_summary", spy)
    with factory() as db:
        user = db.get(User, user_id)
        assert user is not None
        response = pending_requisitions(db, user)

    assert called_item_ids
    assert response["items"][0]["item_id"] in called_item_ids
    assert response["items"][0]["semi_finished_reserved_piece_qty"] == 0


def test_ordinary_fast_requirements_match_existing_whole_item_summary(
    tmp_path: Path,
) -> None:
    from app.api.requisition import (
        _current_requisition_summary,
        _ordinary_requisition_requirements,
    )
    from app.models.order import Order, OrderItem

    _engine, factory, _user_id = _fixture(tmp_path, visible_count=1)
    with factory() as db:
        item = db.scalar(
            select(OrderItem)
            .join(Order, Order.id == OrderItem.order_id)
            .where(Order.order_number == "P1-09C-V-000")
        )
        assert item is not None
        existing = _current_requisition_summary(db, item)
        fast = _ordinary_requisition_requirements(item)

    assert fast == existing
    assert fast["component_requirements"] == [
        {
            key: value
            for key, value in fast.items()
            if key != "component_requirements"
        }
    ]


def test_ordinary_fast_pending_fields_match_existing_helpers(
    tmp_path: Path,
) -> None:
    from app.api.requisition import (
        _current_requisition_summary,
        _late_finished_inventory_preview,
        _safe_customer_board_preparation_options,
        pending_requisitions,
    )
    from app.models.customer import Customer
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.user import User

    _engine, factory, user_id = _fixture(tmp_path, visible_count=1)
    with factory() as db:
        item, order, customer, product = db.execute(
            select(OrderItem, Order, Customer, Product)
            .join(Order, Order.id == OrderItem.order_id)
            .join(Customer, Customer.id == Order.customer_id)
            .join(Product, Product.id == OrderItem.product_id)
            .where(Order.order_number == "P1-09C-V-000")
        ).one()
        requirements = _current_requisition_summary(db, item)
        preview = _late_finished_inventory_preview(
            db, item=item, order=order, product=product
        )
        board_options = _safe_customer_board_preparation_options(
            db, item=item, order=order, product=product
        )
        user = db.get(User, user_id)
        assert user is not None
        response = pending_requisitions(db, user)

    row = response["items"][0]
    for field in (
        "finished_inventory_reserved_qty",
        "production_required_qty",
        "fully_covered_by_finished_inventory",
        "requisition_qty",
        "cutting_mode",
        "cutting_factor",
        "pieces_per_box",
        "required_piece_qty",
        "semi_finished_reserved_piece_qty",
        "remaining_required_piece_qty",
        "component_requirements",
    ):
        assert row[field] == requirements[field]
    assert row["late_finished_inventory"] == preview
    assert row["customer_board_preparation_available_piece_qty"] == sum(
        int(option["available_piece_quantity"]) for option in board_options
    )
    assert row["customer_board_preparation_available_sheet_qty"] == sum(
        int(option["available_sheet_quantity"]) for option in board_options
    )


def test_pending_telescoping_lid_box_falls_back_to_cover_and_base_calculation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import app.api.requisition as requisition_api
    from app.api.requisition import pending_requisitions
    from app.models.order import Order, OrderItem
    from app.models.user import User

    _engine, factory, user_id = _fixture(tmp_path, visible_count=1)
    with factory() as db:
        item = db.scalar(
            select(OrderItem)
            .join(Order, Order.id == OrderItem.order_id)
            .where(Order.order_number == "P1-09C-V-000")
        )
        assert item is not None
        item.product.box_style = "A3"
        item.snapshot_base_report_length_mm = 480
        item.snapshot_base_report_width_mm = 320
        db.commit()

    called_item_ids: list[int] = []
    original = requisition_api._current_requisition_summary

    def spy(db, item, **kwargs):
        called_item_ids.append(item.id)
        return original(db, item, **kwargs)

    monkeypatch.setattr(requisition_api, "_current_requisition_summary", spy)
    with factory() as db:
        user = db.get(User, user_id)
        assert user is not None
        response = pending_requisitions(db, user)

    assert response["items"][0]["item_id"] in called_item_ids
    assert [row["component_type"] for row in response["items"][0]["component_requirements"]] == [
        "cover",
        "base",
    ]
