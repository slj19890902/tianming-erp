from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request


def _request(path: str) -> Request:
    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 18179),
            "server": ("127.0.0.1", 18179),
        }
    )
    request.state.request_id = "q1-02-production-audit"
    return request


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture()
def production_audit_db(tmp_path: Path):
    from app.core.database import create_sqlite_engine
    from app.models import Base
    from app.models.customer import Customer
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.production import ProductionTask
    from app.models.user import User
    from app.models.warehouse_inventory import WarehouseLocation

    engine = create_sqlite_engine(tmp_path / "production-audit.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        user = User(
            username="production-audit",
            password_hash="test",
            role="admin",
            real_name="生产审计员",
            customer_access_mode="all",
        )
        customer = Customer(name="生产审计客户")
        db.add_all([user, customer])
        db.flush()
        product = Product(
            customer_id=customer.id,
            product_code="PROD-AUDIT",
            customer_material_code="PROD-AUDIT",
            product_name="生产审计纸箱",
            box_category="normal",
        )
        location = WarehouseLocation(
            location_code="F12-P01",
            location_name="F12-P01",
            warehouse_type="finished",
            is_active=True,
            warehouse_floor=3,
            area_code="F12",
            storage_type="temporary_aisle",
            is_temporary=True,
            source_version="V11",
        )
        stock_location = WarehouseLocation(
            location_code="F12-P02",
            location_name="F12-P02",
            warehouse_type="finished",
            is_active=True,
            warehouse_floor=3,
            area_code="F12",
            storage_type="temporary_aisle",
            is_temporary=True,
            source_version="V11",
        )
        direct_staging = WarehouseLocation(
            location_code="F1-DISPATCH-01",
            location_name="一楼待送区",
            warehouse_type="finished",
            is_active=True,
            warehouse_floor=1,
            area_code="DISPATCH",
            storage_type="temporary_aisle",
            placement_status="placed",
            is_temporary=True,
            source_version="P1-25C",
        )
        db.add_all([product, location, stock_location, direct_staging])
        db.flush()

        ids: dict[str, int] = {}
        for key, quantity in (("direct", 4), ("stock", 5), ("rollback", 6)):
            order = Order(
                order_number=f"Q102-{key}",
                customer_id=customer.id,
                order_date=date(2026, 8, 1),
                delivery_date=date(2026, 8, 1),
                status="production",
                payment_status="unpaid",
                total_amount=Decimal(quantity),
            )
            db.add(order)
            db.flush()
            item = OrderItem(
                order_id=order.id,
                product_id=product.id,
                item_order_number=f"{key}-001",
                item_sequence=1,
                quantity=quantity,
                delivered_quantity=0,
                unit_price=Decimal("1"),
                subtotal=Decimal(quantity),
                material_status="received",
                material_received_at=_now(),
                snapshot_product_name="生产审计纸箱",
                snapshot_product_code="PROD-AUDIT",
                snapshot_spec="500x300x200",
                snapshot_material="K=A",
                inventory_deducted_qty=0,
                requisition_qty=quantity,
                requisition_status="未报料",
                special_process="无",
                flute_type="A",
            )
            db.add(item)
            db.flush()
            task = ProductionTask(
                order_item_id=item.id,
                status="pending",
                planned_quantity=quantity,
                finished_coverage_snapshot=0,
                readiness_basis="material_received",
                ready_at=_now(),
                version=1,
            )
            db.add(task)
            db.flush()
            ids[f"{key}_task"] = task.id

        db.commit()
        yield db, user, customer, location, stock_location, ids
    engine.dispose()


def _batch_payload(task_id: int, *, disposition: str, location_id: int | None = None):
    from app.api.production import CompletionBatchItem, CompletionBatchRequest

    return CompletionBatchRequest(
        idempotency_key=f"q1-02-{disposition}-{task_id}",
        items=[
            CompletionBatchItem(
                task_id=task_id,
                expected_version=1,
                disposition=disposition,
                location_id=location_id,
            )
        ],
    )


def test_production_writes_are_structured_once_with_customer_and_request_context(
    production_audit_db,
) -> None:
    from app.api import production as production_api
    from app.core.request_context import bind_current_request, reset_current_request
    from app.models.audit import OperationLog

    db, user, customer, location, stock_location, ids = production_audit_db
    token = bind_current_request(_request("/api/production/completion-batches"))
    try:
        direct_payload = _batch_payload(ids["direct_task"], disposition="direct")
        completed = production_api.post_completion_batch(
            payload=direct_payload, user=user, db=db
        )
        assert completed["replayed"] is False
        replayed = production_api.post_completion_batch(
            payload=direct_payload, user=user, db=db
        )
        assert replayed["replayed"] is True

        completion_id = completed["items"][0]["id"]
        transfer_payload = production_api.StockTransferRequest(
            idempotency_key="q1-02-direct-transfer",
            location_id=location.id,
        )
        transferred = production_api.post_completion_stock_transfer(
            completion_id=completion_id,
            payload=transfer_payload,
            user=user,
            db=db,
        )
        assert transferred["replayed"] is False
        assert production_api.post_completion_stock_transfer(
            completion_id=completion_id,
            payload=transfer_payload,
            user=user,
            db=db,
        )["replayed"] is True

        stock_payload = _batch_payload(
            ids["stock_task"], disposition="stock", location_id=stock_location.id
        )
        stock_completion = production_api.post_completion_batch(
            payload=stock_payload, user=user, db=db
        )
        stock_completion_id = stock_completion["items"][0]["id"]
        reverted = production_api.revert_production_completion(
            completion_id=stock_completion_id,
            payload=production_api.CompletionReversalRequest(reason="录入错误"),
            user=user,
            db=db,
        )
        assert reverted["completion"]["status"] == "reversed"
    finally:
        reset_current_request(token)

    events = db.scalars(
        select(OperationLog)
        .where(OperationLog.module_code == "production")
        .order_by(OperationLog.id)
    ).all()
    assert [event.action_code for event in events] == [
        "production.completion.posted",
        "production.completion.batch_posted",
        "production.stock_transfer.posted",
        "production.completion.posted",
        "production.completion.batch_posted",
        "production.completion.reverted",
    ]
    assert all(event.event_category == "business" for event in events)
    assert all(event.result == "success" for event in events)
    assert all(event.actor_user_id_snapshot == user.id for event in events)
    assert all(event.customer_id_snapshot == customer.id for event in events)
    assert all(event.customer_name_snapshot == customer.name for event in events)
    assert all(event.request_id == "q1-02-production-audit" for event in events)
    assert all(event.schema_version == 1 for event in events)

    batch_event = events[1]
    assert batch_event.entity_type == "production_completion_batch"
    assert batch_event.batch_id == str(completed["batch_id"])
    assert batch_event.object_ref == f"production_completion_batch:{completed['batch_id']}"
    transfer_event = events[2]
    assert transfer_event.entity_type == "production_stock_transfer"
    assert transfer_event.entity_id == transferred["transfer_id"]
    assert "inventory_lot_id" in (transfer_event.details or "")
    reversal_event = events[-1]
    assert reversal_event.entity_id == stock_completion_id
    assert "reversed_semi_movement_ids" in (reversal_event.details or "")


def test_production_audit_failure_rolls_back_completion_batch(
    production_audit_db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api import production as production_api
    from app.core.request_context import bind_current_request, reset_current_request
    from app.models.audit import OperationLog
    from app.models.production import ProductionCompletion

    db, user, _customer, _location, _stock_location, ids = production_audit_db

    def fail_audit(*_args, **_kwargs):
        raise RuntimeError("forced production audit failure")

    monkeypatch.setattr(production_api, "append_audit_event", fail_audit)
    token = bind_current_request(_request("/api/production/completion-batches"))
    try:
        with pytest.raises(RuntimeError, match="forced production audit failure"):
            production_api.post_completion_batch(
                payload=_batch_payload(ids["rollback_task"], disposition="direct"),
                user=user,
                db=db,
            )
    finally:
        reset_current_request(token)

    assert db.scalar(select(func.count()).select_from(ProductionCompletion)) == 0
    assert (
        db.scalar(
            select(func.count())
            .select_from(OperationLog)
            .where(OperationLog.module_code == "production")
        )
        == 0
    )
