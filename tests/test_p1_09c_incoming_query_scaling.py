from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from fastapi import Response
from sqlalchemy import event, select
from sqlalchemy.orm import sessionmaker


def _fixture(tmp_path: Path, *, ordinary_count: int):
    from app.core.database import create_sqlite_engine
    from app.models import Base
    from app.models.access_control import UserCustomerScope
    from app.models.customer import Customer
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "p1_09c_incoming.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        visible = Customer(
            customer_number=1,
            customer_code="VISIBLE",
            name="Visible Incoming Customer",
            payment_term_days=0,
            credit_limit=Decimal("0"),
        )
        hidden = Customer(
            customer_number=2,
            customer_code="HIDDEN",
            name="Hidden Incoming Customer",
            payment_term_days=0,
            credit_limit=Decimal("0"),
        )
        user = User(
            username="p1-09c-incoming-scoped",
            password_hash="not-used-by-direct-endpoint-test",
            role="workshop",
            real_name="P1 incoming scope",
            must_change_password=False,
            customer_access_mode="selected",
        )
        db.add_all([visible, hidden, user])
        db.flush()
        db.add(UserCustomerScope(user_id=user.id, customer_id=visible.id))
        product = Product(
            customer_id=visible.id,
            product_code="P1-09C-IN",
            customer_material_code="P1-09C-IN",
            product_name="Pending incoming box",
            box_category="normal",
        )
        hidden_product = Product(
            customer_id=hidden.id,
            product_code="P1-09C-IN-H",
            customer_material_code="P1-09C-IN-H",
            product_name="Hidden pending incoming box",
            box_category="normal",
        )
        db.add_all([product, hidden_product])
        db.flush()

        visible_item_ids: list[int] = []
        for index in range(ordinary_count):
            order = Order(
                order_number=f"P1-09C-IN-{index:03d}",
                customer_id=visible.id,
                order_date=date(2026, 7, 29),
                status="pending_production",
                payment_status="unpaid",
                total_amount=Decimal("0"),
            )
            db.add(order)
            db.flush()
            item = OrderItem(
                order_id=order.id,
                product_id=product.id,
                quantity=100,
                delivered_quantity=0,
                unit_price=Decimal("0"),
                subtotal=Decimal("0"),
                material_status="pending",
                requisition_status="已报料",
                supplier_order_number=f"P1-09C-SRO-{index:03d}",
                snapshot_product_name="Pending incoming box",
                snapshot_product_code="P1-09C-IN",
                snapshot_material="A=B",
                cardboard_len=500,
                cardboard_width=300,
            )
            db.add(item)
            db.flush()
            visible_item_ids.append(item.id)

        hidden_order = Order(
            order_number="P1-09C-IN-H-001",
            customer_id=hidden.id,
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
                requisition_status="已报料",
                snapshot_product_name="Hidden pending incoming box",
                snapshot_product_code="P1-09C-IN-H",
                snapshot_material="A=B",
            )
        )
        db.commit()
        return engine, factory, user.id, visible_item_ids


def _read_and_count(factory, user_id: int):
    from app.api.incoming import pending_items
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
            response = pending_items(Response(), db, user)
    finally:
        event.remove(engine, "before_cursor_execute", record_sql)
    return response, statements


def _select_count(statements: list[str]) -> int:
    return sum(statement.startswith("select") for statement in statements)


def _dashboard_and_count(factory, user_id: int):
    from app.api.dashboard import dashboard_overview
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
            response = dashboard_overview(db, user)
    finally:
        event.remove(engine, "before_cursor_execute", record_sql)
    return response, statements


def test_pending_incoming_ordinary_rows_scale_without_writes(tmp_path: Path) -> None:
    _engine, small_factory, small_user_id, _ids = _fixture(
        tmp_path / "small", ordinary_count=1
    )
    _engine, large_factory, large_user_id, _ids = _fixture(
        tmp_path / "large", ordinary_count=20
    )
    small, small_sql = _read_and_count(small_factory, small_user_id)
    large, large_sql = _read_and_count(large_factory, large_user_id)

    assert len(small["items"]) == 1
    assert len(large["items"]) == 20
    assert all(row["customer_name"] == "Visible Incoming Customer" for row in large["items"])
    assert all(
        not statement.startswith(("insert", "update", "delete"))
        for statement in [*small_sql, *large_sql]
    )
    # Receipt facts, physical supplier-order lines, stock-replenishment sources
    # and P1-150B physical groups are fetched in constant batches; the total
    # must remain independent of the number of rows.
    assert _select_count(small_sql) == 15
    assert _select_count(large_sql) == _select_count(small_sql)
    row = large["items"][0]
    assert {
        "planned_quantity": 100,
        "cumulative_received_quantity": 0,
        "remaining_quantity": 100,
        "variance_quantity": 0,
        "variance_type": None,
        "resolution_status": "not_required",
        "resolution_action": None,
        "pending_receipt_item_id": None,
    }.items() <= row.items()


def test_dashboard_incoming_collection_does_not_restore_per_row_queries(
    tmp_path: Path,
) -> None:
    _engine, small_factory, small_user_id, _ids = _fixture(
        tmp_path / "small-dashboard", ordinary_count=1
    )
    _engine, large_factory, large_user_id, _ids = _fixture(
        tmp_path / "large-dashboard", ordinary_count=20
    )
    small, small_sql = _dashboard_and_count(small_factory, small_user_id)
    large, large_sql = _dashboard_and_count(large_factory, large_user_id)

    small_card = next(
        card for card in small["cards"] if card["key"] == "pending_incoming"
    )
    large_card = next(
        card for card in large["cards"] if card["key"] == "pending_incoming"
    )
    assert small_card["count"] == 1
    assert large_card["count"] == 20
    assert _select_count(large_sql) <= _select_count(small_sql) + 5
    assert all(
        not statement.startswith(("insert", "update", "delete"))
        for statement in [*small_sql, *large_sql]
    )


def test_pending_incoming_receipt_and_component_rows_use_batch_summary(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import app.api.incoming as incoming_api
    from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem
    from app.models.order import OrderItem
    from app.models.requisition import Requisition, RequisitionItem
    from app.models.user import User

    _engine, factory, user_id, item_ids = _fixture(tmp_path, ordinary_count=5)
    with factory() as db:
        items = db.scalars(
            select(OrderItem).where(OrderItem.id.in_(item_ids)).order_by(OrderItem.id)
        ).all()
        receipts = [
            IncomingReceipt(
                receipt_number=f"P1-09C-R-{index}",
                status="posted" if index < 2 else "reversed",
                received_at=datetime(2026, 7, 29, 9, index),
                idempotency_key=f"p1-09c-r-{index}",
            )
            for index in range(3)
        ]
        db.add_all(receipts)
        db.flush()
        db.add_all(
            [
                IncomingReceiptItem(
                    receipt_id=receipts[0].id,
                    order_id=items[0].order_id,
                    order_item_id=items[0].id,
                    planned_quantity=100,
                    received_quantity=40,
                    cumulative_received_quantity=40,
                    variance_quantity=-60,
                    variance_type="short",
                    resolution_status="pending",
                    resolution_action="await_supplier",
                    status="posted",
                ),
                IncomingReceiptItem(
                    receipt_id=receipts[1].id,
                    order_id=items[1].order_id,
                    order_item_id=items[1].id,
                    planned_quantity=100,
                    received_quantity=120,
                    cumulative_received_quantity=120,
                    variance_quantity=20,
                    variance_type="over",
                    resolution_status="resolved",
                    resolution_action="all_to_production",
                    status="posted",
                ),
                IncomingReceiptItem(
                    receipt_id=receipts[2].id,
                    order_id=items[2].order_id,
                    order_item_id=items[2].id,
                    planned_quantity=100,
                    received_quantity=20,
                    cumulative_received_quantity=20,
                    variance_quantity=-80,
                    variance_type="short",
                    resolution_status="pending",
                    resolution_action="await_supplier",
                    status="reversed",
                ),
            ]
        )
        requisition = Requisition(
            requisition_number="P1-09C-COMPONENT",
            requisition_date=date(2026, 7, 29),
        )
        db.add(requisition)
        db.flush()
        component = RequisitionItem(
            requisition_id=requisition.id,
            order_item_id=items[3].id,
            requisition_qty=100,
            cardboard_len=Decimal("500"),
            cardboard_width=Decimal("300"),
            product_name_snapshot="Pending incoming box-盖",
            status="有效",
        )
        db.add(component)
        db.commit()

    called_keys: list[int | str] = []
    original = incoming_api.source_summary_for_item

    def spy(db, item_key):
        called_keys.append(item_key)
        return original(db, item_key)

    monkeypatch.setattr(incoming_api, "source_summary_for_item", spy)
    response, statements = _read_and_count(factory, user_id)
    rows = {str(row["item_id"]): row for row in response["items"]}

    assert called_keys == []
    assert rows[str(item_ids[0])]["cumulative_received_quantity"] == 40
    assert rows[str(item_ids[0])]["remaining_quantity"] == 60
    assert rows[str(item_ids[0])]["resolution_action"] == "await_supplier"
    assert rows[str(item_ids[1])]["variance_type"] == "over"
    assert rows[str(item_ids[2])]["latest_receipt_item_id"] is None
    with factory() as db:
        expected = {
            str(item_key): original(db, item_key)
            for item_key in (*item_ids[:3], f"r{component.id}")
        }
    summary_keys = {
        "planned_quantity",
        "cumulative_received_quantity",
        "remaining_quantity",
        "variance_quantity",
        "variance_type",
        "resolution_status",
        "resolution_action",
        "pending_receipt_item_id",
        "latest_receipt_item_id",
        "latest_receipt_id",
        "surplus_inventory_lot_id",
    }
    for item_key, expected_summary in expected.items():
        if expected_summary is None:
            assert rows[item_key]["latest_receipt_item_id"] is None
            continue
        assert {
            key: rows[item_key].get(key) for key in summary_keys
        } == {
            key: expected_summary.get(key) for key in summary_keys
        }
    # The receipt/source/component facts are fixed-size batch queries.  The
    # current strict map projection adds constant joins but must never scale
    # with the number of rows. P0-23 keeps unused location projections lazy;
    # P1-150B adds one fixed physical-group lookup for synthetic reserve rows.
    assert _select_count(statements) <= 20
    assert all(
        not statement.startswith(("insert", "update", "delete"))
        for statement in statements
    )
