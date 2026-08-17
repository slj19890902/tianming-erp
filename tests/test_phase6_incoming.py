from __future__ import annotations

import os
import sqlite3
from collections.abc import Generator
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture()
def incoming_api_app(tmp_path: Path):
    from app.api.auth import router as auth_router
    from app.api.dashboard import router as dashboard_router
    from app.api.deps import get_db
    from app.api.incoming import router as incoming_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.customer import Customer
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "incoming.sqlite3")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(
        bind=engine,
        autoflush=False,
        expire_on_commit=False,
    )
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with session_factory() as session:
        users = [
            User(
                username=role,
                password_hash=hash_password("RolePass123!"),
                role=role,
                real_name=role,
                display_name=role,
                must_change_password=False,
            )
            for role in ("admin", "finance", "sales", "workshop")
        ]
        customer = Customer(
            customer_number=1,
            customer_code="SME",
            name="苏州思迈尔包装有限公司",
            payment_term_days=30,
            credit_limit=Decimal("100000"),
        )
        session.add_all([*users, customer])
        session.flush()
        product = Product(
            customer_id=customer.id,
            product_code="SME-001",
            customer_material_code="KH-001",
            product_name="五层加强纸箱",
            legacy_material_text="K=A-BC",
            length_mm=Decimal("520"),
            width_mm=Decimal("350"),
            height_mm=Decimal("300"),
            box_category="normal",
        )
        session.add(product)
        session.flush()
        orders = [
            Order(
                order_number="PO-20260613-001",
                customer_id=customer.id,
                order_date=date(2026, 6, 13),
                delivery_date=date(2026, 6, 15),
                status="pending_production",
                payment_status="unpaid",
                total_amount=Decimal("360"),
            ),
            Order(
                order_number="PO-20260613-002",
                customer_id=customer.id,
                order_date=date(2026, 6, 13),
                delivery_date=date(2026, 6, 14),
                status="pending_production",
                payment_status="unpaid",
                total_amount=Decimal("180"),
            ),
            Order(
                order_number="PO-20260613-003",
                customer_id=customer.id,
                order_date=date(2026, 6, 13),
                delivery_date=date(2026, 6, 16),
                status="pending_production",
                payment_status="unpaid",
                total_amount=Decimal("90"),
            ),
            Order(
                order_number="PO-20260613-004",
                customer_id=customer.id,
                order_date=date(2026, 6, 13),
                delivery_date=date(2026, 6, 17),
                status="delivered",
                payment_status="unpaid",
                total_amount=Decimal("90"),
            ),
        ]
        session.add_all(orders)
        session.flush()
        session.add_all(
            [
                OrderItem(
                    order_id=orders[0].id,
                    product_id=product.id,
                    quantity=100,
                    unit_price=Decimal("3.60"),
                    subtotal=Decimal("360"),
                    material_status="pending",
                    requisition_status="已报料",
                    snapshot_product_name="五层加强纸箱",
                    snapshot_spec="520×350×300mm",
                    snapshot_material="K=A-BC",
                ),
                OrderItem(
                    order_id=orders[1].id,
                    product_id=product.id,
                    quantity=50,
                    unit_price=Decimal("3.60"),
                    subtotal=Decimal("180"),
                    material_status="pending",
                    requisition_status="供应商已排单",
                    supplier_delivery_time=now + timedelta(hours=6),
                    snapshot_product_name="五层加强纸箱",
                    snapshot_spec="520×350×300mm",
                    snapshot_material="K=A-BC",
                ),
                OrderItem(
                    order_id=orders[2].id,
                    product_id=product.id,
                    quantity=25,
                    unit_price=Decimal("3.60"),
                    subtotal=Decimal("90"),
                    material_status="received",
                    material_received_at=now - timedelta(hours=2),
                    material_received_by=users[3].id,
                    snapshot_product_name="五层加强纸箱",
                    snapshot_spec="520×350×300mm",
                    snapshot_material="K=A-BC",
                ),
                OrderItem(
                    order_id=orders[2].id,
                    product_id=product.id,
                    quantity=20,
                    unit_price=Decimal("3.60"),
                    subtotal=Decimal("72"),
                    material_status="received",
                    material_received_at=now - timedelta(hours=25),
                    material_received_by=users[3].id,
                    snapshot_product_name="五层加强纸箱",
                    snapshot_spec="520×350×300mm",
                    snapshot_material="K=A-BC",
                ),
                OrderItem(
                    order_id=orders[3].id,
                    product_id=product.id,
                    quantity=25,
                    unit_price=Decimal("3.60"),
                    subtotal=Decimal("90"),
                    material_status="received",
                    material_received_at=now - timedelta(hours=1),
                    material_received_by=users[3].id,
                    snapshot_product_name="五层加强纸箱",
                    snapshot_spec="520×350×300mm",
                    snapshot_material="K=A-BC",
                ),
            ]
        )
        session.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(dashboard_router, prefix="/api/dashboard")
    app.include_router(incoming_router, prefix="/api/incoming")

    def override_get_db() -> Generator[Session, None, None]:
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    return app, session_factory


def _login(client: TestClient, role: str) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": role, "password": "RolePass123!"},
    )
    assert response.status_code == 200


@pytest.mark.parametrize("role", ["admin", "workshop"])
def test_only_admin_and_workshop_can_read_pending_sorted_by_recent_record(
    incoming_api_app,
    role: str,
) -> None:
    app, _ = incoming_api_app
    with TestClient(app) as client:
        _login(client, role)
        response = client.get("/api/incoming/pending")

    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["order_number"] for item in items] == [
        "PO-20260613-002",
        "PO-20260613-001",
    ]
    assert items[0]["customer_name"] == "苏州思迈尔包装有限公司"
    assert items[0]["specification"] == "520×350×300mm"
    assert items[0]["material"] == "K=A-BC"


def test_surplus_locations_use_incoming_execute_without_warehouse_view(
    incoming_api_app,
) -> None:
    from app.models.access_control import UserPermissionOverride
    from app.models.user import User
    from app.models.warehouse_inventory import WarehouseLocation

    app, session_factory = incoming_api_app
    with session_factory() as session:
        workshop = session.scalar(select(User).where(User.username == "workshop"))
        sales = session.scalar(select(User).where(User.username == "sales"))
        session.add_all(
            [
                UserPermissionOverride(
                    user_id=workshop.id,
                    permission_code="warehouse.view",
                    is_allowed=False,
                ),
                UserPermissionOverride(
                    user_id=sales.id,
                    permission_code="incoming.view",
                    is_allowed=True,
                ),
                WarehouseLocation(
                    location_code="N005-SEMI",
                    location_name="N005半成品库位",
                    warehouse_type="semi_finished",
                    is_active=True,
                ),
                WarehouseLocation(
                    location_code="N005-SHARED",
                    location_name="N005共享库位",
                    warehouse_type="shared",
                    is_active=True,
                ),
                WarehouseLocation(
                    location_code="N005-FINISHED",
                    location_name="N005成品库位",
                    warehouse_type="finished",
                    is_active=True,
                ),
                WarehouseLocation(
                    location_code="N005-INACTIVE",
                    location_name="N005停用库位",
                    warehouse_type="semi_finished",
                    is_active=False,
                ),
                WarehouseLocation(
                    location_code="N005-UNPLACED",
                    location_name="N005待布局半成品库位",
                    warehouse_type="semi_finished",
                    placement_status="unplaced",
                    is_active=True,
                ),
            ]
        )
        session.commit()

    with TestClient(app) as client:
        _login(client, "workshop")
        allowed = client.get("/api/incoming/surplus-locations")
        client.post("/api/auth/logout")
        _login(client, "sales")
        read_only = client.get("/api/incoming/surplus-locations")

    assert allowed.status_code == 200
    assert [row["location_code"] for row in allowed.json()["items"]] == [
        "N005-SEMI",
        "N005-SHARED",
    ]
    assert read_only.status_code == 403


def test_pending_incoming_prioritizes_latest_requisition_operation(
    incoming_api_app,
) -> None:
    from app.models.order import OrderItem

    app, session_factory = incoming_api_app
    with session_factory() as session:
        session.get(OrderItem, 1).requisition_date = date(2026, 6, 30)
        session.get(OrderItem, 2).requisition_date = date(2026, 6, 29)
        session.commit()
    with TestClient(app) as client:
        _login(client, "admin")
        response = client.get("/api/incoming/pending")

    assert response.status_code == 200
    assert [row["item_id"] for row in response.json()["items"]] == [1, 2]


def test_pending_incoming_uses_current_confirmed_supplier_group_only(
    incoming_api_app,
) -> None:
    from app.models.order import Order, OrderItem
    from app.models.requisition import Requisition, RequisitionItem
    from app.models.supplier_requisition_order import (
        SupplierRequisitionOrder,
        SupplierRequisitionOrderItem,
    )

    app, session_factory = incoming_api_app
    with session_factory() as session:
        first = session.get(OrderItem, 1)
        second = session.get(OrderItem, 2)
        cancelled = session.get(OrderItem, 5)
        first.supplier_order_number = "SRO-20260710-0003"
        second.requisition_status = "已报料"
        cancelled_order = session.get(Order, cancelled.order_id)
        cancelled_order.status = "cancelled"
        cancelled.material_status = "pending"
        cancelled.requisition_status = "已报料"

        old_group = Requisition(
            requisition_number="MR-OLD",
            requisition_date=date(2026, 7, 10),
            status="supplier_requisition_created",
        )
        current_group = Requisition(
            requisition_number="MR-CURRENT",
            requisition_date=date(2026, 7, 10),
            status="supplier_requisition_created",
        )
        legacy_group = Requisition(
            requisition_number="MR-LEGACY",
            requisition_date=date(2026, 7, 10),
            status="已报料",
        )
        cancelled_group = Requisition(
            requisition_number="MR-CANCELLED",
            requisition_date=date(2026, 7, 10),
            status="已报料",
        )
        session.add_all([old_group, current_group, legacy_group, cancelled_group])
        session.flush()

        def component(group: Requisition, order_item_id: int, name: str) -> RequisitionItem:
            return RequisitionItem(
                requisition_id=group.id,
                order_item_id=order_item_id,
                requisition_qty=1,
                cardboard_len=Decimal("400"),
                cardboard_width=Decimal("300"),
                product_name_snapshot=name,
                status="supplier_requisition_created",
            )

        old_cover = component(old_group, first.id, "Old box-盖")
        old_base = component(old_group, first.id, "Old box-底")
        current_cover = component(current_group, first.id, "Current box-盖")
        current_base = component(current_group, first.id, "Current box-底")
        legacy_item = RequisitionItem(
            requisition_id=legacy_group.id,
            order_item_id=second.id,
            requisition_qty=50,
            cardboard_len=Decimal("520"),
            cardboard_width=Decimal("350"),
            product_name_snapshot="Legacy effective row",
            status="有效",
        )
        cancelled_item = RequisitionItem(
            requisition_id=cancelled_group.id,
            order_item_id=cancelled.id,
            requisition_qty=25,
            cardboard_len=Decimal("520"),
            cardboard_width=Decimal("350"),
            product_name_snapshot="Cancelled effective row",
            status="有效",
        )
        session.add_all(
            [
                old_cover,
                old_base,
                current_cover,
                current_base,
                legacy_item,
                cancelled_item,
            ]
        )
        session.add_all(
            [
                SupplierRequisitionOrder(
                    order_number="SRO-20260710-0001",
                    status="voided",
                ),
                SupplierRequisitionOrder(
                    order_number="SRO-20260710-0003",
                    status="confirmed",
                ),
            ]
        )
        session.flush()
        supplier_orders = session.scalars(
            select(SupplierRequisitionOrder).order_by(SupplierRequisitionOrder.id)
        ).all()
        session.add_all(
            [
                SupplierRequisitionOrderItem(
                    supplier_order_id=supplier_orders[0].id,
                    order_item_id=first.id,
                    quantity=2,
                    requisition_qty=2,
                ),
                SupplierRequisitionOrderItem(
                    supplier_order_id=supplier_orders[1].id,
                    order_item_id=first.id,
                    quantity=2,
                    requisition_qty=2,
                ),
            ]
        )
        session.commit()
        ids = {
            "old_cover": old_cover.id,
            "old_base": old_base.id,
            "current_cover": current_cover.id,
            "current_base": current_base.id,
            "legacy_requisition_item": legacy_item.id,
            "cancelled_order_item": cancelled.id,
        }

    with TestClient(app) as client:
        _login(client, "admin")
        pending = client.get("/api/incoming/pending")
        overview = client.get("/api/dashboard/overview")
        old_receive = client.put(f"/api/incoming/receive/r{ids['old_cover']}")
        receive_cover = client.put(f"/api/incoming/receive/r{ids['current_cover']}")
        pending_after_cover = client.get("/api/incoming/pending")
        receive_base = client.put(f"/api/incoming/receive/r{ids['current_base']}")
        pending_after_all = client.get("/api/incoming/pending")

    assert pending.status_code == 200
    assert {row["item_id"] for row in pending.json()["items"]} == {
        f"r{ids['current_cover']}",
        f"r{ids['current_base']}",
        f"r{ids['legacy_requisition_item']}",
    }
    assert ids["cancelled_order_item"] not in {
        row["order_item_id"] for row in pending.json()["items"]
    }
    assert overview.status_code == 200
    pending_card = next(
        card for card in overview.json()["cards"] if card["key"] == "pending_incoming"
    )
    # Dashboard and incoming now share the actionable physical-route count.
    assert pending_card["count"] == 3
    assert old_receive.status_code == 409
    assert receive_cover.status_code == 200, receive_cover.text
    assert {row["item_id"] for row in pending_after_cover.json()["items"]} == {
        f"r{ids['current_base']}",
        f"r{ids['legacy_requisition_item']}",
    }
    assert receive_base.status_code == 200, receive_base.text
    assert {row["item_id"] for row in pending_after_all.json()["items"]} == {
        f"r{ids['legacy_requisition_item']}",
    }

    with session_factory() as session:
        first = session.get(OrderItem, 1)
        first.material_status = "pending"
        first.requisition_status = "已报料"
        first.material_received_at = None
        session.commit()
    with TestClient(app) as client:
        _login(client, "admin")
        pending_after_stale_reset = client.get("/api/incoming/pending")

    assert {row["item_id"] for row in pending_after_stale_reset.json()["items"]} == {
        f"r{ids['legacy_requisition_item']}",
    }

    with TestClient(app) as client:
        _login(client, "admin")
        receive_single = client.put(
            f"/api/incoming/receive/r{ids['legacy_requisition_item']}"
        )
        received = client.get("/api/incoming/received")
        revert_single = client.put(
            f"/api/incoming/revert/r{ids['legacy_requisition_item']}",
            json={"reason": "single requisition row regression"},
        )
        pending_after_revert = client.get("/api/incoming/pending")

    assert receive_single.status_code == 200, receive_single.text
    assert f"r{ids['legacy_requisition_item']}" in {
        row["item_id"] for row in received.json()["items"]
    }
    assert revert_single.status_code == 200, revert_single.text
    assert {row["item_id"] for row in pending_after_revert.json()["items"]} == {
        f"r{ids['legacy_requisition_item']}",
    }


def test_incoming_api_hides_legacy_history_prefix_in_order_number(
    incoming_api_app,
) -> None:
    from app.models.order import Order

    app, session_factory = incoming_api_app
    with session_factory() as session:
        session.get(Order, 1).order_number = "RUIDA-50001"
        session.commit()
    with TestClient(app) as client:
        _login(client, "admin")
        response = client.get("/api/incoming/pending")

    assert response.status_code == 200
    matched = next(
        item
        for item in response.json()["items"]
        if item["display_order_number"].startswith("TM20260613-")
    )
    assert matched["order_number"].startswith("TM20260613-")
    assert "RUIDA" not in str(response.json())


def test_received_returns_only_last_24_hours_for_authorized_roles(incoming_api_app) -> None:
    app, _ = incoming_api_app
    with TestClient(app) as client:
        _login(client, "workshop")
        response = client.get("/api/incoming/received")

    assert response.status_code == 200
    assert len(response.json()["items"]) == 2


def test_received_history_supports_stable_server_paging_and_structured_filters(
    incoming_api_app,
) -> None:
    app, _ = incoming_api_app
    with TestClient(app) as client:
        _login(client, "admin")
        first = client.get(
            "/api/incoming/history",
            params={
                "page": 1,
                "page_size": 1,
                "customer_id": 1,
                "product_code": "SME-001",
                "product_name": "加强纸箱",
            },
        )
        second = client.get(
            "/api/incoming/history",
            params={
                "page": 2,
                "page_size": 1,
                "customer_id": 1,
                "product_code": "SME-001",
                "product_name": "加强纸箱",
            },
        )
        invalid_range = client.get(
            "/api/incoming/history",
            params={"date_from": "2026-07-02", "date_to": "2026-07-01"},
        )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    first_payload = first.json()
    second_payload = second.json()
    assert first_payload["total"] == 3
    assert first_payload["page"] == 1
    assert first_payload["page_size"] == 1
    assert first_payload["sort"] == [
        "material_received_at:desc",
        "history_key:desc",
    ]
    assert first_payload["items"][0]["item_id"] != second_payload["items"][0]["item_id"]
    assert invalid_range.status_code == 422
    assert "开始日期" in invalid_range.json()["detail"]


@pytest.mark.parametrize("role", ["finance", "sales"])
def test_finance_and_sales_cannot_read_incoming_lists(incoming_api_app, role: str) -> None:
    app, _ = incoming_api_app
    with TestClient(app) as client:
        _login(client, role)
        pending = client.get("/api/incoming/pending")
        received = client.get("/api/incoming/received")

    assert pending.status_code == 403
    assert received.status_code == 403


def test_workshop_receive_is_conditional_and_audited(incoming_api_app) -> None:
    from app.models.audit import OperationLog
    from app.models.order import OrderItem

    app, session_factory = incoming_api_app
    with TestClient(app) as client:
        _login(client, "workshop")
        first = client.put("/api/incoming/receive/1")
        repeated = client.put("/api/incoming/receive/1")

    assert first.status_code == 200
    assert first.json()["material_status"] == "received"
    assert first.json()["material_received_at"] is not None
    assert repeated.status_code == 409
    with session_factory() as session:
        item = session.get(OrderItem, 1)
        user_id = session.scalar(
            select(OperationLog.user_id).where(
                OperationLog.action == "RECEIVE_MATERIAL",
                OperationLog.entity_id == 1,
            )
        )
    assert item is not None
    assert item.material_received_by is not None
    assert user_id == item.material_received_by


def test_batch_receive_supports_partial_success_and_backend_validation(
    incoming_api_app,
) -> None:
    from app.models.order import OrderItem

    app, session_factory = incoming_api_app
    with TestClient(app) as client:
        _login(client, "workshop")
        response = client.put(
            "/api/incoming/batch-receive",
            json={
                "items": [
                    {
                        "item_id": 1,
                        "received_quantity": 95,
                        "resolution_action": "accept_short",
                    },
                    {"item_id": 3, "received_quantity": 25},
                ]
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["succeeded"] == 1
    assert body["failed"] == 1
    assert body["results"][0]["success"] is True
    assert body["results"][1]["success"] is False
    assert "不可入库" in body["results"][1]["message"]
    with session_factory() as session:
        received = session.get(OrderItem, 1)
        already_received = session.get(OrderItem, 3)
        assert received.material_status == "received"
        assert received.requisition_status == "已入库"
        assert received.requisition_qty is None
        assert already_received.material_received_at is not None


def test_partial_receipt_keeps_original_plan_and_can_continue_receiving(
    incoming_api_app,
) -> None:
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.order import Order, OrderItem

    app, session_factory = incoming_api_app
    with session_factory() as session:
        session.get(OrderItem, 1).requisition_qty = 100
        session.commit()

    with TestClient(app) as client:
        _login(client, "workshop")
        first = client.put(
            "/api/incoming/receive/1",
            json={
                "received_quantity": 20,
                "resolution_action": "await_supplier",
                "idempotency_key": "partial-20",
            },
        )
        pending = client.get("/api/incoming/pending")
        second = client.put(
            "/api/incoming/receive/1",
            json={
                "received_quantity": 80,
                "idempotency_key": "partial-80",
            },
        )

    assert first.status_code == 200
    assert first.json()["material_status"] == "pending"
    assert first.json()["planned_quantity"] == 100
    assert first.json()["cumulative_received_quantity"] == 20
    assert first.json()["remaining_quantity"] == 80
    pending_row = next(row for row in pending.json()["items"] if row["item_id"] == 1)
    assert pending_row["incoming_quantity"] == 80
    assert pending_row["resolution_action"] == "await_supplier"
    assert second.status_code == 200
    assert second.json()["material_status"] == "received"
    assert second.json()["cumulative_received_quantity"] == 100
    with session_factory() as session:
        item = session.get(OrderItem, 1)
        facts = session.scalars(
            select(IncomingReceiptItem).where(
                IncomingReceiptItem.order_item_id == 1,
                IncomingReceiptItem.status == "posted",
            ).order_by(IncomingReceiptItem.id)
        ).all()
        assert item.requisition_qty == 100
        assert [fact.received_quantity for fact in facts] == [20, 80]
        assert [fact.cumulative_received_quantity for fact in facts] == [20, 100]


def test_short_receipt_can_be_manually_closed_after_supplier_declines_replenishment(
    incoming_api_app,
) -> None:
    from app.models.order import OrderItem

    app, session_factory = incoming_api_app
    with session_factory() as session:
        session.get(OrderItem, 1).requisition_qty = 100
        session.commit()
    with TestClient(app) as client:
        _login(client, "admin")
        received = client.put(
            "/api/incoming/receive/1",
            json={
                "received_quantity": 99,
                "resolution_action": "await_supplier",
                "idempotency_key": "short-wait",
            },
        )
        closed = client.put(
            f"/api/incoming/receipt-items/{received.json()['receipt_item_id']}/accept-short",
            json={},
        )

    assert received.status_code == 200
    assert received.json()["material_status"] == "pending"
    assert closed.status_code == 200
    assert closed.json()["material_status"] == "received"
    assert closed.json()["resolution_action"] == "accept_short"
    assert closed.json()["resolution_reason"] is None
    with session_factory() as session:
        assert session.get(OrderItem, 1).requisition_qty == 100


def test_variance_requires_explicit_human_decision(incoming_api_app) -> None:
    app, _ = incoming_api_app
    with TestClient(app) as client:
        _login(client, "admin")
        short = client.put(
            "/api/incoming/receive/1",
            json={"received_quantity": 99, "idempotency_key": "missing-short"},
        )
        over = client.put(
            "/api/incoming/receive/2",
            json={"received_quantity": 51, "idempotency_key": "missing-over"},
        )

    assert short.status_code == 400
    assert "短收时请选择" in short.json()["detail"]
    assert over.status_code == 400
    assert "超收时请选择" in over.json()["detail"]


def test_over_receipt_can_transfer_only_surplus_to_semi_finished_inventory(
    incoming_api_app,
) -> None:
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.order import OrderItem
    from app.models.warehouse_inventory import InventoryLot, WarehouseLocation

    app, session_factory = incoming_api_app
    with session_factory() as session:
        item = session.get(OrderItem, 1)
        item.requisition_qty = 100
        item.cardboard_len = 1200
        item.cardboard_width = 800
        item.layer_count = 5
        item.flute_type = "AB"
        item.snapshot_material = "K616K"
        location = WarehouseLocation(
            location_code="N005-SI-01",
            location_name="N005半成品测试位",
            warehouse_type="semi_finished",
            is_active=True,
        )
        session.add(location)
        session.commit()
        location_id = location.id

    with TestClient(app) as client:
        _login(client, "workshop")
        response = client.put(
            "/api/incoming/receive/1",
            json={
                "received_quantity": 102,
                "resolution_action": "transfer_to_semi_inventory",
                "resolution_reason": "超收2张转库存",
                "surplus_location_id": location_id,
                "idempotency_key": "over-to-semi",
            },
        )

    assert response.status_code == 200
    assert response.json()["variance_quantity"] == 2
    with session_factory() as session:
        fact = session.scalar(
            select(IncomingReceiptItem).where(
                IncomingReceiptItem.resolution_action == "transfer_to_semi_inventory"
            )
        )
        lot = session.get(InventoryLot, fact.surplus_inventory_lot_id)
        assert lot.source_type == "purchase_surplus"
        assert lot.quantity_available == 2
        assert lot.semi_finished_detail.board_length_mm == 1200
        assert session.get(OrderItem, 1).requisition_qty == 100

    with TestClient(app) as client:
        _login(client, "admin")
        reverted = client.put(
            f"/api/incoming/receipt-items/{response.json()['receipt_item_id']}/revert",
            json={"reason": "超收数量录入错误"},
        )
    assert reverted.status_code == 200
    with session_factory() as session:
        fact = session.get(IncomingReceiptItem, response.json()["receipt_item_id"])
        lot = session.get(InventoryLot, fact.surplus_inventory_lot_id)
        assert fact.status == "reversed"
        assert lot.status == "closed"
        assert lot.quantity_available == 0
        assert session.get(OrderItem, 1).material_status == "pending"


@pytest.mark.parametrize("used_field", ["quantity_reserved", "quantity_consumed"])
def test_surplus_inventory_in_use_blocks_receipt_revert(
    incoming_api_app,
    used_field: str,
) -> None:
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.order import OrderItem
    from app.models.warehouse_inventory import InventoryLot, WarehouseLocation

    app, session_factory = incoming_api_app
    with session_factory() as session:
        item = session.get(OrderItem, 1)
        item.requisition_qty = 100
        item.cardboard_len = 1200
        item.cardboard_width = 800
        item.layer_count = 5
        item.flute_type = "AB"
        item.snapshot_material = "K616K"
        location = WarehouseLocation(
            location_code=f"N005-IN-USE-{used_field}",
            location_name="N005撤销阻断测试位",
            warehouse_type="semi_finished",
            is_active=True,
        )
        session.add(location)
        session.commit()
        location_id = location.id

    with TestClient(app) as client:
        _login(client, "admin")
        received = client.put(
            "/api/incoming/receive/1",
            json={
                "received_quantity": 102,
                "resolution_action": "transfer_to_semi_inventory",
                "surplus_location_id": location_id,
                "idempotency_key": f"surplus-in-use-{used_field}",
            },
        )
        assert received.status_code == 200, received.text
        with session_factory() as session:
            fact = session.get(
                IncomingReceiptItem,
                received.json()["receipt_item_id"],
            )
            lot = session.get(InventoryLot, fact.surplus_inventory_lot_id)
            lot.quantity_available = 1
            setattr(lot, used_field, 1)
            session.commit()
        reverted = client.put(
            f"/api/incoming/receipt-items/{received.json()['receipt_item_id']}/revert",
            json={"reason": "库存已使用时不应撤销"},
        )

    assert reverted.status_code == 409
    assert "预占、消耗" in reverted.json()["detail"]
    with session_factory() as session:
        fact = session.get(IncomingReceiptItem, received.json()["receipt_item_id"])
        assert fact.status == "posted"
        assert session.get(OrderItem, 1).material_status == "received"


def test_over_receipt_requires_selected_location_layout_version(
    incoming_api_app,
) -> None:
    from app.models.incoming_receipt import IncomingReceipt
    from app.models.order import OrderItem
    from app.models.warehouse_inventory import (
        Floor3LocationLayout,
        WarehouseLocation,
    )

    app, session_factory = incoming_api_app
    with session_factory() as session:
        item = session.get(OrderItem, 1)
        item.requisition_qty = 100
        item.cardboard_len = 1200
        item.cardboard_width = 800
        item.layer_count = 5
        item.flute_type = "AB"
        item.snapshot_material = "K616K"
        location = WarehouseLocation(
            location_code="N005-SI-MAPPED",
            location_name="N005半成品地图位",
            warehouse_type="semi_finished",
            warehouse_floor=3,
            placement_status="placed",
            is_active=True,
        )
        session.add(location)
        session.flush()
        layout = Floor3LocationLayout(
            location_id=location.id,
            left_pct=10,
            top_pct=10,
            width_pct=12,
            height_pct=10,
            layout_kind="physical_pallet",
            source_type="seeded",
            version=1,
        )
        session.add(layout)
        session.commit()
        location_id = location.id

    with TestClient(app) as client:
        _login(client, "workshop")
        candidates = client.get("/api/incoming/surplus-locations")
        assert candidates.status_code == 200, candidates.text
        selected = next(
            row
            for row in candidates.json()["items"]
            if row["id"] == location_id
        )
        assert selected["layout_version"] == 1

        with session_factory() as session:
            layout = session.scalar(
                select(Floor3LocationLayout).where(
                    Floor3LocationLayout.location_id == location_id
                )
            )
            layout.left_pct = 40
            layout.version = 2
            session.commit()

        stale = client.put(
            "/api/incoming/receive/1",
            json={
                "received_quantity": 102,
                "resolution_action": "transfer_to_semi_inventory",
                "surplus_location_id": location_id,
                "expected_surplus_layout_version": 1,
                "idempotency_key": "over-to-mapped-stale",
            },
        )
        assert stale.status_code == 409, stale.text

        current = client.put(
            "/api/incoming/receive/1",
            json={
                "received_quantity": 102,
                "resolution_action": "transfer_to_semi_inventory",
                "surplus_location_id": location_id,
                "expected_surplus_layout_version": 2,
                "idempotency_key": "over-to-mapped-current",
            },
        )
        assert current.status_code == 200, current.text

    with session_factory() as session:
        assert int(session.scalar(select(func.count(IncomingReceipt.id))) or 0) == 1


def test_over_receipt_all_to_production_does_not_create_inventory(
    incoming_api_app,
) -> None:
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.order import OrderItem
    from app.models.warehouse_inventory import InventoryLot

    app, session_factory = incoming_api_app
    with session_factory() as session:
        session.get(OrderItem, 1).requisition_qty = 100
        session.commit()
    with TestClient(app) as client:
        _login(client, "workshop")
        response = client.put(
            "/api/incoming/receive/1",
            json={
                "received_quantity": 102,
                "resolution_action": "all_to_production",
            },
        )

    assert response.status_code == 200
    assert response.json()["variance_quantity"] == 2
    assert response.json()["resolution_action"] == "all_to_production"
    with session_factory() as session:
        fact = session.get(IncomingReceiptItem, response.json()["receipt_item_id"])
        assert fact.surplus_inventory_lot_id is None
        assert session.scalar(select(func.count(InventoryLot.id))) == 0
        assert session.get(OrderItem, 1).requisition_qty == 100


def test_receipt_idempotency_does_not_duplicate_quantity(incoming_api_app) -> None:
    from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem

    app, session_factory = incoming_api_app
    payload = {"received_quantity": 100, "idempotency_key": "same-receipt-key"}
    with TestClient(app) as client:
        _login(client, "admin")
        first = client.put("/api/incoming/receive/1", json=payload)
        repeated = client.put("/api/incoming/receive/1", json=payload)

    assert first.status_code == 200
    assert repeated.status_code == 200
    assert repeated.json()["receipt_item_id"] == first.json()["receipt_item_id"]
    with session_factory() as session:
        assert session.scalar(select(func.count(IncomingReceipt.id))) == 1
        assert session.scalar(select(func.count(IncomingReceiptItem.id))) == 1


def test_receipt_idempotency_key_rejects_a_different_target(
    incoming_api_app,
) -> None:
    from app.models.incoming_receipt import IncomingReceipt
    from app.models.order import OrderItem

    app, session_factory = incoming_api_app
    payload = {"idempotency_key": "shared-but-not-interchangeable"}
    with TestClient(app) as client:
        _login(client, "admin")
        first = client.put("/api/incoming/receive/1", json=payload)
        reused = client.put("/api/incoming/receive/2", json=payload)

    assert first.status_code == 200, first.text
    assert reused.status_code == 409
    assert "幂等键已用于其他入库操作" in reused.json()["detail"]
    with session_factory() as session:
        assert session.scalar(select(func.count(IncomingReceipt.id))) == 1
        assert session.get(OrderItem, 2).material_status == "pending"


def test_partial_receipt_is_visible_in_today_history_while_waiting(
    incoming_api_app,
) -> None:
    from app.models.order import OrderItem

    app, session_factory = incoming_api_app
    with session_factory() as session:
        session.get(OrderItem, 1).requisition_qty = 100
        session.commit()
    with TestClient(app) as client:
        _login(client, "workshop")
        received = client.put(
            "/api/incoming/receive/1",
            json={
                "received_quantity": 20,
                "resolution_action": "await_supplier",
            },
        )
        today = client.get("/api/incoming/received")

    assert received.status_code == 200
    fact = next(row for row in today.json()["items"] if row.get("receipt_item_id"))
    assert fact["received_quantity_this_time"] == 20
    assert fact["cumulative_received_quantity"] == 20
    assert fact["remaining_quantity"] == 80
    assert fact["resolution_action"] == "await_supplier"


def test_posted_receipt_production_card_is_read_only_and_uses_frozen_process(
    incoming_api_app,
) -> None:
    from app.models.audit import OperationLog
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.order import OrderItem
    from app.models.production import ProductionCompletion
    from app.models.warehouse_inventory import InventoryLot

    app, session_factory = incoming_api_app
    with session_factory() as session:
        item = session.get(OrderItem, 1)
        item.requisition_qty = 100
        item.snapshot_product_code = "KH-001"
        item.snapshot_production_notes = "先印刷，再开槽，开槽后模切，最后粘箱"
        item.cardboard_len = Decimal("1000")
        item.cardboard_width = Decimal("800")
        item.flute_type = "BC"
        item.special_process = "一开二"
        item.snapshot_crease_type = "净料"
        item.snapshot_crease_left_mm = 260
        item.snapshot_crease_middle_mm = 350
        item.snapshot_crease_right_mm = 260
        session.commit()

    with TestClient(app) as client:
        _login(client, "workshop")
        received = client.put(
            "/api/incoming/receive/1",
            json={"received_quantity": 20, "resolution_action": "await_supplier"},
        )
        assert received.status_code == 200, received.text
        receipt_item_id = received.json()["receipt_item_id"]
        with session_factory() as session:
            before = {
                "receipt_items": session.scalar(select(func.count(IncomingReceiptItem.id))),
                "completions": session.scalar(select(func.count(ProductionCompletion.id))),
                "lots": session.scalar(select(func.count(InventoryLot.id))),
                "logs": session.scalar(select(func.count(OperationLog.id))),
            }
        card = client.get(
            f"/api/incoming/receipt-items/{receipt_item_id}/production-card"
        )
        repeated = client.get(
            f"/api/incoming/receipt-items/{receipt_item_id}/production-card"
        )

    assert card.status_code == 200, card.text
    assert repeated.status_code == 200, repeated.text
    payload = card.json()
    assert payload["receipt_item_id"] == receipt_item_id
    assert payload["customer_name"] == "苏州思迈尔包装有限公司"
    assert payload["product_code"] == "KH-001"
    assert payload["received_sheet_quantity"] == 20
    assert payload["output_factor"] == 2
    assert payload["production_capacity_quantity"] == 40
    assert payload["board_length_mm"] == 1000
    assert payload["board_width_mm"] == 800
    assert payload["process_steps"] == ["印刷", "开槽", "模切", "粘箱"]
    assert payload["production_notes"] == "先印刷，再开槽，开槽后模切，最后粘箱"
    assert payload["paper_phase"] == "actual_receipt"
    assert payload["paper_version_key"] == f"actual:receipt:{receipt_item_id}"
    assert payload["card_count"] == payload["page_count"] == 1
    assert payload["cards"][0]["receipt_item_id"] == receipt_item_id
    assert payload["cards"][0]["received_sheet_quantity"] == 20
    assert payload["cards"][0]["production_capacity_quantity"] == 40
    assert "unit_price" not in payload
    assert "cost" not in card.text.lower()
    assert repeated.json()["card_number"] == payload["card_number"]
    with session_factory() as session:
        after = {
            "receipt_items": session.scalar(select(func.count(IncomingReceiptItem.id))),
            "completions": session.scalar(select(func.count(ProductionCompletion.id))),
            "lots": session.scalar(select(func.count(InventoryLot.id))),
            "logs": session.scalar(select(func.count(OperationLog.id))),
        }
    assert after == before


def test_reversed_or_unauthorized_receipt_cannot_open_production_card(
    incoming_api_app,
) -> None:
    from app.models.order import OrderItem

    app, session_factory = incoming_api_app
    with session_factory() as session:
        session.get(OrderItem, 1).requisition_qty = 100
        session.commit()
    with TestClient(app) as client:
        _login(client, "workshop")
        received = client.put(
            "/api/incoming/receive/1",
            json={"received_quantity": 100, "idempotency_key": "card-revert"},
        )
        receipt_item_id = received.json()["receipt_item_id"]
        client.post("/api/auth/logout")
        _login(client, "finance")
        denied = client.get(
            f"/api/incoming/receipt-items/{receipt_item_id}/production-card"
        )
        client.post("/api/auth/logout")
        _login(client, "admin")
        reverted = client.put(
            f"/api/incoming/receipt-items/{receipt_item_id}/revert",
            json={},
        )
        invalid = client.get(
            f"/api/incoming/receipt-items/{receipt_item_id}/production-card"
        )

    assert denied.status_code == 403
    assert reverted.status_code == 200, reverted.text
    assert invalid.status_code == 409
    assert "已撤销" in invalid.text


def test_new_receipt_revert_restores_pending_without_changing_plan(
    incoming_api_app,
) -> None:
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.order import Order, OrderItem

    app, session_factory = incoming_api_app
    with session_factory() as session:
        session.get(OrderItem, 1).requisition_qty = 100
        session.commit()
    with TestClient(app) as client:
        _login(client, "admin")
        received = client.put(
            "/api/incoming/receive/1",
            json={"received_quantity": 100, "idempotency_key": "revert-me"},
        )
        reverted = client.put(
            f"/api/incoming/receipt-items/{received.json()['receipt_item_id']}/revert",
            json={},
        )
        pending = client.get("/api/incoming/pending")

    assert received.status_code == 200
    assert reverted.status_code == 200
    assert any(row["item_id"] == 1 for row in pending.json()["items"])
    with session_factory() as session:
        item = session.get(OrderItem, 1)
        order = session.get(Order, item.order_id)
        fact = session.get(IncomingReceiptItem, received.json()["receipt_item_id"])
        assert item.material_status == "pending"
        assert item.requisition_status == "已报料"
        assert item.material_received_at is None
        assert item.material_received_by is None
        assert item.requisition_qty == 100
        assert order.status == "pending_production"
        assert fact.status == "reversed"
        assert fact.reversal_reason == "撤回来料实收（系统记录）"


def test_reverting_final_partial_receipt_restores_previous_waiting_decision(
    incoming_api_app,
) -> None:
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.order import OrderItem

    app, session_factory = incoming_api_app
    with session_factory() as session:
        session.get(OrderItem, 1).requisition_qty = 100
        session.commit()
    with TestClient(app) as client:
        _login(client, "admin")
        first = client.put(
            "/api/incoming/receive/1",
            json={
                "received_quantity": 20,
                "resolution_action": "await_supplier",
                "idempotency_key": "revert-partial-first",
            },
        )
        final = client.put(
            "/api/incoming/receive/1",
            json={
                "received_quantity": 80,
                "idempotency_key": "revert-partial-final",
            },
        )
        reverted = client.put(
            f"/api/incoming/receipt-items/{final.json()['receipt_item_id']}/revert",
            json={"reason": "第二次实收录入错误"},
        )
        pending = client.get("/api/incoming/pending")

    assert first.status_code == 200
    assert final.status_code == 200
    assert reverted.status_code == 200
    pending_row = next(row for row in pending.json()["items"] if row["item_id"] == 1)
    assert pending_row["cumulative_received_quantity"] == 20
    assert pending_row["remaining_quantity"] == 80
    assert pending_row["resolution_status"] == "pending"
    assert pending_row["resolution_action"] == "await_supplier"
    assert pending_row["pending_receipt_item_id"] == first.json()["receipt_item_id"]
    with session_factory() as session:
        first_fact = session.get(
            IncomingReceiptItem, first.json()["receipt_item_id"]
        )
        assert first_fact.status == "posted"
        assert first_fact.resolution_status == "pending"
        assert session.get(OrderItem, 1).requisition_qty == 100


def test_only_latest_posted_fact_can_be_reverted_or_accept_short(
    incoming_api_app,
) -> None:
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.order import OrderItem

    app, session_factory = incoming_api_app
    with session_factory() as session:
        session.get(OrderItem, 1).requisition_qty = 100
        session.commit()

    with TestClient(app) as client:
        _login(client, "admin")
        first = client.put(
            "/api/incoming/receive/1",
            json={
                "received_quantity": 20,
                "resolution_action": "await_supplier",
                "idempotency_key": "fact-chain-first",
            },
        )
        second = client.put(
            "/api/incoming/receive/1",
            json={
                "received_quantity": 30,
                "resolution_action": "await_supplier",
                "idempotency_key": "fact-chain-second",
            },
        )
        stale_revert = client.put(
            f"/api/incoming/receipt-items/{first.json()['receipt_item_id']}/revert",
            json={"reason": "不应允许跨过更新事实撤销"},
        )
        stale_accept = client.put(
            f"/api/incoming/receipt-items/{first.json()['receipt_item_id']}/accept-short",
            json={"reason": "不应改写旧事实"},
        )
        accepted = client.put(
            f"/api/incoming/receipt-items/{second.json()['receipt_item_id']}/accept-short",
            json={"reason": "供应商确认余量不补"},
        )

    assert stale_revert.status_code == 409
    assert "更晚的实收记录" in stale_revert.json()["detail"]
    assert stale_accept.status_code == 409
    assert "最新一笔" in stale_accept.json()["detail"]
    assert accepted.status_code == 200
    with session_factory() as session:
        first_fact = session.get(IncomingReceiptItem, first.json()["receipt_item_id"])
        second_fact = session.get(IncomingReceiptItem, second.json()["receipt_item_id"])
        assert first_fact.status == "posted"
        assert first_fact.resolution_status == "resolved"
        assert first_fact.resolution_action == "await_supplier"
        assert first_fact.resolution_reason is None
        assert second_fact.resolution_status == "resolved"
        assert second_fact.resolution_action == "accept_short"
        assert second_fact.resolution_reason == "供应商确认余量不补"


def test_cross_customer_fact_actions_return_403_without_writes(
    incoming_api_app,
) -> None:
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.order import OrderItem
    from app.models.user import User

    app, session_factory = incoming_api_app
    with session_factory() as session:
        session.get(OrderItem, 1).requisition_qty = 100
        session.commit()

    with TestClient(app) as client:
        _login(client, "admin")
        received = client.put(
            "/api/incoming/receive/1",
            json={
                "received_quantity": 20,
                "resolution_action": "await_supplier",
                "idempotency_key": "cross-customer-fact",
            },
        )
        assert received.status_code == 200, received.text
        client.post("/api/auth/logout")
        with session_factory() as session:
            workshop = session.scalar(select(User).where(User.username == "workshop"))
            workshop.customer_access_mode = "selected"
            session.commit()
        _login(client, "workshop")
        accepted = client.put(
            f"/api/incoming/receipt-items/{received.json()['receipt_item_id']}/accept-short",
            json={"reason": "越权尝试"},
        )
        reverted = client.put(
            f"/api/incoming/receipt-items/{received.json()['receipt_item_id']}/revert",
            json={"reason": "越权尝试"},
        )

    assert accepted.status_code == 403
    assert reverted.status_code == 403
    with session_factory() as session:
        fact = session.get(IncomingReceiptItem, received.json()["receipt_item_id"])
        item = session.get(OrderItem, 1)
        assert fact.status == "posted"
        assert fact.resolution_status == "pending"
        assert fact.resolution_action == "await_supplier"
        assert item.material_status == "pending"


def test_incoming_receipt_migration_round_trip_on_copy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "n005_migration.sqlite3"
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("ERP_SECRET_KEY", "n005-migration-test")
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    current_head = ScriptDirectory.from_config(config).get_current_head()
    assert current_head is not None

    command.upgrade(config, "head")
    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        version = connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        finance_columns = {
            row[1]: row
            for row in connection.execute(
                "PRAGMA table_info(finance_return_receipt_items)"
            )
        }
        trigger_names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        }
        temporary_tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE '_alembic_tmp_%'"
        ).fetchall()
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert {"incoming_receipts", "incoming_receipt_items"} <= tables
    assert version == current_head
    assert finance_columns["resolution_action"][3] == 0
    assert finance_columns["resolution_action"][4] is None
    assert {
        "trg_finance_receipt_resolution_action_insert",
        "trg_finance_receipt_resolution_action_update",
    } <= trigger_names
    assert temporary_tables == []

    # This round-trip uses a disposable tmp_path database.  Explicitly
    # acknowledge the auth-version data-loss guard before crossing N031;
    # production downgrades must continue to fail closed without it.
    monkeypatch.setenv(
        "N031_AUTH_VERSION_DOWNGRADE_CONFIRM",
        "DOWNTIME_COMPLETE_AND_SESSION_SECRET_ROTATED",
    )
    command.downgrade(config, "aj37v7w8x9f27")
    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        finance_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(finance_return_receipt_items)"
            )
        }
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert "incoming_receipts" not in tables
    assert "incoming_receipt_items" not in tables
    assert "resolution_action" not in finance_columns

    command.upgrade(config, "an41v7w8x9j31")
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0] == "an41v7w8x9j31"


def test_migration_downgrade_refuses_to_destroy_n005_facts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "n005_migration_loss_guard.sqlite3"
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("ERP_SECRET_KEY", "n005-migration-loss-guard")
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(config, "an41v7w8x9j31")

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO incoming_receipts
                (receipt_number, status, received_at, idempotency_key)
            VALUES ('N005-LOSS-GUARD', 'posted', CURRENT_TIMESTAMP, 'loss-guard')
            """
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="N005"):
        command.downgrade(config, "aj37v7w8x9f27")

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0] == "an41v7w8x9j31"
        assert connection.execute(
            "SELECT COUNT(*) FROM incoming_receipts"
        ).fetchone()[0] == 1
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_batch_receive_rejects_invalid_quantity_and_duplicate_items(
    incoming_api_app,
) -> None:
    app, _ = incoming_api_app
    with TestClient(app) as client:
        _login(client, "admin")
        invalid_quantity = client.put(
            "/api/incoming/batch-receive",
            json={"items": [{"item_id": 1, "received_quantity": 0}]},
        )
        duplicate = client.put(
            "/api/incoming/batch-receive",
            json={
                "items": [
                    {"item_id": 1, "received_quantity": 100},
                    {"item_id": 1, "received_quantity": 100},
                ]
            },
        )

    assert invalid_quantity.status_code == 422
    assert duplicate.status_code == 200
    assert duplicate.json()["succeeded"] == 1
    assert duplicate.json()["failed"] == 1
    assert duplicate.json()["results"][1]["message"] == "同一明细不能重复提交"


@pytest.mark.parametrize("role", ["sales", "finance"])
def test_read_only_roles_cannot_receive(incoming_api_app, role: str) -> None:
    app, _ = incoming_api_app
    with TestClient(app) as client:
        _login(client, role)
        response = client.put("/api/incoming/receive/1")

    assert response.status_code == 403


def test_revert_without_reason_clears_receiving_fields_and_audits(
    incoming_api_app,
) -> None:
    from app.models.audit import OperationLog
    from app.models.order import OrderItem

    app, session_factory = incoming_api_app
    with TestClient(app) as client:
        _login(client, "admin")
        reverted = client.put(
            "/api/incoming/revert/3",
            json={},
        )

    assert reverted.status_code == 200
    with session_factory() as session:
        item = session.get(OrderItem, 3)
        details = session.scalar(
            select(OperationLog.details).where(
                OperationLog.action == "REVERT_MATERIAL",
                OperationLog.entity_id == 3,
            )
        )
    assert item.material_status == "pending"
    assert item.material_received_at is None
    assert item.material_received_by is None
    assert "撤回来料实收（系统记录）" in details
    assert '"before_status": "received"' in details
    assert '"after_status": "pending"' in details


def test_workshop_execute_permission_cannot_revert_received_fact(
    incoming_api_app,
) -> None:
    app, _ = incoming_api_app
    with TestClient(app) as client:
        _login(client, "workshop")
        response = client.put(
            "/api/incoming/revert/3",
            json={"reason": "不应由普通执行账号撤销"},
        )

    assert response.status_code == 403


def test_revert_rejects_delivered_order(incoming_api_app) -> None:
    app, _ = incoming_api_app
    with TestClient(app) as client:
        _login(client, "admin")
        response = client.put(
            "/api/incoming/revert/5",
            json={"reason": "误操作"},
        )

    assert response.status_code == 409


def test_phase6_migration_preserves_legacy_orders(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY,
                order_no TEXT NOT NULL
            );
            INSERT INTO orders VALUES (4, 'SO20260529132650852');
            """
        )
        connection.commit()

    monkeypatch.setenv("ERP_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("ERP_SECRET_KEY", "phase6-migration-test")
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(config, "head")

    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(sales_order_items)"
            )
        }
        legacy_row = connection.execute(
            "SELECT id, order_no FROM orders"
        ).fetchone()

    assert "material_received_by" in columns
    assert legacy_row == (4, "SO20260529132650852")


def test_incoming_rows_expose_material_code_flute_and_display(
    incoming_api_app,
) -> None:
    from app.models.order import OrderItem

    app, session_factory = incoming_api_app
    with session_factory() as session:
        session.get(OrderItem, 1).flute_type = "BE"
        # item 2 deliberately has no flute_type snapshot
        session.commit()
    with TestClient(app) as client:
        _login(client, "admin")
        response = client.get("/api/incoming/pending")

    assert response.status_code == 200
    items = {item["item_id"]: item for item in response.json()["items"]}
    with_flute = items[1]
    without_flute = items[2]

    assert with_flute["material_code"] == "K=A-BC"
    assert with_flute["flute_type"] == "BE"
    assert with_flute["material_display"] == "K=A-BC / BE"

    assert without_flute["material_code"] == "K=A-BC"
    assert without_flute["flute_type"] == ""
    assert without_flute["material_display"] == "K=A-BC"
    # never leak literal null/undefined into the display fields
    assert without_flute["flute_type"] is not None


def test_incoming_rows_expose_crease_snapshot_fields(incoming_api_app) -> None:
    from app.models.order import OrderItem

    app, session_factory = incoming_api_app
    with session_factory() as session:
        item = session.get(OrderItem, 1)
        item.snapshot_crease_type = "压线"
        item.snapshot_crease_left_mm = 110
        item.snapshot_crease_middle_mm = 450
        item.snapshot_crease_right_mm = 110
        session.commit()
    with TestClient(app) as client:
        _login(client, "admin")
        response = client.get("/api/incoming/pending")

    assert response.status_code == 200
    items = {item["item_id"]: item for item in response.json()["items"]}
    with_crease = items[1]
    without_crease = items[2]

    assert with_crease["snapshot_crease_left_mm"] == 110
    assert with_crease["snapshot_crease_middle_mm"] == 450
    assert with_crease["snapshot_crease_right_mm"] == 110
    assert without_crease["snapshot_crease_left_mm"] is None


def test_incoming_rows_fall_back_to_product_when_snapshot_material_missing(
    incoming_api_app,
) -> None:
    # v0.23.0 P0-4：明细快照优先；快照为空时才回退到常用箱当前的
    # flute_type / material，绝不从材质字典反查。
    from app.models.order import OrderItem
    from app.models.product import Product

    app, session_factory = incoming_api_app
    with session_factory() as session:
        item = session.get(OrderItem, 2)
        item.snapshot_material = None
        item.flute_type = None
        session.commit()
        product_id = item.product_id
        product = session.get(Product, product_id)
        product.flute_type = "BE"
        session.commit()
    with TestClient(app) as client:
        _login(client, "admin")
        response = client.get("/api/incoming/pending")

    assert response.status_code == 200
    items = {item["item_id"]: item for item in response.json()["items"]}
    fallback_item = items[2]
    assert fallback_item["flute_type"] == "BE"
    assert fallback_item["material_code"] == "K=A-BC"  # 回退到 legacy_material_text
    assert fallback_item["material_display"] == "K=A-BC / BE"


def test_incoming_rows_fall_back_to_product_when_snapshot_crease_missing(
    incoming_api_app,
) -> None:
    # v0.23.0 P0-5：压线快照优先；快照为空时回退到常用箱当前压线值。
    from app.models.order import OrderItem
    from app.models.product import Product

    app, session_factory = incoming_api_app
    with session_factory() as session:
        item = session.get(OrderItem, 2)
        product = session.get(Product, item.product_id)
        product.crease_type = "压线"
        product.crease_left_mm = 110
        product.crease_middle_mm = 450
        product.crease_right_mm = 110
        item.snapshot_crease_type = None
        session.commit()
    with TestClient(app) as client:
        _login(client, "admin")
        response = client.get("/api/incoming/pending")

    assert response.status_code == 200
    items = {item["item_id"]: item for item in response.json()["items"]}
    fallback_item = items[2]
    assert fallback_item["snapshot_crease_type"] == "压线"
    assert fallback_item["snapshot_crease_left_mm"] == 110
    assert fallback_item["snapshot_crease_middle_mm"] == 450
    assert fallback_item["snapshot_crease_right_mm"] == 110


def test_incoming_rows_prioritize_order_item_drawing_over_product_drawing(
    incoming_api_app,
) -> None:
    # v0.23.0 P0-3：订单/明细自身上传的图纸优先于常用箱图纸。
    from app.models.order import OrderItem
    from app.models.product_drawing import ProductDrawing

    app, session_factory = incoming_api_app
    with session_factory() as session:
        item = session.get(OrderItem, 1)
        session.add(
            ProductDrawing(
                product_id=item.product_id,
                image_path="/static/uploads/drawings/product-only.pdf",
                thumbnail_path="/static/uploads/drawings/product-only.pdf",
            )
        )
        session.commit()
    with TestClient(app) as client:
        _login(client, "admin")
        response = client.get("/api/incoming/pending")

    assert response.status_code == 200
    items = {item["item_id"]: item for item in response.json()["items"]}
    # item 2 只有常用箱图纸 -> 使用常用箱图纸
    assert (
        items[2]["drawing_path"]
        == "/api/master/products/drawings/1/content/original.pdf"
    )
    assert items[2]["drawing_is_pdf"] is True

    with session_factory() as session:
        item = session.get(OrderItem, 1)
        item.drawing_file = "/static/uploads/drawings/order-item.jpg"
        session.commit()
    with TestClient(app) as client:
        _login(client, "admin")
        response = client.get("/api/incoming/pending")

    items = {item["item_id"]: item for item in response.json()["items"]}
    # item 1 同时存在订单图纸与常用箱图纸 -> 订单图纸优先
    assert (
        items[1]["drawing_path"]
        == "/api/orders/items/1/drawing/content/file.jpg"
    )
    assert items[1]["drawing_is_pdf"] is False
    assert (
        items[1]["order_drawing_path"]
        == "/api/orders/items/1/drawing/content/file.jpg"
    )
    assert (
        items[1]["product_drawing_path"]
        == "/api/master/products/drawings/1/content/original.pdf"
    )


def test_no_received_state_is_changed_when_duplicate_receive_races(
    incoming_api_app,
) -> None:
    from app.models.order import OrderItem

    app, session_factory = incoming_api_app
    with session_factory() as session:
        session.query(OrderItem).filter(OrderItem.id == 1).update(
            {
                OrderItem.material_status: "received",
                OrderItem.material_received_at: datetime.now(timezone.utc).replace(tzinfo=None),
            }
        )
        session.commit()
    with TestClient(app) as client:
        _login(client, "workshop")
        response = client.put("/api/incoming/receive/1")

    assert response.status_code == 409
    with session_factory() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(OrderItem)
                .where(OrderItem.id == 1, OrderItem.material_status == "received")
            )
            == 1
        )
