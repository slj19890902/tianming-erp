from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker


@pytest.fixture()
def moq_app(tmp_path):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.requisition_moq import router as moq_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.access_control import UserPermissionOverride
    from app.models.customer import Customer
    from app.models.supplier import Supplier
    from app.models.user import User
    from app.services.supplier_master import normalize_supplier_identity

    engine = create_sqlite_engine(tmp_path / "p1-11d1-moq.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        users = [
            User(username="moq-admin", password_hash=hash_password("123456"), role="admin", real_name="MOQ Admin", must_change_password=False),
            User(username="moq-boss", password_hash=hash_password("123456"), role="boss", real_name="MOQ Boss", must_change_password=False),
            User(username="moq-sales", password_hash=hash_password("123456"), role="sales", real_name="MOQ Sales", must_change_password=False),
            User(
                username="moq-config",
                password_hash=hash_password("123456"),
                role="sales",
                real_name="MOQ Config",
                must_change_password=False,
                permission_overrides=[
                    UserPermissionOverride(permission_code="requisition.config", is_allowed=True),
                    UserPermissionOverride(permission_code="requisition.view", is_allowed=True),
                ],
            ),
        ]
        suppliers = [
            Supplier(
                standard_name="匿名纸板供应商甲",
                normalized_name=normalize_supplier_identity("匿名纸板供应商甲"),
                display_name="匿名甲",
                is_active=True,
                version=1,
            ),
            Supplier(
                standard_name="匿名纸板供应商乙",
                normalized_name=normalize_supplier_identity("匿名纸板供应商乙"),
                display_name="匿名乙",
                is_active=False,
                version=1,
            ),
        ]
        db.add_all(users + suppliers + [Customer(name="匿名客户", customer_code="MOQ-C", is_active=True)])
        db.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(moq_router, prefix="/api/requisition/moq-rules")

    def override_get_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    app.state.session_factory = factory
    yield app
    engine.dispose()


def _login(client: TestClient, username: str) -> None:
    response = client.post("/api/auth/login", json={"username": username, "password": "123456"})
    assert response.status_code == 200


def _ids(app: FastAPI) -> tuple[int, int, int]:
    from app.models.customer import Customer
    from app.models.supplier import Supplier

    with app.state.session_factory() as db:
        supplier_ids = list(db.scalars(select(Supplier.id).order_by(Supplier.id)))
        customer_id = int(db.scalar(select(Customer.id)))
    return int(supplier_ids[0]), int(supplier_ids[1]), customer_id


def _payload(supplier_id: int, **overrides) -> dict:
    payload = {
        "supplier_id": supplier_id,
        "scope_type": "supplier",
        "scope_value": None,
        "minimum_quantity": "100",
        "unit": "sheets",
        "merge_allowed": True,
        "merge_window_days": 7,
        "effective_from": "2026-08-14",
        "effective_to": None,
        "evidence_reference": "匿名供应商确认单 A-001",
    }
    payload.update(overrides)
    return payload


def test_config_permission_is_separate_from_requisition_and_boss_defaults(moq_app: FastAPI) -> None:
    supplier_id, _, _ = _ids(moq_app)
    with TestClient(moq_app) as client:
        for username in ("moq-sales", "moq-boss"):
            _login(client, username)
            assert client.get("/api/requisition/moq-rules").status_code == 403
            assert client.post("/api/requisition/moq-rules", json=_payload(supplier_id)).status_code == 403

        _login(client, "moq-config")
        assert client.get("/api/requisition/moq-rules").status_code == 200
        assert client.post("/api/requisition/moq-rules", json=_payload(supplier_id)).status_code == 201


def test_create_list_scope_normalization_and_audit(moq_app: FastAPI) -> None:
    from app.models.audit import OperationLog
    from app.models.supplier_moq import SupplierMinimumOrderRule

    supplier_id, _, customer_id = _ids(moq_app)
    with TestClient(moq_app) as client:
        _login(client, "moq-admin")
        created = client.post(
            "/api/requisition/moq-rules",
            json=_payload(
                supplier_id,
                scope_type="customer",
                scope_value=f"  {customer_id}  ",
                unit="square_meters",
                minimum_quantity="12.5000",
            ),
        )
        assert created.status_code == 201, created.text
        row = created.json()
        assert row["scope_value"] == str(customer_id)
        assert row["scope_label"] == "匿名客户"
        assert row["minimum_quantity"] == "12.5"
        assert row["version"] == 1
        assert row["source"] == "manual_confirmation"
        listed = client.get(
            "/api/requisition/moq-rules",
            params={"supplier_id": supplier_id, "scope_type": "customer", "status": "active"},
        )
        assert listed.status_code == 200
        assert listed.json()["total"] == 1
        assert listed.json()["items"][0]["id"] == row["id"]

    with moq_app.state.session_factory() as db:
        stored = db.scalar(select(SupplierMinimumOrderRule))
        assert stored.minimum_quantity == Decimal("12.5000")
        audits = list(db.scalars(select(OperationLog).where(OperationLog.resource == "SupplierMinimumOrderRule")))
        assert len(audits) == 1
        assert audits[0].action == "CREATE"


def test_overlap_is_inclusive_and_inactive_history_does_not_block(moq_app: FastAPI) -> None:
    supplier_id, _, _ = _ids(moq_app)
    with TestClient(moq_app) as client:
        _login(client, "moq-admin")
        first = client.post(
            "/api/requisition/moq-rules",
            json=_payload(supplier_id, effective_to="2026-08-31"),
        )
        assert first.status_code == 201
        overlap = client.post(
            "/api/requisition/moq-rules",
            json=_payload(supplier_id, effective_from="2026-08-31", effective_to="2026-09-30"),
        )
        assert overlap.status_code == 409
        assert "生效区间重叠" in overlap.json()["detail"]
        adjacent = client.post(
            "/api/requisition/moq-rules",
            json=_payload(supplier_id, effective_from="2026-09-01", effective_to="2026-09-30"),
        )
        assert adjacent.status_code == 201, adjacent.text
        disabled = client.put(
            f"/api/requisition/moq-rules/{first.json()['id']}/status",
            json={"expected_version": 1, "status": "inactive"},
        )
        assert disabled.status_code == 200
        replacement = client.post(
            "/api/requisition/moq-rules",
            json=_payload(supplier_id, effective_to="2026-08-31"),
        )
        assert replacement.status_code == 201
        reactivate = client.put(
            f"/api/requisition/moq-rules/{first.json()['id']}/status",
            json={"expected_version": 2, "status": "active"},
        )
        assert reactivate.status_code == 409


def test_update_is_versioned_and_conflict_rolls_back(moq_app: FastAPI) -> None:
    from app.models.supplier_moq import SupplierMinimumOrderRule

    supplier_id, _, _ = _ids(moq_app)
    with TestClient(moq_app) as client:
        _login(client, "moq-admin")
        first = client.post("/api/requisition/moq-rules", json=_payload(supplier_id)).json()
        changed_payload = _payload(supplier_id, minimum_quantity="120", expected_version=1)
        changed = client.put(f"/api/requisition/moq-rules/{first['id']}", json=changed_payload)
        assert changed.status_code == 200, changed.text
        assert changed.json()["version"] == 2
        assert changed.json()["minimum_quantity"] == "120"
        stale = client.put(f"/api/requisition/moq-rules/{first['id']}", json=changed_payload)
        assert stale.status_code == 409
        assert "版本" in stale.json()["detail"]

    with moq_app.state.session_factory() as db:
        stored = db.get(SupplierMinimumOrderRule, first["id"])
        assert stored.version == 2
        assert stored.minimum_quantity == Decimal("120")


@pytest.mark.parametrize(
    "changes, expected_text",
    [
        ({"minimum_quantity": "0"}, "greater than 0"),
        ({"unit": "boxes"}, "Input should be"),
        ({"merge_allowed": True, "merge_window_days": None}, "合并窗口"),
        ({"merge_allowed": False, "merge_window_days": 7}, "不允许合并"),
        ({"effective_to": "2026-08-13"}, "结束日期"),
        ({"scope_type": "supplier", "scope_value": "X"}, "供应商全局范围"),
    ],
)
def test_payload_validation_is_fail_closed(moq_app: FastAPI, changes: dict, expected_text: str) -> None:
    supplier_id, _, _ = _ids(moq_app)
    with TestClient(moq_app) as client:
        _login(client, "moq-admin")
        response = client.post("/api/requisition/moq-rules", json=_payload(supplier_id, **changes))
        assert response.status_code == 422
        assert expected_text in response.text


def test_inactive_supplier_and_unknown_scope_customer_are_rejected(moq_app: FastAPI) -> None:
    supplier_id, inactive_supplier_id, _ = _ids(moq_app)
    with TestClient(moq_app) as client:
        _login(client, "moq-admin")
        inactive = client.post("/api/requisition/moq-rules", json=_payload(inactive_supplier_id))
        assert inactive.status_code == 409
        unknown = client.post(
            "/api/requisition/moq-rules",
            json=_payload(supplier_id, scope_type="customer", scope_value="999999"),
        )
        assert unknown.status_code == 422


def test_existing_rule_can_be_disabled_after_supplier_is_deactivated(moq_app: FastAPI) -> None:
    from app.models.supplier import Supplier

    supplier_id, _, _ = _ids(moq_app)
    with TestClient(moq_app) as client:
        _login(client, "moq-admin")
        created = client.post("/api/requisition/moq-rules", json=_payload(supplier_id))
        assert created.status_code == 201
        with moq_app.state.session_factory() as db:
            supplier = db.get(Supplier, supplier_id)
            supplier.is_active = False
            supplier.version += 1
            db.commit()
        disabled = client.put(
            f"/api/requisition/moq-rules/{created.json()['id']}/status",
            json={"expected_version": 1, "status": "inactive"},
        )
        assert disabled.status_code == 200, disabled.text
        assert disabled.json()["status"] == "inactive"


def test_no_order_requisition_or_inventory_fact_is_written(moq_app: FastAPI) -> None:
    from app.models.audit import OperationLog
    from app.models.supplier_moq import SupplierMinimumOrderRule

    supplier_id, _, _ = _ids(moq_app)
    with moq_app.state.session_factory() as db:
        protected_before = {
            table: db.scalar(select(func.count()).select_from(model))
            for table, model in (
                ("orders", __import__("app.models.order", fromlist=["Order"]).Order),
                ("requisitions", __import__("app.models.requisition", fromlist=["Requisition"]).Requisition),
                ("lots", __import__("app.models.warehouse_inventory", fromlist=["InventoryLot"]).InventoryLot),
            )
        }
    with TestClient(moq_app) as client:
        _login(client, "moq-admin")
        assert client.post("/api/requisition/moq-rules", json=_payload(supplier_id)).status_code == 201
    with moq_app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(SupplierMinimumOrderRule)) == 1
        assert db.scalar(select(func.count()).select_from(OperationLog).where(OperationLog.resource == "SupplierMinimumOrderRule")) == 1
        protected_after = {
            table: db.scalar(select(func.count()).select_from(model))
            for table, model in (
                ("orders", __import__("app.models.order", fromlist=["Order"]).Order),
                ("requisitions", __import__("app.models.requisition", fromlist=["Requisition"]).Requisition),
                ("lots", __import__("app.models.warehouse_inventory", fromlist=["InventoryLot"]).InventoryLot),
            )
        }
    assert protected_after == protected_before
