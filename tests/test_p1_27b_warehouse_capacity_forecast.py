from __future__ import annotations

from collections.abc import Generator
from datetime import date, datetime, timedelta
from decimal import Decimal
import importlib.util
from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker


ROOT = Path(__file__).resolve().parents[1]
PARENT_REVISION = "du03v8x9z92"
TARGET_REVISION = "dv04v8x9z93"
MIGRATION = ROOT / "alembic" / "versions" / "dv04v8x9z93_warehouse_capacity_forecast_plans.py"


def _config(monkeypatch: pytest.MonkeyPatch, path: Path) -> Config:
    monkeypatch.setenv("ERP_DATABASE_PATH", str(path))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path.as_posix()}")
    return config


def _checks(path: Path) -> tuple[str, int]:
    with sqlite3.connect(path) as connection:
        return (
            connection.execute("PRAGMA integrity_check").fetchone()[0],
            len(connection.execute("PRAGMA foreign_key_check").fetchall()),
        )


def test_forecast_migration_is_linear() -> None:
    spec = importlib.util.spec_from_file_location("warehouse_capacity_forecast", MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == TARGET_REVISION
    assert module.down_revision == PARENT_REVISION


def test_forecast_migration_round_trip_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "forecast-migration.sqlite3"
    config = _config(monkeypatch, path)
    command.upgrade(config, PARENT_REVISION)
    command.upgrade(config, TARGET_REVISION)
    assert _checks(path) == ("ok", 0)
    command.downgrade(config, PARENT_REVISION)
    command.upgrade(config, TARGET_REVISION)
    with sqlite3.connect(path) as connection:
        floor_id = connection.execute(
            "SELECT id FROM warehouse_floors WHERE floor_number=1"
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO warehouse_capacity_forecast_plans "
            "(source_type,source_id,source_number_snapshot,source_label_snapshot,effect,"
            "floor_id,scope_key,planned_date,pallet_slots,status,version,last_operation_key,"
            "last_request_hash) VALUES "
            "('delivery',1,'D1','测试送货','outflow',?,'floor:' || ?,date('now'),1,'active',1,"
            "'migration-test','0000000000000000000000000000000000000000000000000000000000000000')",
            (floor_id, floor_id),
        )
        connection.commit()
    with pytest.raises(RuntimeError, match="禁止破坏性降级"):
        command.downgrade(config, PARENT_REVISION)
    assert _checks(path) == ("ok", 0)


@pytest.fixture()
def forecast_app(tmp_path):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.warehouse import router as warehouse_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.customer import Customer
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.supplier_requisition_order import (
        SupplierRequisitionOrder,
        SupplierRequisitionOrderItem,
    )
    from app.models.user import User
    from app.models.warehouse_inventory import WarehouseArea, WarehouseFloor

    engine = create_sqlite_engine(tmp_path / "forecast-api.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    today = date.today()
    with factory() as db:
        admin = User(
            username="forecast-admin",
            password_hash=hash_password("123456"),
            role="admin",
            real_name="预测管理员",
            must_change_password=False,
        )
        worker = User(
            username="forecast-worker",
            password_hash=hash_password("123456"),
            role="workshop",
            real_name="普通员工",
            must_change_password=False,
        )
        db.add_all([admin, worker])
        customer = Customer(name="预测测试客户")
        db.add(customer)
        db.flush()
        product = Product(
            customer_id=customer.id,
            product_code="FORECAST-BOX",
            customer_material_code="FORECAST-BOX",
            product_name="预测测试纸箱",
        )
        db.add(product)
        db.flush()
        order = Order(
            order_number="TM-FORECAST-001",
            customer_id=customer.id,
            order_date=today,
            delivery_date=today + timedelta(days=2),
            status="pending_production",
            total_amount=Decimal("100"),
        )
        db.add(order)
        db.flush()
        order_items = [
            OrderItem(
                order_id=order.id,
                product_id=product.id,
                item_order_number=f"TM-FORECAST-001-{index:03d}",
                quantity=quantity,
                unit_price=Decimal("1"),
                subtotal=Decimal(quantity),
                snapshot_product_name=f"预测纸箱{index}",
                requisition_qty=quantity,
                requisition_status="供应商已排单",
            )
            for index, quantity in enumerate((60, 40), start=1)
        ]
        db.add_all(order_items)
        floor1 = WarehouseFloor(
            floor_code="1F",
            floor_name="一楼周转仓",
            floor_number=1,
            construction_status="enabled",
            planning_reference_pallet_capacity=10,
        )
        floor3 = WarehouseFloor(
            floor_code="3F",
            floor_name="三楼成品仓",
            floor_number=3,
            construction_status="enabled",
            planning_reference_pallet_capacity=20,
        )
        db.add_all([floor1, floor3])
        db.flush()
        db.add_all(
            [
                WarehouseArea(
                    floor_id=floor1.id,
                    area_code="D2",
                    area_name="一楼成品区",
                    planned_location_count=10,
                    planned_pallet_capacity=10,
                    construction_status="enabled",
                    capacity_review_status="confirmed",
                    capacity_eligible=True,
                    confirmed_pallet_capacity=10,
                    capacity_reviewed_by="forecast-admin",
                    capacity_reviewed_at=datetime(2026, 8, 10, 8, 0, 0),
                ),
                WarehouseArea(
                    floor_id=floor3.id,
                    area_code="A1",
                    area_name="三楼成品区",
                    planned_location_count=20,
                    planned_pallet_capacity=20,
                    construction_status="enabled",
                    capacity_review_status="confirmed",
                    capacity_eligible=True,
                    confirmed_pallet_capacity=20,
                    capacity_reviewed_by="forecast-admin",
                    capacity_reviewed_at=datetime(2026, 8, 10, 8, 0, 0),
                ),
            ]
        )
        source = SupplierRequisitionOrder(
            order_number="BL-FORECAST-001",
            supplier_name="鸣朋",
            total_quantity=120,
            requisition_qty=100,
            stock_deduction_qty=20,
            status="confirmed",
            created_at=datetime.now() - timedelta(days=90),
        )
        db.add(source)
        db.flush()
        supplier_items = [
            SupplierRequisitionOrderItem(
                supplier_order_id=source.id,
                order_item_id=order_item.id,
                order_number=order_item.item_order_number,
                product_code=product.product_code,
                product_name=order_item.snapshot_product_name,
                quantity=quantity + 10,
                requisition_qty=quantity,
                delivery_date=today + timedelta(days=index),
            )
            for index, (order_item, quantity) in enumerate(
                zip(order_items, (60, 40), strict=True), start=1
            )
        ]
        db.add_all(supplier_items)
        db.commit()
        ids = {
            "source": source.id,
            "floor": floor1.id,
            "supplier_items": [item.id for item in supplier_items],
            "order_items": [item.id for item in order_items],
            "order": order.id,
        }

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(warehouse_router, prefix="/api/warehouse")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield app, ids, today, factory
    finally:
        engine.dispose()


def _login(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/auth/login", json={"username": username, "password": "123456"}
    )
    assert response.status_code == 200, response.text


def _post_supplier_receipt(
    factory,
    ids: dict,
    *,
    item_index: int,
    quantity: int,
    receipt_key: str,
) -> tuple[int, int]:
    from sqlalchemy import func, select

    from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem
    from app.models.supplier_requisition_order import SupplierRequisitionOrderItem

    with factory() as db:
        supplier_item = db.get(
            SupplierRequisitionOrderItem, ids["supplier_items"][item_index]
        )
        cumulative = int(
            db.scalar(
                select(func.coalesce(func.sum(IncomingReceiptItem.received_quantity), 0))
                .where(
                    IncomingReceiptItem.supplier_order_item_id == supplier_item.id,
                    IncomingReceiptItem.status == "posted",
                )
            )
            or 0
        ) + int(quantity)
        planned = int(supplier_item.requisition_qty)
        variance = cumulative - planned
        variance_type = "matched" if variance == 0 else "short" if variance < 0 else "over"
        receipt = IncomingReceipt(
            receipt_number=f"IR-{receipt_key}",
            status="posted",
            received_at=datetime.now(),
            idempotency_key=receipt_key,
        )
        db.add(receipt)
        db.flush()
        fact = IncomingReceiptItem(
            receipt_id=receipt.id,
            order_id=ids["order"],
            order_item_id=ids["order_items"][item_index],
            supplier_order_id=ids["source"],
            supplier_order_item_id=supplier_item.id,
            planned_quantity=planned,
            received_quantity=quantity,
            cumulative_received_quantity=cumulative,
            variance_quantity=variance,
            variance_type=variance_type,
            resolution_status="pending" if variance < 0 else "resolved",
            resolution_action=(
                "await_supplier"
                if variance < 0
                else "all_to_production" if variance > 0 else None
            ),
            status="posted",
        )
        db.add(fact)
        db.commit()
        return receipt.id, fact.id


def _mark_legacy_received(factory, ids: dict, *item_indexes: int) -> None:
    from app.models.order import OrderItem

    with factory() as db:
        for item_index in item_indexes:
            item = db.get(OrderItem, ids["order_items"][item_index])
            item.material_status = "received"
            item.requisition_status = "已入库"
            item.material_received_at = datetime.now()
        db.commit()


def test_missing_source_is_not_guessed_and_admin_plan_drives_forecast(forecast_app) -> None:
    app, ids, today, _factory = forecast_app
    with TestClient(app) as client:
        _login(client, "forecast-admin")
        initial = client.get("/api/warehouse/capacity/forecast?horizon=7")
        assert initial.status_code == 200, initial.text
        payload = initial.json()
        assert payload["forecast_complete"] is False
        assert payload["missing_sources"][0]["source_number"] == "BL-FORECAST-001"
        assert "猜成栈板数" in payload["notice"]

        operation_key = "forecast-save-0001"
        body = {
            "source_type": "supplier_requisition",
            "source_id": ids["source"],
            "effect": "inflow",
            "floor_id": ids["floor"],
            "planned_date": (today + timedelta(days=1)).isoformat(),
            "pallet_slots": 9,
            "operation_key": operation_key,
        }
        saved = client.post("/api/warehouse/capacity/forecast-plans", json=body)
        assert saved.status_code == 200, saved.text
        assert saved.json()["replayed"] is False
        replayed = client.post("/api/warehouse/capacity/forecast-plans", json=body)
        assert replayed.status_code == 200, replayed.text
        assert replayed.json()["replayed"] is True

        projected = client.get("/api/warehouse/capacity/forecast?horizon=7").json()
        floor = next(row for row in projected["floors"] if row["floor_code"] == "1F")
        assert floor["peak_occupied"] == 9
        assert floor["peak_utilization_percent"] == 90.0
        assert floor["threshold_crossings"]["80"] == (today + timedelta(days=1)).isoformat()
        assert floor["threshold_crossings"]["90"] == (today + timedelta(days=1)).isoformat()
        assert projected["missing_sources"] == []
        assert projected["forecast_complete"] is True
        home = client.get("/api/warehouse/capacity/summary").json()
        assert home["forecast_7d_peak_floor_code"] == "1F"
        assert home["forecast_7d_peak_utilization_percent"] == 90.0
        assert home["forecast_7d_action_count"] >= 1


def test_forecast_and_home_metrics_wait_until_every_area_is_confirmed(
    forecast_app,
) -> None:
    from app.models.warehouse_inventory import WarehouseArea

    app, _ids, _today, factory = forecast_app
    with factory() as db:
        area = db.scalar(
            select(WarehouseArea).where(WarehouseArea.area_code == "A1")
        )
        area.capacity_review_status = "pending"
        area.capacity_eligible = False
        area.confirmed_pallet_capacity = None
        area.capacity_reviewed_by = None
        area.capacity_reviewed_at = None
        db.commit()

    with TestClient(app) as client:
        _login(client, "forecast-admin")
        forecast = client.get("/api/warehouse/capacity/forecast?horizon=7").json()
        home = client.get("/api/warehouse/capacity/summary").json()

    assert forecast["confirmed"] is False
    assert forecast["floors"] == []
    assert forecast["plans"] == []
    assert forecast["missing_sources"] == []
    assert forecast["actions"] == []
    assert "预测暂不发布" in forecast["notice"]
    assert home["confirmed"] is False
    assert home["planned_pallet_capacity"] == 30
    assert home["reference_pallet_capacity"] is None
    assert home["empty_pallet_slots"] is None
    assert home["utilization_percent"] is None
    assert home["forecast_7d_peak_floor_code"] is None
    assert home["forecast_7d_peak_utilization_percent"] is None
    assert home["forecast_7d_action_count"] == 0


def test_forecast_write_is_admin_only(forecast_app) -> None:
    app, ids, today, _factory = forecast_app
    with TestClient(app) as client:
        _login(client, "forecast-worker")
        response = client.post(
            "/api/warehouse/capacity/forecast-plans",
            json={
                "source_type": "supplier_requisition",
                "source_id": ids["source"],
                "effect": "inflow",
                "floor_id": ids["floor"],
                "planned_date": today.isoformat(),
                "pallet_slots": 1,
                "operation_key": "forecast-denied-01",
            },
        )
    assert response.status_code == 403


def test_supplier_receipts_reduce_candidates_and_stale_existing_plan(forecast_app) -> None:
    from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem
    from app.services.warehouse_capacity_forecast import (
        resolve_capacity_forecast_source,
    )

    app, ids, today, factory = forecast_app
    with TestClient(app) as client:
        _login(client, "forecast-admin")
        initial = client.get("/api/warehouse/capacity/forecast?horizon=7").json()
        source = initial["missing_sources"][0]
        assert source["planned_quantity"] == 100
        assert source["posted_received_quantity"] == 0
        assert source["received_quantity"] == 0
        assert source["remaining_quantity"] == 100

        _post_supplier_receipt(
            factory,
            ids,
            item_index=0,
            quantity=30,
            receipt_key="forecast-partial-30",
        )
        with factory() as db:
            resolved = resolve_capacity_forecast_source(
                db, "supplier_requisition", ids["source"]
            )
        assert resolved is not None
        assert resolved["planned_quantity"] == 100
        assert resolved["posted_received_quantity"] == 30
        assert resolved["received_quantity"] == 30
        assert resolved["remaining_quantity"] == 70
        assert resolved["reference_date"] == today + timedelta(days=1)
        assert resolved["valid"] is True

        partial = client.get("/api/warehouse/capacity/forecast?horizon=7").json()
        assert partial["missing_sources"][0]["remaining_quantity"] == 70
        saved = client.post(
            "/api/warehouse/capacity/forecast-plans",
            json={
                "source_type": "supplier_requisition",
                "source_id": ids["source"],
                "effect": "inflow",
                "floor_id": ids["floor"],
                "planned_date": (today + timedelta(days=1)).isoformat(),
                "pallet_slots": 4,
                "operation_key": "forecast-plan-for-remaining-70",
            },
        )
        assert saved.status_code == 200, saved.text

        _post_supplier_receipt(
            factory,
            ids,
            item_index=0,
            quantity=40,
            receipt_key="forecast-over-first-line",
        )
        changed = client.get("/api/warehouse/capacity/forecast?horizon=7").json()
        assert changed["missing_sources"][0]["posted_received_quantity"] == 70
        assert changed["missing_sources"][0]["received_quantity"] == 60
        assert changed["missing_sources"][0]["remaining_quantity"] == 40
        assert changed["missing_sources"][0]["reference_date"] == (
            today + timedelta(days=2)
        ).isoformat()
        assert changed["stale_plans"][0]["source_valid"] is True
        assert changed["stale_plans"][0]["source_snapshot_current"] is False
        assert (
            changed["stale_plans"][0]["stale_reason"]
            == "supplier_remaining_quantity_changed"
        )
        floor = next(row for row in changed["floors"] if row["floor_code"] == "1F")
        assert floor["peak_occupied"] == 0

        receipt_id, receipt_item_id = _post_supplier_receipt(
            factory,
            ids,
            item_index=1,
            quantity=40,
            receipt_key="forecast-final-second-line",
        )
        fully_received = client.get(
            "/api/warehouse/capacity/forecast?horizon=7"
        ).json()
        assert fully_received["missing_sources"] == []
        assert fully_received["stale_plans"][0]["source_valid"] is False
        assert fully_received["stale_plans"][0]["stale_reason"] == "source_invalid"
        with factory() as db:
            resolved = resolve_capacity_forecast_source(
                db, "supplier_requisition", ids["source"]
            )
            assert resolved is not None
            assert resolved["remaining_quantity"] == 0
            assert resolved["valid"] is False

            db.get(IncomingReceiptItem, receipt_item_id).status = "reversed"
            db.get(IncomingReceipt, receipt_id).status = "reversed"
            db.commit()

        restored = client.get("/api/warehouse/capacity/forecast?horizon=7").json()
        assert restored["missing_sources"][0]["remaining_quantity"] == 40
        assert restored["stale_plans"][0]["source_valid"] is True
        assert (
            restored["stale_plans"][0]["stale_reason"]
            == "supplier_remaining_quantity_changed"
        )

        refreshed = client.post(
            "/api/warehouse/capacity/forecast-plans",
            json={
                "source_type": "supplier_requisition",
                "source_id": ids["source"],
                "effect": "inflow",
                "floor_id": ids["floor"],
                "planned_date": (today + timedelta(days=2)).isoformat(),
                "pallet_slots": 2,
                "expected_version": 1,
                "operation_key": "forecast-refresh-for-remaining-40",
            },
        )
        assert refreshed.status_code == 200, refreshed.text
        current = client.get("/api/warehouse/capacity/forecast?horizon=7").json()
        assert current["missing_sources"] == []
        assert current["stale_plans"] == []
        floor = next(row for row in current["floors"] if row["floor_code"] == "1F")
        assert floor["peak_occupied"] == 2


def test_legacy_completed_supplier_lines_are_excluded_without_fake_receipts(
    forecast_app,
) -> None:
    from app.services.warehouse_capacity_forecast import (
        resolve_capacity_forecast_source,
    )

    app, ids, _today, factory = forecast_app
    _mark_legacy_received(factory, ids, 0, 1)

    with factory() as db:
        resolved = resolve_capacity_forecast_source(
            db, "supplier_requisition", ids["source"]
        )
    assert resolved is not None
    assert resolved["planned_quantity"] == 100
    assert resolved["posted_received_quantity"] == 0
    assert resolved["received_quantity"] == 0
    assert resolved["legacy_completed"] is True
    assert resolved["legacy_completed_quantity"] == 100
    assert resolved["legacy_completion_label"] == "旧流程已入库 100 张"
    assert resolved["remaining_quantity"] == 0
    assert resolved["valid"] is False

    with TestClient(app) as client:
        _login(client, "forecast-admin")
        payload = client.get("/api/warehouse/capacity/forecast?horizon=7").json()
    assert payload["missing_sources"] == []


def test_partially_legacy_completed_supplier_order_keeps_only_open_line(
    forecast_app,
) -> None:
    from app.services.warehouse_capacity_forecast import (
        resolve_capacity_forecast_source,
    )

    app, ids, today, factory = forecast_app
    _mark_legacy_received(factory, ids, 0)

    with factory() as db:
        resolved = resolve_capacity_forecast_source(
            db, "supplier_requisition", ids["source"]
        )
    assert resolved is not None
    assert resolved["posted_received_quantity"] == 0
    assert resolved["received_quantity"] == 0
    assert resolved["legacy_completed_quantity"] == 60
    assert resolved["remaining_quantity"] == 40
    assert resolved["reference_date"] == today + timedelta(days=2)
    assert resolved["source_label"].endswith("旧流程已入库 60 张")
    assert resolved["valid"] is True

    with TestClient(app) as client:
        _login(client, "forecast-admin")
        source = client.get(
            "/api/warehouse/capacity/forecast?horizon=7"
        ).json()["missing_sources"][0]
    assert source["legacy_completed"] is True
    assert source["legacy_completed_quantity"] == 60
    assert source["legacy_completion_label"] == "旧流程已入库 60 张"
    assert source["remaining_quantity"] == 40


def test_modern_receipt_quantity_wins_and_reversal_restores_legacy_completion(
    forecast_app,
) -> None:
    from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem
    from app.services.warehouse_capacity_forecast import (
        resolve_capacity_forecast_source,
    )

    _app, ids, _today, factory = forecast_app
    _mark_legacy_received(factory, ids, 0)
    receipt_id, receipt_item_id = _post_supplier_receipt(
        factory,
        ids,
        item_index=0,
        quantity=30,
        receipt_key="forecast-modern-priority",
    )

    with factory() as db:
        modern = resolve_capacity_forecast_source(
            db, "supplier_requisition", ids["source"]
        )
    assert modern is not None
    assert modern["posted_received_quantity"] == 30
    assert modern["received_quantity"] == 30
    assert modern["legacy_completed"] is False
    assert modern["legacy_completed_quantity"] == 0
    assert modern["legacy_completion_label"] is None
    assert modern["remaining_quantity"] == 70

    with factory() as db:
        db.get(IncomingReceiptItem, receipt_item_id).status = "reversed"
        db.get(IncomingReceipt, receipt_id).status = "reversed"
        db.commit()
        restored = resolve_capacity_forecast_source(
            db, "supplier_requisition", ids["source"]
        )
    assert restored is not None
    assert restored["posted_received_quantity"] == 0
    assert restored["received_quantity"] == 0
    assert restored["legacy_completed"] is True
    assert restored["legacy_completed_quantity"] == 60
    assert restored["remaining_quantity"] == 40


def test_legacy_completion_requires_all_three_order_item_fields(forecast_app) -> None:
    from app.models.order import OrderItem
    from app.services.warehouse_capacity_forecast import (
        resolve_capacity_forecast_source,
    )

    _app, ids, _today, factory = forecast_app
    with factory() as db:
        item = db.get(OrderItem, ids["order_items"][0])
        item.material_status = "received"
        item.requisition_status = "已入库"
        item.material_received_at = None
        db.commit()
        incomplete = resolve_capacity_forecast_source(
            db, "supplier_requisition", ids["source"]
        )
        item.material_received_at = datetime.now()
        db.commit()
        complete = resolve_capacity_forecast_source(
            db, "supplier_requisition", ids["source"]
        )
    assert incomplete is not None
    assert incomplete["legacy_completed_quantity"] == 0
    assert incomplete["remaining_quantity"] == 100
    assert complete is not None
    assert complete["legacy_completed_quantity"] == 60
    assert complete["remaining_quantity"] == 40


def test_published_floor4_is_included_in_capacity_summary_and_forecast(
    forecast_app,
) -> None:
    from app.models.warehouse_inventory import WarehouseArea, WarehouseFloor

    app, ids, today, factory = forecast_app
    with factory() as db:
        floor4 = WarehouseFloor(
            floor_code="4F",
            floor_name="四楼成品仓",
            floor_number=4,
            construction_status="enabled",
            planning_reference_pallet_capacity=12,
        )
        db.add(floor4)
        db.flush()
        db.add(
            WarehouseArea(
                floor_id=floor4.id,
                area_code="N1",
                area_name="四楼成品区",
                planned_location_count=12,
                planned_pallet_capacity=12,
                construction_status="enabled",
                capacity_review_status="confirmed",
                capacity_eligible=True,
                confirmed_pallet_capacity=12,
                capacity_reviewed_by="forecast-admin",
                capacity_reviewed_at=datetime(2026, 9, 1, 8, 0, 0),
            )
        )
        db.commit()
        floor4_id = int(floor4.id)

    with TestClient(app) as client:
        _login(client, "forecast-admin")
        summary = client.get("/api/warehouse/capacity/summary").json()
        forecast = client.get(
            "/api/warehouse/capacity/forecast?horizon=7"
        ).json()
        saved = client.post(
            "/api/warehouse/capacity/forecast-plans",
            json={
                "source_type": "supplier_requisition",
                "source_id": ids["source"],
                "effect": "inflow",
                "floor_id": floor4_id,
                "planned_date": (today + timedelta(days=1)).isoformat(),
                "pallet_slots": 2,
                "operation_key": "forecast-floor4-0001",
            },
        )

    assert {row["floor_code"] for row in summary["floors"]} == {
        "1F",
        "3F",
        "4F",
    }
    assert {row["floor_code"] for row in forecast["floors"]} == {
        "1F",
        "3F",
        "4F",
    }
    assert saved.status_code == 200, saved.text
    assert saved.json()["plan"]["floor_code"] == "4F"
