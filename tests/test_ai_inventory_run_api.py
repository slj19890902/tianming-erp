from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.api.ai_assistant import router as ai_router
from app.api.auth import router as auth_router
from app.api.deps import get_db
from app.core.database import create_sqlite_engine
from app.core.security import hash_password
from app.models import Base
from app.models.access_control import UserPermissionOverride
from app.models.ai_assistant import (
    AiAnalysisFeedback,
    AiAnalysisRun,
    AiUsageLedger,
)
from app.models.audit import OperationLog
from app.models.user import User


def _insights() -> dict:
    return {
        "generated_at": "2026-07-26T10:00:00+08:00",
        "as_of": "2026-07-26",
        "summary": {
            "recorded_lots": 1,
            "available_lots": 1,
            "finished_available": 80,
            "semi_finished_available": 0,
            "total_reserved": 0,
            "total_damaged": 0,
            "total_scrapped": 0,
            "actual_inventory_value": None,
            "estimated_inventory_value": "80.00",
        },
        "data_quality": {
            "active_location_lots": 1,
            "exact_stock_date_lots": 1,
            "estimated_stock_date_lots": 0,
            "unknown_stock_date_lots": 0,
            "cost_ready_lots": 1,
            "missing_cost_lots": 0,
            "cost_coverage_percent": 100.0,
            "snapshot_estimate_coverage": 100.0,
            "current_quote_coverage": 0.0,
            "product_reference_coverage": 0.0,
            "actual_cost_supported": False,
            "actual_cost_message": "仅为估算",
        },
        "action_items": [
            {
                "priority": 1,
                "lot_id": 8001,
                "lot_number": "LOT-AI-RUN-001",
                "inventory_type": "finished",
                "status": "active",
                "location_code": "E1-L09",
                "quantity_available": 80,
                "unit": "个",
                "age_days": 200,
                "detail": {
                    "customer_name": "匿名客户",
                    "inventory_code": "AI-RUN-001",
                    "name": "库存经营解读测试箱",
                },
                "demand": {
                    "demand_30": 0,
                    "demand_90": 0,
                    "demand_180": 0,
                    "open_demand": 20,
                },
                "covered_demand_quantity": 20,
                "uncovered_demand_quantity": 0,
                "coverage_percent": 100.0,
                "coverage_basis": "finished_available_vs_open_order_demand",
                "cost_status": "estimated_snapshot",
                "estimated_unit_cost": "1.00",
                "estimated_value": "80.00",
                "reasons": [
                    {"code": "age_slow", "text": "库龄超过 180 天"},
                    {
                        "code": "finished_stock_can_cover_order",
                        "text": "现有库存可覆盖需求",
                    },
                ],
            }
        ],
    }


def _factory(tmp_path: Path) -> tuple[object, sessionmaker[Session]]:
    engine = create_sqlite_engine(tmp_path / "ai-run-api.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        users = [
            User(
                username="admin",
                password_hash=hash_password("123456"),
                role="admin",
                real_name="管理员",
                must_change_password=False,
            ),
            User(
                username="operator",
                password_hash=hash_password("123456"),
                role="sales",
                real_name="库存解读操作员",
                must_change_password=False,
                customer_access_mode="selected",
            ),
            User(
                username="other",
                password_hash=hash_password("123456"),
                role="sales",
                real_name="另一操作员",
                must_change_password=False,
                customer_access_mode="selected",
            ),
            User(
                username="no-warehouse",
                password_hash=hash_password("123456"),
                role="sales",
                real_name="无库存权限用户",
                must_change_password=False,
                customer_access_mode="selected",
            ),
        ]
        db.add_all(users)
        db.flush()
        by_name = {user.username: user for user in users}
        db.add_all(
            [
                UserPermissionOverride(
                    user_id=by_name["operator"].id,
                    permission_code="ai.inventory.view",
                    is_allowed=True,
                ),
                UserPermissionOverride(
                    user_id=by_name["operator"].id,
                    permission_code="warehouse.view",
                    is_allowed=True,
                ),
                UserPermissionOverride(
                    user_id=by_name["other"].id,
                    permission_code="ai.inventory.view",
                    is_allowed=True,
                ),
                UserPermissionOverride(
                    user_id=by_name["other"].id,
                    permission_code="warehouse.view",
                    is_allowed=True,
                ),
                UserPermissionOverride(
                    user_id=by_name["no-warehouse"].id,
                    permission_code="ai.inventory.view",
                    is_allowed=True,
                ),
            ]
        )
        db.commit()
    return engine, factory


def _app(factory: sessionmaker[Session]) -> FastAPI:
    application = FastAPI()
    application.include_router(auth_router, prefix="/api/auth")
    application.include_router(ai_router, prefix="/api/ai")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    application.dependency_overrides[get_db] = override_get_db
    return application


def _login(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": "123456"},
    )
    assert response.status_code == 200, response.text


def _business_table_counts(engine) -> dict[str, int]:
    table_names = [
        name
        for name in inspect(engine).get_table_names()
        if not name.startswith("ai_")
        and name not in {"operation_logs", "users", "user_permission_overrides"}
    ]
    with engine.connect() as connection:
        return {
            name: int(
                connection.execute(text(f'SELECT COUNT(*) FROM "{name}"')).scalar_one()
            )
            for name in table_names
        }


def test_mock_run_is_idempotent_audited_scoped_and_business_read_only(
    monkeypatch,
    tmp_path: Path,
) -> None:
    engine, factory = _factory(tmp_path)
    monkeypatch.setenv("ERP_ENVIRONMENT", "test")
    monkeypatch.setenv("ERP_AI_INVENTORY_PROVIDER", "mock")
    monkeypatch.setattr(
        "app.api.ai_assistant.build_inventory_insights",
        lambda *_args, **_kwargs: _insights(),
    )
    application = _app(factory)

    with TestClient(application) as client:
        _login(client, "operator")
        before = _business_table_counts(engine)
        first = client.post(
            "/api/ai/inventory-insights/runs",
            json={
                "as_of": "2026-07-26",
                "focus": "all",
                "idempotency_key": "ai-run-idempotent-001",
            },
        )
        assert first.status_code == 200, first.text
        body = first.json()
        assert body["status"] == "success"
        assert body["provider_code"] == "mock"
        assert body["snapshot"]["contains_cost_data"] is False
        assert "estimated_inventory_value" not in body["snapshot"]["summary"]
        assert body["interpretation"]["risk_groups"]
        assert "usage" not in body
        public_id = body["public_id"]

        repeated = client.post(
            "/api/ai/inventory-insights/runs",
            json={
                "as_of": "2026-07-26",
                "focus": "all",
                "idempotency_key": "ai-run-idempotent-001",
            },
        )
        assert repeated.status_code == 200
        assert repeated.json()["public_id"] == public_id

        changed = client.post(
            "/api/ai/inventory-insights/runs",
            json={
                "as_of": "2026-07-27",
                "focus": "all",
                "idempotency_key": "ai-run-idempotent-001",
            },
        )
        assert changed.status_code == 409

        feedback = client.post(
            f"/api/ai/inventory-insights/runs/{public_id}/feedback",
            json={
                "rating": "helpful",
                "reason_code": "useful_actionable",
                "remark": "现场核对入口清楚",
            },
        )
        assert feedback.status_code == 200, feedback.text
        updated_feedback = client.post(
            f"/api/ai/inventory-insights/runs/{public_id}/feedback",
            json={
                "rating": "inaccurate",
                "reason_code": "missing_context",
                "remark": "盘点尚未覆盖全部现场",
            },
        )
        assert updated_feedback.status_code == 200
        assert updated_feedback.json()["feedback"]["rating"] == "inaccurate"

        read_back = client.get(
            f"/api/ai/inventory-insights/runs/{public_id}"
        )
        assert read_back.status_code == 200
        assert read_back.json()["feedback"]["reason_code"] == "missing_context"
        assert _business_table_counts(engine) == before

    with factory() as db:
        assert db.scalar(select(func.count(AiAnalysisRun.id))) == 1
        assert db.scalar(select(func.count(AiUsageLedger.id))) == 1
        assert db.scalar(select(func.count(AiAnalysisFeedback.id))) == 1
        actions = set(
            db.scalars(
                select(OperationLog.action).where(
                    OperationLog.action.like("AI_INVENTORY_%")
                )
            ).all()
        )
        assert {
            "AI_INVENTORY_RUN",
            "AI_INVENTORY_FEEDBACK_CREATE",
            "AI_INVENTORY_FEEDBACK_UPDATE",
        } <= actions

    with TestClient(application) as other_client:
        _login(other_client, "other")
        hidden = other_client.get(
            f"/api/ai/inventory-insights/runs/{public_id}"
        )
        assert hidden.status_code == 404

    with TestClient(application) as admin_client:
        _login(admin_client, "admin")
        audited = admin_client.get(
            f"/api/ai/inventory-insights/runs/{public_id}"
        )
        assert audited.status_code == 200
        assert audited.json()["usage"]["provider_cost_estimate"] == "0.000000"
        usage = admin_client.get("/api/ai/usage/summary")
        assert usage.status_code == 200
        assert usage.json()["ledger_rows"] == 1
        assert usage.json()["request_count"] == 1
        assert usage.json()["runs_by_status"] == {"success": 1}


def test_missing_warehouse_permission_and_disabled_provider_fail_closed(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _engine, factory = _factory(tmp_path)
    monkeypatch.setenv("ERP_ENVIRONMENT", "test")
    monkeypatch.setenv("ERP_AI_INVENTORY_PROVIDER", "disabled")
    monkeypatch.setattr(
        "app.api.ai_assistant.build_inventory_insights",
        lambda *_args, **_kwargs: _insights(),
    )
    application = _app(factory)

    with TestClient(application) as no_warehouse_client:
        _login(no_warehouse_client, "no-warehouse")
        denied = no_warehouse_client.get("/api/ai/status")
        assert denied.status_code == 403
        denied_run = no_warehouse_client.post(
            "/api/ai/inventory-insights/runs",
            json={"idempotency_key": "no-warehouse"},
        )
        assert denied_run.status_code == 403

    with TestClient(application) as operator_client:
        _login(operator_client, "operator")
        provider_status = operator_client.get("/api/ai/status")
        assert provider_status.status_code == 200
        assert provider_status.json()["enabled"] is False
        degraded = operator_client.post(
            "/api/ai/inventory-insights/runs",
            json={
                "as_of": "2026-07-26",
                "idempotency_key": "disabled-provider",
            },
        )
        assert degraded.status_code == 200
        assert degraded.json()["status"] == "degraded"
        assert degraded.json()["interpretation"] is None
        assert degraded.json()["error_code"] == "ai_inventory_disabled"

    with factory() as db:
        run = db.scalar(select(AiAnalysisRun))
        usage = db.scalar(select(AiUsageLedger))
        assert run is not None and run.status == "degraded"
        assert usage is not None and usage.request_count == 0
        assert usage.provider_cost_estimate == 0


def test_production_never_enables_the_mock_provider(monkeypatch) -> None:
    from app.services.ai.providers import (
        DisabledInventoryInsightProvider,
        inventory_provider_status,
        resolve_inventory_provider,
    )

    monkeypatch.setenv("ERP_ENVIRONMENT", "production")
    monkeypatch.setenv("ERP_AI_INVENTORY_PROVIDER", "mock")
    provider_status = inventory_provider_status()

    assert provider_status["enabled"] is False
    assert provider_status["provider_code"] == "disabled"
    assert isinstance(resolve_inventory_provider(), DisabledInventoryInsightProvider)
