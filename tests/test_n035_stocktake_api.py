from __future__ import annotations

from collections.abc import Generator
from datetime import date, datetime
import importlib.util
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.api import auth, stocktake
from app.api.deps import get_db
from app.core.database import create_sqlite_engine
from app.core.security import hash_password
from app.models import Base
from app.models.audit import OperationLog
from app.models.customer import Customer
from app.models.product import Product
from app.models.stocktake import StocktakeItem, StocktakeOrder, StocktakeReview
from app.models.user import User
from app.models.warehouse_inventory import (
    FinishedGoodsInventoryDetail,
    InventoryLot,
    InventoryMovement,
    WarehouseLocation,
)


PASSWORD = "StocktakeTest123!"


def _install_stocktake_guards(engine) -> None:
    migration_path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "be58v8x9z49_n035_mobile_stocktake.py"
    )
    spec = importlib.util.spec_from_file_location("n035_api_stocktake_migration", migration_path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with engine.begin() as connection:
        migration._create_sqlite_guards(connection)


@pytest.fixture()
def stocktake_api(tmp_path: Path):
    engine = create_sqlite_engine(tmp_path / "n035-stocktake-api.sqlite3")
    Base.metadata.create_all(engine)
    _install_stocktake_guards(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    with factory() as db:
        admin = User(
            username="n035-admin",
            password_hash=hash_password(PASSWORD),
            role="admin",
            real_name="盘点管理员",
            must_change_password=False,
        )
        workshop = User(
            username="n035-workshop",
            password_hash=hash_password(PASSWORD),
            role="workshop",
            real_name="盘点车间",
            must_change_password=False,
        )
        restricted = User(
            username="n035-restricted",
            password_hash=hash_password(PASSWORD),
            role="workshop",
            real_name="受限车间",
            must_change_password=False,
            customer_access_mode="selected",
        )
        customer = Customer(name="N035 测试客户")
        location = WarehouseLocation(
            location_code="N035-A-01",
            location_name="N035 盘点库位",
            warehouse_type="finished",
            is_active=True,
        )
        other_location = WarehouseLocation(
            location_code="N035-B-01",
            location_name="N035 其他库位",
            warehouse_type="finished",
            is_active=True,
        )
        unplaced_location = WarehouseLocation(
            location_code="N035-UNPLACED",
            location_name="N035 未放置库位",
            warehouse_type="finished",
            placement_status="unplaced",
            is_active=True,
        )
        db.add_all(
            [
                admin,
                workshop,
                restricted,
                customer,
                location,
                other_location,
                unplaced_location,
            ]
        )
        db.flush()
        product = Product(
            customer_id=customer.id,
            product_code="N035-P001",
            customer_material_code="N035-P001",
            product_name="N035 纸箱",
            box_category="normal",
        )
        db.add(product)
        db.flush()

        lots = [
            InventoryLot(
                lot_number="N035-LOT-1",
                inventory_type="finished",
                warehouse_location_id=location.id,
                quantity_available=10,
                quantity_reserved=2,
                quantity_consumed=1,
                quantity_damaged=0,
                quantity_scrapped=0,
                unit="boxes",
                status="active",
                source_type="manual",
                stock_date=date(2026, 7, 18),
                last_movement_at=datetime(2026, 7, 18, 1, 0),
                version=3,
                created_by=workshop.id,
            ),
            InventoryLot(
                lot_number="N035-LOT-2",
                inventory_type="finished",
                warehouse_location_id=location.id,
                quantity_available=4,
                quantity_reserved=0,
                quantity_consumed=0,
                quantity_damaged=1,
                quantity_scrapped=0,
                unit="boxes",
                status="frozen",
                source_type="manual",
                stock_date=date(2026, 7, 17),
                last_movement_at=datetime(2026, 7, 18, 2, 0),
                version=5,
                created_by=workshop.id,
            ),
            InventoryLot(
                lot_number="N035-OTHER-LOT",
                inventory_type="finished",
                warehouse_location_id=other_location.id,
                quantity_available=7,
                quantity_reserved=0,
                quantity_consumed=0,
                quantity_damaged=0,
                quantity_scrapped=0,
                unit="boxes",
                status="active",
                source_type="manual",
                stock_date=date(2026, 7, 18),
                last_movement_at=datetime(2026, 7, 18, 3, 0),
                version=1,
                created_by=workshop.id,
            ),
            InventoryLot(
                lot_number="N035-SEMI-LOT",
                inventory_type="semi_finished",
                warehouse_location_id=location.id,
                quantity_available=99,
                quantity_reserved=1,
                quantity_consumed=0,
                quantity_damaged=0,
                quantity_scrapped=0,
                unit="sheets",
                status="active",
                source_type="manual",
                stock_date=date(2026, 7, 18),
                last_movement_at=datetime(2026, 7, 18, 4, 0),
                version=2,
                created_by=workshop.id,
            ),
        ]
        db.add_all(lots)
        db.flush()
        for lot in lots[:3]:
            lot.finished_detail = FinishedGoodsInventoryDetail(
                owner_customer_id=customer.id,
                owner_customer_name_snapshot=customer.name,
                is_general=False,
                product_id=product.id,
                inventory_code_snapshot=product.product_code,
                product_name_snapshot=product.product_name,
                length_mm=500,
                width_mm=300,
                height_mm=200,
            )
        db.commit()
        ids = {
            "admin": admin.id,
            "workshop": workshop.id,
            "restricted": restricted.id,
            "location": location.id,
            "other_location": other_location.id,
            "unplaced_location": unplaced_location.id,
            "lot1": lots[0].id,
            "lot2": lots[1].id,
            "other_lot": lots[2].id,
            "semi_lot": lots[3].id,
        }

    application = FastAPI()
    application.include_router(auth.router, prefix="/api/auth")
    application.include_router(stocktake.router, prefix="/api/warehouse")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    application.dependency_overrides[get_db] = override_get_db
    try:
        yield application, factory, ids
    finally:
        engine.dispose()


def _login(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text


def _logout(client: TestClient) -> None:
    response = client.post("/api/auth/logout")
    assert response.status_code == 200


def _submission_payload(
    client: TestClient,
    location_id: int,
    *,
    key: str,
    counts: dict[int, int] | None = None,
) -> dict[str, object]:
    response = client.get(f"/api/warehouse/stocktake/locations/{location_id}")
    assert response.status_code == 200, response.text
    lots = response.json()["lots"]
    return {
        "location_id": location_id,
        "items": [
            {
                "inventory_lot_id": row["inventory_lot_id"],
                "counted_quantity": (
                    counts[row["inventory_lot_id"]]
                    if counts and row["inventory_lot_id"] in counts
                    else row["on_hand"]
                ),
                "expected_version": row["version"],
                "expected_available": row["quantity_available"],
                "expected_reserved": row["quantity_reserved"],
                "client_line_id": f"line-{row['inventory_lot_id']}",
            }
            for row in lots
        ],
        "idempotency_key": key,
    }


def _submit(
    client: TestClient,
    ids: dict[str, int],
    *,
    key: str,
    counts: dict[int, int] | None = None,
) -> dict[str, object]:
    payload = _submission_payload(client, ids["location"], key=key, counts=counts)
    response = client.post("/api/warehouse/stocktakes", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _inventory_state(factory, lot_ids: list[int]) -> dict[int, tuple[int, int, int]]:
    with factory() as db:
        return {
            lot_id: (
                db.get(InventoryLot, lot_id).quantity_available,
                db.get(InventoryLot, lot_id).quantity_reserved,
                db.get(InventoryLot, lot_id).version,
            )
            for lot_id in lot_ids
        }


@pytest.mark.parametrize(
    ("changed_field", "new_value"),
    (
        ("version", 4),
        ("quantity_available", 11),
        ("quantity_reserved", 3),
    ),
)
def test_submit_rejects_open_snapshot_drift_before_creating_order(
    stocktake_api,
    changed_field: str,
    new_value: int,
) -> None:
    application, factory, ids = stocktake_api
    with TestClient(application) as client:
        _login(client, "n035-workshop")
        payload = _submission_payload(
            client,
            ids["location"],
            key=f"n035-open-drift-{changed_field}",
        )
        with factory() as db:
            lot = db.get(InventoryLot, ids["lot1"])
            setattr(lot, changed_field, new_value)
            db.commit()

        response = client.post("/api/warehouse/stocktakes", json=payload)
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "STOCKTAKE_DRIFT"
        assert "库存已发生变化" in response.json()["detail"]["message"]

    with factory() as db:
        assert db.scalar(select(func.count(StocktakeOrder.id))) == 0
        assert db.scalar(
            select(func.count(OperationLog.id)).where(
                OperationLog.action == "STOCKTAKE_SUBMIT"
            )
        ) == 0
        assert db.scalar(select(func.count(InventoryMovement.id))) == 0


def test_countable_lots_include_only_finished_and_keep_real_specification_snapshot(
    stocktake_api,
) -> None:
    application, factory, ids = stocktake_api
    with TestClient(application) as client:
        _login(client, "n035-workshop")
        locations = client.get("/api/warehouse/stocktake/locations")
        assert locations.status_code == 200
        location = next(
            row for row in locations.json()["items"] if row["id"] == ids["location"]
        )
        assert location["lot_count"] == 2
        assert location["current_on_hand"] == 16

        detail = client.get(
            f"/api/warehouse/stocktake/locations/{ids['location']}"
        )
        assert detail.status_code == 200
        lots = detail.json()["lots"]
        assert {row["inventory_lot_id"] for row in lots} == {
            ids["lot1"],
            ids["lot2"],
        }
        assert all(row["inventory_type"] == "finished" for row in lots)
        assert all(row["specification"] == "500 × 300 × 200" for row in lots)

        order = _submit(client, ids, key="n035-finished-only-submit")
        assert {item["inventory_lot_id"] for item in order["items"]} == {
            ids["lot1"],
            ids["lot2"],
        }
        assert all(
            item["specification"] == "500 × 300 × 200"
            for item in order["items"]
        )

    with factory() as db:
        row = db.get(StocktakeOrder, order["id"])
        assert row.status == "submitted"
        assert {item.inventory_lot_id for item in row.items} == {
            ids["lot1"],
            ids["lot2"],
        }
        assert all(
            item.specification_snapshot == "500 × 300 × 200"
            for item in row.items
        )


def test_unplaced_location_is_hidden_and_cannot_start_stocktake(
    stocktake_api,
) -> None:
    application, _factory, ids = stocktake_api
    with TestClient(application) as client:
        _login(client, "n035-workshop")
        listing = client.get("/api/warehouse/stocktake/locations")
        assert listing.status_code == 200
        assert ids["unplaced_location"] not in {
            row["id"] for row in listing.json()["items"]
        }

        detail = client.get(
            f"/api/warehouse/stocktake/locations/{ids['unplaced_location']}"
        )
        assert detail.status_code == 409
        assert detail.json()["detail"]["code"] == "STOCKTAKE_LOCATION_UNPLACED"

        submit = client.post(
            "/api/warehouse/stocktakes",
            json={
                "location_id": ids["unplaced_location"],
                "items": [
                    {
                        "inventory_lot_id": ids["lot1"],
                        "counted_quantity": 8,
                        "expected_version": 3,
                        "expected_available": 10,
                        "expected_reserved": 2,
                        "client_line_id": "n081-unplaced-line",
                    }
                ],
                "idempotency_key": "n081-unplaced-stocktake",
            },
        )
        assert submit.status_code == 409
        assert submit.json()["detail"]["code"] == "STOCKTAKE_LOCATION_UNPLACED"


def test_submission_flushes_draft_items_before_transitioning_to_submitted(
    stocktake_api,
) -> None:
    application, factory, ids = stocktake_api
    observed: list[tuple[str, str]] = []

    def capture_stocktake_flush(session, _flush_context, _instances) -> None:
        for row in session.new:
            if isinstance(row, StocktakeOrder):
                observed.append(("order_new", row.status))
            elif isinstance(row, StocktakeItem):
                observed.append(("item_new", row.order.status))
        for row in session.dirty:
            if isinstance(row, StocktakeOrder):
                observed.append(("order_dirty", row.status))

    with TestClient(application) as client:
        _login(client, "n035-workshop")
        payload = _submission_payload(
            client,
            ids["location"],
            key="n035-draft-transition",
        )
        event.listen(factory.class_, "before_flush", capture_stocktake_flush)
        try:
            response = client.post("/api/warehouse/stocktakes", json=payload)
        finally:
            event.remove(factory.class_, "before_flush", capture_stocktake_flush)

    assert response.status_code == 201, response.text
    assert response.json()["status"] == "submitted"
    assert response.json()["version"] == 2
    assert ("order_new", "draft") in observed
    assert ("item_new", "draft") in observed
    assert ("order_dirty", "submitted") in observed


def test_sqlite_lock_conflict_returns_structured_409_without_creating_order(
    stocktake_api,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, factory, ids = stocktake_api
    with TestClient(application) as client:
        _login(client, "n035-workshop")
        payload = _submission_payload(
            client,
            ids["location"],
            key="n035-sqlite-locked",
        )

        def raise_locked(*_args, **_kwargs):
            raise OperationalError(
                "INSERT INTO stocktake_orders",
                {},
                RuntimeError("database is locked"),
            )

        monkeypatch.setattr(
            stocktake.stocktake_service,
            "create_stocktake",
            raise_locked,
        )
        response = client.post("/api/warehouse/stocktakes", json=payload)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "STOCKTAKE_DRIFT"
    assert "稍后" in response.json()["detail"]["message"]
    with factory() as db:
        assert db.scalar(select(func.count(StocktakeOrder.id))) == 0
        assert db.scalar(
            select(func.count(OperationLog.id)).where(
                OperationLog.action == "STOCKTAKE_SUBMIT"
            )
        ) == 0


def test_submit_does_not_change_inventory_and_requires_exact_lot_coverage(
    stocktake_api,
) -> None:
    application, factory, ids = stocktake_api
    lot_ids = [ids["lot1"], ids["lot2"]]
    before = _inventory_state(factory, lot_ids)
    with TestClient(application) as client:
        _login(client, "n035-workshop")
        payload = _submission_payload(
            client,
            ids["location"],
            key="n035-submit-complete",
            counts={ids["lot1"]: 15, ids["lot2"]: 3},
        )
        missing = {**payload, "idempotency_key": "n035-submit-missing"}
        missing["items"] = payload["items"][:-1]
        missing_response = client.post("/api/warehouse/stocktakes", json=missing)
        assert missing_response.status_code == 409
        assert missing_response.json()["detail"]["code"] == "STOCKTAKE_DRIFT"

        duplicate = {**payload, "idempotency_key": "n035-submit-duplicate"}
        duplicate["items"] = [payload["items"][0], payload["items"][0]]
        duplicate_response = client.post("/api/warehouse/stocktakes", json=duplicate)
        assert duplicate_response.status_code == 409
        assert duplicate_response.json()["detail"]["code"] == "STOCKTAKE_DRIFT"

        extra = {**payload, "idempotency_key": "n035-submit-extra"}
        extra["items"] = [
            *payload["items"],
            {
                "inventory_lot_id": ids["other_lot"],
                "counted_quantity": 7,
                "expected_version": 1,
                "expected_available": 7,
                "expected_reserved": 0,
                "client_line_id": "line-extra",
            },
        ]
        extra_response = client.post("/api/warehouse/stocktakes", json=extra)
        assert extra_response.status_code == 409
        assert extra_response.json()["detail"]["code"] == "STOCKTAKE_DRIFT"

        submitted = client.post("/api/warehouse/stocktakes", json=payload)
        assert submitted.status_code == 201, submitted.text
        assert submitted.json()["status"] == "submitted"

    assert _inventory_state(factory, lot_ids) == before
    with factory() as db:
        assert db.scalar(select(func.count(InventoryMovement.id))) == 0
        assert db.scalar(
            select(func.count(OperationLog.id)).where(
                OperationLog.action == "STOCKTAKE_SUBMIT"
            )
        ) == 1


def test_approve_applies_difference_preserves_reserved_and_writes_audit(
    stocktake_api,
) -> None:
    application, factory, ids = stocktake_api
    approval_update_order: list[str] = []

    def capture_approval_update(
        _connection,
        _cursor,
        statement: str,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        normalized = " ".join(statement.lower().split())
        if normalized.startswith("update stocktake_orders"):
            approval_update_order.append("order")
        elif normalized.startswith("update stocktake_items"):
            approval_update_order.append("item")

    with TestClient(application) as client:
        _login(client, "n035-workshop")
        order = _submit(
            client,
            ids,
            key="n035-approve-submit",
            counts={ids["lot1"]: 15, ids["lot2"]: 4},
        )
        denied = client.post(
            f"/api/warehouse/stocktakes/{order['id']}/approve",
            json={"idempotency_key": "n035-workshop-review"},
        )
        assert denied.status_code == 403

        _logout(client)
        _login(client, "n035-admin")
        engine = factory.kw["bind"]
        event.listen(engine, "before_cursor_execute", capture_approval_update)
        try:
            approved = client.post(
                f"/api/warehouse/stocktakes/{order['id']}/approve",
                json={
                    "idempotency_key": "n035-approve-review",
                    "reason": "复核无误",
                },
            )
        finally:
            event.remove(engine, "before_cursor_execute", capture_approval_update)
        assert approved.status_code == 200, approved.text
        body = approved.json()
        assert body["status"] == "approved"
        assert body["review_note"] == "复核无误"
        assert approval_update_order.index("order") < approval_update_order.index("item")

    with factory() as db:
        lot1 = db.get(InventoryLot, ids["lot1"])
        lot2 = db.get(InventoryLot, ids["lot2"])
        assert (lot1.quantity_available, lot1.quantity_reserved, lot1.version) == (13, 2, 4)
        assert (lot2.quantity_available, lot2.quantity_reserved, lot2.version) == (4, 0, 5)
        movement = db.scalar(select(InventoryMovement))
        assert movement is not None
        assert movement.movement_type == "adjust"
        assert movement.quantity == 3
        assert movement.reason == "库存盘点审核"
        assert (movement.before_available, movement.after_available) == (10, 13)
        assert (movement.before_reserved, movement.after_reserved) == (2, 2)
        reviewed = db.get(StocktakeOrder, order["id"])
        changed_item = next(
            item for item in reviewed.items if item.inventory_lot_id == ids["lot1"]
        )
        assert changed_item.adjustment_movement_id == movement.id
        assert db.scalar(select(func.count(StocktakeReview.id))) == 1
        assert db.scalar(
            select(func.count(OperationLog.id)).where(
                OperationLog.action == "STOCKTAKE_APPROVE"
            )
        ) == 1


def test_approve_rejects_count_below_reserved_without_writes(stocktake_api) -> None:
    application, factory, ids = stocktake_api
    with TestClient(application) as client:
        _login(client, "n035-workshop")
        order = _submit(
            client,
            ids,
            key="n035-below-reserved-submit",
            counts={ids["lot1"]: 1, ids["lot2"]: 4},
        )
        _logout(client)
        _login(client, "n035-admin")
        response = client.post(
            f"/api/warehouse/stocktakes/{order['id']}/approve",
            json={"idempotency_key": "n035-below-reserved-review"},
        )
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "BELOW_RESERVED"
        assert "小于已预占" in response.json()["detail"]["message"]

    with factory() as db:
        assert db.get(StocktakeOrder, order["id"]).status == "submitted"
        assert db.scalar(select(func.count(InventoryMovement.id))) == 0
        assert db.scalar(select(func.count(StocktakeReview.id))) == 0


def test_inventory_drift_returns_409_without_partial_application(stocktake_api) -> None:
    application, factory, ids = stocktake_api
    with TestClient(application) as client:
        _login(client, "n035-workshop")
        order = _submit(
            client,
            ids,
            key="n035-drift-submit",
            counts={ids["lot1"]: 18, ids["lot2"]: 8},
        )
        _logout(client)

        with factory() as db:
            drifted = db.get(InventoryLot, ids["lot2"])
            drifted.quantity_available += 1
            drifted.version += 1
            db.commit()
        lot1_before = _inventory_state(factory, [ids["lot1"]])

        _login(client, "n035-admin")
        response = client.post(
            f"/api/warehouse/stocktakes/{order['id']}/approve",
            json={"idempotency_key": "n035-drift-review"},
        )
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "STOCKTAKE_DRIFT"
        assert "库存已发生变化" in response.json()["detail"]["message"]

    assert _inventory_state(factory, [ids["lot1"]]) == lot1_before
    with factory() as db:
        assert db.get(StocktakeOrder, order["id"]).status == "submitted"
        assert db.scalar(select(func.count(InventoryMovement.id))) == 0
        assert db.scalar(select(func.count(StocktakeReview.id))) == 0


def test_reject_is_idempotent_and_never_changes_inventory(stocktake_api) -> None:
    application, factory, ids = stocktake_api
    lot_ids = [ids["lot1"], ids["lot2"]]
    before = _inventory_state(factory, lot_ids)
    with TestClient(application) as client:
        _login(client, "n035-workshop")
        order = _submit(
            client,
            ids,
            key="n035-reject-submit",
            counts={ids["lot1"]: 20, ids["lot2"]: 1},
        )
        _logout(client)
        _login(client, "n035-admin")
        request = {"idempotency_key": "n035-reject-review"}
        first = client.post(
            f"/api/warehouse/stocktakes/{order['id']}/reject", json=request
        )
        second = client.post(
            f"/api/warehouse/stocktakes/{order['id']}/reject", json=request
        )
        assert first.status_code == second.status_code == 200
        assert first.json()["status"] == second.json()["status"] == "rejected"
        assert first.json()["review_note"] == "驳回库存盘点单（系统记录）"
        reused_with_changed_reason = client.post(
            f"/api/warehouse/stocktakes/{order['id']}/reject",
            json={"idempotency_key": "n035-reject-review", "reason": "不同原因"},
        )
        assert reused_with_changed_reason.status_code == 409
        assert (
            reused_with_changed_reason.json()["detail"]["code"]
            == "IDEMPOTENCY_CONFLICT"
        )
        repeated_with_new_key = client.post(
            f"/api/warehouse/stocktakes/{order['id']}/reject",
            json={"idempotency_key": "n035-reject-new-key", "reason": "再次驳回"},
        )
        assert repeated_with_new_key.status_code == 409
        assert repeated_with_new_key.json()["detail"]["code"] == "ALREADY_REVIEWED"

    assert _inventory_state(factory, lot_ids) == before
    with factory() as db:
        assert db.scalar(select(func.count(InventoryMovement.id))) == 0
        assert db.scalar(select(func.count(StocktakeReview.id))) == 1
        assert db.scalar(
            select(func.count(OperationLog.id)).where(
                OperationLog.action == "STOCKTAKE_REJECT"
            )
        ) == 1
        review = db.scalar(select(StocktakeReview))
        assert review is not None
        assert review.reason == "驳回库存盘点单（系统记录）"


def test_submission_and_approval_are_safe_to_retry_with_same_idempotency_key(
    stocktake_api,
) -> None:
    application, factory, ids = stocktake_api
    with TestClient(application) as client:
        _login(client, "n035-workshop")
        payload = _submission_payload(
            client,
            ids["location"],
            key="n035-idempotent-submit",
            counts={ids["lot1"]: 15, ids["lot2"]: 4},
        )
        first = client.post("/api/warehouse/stocktakes", json=payload)
        second = client.post("/api/warehouse/stocktakes", json=payload)
        assert first.status_code == second.status_code == 201
        assert first.json()["id"] == second.json()["id"]

        changed = {**payload, "items": [dict(row) for row in payload["items"]]}
        changed["items"][0]["counted_quantity"] += 1
        changed_response = client.post("/api/warehouse/stocktakes", json=changed)
        assert changed_response.status_code == 409
        assert changed_response.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"

        _logout(client)
        _login(client, "n035-admin")
        review = {"idempotency_key": "n035-idempotent-approve", "reason": None}
        url = f"/api/warehouse/stocktakes/{first.json()['id']}/approve"
        approved_once = client.post(url, json=review)
        approved_twice = client.post(url, json=review)
        assert approved_once.status_code == approved_twice.status_code == 200
        assert approved_once.json()["id"] == approved_twice.json()["id"]

    with factory() as db:
        assert db.scalar(select(func.count(StocktakeOrder.id))) == 1
        assert db.scalar(select(func.count(StocktakeReview.id))) == 1
        assert db.scalar(select(func.count(InventoryMovement.id))) == 1
        assert db.scalar(
            select(func.count(OperationLog.id)).where(
                OperationLog.action == "STOCKTAKE_SUBMIT"
            )
        ) == 1
        assert db.scalar(
            select(func.count(OperationLog.id)).where(
                OperationLog.action == "STOCKTAKE_APPROVE"
            )
        ) == 1


def test_pending_stocktake_locks_location_and_history_keeps_difference_and_same(
    stocktake_api,
) -> None:
    application, _factory, ids = stocktake_api
    with TestClient(application) as client:
        _login(client, "n035-workshop")
        first = _submit(
            client,
            ids,
            key="n035-pending-first",
            counts={ids["lot1"]: 13, ids["lot2"]: 4},
        )

        location = client.get(
            f"/api/warehouse/stocktake/locations/{ids['location']}"
        )
        assert location.status_code == 200
        pending = location.json()["pending_stocktake"]
        assert pending["id"] == first["id"]
        assert pending["status"] == "submitted"
        assert pending["difference_quantity"] == 1

        duplicate_payload = _submission_payload(
            client,
            ids["location"],
            key="n035-pending-duplicate",
        )
        duplicate = client.post(
            "/api/warehouse/stocktakes", json=duplicate_payload
        )
        assert duplicate.status_code == 409
        assert duplicate.json()["detail"]["code"] == "STOCKTAKE_PENDING_REVIEW"
        assert first["order_number"] in duplicate.json()["detail"]["message"]

        _logout(client)
        _login(client, "n035-admin")
        approved = client.post(
            f"/api/warehouse/stocktakes/{first['id']}/approve",
            json={"idempotency_key": "n035-pending-approve"},
        )
        assert approved.status_code == 200, approved.text

        _logout(client)
        _login(client, "n035-workshop")
        second = _submit(client, ids, key="n035-pending-second")
        assert second["difference_quantity"] == 0

        all_rows = client.get("/api/warehouse/stocktakes")
        assert all_rows.status_code == 200
        rows = all_rows.json()["items"]
        assert {row["id"] for row in rows} == {first["id"], second["id"]}
        assert {row["difference_quantity"] for row in rows} == {0, 1}

        submitted_rows = client.get("/api/warehouse/stocktakes?status=submitted")
        assert submitted_rows.status_code == 200
        assert [row["id"] for row in submitted_rows.json()["items"]] == [
            second["id"]
        ]


def test_permissions_workshop_can_count_admin_can_review_and_scope_is_blocked(
    stocktake_api,
) -> None:
    application, _factory, ids = stocktake_api
    with TestClient(application) as client:
        _login(client, "n035-workshop")
        locations = client.get("/api/warehouse/stocktake/locations")
        assert locations.status_code == 200
        assert locations.json()["items"][0]["active_lot_count"] >= 1
        order = _submit(client, ids, key="n035-permission-submit")
        assert client.post(
            f"/api/warehouse/stocktakes/{order['id']}/approve",
            json={"idempotency_key": "n035-permission-denied"},
        ).status_code == 403

        _logout(client)
        _login(client, "n035-admin")
        assert client.post(
            f"/api/warehouse/stocktakes/{order['id']}/approve",
            json={"idempotency_key": "n035-permission-approved"},
        ).status_code == 200

        _logout(client)
        _login(client, "n035-restricted")
        assert client.get("/api/warehouse/stocktake/locations").status_code == 403
        assert client.get(
            f"/api/warehouse/stocktake/locations/{ids['location']}"
        ).status_code == 403
        restricted_payload = {
            "location_id": ids["location"],
            "items": [
                {
                    "inventory_lot_id": ids["lot1"],
                    "counted_quantity": 12,
                    "expected_version": 3,
                    "expected_available": 10,
                    "expected_reserved": 2,
                },
                {
                    "inventory_lot_id": ids["lot2"],
                    "counted_quantity": 4,
                    "expected_version": 5,
                    "expected_available": 4,
                    "expected_reserved": 0,
                },
            ],
            "idempotency_key": "n035-restricted-submit",
        }
        assert client.post(
            "/api/warehouse/stocktakes", json=restricted_payload
        ).status_code == 403
        assert client.get("/api/warehouse/stocktakes").status_code == 403
