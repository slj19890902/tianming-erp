from __future__ import annotations

import json
from collections.abc import Generator
from datetime import datetime
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture()
def audit_api_context(tmp_path: Path):
    from app.api.audit import router as audit_router
    from app.api.auth import router as auth_router
    from app.api.deps import get_current_user, get_db, require_customer_access
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.audit import OperationLog
    from app.models.customer import Customer
    from app.models.user import User
    from app.middleware.performance import PerformanceObservabilityMiddleware
    from app.services.audit_log import append_audit_event

    engine = create_sqlite_engine(tmp_path / "q1-02-audit.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        admin = User(
            username="audit-admin",
            password_hash=hash_password("AuditAdmin123!"),
            role="admin",
            real_name="审计管理员",
            display_name="审计管理员",
            must_change_password=False,
        )
        boss = User(
            username="audit-boss",
            password_hash=hash_password("AuditBoss123!"),
            role="boss",
            real_name="老板账号",
            display_name="老板账号",
            must_change_password=False,
        )
        sales = User(
            username="audit-sales",
            password_hash=hash_password("AuditSales123!"),
            role="sales",
            real_name="录单员",
            display_name="录单员",
            must_change_password=False,
            customer_access_mode="selected",
        )
        customer = Customer(name="匿名范围客户")
        db.add_all([admin, boss, sales, customer])
        db.flush()
        first = append_audit_event(
            db,
            actor=admin,
            event_category="business",
            result="success",
            source="web",
            module_code="orders",
            action_code="order.update",
            resource="Order",
            entity_type="order",
            entity_id=101,
            object_ref="ORD-A",
            customer_id=1,
            customer_name="匿名客户甲",
            description="修改订单",
            details={"changed_fields": ["delivery_date"]},
        )
        first.created_at = datetime(2026, 7, 29, 10, 0, 0)
        second = append_audit_event(
            db,
            actor=sales,
            event_category="business",
            result="success",
            source="web",
            module_code="orders",
            action_code="order.create",
            resource="Order",
            entity_type="order",
            entity_id=102,
            object_ref="ORD-B",
            customer_id=1,
            customer_name="匿名客户甲",
            description="创建订单",
            details={"line_count": 2},
        )
        second.created_at = datetime(2026, 7, 29, 10, 0, 0)
        incoming = append_audit_event(
            db,
            actor=sales,
            event_category="business",
            result="failed",
            source="mobile",
            module_code="incoming",
            action_code="incoming.receive",
            resource="IncomingReceipt",
            entity_type="incoming_receipt",
            entity_id=201,
            object_ref="IN-201",
            customer_id=2,
            customer_name="匿名客户乙",
            description="收料失败",
            details={"reason": "数量不符"},
        )
        incoming.created_at = datetime(2026, 7, 29, 11, 0, 0)
        db.add(
            OperationLog(
                user_id=admin.id,
                action="UPDATE",
                resource="LegacyOrder",
                details=json.dumps(
                    {"order_no": "OLD-1", "password": "do-not-return"},
                    ensure_ascii=False,
                ),
                username=admin.username,
                role=admin.role,
                entity_type="order",
                entity_id=99,
                description="历史订单修改",
                extra_json="token=should-not-return",
                created_at=datetime(2026, 7, 28, 9, 0, 0),
            )
        )
        db.commit()
        ids = {
            "admin": admin.id,
            "boss": boss.id,
            "sales": sales.id,
            "first": first.id,
            "second": second.id,
            "customer": customer.id,
        }

    app = FastAPI()
    app.add_middleware(
        PerformanceObservabilityMiddleware,
        slow_request_ms=60_000,
    )
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(audit_router, prefix="/api/audit")

    @app.get("/api/test/customers/{customer_id}")
    def protected_customer(
        customer_id: int,
        _user=Depends(require_customer_access),
    ) -> dict[str, int]:
        return {"customer_id": customer_id}

    @app.get("/api/test/manual-customers/{customer_id}")
    def manually_protected_customer(
        customer_id: int,
        current_user=Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> dict[str, int]:
        require_customer_access(
            customer_id,
            current_user=current_user,
            db=db,
        )
        return {"customer_id": customer_id}

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield app, factory, ids
    finally:
        engine.dispose()


def _login(client: TestClient, username: str, password: str) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200


def test_audit_view_is_admin_only_and_denial_is_recorded(
    audit_api_context,
) -> None:
    from app.api.deps import (
        ADMIN_ONLY_PERMISSIONS,
        PERMISSION_CATALOG,
        effective_permissions,
    )
    from app.models.access_control import UserPermissionOverride
    from app.models.audit import OperationLog
    from app.models.user import User

    app, factory, ids = audit_api_context
    assert "audit.view" in PERMISSION_CATALOG
    assert "audit.view" in ADMIN_ONLY_PERMISSIONS
    with factory() as db:
        boss = db.get(User, ids["boss"])
        db.add(
            UserPermissionOverride(
                user_id=boss.id,
                permission_code="audit.view",
                is_allowed=True,
                granted_by=ids["admin"],
            )
        )
        db.commit()
        assert "audit.view" not in effective_permissions(boss)

    with TestClient(app) as client:
        _login(client, "audit-boss", "AuditBoss123!")
        denied = client.get("/api/audit/logs")
    assert denied.status_code == 403
    with factory() as db:
        event = db.scalar(
            select(OperationLog)
            .where(
                OperationLog.event_category == "security",
                OperationLog.result == "denied",
                OperationLog.action_code == "permission.denied",
            )
            .order_by(OperationLog.id.desc())
        )
    assert event is not None
    assert event.actor_user_id_snapshot == ids["boss"]
    assert event.operator_name_snapshot == "老板账号"
    assert event.object_ref == "audit.view"
    assert json.loads(event.details)["path"] == "/api/audit/logs"


def test_customer_scope_denial_is_recorded_without_weakening_403(
    audit_api_context,
) -> None:
    from app.models.audit import OperationLog

    app, factory, ids = audit_api_context
    with TestClient(app) as client:
        _login(client, "audit-sales", "AuditSales123!")
        response = client.get(f"/api/test/customers/{ids['customer']}")
    assert response.status_code == 403
    with factory() as db:
        event = db.scalar(
            select(OperationLog)
            .where(OperationLog.action_code == "customer_scope.denied")
            .order_by(OperationLog.id.desc())
        )
    assert event is not None
    assert event.result == "denied"
    assert event.object_ref == str(ids["customer"])
    assert json.loads(event.details)["path"].startswith("/api/test/customers/")


def test_manual_customer_scope_denial_inherits_server_request_context(
    audit_api_context,
) -> None:
    from app.models.audit import OperationLog

    app, factory, ids = audit_api_context
    with TestClient(app) as client:
        _login(client, "audit-sales", "AuditSales123!")
        response = client.get(
            f"/api/test/manual-customers/{ids['customer']}"
        )

    assert response.status_code == 403
    request_id = response.headers["X-Request-ID"]
    with factory() as db:
        event = db.scalar(
            select(OperationLog)
            .where(OperationLog.action_code == "customer_scope.denied")
            .order_by(OperationLog.id.desc())
        )
    assert event is not None
    assert event.request_id == request_id
    details = json.loads(event.details)
    assert details["method"] == "GET"
    assert details["path"].startswith("/api/test/manual-customers/")


def test_security_audit_outage_still_fails_closed(
    audit_api_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.api.deps as deps

    app, _factory, _ids = audit_api_context
    with TestClient(app) as client:
        _login(client, "audit-boss", "AuditBoss123!")

        def reject_audit(*_args, **_kwargs):
            raise RuntimeError("simulated security audit outage")

        monkeypatch.setattr(deps, "append_audit_event", reject_audit)
        response = client.get("/api/audit/logs")
    assert response.status_code == 403
    assert response.json()["detail"] == "权限不足"


def test_list_filters_paginates_stably_and_self_audits(
    audit_api_context,
) -> None:
    from app.models.audit import OperationLog

    app, factory, ids = audit_api_context
    with TestClient(app) as client:
        _login(client, "audit-admin", "AuditAdmin123!")
        response = client.get(
            "/api/audit/logs",
            params={
                "event_category": "business",
                "date_from": "2026-07-29",
                "date_to": "2026-07-29",
                "operator": "audit",
                "module": "orders",
                "action": "order",
                "object_ref": "ORD",
                "customer": "匿名客户甲",
                "result": "success",
                "source": "web",
                "page": 1,
                "page_size": 1,
            },
        )
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 2
    assert payload["pages"] == 2
    assert [item["id"] for item in payload["items"]] == [ids["second"]]
    assert payload["items"][0]["operator"]["name"] == "录单员"

    with factory() as db:
        event = db.scalar(
            select(OperationLog)
            .where(OperationLog.action_code == "audit.list.view")
            .order_by(OperationLog.id.desc())
        )
    assert event is not None
    assert event.event_category == "audit_access"
    details = json.loads(event.details)
    assert details["result_count"] == 2
    assert details["filters"]["module"] == "orders"
    assert "items" not in details
    assert "ORD-B" not in event.details


def test_audit_dates_use_beijing_business_day_and_explicit_utc(
    audit_api_context,
) -> None:
    from app.models.audit import OperationLog

    app, factory, ids = audit_api_context
    with factory() as db:
        db.add_all(
            [
                OperationLog(
                    user_id=ids["admin"],
                    action="BOUNDARY",
                    resource="BoundaryInside",
                    event_category="business",
                    result="success",
                    source="web",
                    module_code="boundary",
                    action_code="boundary.inside",
                    schema_version=1,
                    created_at=datetime(2026, 7, 28, 16, 30, 0),
                ),
                OperationLog(
                    user_id=ids["admin"],
                    action="BOUNDARY",
                    resource="BoundaryOutside",
                    event_category="business",
                    result="success",
                    source="web",
                    module_code="boundary",
                    action_code="boundary.outside",
                    schema_version=1,
                    created_at=datetime(2026, 7, 28, 15, 59, 59),
                ),
            ]
        )
        db.commit()

    with TestClient(app) as client:
        _login(client, "audit-admin", "AuditAdmin123!")
        response = client.get(
            "/api/audit/logs",
            params={
                "date_from": "2026-07-29",
                "date_to": "2026-07-29",
                "module": "boundary",
            },
        )

    assert response.status_code == 200
    assert response.json()["total"] == 1
    row = response.json()["items"][0]
    assert row["resource"] == "BoundaryInside"
    assert row["created_at"] == "2026-07-28T16:30:00Z"


def test_security_filter_includes_audit_access_and_detail_logs_only_target(
    audit_api_context,
) -> None:
    from app.models.audit import OperationLog

    app, factory, ids = audit_api_context
    with TestClient(app) as client:
        _login(client, "audit-admin", "AuditAdmin123!")
        first_list = client.get(
            "/api/audit/logs",
            params={"event_category": "business", "page_size": 5},
        )
        assert first_list.status_code == 200
        detail = client.get(f"/api/audit/logs/{ids['first']}")
        assert detail.status_code == 200
        security = client.get(
            "/api/audit/logs",
            params={"event_category": "security", "page_size": 100},
        )

    assert security.status_code == 200
    categories = {item["event_category"] for item in security.json()["items"]}
    assert "security" in categories
    assert "audit_access" in categories
    assert detail.json()["id"] == ids["first"]
    with factory() as db:
        event = db.scalar(
            select(OperationLog)
            .where(OperationLog.action_code == "audit.detail.view")
            .order_by(OperationLog.id.desc())
        )
    assert event is not None
    assert json.loads(event.details) == {"target_log_id": ids["first"]}


def test_legacy_rows_are_not_inferred_as_success_and_are_sanitized(
    audit_api_context,
) -> None:
    app, _factory, _ids = audit_api_context
    with TestClient(app) as client:
        _login(client, "audit-admin", "AuditAdmin123!")
        response = client.get(
            "/api/audit/logs",
            params={
                "event_category": "business",
                "date_from": "2026-07-28",
                "date_to": "2026-07-28",
            },
        )
        success_only = client.get(
            "/api/audit/logs",
            params={
                "event_category": "business",
                "result": "success",
                "date_from": "2026-07-28",
                "date_to": "2026-07-28",
            },
        )

    assert response.status_code == 200
    legacy = response.json()["items"][0]
    assert legacy["event_category"] == "legacy"
    assert legacy["result"] == "legacy"
    assert legacy["source"] == "legacy"
    assert legacy["details"]["password"] == "[已脱敏]"
    assert "should-not-return" not in json.dumps(
        legacy,
        ensure_ascii=False,
    )
    assert success_only.status_code == 200
    assert success_only.json()["total"] == 0


def test_legacy_filters_include_migrated_and_later_legacy_rows(
    audit_api_context,
) -> None:
    from app.models.audit import OperationLog

    app, factory, _ids = audit_api_context
    with factory() as db:
        db.add(
            OperationLog(
                action="UPDATE",
                resource="MigratedLegacyOrder",
                result="legacy",
                source="legacy",
                schema_version=0,
            )
        )
        db.commit()

    with TestClient(app) as client:
        _login(client, "audit-admin", "AuditAdmin123!")
        response = client.get(
            "/api/audit/logs",
            params={"result": "legacy", "source": "legacy"},
        )

    assert response.status_code == 200
    resources = {item["resource"] for item in response.json()["items"]}
    assert {"LegacyOrder", "MigratedLegacyOrder"} <= resources


def test_known_legacy_user_security_actions_stay_in_security_tab(
    audit_api_context,
) -> None:
    from app.models.audit import OperationLog

    app, factory, ids = audit_api_context
    with factory() as db:
        db.add(
            OperationLog(
                user_id=ids["admin"],
                action="UPDATE_USER",
                resource="User",
                result="legacy",
                source="legacy",
                schema_version=0,
            )
        )
        db.commit()

    with TestClient(app) as client:
        _login(client, "audit-admin", "AuditAdmin123!")
        security = client.get(
            "/api/audit/logs",
            params={
                "event_category": "security",
                "action": "UPDATE_USER",
            },
        )
        business = client.get(
            "/api/audit/logs",
            params={
                "event_category": "business",
                "action": "UPDATE_USER",
            },
        )

    assert security.status_code == 200
    assert security.json()["total"] == 1
    assert security.json()["items"][0]["event_category"] == "legacy"
    assert business.status_code == 200
    assert business.json()["total"] == 0


def test_audit_persistence_failure_never_returns_selected_data(
    audit_api_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.api.audit as audit_api

    app, factory, _ids = audit_api_context
    with TestClient(app, raise_server_exceptions=False) as client:
        _login(client, "audit-admin", "AuditAdmin123!")

        def reject_audit(*_args, **_kwargs):
            raise RuntimeError("simulated audit outage")

        monkeypatch.setattr(audit_api, "append_audit_event", reject_audit)
        response = client.get("/api/audit/logs")

    assert response.status_code == 500
    assert response.json()["detail"] == "审计留痕失败，未返回查询结果"
    with factory() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(audit_api.OperationLog)
                .where(audit_api.OperationLog.action_code == "audit.list.view")
            )
            or 0
        ) == 0


def test_audit_api_exposes_no_write_export_or_delete_route(
    audit_api_context,
) -> None:
    app, _factory, _ids = audit_api_context
    audit_routes = [
        route
        for route in app.routes
        if getattr(route, "path", "").startswith("/api/audit/")
    ]
    assert {route.path for route in audit_routes} == {
        "/api/audit/logs",
        "/api/audit/logs/{log_id}",
    }
    assert all(getattr(route, "methods", set()) <= {"GET", "HEAD"} for route in audit_routes)


def test_login_failure_success_and_logout_use_structured_security_events(
    audit_api_context,
) -> None:
    from app.models.audit import OperationLog

    app, factory, ids = audit_api_context
    with TestClient(app) as client:
        failed = client.post(
            "/api/auth/login",
            json={"username": "audit-admin", "password": "wrong"},
        )
        assert failed.status_code == 401
        _login(client, "audit-admin", "AuditAdmin123!")
        logout = client.post("/api/auth/logout")
        assert logout.status_code == 200

    with factory() as db:
        rows = db.scalars(
            select(OperationLog)
            .where(
                OperationLog.module_code == "auth",
                OperationLog.username == "audit-admin",
            )
            .order_by(OperationLog.id)
        ).all()
    assert [(row.action_code, row.result) for row in rows] == [
        ("LOGIN_FAILED", "failed"),
        ("LOGIN", "success"),
        ("LOGOUT", "success"),
    ]
    assert all(row.event_category == "security" for row in rows)
    assert rows[-1].actor_user_id_snapshot == ids["admin"]
    assert all(
        "password" not in (row.details or "").lower()
        for row in rows
    )


def test_user_create_and_update_audit_safe_before_after(
    audit_api_context,
) -> None:
    from app.models.audit import OperationLog

    app, factory, _ids = audit_api_context
    with TestClient(app) as client:
        _login(client, "audit-admin", "AuditAdmin123!")
        created = client.post(
            "/api/auth/users",
            json={
                "username": "audit-worker",
                "password": "AuditWorker123!",
                "role": "sales",
                "real_name": "匿名操作员",
                "display_name": "操作员甲",
                "is_active": True,
                "must_change_password": True,
            },
        )
        assert created.status_code == 201
        user_id = created.json()["user"]["id"]
        updated = client.put(
            f"/api/auth/users/{user_id}",
            json={
                "password": "AuditWorker456!",
                "role": "workshop",
                "display_name": "操作员乙",
                "must_change_password": False,
            },
        )
        assert updated.status_code == 200

    with factory() as db:
        rows = db.scalars(
            select(OperationLog)
            .where(
                OperationLog.entity_type == "user",
                OperationLog.entity_id == user_id,
                OperationLog.action_code.in_(
                    ("CREATE_USER", "UPDATE_USER")
                ),
            )
            .order_by(OperationLog.id)
        ).all()

    assert [row.action_code for row in rows] == [
        "CREATE_USER",
        "UPDATE_USER",
    ]
    created_details = json.loads(rows[0].details)
    assert created_details["after"]["role"] == "sales"
    assert created_details["after"]["is_active"] is True
    assert created_details["after"]["credential_change_required"] is True
    updated_details = json.loads(rows[1].details)
    assert updated_details["before"]["role"] == "sales"
    assert updated_details["after"]["role"] == "workshop"
    assert updated_details["before"]["display_name"] == "操作员甲"
    assert updated_details["after"]["display_name"] == "操作员乙"
    assert updated_details["credential_changed"] is True
    assert "password" not in updated_details["before"]
    assert "password" not in updated_details["after"]
    assert "AuditWorker123!" not in rows[0].details
    assert "AuditWorker456!" not in rows[1].details
