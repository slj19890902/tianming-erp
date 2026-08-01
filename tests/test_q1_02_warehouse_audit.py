from __future__ import annotations

from datetime import date
import json
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request


def _request(path: str) -> Request:
    request = Request(
        {
            "type": "http", "http_version": "1.1", "method": "POST",
            "scheme": "http", "path": path, "raw_path": path.encode(),
            "query_string": b"", "headers": [],
            "client": ("127.0.0.1", 18179), "server": ("127.0.0.1", 18179),
        }
    )
    request.state.request_id = "q1-02-warehouse-audit"
    return request


@pytest.fixture()
def warehouse_audit_db(tmp_path: Path):
    from app.core.database import create_sqlite_engine
    from app.models import Base
    from app.models.customer import Customer
    from app.models.product import Product
    from app.models.user import User
    from app.models.warehouse_inventory import WarehouseLocation

    engine = create_sqlite_engine(tmp_path / "warehouse-audit.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        user = User(
            username="warehouse-audit",
            password_hash="test",
            role="admin",
            real_name="Warehouse Audit",
        )
        customer = Customer(name="审计库存客户")
        db.add_all([user, customer])
        db.flush()
        product = Product(
            customer_id=customer.id, product_code="AUDIT-BOX",
            customer_material_code="AUDIT-BOX", product_name="审计纸箱",
            box_category="normal",
        )
        location = WarehouseLocation(
            location_code="FG-AUDIT", location_name="审计共用库", warehouse_type="shared"
        )
        db.add_all([product, location])
        db.commit()
        yield db, user, customer, product, location


def test_manual_in_is_audited_once_and_audit_failure_rolls_back_lot(
    warehouse_audit_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api import warehouse as warehouse_api
    from app.models.audit import OperationLog
    from app.models.warehouse_inventory import InventoryLot

    db, user, customer, product, location = warehouse_audit_db
    payload = warehouse_api.FinishedManualInPayload(
        customer_id=customer.id, product_id=product.id, location_id=location.id,
        quantity=10, stock_date=date(2026, 8, 1), idempotency_key="audit-manual-in-1",
    )
    created = warehouse_api.finished_manual_in(
        payload=payload, request=_request("/api/warehouse/finished/manual-in"), db=db, user=user
    )
    repeated = warehouse_api.finished_manual_in(
        payload=payload, request=_request("/api/warehouse/finished/manual-in"), db=db, user=user
    )
    assert repeated["id"] == created["id"]
    logs = db.scalars(select(OperationLog).where(
        OperationLog.action_code == "warehouse.finished.manual_in"
    )).all()
    assert len(logs) == 1
    assert logs[0].object_ref == created["lot_number"]
    assert logs[0].customer_id_snapshot == customer.id
    assert logs[0].request_id == "q1-02-warehouse-audit"

    def fail_audit(*_args, **_kwargs):
        raise RuntimeError("forced audit failure")

    monkeypatch.setattr(warehouse_api, "append_audit_event", fail_audit)
    with pytest.raises(RuntimeError, match="forced audit failure"):
        warehouse_api._operate(
            db, user, created["id"], "adjust",
            warehouse_api.AdjustPayload(expected_version=1, quantity_delta=3, reason="审计回滚"),
            quantity=3,
            request=_request(f"/api/warehouse/lots/{created['id']}/adjust"),
        )
    db.rollback()
    assert db.get(InventoryLot, created["id"]).quantity_available == 10
    assert db.scalar(select(func.count()).select_from(OperationLog).where(
        OperationLog.action_code == "warehouse.lot.adjust"
    )) == 0


def test_semi_finished_manual_in_and_lot_operations_write_safe_audits(
    warehouse_audit_db,
) -> None:
    from app.api import warehouse as warehouse_api
    from app.models.audit import OperationLog

    db, user, customer, product, location = warehouse_audit_db
    semi = warehouse_api.semi_finished_manual_in(
        payload=warehouse_api.SemiFinishedManualInPayload(
            location_id=location.id,
            quantity=20,
            stock_date=date(2026, 8, 1),
            material_code="K=A",
            layer_count=3,
            flute_type="B",
            board_length_mm=520,
            board_width_mm=350,
            sheet_type="raw_board",
            customer_id=customer.id,
            idempotency_key="audit-semi-in-1",
        ),
        request=_request("/api/warehouse/semi-finished/manual-in"),
        db=db,
        user=user,
    )
    assert semi["inventory_type"] == "semi_finished"

    lot = warehouse_api.finished_manual_in(
        payload=warehouse_api.FinishedManualInPayload(
            customer_id=customer.id,
            product_id=product.id,
            location_id=location.id,
            quantity=10,
            stock_date=date(2026, 8, 1),
            idempotency_key="audit-operations-in-1",
        ),
        request=_request("/api/warehouse/finished/manual-in"),
        db=db,
        user=user,
    )
    operation_payloads = [
        ("adjust", warehouse_api.AdjustPayload(expected_version=1, quantity_delta=2, reason="盘点调整"), 2),
        ("freeze", warehouse_api.VersionPayload(expected_version=2, reason="暂缓出库"), 0),
        ("unfreeze", warehouse_api.VersionPayload(expected_version=3, reason="恢复出库"), 0),
        ("damage", warehouse_api.QuantityOperationPayload(expected_version=4, quantity=1, reason="破损"), 1),
        ("scrap", warehouse_api.QuantityOperationPayload(expected_version=5, quantity=1, reason="报废"), 1),
        ("transfer_to_general", warehouse_api.VersionPayload(expected_version=6, reason="转通用库存"), 0),
    ]
    for index, (operation, payload, quantity) in enumerate(operation_payloads, start=1):
        payload.idempotency_key = f"audit-operation-{index}"
        lot = warehouse_api._operate(
            db,
            user,
            lot["id"],
            operation,
            payload,
            quantity=quantity,
            request=_request(f"/api/warehouse/lots/{lot['id']}/{operation}"),
        )

    adjust_replay = warehouse_api._operate(
        db,
        user,
        lot["id"],
        "adjust",
        warehouse_api.AdjustPayload(
            expected_version=1,
            quantity_delta=2,
            reason="盘点调整",
            idempotency_key="audit-operation-1",
        ),
        quantity=2,
        request=_request(f"/api/warehouse/lots/{lot['id']}/adjust"),
    )
    assert adjust_replay["id"] == lot["id"]
    logs = db.scalars(
        select(OperationLog).where(OperationLog.module_code == "warehouse")
    ).all()
    action_codes = {log.action_code for log in logs}
    assert {
        "warehouse.semi_finished.manual_in",
        "warehouse.finished.manual_in",
        "warehouse.lot.adjust",
        "warehouse.lot.freeze",
        "warehouse.lot.unfreeze",
        "warehouse.lot.damage",
        "warehouse.lot.scrap",
        "warehouse.lot.transfer_to_general",
    } <= action_codes
    adjust_log = next(log for log in logs if log.action_code == "warehouse.lot.adjust")
    details = json.loads(adjust_log.details or "{}")
    assert details["before"]["available"] == 10
    assert details["after"]["available"] == 12
    assert adjust_log.object_ref == lot["lot_number"]
    assert adjust_log.customer_id_snapshot == customer.id
    assert db.scalar(select(func.count()).select_from(OperationLog).where(
        OperationLog.action_code == "warehouse.lot.adjust"
    )) == 1
