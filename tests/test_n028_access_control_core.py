from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import inspect
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture()
def access_control_app(tmp_path: Path):
    from app.api.auth import router as auth_router
    from app.api.deps import (
        PermissionChecker,
        get_db,
        require_customer_access,
    )
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.customer import Customer
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "access-control.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        db.add_all(
            [
                User(
                    username="admin",
                    password_hash=hash_password("AdminPass123!"),
                    role="admin",
                    real_name="Admin",
                    must_change_password=False,
                ),
                User(
                    username="boss",
                    password_hash=hash_password("BossPass123!"),
                    role="boss",
                    real_name="Boss",
                    must_change_password=False,
                ),
                User(
                    username="sales",
                    password_hash=hash_password("SalesPass123!"),
                    role="sales",
                    real_name="Sales",
                    must_change_password=False,
                ),
                Customer(name="Customer A"),
                Customer(name="Customer B"),
            ]
        )
        db.commit()
        ids = {
            user.username: user.id
            for user in db.query(User).order_by(User.username).all()
        }
        customer_ids = [
            customer.id for customer in db.query(Customer).order_by(Customer.id).all()
        ]

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")

    @app.get("/permission/orders-create")
    def orders_create(
        _user=Depends(PermissionChecker("orders.create")),
    ) -> dict[str, bool]:
        return {"ok": True}

    @app.get("/customer/{customer_id}/protected")
    def customer_protected(
        customer_id: int,
        _user=Depends(require_customer_access),
    ) -> dict[str, int]:
        return {"customer_id": customer_id}

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield app, factory, ids, customer_ids, engine
    finally:
        engine.dispose()


def _login(client: TestClient, username: str, password: str) -> dict:
    response = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200
    return response.json()


def test_sales_defaults_are_business_limited_and_boss_excludes_backup(
    access_control_app,
) -> None:
    from app.api.deps import SALES_DEFAULT_PERMISSIONS, has_permission
    from app.models.user import User

    app, factory, ids, _customer_ids, _engine = access_control_app
    with factory() as db:
        sales = db.get(User, ids["sales"])
        boss = db.get(User, ids["boss"])
        assert SALES_DEFAULT_PERMISSIONS == frozenset(
            {
                "customers.view",
                "customers.edit",
                "products.view",
                "products.edit",
                "quotations.view",
                "quotations.edit",
                "contracts.view",
                "contracts.edit",
                "contracts.convert",
                "orders.view",
                "orders.create",
                "orders.edit",
                "dashboard.view",
            }
        )
        assert has_permission(sales, "customers.edit")
        assert has_permission(sales, "products.edit")
        assert has_permission(sales, "quotations.view")
        assert has_permission(sales, "quotations.edit")
        assert has_permission(sales, "contracts.view")
        assert has_permission(sales, "contracts.edit")
        assert has_permission(sales, "contracts.convert")
        assert has_permission(sales, "orders.create")
        assert has_permission(sales, "orders.edit")
        for permission in (
            "customers.deactivate",
            "customers.delete",
            "products.deactivate",
            "products.delete",
            "orders.status",
            "orders.delete",
            "orders.rollback",
            "incoming.view",
            "incoming.execute",
            "warehouse.view",
            "warehouse.reserve",
            "deliveries.view",
            "deliveries.execute",
            "requisition.view",
            "requisition.execute",
            "finance.view",
            "finance.execute",
            "cost.view",
            "system.backup",
            "users.manage",
        ):
            assert not has_permission(sales, permission)
        assert has_permission(boss, "orders.rollback")
        assert not has_permission(boss, "users.manage")
        assert not has_permission(boss, "system.backup")

    with TestClient(app) as client:
        boss_payload = _login(client, "boss", "BossPass123!")
        assert "orders.rollback" in boss_payload["permissions"]
        assert "system.backup" not in boss_payload["permissions"]
        assert client.get("/api/auth/users").status_code == 403


def test_admin_saves_user_overrides_and_customer_scope(access_control_app) -> None:
    app, _factory, ids, customer_ids, _engine = access_control_app
    with TestClient(app) as client:
        _login(client, "admin", "AdminPass123!")
        created = client.post(
            "/api/auth/users",
            json={
                "username": "new-sales",
                "password": "NewSales123!",
                "role": "sales",
                "real_name": "New Sales",
            },
        )
        assert created.status_code == 201
        created_id = created.json()["user"]["id"]
        updated = client.put(
            f"/api/auth/users/{created_id}",
            json={"display_name": "Sales Person", "is_active": False},
        )
        assert updated.status_code == 200
        assert updated.json()["user"]["display_name"] == "Sales Person"

        saved_overrides = client.put(
            f"/api/auth/users/{ids['sales']}/permission-overrides",
            json={"overrides": {"orders.create": False, "cost.view": True}},
        )
        assert saved_overrides.status_code == 200
        assert "orders.create" not in saved_overrides.json()["effective_permissions"]
        assert "cost.view" in saved_overrides.json()["effective_permissions"]
        assert client.get(
            f"/api/auth/users/{ids['sales']}/permission-overrides"
        ).json()["overrides"] == [
            {"permission_code": "cost.view", "is_allowed": True},
            {"permission_code": "orders.create", "is_allowed": False},
        ]

        saved_scopes = client.put(
            f"/api/auth/users/{ids['sales']}/customer-scopes",
            json={"mode": "selected", "customer_ids": [customer_ids[0]]},
        )
        assert saved_scopes.status_code == 200
        assert saved_scopes.json()["customer_scope"] == [customer_ids[0]]

        client.post("/api/auth/logout")
        sales_payload = _login(client, "sales", "SalesPass123!")
        assert sales_payload["customer_scope"] == [customer_ids[0]]
        assert sales_payload["customer_access_mode"] == "selected"
        assert client.get("/permission/orders-create").status_code == 403
        assert client.get(f"/customer/{customer_ids[0]}/protected").status_code == 200
        assert client.get(f"/customer/{customer_ids[1]}/protected").status_code == 403


def test_selected_customer_scope_can_explicitly_deny_all_customers(
    access_control_app,
) -> None:
    app, _factory, ids, customer_ids, _engine = access_control_app
    with TestClient(app) as client:
        _login(client, "admin", "AdminPass123!")
        saved = client.put(
            f"/api/auth/users/{ids['sales']}/customer-scopes",
            json={"mode": "selected", "customer_ids": []},
        )
        assert saved.status_code == 200
        assert saved.json()["customer_access_mode"] == "selected"
        assert saved.json()["unrestricted_customer_access"] is False

        client.post("/api/auth/logout")
        payload = _login(client, "sales", "SalesPass123!")
        assert payload["customer_access_mode"] == "selected"
        assert payload["unrestricted_customer_access"] is False
        assert client.get(f"/customer/{customer_ids[0]}/protected").status_code == 403


def test_atomic_access_update_rejects_invalid_scope_without_partial_save(
    access_control_app,
) -> None:
    app, _factory, ids, customer_ids, _engine = access_control_app
    with TestClient(app) as client:
        _login(client, "admin", "AdminPass123!")
        saved = client.put(
            f"/api/auth/users/{ids['sales']}/access",
            json={
                "overrides": {"cost.view": True},
                "mode": "selected",
                "customer_ids": [customer_ids[0]],
            },
        )
        assert saved.status_code == 200

        rejected = client.put(
            f"/api/auth/users/{ids['sales']}/access",
            json={
                "overrides": {"orders.create": False},
                "mode": "selected",
                "customer_ids": [999999],
            },
        )
        assert rejected.status_code == 404
        current = client.get(
            f"/api/auth/users/{ids['sales']}/permission-overrides"
        ).json()
        assert current["overrides"] == [
            {"permission_code": "cost.view", "is_allowed": True}
        ]


def test_admin_cannot_demote_or_deactivate_self(access_control_app) -> None:
    app, _factory, ids, _customer_ids, _engine = access_control_app
    with TestClient(app) as client:
        _login(client, "admin", "AdminPass123!")
        assert client.put(
            f"/api/auth/users/{ids['admin']}", json={"role": "sales"}
        ).status_code == 400
        assert client.put(
            f"/api/auth/users/{ids['admin']}", json={"is_active": False}
        ).status_code == 400


def test_admin_only_permissions_cannot_be_granted_to_business_roles(
    access_control_app,
) -> None:
    app, _factory, ids, _customer_ids, _engine = access_control_app
    with TestClient(app) as client:
        _login(client, "admin", "AdminPass123!")
        response = client.put(
            f"/api/auth/users/{ids['sales']}/access",
            json={
                "overrides": {"users.manage": True, "system.backup": True},
                "mode": "all",
                "customer_ids": [],
            },
        )
        assert response.status_code == 400
        catalog = client.get(
            f"/api/auth/users/{ids['sales']}/permission-overrides"
        ).json()["permissions"]
        locked = {row["code"] for row in catalog if row["locked"]}
        assert {"users.manage", "system.backup"}.issubset(locked)


def test_access_control_models_have_unique_constraints(access_control_app) -> None:
    _app, _factory, _ids, _customer_ids, engine = access_control_app
    inspector = inspect(engine)
    overrides = inspector.get_unique_constraints("user_permission_overrides")
    scopes = inspector.get_unique_constraints("user_customer_scopes")
    assert frozenset({"user_id", "permission_code"}) in {
        frozenset(item["column_names"]) for item in overrides
    }
    assert frozenset({"user_id", "customer_id"}) in {
        frozenset(item["column_names"]) for item in scopes
    }


def test_n028_migration_has_required_revision_link() -> None:
    migration_path = (
        Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / "aj37v7w8x9f27_n028_access_control_core.py"
    )
    source = migration_path.read_text(encoding="utf-8")
    assert 'down_revision = "ai36v7w8x9e26"' in source
    assert "user_permission_overrides" in source
    assert "user_customer_scopes" in source


def test_all_access_write_endpoints_record_complete_audit_and_revoke_session(
    access_control_app,
) -> None:
    import json

    from sqlalchemy import select

    from app.models.audit import OperationLog
    from app.models.user import User

    app, factory, ids, customer_ids, _engine = access_control_app
    actions = [
        "UPDATE_PERMISSION_OVERRIDES",
        "UPDATE_CUSTOMER_SCOPES",
        "UPDATE_USER_ACCESS",
    ]
    with TestClient(app) as admin_client, TestClient(app) as sales_client:
        _login(admin_client, "admin", "AdminPass123!")
        _login(sales_client, "sales", "SalesPass123!")
        assert sales_client.get("/api/auth/me").status_code == 200

        permission_response = admin_client.put(
            f"/api/auth/users/{ids['sales']}/permission-overrides",
            json={"overrides": {"orders.create": False, "cost.view": True}},
        )
        assert permission_response.status_code == 200
        assert sales_client.get("/api/auth/me").status_code == 401

        scope_response = admin_client.put(
            f"/api/auth/users/{ids['sales']}/customer-scopes",
            json={
                "mode": "selected",
                "customer_ids": [customer_ids[1], customer_ids[0]],
            },
        )
        assert scope_response.status_code == 200

        access_response = admin_client.put(
            f"/api/auth/users/{ids['sales']}/access",
            json={
                "overrides": {
                    "warehouse.view": True,
                    "orders.create": False,
                },
                "mode": "selected",
                "customer_ids": [customer_ids[1]],
            },
        )
        assert access_response.status_code == 200

    with factory() as db:
        target = db.get(User, ids["sales"])
        logs = db.scalars(
            select(OperationLog)
            .where(OperationLog.action.in_(actions))
            .order_by(OperationLog.id)
        ).all()

    assert target.auth_version == 4
    assert [log.action for log in logs] == actions
    expected_versions = [(1, 2), (2, 3), (3, 4)]
    for log, (before_version, after_version) in zip(logs, expected_versions):
        details = json.loads(log.details)
        assert log.user_id == ids["admin"]
        assert log.username == "admin"
        assert log.role == "admin"
        assert log.entity_id == ids["sales"]
        assert details["action"] == log.action
        assert details["actor"] == {
            "user_id": ids["admin"],
            "username": "admin",
            "role": "admin",
        }
        assert details["target_user_id"] == ids["sales"]
        assert details["result"] == "changed"
        assert details["auth_version"] == {
            "before": before_version,
            "after": after_version,
        }
        for side in ("before", "after"):
            snapshot = details[side]
            assert list(snapshot) == [
                "customer_access_mode",
                "customer_ids",
                "effective_permissions",
                "permission_overrides",
            ]
            assert snapshot["customer_ids"] == sorted(snapshot["customer_ids"])
            assert snapshot["effective_permissions"] == sorted(
                snapshot["effective_permissions"]
            )
            assert snapshot["permission_overrides"] == sorted(
                snapshot["permission_overrides"], key=lambda row: row["code"]
            )
            assert all(
                row["decision"] in {"allow", "deny"}
                for row in snapshot["permission_overrides"]
            )
        lowered = log.details.lower()
        for sensitive_name in ("password", "password_hash", "token", "secret"):
            assert sensitive_name not in lowered

    first = json.loads(logs[0].details)
    assert first["after"]["permission_overrides"] == [
        {"code": "cost.view", "decision": "allow"},
        {"code": "orders.create", "decision": "deny"},
    ]
    second = json.loads(logs[1].details)
    assert second["after"]["customer_ids"] == sorted(customer_ids)
    third = json.loads(logs[2].details)
    assert third["after"]["customer_ids"] == [customer_ids[1]]


def test_no_change_access_save_is_audited_without_revoking_session(
    access_control_app,
) -> None:
    import json

    from sqlalchemy import select

    from app.models.audit import OperationLog
    from app.models.user import User

    app, factory, ids, _customer_ids, _engine = access_control_app
    with TestClient(app) as admin_client, TestClient(app) as sales_client:
        _login(admin_client, "admin", "AdminPass123!")
        _login(sales_client, "sales", "SalesPass123!")
        response = admin_client.put(
            f"/api/auth/users/{ids['sales']}/access",
            json={"overrides": {}, "mode": "all", "customer_ids": []},
        )
        assert response.status_code == 200
        assert sales_client.get("/api/auth/me").status_code == 200

    with factory() as db:
        target = db.get(User, ids["sales"])
        log = db.scalar(
            select(OperationLog)
            .where(OperationLog.action == "UPDATE_USER_ACCESS")
            .order_by(OperationLog.id.desc())
        )
    details = json.loads(log.details)
    assert target.auth_version == 1
    assert details["result"] == "no_change"
    assert details["before"] == details["after"]
    assert details["auth_version"] == {"before": 1, "after": 1}


def test_access_audit_failure_rolls_back_configuration_version_and_log(
    access_control_app,
) -> None:
    from sqlalchemy import event, func, select

    from app.models.access_control import UserCustomerScope, UserPermissionOverride
    from app.models.audit import OperationLog
    from app.models.user import User

    app, factory, ids, customer_ids, _engine = access_control_app

    def reject_access_audit(session, _flush_context, _instances) -> None:
        if any(
            isinstance(row, OperationLog) and row.action == "UPDATE_USER_ACCESS"
            for row in session.new
        ):
            raise RuntimeError("simulated audit persistence failure")

    with TestClient(app, raise_server_exceptions=False) as client:
        _login(client, "admin", "AdminPass123!")
        event.listen(factory.class_, "before_flush", reject_access_audit)
        try:
            response = client.put(
                f"/api/auth/users/{ids['sales']}/access",
                json={
                    "overrides": {"cost.view": True},
                    "mode": "selected",
                    "customer_ids": [customer_ids[0]],
                },
            )
        finally:
            event.remove(factory.class_, "before_flush", reject_access_audit)
    assert response.status_code == 500

    with factory() as db:
        target = db.get(User, ids["sales"])
        override_count = db.scalar(
            select(func.count(UserPermissionOverride.id)).where(
                UserPermissionOverride.user_id == ids["sales"]
            )
        )
        scope_count = db.scalar(
            select(func.count(UserCustomerScope.id)).where(
                UserCustomerScope.user_id == ids["sales"]
            )
        )
        log_count = db.scalar(
            select(func.count(OperationLog.id)).where(
                OperationLog.action == "UPDATE_USER_ACCESS"
            )
        )
    assert target.auth_version == 1
    assert target.customer_access_mode == "all"
    assert override_count == 0
    assert scope_count == 0
    assert log_count == 0


@pytest.mark.parametrize("_attempt", range(5))
def test_concurrent_access_updates_return_one_conflict_without_lost_update(
    access_control_app,
    monkeypatch,
    _attempt: int,
) -> None:
    import json
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from sqlalchemy import select

    import app.api.auth as auth_api
    from app.models.access_control import UserPermissionOverride
    from app.models.audit import OperationLog
    from app.models.user import User

    app, factory, ids, _customer_ids, _engine = access_control_app
    barrier = Barrier(2)
    original_claim = auth_api._claim_access_auth_version

    def synchronized_claim(db, *, target, expected_auth_version, increment) -> None:
        barrier.wait(timeout=10)
        original_claim(
            db,
            target=target,
            expected_auth_version=expected_auth_version,
            increment=increment,
        )

    monkeypatch.setattr(auth_api, "_claim_access_auth_version", synchronized_claim)
    payloads = {
        "cost": {
            "overrides": {"cost.view": True},
            "mode": "all",
            "customer_ids": [],
        },
        "warehouse": {
            "overrides": {"warehouse.view": True},
            "mode": "all",
            "customer_ids": [],
        },
    }
    with TestClient(app) as first, TestClient(app) as second:
        _login(first, "admin", "AdminPass123!")
        _login(second, "admin", "AdminPass123!")
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = {
                "cost": executor.submit(
                    first.put,
                    f"/api/auth/users/{ids['sales']}/access",
                    json=payloads["cost"],
                ),
                "warehouse": executor.submit(
                    second.put,
                    f"/api/auth/users/{ids['sales']}/access",
                    json=payloads["warehouse"],
                ),
            }
            responses = {name: future.result(timeout=20) for name, future in futures.items()}

    assert sorted(response.status_code for response in responses.values()) == [200, 409]
    winner = next(name for name, response in responses.items() if response.status_code == 200)
    conflict = next(response for response in responses.values() if response.status_code == 409)
    assert "其他管理员修改" in conflict.json()["detail"]
    expected_code = "cost.view" if winner == "cost" else "warehouse.view"

    with factory() as db:
        target = db.get(User, ids["sales"])
        overrides = db.scalars(
            select(UserPermissionOverride)
            .where(UserPermissionOverride.user_id == ids["sales"])
            .order_by(UserPermissionOverride.permission_code)
        ).all()
        logs = db.scalars(
            select(OperationLog).where(OperationLog.action == "UPDATE_USER_ACCESS")
        ).all()

    assert target.auth_version == 2
    assert [(row.permission_code, row.is_allowed) for row in overrides] == [
        (expected_code, True)
    ]
    assert len(logs) == 1
    details = json.loads(logs[0].details)
    assert details["auth_version"] == {"before": 1, "after": 2}
    assert details["before"]["permission_overrides"] == []
    assert details["after"]["permission_overrides"] == [
        {"code": expected_code, "decision": "allow"}
    ]


def test_audit_effective_permissions_exclude_historical_admin_only_override(
    access_control_app,
) -> None:
    import json

    from sqlalchemy import select

    from app.api.deps import effective_permissions
    from app.models.access_control import UserPermissionOverride
    from app.models.audit import OperationLog
    from app.models.user import User

    app, factory, ids, customer_ids, _engine = access_control_app
    with factory() as db:
        db.add(
            UserPermissionOverride(
                user_id=ids["sales"],
                permission_code="users.manage",
                is_allowed=True,
                granted_by=ids["admin"],
            )
        )
        db.commit()
        assert "users.manage" not in effective_permissions(db.get(User, ids["sales"]))

    with TestClient(app) as client:
        _login(client, "admin", "AdminPass123!")
        response = client.put(
            f"/api/auth/users/{ids['sales']}/customer-scopes",
            json={"mode": "selected", "customer_ids": [customer_ids[0]]},
        )
    assert response.status_code == 200

    with factory() as db:
        log = db.scalar(
            select(OperationLog)
            .where(OperationLog.action == "UPDATE_CUSTOMER_SCOPES")
            .order_by(OperationLog.id.desc())
        )
    details = json.loads(log.details)
    assert details["before"]["permission_overrides"] == [
        {"code": "users.manage", "decision": "allow"}
    ]
    assert "users.manage" not in details["before"]["effective_permissions"]
    assert "users.manage" not in details["after"]["effective_permissions"]


@pytest.mark.parametrize(
    ("route", "noop_payload", "change_payload", "audit_action"),
    [
        (
            "permission-overrides",
            {"overrides": {}},
            {"overrides": {"cost.view": True}},
            "UPDATE_PERMISSION_OVERRIDES",
        ),
        (
            "customer-scopes",
            {"mode": "all", "customer_ids": []},
            {"mode": "selected", "customer_ids": [1]},
            "UPDATE_CUSTOMER_SCOPES",
        ),
        (
            "access",
            {"overrides": {}, "mode": "all", "customer_ids": []},
            {
                "overrides": {"cost.view": True},
                "mode": "selected",
                "customer_ids": [1],
            },
            "UPDATE_USER_ACCESS",
        ),
    ],
)
def test_stale_noop_conflicts_with_concurrent_real_change_for_every_access_route(
    access_control_app,
    monkeypatch,
    route: str,
    noop_payload: dict,
    change_payload: dict,
    audit_action: str,
) -> None:
    import json
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from sqlalchemy import select

    import app.api.auth as auth_api
    from app.models.access_control import UserCustomerScope, UserPermissionOverride
    from app.models.audit import OperationLog
    from app.models.user import User

    app, factory, ids, customer_ids, _engine = access_control_app
    if route in {"customer-scopes", "access"}:
        change_payload = {**change_payload, "customer_ids": [customer_ids[0]]}
    noop_ready = Event()
    real_claimed = Event()
    original_claim = auth_api._claim_access_auth_version

    def ordered_claim(db, *, target, expected_auth_version, increment) -> None:
        if increment:
            assert noop_ready.wait(timeout=10)
            original_claim(
                db,
                target=target,
                expected_auth_version=expected_auth_version,
                increment=True,
            )
            real_claimed.set()
            return
        noop_ready.set()
        assert real_claimed.wait(timeout=10)
        original_claim(
            db,
            target=target,
            expected_auth_version=expected_auth_version,
            increment=False,
        )

    monkeypatch.setattr(auth_api, "_claim_access_auth_version", ordered_claim)
    url = f"/api/auth/users/{ids['sales']}/{route}"
    with TestClient(app) as noop_client, TestClient(app) as change_client:
        _login(noop_client, "admin", "AdminPass123!")
        _login(change_client, "admin", "AdminPass123!")
        with ThreadPoolExecutor(max_workers=2) as executor:
            noop_future = executor.submit(noop_client.put, url, json=noop_payload)
            change_future = executor.submit(change_client.put, url, json=change_payload)
            noop_response = noop_future.result(timeout=20)
            change_response = change_future.result(timeout=20)

    assert change_response.status_code == 200
    assert noop_response.status_code == 409
    assert "其他管理员修改" in noop_response.json()["detail"]
    with factory() as db:
        target = db.get(User, ids["sales"])
        overrides = db.scalars(
            select(UserPermissionOverride).where(
                UserPermissionOverride.user_id == ids["sales"]
            )
        ).all()
        scopes = db.scalars(
            select(UserCustomerScope).where(UserCustomerScope.user_id == ids["sales"])
        ).all()
        logs = db.scalars(
            select(OperationLog).where(OperationLog.action == audit_action)
        ).all()

    assert target.auth_version == 2
    assert len(logs) == 1
    details = json.loads(logs[0].details)
    assert details["result"] == "changed"
    assert details["auth_version"] == {"before": 1, "after": 2}
    if route in {"permission-overrides", "access"}:
        assert [(row.permission_code, row.is_allowed) for row in overrides] == [
            ("cost.view", True)
        ]
    else:
        assert overrides == []
    if route in {"customer-scopes", "access"}:
        assert [row.customer_id for row in scopes] == [customer_ids[0]]
        assert target.customer_access_mode == "selected"
    else:
        assert scopes == []
        assert target.customer_access_mode == "all"
