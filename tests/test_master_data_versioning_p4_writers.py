from __future__ import annotations

from collections.abc import Generator
from datetime import date, datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture()
def writer_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    from app.api.customers import router as customers_router
    from app.api.deps import get_current_user, get_db
    from app.api.materials import router as materials_router
    from app.api.master_data_versions import router as master_data_versions_router
    from app.api.products import router as products_router
    from app.core.database import create_sqlite_engine
    from app.models import Base
    from app.models.supplier import Supplier
    from app.models.user import User
    from app.services import material_price_adjust
    from app.services.supplier_master import normalize_supplier_identity

    monkeypatch.setenv("ERP_SECRET_KEY", "p4-writer-tests-only")
    engine = create_sqlite_engine(tmp_path / "p4-writers.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as session:
        admin = User(
            username="p4-writer-admin",
            password_hash="not-used",
            role="admin",
            real_name="P4 Writer Admin",
            display_name="P4 Writer Admin",
            is_active=True,
            must_change_password=False,
            customer_access_mode="all",
        )
        session.add(admin)
        for index, supplier_name in enumerate(
            (
                "P4供应商A1",
                "P4供应商LIF",
                "P4批量供应商",
                "P4成功调价供应商",
                "P4事务供应商",
            ),
            start=1,
        ):
            session.add(
                Supplier(
                    standard_name=supplier_name,
                    normalized_name=normalize_supplier_identity(supplier_name),
                    display_name=supplier_name,
                    sort_order=index * 10,
                    is_active=True,
                    version=1,
                )
            )
        session.commit()
        admin_id = admin.id

    app = FastAPI()
    app.include_router(customers_router, prefix="/api/master/customers")
    app.include_router(products_router, prefix="/api/master/products")
    app.include_router(materials_router, prefix="/api/master/materials")
    app.include_router(master_data_versions_router, prefix="/api/master-data")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as session:
            yield session

    def override_current_user() -> User:
        with factory() as session:
            return session.get(User, admin_id)

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user
    app.state.session_factory = factory
    app.state.admin_id = admin_id
    monkeypatch.setattr(
        material_price_adjust,
        "backup_database",
        lambda: tmp_path / "price-adjust-backup.sqlite3",
    )
    return app


def _customer_payload(
    number: int,
    code: str,
    name: str,
    **overrides,
) -> dict:
    payload = {
        "customer_number": number,
        "customer_code": code,
        "name": name,
    }
    payload.update(overrides)
    return payload


def _product_payload(customer_id: int, suffix: str, **overrides) -> dict:
    payload = {
        "customer_id": customer_id,
        "product_code": f"P4-P-{suffix}",
        "customer_material_code": f"P4-C-{suffix}",
        "product_name": f"P4 常用箱 {suffix}",
        "box_category": "normal",
    }
    payload.update(overrides)
    return payload


def _material_payload(suffix: str, **overrides) -> dict:
    payload = {
        "code": f"M{suffix}"[:3],
        "layer_count": 3,
        "supplier_name": f"P4供应商{suffix}",
        "quote_price": "1.00",
        "is_active": True,
    }
    payload.update(overrides)
    return payload


def _create_customer(client: TestClient, suffix: str, number: int = 1) -> dict:
    response = client.post(
        "/api/master/customers",
        json=_customer_payload(number, f"C{suffix}", f"P4客户{suffix}"),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _create_product(
    client: TestClient,
    customer_id: int,
    suffix: str,
    **overrides,
) -> dict:
    response = client.post(
        "/api/master/products",
        json=_product_payload(customer_id, suffix, **overrides),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _create_material(client: TestClient, suffix: str, **overrides) -> dict:
    response = client.post(
        "/api/master/materials",
        json=_material_payload(suffix, **overrides),
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_product_material_supplier_gate_blocks_new_links_but_keeps_unchanged_history(
    writer_app: FastAPI,
) -> None:
    from app.models.material import Material
    from app.models.product import Product
    from app.models.supplier import Supplier, SupplierAlias
    from app.services.supplier_master import normalize_supplier_identity

    with TestClient(writer_app) as client:
        customer = _create_customer(client, "SUPGATE", 199)
        with writer_app.state.session_factory() as session:
            disabled_supplier = Supplier(
                standard_name="苏州佳丰",
                normalized_name=normalize_supplier_identity("苏州佳丰"),
                display_name="佳丰",
                sort_order=900,
                is_active=False,
                version=1,
            )
            disabled_material = Material(
                code="P4-JF",
                layer_count=3,
                supplier_name="苏州佳丰",
                is_active=True,
            )
            unknown_material = Material(
                code="P4-UNKNOWN",
                layer_count=3,
                supplier_name="未建档纸板厂",
                is_active=True,
            )
            session.add_all(
                [disabled_supplier, disabled_material, unknown_material]
            )
            session.flush()
            session.add(
                SupplierAlias(
                    supplier_id=disabled_supplier.id,
                    alias_name="佳丰",
                    normalized_alias=normalize_supplier_identity("佳丰"),
                )
            )
            historical_product = Product(
                customer_id=customer["id"],
                product_code="P4-HISTORY-JF",
                customer_material_code="P4-HISTORY-JF",
                product_name="历史佳丰常用箱",
                material_id=disabled_material.id,
                box_category="normal",
                layer_count=3,
                splice_mode="single",
                pieces_per_box=1,
                version=1,
            )
            session.add(historical_product)
            session.commit()
            disabled_material_id = disabled_material.id
            unknown_material_id = unknown_material.id
            historical_product_id = historical_product.id

        disabled = client.post(
            "/api/master/products",
            json=_product_payload(
                customer["id"],
                "JF-NEW",
                material_id=disabled_material_id,
            ),
        )
        unknown = client.post(
            "/api/master/products",
            json=_product_payload(
                customer["id"],
                "UNKNOWN-NEW",
                material_id=unknown_material_id,
            ),
        )
        unchanged_history = client.put(
            f"/api/master/products/{historical_product_id}",
            json={
                **_product_payload(
                    customer["id"],
                    "HISTORY-JF",
                    product_code="P4-HISTORY-JF",
                    customer_material_code="P4-HISTORY-JF",
                    product_name="历史佳丰常用箱（仅改备注）",
                    material_id=disabled_material_id,
                ),
                "expected_version": 1,
                "change_reason": "只补历史备注，不改变供应商",
            },
        )

    assert disabled.status_code == 400
    assert "已停用" in disabled.text
    assert unknown.status_code == 400
    assert "尚未建档" in unknown.text
    assert unchanged_history.status_code == 200, unchanged_history.text


def test_customer_product_material_create_v1_and_update_v2(
    writer_app: FastAPI,
) -> None:
    from app.models.master_data_object_version import MasterDataObjectVersion

    with TestClient(writer_app) as client:
        customer = _create_customer(client, "A", 101)
        product = _create_product(client, customer["id"], "A")
        material = _create_material(client, "A1")

        customer_update = _customer_payload(
            101,
            "CA",
            "P4客户A",
            remark="客户更新到v2",
            expected_version=1,
            change_reason="补充客户备注",
        )
        updated_customer = client.put(
            f"/api/master/customers/{customer['id']}",
            json=customer_update,
        )
        product_update = _product_payload(
            customer["id"],
            "A",
            remark="常用箱更新到v2",
            expected_version=1,
            change_reason="补充常用箱备注",
        )
        updated_product = client.put(
            f"/api/master/products/{product['id']}",
            json=product_update,
        )
        material_update = _material_payload(
            "A1",
            remarks="材质更新到v2",
            expected_version=1,
            change_reason="补充材质备注",
        )
        updated_material = client.put(
            f"/api/master/materials/{material['id']}",
            json=material_update,
        )

    assert (customer["version"], product["version"], material["version"]) == (
        1,
        1,
        1,
    )
    assert updated_customer.status_code == 200, updated_customer.text
    assert updated_product.status_code == 200, updated_product.text
    assert updated_material.status_code == 200, updated_material.text
    assert updated_customer.json()["version"] == 2
    assert updated_product.json()["version"] == 2
    assert updated_material.json()["version"] == 2

    with writer_app.state.session_factory() as session:
        rows = session.execute(
            select(
                MasterDataObjectVersion.object_type,
                MasterDataObjectVersion.object_id,
                MasterDataObjectVersion.version,
            ).where(MasterDataObjectVersion.version.in_([1, 2]))
        ).all()
    assert len(rows) == 6


def test_empty_reason_noop_and_stale_version_are_rejected_or_stable(
    writer_app: FastAPI,
) -> None:
    from app.api.customers import CustomerUpdatePayload
    from app.api.materials import MaterialUpdatePayload
    from app.api.products import ProductUpdatePayload
    from app.models.master_data_object_version import MasterDataObjectVersion

    customer_payload = CustomerUpdatePayload(
        **_customer_payload(1, "EMPTY", "空原因"),
        expected_version=1,
        change_reason="   ",
    )
    assert customer_payload.change_reason is None
    customer_payload_without_reason = CustomerUpdatePayload(
        **_customer_payload(1, "EMPTY", "空原因"),
        expected_version=1,
    )
    assert customer_payload_without_reason.change_reason is None
    product_payload = ProductUpdatePayload(
        **_product_payload(1, "EMPTY"),
        expected_version=1,
        change_reason="   ",
    )
    assert product_payload.change_reason is None
    product_payload_without_reason = ProductUpdatePayload(
        **_product_payload(1, "EMPTY"),
        expected_version=1,
    )
    assert product_payload_without_reason.change_reason is None
    material_payload = MaterialUpdatePayload(
        **_material_payload("E1"),
        expected_version=1,
        change_reason="   ",
    )
    assert material_payload.change_reason is None
    material_payload_without_reason = MaterialUpdatePayload(
        **_material_payload("E1"),
        expected_version=1,
    )
    assert material_payload_without_reason.change_reason is None

    with TestClient(writer_app) as client:
        customer = _create_customer(client, "N", 102)
        no_change = client.put(
            f"/api/master/customers/{customer['id']}",
            json={
                **_customer_payload(102, "CN", "P4客户N"),
                "expected_version": 1,
            },
        )
        changed = client.put(
            f"/api/master/customers/{customer['id']}",
            json={
                **_customer_payload(102, "CN", "P4客户N", remark="v2"),
                "expected_version": 1,
            },
        )
        stale = client.put(
            f"/api/master/customers/{customer['id']}",
            json={
                **_customer_payload(102, "CN", "P4客户N", remark="过期写入"),
                "expected_version": 1,
            },
        )

    assert no_change.status_code == 200
    assert no_change.json()["version"] == 1
    assert changed.status_code == 200
    assert changed.json()["version"] == 2
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "MASTER_VERSION_CONFLICT"
    with writer_app.state.session_factory() as session:
        count = session.scalar(
            select(func.count())
            .select_from(MasterDataObjectVersion)
            .where(
                MasterDataObjectVersion.object_type == "customer",
                MasterDataObjectVersion.object_id == customer["id"],
            )
        )
    assert count == 2


def test_product_update_preview_supports_one_confirmation_without_reason(
    writer_app: FastAPI,
) -> None:
    from app.models.audit import OperationLog
    from app.models.master_data_object_version import MasterDataObjectVersion
    from app.models.product import Product

    with TestClient(writer_app) as client:
        customer = _create_customer(client, "P1-20", 112)
        product = _create_product(client, customer["id"], "P1-20")
        endpoint = f"/api/master/products/{product['id']}"
        payload = {
            **_product_payload(
                customer["id"],
                "P1-20",
                remark="无需填写原因的一次确认",
            ),
            "expected_version": 1,
        }

        with writer_app.state.session_factory() as session:
            versions_before = session.scalar(
                select(func.count(MasterDataObjectVersion.id)).where(
                    MasterDataObjectVersion.object_type == "product",
                    MasterDataObjectVersion.object_id == product["id"],
                )
            )
            logs_before = session.scalar(
                select(func.count(OperationLog.id)).where(
                    OperationLog.entity_type == "product",
                    OperationLog.entity_id == product["id"],
                )
            )
        preview = client.post(f"{endpoint}/update-preview", json=payload)
        unchanged_after_preview = client.get(endpoint)
        with writer_app.state.session_factory() as session:
            assert session.scalar(
                select(func.count(MasterDataObjectVersion.id)).where(
                    MasterDataObjectVersion.object_type == "product",
                    MasterDataObjectVersion.object_id == product["id"],
                )
            ) == versions_before
            assert session.scalar(
                select(func.count(OperationLog.id)).where(
                    OperationLog.entity_type == "product",
                    OperationLog.entity_id == product["id"],
                )
            ) == logs_before
        saved = client.put(endpoint, json=payload)

    assert preview.status_code == 200, preview.text
    assert preview.json()["can_update"] is True
    assert preview.json()["current_version"] == 1
    assert preview.json()["changed_fields"] == ["remark"]
    assert preview.json()["warnings"] == []
    assert preview.json()["confirmation_token"] is None
    assert unchanged_after_preview.status_code == 200
    assert unchanged_after_preview.json()["version"] == 1
    assert unchanged_after_preview.json()["remark"] is None

    assert saved.status_code == 200, saved.text
    assert saved.json()["version"] == 2
    assert saved.json()["remark"] == "无需填写原因的一次确认"
    with writer_app.state.session_factory() as session:
        assert session.scalar(
            select(func.count(MasterDataObjectVersion.id)).where(
                MasterDataObjectVersion.object_type == "product",
                MasterDataObjectVersion.object_id == product["id"],
            )
        ) == versions_before + 1
        assert session.scalar(
            select(func.count(OperationLog.id)).where(
                OperationLog.entity_type == "product",
                OperationLog.entity_id == product["id"],
            )
        ) == logs_before + 1
        latest = session.scalar(
            select(OperationLog)
            .where(
                OperationLog.entity_type == "product",
                OperationLog.entity_id == product["id"],
            )
            .order_by(OperationLog.id.desc())
        )
        revision = session.scalar(
            select(MasterDataObjectVersion).where(
                MasterDataObjectVersion.object_type == "product",
                MasterDataObjectVersion.object_id == product["id"],
                MasterDataObjectVersion.version == 2,
            )
        )
        stored = session.get(Product, product["id"])

    assert latest is not None
    assert latest.action == "MASTER_UPDATE"
    assert latest.username == "p4-writer-admin"
    assert json.loads(latest.details or "{}")["reason"] is None
    assert revision is not None
    assert revision.actor_username_snapshot == "p4-writer-admin"
    assert revision.reason is None
    assert stored is not None
    assert stored.manual_modified is True
    assert stored.manual_modified_at is not None


def test_product_update_preview_returns_bound_warning_token_before_write(
    writer_app: FastAPI,
) -> None:
    from app.models.audit import OperationLog
    from app.models.master_data_object_version import MasterDataObjectVersion

    with TestClient(writer_app) as client:
        customer = _create_customer(client, "P1-20-W", 113)
        product = _create_product(client, customer["id"], "P1-20-W")
        endpoint = f"/api/master/products/{product['id']}"
        payload = {
            **_product_payload(customer["id"], "P1-20-W"),
            "product_code": "P1-20-W-NEW",
            "expected_version": 1,
        }

        preview = client.post(f"{endpoint}/update-preview", json=payload)
        token = preview.json()["confirmation_token"]
        missing_token = client.put(endpoint, json=payload)
        unchanged = client.get(endpoint)
        confirmed = client.put(
            endpoint,
            json={**payload, "confirmation_token": token},
        )
        stale = client.put(
            endpoint,
            json={
                **payload,
                "product_name": "不能用旧版本覆盖",
                "confirmation_token": token,
            },
        )

    assert preview.status_code == 200, preview.text
    assert preview.json()["can_update"] is True
    assert preview.json()["changed_fields"] == ["product_code"]
    assert any(
        warning["code"] == "PRODUCT_CODE_CHANGE"
        for warning in preview.json()["warnings"]
    )
    assert token
    assert missing_token.status_code == 409
    assert missing_token.json()["detail"]["code"] == "MASTER_CHANGE_CONFIRMATION_REQUIRED"
    assert unchanged.json()["version"] == 1
    assert unchanged.json()["product_code"] == product["product_code"]
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["version"] == 2
    assert confirmed.json()["product_code"] == "P1-20-W-NEW"
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "MASTER_VERSION_CONFLICT"
    with writer_app.state.session_factory() as session:
        assert session.scalar(
            select(func.count(MasterDataObjectVersion.id)).where(
                MasterDataObjectVersion.object_type == "product",
                MasterDataObjectVersion.object_id == product["id"],
            )
        ) == 2
        assert session.scalar(
            select(func.count(OperationLog.id)).where(
                OperationLog.entity_type == "product",
                OperationLog.entity_id == product["id"],
                OperationLog.action == "MASTER_UPDATE",
            )
        ) == 1


def test_product_noop_without_reason_keeps_version_audit_and_manual_flag_unchanged(
    writer_app: FastAPI,
) -> None:
    from app.models.audit import OperationLog
    from app.models.master_data_object_version import MasterDataObjectVersion
    from app.models.product import Product

    with TestClient(writer_app) as client:
        customer = _create_customer(client, "P1-20-N", 114)
        numeric_values = {
            "length_mm": 520,
            "width_mm": 350,
            "height_mm": 300,
            "pieces_per_box": 1,
            "flap_mm": 30,
            "sale_unit_price": "3.6800",
        }
        product = _create_product(
            client,
            customer["id"],
            "P1-20-N",
            **numeric_values,
        )
        endpoint = f"/api/master/products/{product['id']}"
        payload = {
            **_product_payload(customer["id"], "P1-20-N", **numeric_values),
            "expected_version": 1,
        }
        with writer_app.state.session_factory() as session:
            versions_before = session.scalar(
                select(func.count(MasterDataObjectVersion.id)).where(
                    MasterDataObjectVersion.object_type == "product",
                    MasterDataObjectVersion.object_id == product["id"],
                )
            )
            logs_before = session.scalar(
                select(func.count(OperationLog.id)).where(
                    OperationLog.entity_type == "product",
                    OperationLog.entity_id == product["id"],
                )
            )
            product_before = session.get(Product, product["id"])
            manual_modified_before = product_before.manual_modified
            manual_modified_at_before = product_before.manual_modified_at

        preview = client.post(f"{endpoint}/update-preview", json=payload)
        saved = client.put(endpoint, json=payload)

    assert preview.status_code == 200, preview.text
    assert preview.json()["can_update"] is False
    assert preview.json()["changed_fields"] == []
    assert preview.json()["confirmation_token"] is None
    assert saved.status_code == 200, saved.text
    assert saved.json()["version"] == 1
    with writer_app.state.session_factory() as session:
        assert session.scalar(
            select(func.count(MasterDataObjectVersion.id)).where(
                MasterDataObjectVersion.object_type == "product",
                MasterDataObjectVersion.object_id == product["id"],
            )
        ) == versions_before
        assert session.scalar(
            select(func.count(OperationLog.id)).where(
                OperationLog.entity_type == "product",
                OperationLog.entity_id == product["id"],
            )
        ) == logs_before
        stored = session.get(Product, product["id"])

    assert stored is not None
    assert stored.version == 1
    assert stored.manual_modified is manual_modified_before
    assert stored.manual_modified_at == manual_modified_at_before


def test_customer_identity_change_requires_bound_confirmation_token(
    writer_app: FastAPI,
) -> None:
    from app.models.audit import OperationLog
    from app.models.master_data_object_version import MasterDataObjectVersion

    with TestClient(writer_app) as client:
        customer = _create_customer(client, "CONF", 103)
        payload = {
            **_customer_payload(103, "CCONF", "P4客户确认后更名"),
            "expected_version": 1,
        }
        preview = client.post(
            f"/api/master/customers/{customer['id']}/update-preview",
            json=payload,
        )
        first = client.put(
            f"/api/master/customers/{customer['id']}",
            json=payload,
        )
        unchanged = client.get(f"/api/master/customers/{customer['id']}")
        confirmed = client.put(
            f"/api/master/customers/{customer['id']}",
            json={**payload, "confirmation_token": preview.json()["confirmation_token"]},
        )

    assert preview.status_code == 200, preview.text
    assert preview.json()["can_update"] is True
    assert preview.json()["confirmation_token"]
    assert preview.json()["warnings"]
    assert first.status_code == 409
    assert first.json()["detail"]["code"] == "MASTER_CHANGE_CONFIRMATION_REQUIRED"
    assert unchanged.json()["version"] == 1
    assert unchanged.json()["name"] == "P4客户CONF"
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["version"] == 2
    assert confirmed.json()["name"] == "P4客户确认后更名"
    with writer_app.state.session_factory() as session:
        revision = session.scalar(
            select(MasterDataObjectVersion)
            .where(
                MasterDataObjectVersion.object_type == "customer",
                MasterDataObjectVersion.object_id == customer["id"],
                MasterDataObjectVersion.version == 2,
            )
        )
        audit = session.scalar(
            select(OperationLog)
            .where(
                OperationLog.entity_type == "customer",
                OperationLog.entity_id == customer["id"],
                OperationLog.action == "MASTER_UPDATE",
            )
        )
    assert revision is not None
    assert revision.reason is None
    assert revision.actor_username_snapshot == "p4-writer-admin"
    assert revision.created_at is not None
    assert '"name"' in revision.changed_fields_json
    assert audit is not None
    assert audit.username == "p4-writer-admin"
    assert audit.created_at is not None


def test_customer_status_and_soft_delete_accept_no_reason_but_keep_version_gate(
    writer_app: FastAPI,
) -> None:
    with TestClient(writer_app) as client:
        status_customer = _create_customer(client, "NOREASON-STATUS", 113)
        disabled = client.put(
            f"/api/master/customers/{status_customer['id']}/status",
            json={"is_active": False, "expected_version": 1},
        )
        stale = client.put(
            f"/api/master/customers/{status_customer['id']}/status",
            json={"is_active": True, "expected_version": 1},
        )

        delete_customer = _create_customer(client, "NOREASON-DELETE", 114)
        deleted = client.request(
            "DELETE",
            f"/api/master/customers/{delete_customer['id']}",
            json={"expected_version": 1},
        )

    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["version"] == 2
    assert disabled.json()["is_active"] is False
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "MASTER_VERSION_CONFLICT"
    assert deleted.status_code == 204, deleted.text


def test_material_update_and_soft_delete_accept_no_reason_with_preview_and_audit(
    writer_app: FastAPI,
) -> None:
    from app.models.audit import OperationLog
    from app.models.master_data_object_version import MasterDataObjectVersion

    with TestClient(writer_app) as client:
        material = _create_material(client, "LIF")
        update_payload = {
            **_material_payload("LIF", code="ML2", remarks="无需手填原因"),
            "expected_version": 1,
        }
        preview = client.post(
            f"/api/master/materials/{material['id']}/update-preview",
            json=update_payload,
        )
        rejected_without_token = client.put(
            f"/api/master/materials/{material['id']}",
            json=update_payload,
        )
        updated = client.put(
            f"/api/master/materials/{material['id']}",
            json={
                **update_payload,
                "confirmation_token": preview.json()["confirmation_token"],
            },
        )
        stale = client.put(
            f"/api/master/materials/{material['id']}",
            json={**update_payload, "remarks": "旧页面覆盖"},
        )

        removable = _create_material(client, "A1")
        deleted = client.request(
            "DELETE",
            f"/api/master/materials/{removable['id']}",
            json={"expected_version": 1},
        )

    assert preview.status_code == 200, preview.text
    assert preview.json()["can_update"] is True
    assert preview.json()["confirmation_token"]
    assert preview.json()["warnings"]
    assert rejected_without_token.status_code == 409
    assert rejected_without_token.json()["detail"]["code"] == "MASTER_CHANGE_CONFIRMATION_REQUIRED"
    assert updated.status_code == 200, updated.text
    assert updated.json()["version"] == 2
    assert updated.json()["code"] == "ML2"
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "MASTER_VERSION_CONFLICT"
    assert deleted.status_code == 204, deleted.text

    with writer_app.state.session_factory() as session:
        revision = session.scalar(
            select(MasterDataObjectVersion)
            .where(
                MasterDataObjectVersion.object_type == "material",
                MasterDataObjectVersion.object_id == material["id"],
                MasterDataObjectVersion.version == 2,
            )
        )
        audit = session.scalar(
            select(OperationLog)
            .where(
                OperationLog.entity_type == "material",
                OperationLog.entity_id == material["id"],
                OperationLog.action == "MASTER_UPDATE",
            )
        )
    assert revision is not None
    assert revision.reason is None
    assert revision.actor_username_snapshot == "p4-writer-admin"
    assert revision.created_at is not None
    assert audit is not None
    assert audit.username == "p4-writer-admin"


def test_status_sync_soft_delete_restore_and_physical_delete_protection(
    writer_app: FastAPI,
) -> None:
    from app.models.product import Product

    with TestClient(writer_app) as client:
        customer = _create_customer(client, "LIFE", 104)
        status_product = _create_product(client, customer["id"], "STATUS")
        status_response = client.put(
            f"/api/master/products/{status_product['id']}/status",
            json={
                "is_active": False,
                "expected_version": 1,
                "change_reason": "暂时停用常用箱",
            },
        )

        sync_product = _create_product(client, customer["id"], "SYNC")
        synced = client.post(
            f"/api/master/products/{sync_product['id']}/sync-fields",
            json={
                "fields": {"remark": "订单确认后同步"},
                "expected_version": 1,
                "change_reason": "订单字段同步回常用箱",
            },
        )

        lifecycle_product = _create_product(client, customer["id"], "TRASH")
        deleted = client.request(
            "DELETE",
            f"/api/master/products/{lifecycle_product['id']}",
            json={"expected_version": 1, "change_reason": "误建后移入垃圾站"},
        )
        generic_restore_preview = client.post(
            f"/api/master-data/product/{lifecycle_product['id']}/versions/1/restore-preview",
            json={"expected_version": 2},
        )
        generic_restore = client.post(
            f"/api/master-data/product/{lifecycle_product['id']}/versions/1/restore",
            json={
                "expected_version": 2,
                "reason": "不能绕过垃圾站生命周期恢复",
                "confirmation_token": "not-a-valid-history-token",
            },
        )
        restore_first = client.put(
            f"/api/master/products/{lifecycle_product['id']}/restore",
            json={"expected_version": 2, "change_reason": "确认恢复常用箱"},
        )
        restore_token = restore_first.json()["detail"]["confirmation_token"]
        restored = client.put(
            f"/api/master/products/{lifecycle_product['id']}/restore",
            json={
                "expected_version": 2,
                "change_reason": "确认恢复常用箱",
                "confirmation_token": restore_token,
            },
        )

        protected_product = _create_product(client, customer["id"], "PROTECT")
        protected_delete = client.request(
            "DELETE",
            f"/api/master/products/{protected_product['id']}",
            json={"expected_version": 1, "change_reason": "移入垃圾站"},
        )
        purge = client.request(
            "DELETE",
            f"/api/master/products/{protected_product['id']}/purge",
            json={"expected_version": 2, "change_reason": "申请永久删除"},
        )

    assert status_response.status_code == 200
    assert status_response.json()["version"] == 2
    assert status_response.json()["is_active"] is False
    assert synced.status_code == 200, synced.text
    assert synced.json() == {
        "updated": ["remark"],
        "product_id": sync_product["id"],
        "version": 2,
    }
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["version"] == 2
    assert deleted.json()["deleted_at"] is not None
    assert generic_restore_preview.status_code == 409
    assert "专用垃圾站恢复接口" in generic_restore_preview.json()["detail"]
    assert generic_restore.status_code == 409
    assert "专用垃圾站恢复接口" in generic_restore.json()["detail"]
    assert restore_first.status_code == 409
    assert restored.status_code == 200, restored.text
    assert restored.json()["version"] == 3
    assert restored.json()["deleted_at"] is None
    assert protected_delete.status_code == 200
    assert purge.status_code == 409
    assert "版本历史" in str(purge.json()["detail"])
    with writer_app.state.session_factory() as session:
        protected = session.get(Product, protected_product["id"])
        assert protected is not None
        protected.deleted_at = datetime.now() - timedelta(days=31)
        session.commit()

    with TestClient(writer_app) as client:
        trash = client.get("/api/master/products/trash")

    assert trash.status_code == 200
    assert any(
        item["id"] == protected_product["id"] for item in trash.json()["items"]
    )
    with writer_app.state.session_factory() as session:
        assert session.get(Product, protected_product["id"]) is not None


def test_product_sync_normalizes_equal_values_without_version_or_audit(
    writer_app: FastAPI,
) -> None:
    from app.models.master_data_object_version import MasterDataObjectVersion
    from app.models.audit import OperationLog

    with TestClient(writer_app) as client:
        customer = _create_customer(client, "SYNC-NOOP", 111)
        created = client.post(
            "/api/master/products",
            json=_product_payload(
                customer["id"],
                "SYNC-NOOP",
                sale_unit_price="1",
            ),
        )
        assert created.status_code == 201, created.text
        product = created.json()

        with writer_app.state.session_factory() as session:
            before_versions = session.scalar(
                select(func.count(MasterDataObjectVersion.id)).where(
                    MasterDataObjectVersion.object_type == "product",
                    MasterDataObjectVersion.object_id == product["id"],
                )
            )
            before_logs = session.scalar(select(func.count(OperationLog.id)))

        no_change = client.post(
            f"/api/master/products/{product['id']}/sync-fields",
            json={
                "fields": {
                    "sale_unit_price": "1.00",
                    "product_name": f"  {product['product_name']}  ",
                },
                "expected_version": 1,
                "change_reason": "订单保存同步常用箱",
            },
        )

        assert no_change.status_code == 200, no_change.text
        assert no_change.json() == {
            "updated": [],
            "product_id": product["id"],
            "version": 1,
        }
        with writer_app.state.session_factory() as session:
            assert session.scalar(
                select(func.count(MasterDataObjectVersion.id)).where(
                    MasterDataObjectVersion.object_type == "product",
                    MasterDataObjectVersion.object_id == product["id"],
                )
            ) == before_versions
            assert session.scalar(select(func.count(OperationLog.id))) == before_logs

        changed = client.post(
            f"/api/master/products/{product['id']}/sync-fields",
            json={
                "fields": {"sale_unit_price": "1.50"},
                "expected_version": 1,
                "change_reason": "显式调整常用箱单价",
            },
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["updated"] == ["sale_unit_price"]
        assert changed.json()["version"] == 2

        normalized_again = client.post(
            f"/api/master/products/{product['id']}/sync-fields",
            json={
                "fields": {"sale_unit_price": "1.5000"},
                "expected_version": 2,
                "change_reason": "等值格式复核",
            },
        )
        assert normalized_again.status_code == 200, normalized_again.text
        assert normalized_again.json()["updated"] == []
        assert normalized_again.json()["version"] == 2


def test_price_adjust_preview_versions_and_stale_apply_rejected(
    writer_app: FastAPI,
) -> None:
    from app.models.material import Material
    from app.models.material_price_history import (
        MaterialPriceAdjustmentBatch,
        MaterialPriceHistory,
    )

    with TestClient(writer_app) as client:
        first = _create_material(
            client,
            "S1",
            supplier_name="P4批量供应商",
            quote_price="1.00",
        )
        second = _create_material(
            client,
            "S2",
            supplier_name="P4批量供应商",
            quote_price="2.00",
        )
        preview = client.post(
            "/api/master/materials/price-adjustments/preview",
            json={"supplier_name": "P4批量供应商", "adjust_percent": "5"},
        )
        changed = client.put(
            f"/api/master/materials/{first['id']}",
            json={
                **_material_payload(
                    "S1",
                    supplier_name="P4批量供应商",
                    quote_price="1.00",
                    remarks="预览后另行修改",
                ),
                "expected_version": 1,
                "change_reason": "预览后补充备注",
            },
        )
        stale_apply = client.post(
            "/api/master/materials/price-adjustments/apply",
            json={
                "supplier_name": "P4批量供应商",
                "adjust_percent": "5",
                "expected_versions": preview.json()["expected_versions"],
            },
        )

    assert preview.status_code == 200, preview.text
    assert preview.json()["expected_versions"] == {
        str(first["id"]): 1,
        str(second["id"]): 1,
    }
    assert {row["version"] for row in preview.json()["details"]} == {1}
    assert changed.status_code == 200
    assert changed.json()["version"] == 2
    assert stale_apply.status_code == 409
    assert "重新预览" in stale_apply.json()["detail"]
    with writer_app.state.session_factory() as session:
        assert session.get(Material, first["id"]).quote_price == Decimal("1.00")
        assert session.get(Material, second["id"]).quote_price == Decimal("2.00")
        assert session.scalar(select(func.count()).select_from(MaterialPriceHistory)) == 2
        assert session.scalar(
            select(func.count()).select_from(MaterialPriceAdjustmentBatch)
        ) == 0


def test_price_adjust_success_versions_every_material_in_one_transaction(
    writer_app: FastAPI,
) -> None:
    from app.models.material import Material
    from app.models.master_data_object_version import MasterDataObjectVersion
    from app.models.material_price_history import (
        MaterialPriceAdjustmentBatch,
        MaterialPriceHistory,
    )

    with TestClient(writer_app) as client:
        first = _create_material(
            client,
            "B1",
            supplier_name="P4成功调价供应商",
            quote_price="1.00",
        )
        second = _create_material(
            client,
            "B2",
            supplier_name="P4成功调价供应商",
            quote_price="2.00",
        )
        preview = client.post(
            "/api/master/materials/price-adjustments/preview",
            json={"supplier_name": "P4成功调价供应商", "adjust_percent": "5"},
        )
        applied = client.post(
            "/api/master/materials/price-adjustments/apply",
            json={
                "supplier_name": "P4成功调价供应商",
                "adjust_percent": "5",
                "expected_versions": preview.json()["expected_versions"],
            },
        )

    assert applied.status_code == 200, applied.text
    assert applied.json()["affected_count"] == 2
    with writer_app.state.session_factory() as session:
        rows = {
            material.id: material
            for material in session.scalars(
                select(Material).where(Material.id.in_([first["id"], second["id"]]))
            )
        }
        assert rows[first["id"]].quote_price == Decimal("1.05")
        assert rows[second["id"]].quote_price == Decimal("2.10")
        assert {row.version for row in rows.values()} == {2}
        history = session.scalars(
            select(MaterialPriceHistory).where(MaterialPriceHistory.batch_id.is_not(None))
        ).all()
        assert len(history) == 2
        assert {row.adjust_reason for row in history} == {None}
        assert {row.operator for row in history} == {"p4-writer-admin"}
        batch = session.scalar(select(MaterialPriceAdjustmentBatch))
        assert batch is not None
        assert batch.remark is None
        assert batch.operator == "p4-writer-admin"
        revisions = session.scalars(
            select(MasterDataObjectVersion).where(
                MasterDataObjectVersion.object_type == "material",
                MasterDataObjectVersion.object_id.in_([first["id"], second["id"]]),
                MasterDataObjectVersion.version == 2,
            )
        ).all()
        assert len(revisions) == 2
        assert {row.reason for row in revisions} == {None}
        assert {row.actor_username_snapshot for row in revisions} == {"p4-writer-admin"}


def test_price_adjust_failure_rolls_back_all_material_versions_and_history(
    writer_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.models.material import Material
    from app.models.master_data_object_version import MasterDataObjectVersion
    from app.models.material_price_history import (
        MaterialPriceAdjustmentBatch,
        MaterialPriceHistory,
    )
    from app.services import material_price_adjust
    from app.services.master_data_versioning import apply_versioned_update as real_apply

    with TestClient(writer_app) as client:
        first = _create_material(
            client,
            "T1",
            supplier_name="P4事务供应商",
            quote_price="1.00",
        )
        second = _create_material(
            client,
            "T2",
            supplier_name="P4事务供应商",
            quote_price="2.00",
        )
        preview = client.post(
            "/api/master/materials/price-adjustments/preview",
            json={"supplier_name": "P4事务供应商", "adjust_percent": "5"},
        )

        calls = 0

        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise HTTPException(status_code=409, detail="模拟第二条材质写入失败")
            return real_apply(*args, **kwargs)

        monkeypatch.setattr(
            material_price_adjust,
            "apply_versioned_update",
            fail_second,
        )
        failed = client.post(
            "/api/master/materials/price-adjustments/apply",
            json={
                "supplier_name": "P4事务供应商",
                "adjust_percent": "5",
                "expected_versions": preview.json()["expected_versions"],
            },
        )

    assert failed.status_code == 409
    with writer_app.state.session_factory() as session:
        rows = {
            material.id: material
            for material in session.scalars(
                select(Material).where(Material.id.in_([first["id"], second["id"]]))
            )
        }
        assert rows[first["id"]].quote_price == Decimal("1.00")
        assert rows[second["id"]].quote_price == Decimal("2.00")
        assert {row.version for row in rows.values()} == {1}
        assert session.scalar(select(func.count()).select_from(MaterialPriceHistory)) == 2
        assert session.scalar(
            select(func.count()).select_from(MaterialPriceAdjustmentBatch)
        ) == 0
        version_rows = session.scalar(
            select(func.count())
            .select_from(MasterDataObjectVersion)
            .where(
                MasterDataObjectVersion.object_type == "material",
                MasterDataObjectVersion.object_id.in_([first["id"], second["id"]]),
            )
        )
        assert version_rows == 2


def test_material_price_history_records_initial_and_single_edit_contract(
    writer_app: FastAPI,
) -> None:
    from app.models.material_price_history import MaterialPriceHistory

    def save_update(
        client: TestClient,
        material_id: int,
        *,
        expected_version: int,
        quote_price: str,
        quote_date: str,
        remarks: str | None = None,
    ):
        payload = {
            **_material_payload(
                "LIF",
                code="+",
                supplier_name="P4供应商LIF",
                quote_price=quote_price,
                quote_date=quote_date,
                price_unit="元/平方米",
                remarks=remarks,
            ),
            "expected_version": expected_version,
            "change_reason": "供应商报价复核",
        }
        preview = client.post(
            f"/api/master/materials/{material_id}/update-preview",
            json=payload,
        )
        assert preview.status_code == 200, preview.text
        token = preview.json().get("confirmation_token")
        if token:
            payload["confirmation_token"] = token
        return client.put(f"/api/master/materials/{material_id}", json=payload)

    with TestClient(writer_app) as client:
        material = _create_material(
            client,
            "LIF",
            code="+",
            supplier_name="P4供应商LIF",
            quote_price="5.00",
            quote_date="2026-08-01",
            price_unit="元/平方米",
        )
        first_read = client.get(
            f"/api/master/materials/{material['id']}/price-history",
            params={"supplier_name": "P4供应商LIF"},
        )
        wrong_supplier = client.get(
            f"/api/master/materials/{material['id']}/price-history",
            params={"supplier_name": "P4供应商A1"},
        )
        raised = save_update(
            client,
            material["id"],
            expected_version=1,
            quote_price="5.20",
            quote_date="2026-08-02",
        )
        lowered = save_update(
            client,
            material["id"],
            expected_version=2,
            quote_price="4.90",
            quote_date="2026-08-03",
        )
        non_price = save_update(
            client,
            material["id"],
            expected_version=3,
            quote_price="4.90",
            quote_date="2026-08-03",
            remarks="只改备注，不生成调价节点",
        )
        stale = client.put(
            f"/api/master/materials/{material['id']}",
            json={
                **_material_payload(
                    "LIF",
                    code="+",
                    supplier_name="P4供应商LIF",
                    quote_price="6.00",
                    quote_date="2026-08-04",
                    price_unit="元/平方米",
                ),
                "expected_version": 3,
                "change_reason": "过期页面不得追加历史",
            },
        )
        history = client.get(
            f"/api/master/materials/{material['id']}/price-history"
        )
        page = client.get(
            f"/api/master/materials/{material['id']}/price-history?offset=0&limit=1"
        )
        newest = client.get(
            f"/api/master/materials/{material['id']}/price-history",
            params={"offset": 0, "limit": 1, "order": "desc"},
        )
        filtered = client.get(
            f"/api/master/materials/{material['id']}/price-history"
            "?date_from=2026-08-02&date_to=2026-08-03"
        )

    assert first_read.status_code == 200, first_read.text
    assert first_read.json()["material_code"] == "+"
    assert first_read.json()["history_incomplete"] is False
    assert first_read.json()["items"][0]["old_price"] is None
    assert first_read.json()["items"][0]["new_price"] == 5.0
    assert wrong_supplier.status_code == 404
    assert raised.status_code == 200, raised.text
    assert lowered.status_code == 200, lowered.text
    assert non_price.status_code == 200, non_price.text
    assert stale.status_code == 409, stale.text
    assert stale.json()["detail"]["code"] == "MASTER_VERSION_CONFLICT"
    assert history.status_code == 200, history.text
    assert history.json()["total"] == 3
    assert history.json()["current_price"] == 4.9
    assert history.json()["previous_effective_price"] == 5.2
    assert history.json()["current_change_amount"] == pytest.approx(-0.3)
    assert history.json()["current_change_percent"] == pytest.approx(-5.7692)
    assert history.json()["chart_compatible"] is True
    assert [item["new_price"] for item in history.json()["items"]] == [
        5.0,
        5.2,
        4.9,
    ]
    assert history.json()["items"][1]["change_amount"] == pytest.approx(0.2)
    assert history.json()["items"][1]["change_percent"] == pytest.approx(4.0)
    assert history.json()["items"][2]["change_percent"] == pytest.approx(
        -5.7692
    )
    assert page.status_code == 200
    assert page.json()["total"] == 3
    assert page.json()["has_more"] is True
    assert len(page.json()["items"]) == 1
    assert newest.status_code == 200
    assert newest.json()["items"][0]["new_price"] == 4.9
    assert newest.json()["order"] == "desc"
    assert filtered.status_code == 200
    assert filtered.json()["total"] == 2
    with writer_app.state.session_factory() as session:
        rows = session.scalars(
            select(MaterialPriceHistory).where(
                MaterialPriceHistory.material_id == material["id"]
            )
        ).all()
        assert len(rows) == 3


def test_material_price_history_marks_legacy_gap_future_and_unknown_time(
    writer_app: FastAPI,
) -> None:
    from app.models.material import Material
    from app.models.material_price_history import MaterialPriceHistory

    with writer_app.state.session_factory() as session:
        legacy = Material(
            code="LEG",
            layer_count=3,
            supplier_name="P4供应商A1",
            quote_price=Decimal("2.00"),
            price_unit="元/平方米",
            is_active=True,
        )
        session.add(legacy)
        session.commit()
        legacy_id = legacy.id

    with TestClient(writer_app) as client:
        empty_history = client.get(
            f"/api/master/materials/{legacy_id}/price-history"
        )

    assert empty_history.status_code == 200, empty_history.text
    assert empty_history.json()["history_incomplete"] is True
    assert "不会补造" in empty_history.json()["history_notice"]
    assert empty_history.json()["items"] == []

    with writer_app.state.session_factory() as session:
        session.add(
            MaterialPriceHistory(
                material_id=legacy_id,
                supplier_name="P4供应商A1",
                material_code="LEG",
                old_price=Decimal("1.80"),
                new_price=Decimal("2.00"),
                effective_date=date(2099, 1, 1),
                operator="history-fixture",
            )
        )
        session.commit()

    with TestClient(writer_app) as client:
        future_history = client.get(
            f"/api/master/materials/{legacy_id}/price-history"
        )

    assert future_history.status_code == 200, future_history.text
    assert future_history.json()["history_incomplete"] is True
    assert future_history.json()["current_price"] == 1.8
    assert future_history.json()["latest_recorded_price"] == 2.0
    assert future_history.json()["next_pending_price"] == 2.0
    assert future_history.json()["next_pending_effective_date"] == "2099-01-01"

    with writer_app.state.session_factory() as session:
        session.add(
            MaterialPriceHistory(
                material_id=legacy_id,
                supplier_name="P4供应商A1",
                material_code="LEG",
                old_price=Decimal("2.00"),
                new_price=Decimal("2.10"),
                effective_date=None,
                operator="history-fixture",
            )
        )
        session.commit()

    with TestClient(writer_app) as client:
        history = client.get(f"/api/master/materials/{legacy_id}/price-history")
        pending_only = client.get(
            f"/api/master/materials/{legacy_id}/price-history",
            params={"effective_status": "pending"},
        )
        unknown_only = client.get(
            f"/api/master/materials/{legacy_id}/price-history",
            params={"effective_status": "time_unknown"},
        )

    assert history.status_code == 200, history.text
    assert [item["effective_status"] for item in history.json()["items"]] == [
        "pending",
        "time_unknown",
    ]
    assert pending_only.status_code == 200
    assert pending_only.json()["total"] == 1
    assert pending_only.json()["items"][0]["effective_status"] == "pending"
    assert unknown_only.status_code == 200
    assert unknown_only.json()["total"] == 1
    assert unknown_only.json()["items"][0]["effective_status"] == "time_unknown"
    with TestClient(writer_app) as client:
        invalid_range = client.get(
            f"/api/master/materials/{legacy_id}/price-history"
            "?date_from=2026-08-03&date_to=2026-08-02"
        )
    assert invalid_range.status_code == 400


def test_material_price_history_records_date_and_unit_only_changes(
    writer_app: FastAPI,
) -> None:
    with TestClient(writer_app) as client:
        material = _create_material(
            client,
            "A1",
            quote_price="3.00",
            quote_date="2026-08-01",
            price_unit="元/平方米",
        )
        date_payload = {
            **_material_payload(
                "A1",
                quote_price="3.00",
                quote_date="2026-08-02",
                price_unit="元/平方米",
            ),
            "expected_version": 1,
        }
        date_saved = client.put(
            f"/api/master/materials/{material['id']}",
            json=date_payload,
        )
        unit_payload = {
            **_material_payload(
                "A1",
                quote_price="3.00",
                quote_date="2026-08-02",
                price_unit="元/张",
            ),
            "expected_version": 2,
        }
        unit_saved = client.put(
            f"/api/master/materials/{material['id']}",
            json=unit_payload,
        )
        history = client.get(
            f"/api/master/materials/{material['id']}/price-history"
        )

    assert date_saved.status_code == 200, date_saved.text
    assert unit_saved.status_code == 200, unit_saved.text
    assert history.status_code == 200, history.text
    assert history.json()["total"] == 3
    assert history.json()["items"][1]["change_percent"] == 0.0
    assert "报价日期" in history.json()["items"][1]["adjust_reason"]
    assert "计价单位" in history.json()["items"][2]["adjust_reason"]
    assert history.json()["current_price_unit"] == "元/张"
    assert history.json()["chart_compatible"] is False
    assert "不同单位" in history.json()["chart_notice"]


def test_material_price_history_requires_cost_permission(writer_app: FastAPI) -> None:
    from app.api.deps import get_current_user
    from app.models.user import User

    with TestClient(writer_app) as client:
        material = _create_material(
            client,
            "A1",
            quote_price="1.00",
        )

    def override_sales_user() -> User:
        user = User(
            id=987654,
            username="price-history-sales",
            password_hash="not-used",
            role="sales",
            is_active=True,
            must_change_password=False,
            customer_access_mode="all",
        )
        user.permission_overrides = []
        return user

    writer_app.dependency_overrides[get_current_user] = override_sales_user
    with TestClient(writer_app) as client:
        denied = client.get(
            f"/api/master/materials/{material['id']}/price-history"
        )

    assert denied.status_code == 403


def test_material_soft_deactivation_and_reactivation_leave_version_and_audit_trails(
    writer_app: FastAPI,
) -> None:
    from app.models.audit import OperationLog
    from app.models.master_data_object_version import MasterDataObjectVersion
    from app.models.material import Material

    with TestClient(writer_app) as client:
        material = _create_material(client, "LIF")
        deactivated = client.request(
            "DELETE",
            f"/api/master/materials/{material['id']}",
            json={"expected_version": 1, "change_reason": "停用失效材质"},
        )
        restored = client.put(
            f"/api/master/materials/{material['id']}",
            json={
                **_material_payload("LIF", is_active=True),
                "expected_version": 2,
                "change_reason": "恢复误停用材质",
            },
        )

    assert deactivated.status_code == 204, deactivated.text
    assert restored.status_code == 200, restored.text
    assert restored.json()["is_active"] is True
    assert restored.json()["version"] == 3
    with writer_app.state.session_factory() as session:
        row = session.get(Material, material["id"])
        assert row is not None
        assert row.is_active is True
        revisions = list(
            session.scalars(
                select(MasterDataObjectVersion)
                .where(
                    MasterDataObjectVersion.object_type == "material",
                    MasterDataObjectVersion.object_id == material["id"],
                )
                .order_by(MasterDataObjectVersion.version)
            )
        )
        assert [(revision.version, revision.action) for revision in revisions] == [
            (1, "create"),
            (2, "soft_delete"),
            (3, "update"),
        ]
        audits = list(
            session.scalars(
                select(OperationLog).where(
                    OperationLog.resource == "Material",
                    OperationLog.entity_id == material["id"],
                )
            )
        )

    disable_audit = next(audit for audit in audits if audit.action == "DISABLE")
    restore_audit = next(audit for audit in audits if audit.action == "UPDATE")
    assert json.loads(disable_audit.details or "{}") == {
        "code": material["code"],
        "reason": "停用失效材质",
    }
    assert json.loads(restore_audit.details or "{}")["after"]["is_active"] is True
