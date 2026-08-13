from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.api.auth import router as auth_router
from app.api.deps import get_db
from app.api.incoming import router as incoming_router
from app.core.database import create_sqlite_engine
from app.core.security import hash_password
from app.models import Base
from app.models.audit import OperationLog
from app.models.customer import Customer
from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.production import ProductionTask
from app.models.user import User
from app.models.warehouse_inventory import (
    FinishedGoodsInventoryDetail,
    InventoryLot,
    InventoryReservation,
    WarehouseLocation,
)
from app.services.production_workflow import (
    ProductionWorkflowError,
    is_production_task_status_quantity_conflict,
    refresh_production_task,
)

pytestmark = pytest.mark.filterwarnings(
    "ignore:The HMAC key is 20 bytes long"
)


PASSWORD = "RolePass123!"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass(frozen=True)
class SeededCase:
    order_id: int
    order_item_id: int
    production_task_id: int
    receipt_id: int | None
    receipt_item_id: int | None


@pytest.fixture()
def p1_56_app(tmp_path: Path):
    engine = create_sqlite_engine(tmp_path / "p1-56-incoming.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(
        bind=engine,
        autoflush=False,
        expire_on_commit=False,
    )

    with factory() as db:
        admin = User(
            username="p1-56-admin",
            password_hash=hash_password(PASSWORD),
            role="admin",
            real_name="P1-56 Admin",
            display_name="P1-56 Admin",
            must_change_password=False,
            customer_access_mode="all",
        )
        customer = Customer(
            customer_number=5601,
            customer_code="P156-SAT",
            name="苏州驶安特汽车电子有限公司",
        )
        db.add_all([admin, customer])
        db.flush()
        product = Product(
            customer_id=customer.id,
            product_code="SATJITP600001",
            customer_material_code="P1-56-SAT-001",
            product_name="TSB60-JH纸箱50*37.5*31",
            legacy_material_text="K=A",
            length_mm=Decimal("495"),
            width_mm=Decimal("365"),
            height_mm=Decimal("305"),
            box_category="normal",
            supply_mode="corrugated_production",
        )
        location = WarehouseLocation(
            location_code="P1-56-FG",
            location_name="P1-56成品预占库位",
            warehouse_type="finished",
            is_active=True,
        )
        db.add_all([product, location])
        db.commit()
        ids = {
            "admin": admin.id,
            "customer": customer.id,
            "product": product.id,
            "location": location.id,
        }

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(incoming_router, prefix="/api/incoming")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield app, factory, ids
    finally:
        engine.dispose()


def _seed_case(
    db: Session,
    *,
    ids: dict[str, int],
    key: str,
    order_quantity: int = 50,
    finished_coverage: int = 30,
    material_input_quantity: int = 20,
    cutting_mode: str = "一开一",
    splice_mode: str = "single",
    pieces_per_box: int = 1,
    with_posted_receipt: bool = True,
) -> SeededCase:
    order = Order(
        order_number=f"P1-56-{key}",
        customer_id=ids["customer"],
        order_date=date.today(),
        delivery_date=date.today(),
        status="pending_production",
        payment_status="unpaid",
        total_amount=Decimal(order_quantity),
    )
    db.add(order)
    db.flush()
    item = OrderItem(
        order_id=order.id,
        product_id=ids["product"],
        item_order_number=f"P1-56-{key}-001",
        item_sequence=1,
        quantity=order_quantity,
        delivered_quantity=0,
        unit_price=Decimal("1"),
        subtotal=Decimal(order_quantity),
        material_status="received" if with_posted_receipt else "pending",
        material_received_at=_now() if with_posted_receipt else None,
        material_received_by=ids["admin"] if with_posted_receipt else None,
        snapshot_product_name="TSB60-JH纸箱50*37.5*31",
        snapshot_product_code="SATJITP600001",
        snapshot_spec="495×365×305mm",
        snapshot_material="K=A",
        supply_mode_snapshot="corrugated_production",
        requisition_qty=material_input_quantity,
        requisition_status="已入库" if with_posted_receipt else "已报料",
        special_process=cutting_mode,
        requisition_spec="1750×675mm",
        cardboard_len=Decimal("1750"),
        cardboard_width=Decimal("675"),
        snapshot_splice_mode=splice_mode,
        snapshot_pieces_per_box=pieces_per_box,
    )
    db.add(item)
    db.flush()
    task = ProductionTask(
        order_item_id=item.id,
        status="waiting_material",
        planned_quantity=0,
        finished_coverage_snapshot=0,
        ordered_quantity_snapshot=order_quantity,
        material_received_quantity=0,
        material_input_quantity=0,
        output_factor=1,
        readiness_basis=None,
        ready_at=None,
        version=1,
    )
    lot = InventoryLot(
        lot_number=f"P1-56-FG-{key}",
        inventory_type="finished",
        warehouse_location_id=ids["location"],
        quantity_available=0,
        quantity_reserved=finished_coverage,
        quantity_consumed=0,
        quantity_damaged=0,
        quantity_scrapped=0,
        unit="boxes",
        status="active",
        source_type="manual",
        stock_date=date.today(),
        last_movement_at=_now(),
        version=2,
    )
    db.add_all([task, lot])
    db.flush()
    db.add(
        FinishedGoodsInventoryDetail(
            inventory_lot_id=lot.id,
            owner_customer_id=ids["customer"],
            owner_customer_name_snapshot="苏州驶安特汽车电子有限公司",
            is_general=False,
            product_id=ids["product"],
            inventory_code_snapshot="SATJITP600001",
            product_name_snapshot="TSB60-JH纸箱50*37.5*31",
        )
    )
    db.add(
        InventoryReservation(
            reservation_number=f"P1-56-RS-{key}",
            inventory_lot_id=lot.id,
            reservation_type="finished_order",
            order_id=order.id,
            order_item_id=item.id,
            reserved_stock_quantity=finished_coverage,
            credited_requirement_quantity=finished_coverage,
            yield_factor=1,
            consumed_stock_quantity=0,
            released_stock_quantity=0,
            consumed_requirement_quantity=0,
            released_requirement_quantity=0,
            status="active",
            reserved_at=_now(),
            idempotency_key=f"p1-56-reserve-{key}",
        )
    )

    receipt_id: int | None = None
    receipt_item_id: int | None = None
    if with_posted_receipt:
        receipt = IncomingReceipt(
            receipt_number=f"P1-56-IR-{key}",
            status="posted",
            received_at=_now(),
            received_by=ids["admin"],
            idempotency_key=f"p1-56-receipt-{key}",
        )
        db.add(receipt)
        db.flush()
        fact = IncomingReceiptItem(
            receipt_id=receipt.id,
            order_id=order.id,
            order_item_id=item.id,
            planned_quantity=material_input_quantity,
            received_quantity=material_input_quantity,
            cumulative_received_quantity=material_input_quantity,
            variance_quantity=0,
            variance_type="matched",
            resolution_status="not_required",
            resolution_action=None,
            status="posted",
        )
        db.add(fact)
        db.flush()
        receipt_id = receipt.id
        receipt_item_id = fact.id

    db.flush()
    return SeededCase(
        order_id=order.id,
        order_item_id=item.id,
        production_task_id=task.id,
        receipt_id=receipt_id,
        receipt_item_id=receipt_item_id,
    )


def _login(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": "p1-56-admin", "password": PASSWORD},
    )
    assert response.status_code == 200, response.text


def test_refresh_uses_received_twenty_after_finished_coverage_thirty(
    p1_56_app,
) -> None:
    _app, factory, ids = p1_56_app
    with factory() as db:
        case = _seed_case(
            db,
            ids=ids,
            key="sat-50-fg30-in20",
            order_quantity=50,
            finished_coverage=30,
            material_input_quantity=20,
        )
        fact = db.get(IncomingReceiptItem, case.receipt_item_id)
        assert fact is not None
        assert (fact.planned_quantity, fact.received_quantity) == (20, 20)

        task = refresh_production_task(db, case.order_item_id)

        assert task is not None
        assert task.status == "pending"
        assert task.planned_quantity == 20
        assert task.planned_quantity not in {0, 50}
        assert task.finished_coverage_snapshot == 30
        assert task.ordered_quantity_snapshot == 50
        assert task.material_received_quantity == 20
        assert task.material_input_quantity == 20
        assert task.output_factor == 1
        assert task.readiness_basis == "incoming_receipt"


@pytest.mark.parametrize(
    (
        "key",
        "cutting_mode",
        "splice_mode",
        "pieces_per_box",
        "material_input_quantity",
        "expected_output_factor",
    ),
    [
        ("one-cut-two", "一开二", "single", 1, 10, 2),
        ("double-splice", "一开一", "double", 2, 40, 1),
    ],
)
def test_finished_coverage_does_not_regress_cutting_or_splice_conversion(
    p1_56_app,
    key: str,
    cutting_mode: str,
    splice_mode: str,
    pieces_per_box: int,
    material_input_quantity: int,
    expected_output_factor: int,
) -> None:
    _app, factory, ids = p1_56_app
    with factory() as db:
        case = _seed_case(
            db,
            ids=ids,
            key=key,
            order_quantity=50,
            finished_coverage=30,
            material_input_quantity=material_input_quantity,
            cutting_mode=cutting_mode,
            splice_mode=splice_mode,
            pieces_per_box=pieces_per_box,
        )

        task = refresh_production_task(db, case.order_item_id)

        assert task is not None
        assert task.status == "pending"
        assert task.planned_quantity == 20
        assert task.finished_coverage_snapshot == 30
        assert task.material_received_quantity == material_input_quantity
        assert task.material_input_quantity == material_input_quantity
        assert task.output_factor == expected_output_factor
        assert task.readiness_basis == "incoming_receipt"


def test_zero_converted_output_is_a_business_409_not_an_integrity_error(
    p1_56_app,
) -> None:
    _app, factory, ids = p1_56_app
    with factory() as db:
        case = _seed_case(
            db,
            ids=ids,
            key="double-splice-zero-output",
            order_quantity=2,
            finished_coverage=1,
            material_input_quantity=1,
            cutting_mode="一开一",
            splice_mode="double",
            pieces_per_box=2,
        )
        db.commit()

        with pytest.raises(ProductionWorkflowError) as raised:
            refresh_production_task(db, case.order_item_id)

        assert raised.value.status_code == 409
        assert "不足1只" in str(raised.value)
        db.rollback()
        task = db.get(ProductionTask, case.production_task_id)
        assert task is not None
        assert task.status == "waiting_material"
        assert task.planned_quantity == 0


def test_only_the_expected_production_constraint_is_classified() -> None:
    expected = IntegrityError(
        "statement",
        {},
        RuntimeError(
            "CHECK constraint failed: ck_production_tasks_status_quantity"
        ),
    )
    unrelated = IntegrityError(
        "statement",
        {},
        RuntimeError("UNIQUE constraint failed: incoming_receipts.idempotency_key"),
    )

    assert is_production_task_status_quantity_conflict(expected) is True
    assert is_production_task_status_quantity_conflict(unrelated) is False


def test_receive_api_posts_once_and_is_idempotent_with_finished_coverage(
    p1_56_app,
) -> None:
    app, factory, ids = p1_56_app
    with factory() as db:
        case = _seed_case(
            db,
            ids=ids,
            key="api-sat-50-fg30-in20",
            order_quantity=50,
            finished_coverage=30,
            material_input_quantity=20,
            with_posted_receipt=False,
        )
        db.commit()

    payload = {
        "received_quantity": 20,
        "idempotency_key": "p1-56-api-sat-50-fg30-in20",
    }
    with TestClient(app, raise_server_exceptions=False) as client:
        _login(client)
        first = client.put(
            f"/api/incoming/receive/{case.order_item_id}",
            json=payload,
        )
        repeated = client.put(
            f"/api/incoming/receive/{case.order_item_id}",
            json=payload,
        )

    assert first.status_code == 200, first.text
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["receipt_item_id"] == first.json()["receipt_item_id"]
    with factory() as db:
        assert (
            db.scalar(select(func.count(IncomingReceipt.id)))
            == 1
        )
        assert (
            db.scalar(select(func.count(IncomingReceiptItem.id)))
            == 1
        )
        assert (
            db.scalar(
                select(func.count(OperationLog.id)).where(
                    OperationLog.module_code == "incoming",
                    OperationLog.action_code == "incoming.receive",
                )
            )
            == 1
        )
        item = db.get(OrderItem, case.order_item_id)
        task = db.get(ProductionTask, case.production_task_id)
        assert item is not None
        assert item.material_status == "received"
        assert item.requisition_status == "已入库"
        assert task is not None
        assert task.status == "pending"
        assert task.planned_quantity == 20
        assert task.finished_coverage_snapshot == 30
        assert task.material_received_quantity == 20
        assert task.material_input_quantity == 20
        assert task.readiness_basis == "incoming_receipt"
