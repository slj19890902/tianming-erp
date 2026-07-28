from __future__ import annotations

from collections.abc import Generator
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload, sessionmaker


@pytest.fixture()
def status_app(tmp_path: Path):
    from app.api.auth import router as auth_router
    from app.api.dashboard import router as dashboard_router
    from app.api.deps import get_db
    from app.api.orders import router as orders_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.access_control import UserPermissionOverride
    from app.models.customer import Customer
    from app.models.product import Product
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "p0-order-status.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        admin = User(
            username="admin",
            password_hash=hash_password("123456"),
            role="admin",
            real_name="测试管理员",
            display_name="测试管理员",
            must_change_password=False,
        )
        dashboard_only = User(
            username="dashboard-only",
            password_hash=hash_password("123456"),
            role="delivery_picker",
            real_name="仅首页测试账号",
            display_name="仅首页测试账号",
            must_change_password=False,
        )
        db.add_all([admin, dashboard_only])
        db.flush()
        db.add_all(
            [
                UserPermissionOverride(
                    user_id=dashboard_only.id,
                    permission_code="dashboard.view",
                    is_allowed=True,
                    granted_by=admin.id,
                ),
                UserPermissionOverride(
                    user_id=dashboard_only.id,
                    permission_code="orders.view",
                    is_allowed=False,
                    granted_by=admin.id,
                ),
            ]
        )
        customer = Customer(
            customer_number=1,
            customer_code="UAT",
            name="匿名状态验收客户",
            payment_term_days=30,
            credit_limit=Decimal("0"),
        )
        db.add(customer)
        db.flush()
        db.add(
            Product(
                customer_id=customer.id,
                product_code="UAT-STATUS-001",
                customer_material_code="UAT-STATUS-001",
                product_name="匿名状态验收纸箱",
                box_category="normal",
            )
        )
        db.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(orders_router, prefix="/api/orders")
    app.include_router(dashboard_router, prefix="/api/dashboard")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    return app, factory


def _add_order(
    db: Session,
    *,
    suffix: str,
    quantity: int = 10,
    sequence: int = 1,
):
    from app.models.order import Order, OrderItem

    order = Order(
        order_number=f"TM-P0-04-{suffix}",
        customer_id=1,
        customer_po=f"PO-P0-04-{suffix}",
        order_date=date(2026, 7, 28),
        delivery_date=date(2026, 7, 28),
        status="pending_production",
        payment_status="unpaid",
        total_amount=Decimal(quantity),
    )
    db.add(order)
    db.flush()
    item = OrderItem(
        order_id=order.id,
        product_id=1,
        item_order_number=f"{order.order_number}-{sequence:03d}",
        item_sequence=sequence,
        quantity=quantity,
        delivered_quantity=0,
        unit_price=Decimal("1"),
        subtotal=Decimal(quantity),
        material_status="pending",
        requisition_status="未报料",
        snapshot_product_name="匿名状态验收纸箱",
        snapshot_product_code=f"UAT-{suffix}-{sequence}",
    )
    db.add(item)
    db.flush()
    return order, item


def _projection(db: Session, order_id: int) -> dict:
    from app.models.order import Order
    from app.services.order_business_status import build_order_business_statuses

    order = db.scalar(
        select(Order)
        .options(selectinload(Order.items))
        .where(Order.id == order_id)
    )
    assert order is not None
    return build_order_business_statuses(db, [order])[order.id]


def _add_confirmed_supplier_order(db: Session, item_id: int, suffix: str) -> None:
    from app.models.supplier_requisition_order import (
        SupplierRequisitionOrder,
        SupplierRequisitionOrderItem,
    )

    supplier_order = SupplierRequisitionOrder(
        order_number=f"SRO-P0-04-{suffix}",
        total_quantity=10,
        requisition_qty=10,
        status="confirmed",
    )
    db.add(supplier_order)
    db.flush()
    db.add(
        SupplierRequisitionOrderItem(
            supplier_order_id=supplier_order.id,
            order_item_id=item_id,
            product_id=1,
            order_number=f"TM-P0-04-{suffix}",
            product_code=f"UAT-{suffix}",
            product_name="匿名状态验收纸箱",
            quantity=10,
            requisition_qty=10,
        )
    )
    db.flush()


def _add_completed_task(db: Session, item_id: int) -> None:
    from app.models.production import ProductionTask

    db.add(
        ProductionTask(
            order_item_id=item_id,
            status="completed",
            planned_quantity=10,
            finished_coverage_snapshot=0,
            ordered_quantity_snapshot=10,
            material_received_quantity=10,
            material_input_quantity=10,
            output_factor=1,
            version=1,
        )
    )
    db.flush()


def test_line_status_progresses_only_on_formal_business_facts(status_app) -> None:
    from app.models.delivery import Delivery, DeliveryItem
    from app.models.finance import (
        Invoice,
        ReturnReceipt,
        ReturnReceiptItem,
        SettlementRecord,
        Statement,
        StatementItem,
    )
    from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem
    from app.models.order import OrderItem
    from app.models.supplier_requisition_order import (
        SupplierRequisitionOrder,
        SupplierRequisitionOrderItem,
    )

    _app, factory = status_app
    with factory() as db:
        order, item = _add_order(db, suffix="FLOW")
        assert _projection(db, order.id)["business_status"] == "pending_material"

        voided_order = SupplierRequisitionOrder(
            order_number="SRO-P0-04-VOID",
            total_quantity=10,
            requisition_qty=10,
            status="voided",
        )
        db.add(voided_order)
        db.flush()
        db.add(
            SupplierRequisitionOrderItem(
                supplier_order_id=voided_order.id,
                order_item_id=item.id,
                product_id=1,
                product_name="匿名状态验收纸箱",
                quantity=10,
                requisition_qty=10,
            )
        )
        db.flush()
        assert _projection(db, order.id)["business_status"] == "pending_material"

        _add_confirmed_supplier_order(db, item.id, "FLOW")
        assert _projection(db, order.id)["business_status"] == "pending_incoming"

        receipt = IncomingReceipt(
            receipt_number="IR-P0-04-FLOW",
            status="reversed",
            received_at=date(2026, 7, 28),
            idempotency_key="ir-p0-04-flow",
        )
        db.add(receipt)
        db.flush()
        incoming_item = IncomingReceiptItem(
            receipt_id=receipt.id,
            order_id=order.id,
            order_item_id=item.id,
            planned_quantity=10,
            received_quantity=10,
            cumulative_received_quantity=10,
            variance_quantity=0,
            variance_type="matched",
            resolution_status="not_required",
            status="reversed",
        )
        db.add(incoming_item)
        db.flush()
        assert _projection(db, order.id)["business_status"] == "pending_incoming"

        receipt.status = "posted"
        incoming_item.status = "posted"
        db.flush()
        assert _projection(db, order.id)["business_status"] == "pending_production"

        _add_completed_task(db, item.id)
        assert _projection(db, order.id)["business_status"] == "pending_delivery"

        delivery = Delivery(
            delivery_number="DN-P0-04-FLOW",
            customer_id=1,
            delivery_date=date(2026, 7, 28),
            status="pending",
            total_quantity=4,
        )
        db.add(delivery)
        db.flush()
        delivery_item = DeliveryItem(
            delivery_id=delivery.id,
            order_item_id=item.id,
            delivered_quantity=4,
        )
        db.add(delivery_item)
        db.flush()
        assert _projection(db, order.id)["business_status"] == "pending_delivery"

        delivery.status = "dispatched"
        item.delivered_quantity = 9
        db.flush()
        partial = _projection(db, order.id)
        assert partial["business_status"] == "partially_delivered"
        assert partial["business_delivery_progress"] == {
            "delivered_quantity": 4,
            "total_quantity": 10,
            "item_sequences": [1],
        }
        item_row = partial["items"][item.id]
        assert item_row["business_delivered_quantity"] == 4
        assert item_row["business_status_evidence"][
            "delivery_counter_mismatch"
        ] == {
            "persisted_quantity": 9,
            "formal_effective_quantity": 4,
        }

        delivery.status = "pending"
        item.delivered_quantity = 0
        db.flush()
        cancelled_dispatch = _projection(db, order.id)
        assert cancelled_dispatch["business_status"] == "pending_delivery"
        assert cancelled_dispatch["business_delivery_progress"] is None

        delivery.status = "dispatched"
        item.delivered_quantity = 4
        delivery.total_quantity = 10
        delivery_item.delivered_quantity = 10
        item.delivered_quantity = 10
        db.flush()
        assert _projection(db, order.id)["business_status"] == "waiting_receipt"

        return_receipt = ReturnReceipt(
            delivery_id=delivery.id,
            actual_received_date=date(2026, 7, 28),
            status="cancelled",
        )
        db.add(return_receipt)
        db.flush()
        return_item = ReturnReceiptItem(
            return_receipt_id=return_receipt.id,
            delivery_item_id=delivery_item.id,
            actual_received_quantity=10,
        )
        db.add(return_item)
        db.flush()
        assert _projection(db, order.id)["business_status"] == "waiting_receipt"

        return_receipt.status = "confirmed"
        db.flush()
        assert _projection(db, order.id)["business_status"] == "pending_reconciliation"

        statement = Statement(
            statement_number="ST-P0-04-FLOW",
            customer_id=1,
            statement_month="2026-07",
            total_receivable=Decimal("10"),
            total_gross_profit=Decimal("0"),
            invoiced_amount=Decimal("0"),
            settled_amount=Decimal("0"),
            status="unsettled",
        )
        db.add(statement)
        db.flush()
        db.add(
            StatementItem(
                statement_id=statement.id,
                return_receipt_item_id=return_item.id,
                actual_received_quantity=10,
                unit_price_snapshot=Decimal("1"),
                unit_cost_snapshot=Decimal("0"),
                receivable_amount=Decimal("10"),
                gross_profit_amount=Decimal("10"),
            )
        )
        db.flush()
        assert _projection(db, order.id)["business_status"] == "pending_invoice"

        db.add(
            Invoice(
                statement_id=statement.id,
                invoice_number="INV-P0-04-FLOW",
                invoice_date=date(2026, 7, 28),
                invoice_amount=Decimal("10"),
            )
        )
        db.flush()
        assert _projection(db, order.id)["business_status"] == "pending_payment"

        db.add(
            SettlementRecord(
                statement_id=statement.id,
                settled_amount=Decimal("10"),
                settlement_date=date(2026, 7, 28),
                account="UAT",
            )
        )
        db.flush()
        assert _projection(db, order.id)["business_status"] == "completed"


def test_partial_incoming_awaiting_supplier_remains_pending_incoming(
    status_app,
) -> None:
    from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem

    _app, factory = status_app
    with factory() as db:
        order, item = _add_order(db, suffix="PARTIAL-INCOMING")
        _add_confirmed_supplier_order(db, item.id, "PARTIAL-INCOMING")
        receipt = IncomingReceipt(
            receipt_number="IR-P0-04-PARTIAL-INCOMING",
            status="posted",
            received_at=date(2026, 7, 28),
            idempotency_key="ir-p0-04-partial-incoming",
        )
        db.add(receipt)
        db.flush()
        incoming_item = IncomingReceiptItem(
            receipt_id=receipt.id,
            order_id=order.id,
            order_item_id=item.id,
            planned_quantity=10,
            received_quantity=4,
            cumulative_received_quantity=4,
            variance_quantity=-6,
            variance_type="short",
            resolution_status="pending",
            resolution_action="await_supplier",
            status="posted",
        )
        db.add(incoming_item)
        db.flush()

        projection = _projection(db, order.id)
        assert projection["business_status"] == "pending_incoming"
        assert projection["items"][item.id]["business_status"] == "pending_incoming"

        incoming_item.resolution_status = "resolved"
        incoming_item.resolution_action = "accept_short"
        db.flush()
        assert _projection(db, order.id)["business_status"] == "pending_production"


def test_zero_short_receipt_can_close_delivery_obligation_and_reach_finance(
    status_app,
) -> None:
    from app.models.delivery import Delivery, DeliveryItem
    from app.models.finance import ReturnReceipt, ReturnReceiptItem

    _app, factory = status_app
    with factory() as db:
        order, item = _add_order(db, suffix="ZERO-SHORT")
        item.material_status = "received"
        _add_completed_task(db, item.id)
        delivery = Delivery(
            delivery_number="DN-P0-04-ZERO-SHORT",
            customer_id=order.customer_id,
            delivery_date=date(2026, 7, 28),
            status="dispatched",
            total_quantity=item.quantity,
        )
        db.add(delivery)
        db.flush()
        delivery_item = DeliveryItem(
            delivery_id=delivery.id,
            order_item_id=item.id,
            delivered_quantity=item.quantity,
        )
        db.add(delivery_item)
        db.flush()
        receipt = ReturnReceipt(
            delivery_id=delivery.id,
            actual_received_date=date(2026, 7, 28),
            status="confirmed",
        )
        db.add(receipt)
        db.flush()
        db.add(
            ReturnReceiptItem(
                return_receipt_id=receipt.id,
                delivery_item_id=delivery_item.id,
                actual_received_quantity=0,
                resolution_action="accept_short",
                difference_reason="隔离测试：客户本次实收为零并结单",
            )
        )
        item.delivered_quantity = 0
        item.is_force_closed = True
        db.flush()

        projection = _projection(db, order.id)
        assert projection["business_status"] == "pending_reconciliation"
        assert projection["items"][item.id]["business_delivered_quantity"] == 0
        assert projection["items"][item.id]["business_remaining_quantity"] == 0
        assert projection["business_delivery_progress"] is None

        receipt.status = "cancelled"
        item.delivered_quantity = item.quantity
        item.is_force_closed = False
        db.flush()
        assert _projection(db, order.id)["business_status"] == "waiting_receipt"


def test_mixed_order_keeps_earliest_blocker_and_compact_delivery_progress(
    status_app,
) -> None:
    from app.models.delivery import Delivery, DeliveryItem
    from app.models.order import OrderItem

    _app, factory = status_app
    with factory() as db:
        order, first = _add_order(db, suffix="MIX", sequence=1)
        _order, second = _add_order(db, suffix="MIX-SECOND", sequence=2)
        second.order_id = order.id
        _order, third = _add_order(db, suffix="MIX-THIRD", sequence=3)
        third.order_id = order.id
        db.flush()

        first.material_status = "received"
        _add_confirmed_supplier_order(db, second.id, "MIX-SECOND")
        _add_completed_task(db, third.id)
        delivery = Delivery(
            delivery_number="DN-P0-04-MIX",
            customer_id=1,
            delivery_date=date(2026, 7, 28),
            status="dispatched",
            total_quantity=4,
        )
        db.add(delivery)
        db.flush()
        db.add(
            DeliveryItem(
                delivery_id=delivery.id,
                order_item_id=third.id,
                delivered_quantity=4,
            )
        )
        third.delivered_quantity = 4
        db.flush()

        projection = _projection(db, order.id)
        assert projection["business_status"] == "pending_incoming"
        assert projection["items"][first.id]["business_status"] == "pending_production"
        assert projection["items"][second.id]["business_status"] == "pending_incoming"
        assert projection["items"][third.id]["business_status"] == "partially_delivered"
        assert projection["business_delivery_progress"]["item_sequences"] == [3]

        second.material_status = "received"
        db.flush()
        assert _projection(db, order.id)["business_status"] == "pending_production"

        _add_completed_task(db, first.id)
        _add_completed_task(db, second.id)
        db.flush()
        assert _projection(db, order.id)["business_status"] == "partially_delivered"


def test_orders_api_filter_detail_and_dashboard_share_projection(status_app) -> None:
    app, factory = status_app
    with factory() as db:
        incoming_order, incoming_item = _add_order(db, suffix="API-IN")
        _add_confirmed_supplier_order(db, incoming_item.id, "API-IN")
        production_order, production_item = _add_order(db, suffix="API-PROD")
        production_item.material_status = "received"
        db.commit()

    with TestClient(app) as client:
        login = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "123456"},
        )
        assert login.status_code == 200, login.text
        filtered = client.get(
            "/api/orders", params={"status": "pending_incoming"}
        )
        detail = client.get(f"/api/orders/{incoming_order.id}")
        dashboard = client.get("/api/dashboard/overview")

    assert filtered.status_code == 200, filtered.text
    assert [row["id"] for row in filtered.json()["items"]] == [incoming_order.id]
    assert filtered.json()["items"][0]["business_status"] == "pending_incoming"
    assert (
        filtered.json()["items"][0]["items"][0]["business_status"]
        == "pending_incoming"
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["business_status"] == "pending_incoming"
    assert detail.json()["items"][0]["business_status"] == "pending_incoming"
    assert dashboard.status_code == 200, dashboard.text
    counts = dashboard.json()["business_status_counts"]
    assert counts["pending_incoming"] == 1
    assert counts["pending_production"] == 1


def test_business_view_and_unfinished_badge_use_derived_status_before_paging(
    status_app,
) -> None:
    from app.models.delivery import Delivery, DeliveryItem

    app, factory = status_app
    with factory() as db:
        order, item = _add_order(db, suffix="BUSINESS-DISPATCH")
        item.material_status = "received"
        _add_completed_task(db, item.id)
        delivery = Delivery(
            delivery_number="DN-P0-04-BUSINESS-DISPATCH",
            customer_id=order.customer_id,
            delivery_date=date(2026, 7, 28),
            status="dispatched",
            total_quantity=item.quantity,
        )
        db.add(delivery)
        db.flush()
        db.add(
            DeliveryItem(
                delivery_id=delivery.id,
                order_item_id=item.id,
                delivered_quantity=item.quantity,
            )
        )
        item.delivered_quantity = item.quantity
        db.commit()

    with TestClient(app) as client:
        login = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "123456"},
        )
        assert login.status_code == 200, login.text
        daily = client.get(
            "/api/orders",
            params={"status": "business", "page_size": 1},
        )
        receipt = client.get(
            "/api/orders",
            params={"status": "waiting_receipt", "page_size": 1},
        )

    assert daily.status_code == 200, daily.text
    assert daily.json()["total"] == 0
    assert daily.json()["unfinished_total"] == 0
    assert receipt.status_code == 200, receipt.text
    assert receipt.json()["total"] == 1


def test_pending_confirmation_is_in_daily_view_and_unfinished_badge(
    status_app,
) -> None:
    app, factory = status_app
    with factory() as db:
        order, _item = _add_order(db, suffix="PENDING-CONFIRMATION")
        order.status = "pending_confirmation"
        db.commit()

    with TestClient(app) as client:
        login = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "123456"},
        )
        assert login.status_code == 200, login.text
        daily = client.get("/api/orders", params={"status": "business"})

    assert daily.status_code == 200, daily.text
    assert daily.json()["total"] == 1
    assert daily.json()["unfinished_total"] == 1
    assert daily.json()["items"][0]["business_status"] == "pending_confirmation"


def test_dashboard_keeps_future_due_accept_short_line_in_finance_chain(
    status_app,
) -> None:
    from app.core.time_contract import beijing_today
    from app.models.delivery import Delivery, DeliveryItem
    from app.models.finance import ReturnReceipt, ReturnReceiptItem

    app, factory = status_app
    with factory() as db:
        order, item = _add_order(db, suffix="DASH-ZERO-SHORT")
        order.delivery_date = beijing_today() + timedelta(days=30)
        delivery = Delivery(
            delivery_number="DN-P0-04-DASH-ZERO-SHORT",
            customer_id=order.customer_id,
            delivery_date=beijing_today(),
            status="dispatched",
            total_quantity=item.quantity,
        )
        db.add(delivery)
        db.flush()
        delivery_item = DeliveryItem(
            delivery_id=delivery.id,
            order_item_id=item.id,
            delivered_quantity=item.quantity,
        )
        db.add(delivery_item)
        db.flush()
        receipt = ReturnReceipt(
            delivery_id=delivery.id,
            actual_received_date=beijing_today(),
            status="confirmed",
        )
        db.add(receipt)
        db.flush()
        db.add(
            ReturnReceiptItem(
                return_receipt_id=receipt.id,
                delivery_item_id=delivery_item.id,
                actual_received_quantity=0,
                resolution_action="accept_short",
                difference_reason="隔离测试：未来交期订单按零实收结单",
            )
        )
        item.delivered_quantity = 0
        item.is_force_closed = True
        db.commit()

    with TestClient(app) as client:
        login = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "123456"},
        )
        assert login.status_code == 200, login.text
        dashboard = client.get("/api/dashboard/overview")

    assert dashboard.status_code == 200, dashboard.text
    body = dashboard.json()
    assert body["business_status_counts"]["pending_reconciliation"] == 1
    assert any(todo["type"] == "待对账" for todo in body["todos"])


def test_dashboard_only_account_cannot_read_order_status_counts(status_app) -> None:
    app, factory = status_app
    with factory() as db:
        _add_order(db, suffix="DASHBOARD-ONLY")
        db.commit()

    with TestClient(app) as client:
        login = client.post(
            "/api/auth/login",
            json={"username": "dashboard-only", "password": "123456"},
        )
        assert login.status_code == 200, login.text
        dashboard = client.get("/api/dashboard/overview")
        orders = client.get("/api/orders", params={"status": "business"})

    assert dashboard.status_code == 200, dashboard.text
    assert dashboard.json()["business_status_counts"] == {}
    assert orders.status_code == 403
