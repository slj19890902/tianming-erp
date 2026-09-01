from __future__ import annotations

from collections.abc import Generator
from decimal import Decimal
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from tests.test_phase5_orders import order_api_app


@pytest.fixture()
def processing_cost_app(tmp_path: Path):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.processing_cost import router as processing_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.customer import Customer
    from app.models.processing_cost import ProcessingCostSettings
    from app.models.product import Product
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "p1-131-processing.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        db.add(
            User(
                username="p1131-processing-admin",
                password_hash=hash_password("RolePass123!"),
                role="admin",
                real_name="Admin",
                must_change_password=False,
            )
        )
        db.add_all(
            [
                User(
                    username="p1131-processing-boss",
                    password_hash=hash_password("RolePass123!"),
                    role="boss",
                    real_name="Boss",
                    must_change_password=False,
                ),
                User(
                    username="p1131-processing-finance",
                    password_hash=hash_password("RolePass123!"),
                    role="finance",
                    real_name="Finance",
                    must_change_password=False,
                ),
                User(
                    username="p1131-processing-sales",
                    password_hash=hash_password("RolePass123!"),
                    role="sales",
                    real_name="Sales",
                    must_change_password=False,
                ),
            ]
        )
        customer = Customer(customer_code="PROC", name="苏州思迈尔包装有限公司")
        db.add(customer)
        db.flush()
        db.add_all(
            [
                Product(
                    customer_id=customer.id,
                    product_code="PROC-A1",
                    customer_material_code="KH-A1",
                    product_name="A1 标准纸箱",
                    box_category="normal",
                    box_style="A1",
                    print_content="无印刷",
                    production_process="",
                    splice_mode="single",
                ),
                Product(
                    customer_id=customer.id,
                    product_code="PROC-IRREGULAR",
                    customer_material_code="KH-IRREGULAR",
                    product_name="异形纸箱",
                    box_category="normal",
                    box_style="异形箱",
                    print_content="无印刷",
                    production_process="无需结合",
                ),
            ]
        )
        db.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(processing_router, prefix="/api/finance")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    yield app, factory
    engine.dispose()


def _login(client: TestClient) -> None:
    _login_as(client, "p1131-processing-admin")


def _login_as(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/auth/login",
        json={
            "username": username,
            "password": "RolePass123!",
        },
    )
    assert response.status_code == 200, response.text


def _settings_update_payload(settings: dict, *, key: str) -> dict:
    from app.api.processing_cost import ProcessingSettingsFields

    payload = {name: settings[name] for name in ProcessingSettingsFields.model_fields}
    payload.update(
        expected_version=settings["version"],
        idempotency_key=key,
    )
    return payload


def test_processing_settings_and_product_profile_api_contract(
    processing_cost_app,
) -> None:
    from app.models.audit import OperationLog
    from app.models.finance import FinanceIdempotencyRecord
    from app.models.processing_cost import ProductProcessingProfile

    app, factory = processing_cost_app
    with TestClient(app) as client:
        _login(client)
        current = client.get("/api/finance/processing-settings")
        assert current.status_code == 200, current.text
        body = current.json()
        assert Decimal(str(body["working_hours_per_day"])) == Decimal("8")
        assert Decimal(str(body["new_printer_normal_sheets_per_minute"])) == Decimal(
            "90"
        )
        assert body["worker_day_cost"] is None
        assert body["worker_day_cost_status"] == "incomplete"
        assert [row["value"] for row in body["default_printer_modes"]] == [
            "new",
            "old",
        ]
        assert [row["value"] for row in body["printer_modes"]] == [
            "auto",
            "new",
            "old",
            "none",
        ]

        payload = _settings_update_payload(body, key="processing-settings-001")
        payload["average_worker_monthly_salary"] = "5000.00"
        payload["average_worker_monthly_social_cost"] = "1000.00"
        updated = client.put("/api/finance/processing-settings", json=payload)
        assert updated.status_code == 200, updated.text
        assert updated.json()["version"] == 2
        assert Decimal(str(updated.json()["worker_day_cost"])) == Decimal(
            "230.769231"
        )
        replay = client.put("/api/finance/processing-settings", json=payload)
        assert replay.status_code == 200, replay.text
        assert replay.json()["version"] == 2

        created = client.put(
            "/api/finance/product-processing-profiles/1",
            json={
                "printer_mode": "old",
                "die_cut_mode": "large",
                "assembly_worker_days_per_1000": "999",
                "assembly_workers": "2",
                "assembly_days": "1.5",
                "assembly_output_quantity": "3000",
                "expected_version": 0,
                "idempotency_key": " profile-upsert-001 ",
            },
        )
        assert created.status_code == 200, created.text
        profile = created.json()
        assert profile["version"] == 1
        assert profile["customer_material_code"] == "KH-A1"
        assert profile["product_code"] == "PROC-A1"
        assert Decimal(str(profile["assembly_worker_days_per_1000"])) == Decimal(
            "1.000000"
        )
        replay = client.put(
            "/api/finance/product-processing-profiles/1",
            json={
                "printer_mode": "old",
                "die_cut_mode": "large",
                "assembly_worker_days_per_1000": "999",
                "assembly_workers": "2",
                "assembly_days": "1.5",
                "assembly_output_quantity": "3000",
                "expected_version": 0,
                "idempotency_key": "profile-upsert-001",
            },
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["id"] == profile["id"]

        rows = client.get(
            "/api/finance/product-processing-profiles", params={"search": "KH-A1"}
        )
        assert rows.status_code == 200, rows.text
        assert rows.json()["total"] == 1
        assert rows.json()["items"][0]["is_override"] is True
        assert [row["value"] for row in rows.json()["die_cut_modes"]] == [
            "auto",
            "none",
            "small_normal",
            "small_complex",
            "large",
            "oversize",
        ]

        stale = client.put(
            "/api/finance/product-processing-profiles/1",
            json={
                "printer_mode": "auto",
                "die_cut_mode": "auto",
                "expected_version": 0,
                "idempotency_key": "profile-stale-001",
            },
        )
        assert stale.status_code == 409
        partial_source = client.post(
            "/api/finance/product-processing-profiles",
            json={
                "product_id": 2,
                "printer_mode": "auto",
                "die_cut_mode": "auto",
                "assembly_workers": "2",
                "idempotency_key": "profile-partial-001",
            },
        )
        assert partial_source.status_code == 422
        blank_key = client.post(
            "/api/finance/product-processing-profiles",
            json={
                "product_id": 2,
                "printer_mode": "auto",
                "die_cut_mode": "auto",
                "idempotency_key": "        ",
            },
        )
        assert blank_key.status_code == 422

        deleted = client.request(
            "DELETE",
            "/api/finance/product-processing-profiles/1",
            json={"expected_version": 1, "idempotency_key": "profile-delete-001"},
        )
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["product_id"] == 1
        assert deleted.json()["restored_default"] is True
        assert deleted.json()["is_override"] is False
        assert deleted.json()["version"] == 2
        assert deleted.json()["printer_mode"] == "auto"
        assert deleted.json()["die_cut_mode"] == "auto"

        with factory() as db:
            restored = db.scalar(select(ProductProcessingProfile))
            assert restored is not None
            assert restored.id == profile["id"]
            assert restored.version == 2

        edited_again = client.put(
            "/api/finance/product-processing-profiles/1",
            json={
                "printer_mode": "new",
                "die_cut_mode": "small_normal",
                "expected_version": 2,
                "idempotency_key": "profile-after-restore-001",
            },
        )
        assert edited_again.status_code == 200, edited_again.text
        assert edited_again.json()["id"] == profile["id"]
        assert edited_again.json()["version"] == 3
        assert edited_again.json()["is_override"] is True

    with factory() as db:
        assert db.scalar(select(func.count()).select_from(ProductProcessingProfile)) == 1
        restored = db.scalar(select(ProductProcessingProfile))
        assert restored.id == profile["id"]
        assert restored.version == 3
        assert restored.printer_mode == "new"
        assert restored.die_cut_mode == "small_normal"
        assert db.scalar(select(func.count()).select_from(FinanceIdempotencyRecord)) == 4
        actions = set(db.scalars(select(OperationLog.action_code)).all())
        assert "finance.processing_settings.update" in actions
        assert "finance.product_processing_profile.upsert" in actions
        assert "finance.product_processing_profile.delete" in actions


def test_processing_cost_permission_matrix_is_least_privilege(
    processing_cost_app,
) -> None:
    app, _factory = processing_cost_app
    with TestClient(app) as client:
        _login_as(client, "p1131-processing-boss")
        boss_read = client.get("/api/finance/processing-settings")
        assert boss_read.status_code == 200, boss_read.text
        boss_payload = _settings_update_payload(
            boss_read.json(), key="processing-boss-denied-001"
        )
        assert client.put(
            "/api/finance/processing-settings", json=boss_payload
        ).status_code == 403

        _login_as(client, "p1131-processing-sales")
        assert client.get("/api/finance/processing-settings").status_code == 403

        _login_as(client, "p1131-processing-finance")
        finance_read = client.get("/api/finance/processing-settings")
        assert finance_read.status_code == 200, finance_read.text
        finance_payload = _settings_update_payload(
            finance_read.json(), key="processing-finance-update-001"
        )
        finance_payload["average_worker_monthly_salary"] = "5000.00"
        finance_write = client.put(
            "/api/finance/processing-settings", json=finance_payload
        )
        assert finance_write.status_code == 200, finance_write.text
        assert finance_write.json()["version"] == 2


def test_processing_settings_idempotency_conflict_and_failure_rollback(
    processing_cost_app,
    monkeypatch,
) -> None:
    from app.models.audit import OperationLog
    from app.models.finance import FinanceIdempotencyRecord
    from app.models.processing_cost import ProcessingCostSettings

    app, factory = processing_cost_app
    with TestClient(app) as client:
        _login(client)
        current = client.get("/api/finance/processing-settings").json()
        payload = _settings_update_payload(
            current, key="processing-idem-conflict-001"
        )
        payload["average_worker_monthly_salary"] = "5000.00"
        updated = client.put("/api/finance/processing-settings", json=payload)
        assert updated.status_code == 200, updated.text
        assert updated.json()["version"] == 2

        conflicting_payload = dict(payload)
        conflicting_payload["average_worker_monthly_salary"] = "5100.00"
        conflict = client.put(
            "/api/finance/processing-settings", json=conflicting_payload
        )
        assert conflict.status_code == 409, conflict.text
        assert conflict.json()["detail"]["code"] == "finance_idempotency_conflict"

    with factory() as db:
        before_idempotency_count = db.scalar(
            select(func.count()).select_from(FinanceIdempotencyRecord)
        )
        before_audit_count = db.scalar(
            select(func.count())
            .select_from(OperationLog)
            .where(
                OperationLog.action_code == "finance.processing_settings.update"
            )
        )
        settings = db.get(ProcessingCostSettings, 1)
        assert settings.version == 2
        assert settings.average_worker_monthly_salary == Decimal("5000.00")

    def fail_audit(*args, **kwargs):
        raise RuntimeError("forced audit failure")

    monkeypatch.setattr("app.api.processing_cost._audit", fail_audit)
    with TestClient(app, raise_server_exceptions=False) as client:
        _login(client)
        current = client.get("/api/finance/processing-settings").json()
        failed_payload = _settings_update_payload(
            current, key="processing-forced-failure-001"
        )
        failed_payload["average_worker_monthly_salary"] = "5200.00"
        failed = client.put(
            "/api/finance/processing-settings", json=failed_payload
        )
        assert failed.status_code == 500, failed.text

    with factory() as db:
        settings = db.get(ProcessingCostSettings, 1)
        assert settings.version == 2
        assert settings.average_worker_monthly_salary == Decimal("5000.00")
        assert db.scalar(
            select(func.count()).select_from(FinanceIdempotencyRecord)
        ) == before_idempotency_count
        assert db.scalar(
            select(func.count())
            .select_from(OperationLog)
            .where(
                OperationLog.action_code == "finance.processing_settings.update"
            )
        ) == before_audit_count


def test_standard_time_rules_and_missing_salary_never_become_zero(
    processing_cost_app,
) -> None:
    from app.models.finance_cost import FinanceCostPoolEntry
    from app.models.processing_cost import ProcessingCostSettings, ProductProcessingProfile
    from app.models.product import Product
    from app.services.processing_cost import estimate_standard_processing_cost

    _app, factory = processing_cost_app
    with factory() as db:
        settings = db.get(ProcessingCostSettings, 1)
        settings.average_worker_monthly_salary = Decimal("5000.00")
        settings.average_worker_monthly_social_cost = Decimal("0.00")
        product = db.get(Product, 1)
        standard = estimate_standard_processing_cost(db, product=product, quantity=1000)
        assert standard["calculation_status"] == "calculated"
        assert standard["worker_day_cost"] == "192.307692"
        assert standard["printing"]["printer_mode"] == "new"
        assert standard["printing"]["sheet_quantity"] == 1000
        assert standard["printing"]["passes"] == 1
        assert standard["joining"]["joining_mode"] == "ordinary"
        assert standard["estimated_processing_cost"] is not None

        product.production_process = "无需结合"
        no_joining = estimate_standard_processing_cost(db, product=product, quantity=1000)
        assert no_joining["joining"]["joining_mode"] == "none"

        product.production_process = "粘贴"
        profile = ProductProcessingProfile(
            product_id=product.id,
            printer_mode="none",
            die_cut_mode="large",
            assembly_worker_days_per_1000=Decimal("1.000000"),
        )
        db.add(profile)
        db.flush()
        with_extra = estimate_standard_processing_cost(db, product=product, quantity=1000)
        assert with_extra["die_cut"]["die_cut_mode"] == "large"
        assert with_extra["die_cut"]["crew_size"] == 2
        assert with_extra["die_cut"]["separate_stripping_workers"] == 0
        assert with_extra["joining"]["joining_mode"] == "ordinary"
        assert with_extra["extra_assembly"]["assembly_mode"] == "product_override"
        assert with_extra["extra_assembly"]["worker_days"] == "1.000000"

        product.splice_mode = "double"
        product.production_process = "无需结合"
        profile.printer_mode = "new"
        double_splice = estimate_standard_processing_cost(
            db, product=product, quantity=1000
        )
        assert double_splice["printing"]["sheet_quantity"] == 2000
        assert double_splice["joining"]["joining_mode"] == "double_splice"

        db.delete(profile)
        db.flush()
        irregular = db.get(Product, 2)
        irregular_result = estimate_standard_processing_cost(
            db, product=irregular, quantity=100
        )
        assert irregular_result["die_cut"]["die_cut_mode"] == "large"
        assert irregular_result["die_cut"]["crew_size"] == 2
        assert irregular_result["die_cut"]["separate_stripping_workers"] == 0

        settings.average_worker_monthly_salary = None
        incomplete = estimate_standard_processing_cost(db, product=product, quantity=1000)
        assert incomplete["calculation_status"] == "incomplete"
        assert incomplete["estimated_processing_cost"] is None
        assert "平均生产月薪待维护" in incomplete["missing_items"]
        assert db.scalar(select(func.count()).select_from(FinanceCostPoolEntry)) == 0


def test_confirmed_processing_capacity_matrix_has_exact_worker_days(
    processing_cost_app,
) -> None:
    from app.models.processing_cost import ProcessingCostSettings, ProductProcessingProfile
    from app.models.product import Product
    from app.services.processing_cost import estimate_standard_processing_cost

    _app, factory = processing_cost_app
    with factory() as db:
        settings = db.get(ProcessingCostSettings, 1)
        settings.average_worker_monthly_salary = Decimal("5200.00")
        settings.average_worker_monthly_social_cost = Decimal("780.00")
        product = db.get(Product, 1)
        product.box_style = "A1"
        product.splice_mode = "single"
        product.production_process = "无需结合"
        profile = ProductProcessingProfile(
            product_id=product.id,
            printer_mode="new",
            die_cut_mode="none",
        )
        db.add(profile)
        db.flush()

        product.print_content = "双色印刷"
        new_two_colors = estimate_standard_processing_cost(
            db, product=product, quantity=180
        )
        assert new_two_colors["printing"]["passes"] == 1
        assert new_two_colors["printing"]["worker_days"] == "0.133333"

        product.print_content = "三色印刷"
        new_three_colors = estimate_standard_processing_cost(
            db, product=product, quantity=180
        )
        assert new_three_colors["printing"]["passes"] == 2
        assert new_three_colors["printing"]["worker_days"] == "0.266667"

        profile.printer_mode = "old"
        product.print_content = "双色印刷"
        old_two_colors = estimate_standard_processing_cost(
            db, product=product, quantity=180
        )
        assert old_two_colors["printing"]["worker_days"] == "0.150000"

        profile.printer_mode = "none"
        for mode, quantity, expected_days in (
            ("small_normal", 1800, "0.250000"),
            ("small_complex", 1800, "0.375000"),
            ("large", 3000, "0.375000"),
            ("oversize", 15, "0.250000"),
        ):
            profile.die_cut_mode = mode
            result = estimate_standard_processing_cost(
                db, product=product, quantity=quantity
            )
            assert result["die_cut"]["worker_days"] == expected_days
            if mode in {"large", "oversize"}:
                assert result["die_cut"]["separate_stripping_workers"] == 0

        profile.die_cut_mode = "none"
        product.splice_mode = "double"
        double_splice = estimate_standard_processing_cost(
            db, product=product, quantity=120
        )
        assert double_splice["joining"]["joining_mode"] == "double_splice"
        assert double_splice["joining"]["worker_days"] == "0.250000"

        product.splice_mode = "single"
        product.production_process = "粘贴"
        ordinary_joining = estimate_standard_processing_cost(
            db, product=product, quantity=3600
        )
        assert ordinary_joining["joining"]["joining_mode"] == "ordinary"
        assert ordinary_joining["joining"]["worker_days"] == "0.250000"

        product.production_process = "无需结合"
        profile.assembly_worker_days_per_1000 = Decimal("3.000000")
        assembly = estimate_standard_processing_cost(
            db, product=product, quantity=2000
        )
        assert assembly["worker_day_cost"] == "230.000000"
        assert assembly["extra_assembly"]["worker_days"] == "6.000000"
        assert assembly["total_worker_days"] == "6.000000"
        assert assembly["estimated_processing_cost"] == "1380.00"

        db.delete(settings)
        db.flush()
        with pytest.raises(ValueError, match="尚未初始化"):
            estimate_standard_processing_cost(db, product=product, quantity=1000)


def test_order_item_supply_mode_snapshot_wins_in_both_directions(
    processing_cost_app,
) -> None:
    from app.models.processing_cost import ProcessingCostSettings
    from app.models.product import Product
    from app.services.processing_cost import estimate_order_item_processing_cost

    _app, factory = processing_cost_app
    with factory() as db:
        settings = db.get(ProcessingCostSettings, 1)
        settings.average_worker_monthly_salary = Decimal("5000.00")
        product = db.get(Product, 1)
        item = SimpleNamespace(
            product=product,
            quantity=100,
            snapshot_splice_mode="single",
            supply_mode_snapshot="external_purchase",
        )
        external_snapshot = estimate_order_item_processing_cost(db, item)
        assert external_snapshot["printing"]["printer_mode"] == "none"
        assert external_snapshot["die_cut"]["die_cut_mode"] == "none"
        assert external_snapshot["joining"]["joining_mode"] == "none"

        product.supply_mode = "external_purchase"
        item.supply_mode_snapshot = "corrugated_production"
        with db.no_autoflush:
            corrugated_snapshot = estimate_order_item_processing_cost(db, item)
        assert corrugated_snapshot["printing"]["printer_mode"] == "new"
        assert corrugated_snapshot["joining"]["joining_mode"] == "ordinary"


def test_missing_salary_snapshot_persists_null_processing_amounts(
    order_api_app,
    monkeypatch,
) -> None:
    from app.models.order_estimated_cost_snapshot import (
        SalesOrderItemEstimatedCostSnapshot,
    )
    from app.models.processing_cost import ProcessingCostSettings
    from app.models.product import Product
    from app.services import order_material_cost
    from tests.test_p1_28c1_estimated_cost_snapshots import _fixed_price
    from tests.test_phase5_orders import _login, _payload

    monkeypatch.setattr(order_material_cost, "get_effective_material_price", _fixed_price)
    app, factory = order_api_app
    with factory() as db:
        settings = db.get(ProcessingCostSettings, 1)
        settings.average_worker_monthly_salary = None
        product = db.get(Product, 1)
        product.box_style = "A1"
        product.print_content = "单色印刷"
        product.report_length_mm = 500
        product.report_width_mm = 400
        db.commit()
    payload = _payload()
    payload["customer_po"] = "PO-P1-131-MISSING-SALARY"
    payload["items"] = [payload["items"][0]]
    with TestClient(app) as client:
        _login(client)
        created = client.post("/api/orders", json=payload)
        assert created.status_code == 201, created.text
        item = created.json()["items"][0]
        assert item["estimated_order_total_cost"] is None
        assert item["estimated_cost_breakdown"]["processing_unit_cost"] is None
        assert item["estimated_cost_breakdown"]["processing_total_cost"] is None
    with factory() as db:
        snapshot = db.scalar(select(SalesOrderItemEstimatedCostSnapshot))
        assert snapshot.processing_unit_cost is None
        assert snapshot.processing_total_cost is None


def test_order_snapshot_freezes_processing_versions_without_actual_wage_write(
    order_api_app,
    monkeypatch,
) -> None:
    from app.models.finance_cost import FinanceCostPoolEntry
    from app.models.order_estimated_cost_snapshot import (
        SalesOrderItemEstimatedCostSnapshot,
    )
    from app.models.processing_cost import ProcessingCostSettings
    from app.models.product import Product
    from app.services import order_material_cost
    from tests.test_p1_28c1_estimated_cost_snapshots import _fixed_price
    from tests.test_phase5_orders import _login, _payload

    monkeypatch.setattr(order_material_cost, "get_effective_material_price", _fixed_price)
    app, factory = order_api_app
    with factory() as db:
        product = db.get(Product, 1)
        product.box_style = "A1"
        product.print_content = "双色印刷"
        product.report_length_mm = 500
        product.report_width_mm = 400
        db.commit()

    payload = _payload()
    payload["items"] = [payload["items"][0]]
    with TestClient(app) as client:
        _login(client)
        created = client.post("/api/orders", json=payload)
        assert created.status_code == 201, created.text
        item = created.json()["items"][0]
        assert item["estimated_total_cost_status"] == "calculated"
        standard_v1 = item["estimated_cost_breakdown"]["standard_processing"]
        assert standard_v1["settings_version"] == 1
        assert standard_v1["product_processing_profile_version"] == 0
        assert standard_v1["printing"]["sheet_quantity"] == 200
        assert standard_v1["printing"]["passes"] == 1
        assert standard_v1["printing"]["machine_hours"] is not None
        assert standard_v1["joining"]["worker_days"] is not None
        assert standard_v1["total_worker_days"] is not None
        assert standard_v1["average_worker_monthly_salary"] == "5000.00"
        assert len(standard_v1["worker_day_cost"].split(".")[1]) == 6
        item_id = item["id"]

        with factory() as db:
            settings = db.get(ProcessingCostSettings, 1)
            settings.new_printer_normal_sheets_per_minute = Decimal("60")
            settings.version = 2
            db.commit()

        refreshed = client.post(
            f"/api/orders/items/{item_id}/estimated-cost",
            json={"expected_snapshot_version": 1, "loss_rate": "0.03"},
        )
        assert refreshed.status_code == 200, refreshed.text
        assert refreshed.json()["estimated_total_cost_snapshot_version"] == 2
        assert (
            refreshed.json()["estimated_cost_breakdown"]["standard_processing"][
                "settings_version"
            ]
            == 2
        )

    with factory() as db:
        snapshots = db.scalars(
            select(SalesOrderItemEstimatedCostSnapshot).order_by(
                SalesOrderItemEstimatedCostSnapshot.snapshot_version
            )
        ).all()
        assert len(snapshots) == 2
        assert json.loads(snapshots[0].breakdown_json)["standard_processing"][
            "settings_version"
        ] == 1
        assert json.loads(snapshots[1].breakdown_json)["standard_processing"][
            "settings_version"
        ] == 2
        assert db.scalar(select(func.count()).select_from(FinanceCostPoolEntry)) == 0
