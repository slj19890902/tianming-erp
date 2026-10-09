from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker


def _seed_dashboard_data(factory: sessionmaker[Session]) -> dict[str, int]:
    from app.core.security import hash_password
    from app.models.access_control import UserCustomerScope, UserPermissionOverride
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

    today = date.today()
    with factory() as db:
        finance = User(
            username="scoped-finance",
            password_hash=hash_password("Pass123!"),
            role="finance",
            real_name="Scoped Finance",
            customer_access_mode="selected",
            must_change_password=False,
        )
        finance_no_cost = User(
            username="finance-no-cost",
            password_hash=hash_password("Pass123!"),
            role="finance",
            real_name="Finance No Cost",
            customer_access_mode="selected",
            must_change_password=False,
        )
        sales = User(
            username="scoped-sales",
            password_hash=hash_password("Pass123!"),
            role="sales",
            real_name="Scoped Sales",
            customer_access_mode="selected",
            must_change_password=False,
        )
        empty_sales = User(
            username="empty-sales",
            password_hash=hash_password("Pass123!"),
            role="sales",
            real_name="Empty Sales",
            customer_access_mode="selected",
            must_change_password=False,
        )
        dashboard_only = User(
            username="dashboard-only-sales",
            password_hash=hash_password("Pass123!"),
            role="sales",
            real_name="Dashboard Only Sales",
            customer_access_mode="selected",
            must_change_password=False,
        )
        no_orders = User(
            username="no-orders-sales",
            password_hash=hash_password("Pass123!"),
            role="sales",
            real_name="No Orders Sales",
            customer_access_mode="selected",
            must_change_password=False,
        )
        allowed = Customer(name="Allowed customer")
        excluded = Customer(name="Excluded customer")
        db.add_all(
            [
                finance,
                finance_no_cost,
                sales,
                empty_sales,
                dashboard_only,
                no_orders,
                allowed,
                excluded,
            ]
        )
        db.flush()
        db.add_all(
            [
                UserCustomerScope(user_id=finance.id, customer_id=allowed.id),
                UserCustomerScope(user_id=finance_no_cost.id, customer_id=allowed.id),
                UserCustomerScope(user_id=sales.id, customer_id=allowed.id),
                UserCustomerScope(user_id=dashboard_only.id, customer_id=allowed.id),
                UserCustomerScope(user_id=no_orders.id, customer_id=allowed.id),
                UserPermissionOverride(
                    user_id=finance_no_cost.id,
                    permission_code="cost.view",
                    is_allowed=False,
                ),
                UserPermissionOverride(
                    user_id=dashboard_only.id,
                    permission_code="deliveries.view",
                    is_allowed=False,
                ),
                UserPermissionOverride(
                    user_id=no_orders.id,
                    permission_code="orders.view",
                    is_allowed=False,
                ),
            ]
        )

        for customer, prefix, price, gross_profit, receivable in (
            (allowed, "ALLOWED", Decimal("5.00"), Decimal("3.00"), Decimal("11.00")),
            (excluded, "EXCLUDED", Decimal("7.00"), Decimal("9.00"), Decimal("22.00")),
        ):
            product = Product(
                customer_id=customer.id,
                product_code=prefix,
                customer_material_code=prefix,
                product_name=prefix,
                box_category="normal",
            )
            db.add(product)
            db.flush()
            order = Order(
                order_number=f"ORDER-{prefix}",
                customer_id=customer.id,
                order_date=today,
                delivery_date=today,
                status="pending_delivery",
                payment_status="unpaid",
                total_amount=0,
            )
            db.add(order)
            db.flush()
            order_item = OrderItem(
                order_id=order.id,
                product_id=product.id,
                quantity=10,
                delivered_quantity=0,
                unit_price=price,
                subtotal=0,
                material_status="received",
                snapshot_product_name=prefix,
            )
            db.add(order_item)
            db.flush()
            delivery = Delivery(
                delivery_number=f"DELIVERY-{prefix}",
                customer_id=customer.id,
                delivery_date=today,
                status="dispatched",
                total_quantity=10,
            )
            db.add(delivery)
            db.add(
                Delivery(
                    delivery_number=f"VOIDED-{prefix}",
                    customer_id=customer.id,
                    delivery_date=today,
                    status="voided",
                    total_quantity=10,
                    ever_dispatched_at=datetime.now(),
                    voided_at=datetime.now(),
                )
            )
            db.flush()
            delivery_item = DeliveryItem(
                delivery_id=delivery.id,
                order_item_id=order_item.id,
                delivered_quantity=10,
            )
            db.add(delivery_item)
            db.flush()
            receipt = ReturnReceipt(
                delivery_id=delivery.id,
                actual_received_date=today,
                status="confirmed",
            )
            db.add(receipt)
            db.flush()
            receipt_item = ReturnReceiptItem(
                return_receipt_id=receipt.id,
                delivery_item_id=delivery_item.id,
                actual_received_quantity=2,
            )
            db.add(receipt_item)
            db.flush()
            statement = Statement(
                statement_number=f"STATEMENT-{prefix}",
                customer_id=customer.id,
                statement_month=today.strftime("%Y-%m"),
                total_receivable=receivable,
                total_gross_profit=gross_profit,
                status="unsettled",
            )
            db.add(statement)
            db.flush()
            db.add(
                StatementItem(
                    statement_id=statement.id,
                    return_receipt_item_id=receipt_item.id,
                    actual_received_quantity=2,
                    unit_price_snapshot=price,
                    unit_cost_snapshot=Decimal("1.00"),
                    receivable_amount=price * 2,
                    gross_profit_amount=gross_profit,
                )
            )
        db.commit()
        return {
            "finance": finance.id,
            "finance_no_cost": finance_no_cost.id,
            "sales": sales.id,
            "empty_sales": empty_sales.id,
            "dashboard_only": dashboard_only.id,
            "no_orders": no_orders.id,
            "allowed_customer": allowed.id,
        }


def test_dashboard_selected_customer_scope_filters_every_aggregate(tmp_path: Path) -> None:
    from app.api.dashboard import dashboard_kpi, dashboard_overview
    from app.core.database import create_sqlite_engine
    from app.models import Base
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "dashboard-scope.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    ids = _seed_dashboard_data(factory)
    with factory() as db:
        user = db.get(User, ids["finance"])
        kpi = dashboard_kpi(db, user)
        overview = dashboard_overview(db, user)

    assert kpi["monthly_revenue"] == Decimal("10.00")
    assert kpi["monthly_gross_profit"] == Decimal("3.00")
    assert kpi["outstanding_receivables"] == Decimal("11.00")
    assert kpi["today_pending_delivery_tasks"] == 1
    assert overview["summary"] == {
        "today_orders": 1,
        "today_deliveries": 1,
        "today_receipts": 1,
        "month_unsettled_amount": Decimal("0.00"),  # Draft, unissued statements are not actionable collections.
        "month_settled_amount": Decimal("0.00"),
    }
    assert {card["key"] for card in overview["cards"]} >= {
        "pending_invoice",
        "pending_payment",
    }
    from app.api.dashboard import RECONCILIATION_REMINDER_START_DAY
    assert ("pending_reconciliation" in {c["key"] for c in overview["cards"]}) == (
        date.fromisoformat(overview["as_of"][:10]).day >= RECONCILIATION_REMINDER_START_DAY
    )
    assert overview["todos"]
    assert {todo["customer_name"] for todo in overview["todos"]} == {
        "Allowed customer"
    }


def test_dashboard_hides_finance_data_and_empty_selected_scope_returns_zeroes(
    tmp_path: Path,
) -> None:
    from app.api.dashboard import dashboard_kpi, dashboard_overview
    from app.core.database import create_sqlite_engine
    from app.models import Base
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "dashboard-permissions.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    ids = _seed_dashboard_data(factory)
    sql: list[str] = []

    def record_sql(_conn, _cursor, statement, _parameters, _context, _many) -> None:
        sql.append(statement.lower())

    event.listen(engine, "before_cursor_execute", record_sql)
    try:
        with factory() as db:
            finance_no_cost = db.get(User, ids["finance_no_cost"])
            no_cost_kpi = dashboard_kpi(db, finance_no_cost)
        assert "monthly_gross_profit" not in no_cost_kpi
        assert no_cost_kpi["monthly_revenue"] == Decimal("10.00")
        assert not any("gross_profit_amount" in statement for statement in sql)

        sql.clear()
        with factory() as db:
            sales = db.get(User, ids["sales"])
            sales_kpi = dashboard_kpi(db, sales)
            sales_overview = dashboard_overview(db, sales)
        assert {"monthly_revenue", "monthly_gross_profit", "outstanding_receivables"}.isdisjoint(
            sales_kpi
        )
        assert {card["key"] for card in sales_overview["cards"]}.isdisjoint(
            {
                "pending_material",
                "pending_incoming",
                "pending_delivery",
                "pending_receipt",
                "pending_reconciliation",
                "pending_invoice",
                "pending_payment",
            }
        )
        assert "today_pending_incoming_tasks" not in sales_kpi
        assert "today_pending_delivery_tasks" not in sales_kpi
        assert all(
            todo["target"] not in {"requisition", "incoming", "deliveries", "finance"}
            for todo in sales_overview["todos"]
        )
        assert {
            "today_deliveries",
            "today_receipts",
            "month_unsettled_amount",
            "month_settled_amount",
        }.isdisjoint(sales_overview["summary"])
        assert not any("finance_statements" in statement for statement in sql)
        assert not any("finance_return_receipt_items" in statement for statement in sql)
        assert not any("requisition_status" in statement for statement in sql)
        assert not any("deliveries" in statement for statement in sql)

        sql.clear()
        with factory() as db:
            dashboard_only = db.get(User, ids["dashboard_only"])
            dashboard_only_kpi = dashboard_kpi(db, dashboard_only)
            dashboard_only_overview = dashboard_overview(db, dashboard_only)
        assert dashboard_only_kpi == {
            "month": date.today().strftime("%Y-%m"),
            "today_pending_production_tasks": 0,
        }
        assert dashboard_only_overview["cards"] == []
        assert dashboard_only_overview["todos"] == []
        assert dashboard_only_overview["summary"] == {"today_orders": 1}
        assert not any("deliveries" in statement for statement in sql)
        assert not any("requisition_status" in statement for statement in sql)

        sql.clear()
        with factory() as db:
            no_orders = db.get(User, ids["no_orders"])
            no_orders_overview = dashboard_overview(db, no_orders)
        assert no_orders_overview["summary"] == {}
        assert no_orders_overview["cards"] == []
        assert no_orders_overview["todos"] == []
        assert not any("sales_orders.order_date" in statement for statement in sql)

        sql.clear()
        with factory() as db:
            empty_sales = db.get(User, ids["empty_sales"])
            empty_kpi = dashboard_kpi(db, empty_sales)
            empty_overview = dashboard_overview(db, empty_sales)
    finally:
        event.remove(engine, "before_cursor_execute", record_sql)

    assert empty_kpi == {
        "month": date.today().strftime("%Y-%m"),
        "today_pending_production_tasks": 0,
    }
    assert empty_overview["cards"] == []
    assert empty_overview["todos"] == []
    assert empty_overview["summary"] == {"today_orders": 0}
    assert not any("deliveries" in statement for statement in sql)
