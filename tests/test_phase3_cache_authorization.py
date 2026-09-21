"""P07 cache keys tested through real login and persisted permission models.

Financial rows are explicit synthetic projection fixtures, not API-chain proof.
"""
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.test_n028_customer_scopes import _login, n028_customer_scope_app


def test_warm_projection_separates_customer_scope_and_finance_then_invalidates(n028_customer_scope_app):
    from app.models.access_control import UserPermissionOverride
    from app.models.user import User
    from app.models.order import Order, OrderItem
    from app.models.delivery import Delivery, DeliveryItem
    from app.models.finance import ReturnReceipt, ReturnReceiptItem, Statement, StatementItem, Invoice
    import app.services.order_business_status as statuses

    app, ids, factory = n028_customer_scope_app
    statement_ids = {}
    with factory() as db:
        sales = db.scalar(select(User).where(User.username == "n028-sales"))
        db.add(UserPermissionOverride(user_id=sales.id, permission_code="finance.view", is_allowed=False))
        for order in db.scalars(select(Order)):
            order.status = "pending_production"
            order.total_amount = Decimal("10")
            item = OrderItem(order_id=order.id,
                product_id=ids["product"] if order.customer_id == ids["customer"] else ids["other_product"],
                item_order_number=f"{order.order_number}-001", item_sequence=1, quantity=10,
                delivered_quantity=10, unit_price=Decimal("1"), subtotal=Decimal("10"),
                snapshot_product_name="P07 synthetic carton", snapshot_product_code=f"P07-{order.id}",
                material_status="received", requisition_status="已报料")
            db.add(item)
            db.flush()
            delivery = Delivery(delivery_number=f"P07-D-{order.id}", customer_id=order.customer_id,
                                delivery_date=date(2026, 9, 21), status="dispatched", total_quantity=10)
            db.add(delivery)
            db.flush()
            line = DeliveryItem(delivery_id=delivery.id, order_item_id=item.id, delivered_quantity=10)
            db.add(line)
            db.flush()
            receipt = ReturnReceipt(delivery_id=delivery.id, actual_received_date=date(2026, 9, 21), status="confirmed")
            db.add(receipt)
            db.flush()
            received = ReturnReceiptItem(return_receipt_id=receipt.id, delivery_item_id=line.id, actual_received_quantity=10)
            db.add(received)
            db.flush()
            statement = Statement(statement_number=f"P07-ST-{order.id}", customer_id=order.customer_id,
                statement_month="2026-09", total_receivable=Decimal("10"), total_gross_profit=Decimal("0"),
                invoiced_amount=Decimal("0"), settled_amount=Decimal("0"), status="unsettled")
            db.add(statement)
            db.flush()
            db.add(StatementItem(statement_id=statement.id, return_receipt_item_id=received.id,
                actual_received_quantity=10, unit_price_snapshot=Decimal("1"), unit_cost_snapshot=Decimal("0"),
                receivable_amount=Decimal("10"), gross_profit_amount=Decimal("10")))
            statement_ids[order.customer_id] = statement.id
        db.commit()

    with TestClient(app) as admin, TestClient(app) as sales, TestClient(app) as empty:
        _login(admin, "n028-admin", "AdminPass123!")
        _login(sales, "n028-sales", "SalesPass123!")
        _login(empty, "n028-empty-sales", "EmptySalesPass123!")

        def read(client, customer, state):
            response = client.get("/api/orders", params={"scope": "active", "detail_level": "summary", "customer_id": customer})
            assert response.status_code == 200, response.text
            body = response.json()
            assert {row["customer_id"] for row in body["items"]} == {customer}
            assert {row["business_status"] for row in body["items"]} == {state}
            # Both fixtures are fully delivered. Financial follow-up belongs
            # to the active list but not the unfulfilled-delivery badge.
            assert body["unfinished_total"] == 0
            assert body["total"] == 1
            return body

        for _ in range(2):
            read(admin, ids["customer"], "pending_invoice")
            read(sales, ids["customer"], "pending_reconciliation")
            read(admin, ids["other_customer"], "pending_invoice")
        keys = list(statuses._ACTIVE_STATUS_BADGE_CACHE)
        assert any(key[1] is None and key[2] is True for key in keys)
        assert any(key[1] == (ids["customer"],) and key[2] is False for key in keys)
        response = empty.get("/api/orders", params={"scope": "active", "keyword": "N028"})
        assert response.status_code == 200
        assert response.json()["items"] == []
        assert response.json()["unfinished_total"] == 0
        with factory() as db:
            db.add(Invoice(statement_id=statement_ids[ids["customer"]], invoice_number="P07-INV",
                           invoice_date=date(2026, 9, 21), invoice_amount=Decimal("10")))
            db.commit()
        assert not statuses._ACTIVE_STATUS_BADGE_CACHE
        read(sales, ids["customer"], "pending_reconciliation")
        read(admin, ids["customer"], "pending_payment")
        read(admin, ids["other_customer"], "pending_invoice")
        # A denied request writes an audit event and intentionally invalidates
        # the process cache. Check it after the warm/commit assertions.
        assert sales.get("/api/orders", params={"customer_id": ids["other_customer"]}).status_code == 403
