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
