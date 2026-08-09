from __future__ import annotations

from collections.abc import Generator
from datetime import date, timedelta
import importlib.util
from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
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
    from app.models.supplier_requisition_order import SupplierRequisitionOrder
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
                ),
                WarehouseArea(
                    floor_id=floor3.id,
                    area_code="A1",
                    area_name="三楼成品区",
                    planned_location_count=20,
                    planned_pallet_capacity=20,
                    construction_status="enabled",
                ),
            ]
        )
        source = SupplierRequisitionOrder(
            order_number="BL-FORECAST-001",
            supplier_name="鸣朋",
            total_quantity=100,
            requisition_qty=100,
            stock_deduction_qty=0,
            status="confirmed",
        )
        db.add(source)
        db.commit()
        ids = {"source": source.id, "floor": floor1.id}

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(warehouse_router, prefix="/api/warehouse")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield app, ids, today
    finally:
        engine.dispose()


def _login(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/auth/login", json={"username": username, "password": "123456"}
    )
    assert response.status_code == 200, response.text


def test_missing_source_is_not_guessed_and_admin_plan_drives_forecast(forecast_app) -> None:
    app, ids, today = forecast_app
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
        assert projected["forecast_complete"] is False  # field capacity is still planning-only
        home = client.get("/api/warehouse/capacity/summary").json()
        assert home["forecast_7d_peak_floor_code"] == "1F"
        assert home["forecast_7d_peak_utilization_percent"] == 90.0
        assert home["forecast_7d_action_count"] >= 1


def test_forecast_write_is_admin_only(forecast_app) -> None:
    app, ids, today = forecast_app
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
