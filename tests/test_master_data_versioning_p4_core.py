from __future__ import annotations

from collections.abc import Generator
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import jwt
import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.deps import get_current_user, get_db
from app.api.master_data_versions import router
from app.core.config import load_settings
from app.models import Base
from app.models.access_control import UserCustomerScope, UserPermissionOverride
from app.models.audit import OperationLog
from app.models.customer import Customer
from app.models.master_data_object_version import MasterDataObjectVersion
from app.models.material import Material
from app.models.mold_tool import MoldTool
from app.models.product import Product
from app.models.user import User
from app.services.master_data_versioning import (
    apply_versioned_update,
    get_object_version,
    list_object_versions,
    preview_versioned_restore,
    record_versioned_create,
    revision_snapshot,
    serialize_versioned_entity,
    snapshot_updates,
)


@pytest.fixture()
def db(monkeypatch: pytest.MonkeyPatch) -> Generator[Session, None, None]:
    monkeypatch.setenv("ERP_SECRET_KEY", "p4-master-data-versioning-test-secret")
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(dbapi_connection, _record) -> None:
        dbapi_connection.execute("PRAGMA foreign_keys = ON")

    tables = [
        User.__table__,
        Customer.__table__,
        UserPermissionOverride.__table__,
        UserCustomerScope.__table__,
        Material.__table__,
        MoldTool.__table__,
        Product.__table__,
        OperationLog.__table__,
        MasterDataObjectVersion.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)
    session_factory = sessionmaker(
        bind=engine,
        autoflush=False,
        expire_on_commit=False,
    )
    with session_factory() as session:
        yield session
        session.rollback()
    Base.metadata.drop_all(engine, tables=list(reversed(tables)))
    engine.dispose()


def _user(db: Session, suffix: str, *, role: str = "admin") -> User:
    user = User(
        username=f"p4-{suffix}",
        password_hash="not-used",
        role=role,
        real_name=f"P4 {suffix}",
        is_active=True,
        must_change_password=False,
        customer_access_mode="all",
    )
    db.add(user)
    db.flush()
    return user


def _customer(db: Session, suffix: str) -> Customer:
    customer = Customer(
        customer_number=None,
        customer_code=f"P4-{suffix}",
        name=f"P4 客户 {suffix}",
        credit_limit=Decimal("1000.00"),
        default_tax_rate=Decimal("0.13"),
        delivery_method="配送",
        status="active",
        is_active=True,
    )
    db.add(customer)
    db.flush()
    return customer


def _confirmation_error(callable_) -> dict:
    with pytest.raises(HTTPException) as caught:
        callable_()
    assert caught.value.status_code == 409
    detail = caught.value.detail
    assert detail["code"] == "MASTER_CHANGE_CONFIRMATION_REQUIRED"
    assert detail["confirmation_token"]
    return detail


def test_first_legacy_update_writes_v1_baseline_and_v2_without_commit(
    db: Session,
) -> None:
    admin = _user(db, "baseline-admin")
    customer = _customer(db, "baseline")

    revision = apply_versioned_update(
        db,
        object_type="customer",
        entity=customer,
        updates={"remark": "第二版备注"},
        expected_version=1,
        user=admin,
        reason="补充备注",
        source="test.p4",
    )

    assert revision is not None
    assert revision.version == 2
    assert customer.version == 2
    assert customer.remark == "第二版备注"
    rows = list_object_versions(
        db,
        object_type="customer",
        object_id=customer.id,
    )
    assert [(row.version, row.action) for row in rows] == [
        (2, "update"),
        (1, "baseline"),
    ]
    assert revision_snapshot(rows[1])["name"] == customer.name
    assert revision_snapshot(rows[0])["remark"] == "第二版备注"
    assert rows[0].change_set_id == rows[1].change_set_id
    assert rows[0].operation_log_id == rows[1].operation_log_id
    assert db.scalar(select(func.count()).select_from(OperationLog)) == 1
    assert db.in_transaction()


def test_create_snapshot_normalizes_json_and_no_change_adds_no_version(
    db: Session,
) -> None:
    admin = _user(db, "create-admin")
    customer = _customer(db, "create")
    created = record_versioned_create(
        db,
        object_type="customer",
        entity=customer,
        user=admin,
        reason="新建客户",
        source="test.p4",
    )

    snapshot = revision_snapshot(created)
    assert snapshot["credit_limit"] == "1000"
    assert snapshot["default_tax_rate"] == "0.13"
    assert "id" not in snapshot
    assert "version" not in snapshot
    assert "created_at" not in snapshot
    assert "updated_at" not in snapshot

    result = apply_versioned_update(
        db,
        object_type="customer",
        entity=customer,
        updates={"remark": customer.remark},
        expected_version=1,
        user=admin,
        reason="无实际变化",
        source="test.p4",
    )
    assert result is None
    assert customer.version == 1
    assert len(
        list_object_versions(
            db,
            object_type="customer",
            object_id=customer.id,
        )
    ) == 1


def test_confirmation_token_is_bound_to_user_object_version_after_hash_and_expiry(
    db: Session,
) -> None:
    admin = _user(db, "token-admin")
    other_admin = _user(db, "token-other")
    customer = _customer(db, "token-a")
    other_customer = _customer(db, "token-bb")
    updates = {"name": "P4 客户 token-a 新名称"}

    detail = _confirmation_error(
        lambda: apply_versioned_update(
            db,
            object_type="customer",
            entity=customer,
            updates=updates,
            expected_version=1,
            user=admin,
            reason="更名",
            source="test.p4",
        )
    )
    token = detail["confirmation_token"]
    assert detail["current_version"] == 1
    assert detail["changed_fields"] == ["name"]
    assert {item["code"] for item in detail["warnings"]} == {
        "CUSTOMER_IDENTITY_CHANGE"
    }

    for bound_entity, bound_user, bound_updates in (
        (customer, other_admin, updates),
        (other_customer, admin, updates),
        (customer, admin, {"name": "另一个变更后快照"}),
    ):
        rebound = _confirmation_error(
            lambda entity=bound_entity, user=bound_user, data=bound_updates: (
                apply_versioned_update(
                    db,
                    object_type="customer",
                    entity=entity,
                    updates=data,
                    expected_version=1,
                    user=user,
                    reason="绑定校验",
                    source="test.p4",
                    confirmation_token=token,
                )
            )
        )
        assert rebound["confirmation_token"] != token

    action_rebound = _confirmation_error(
        lambda: apply_versioned_update(
            db,
            object_type="customer",
            entity=customer,
            updates=updates,
            expected_version=1,
            user=admin,
            reason="操作类型绑定校验",
            source="test.p4",
            action="status_change",
            confirmation_token=token,
        )
    )
    assert action_rebound["confirmation_token"] != token

    claims = jwt.decode(
        token,
        options={"verify_signature": False},
        algorithms=["HS256"],
    )
    assert claims["action"] == "update"
    assert claims["purpose"] == "master_data_update"
    assert claims["target_version"] == 2
    assert claims["restored_from_version"] == 0
    claims["exp"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    expired = jwt.encode(
        claims,
        load_settings().secret_key,
        algorithm="HS256",
    )
    _confirmation_error(
        lambda: apply_versioned_update(
            db,
            object_type="customer",
            entity=customer,
            updates=updates,
            expected_version=1,
            user=admin,
            reason="过期令牌",
            source="test.p4",
            confirmation_token=expired,
        )
    )

    revision = apply_versioned_update(
        db,
        object_type="customer",
        entity=customer,
        updates=updates,
        expected_version=1,
        user=admin,
        reason="确认更名",
        source="test.p4",
        confirmation_token=token,
    )
    assert revision is not None
    assert revision.version == 2
    assert customer.name == updates["name"]


def test_stale_expected_version_raises_stable_conflict_detail(db: Session) -> None:
    admin = _user(db, "conflict-admin")
    customer = _customer(db, "conflict")
    first = apply_versioned_update(
        db,
        object_type="customer",
        entity=customer,
        updates={"remark": "v2"},
        expected_version=1,
        user=admin,
        reason="第一次更新",
        source="test.p4",
    )
    assert first is not None

    with pytest.raises(HTTPException) as caught:
        apply_versioned_update(
            db,
            object_type="customer",
            entity=customer,
            updates={"remark": "过期更新"},
            expected_version=1,
            user=admin,
            reason="使用旧版本",
            source="test.p4",
        )
    assert caught.value.status_code == 409
    assert caught.value.detail == {
        "code": "MASTER_VERSION_CONFLICT",
        "expected_version": 1,
        "current_version": 2,
    }


def test_restore_appends_new_version_and_keeps_all_history(db: Session) -> None:
    admin = _user(db, "restore-admin")
    customer = _customer(db, "restore")
    v1 = record_versioned_create(
        db,
        object_type="customer",
        entity=customer,
        user=admin,
        reason="创建",
        source="test.p4",
    )
    v2 = apply_versioned_update(
        db,
        object_type="customer",
        entity=customer,
        updates={"remark": "当前备注"},
        expected_version=1,
        user=admin,
        reason="更新备注",
        source="test.p4",
    )
    assert v2 is not None

    preview = preview_versioned_restore(
        db,
        object_type="customer",
        entity=customer,
        revision=v1,
        expected_version=2,
        user=admin,
    )
    assert preview["changed_fields"] == ["remark"]
    assert preview["can_restore"] is True
    assert "MASTER_DATA_RESTORE" in {
        item["code"] for item in preview["warnings"]
    }

    v3 = apply_versioned_update(
        db,
        object_type="customer",
        entity=customer,
        updates=snapshot_updates("customer", v1),
        expected_version=2,
        user=admin,
        reason="恢复初始资料",
        source="test.p4",
        action="restore",
        confirmation_token=preview["confirmation_token"],
        restored_from_version=1,
    )
    assert v3 is not None
    assert v3.version == 3
    assert v3.action == "restore"
    assert v3.restored_from_version == 1
    assert customer.version == 3
    assert customer.remark is None
    assert [
        row.version
        for row in list_object_versions(
            db,
            object_type="customer",
            object_id=customer.id,
        )
    ] == [3, 2, 1]


def test_minimum_anomaly_rules_cover_product_material_and_numeric_changes(
    db: Session,
) -> None:
    admin = _user(db, "rules-admin")
    customer = _customer(db, "rules-customer")
    other_customer = _customer(db, "rules-other-cc")
    material = Material(
        code="P4MAT",
        supplier_name="供应商甲",
        layer_count=3,
        quote_price=Decimal("2.00"),
        is_active=True,
    )
    db.add(material)
    db.flush()
    product = Product(
        customer_id=customer.id,
        product_code="P4-P001",
        customer_material_code="P4-C001",
        product_name="P4 测试产品",
        material_id=material.id,
        length_mm=Decimal("10"),
        width_mm=Decimal("20"),
        height_mm=Decimal("30"),
        box_category="normal",
        unit="只",
        flute_type="B",
        layer_count=3,
        is_active=True,
    )
    db.add(product)
    db.flush()

    product_detail = _confirmation_error(
        lambda: apply_versioned_update(
            db,
            object_type="product",
            entity=product,
            updates={
                "product_code": "P4-P002",
                "customer_id": other_customer.id,
                "length_mm": Decimal("50"),
                "flute_type": "E",
            },
            expected_version=1,
            user=admin,
            reason="异常规则",
            source="test.p4",
        )
    )
    product_codes = {item["code"] for item in product_detail["warnings"]}
    assert {
        "PRODUCT_CODE_CHANGE",
        "PRODUCT_CUSTOMER_CHANGE",
        "PRODUCT_MATERIAL_FLUTE_CHANGE",
        "NUMERIC_VALUE_MAGNITUDE_CHANGE",
    } <= product_codes

    material_detail = _confirmation_error(
        lambda: apply_versioned_update(
            db,
            object_type="material",
            entity=material,
            updates={
                "code": "P4MAT2",
                "supplier_name": "供应商乙",
                "layer_count": 5,
                "quote_price": None,
            },
            expected_version=1,
            user=admin,
            reason="异常规则",
            source="test.p4",
        )
    )
    material_codes = {item["code"] for item in material_detail["warnings"]}
    assert {
        "MATERIAL_CODE_CHANGE",
        "MATERIAL_SUPPLIER_CHANGE",
        "MATERIAL_LAYER_COUNT_CHANGE",
        "NUMERIC_VALUE_CLEARED",
    } <= material_codes

    product_snapshot = serialize_versioned_entity("product", product)
    assert "die_cut_path" not in product_snapshot
    assert "drawings" not in product_snapshot
    assert "deleted_at" not in product_snapshot


def test_product_history_restore_rejects_trash_purged_and_soft_delete_state(
    db: Session,
) -> None:
    admin = _user(db, "trash-restore-admin")
    customer = _customer(db, "trash-restore-customer")
    product = Product(
        customer_id=customer.id,
        product_code="P4-TRASH-RESTORE",
        customer_material_code="P4-C-TRASH-RESTORE",
        product_name="P4 垃圾站恢复保护产品",
        box_category="normal",
        unit="只",
        is_active=True,
    )
    db.add(product)
    db.flush()
    active_revision = record_versioned_create(
        db,
        object_type="product",
        entity=product,
        user=admin,
        reason="创建产品",
        source="test.p4",
    )
    deleted_revision = apply_versioned_update(
        db,
        object_type="product",
        entity=product,
        updates={"is_active": False},
        expected_version=1,
        user=admin,
        reason="移入垃圾站",
        source="test.p4",
        action="soft_delete",
    )
    assert deleted_revision is not None
    product.deleted_at = datetime.now()
    product.deleted_by = admin.id

    with pytest.raises(ValueError, match="专用垃圾站恢复接口"):
        preview_versioned_restore(
            db,
            object_type="product",
            entity=product,
            revision=active_revision,
            expected_version=2,
            user=admin,
        )

    product.purged_at = datetime.now()
    with pytest.raises(ValueError, match="永久归档"):
        preview_versioned_restore(
            db,
            object_type="product",
            entity=product,
            revision=active_revision,
            expected_version=2,
            user=admin,
        )

    product.deleted_at = None
    product.deleted_by = None
    product.purged_at = None
    product.is_active = True
    with pytest.raises(ValueError, match="垃圾站生命周期版本"):
        preview_versioned_restore(
            db,
            object_type="product",
            entity=product,
            revision=deleted_revision,
            expected_version=2,
            user=admin,
        )
    with pytest.raises(ValueError, match="垃圾站生命周期版本"):
        apply_versioned_update(
            db,
            object_type="product",
            entity=product,
            updates=snapshot_updates("product", deleted_revision),
            expected_version=2,
            user=admin,
            reason="禁止恢复到软删除版本",
            source="test.p4",
            action="restore",
            confirmation_token="not-relevant",
            restored_from_version=deleted_revision.version,
        )


def test_restore_api_rejects_non_admin(db: Session) -> None:
    admin = _user(db, "api-admin")
    sales = _user(db, "api-sales", role="sales")
    customer = _customer(db, "api-restore")
    target = record_versioned_create(
        db,
        object_type="customer",
        entity=customer,
        user=admin,
        reason="创建",
        source="test.p4",
    )
    updated = apply_versioned_update(
        db,
        object_type="customer",
        entity=customer,
        updates={"remark": "当前备注"},
        expected_version=1,
        user=admin,
        reason="更新",
        source="test.p4",
    )
    assert updated is not None

    app = FastAPI()
    app.include_router(router, prefix="/api/master-data")

    def override_get_db() -> Generator[Session, None, None]:
        yield db

    def override_current_user() -> User:
        return sales

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user
    with TestClient(app) as client:
        preview = client.post(
            f"/api/master-data/customer/{customer.id}/versions/{target.version}/restore-preview",
            json={"expected_version": 2},
        )
        restore = client.post(
            f"/api/master-data/customer/{customer.id}/versions/{target.version}/restore",
            json={
                "expected_version": 2,
                "reason": "越权恢复",
                "confirmation_token": "not-authorized",
            },
        )
    assert preview.status_code == 403
    assert restore.status_code == 403
    assert customer.version == 2
    assert get_object_version(
        db,
        object_type="customer",
        object_id=customer.id,
        version=3,
    ) is None


def test_history_api_enforces_view_permission_and_customer_scope(db: Session) -> None:
    admin = _user(db, "scope-admin")
    sales = _user(db, "scope-sales", role="sales")
    sales.customer_access_mode = "selected"
    visible = _customer(db, "scope-visible")
    hidden = _customer(db, "scope-hidden")
    db.add(
        UserCustomerScope(
            user_id=sales.id,
            customer_id=visible.id,
            assigned_by=admin.id,
        )
    )
    record_versioned_create(
        db,
        object_type="customer",
        entity=visible,
        user=admin,
        reason="创建",
        source="test.p4",
    )
    record_versioned_create(
        db,
        object_type="customer",
        entity=hidden,
        user=admin,
        reason="创建",
        source="test.p4",
    )
    db.flush()

    app = FastAPI()
    app.include_router(router, prefix="/api/master-data")

    def override_get_db() -> Generator[Session, None, None]:
        yield db

    def override_current_user() -> User:
        return sales

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user
    with TestClient(app) as client:
        allowed = client.get(
            f"/api/master-data/customer/{visible.id}/versions"
        )
        denied = client.get(
            f"/api/master-data/customer/{hidden.id}/versions"
        )
    assert allowed.status_code == 200
    assert allowed.json()["total"] == 1
    assert denied.status_code == 403
    assert denied.json()["detail"] == "无客户访问权限"


def test_restore_api_commits_confirmed_restore_as_next_version(db: Session) -> None:
    admin = _user(db, "api-success-admin")
    customer = _customer(db, "api-success")
    target = record_versioned_create(
        db,
        object_type="customer",
        entity=customer,
        user=admin,
        reason="创建",
        source="test.p4",
    )
    updated = apply_versioned_update(
        db,
        object_type="customer",
        entity=customer,
        updates={"remark": "待恢复备注"},
        expected_version=1,
        user=admin,
        reason="更新",
        source="test.p4",
    )
    assert updated is not None
    db.commit()

    app = FastAPI()
    app.include_router(router, prefix="/api/master-data")

    def override_get_db() -> Generator[Session, None, None]:
        yield db

    def override_current_user() -> User:
        return admin

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user
    with TestClient(app) as client:
        preview = client.post(
            f"/api/master-data/customer/{customer.id}/versions/{target.version}/restore-preview",
            json={"expected_version": 2},
        )
        assert preview.status_code == 200
        token = preview.json()["confirmation_token"]
        restored = client.post(
            f"/api/master-data/customer/{customer.id}/versions/{target.version}/restore",
            json={
                "expected_version": 2,
                "reason": "恢复初始客户资料",
                "confirmation_token": token,
            },
        )
    assert restored.status_code == 200
    assert restored.json()["version"] == 3
    assert restored.json()["restored_from_version"] == 1
    assert customer.remark is None
    assert customer.version == 3


def test_product_history_redaction_matches_current_product_api(db: Session) -> None:
    admin = _user(db, "product-history-admin")
    sales = _user(db, "product-history-sales", role="sales")
    workshop = _user(db, "product-history-workshop", role="workshop")
    customer = _customer(db, "product-history")
    product = Product(
        customer_id=customer.id,
        product_code="P4-HISTORY-PRICE",
        customer_material_code="P4-C-HISTORY-PRICE",
        product_name="P4 历史价格脱敏产品",
        box_category="normal",
        unit="只",
        sale_unit_price=Decimal("12.00"),
        sale_unit_price_no_tax=Decimal("10.62"),
        cost_unit_price=Decimal("8.00"),
        board_price=Decimal("6.00"),
        suggested_price=Decimal("13.00"),
        is_active=True,
    )
    db.add(product)
    db.flush()
    revision = record_versioned_create(
        db,
        object_type="product",
        entity=product,
        user=admin,
        reason="创建含完整价格的产品",
        source="test.p4",
    )
    db.add(
        UserPermissionOverride(
            user_id=workshop.id,
            permission_code="cost.view",
            is_allowed=True,
            granted_by=admin.id,
        )
    )
    db.commit()

    app = FastAPI()
    app.include_router(router, prefix="/api/master-data")
    current_user = {"value": sales}

    def override_get_db() -> Generator[Session, None, None]:
        yield db

    def override_current_user() -> User:
        return current_user["value"]

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user
    with TestClient(app) as client:
        sales_detail = client.get(
            f"/api/master-data/product/{product.id}/versions/{revision.version}"
        )
        current_user["value"] = workshop
        workshop_detail = client.get(
            f"/api/master-data/product/{product.id}/versions/{revision.version}"
        )

    cost_fields = {"cost_unit_price", "board_price", "suggested_price"}
    all_price_fields = cost_fields | {
        "sale_unit_price",
        "sale_unit_price_no_tax",
    }
    assert sales_detail.status_code == 200
    assert set(sales_detail.json()["snapshot"]).isdisjoint(cost_fields)
    assert {
        "sale_unit_price",
        "sale_unit_price_no_tax",
    } <= set(sales_detail.json()["snapshot"])
    assert set(sales_detail.json()["changed_fields"]).isdisjoint(cost_fields)
    assert workshop_detail.status_code == 200
    assert set(workshop_detail.json()["snapshot"]).isdisjoint(all_price_fields)
    assert set(workshop_detail.json()["changed_fields"]).isdisjoint(
        all_price_fields
    )


def test_history_without_cost_view_redacts_material_price_fields(db: Session) -> None:
    admin = _user(db, "history-cost-admin")
    sales = _user(db, "history-no-cost", role="sales")
    workshop = _user(db, "history-workshop", role="workshop")
    material = Material(
        code="P4COST",
        supplier_name="P4价格供应商",
        layer_count=3,
        quote_price=Decimal("2.50"),
        rule_base_price=Decimal("2.20"),
        price_source="报价单",
        price_unit="元/㎡",
        is_active=True,
    )
    db.add(material)
    db.flush()
    first = record_versioned_create(
        db,
        object_type="material",
        entity=material,
        user=admin,
        reason="创建含价格材质",
        source="test.p4",
    )
    second = apply_versioned_update(
        db,
        object_type="material",
        entity=material,
        updates={"price_source": "最新报价单"},
        expected_version=1,
        user=admin,
        reason="更新报价来源",
        source="test.p4",
    )
    assert second is not None
    db.commit()

    app = FastAPI()
    app.include_router(router, prefix="/api/master-data")

    def override_get_db() -> Generator[Session, None, None]:
        yield db

    current_user = {"value": sales}

    def override_current_user() -> User:
        return current_user["value"]

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user
    with TestClient(app) as client:
        history = client.get(f"/api/master-data/material/{material.id}/versions")
        detail = client.get(
            f"/api/master-data/material/{material.id}/versions/{first.version}"
        )
        current_user["value"] = workshop
        workshop_detail = client.get(
            f"/api/master-data/material/{material.id}/versions/{first.version}"
        )

    sensitive = {
        "quote_price",
        "rule_base_price",
        "price_source",
        "price_unit",
        "supplier_name",
    }
    assert history.status_code == 200
    assert detail.status_code == 200
    assert set(history.json()["items"][0]["changed_fields"]).isdisjoint(sensitive)
    assert set(detail.json()["changed_fields"]).isdisjoint(sensitive)
    assert set(detail.json()["snapshot"]).isdisjoint(sensitive)
    assert workshop_detail.status_code == 200
    assert set(workshop_detail.json()["changed_fields"]).isdisjoint(
        sensitive | {"quote_date", "remarks"}
    )
    assert set(workshop_detail.json()["snapshot"]).isdisjoint(
        sensitive | {"quote_date", "remarks"}
    )


def test_restore_token_is_bound_to_historical_source_version(db: Session) -> None:
    admin = _user(db, "restore-reference-admin")
    customer = _customer(db, "restore-reference")
    first = record_versioned_create(
        db,
        object_type="customer",
        entity=customer,
        user=admin,
        reason="创建",
        source="test.p4",
    )
    second = apply_versioned_update(
        db,
        object_type="customer",
        entity=customer,
        updates={"remark": "中间值"},
        expected_version=1,
        user=admin,
        reason="更新到中间值",
        source="test.p4",
    )
    assert second is not None
    third = apply_versioned_update(
        db,
        object_type="customer",
        entity=customer,
        updates={"remark": None},
        expected_version=2,
        user=admin,
        reason="恢复到与v1相同的业务快照",
        source="test.p4",
    )
    assert third is not None
    fourth = apply_versioned_update(
        db,
        object_type="customer",
        entity=customer,
        updates={"remark": "当前值"},
        expected_version=3,
        user=admin,
        reason="形成待恢复当前版本",
        source="test.p4",
    )
    assert fourth is not None

    preview = preview_versioned_restore(
        db,
        object_type="customer",
        entity=customer,
        revision=first,
        expected_version=4,
        user=admin,
    )
    claims = jwt.decode(
        preview["confirmation_token"],
        options={"verify_signature": False},
        algorithms=["HS256"],
    )
    assert claims["action"] == "restore"
    assert claims["purpose"] == "historical_restore"
    assert claims["target_version"] == 5
    assert claims["restored_from_version"] == 1

    rebound = _confirmation_error(
        lambda: apply_versioned_update(
            db,
            object_type="customer",
            entity=customer,
            updates=snapshot_updates("customer", third),
            expected_version=4,
            user=admin,
            reason="尝试跨历史版本复用令牌",
            source="test.p4",
            action="restore",
            confirmation_token=preview["confirmation_token"],
            restored_from_version=3,
        )
    )
    assert rebound["confirmation_token"] != preview["confirmation_token"]
    assert customer.version == 4
    assert customer.remark == "当前值"

    restored = apply_versioned_update(
        db,
        object_type="customer",
        entity=customer,
        updates=snapshot_updates("customer", first),
        expected_version=4,
        user=admin,
        reason="使用正确历史来源令牌恢复",
        source="test.p4",
        action="restore",
        confirmation_token=preview["confirmation_token"],
        restored_from_version=1,
    )
    assert restored is not None
    assert restored.version == 5
    assert customer.remark is None


def test_restore_audit_records_source_versions_and_linked_revision(
    db: Session,
) -> None:
    admin = _user(db, "restore-audit-admin")
    customer = _customer(db, "restore-audit")
    first = record_versioned_create(
        db,
        object_type="customer",
        entity=customer,
        user=admin,
        reason="创建",
        source="test.p4",
    )
    updated = apply_versioned_update(
        db,
        object_type="customer",
        entity=customer,
        updates={"remark": "恢复前备注"},
        expected_version=1,
        user=admin,
        reason="更新",
        source="test.p4",
    )
    assert updated is not None
    preview = preview_versioned_restore(
        db,
        object_type="customer",
        entity=customer,
        revision=first,
        expected_version=2,
        user=admin,
    )
    restored = apply_versioned_update(
        db,
        object_type="customer",
        entity=customer,
        updates=snapshot_updates("customer", first),
        expected_version=2,
        user=admin,
        reason="恢复初始客户资料",
        source="test.p4.restore-audit",
        action="restore",
        confirmation_token=preview["confirmation_token"],
        restored_from_version=1,
    )
    assert restored is not None

    audit = db.get(OperationLog, restored.operation_log_id)
    assert audit is not None
    assert audit.action == "MASTER_RESTORE"
    assert audit.resource == "master_data.customer"
    assert audit.entity_id == customer.id
    assert json.loads(audit.details or "{}") == {
        "changed_fields": {"remark": {"after": None, "before": "恢复前备注"}},
        "from_version": 2,
        "object_id": customer.id,
        "object_type": "customer",
        "reason": "恢复初始客户资料",
        "restored_from_version": 1,
        "source": "test.p4.restore-audit",
        "to_version": 3,
    }
