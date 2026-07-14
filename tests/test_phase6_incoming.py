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
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
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
                    {"item_id": 1, "received_quantity": 95},
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
        assert received.requisition_qty == 95
        assert already_received.material_received_at is not None


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


def test_revert_requires_reason_and_clears_receiving_fields(
    incoming_api_app,
) -> None:
    from app.models.audit import OperationLog
    from app.models.order import OrderItem

    app, session_factory = incoming_api_app
    with TestClient(app) as client:
        _login(client, "admin")
        missing_reason = client.put(
            "/api/incoming/revert/3",
            json={"reason": "  "},
        )
        reverted = client.put(
            "/api/incoming/revert/3",
            json={"reason": "纸板规格核对错误"},
        )

    assert missing_reason.status_code == 422
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
    assert "纸板规格核对错误" in details


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
    assert items[2]["drawing_path"] == "/static/uploads/drawings/product-only.pdf"
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
    assert items[1]["drawing_path"] == "/static/uploads/drawings/order-item.jpg"
    assert items[1]["drawing_is_pdf"] is False
    assert items[1]["order_drawing_path"] == "/static/uploads/drawings/order-item.jpg"
    assert items[1]["product_drawing_path"] == "/static/uploads/drawings/product-only.pdf"


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
