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
def order_trace_app(tmp_path: Path):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.orders import router as orders_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.access_control import UserCustomerScope
    from app.models.customer import Customer
    from app.models.delivery import Delivery, DeliveryItem
    from app.models.finance import (
        Invoice,
        ReturnReceipt,
        ReturnReceiptItem,
        SettlementRecord,
        Statement,
        StatementItem,
    )
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.requisition import Requisition, RequisitionItem
    from app.models.user import User
    from app.models.warehouse_inventory import (
        FinishedGoodsInventoryDetail,
        InventoryLot,
        WarehouseLocation,
    )

    engine = create_sqlite_engine(tmp_path / "p1-04-order-trace.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    base_time = datetime(2026, 7, 29, 1, 0, 0)
    with factory() as db:
        admin = User(
            username="trace-admin",
            password_hash=hash_password("RolePass123!"),
            role="admin",
            real_name="Trace Admin",
            must_change_password=False,
        )
        sales = User(
            username="trace-sales",
            password_hash=hash_password("RolePass123!"),
            role="sales",
            real_name="Trace Sales",
            must_change_password=False,
            customer_access_mode="selected",
        )
        customer_a = Customer(
            customer_number=401,
            customer_code="TRA",
            name="追溯客户甲",
        )
        customer_b = Customer(
            customer_number=402,
            customer_code="TRB",
            name="追溯客户乙",
        )
        db.add_all([admin, sales, customer_a, customer_b])
        db.flush()
        product_a = Product(
            customer_id=customer_a.id,
            product_code="TRACE-A",
            customer_material_code="TRACE-A",
            product_name="追溯纸箱甲",
        )
        product_b = Product(
            customer_id=customer_b.id,
            product_code="TRACE-B",
            customer_material_code="TRACE-B",
            product_name="追溯纸箱乙",
        )
        db.add_all([product_a, product_b])
        db.flush()
        unrelated_location = WarehouseLocation(
            location_code="E1-L99",
            location_name="同款但无单据关联的测试库位",
            warehouse_type="finished",
        )
        db.add(unrelated_location)
        db.flush()
        unrelated_lot = InventoryLot(
            lot_number="LOT-UNRELATED-SAME-PRODUCT",
            inventory_type="finished",
            warehouse_location_id=unrelated_location.id,
            quantity_available=99,
            quantity_reserved=0,
            quantity_consumed=0,
            quantity_damaged=0,
            quantity_scrapped=0,
            unit="boxes",
            status="active",
            source_type="manual",
            stock_date=date(2026, 7, 29),
            stock_date_accuracy="exact",
            last_movement_at=base_time,
        )
        db.add(unrelated_lot)
        db.flush()
        db.add(
            FinishedGoodsInventoryDetail(
                inventory_lot_id=unrelated_lot.id,
                owner_customer_id=customer_a.id,
                owner_customer_name_snapshot=customer_a.name,
                is_general=False,
                product_id=product_a.id,
                inventory_code_snapshot="TRACE-A",
                product_name_snapshot="追溯纸箱甲",
            )
        )
        order_a = Order(
            order_number="TMTRACE-A1",
            customer_id=customer_a.id,
            customer_po="SAME-PO",
            order_date=date(2026, 7, 29),
            status="pending_reconciliation",
            payment_status="unpaid",
            total_amount=Decimal("30"),
            created_at=base_time,
        )
        order_a_same_po = Order(
            order_number="TMTRACE-A2",
            customer_id=customer_a.id,
            customer_po="SAME-PO",
            order_date=date(2026, 7, 29),
            status="pending_production",
            payment_status="unpaid",
            total_amount=Decimal("20"),
            created_at=base_time + timedelta(minutes=1),
        )
        order_b = Order(
            order_number="TMTRACE-B1",
            customer_id=customer_b.id,
            customer_po="SAME-PO",
            order_date=date(2026, 7, 29),
            status="pending_production",
            payment_status="unpaid",
            total_amount=Decimal("10"),
            created_at=base_time + timedelta(minutes=2),
        )
        order_a_without_po = Order(
            order_number="TMTRACE-A-NO-PO-1",
            customer_id=customer_a.id,
            customer_po=None,
            order_date=date(2026, 7, 29),
            status="pending_production",
            payment_status="unpaid",
            total_amount=Decimal("5"),
            created_at=base_time + timedelta(minutes=3),
        )
        order_a_without_po_other = Order(
            order_number="TMTRACE-A-NO-PO-2",
            customer_id=customer_a.id,
            customer_po="",
            order_date=date(2026, 7, 29),
            status="pending_production",
            payment_status="unpaid",
            total_amount=Decimal("6"),
            created_at=base_time + timedelta(minutes=4),
        )
        db.add_all(
            [
                order_a,
                order_a_same_po,
                order_b,
                order_a_without_po,
                order_a_without_po_other,
            ]
        )
        db.flush()
        item_a = OrderItem(
            order_id=order_a.id,
            product_id=product_a.id,
            item_order_number="TMTRACE-A1-001",
            item_sequence=1,
            quantity=30,
            delivered_quantity=30,
            unit_price=Decimal("1"),
            subtotal=Decimal("30"),
            material_status="received",
            requisition_status="已入库",
            snapshot_product_code="TRACE-A",
            snapshot_product_name="追溯纸箱甲",
            snapshot_spec="300×200×100",
        )
        item_a_same_po = OrderItem(
            order_id=order_a_same_po.id,
            product_id=product_a.id,
            item_order_number="TMTRACE-A2-001",
            item_sequence=1,
            quantity=20,
            unit_price=Decimal("1"),
            subtotal=Decimal("20"),
            material_status="pending",
            requisition_status="未报料",
            snapshot_product_code="TRACE-A",
            snapshot_product_name="同客户同单号另一订单",
        )
        item_b = OrderItem(
            order_id=order_b.id,
            product_id=product_b.id,
            item_order_number="TMTRACE-B1-001",
            item_sequence=1,
            quantity=10,
            unit_price=Decimal("1"),
            subtotal=Decimal("10"),
            material_status="pending",
            requisition_status="未报料",
            snapshot_product_code="TRACE-B",
            snapshot_product_name="追溯纸箱乙",
        )
        item_a_without_po = OrderItem(
            order_id=order_a_without_po.id,
            product_id=product_a.id,
            item_order_number="TMTRACE-A-NO-PO-1-001",
            item_sequence=1,
            quantity=5,
            unit_price=Decimal("1"),
            subtotal=Decimal("5"),
            material_status="pending",
            requisition_status="未报料",
            snapshot_product_code="TRACE-NO-PO-1",
            snapshot_product_name="无客户单号订单一",
        )
        item_a_without_po_other = OrderItem(
            order_id=order_a_without_po_other.id,
            product_id=product_a.id,
            item_order_number="TMTRACE-A-NO-PO-2-001",
            item_sequence=1,
            quantity=6,
            unit_price=Decimal("1"),
            subtotal=Decimal("6"),
            material_status="pending",
            requisition_status="未报料",
            snapshot_product_code="TRACE-NO-PO-2",
            snapshot_product_name="无客户单号订单二",
        )
        db.add_all(
            [
                item_a,
                item_a_same_po,
                item_b,
                item_a_without_po,
                item_a_without_po_other,
            ]
        )
        db.flush()
        requisition = Requisition(
            requisition_number="REQ-TRACE-A",
            requisition_date=date(2026, 7, 29),
            supplier_name="鸣朋",
            status="已报料",
            created_at=base_time + timedelta(hours=1),
        )
        db.add(requisition)
        db.flush()
        db.add(
            RequisitionItem(
                requisition_id=requisition.id,
                order_item_id=item_a.id,
                requisition_qty=30,
                cardboard_len=Decimal("400"),
                cardboard_width=Decimal("300"),
                product_code_snapshot="TRACE-A",
                product_name_snapshot="追溯纸箱甲",
                specification_snapshot="300×200×100",
                status="有效",
            )
        )
        delivery = Delivery(
            delivery_number="TH000001",
            customer_id=customer_a.id,
            delivery_date=date(2026, 7, 29),
            status="dispatched",
            total_quantity=30,
            created_at=base_time + timedelta(hours=2),
            dispatched_at=base_time + timedelta(hours=3),
            ever_dispatched_at=base_time + timedelta(hours=3),
        )
        db.add(delivery)
        db.flush()
        delivery_item = DeliveryItem(
            delivery_id=delivery.id,
            order_item_id=item_a.id,
            delivered_quantity=30,
            ordered_quantity_snapshot=30,
            order_remaining_snapshot=30,
            created_at=base_time + timedelta(hours=2),
        )
        db.add(delivery_item)
        db.flush()
        receipt = ReturnReceipt(
            delivery_id=delivery.id,
            actual_received_date=date(2026, 7, 30),
            status="confirmed",
            created_at=base_time + timedelta(days=1),
        )
        db.add(receipt)
        db.flush()
        receipt_item = ReturnReceiptItem(
            return_receipt_id=receipt.id,
            delivery_item_id=delivery_item.id,
            actual_received_quantity=30,
            created_at=base_time + timedelta(days=1),
        )
        db.add(receipt_item)
        db.flush()
        statement = Statement(
            statement_number="ST-TRACE-001",
            customer_id=customer_a.id,
            statement_month="2026-07",
            total_receivable=Decimal("30"),
            status="settled",
            created_at=base_time + timedelta(days=2),
        )
        db.add(statement)
        db.flush()
        db.add(
            StatementItem(
                statement_id=statement.id,
                return_receipt_item_id=receipt_item.id,
                actual_received_quantity=30,
                unit_price_snapshot=Decimal("1"),
                unit_cost_snapshot=Decimal("0.5"),
                receivable_amount=Decimal("30"),
                gross_profit_amount=Decimal("15"),
                created_at=base_time + timedelta(days=2),
            )
        )
        db.add_all(
            [
                Invoice(
                    statement_id=statement.id,
                    invoice_number="INV-TRACE-001",
                    invoice_date=date(2026, 8, 1),
                    invoice_amount=Decimal("30"),
                    created_at=base_time + timedelta(days=3),
                ),
                SettlementRecord(
                    statement_id=statement.id,
                    settled_amount=Decimal("30"),
                    settlement_date=date(2026, 8, 2),
                    account="测试账户",
                    created_at=base_time + timedelta(days=4),
                ),
                UserCustomerScope(user_id=sales.id, customer_id=customer_a.id),
            ]
        )
        db.commit()
        ids = {
            "customer_a": customer_a.id,
            "customer_b": customer_b.id,
            "order_a": order_a.id,
            "item_a": item_a.id,
            "order_a_same_po": order_a_same_po.id,
            "item_a_same_po": item_a_same_po.id,
            "order_b": order_b.id,
            "item_b": item_b.id,
            "order_a_without_po": order_a_without_po.id,
            "item_a_without_po": item_a_without_po.id,
            "order_a_without_po_other": order_a_without_po_other.id,
            "item_a_without_po_other": item_a_without_po_other.id,
        }

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(orders_router, prefix="/api/orders")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    return app, ids


def _login(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": "RolePass123!"},
    )
    assert response.status_code == 200, response.text


def test_exact_item_trace_does_not_mix_same_customer_po(order_trace_app) -> None:
    app, ids = order_trace_app
    with TestClient(app) as client:
        _login(client, "trace-admin")
        response = client.get(
            f"/api/orders/{ids['order_a']}/items/{ids['item_a']}/documents"
        )
        empty_response = client.get(
            f"/api/orders/{ids['order_a_same_po']}/items/{ids['item_a_same_po']}/documents"
        )

    assert response.status_code == 200, response.text
    body = response.json()
    document_numbers = {event["document_number"] for event in body["events"]}
    assert "REQ-TRACE-A" in document_numbers
    assert "TH000001" in document_numbers
    assert "ST-TRACE-001" in document_numbers
    assert "INV-TRACE-001" in document_numbers
    assert body["item"]["id"] == ids["item_a"]
    assert body["current_inventory"] == []
    assert not any("unit_cost" in str(event) for event in body["events"])

    assert empty_response.status_code == 200, empty_response.text
    empty_body = empty_response.json()
    assert len(empty_body["events"]) == 1
    assert empty_body["events"][0]["stage"] == "order"
    assert "REQ-TRACE-A" not in {
        event["document_number"] for event in empty_body["events"]
    }


def test_scope_and_stage_permissions_are_enforced(order_trace_app) -> None:
    app, ids = order_trace_app
    with TestClient(app) as client:
        _login(client, "trace-sales")
        allowed = client.get(
            f"/api/orders/{ids['order_a']}/items/{ids['item_a']}/documents"
        )
        denied = client.get(
            f"/api/orders/{ids['order_b']}/items/{ids['item_b']}/documents"
        )

    assert allowed.status_code == 200, allowed.text
    body = allowed.json()
    assert {row["stage"] for row in body["restricted_stages"]} >= {
        "requisition",
        "incoming",
        "inventory",
        "delivery",
        "statement",
        "invoice",
        "settlement",
    }
    assert {event["stage"] for event in body["events"]} == {"order"}
    assert denied.status_code == 403


def test_item_must_belong_to_order(order_trace_app) -> None:
    app, ids = order_trace_app
    with TestClient(app) as client:
        _login(client, "trace-admin")
        response = client.get(
            f"/api/orders/{ids['order_a']}/items/{ids['item_b']}/documents"
        )
    assert response.status_code == 404


def test_group_detail_returns_all_same_customer_po_orders_across_pages(
    order_trace_app,
) -> None:
    app, ids = order_trace_app
    with TestClient(app) as client:
        _login(client, "trace-admin")
        response = client.get(
            "/api/orders/group-detail",
            params={
                "customer_id": ids["customer_a"],
                "customer_po": "SAME-PO",
                "anchor_order_id": ids["order_a"],
                "scope": "all",
            },
        )
        first_page = client.get(
            "/api/orders",
            params={
                "customer_id": ids["customer_a"],
                "customer_po": "SAME-PO",
                "scope": "all",
                "page": 1,
                "page_size": 1,
            },
        )
        second_page = client.get(
            "/api/orders",
            params={
                "customer_id": ids["customer_a"],
                "customer_po": "SAME-PO",
                "scope": "all",
                "page": 2,
                "page_size": 1,
            },
        )

    assert response.status_code == 200, response.text
    assert first_page.status_code == 200, first_page.text
    assert second_page.status_code == 200, second_page.text
    assert first_page.json()["total"] == second_page.json()["total"] == 2
    assert {
        first_page.json()["items"][0]["id"],
        second_page.json()["items"][0]["id"],
    } == {ids["order_a"], ids["order_a_same_po"]}
    body = response.json()
    assert {row["id"] for row in body["orders"]} == {
        ids["order_a"],
        ids["order_a_same_po"],
    }
    assert {
        item["snapshot_product_code"]
        for row in body["orders"]
        for item in row["items"]
    } == {"TRACE-A"}
    assert ids["order_b"] not in {row["id"] for row in body["orders"]}


def test_group_detail_without_customer_po_is_exactly_one_anchor_order(
    order_trace_app,
) -> None:
    app, ids = order_trace_app
    with TestClient(app) as client:
        _login(client, "trace-admin")
        response = client.get(
            "/api/orders/group-detail",
            params={
                "customer_id": ids["customer_a"],
                "anchor_order_id": ids["order_a_without_po"],
            },
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert [row["id"] for row in body["orders"]] == [ids["order_a_without_po"]]
    assert body["orders"][0]["items"][0]["snapshot_product_code"] == "TRACE-NO-PO-1"


def test_group_detail_respects_current_business_scope(order_trace_app) -> None:
    app, ids = order_trace_app
    with TestClient(app) as client:
        _login(client, "trace-admin")
        active = client.get(
            "/api/orders/group-detail",
            params={
                "customer_id": ids["customer_a"],
                "customer_po": "SAME-PO",
                "anchor_order_id": ids["order_a_same_po"],
                "scope": "active",
            },
        )
        stale_anchor = client.get(
            "/api/orders/group-detail",
            params={
                "customer_id": ids["customer_a"],
                "customer_po": "SAME-PO",
                "anchor_order_id": ids["order_a"],
                "scope": "active",
            },
        )

    assert active.status_code == 200, active.text
    assert [row["id"] for row in active.json()["orders"]] == [
        ids["order_a_same_po"]
    ]
    assert stale_anchor.status_code == 409


def test_group_detail_rechecks_customer_scope(order_trace_app) -> None:
    app, ids = order_trace_app
    with TestClient(app) as client:
        _login(client, "trace-sales")
        allowed = client.get(
            "/api/orders/group-detail",
            params={
                "customer_id": ids["customer_a"],
                "customer_po": "SAME-PO",
                "anchor_order_id": ids["order_a"],
                "scope": "all",
            },
        )
        denied = client.get(
            "/api/orders/group-detail",
            params={
                "customer_id": ids["customer_b"],
                "customer_po": "SAME-PO",
                "anchor_order_id": ids["order_b"],
            },
        )

    assert allowed.status_code == 200, allowed.text
    assert denied.status_code == 403
    assert "estimated_cost" not in str(allowed.json())
    assert "total_estimated_cost" not in str(allowed.json())


def test_group_detail_rejects_mismatched_anchor_identity(order_trace_app) -> None:
    app, ids = order_trace_app
    with TestClient(app) as client:
        _login(client, "trace-admin")
        wrong_customer = client.get(
            "/api/orders/group-detail",
            params={
                "customer_id": ids["customer_a"],
                "customer_po": "SAME-PO",
                "anchor_order_id": ids["order_b"],
                "scope": "all",
            },
        )
        wrong_po = client.get(
            "/api/orders/group-detail",
            params={
                "customer_id": ids["customer_a"],
                "customer_po": "OTHER-PO",
                "anchor_order_id": ids["order_a"],
                "scope": "all",
            },
        )

    assert wrong_customer.status_code == 404
    assert wrong_po.status_code == 409


@pytest.mark.parametrize(
    ("stage", "source_type", "status", "expected"),
    [
        ("order", "sales_order", "pending_production", "待生产"),
        ("order", "sales_order", "waiting_material", "待收料"),
        ("order", "sales_order", "production", "生产中"),
        ("order", "sales_order", "delivered", "已送完"),
        ("order", "sales_order", "pending_material", "待报料"),
        ("order", "sales_order", "pending_incoming", "待收料"),
        ("order", "sales_order", "pending_delivery", "待送货"),
        ("order", "sales_order", "partially_delivered", "部分送完"),
        ("order", "sales_order", "waiting_receipt", "待回单"),
        ("order", "sales_order", "pending_reconciliation", "待对账"),
        ("order", "sales_order", "pending_invoice", "待开票"),
        ("order", "sales_order", "pending_payment", "待结款"),
        ("order", "sales_order", "completed", "订单完成"),
        ("production", "production_task", "waiting_material", "待收料"),
        ("production", "production_task", "pending", "待生产"),
        ("production", "production_task", "completed", "已完成"),
        ("production", "production_task", "not_required", "无需生产"),
        ("inventory", "inventory_movement", "reserve", "预占库存"),
        ("inventory", "inventory_movement", "release_reserve", "释放预占"),
        ("inventory", "inventory_movement", "manual_in", "手工入库"),
        ("inventory", "inventory_movement", "adjust", "库存调整"),
        ("inventory", "inventory_movement", "freeze", "冻结"),
        ("inventory", "inventory_movement", "unfreeze", "解冻"),
        ("inventory", "inventory_movement", "damage", "报损"),
        ("inventory", "inventory_movement", "scrap", "报废"),
        ("inventory", "inventory_movement", "consume", "出库扣减"),
        ("inventory", "inventory_movement", "reverse_consume", "撤销出库"),
        ("inventory", "inventory_reservation", "active", "预占中"),
        ("inventory", "inventory_lot", "active", "正常在库"),
        ("inventory", "inventory_lot", "frozen", "已冻结"),
        ("delivery", "delivery_dispatch", "closed", "已关闭"),
        ("inventory", "inventory_lot", "future_status", "状态待确认"),
    ],
)
def test_trace_status_labels_are_contextual_chinese(
    stage: str,
    source_type: str,
    status: str,
    expected: str,
) -> None:
    from app.services.order_document_trace import _status_label

    assert (
        _status_label(status, stage=stage, source_type=source_type)
        == expected
    )
