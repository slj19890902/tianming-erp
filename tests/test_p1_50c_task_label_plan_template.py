from __future__ import annotations

import json
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from tests.test_p1_32a2_requisition_production_print import (
    _login,
    production_print_app,
)


ROOT = Path(__file__).resolve().parents[1]
CURRENT_TEMPLATE = "current_40x30_v2"
LEGACY_TEMPLATE = "legacy_65x45_v1"


def _task_context(production_print_app: dict) -> tuple[int, int, int, int]:
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.production import ProductionTask

    with production_print_app["session_factory"]() as db:
        task = db.scalar(
            select(ProductionTask).where(
                ProductionTask.order_item_id == production_print_app["order_item_id"]
            )
        )
        assert task is not None
        item = db.get(OrderItem, task.order_item_id)
        assert item is not None
        order = db.get(Order, item.order_id)
        product = db.get(Product, item.product_id)
        assert order is not None and product is not None
        return int(task.id), int(task.version), int(product.version), int(order.customer_id)


def _add_actor(
    production_print_app: dict,
    *,
    username: str,
    role: str = "boss",
    customer_access_mode: str = "all",
    allow_orders_status: bool | None = None,
    customer_id: int | None = None,
) -> int:
    from app.core.security import hash_password
    from app.models.access_control import UserCustomerScope, UserPermissionOverride
    from app.models.user import User

    with production_print_app["session_factory"]() as db:
        user = User(
            username=username,
            password_hash=hash_password("123456"),
            role=role,
            real_name=username,
            display_name=username,
            customer_access_mode=customer_access_mode,
            must_change_password=False,
        )
        db.add(user)
        db.flush()
        if allow_orders_status is not None:
            db.add(
                UserPermissionOverride(
                    user_id=user.id,
                    permission_code="orders.status",
                    is_allowed=allow_orders_status,
                )
            )
        if customer_id is not None:
            db.add(
                UserCustomerScope(
                    user_id=user.id,
                    customer_id=customer_id,
                    assigned_by=user.id,
                )
            )
        db.commit()
        return int(user.id)


def _configure_refresh_candidate(
    production_print_app: dict,
    *,
    product_version: int = 9,
) -> tuple[int, int, int]:
    from app.models.order import OrderItem
    from app.models.product import Product
    from app.models.production import ProductionTask

    with production_print_app["session_factory"]() as db:
        task = db.scalar(
            select(ProductionTask).where(
                ProductionTask.order_item_id == production_print_app["order_item_id"]
            )
        )
        assert task is not None
        item = db.get(OrderItem, task.order_item_id)
        assert item is not None
        product = db.get(Product, item.product_id)
        assert product is not None
        product.production_label_enabled = True
        product.production_label_units_per_label = 5
        product.version = product_version
        task.production_label_template_version_snapshot = LEGACY_TEMPLATE
        task.production_label_product_version_snapshot = None
        db.commit()
        return int(task.id), int(task.version), int(product.version)


def _refresh_payload(
    *,
    key: str,
    task_version: int,
    product_version: int,
) -> dict:
    return {
        "idempotency_key": key,
        "expected_task_version": task_version,
        "expected_product_version": product_version,
        "confirmed_not_started": True,
        "confirmed_no_prior_print": True,
    }


def _production_router_app(production_print_app: dict) -> dict:
    from app.api.production import router as production_router

    app = production_print_app["app"]
    if not any(
        getattr(route, "path", "") == "/api/production/tasks/{task_id}/label-plan-refresh"
        for route in app.routes
    ):
        app.include_router(production_router, prefix="/api/production")
    return production_print_app


def _enable_one_frozen_plan(
    production_print_app: dict,
    *,
    template_version: str = CURRENT_TEMPLATE,
) -> tuple[int, int, int]:
    from app.models.order import OrderItem
    from app.models.product import Product
    from app.models.production import ProductionTask

    with production_print_app["session_factory"]() as db:
        task = db.scalar(
            select(ProductionTask)
            .join(OrderItem, OrderItem.id == ProductionTask.order_item_id)
            .where(OrderItem.id == production_print_app["order_item_id"])
        )
        assert task is not None
        item = db.get(OrderItem, task.order_item_id)
        assert item is not None
        product = db.get(Product, item.product_id)
        assert product is not None
        product.production_label_enabled = True
        product.production_label_units_per_label = 5
        task.production_label_enabled_snapshot = True
        task.production_label_units_per_label_snapshot = 5
        task.production_label_total_quantity_snapshot = 23
        task.production_label_count_snapshot = 5
        task.production_label_template_version_snapshot = template_version
        task.production_label_product_version_snapshot = int(product.version)
        db.commit()
        return int(task.id), int(task.version), int(product.version)


def _write_fact_snapshot(session_factory) -> dict:
    from app.models.audit import OperationLog
    from app.models.order import Order, OrderItem
    from app.models.production import ProductionCompletion, ProductionTask
    from app.models.production_label_print import (
        ProductionLabelPlanRefresh,
        ProductionPackagingLabelPrintJob,
        ProductionPackagingLabelPrintJobTask,
    )
    from app.models.supplier_requisition_order import SupplierRequisitionOrder

    with session_factory() as db:
        tasks = list(db.scalars(select(ProductionTask).order_by(ProductionTask.id)))
        audit_actions = list(
            db.scalars(select(OperationLog.action_code).order_by(OperationLog.id))
        )
        return {
            "orders": [
                (row.id, row.status, str(row.total_amount), row.updated_at)
                for row in db.scalars(select(Order).order_by(Order.id))
            ],
            "items": [
                (
                    row.id,
                    row.quantity,
                    row.material_status,
                    row.delivered_quantity,
                    row.created_at,
                )
                for row in db.scalars(select(OrderItem).order_by(OrderItem.id))
            ],
            "tasks": [
                (
                    row.id,
                    row.status,
                    row.planned_quantity,
                    row.material_input_quantity,
                    row.version,
                    row.production_label_enabled_snapshot,
                    row.production_label_units_per_label_snapshot,
                    row.production_label_total_quantity_snapshot,
                    row.production_label_count_snapshot,
                    row.production_label_template_version_snapshot,
                    row.production_label_product_version_snapshot,
                )
                for row in tasks
            ],
            "supplier_orders": [
                (row.id, row.status, row.total_quantity, row.requisition_qty)
                for row in db.scalars(
                    select(SupplierRequisitionOrder).order_by(SupplierRequisitionOrder.id)
                )
            ],
            "completions": int(db.scalar(select(func.count()).select_from(ProductionCompletion)) or 0),
            "refreshes": int(db.scalar(select(func.count()).select_from(ProductionLabelPlanRefresh)) or 0),
            "jobs": int(db.scalar(select(func.count()).select_from(ProductionPackagingLabelPrintJob)) or 0),
            "job_tasks": int(db.scalar(select(func.count()).select_from(ProductionPackagingLabelPrintJobTask)) or 0),
            "audits": int(db.scalar(select(func.count()).select_from(OperationLog)) or 0),
            "label_refresh_audits": audit_actions.count(
                "production.label_plan.refreshed"
            ),
            "label_job_prepared_audits": audit_actions.count(
                "production.packaging_label_job.prepared"
            ),
            "label_job_printed_audits": audit_actions.count(
                "production.packaging_label_job.printed"
            ),
        }


def _business_snapshot_without_denial_audits(session_factory) -> dict:
    """Security-denial evidence is an intentional separate transaction."""

    snapshot = _write_fact_snapshot(session_factory)
    snapshot.pop("audits")
    return snapshot


def test_new_task_snapshot_contract_freezes_current_template_and_product_version() -> None:
    from app.models.product import Product
    from app.services.production_label_strategy import (
        build_new_task_production_label_snapshot,
    )

    product = Product(
        version=7,
        box_style="A1",
        production_label_enabled=True,
        production_label_units_per_label=5,
    )
    snapshot = build_new_task_production_label_snapshot(product, total_quantity=23)
    assert snapshot == {
        "production_label_enabled_snapshot": True,
        "production_label_units_per_label_snapshot": 5,
        "production_label_total_quantity_snapshot": 23,
        "production_label_count_snapshot": 5,
        "production_label_template_version_snapshot": CURRENT_TEMPLATE,
        "production_label_product_version_snapshot": 7,
    }


def test_manual_refresh_is_versioned_idempotent_and_changes_only_label_plan(
    production_print_app,
) -> None:
    from app.models.order import OrderItem
    from app.models.product import Product
    from app.models.production import ProductionTask

    fixture = _production_router_app(production_print_app)
    with fixture["session_factory"]() as db:
        task = db.scalar(
            select(ProductionTask).where(
                ProductionTask.order_item_id == fixture["order_item_id"]
            )
        )
        assert task is not None
        item = db.get(OrderItem, task.order_item_id)
        product = db.get(Product, item.product_id)
        product.production_label_enabled = True
        product.production_label_units_per_label = 5
        product.version = 9
        task.production_label_template_version_snapshot = LEGACY_TEMPLATE
        task.production_label_product_version_snapshot = None
        db.commit()
        task_id = int(task.id)
        expected_task_version = int(task.version)

    before = _write_fact_snapshot(fixture["session_factory"])
    payload = {
        "idempotency_key": "p1-50c-refresh-one",
        "expected_task_version": expected_task_version,
        "expected_product_version": 9,
        "confirmed_not_started": True,
        "confirmed_no_prior_print": True,
    }
    with TestClient(fixture["app"]) as client:
        _login(client, "p132a2-admin")
        first = client.post(
            f"/api/production/tasks/{task_id}/label-plan-refresh",
            json=payload,
        )
        assert first.status_code == 200, first.text
        replay = client.post(
            f"/api/production/tasks/{task_id}/label-plan-refresh",
            json=payload,
        )
        assert replay.status_code == 200, replay.text
        assert replay.json() == first.json()
        conflict = client.post(
            f"/api/production/tasks/{task_id}/label-plan-refresh",
            json={**payload, "expected_product_version": 8},
        )
        assert conflict.status_code == 409

    after = _write_fact_snapshot(fixture["session_factory"])
    assert after["refreshes"] == before["refreshes"] + 1
    assert after["label_refresh_audits"] == before["label_refresh_audits"] + 1
    before_task = next(row for row in before["tasks"] if row[0] == task_id)
    after_task = next(row for row in after["tasks"] if row[0] == task_id)
    assert after_task[:4] == before_task[:4]
    assert after_task[4] == before_task[4] + 1
    assert after_task[5:] == (True, 5, 200, 40, CURRENT_TEMPLATE, 9)
    assert after["orders"] == before["orders"]
    assert after["items"] == before["items"]
    assert after["supplier_orders"] == before["supplier_orders"]
    assert after["completions"] == before["completions"] == 0
    assert after["jobs"] == before["jobs"] == 0


def test_legacy_disabled_snapshot_is_presented_as_manual_refresh_not_creation_error(
    production_print_app,
) -> None:
    fixture = _production_router_app(production_print_app)
    task_id, _task_version, _product_version = _configure_refresh_candidate(fixture)

    with TestClient(fixture["app"]) as client:
        _login(client, "p132a2-admin")
        response = client.get(
            "/api/production/tasks",
            params={"status": "waiting_material", "page": 1, "page_size": 200},
        )

    assert response.status_code == 200, response.text
    row = next(item for item in response.json()["items"] if item["id"] == task_id)
    assert row["production_label_plan"]["enabled"] is False
    assert row["production_label_plan"]["template_version"] == LEGACY_TEMPLATE
    assert row["current_production_label_product"]["enabled"] is True
    assert row["can_refresh_production_label_plan"] is True
    assert row["production_label_refresh_block_reason"] is None


def test_print_job_requires_explicit_confirmation_and_replays_frozen_template(
    production_print_app,
) -> None:
    from app.models.production_label_print import ProductionPackagingLabelPrintJob

    fixture = production_print_app
    task_id, _task_version, _product_version = _enable_one_frozen_plan(fixture)
    before = _write_fact_snapshot(fixture["session_factory"])
    order_id = fixture["supplier_order_id"]
    with TestClient(fixture["app"]) as client:
        _login(client, "p132a2-admin")
        preview = client.get(
            f"/api/requisition/supplier-orders/{order_id}/production-packaging-label-package"
        )
        assert preview.status_code == 200, preview.text
        package = preview.json()
        assert package["template_version"] == CURRENT_TEMPLATE
        assert package["label_count"] == 5
        assert package["labels"][-1]["quantity"] == 3

        prepare_payload = {
            "idempotency_key": "p1-50c-print-job-one",
            "confirmed": True,
            "plan_fingerprint": package["plan_fingerprint"],
        }
        prepared = client.post(
            f"/api/requisition/supplier-orders/{order_id}/production-packaging-label-jobs",
            json=prepare_payload,
        )
        assert prepared.status_code == 200, prepared.text
        prepared_data = prepared.json()
        job_id = int(prepared_data["job_id"])
        assert prepared_data["status"] == "prepared"
        assert prepared_data["template_version"] == CURRENT_TEMPLATE
        assert prepared_data["package"]["labels"][-1]["quantity"] == 3

        prepared_replay = client.post(
            f"/api/requisition/supplier-orders/{order_id}/production-packaging-label-jobs",
            json=prepare_payload,
        )
        assert prepared_replay.status_code == 200
        assert prepared_replay.json() == prepared_data

        frozen = client.get(
            f"/api/requisition/production-packaging-label-jobs/{job_id}"
        )
        assert frozen.status_code == 200, frozen.text
        assert frozen.json()["package"] == prepared_data["package"]

        confirm = client.post(
            f"/api/requisition/production-packaging-label-jobs/{job_id}/confirm",
            json={"idempotency_key": "p1-50c-print-confirm-one", "confirmed": True},
        )
        assert confirm.status_code == 200, confirm.text
        assert confirm.json()["status"] == "printed"
        confirm_replay = client.post(
            f"/api/requisition/production-packaging-label-jobs/{job_id}/confirm",
            json={"idempotency_key": "p1-50c-print-confirm-one", "confirmed": True},
        )
        assert confirm_replay.status_code == 200
        assert confirm_replay.json() == confirm.json()

    after = _write_fact_snapshot(fixture["session_factory"])
    assert after["jobs"] == before["jobs"] + 1
    assert after["job_tasks"] == before["job_tasks"] + 1
    with fixture["session_factory"]() as db:
        job = db.get(ProductionPackagingLabelPrintJob, job_id)
        assert job is not None
        assert job.status == "printed"
        frozen_payload = json.loads(job.payload_json)
        assert frozen_payload["template_version"] == CURRENT_TEMPLATE
        assert {row["production_task_id"] for row in frozen_payload["plans"]} == {task_id}


def test_current_task_disabled_snapshot_is_a_fail_closed_error_not_a_legacy_refresh(
    production_print_app,
) -> None:
    from app.models.order import OrderItem
    from app.models.product import Product
    from app.models.production import ProductionTask

    fixture = production_print_app
    with fixture["session_factory"]() as db:
        task = db.scalar(
            select(ProductionTask).where(
                ProductionTask.order_item_id == fixture["order_item_id"]
            )
        )
        assert task is not None
        item = db.get(OrderItem, task.order_item_id)
        assert item is not None
        product = db.get(Product, item.product_id)
        assert product is not None
        product.production_label_enabled = True
        product.production_label_units_per_label = 5
        task.production_label_enabled_snapshot = False
        task.production_label_units_per_label_snapshot = None
        task.production_label_total_quantity_snapshot = 0
        task.production_label_count_snapshot = 0
        task.production_label_template_version_snapshot = CURRENT_TEMPLATE
        task.production_label_product_version_snapshot = int(product.version)
        db.commit()

    with TestClient(fixture["app"]) as client:
        _login(client, "p132a2-admin")
        response = client.get(
            "/api/requisition/supplier-orders/"
            f"{fixture['supplier_order_id']}/production-packaging-label-package"
        )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "production_label_review_required"
    assert any("任务快照未启用" in reason for reason in detail["reasons"])


def test_print_template_has_exact_current_size_and_no_silent_core_field_elision(
    tmp_path: Path,
) -> None:
    source = (ROOT / "static" / "production-packaging-label.html").read_text(
        encoding="utf-8"
    )
    compact = re.sub(r"\s+", "", source)
    assert "@page{size:40mm30mm;margin:0" in compact
    assert "current_40x30_v2" in source
    assert "current_40x30_v1" in source
    assert "legacy_65x45_v1" in source
    assert "customer_name" in source
    assert "product_code" in source
    assert "product_name" in source
    assert "specification" in source
    assert "text-overflow:ellipsis" not in source
    assert "scrollHeight" in source and "clientHeight" in source
    assert "scrollWidth" in source and "clientWidth" in source
    assert "/production-packaging-label-jobs" in source
    assert "window.print()" in source
    assert "确认已实际打印" in source
    assert source.count("window.print()") == 1

    index_source = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    assert "productionLabelPlanInconsistent(row)" in index_source
    assert 'plan.templateVersion === "current_40x30_v2"' in index_source
    assert "plan.productVersion === current.version" in index_source
    assert "旧任务冻结未启用｜可按当前常用箱人工刷新" in index_source
    assert "标签异常 / 维护" in index_source
    assert "modal.type === 'productionLabelMaintenance'" in index_source
    assert 'status:"waiting_material"' in index_source
    assert '!["waiting_material", "pending"].includes(row?.status)' in index_source
    assert "loadProductionWaitingLabelPage(this.pages.productionWaitingLabels" in index_source
    cold_load = index_source.split("async loadProduction()", 1)[1].split(
        "async openProductionLabelMaintenance", 1
    )[0]
    assert "loadProductionWaitingLabelPage" not in cold_load

    node = shutil.which("node")
    assert node is not None
    scripts = [
        body
        for body in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", source, re.S)
        if body.strip()
    ]
    assert len(scripts) == 1
    target = tmp_path / "p1-50c-label-page.js"
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("method", ["put", "patch", "delete"])
def test_label_preview_and_frozen_job_reads_never_accept_business_writes(
    production_print_app,
    method: str,
) -> None:
    order_id = production_print_app["supplier_order_id"]
    with TestClient(production_print_app["app"]) as client:
        _login(client, "p132a2-admin")
        response = getattr(client, method)(
            f"/api/requisition/supplier-orders/{order_id}/production-packaging-label-package"
        )
        assert response.status_code == 405


@pytest.mark.parametrize("failure_kind", ["audit", "commit"])
def test_refresh_failure_rolls_back_task_receipt_and_audit(
    production_print_app,
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
) -> None:
    from app.api import production as production_api
    from sqlalchemy.orm import Session

    fixture = _production_router_app(production_print_app)
    task_id, task_version, product_version = _configure_refresh_candidate(fixture)
    payload = _refresh_payload(
        key=f"p1-50c-refresh-{failure_kind}-failure",
        task_version=task_version,
        product_version=product_version,
    )
    before = _write_fact_snapshot(fixture["session_factory"])

    with TestClient(fixture["app"], raise_server_exceptions=False) as client:
        _login(client, "p132a2-admin")
        if failure_kind == "audit":
            monkeypatch.setattr(
                production_api,
                "append_audit_event",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    RuntimeError("forced label refresh audit failure")
                ),
            )
        else:
            original_commit = Session.commit

            def fail_commit(self):
                raise RuntimeError("forced label refresh commit failure")

            monkeypatch.setattr(Session, "commit", fail_commit)
        failed = client.post(
            f"/api/production/tasks/{task_id}/label-plan-refresh", json=payload
        )
        assert failed.status_code == 500

    if failure_kind == "commit":
        monkeypatch.setattr(Session, "commit", original_commit)
    assert _business_snapshot_without_denial_audits(
        fixture["session_factory"]
    ) == {key: value for key, value in before.items() if key != "audits"}


def test_refresh_idempotency_is_actor_bound_and_scope_is_rechecked_on_replay(
    production_print_app,
) -> None:
    from app.models.access_control import UserCustomerScope
    from app.models.user import User

    fixture = _production_router_app(production_print_app)
    task_id, task_version, product_version = _configure_refresh_candidate(fixture)
    _task_id, _task_version, _product_version, customer_id = _task_context(fixture)
    actor_one_id = _add_actor(
        fixture,
        username="p150c-refresh-actor-one",
        role="sales",
        customer_access_mode="selected",
        allow_orders_status=True,
        customer_id=customer_id,
    )
    _add_actor(
        fixture,
        username="p150c-refresh-actor-two",
        role="boss",
        customer_access_mode="all",
    )
    payload = _refresh_payload(
        key="p1-50c-refresh-actor-bound",
        task_version=task_version,
        product_version=product_version,
    )

    with TestClient(fixture["app"]) as client:
        _login(client, "p150c-refresh-actor-one")
        created = client.post(
            f"/api/production/tasks/{task_id}/label-plan-refresh", json=payload
        )
        assert created.status_code == 200, created.text
        audits_after_create = _write_fact_snapshot(fixture["session_factory"])[
            "label_refresh_audits"
        ]

        changed_payload = client.post(
            f"/api/production/tasks/{task_id}/label-plan-refresh",
            json={**payload, "expected_product_version": product_version + 1},
        )
        assert changed_payload.status_code == 409

        client.cookies.clear()
        _login(client, "p150c-refresh-actor-two")
        cross_actor = client.post(
            f"/api/production/tasks/{task_id}/label-plan-refresh", json=payload
        )
        assert cross_actor.status_code == 409

        with fixture["session_factory"]() as db:
            db.query(UserCustomerScope).filter(
                UserCustomerScope.user_id == actor_one_id
            ).delete(synchronize_session=False)
            user = db.get(User, actor_one_id)
            assert user is not None
            user.auth_version += 1
            db.commit()
        client.cookies.clear()
        _login(client, "p150c-refresh-actor-one")
        revoked = client.post(
            f"/api/production/tasks/{task_id}/label-plan-refresh", json=payload
        )
        assert revoked.status_code == 403
        assert "production_label_plan" not in revoked.text

    after = _write_fact_snapshot(fixture["session_factory"])
    assert after["refreshes"] == 1
    assert after["label_refresh_audits"] == audits_after_create == 1


@pytest.mark.parametrize("blocking_fact", ["completion", "printed_job"])
def test_refresh_after_completion_or_print_fact_is_rejected_without_writes(
    production_print_app,
    blocking_fact: str,
) -> None:
    from app.core.time_contract import utc_now_naive
    from app.models.production import (
        ProductionCompletion,
        ProductionCompletionBatch,
        ProductionTask,
    )

    fixture = _production_router_app(production_print_app)
    task_id, task_version, product_version = _configure_refresh_candidate(fixture)
    if blocking_fact == "completion":
        with fixture["session_factory"]() as db:
            batch = ProductionCompletionBatch(
                idempotency_key="p1-50c-block-refresh-completion",
                request_hash="a" * 64,
                item_count=1,
                completed_by=1,
                completed_at=utc_now_naive(),
            )
            db.add(batch)
            db.flush()
            task = db.get(ProductionTask, task_id)
            assert task is not None
            db.add(
                ProductionCompletion(
                    batch_id=batch.id,
                    task_id=task.id,
                    order_item_id=task.order_item_id,
                    expected_version=task.version,
                    quantity=1,
                    material_input_quantity=1,
                    planned_output_quantity=1,
                    actual_output_quantity=1,
                    defective_quantity=0,
                    order_reserved_quantity=1,
                    direct_delivery_quantity=1,
                    stock_quantity=0,
                    surplus_finished_quantity=0,
                    initial_disposition="direct",
                    status="posted",
                    completed_by=1,
                    completed_at=utc_now_naive(),
                )
            )
            db.commit()
    else:
        _enable_one_frozen_plan(fixture, template_version=LEGACY_TEMPLATE)
        order_id = fixture["supplier_order_id"]
        with TestClient(fixture["app"]) as client:
            _login(client, "p132a2-admin")
            preview = client.get(
                f"/api/requisition/supplier-orders/{order_id}/production-packaging-label-package"
            )
            assert preview.status_code == 200, preview.text
            prepared = client.post(
                f"/api/requisition/supplier-orders/{order_id}/production-packaging-label-jobs",
                json={
                    "idempotency_key": "p1-50c-refresh-blocking-print-job",
                    "plan_fingerprint": preview.json()["plan_fingerprint"],
                    "confirmed": True,
                },
            )
            assert prepared.status_code == 200, prepared.text
            printed = client.post(
                f"/api/requisition/production-packaging-label-jobs/{prepared.json()['job_id']}/confirm",
                json={
                    "idempotency_key": "p1-50c-refresh-blocking-print-confirm",
                    "confirmed": True,
                },
            )
            assert printed.status_code == 200, printed.text

    before = _write_fact_snapshot(fixture["session_factory"])
    with TestClient(fixture["app"]) as client:
        _login(client, "p132a2-admin")
        blocked = client.post(
            f"/api/production/tasks/{task_id}/label-plan-refresh",
            json=_refresh_payload(
                key=f"p1-50c-refresh-blocked-{blocking_fact}",
                task_version=task_version,
                product_version=product_version,
            ),
        )
        assert blocked.status_code == 409
    assert _business_snapshot_without_denial_audits(
        fixture["session_factory"]
    ) == {key: value for key, value in before.items() if key != "audits"}


@pytest.mark.parametrize("operation", ["prepare", "confirm"])
@pytest.mark.parametrize("failure_kind", ["audit", "commit"])
def test_print_job_failure_rolls_back_all_business_and_audit_rows(
    production_print_app,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    failure_kind: str,
) -> None:
    from app.api import requisition as requisition_api
    from sqlalchemy.orm import Session

    fixture = production_print_app
    _enable_one_frozen_plan(fixture)
    order_id = fixture["supplier_order_id"]
    job_id: int | None = None
    with TestClient(fixture["app"]) as client:
        _login(client, "p132a2-admin")
        preview = client.get(
            f"/api/requisition/supplier-orders/{order_id}/production-packaging-label-package"
        )
        assert preview.status_code == 200, preview.text
        prepare_payload = {
            "idempotency_key": f"p1-50c-{operation}-{failure_kind}-job",
            "plan_fingerprint": preview.json()["plan_fingerprint"],
            "confirmed": True,
        }
        if operation == "confirm":
            prepared = client.post(
                f"/api/requisition/supplier-orders/{order_id}/production-packaging-label-jobs",
                json=prepare_payload,
            )
            assert prepared.status_code == 200, prepared.text
            job_id = int(prepared.json()["job_id"])

    before = _write_fact_snapshot(fixture["session_factory"])
    with TestClient(fixture["app"], raise_server_exceptions=False) as client:
        _login(client, "p132a2-admin")
        if failure_kind == "audit":
            monkeypatch.setattr(
                requisition_api,
                "append_audit_event",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    RuntimeError(f"forced label {operation} audit failure")
                ),
            )
        else:
            original_commit = Session.commit

            def fail_commit(self):
                raise RuntimeError(f"forced label {operation} commit failure")

            monkeypatch.setattr(Session, "commit", fail_commit)
        if operation == "prepare":
            failed = client.post(
                f"/api/requisition/supplier-orders/{order_id}/production-packaging-label-jobs",
                json=prepare_payload,
            )
        else:
            assert job_id is not None
            failed = client.post(
                f"/api/requisition/production-packaging-label-jobs/{job_id}/confirm",
                json={
                    "idempotency_key": f"p1-50c-{failure_kind}-confirm",
                    "confirmed": True,
                },
            )
        assert failed.status_code == 500

    if failure_kind == "commit":
        monkeypatch.setattr(Session, "commit", original_commit)
    assert _business_snapshot_without_denial_audits(
        fixture["session_factory"]
    ) == {key: value for key, value in before.items() if key != "audits"}


def test_print_job_prepare_and_confirm_idempotency_are_actor_bound(
    production_print_app,
) -> None:
    fixture = production_print_app
    _enable_one_frozen_plan(fixture)
    _add_actor(fixture, username="p150c-print-actor-two", role="boss")
    order_id = fixture["supplier_order_id"]
    with TestClient(fixture["app"]) as client:
        _login(client, "p132a2-admin")
        preview = client.get(
            f"/api/requisition/supplier-orders/{order_id}/production-packaging-label-package"
        )
        assert preview.status_code == 200, preview.text
        payload = {
            "idempotency_key": "p1-50c-print-idempotency",
            "plan_fingerprint": preview.json()["plan_fingerprint"],
            "confirmed": True,
        }
        first = client.post(
            f"/api/requisition/supplier-orders/{order_id}/production-packaging-label-jobs",
            json=payload,
        )
        assert first.status_code == 200, first.text
        after_first = _write_fact_snapshot(fixture["session_factory"])
        replay = client.post(
            f"/api/requisition/supplier-orders/{order_id}/production-packaging-label-jobs",
            json=payload,
        )
        assert replay.status_code == 200
        assert replay.json() == first.json()
        assert _write_fact_snapshot(fixture["session_factory"]) == after_first

        changed = client.post(
            f"/api/requisition/supplier-orders/{order_id}/production-packaging-label-jobs",
            json={**payload, "plan_fingerprint": "f" * 64},
        )
        assert changed.status_code == 409

        client.cookies.clear()
        _login(client, "p150c-print-actor-two")
        cross_actor = client.post(
            f"/api/requisition/supplier-orders/{order_id}/production-packaging-label-jobs",
            json=payload,
        )
        assert cross_actor.status_code == 409

        client.cookies.clear()
        _login(client, "p132a2-admin")
        job_id = int(first.json()["job_id"])
        confirm_payload = {
            "idempotency_key": "p1-50c-print-confirm-idempotency",
            "confirmed": True,
        }
        confirmed = client.post(
            f"/api/requisition/production-packaging-label-jobs/{job_id}/confirm",
            json=confirm_payload,
        )
        assert confirmed.status_code == 200, confirmed.text
        after_confirm = _write_fact_snapshot(fixture["session_factory"])
        confirm_replay = client.post(
            f"/api/requisition/production-packaging-label-jobs/{job_id}/confirm",
            json=confirm_payload,
        )
        assert confirm_replay.status_code == 200
        assert confirm_replay.json() == confirmed.json()
        assert _write_fact_snapshot(fixture["session_factory"]) == after_confirm

        changed_confirmation = client.post(
            f"/api/requisition/production-packaging-label-jobs/{job_id}/confirm",
            json={"idempotency_key": "p1-50c-other-confirm-key", "confirmed": True},
        )
        assert changed_confirmation.status_code == 409
        client.cookies.clear()
        _login(client, "p150c-print-actor-two")
        cross_actor_confirmation = client.post(
            f"/api/requisition/production-packaging-label-jobs/{job_id}/confirm",
            json=confirm_payload,
        )
        assert cross_actor_confirmation.status_code == 409


def test_legacy_printed_job_reprint_stays_frozen_at_65x45(
    production_print_app,
) -> None:
    fixture = production_print_app
    _enable_one_frozen_plan(fixture, template_version=LEGACY_TEMPLATE)
    order_id = fixture["supplier_order_id"]
    with TestClient(fixture["app"]) as client:
        _login(client, "p132a2-admin")
        preview = client.get(
            f"/api/requisition/supplier-orders/{order_id}/production-packaging-label-package"
        )
        assert preview.status_code == 200, preview.text
        assert preview.json()["template_dimensions"] == {
            "width_mm": 65,
            "height_mm": 45,
        }
        prepared = client.post(
            f"/api/requisition/supplier-orders/{order_id}/production-packaging-label-jobs",
            json={
                "idempotency_key": "p1-50c-legacy-frozen-job",
                "plan_fingerprint": preview.json()["plan_fingerprint"],
                "confirmed": True,
            },
        )
        assert prepared.status_code == 200, prepared.text
        job_id = int(prepared.json()["job_id"])
        confirmed = client.post(
            f"/api/requisition/production-packaging-label-jobs/{job_id}/confirm",
            json={
                "idempotency_key": "p1-50c-legacy-frozen-confirm",
                "confirmed": True,
            },
        )
        assert confirmed.status_code == 200, confirmed.text

        with fixture["session_factory"]() as db:
            from app.models.production import ProductionTask

            task = db.get(ProductionTask, confirmed.json()["package"]["plans"][0]["production_task_id"])
            assert task is not None
            task.production_label_template_version_snapshot = CURRENT_TEMPLATE
            task.version += 1
            db.commit()

        frozen = client.get(
            f"/api/requisition/production-packaging-label-jobs/{job_id}"
        )
        assert frozen.status_code == 200, frozen.text
        assert frozen.json()["template_version"] == LEGACY_TEMPLATE
        assert frozen.json()["package"]["template_dimensions"] == {
            "width_mm": 65,
            "height_mm": 45,
        }


def test_prepared_job_does_not_block_refresh_but_stale_preview_cannot_be_confirmed(
    production_print_app,
) -> None:
    fixture = _production_router_app(production_print_app)
    task_id, task_version, product_version = _configure_refresh_candidate(fixture)
    _enable_one_frozen_plan(fixture, template_version=LEGACY_TEMPLATE)
    order_id = fixture["supplier_order_id"]
    with TestClient(fixture["app"]) as client:
        _login(client, "p132a2-admin")
        old_preview = client.get(
            f"/api/requisition/supplier-orders/{order_id}/production-packaging-label-package"
        )
        assert old_preview.status_code == 200, old_preview.text
        prepared = client.post(
            f"/api/requisition/supplier-orders/{order_id}/production-packaging-label-jobs",
            json={
                "idempotency_key": "p1-50c-prepared-before-refresh",
                "plan_fingerprint": old_preview.json()["plan_fingerprint"],
                "confirmed": True,
            },
        )
        assert prepared.status_code == 200, prepared.text
        refreshed = client.post(
            f"/api/production/tasks/{task_id}/label-plan-refresh",
            json=_refresh_payload(
                key="p1-50c-refresh-after-prepared",
                task_version=task_version,
                product_version=product_version,
            ),
        )
        assert refreshed.status_code == 200, refreshed.text
        blocked = client.post(
            f"/api/requisition/production-packaging-label-jobs/{prepared.json()['job_id']}/confirm",
            json={
                "idempotency_key": "p1-50c-confirm-stale-prepared",
                "confirmed": True,
            },
        )
        assert blocked.status_code == 409

    with fixture["session_factory"]() as db:
        from app.models.production_label_print import ProductionPackagingLabelPrintJob

        job = db.get(ProductionPackagingLabelPrintJob, prepared.json()["job_id"])
        assert job is not None
        assert job.status == "prepared"
        assert job.printed_at is None


def test_corrupted_job_task_link_is_rejected_without_print_fact(
    production_print_app,
) -> None:
    from app.models.production_label_print import (
        ProductionPackagingLabelPrintJob,
        ProductionPackagingLabelPrintJobTask,
    )

    fixture = production_print_app
    _enable_one_frozen_plan(fixture)
    order_id = fixture["supplier_order_id"]
    with TestClient(fixture["app"]) as client:
        _login(client, "p132a2-admin")
        preview = client.get(
            f"/api/requisition/supplier-orders/{order_id}/production-packaging-label-package"
        )
        prepared = client.post(
            f"/api/requisition/supplier-orders/{order_id}/production-packaging-label-jobs",
            json={
                "idempotency_key": "p1-50c-corrupt-link-job",
                "plan_fingerprint": preview.json()["plan_fingerprint"],
                "confirmed": True,
            },
        )
        assert prepared.status_code == 200, prepared.text
        job_id = int(prepared.json()["job_id"])

        with fixture["session_factory"]() as db:
            link = db.scalar(
                select(ProductionPackagingLabelPrintJobTask).where(
                    ProductionPackagingLabelPrintJobTask.print_job_id == job_id
                )
            )
            assert link is not None
            link.snapshot_json = "{}"
            db.commit()

        before = _write_fact_snapshot(fixture["session_factory"])
        blocked = client.post(
            f"/api/requisition/production-packaging-label-jobs/{job_id}/confirm",
            json={
                "idempotency_key": "p1-50c-corrupt-link-confirm",
                "confirmed": True,
            },
        )
        assert blocked.status_code == 409
        assert _write_fact_snapshot(fixture["session_factory"]) == before

    with fixture["session_factory"]() as db:
        job = db.get(ProductionPackagingLabelPrintJob, job_id)
        assert job is not None
        assert job.status == "prepared"
        assert job.printed_confirmation_key is None


def test_orders_view_without_orders_status_can_prepare_and_confirm_print_job(
    production_print_app,
) -> None:
    fixture = _production_router_app(production_print_app)
    task_id, task_version, product_version = _enable_one_frozen_plan(fixture)
    _add_actor(
        fixture,
        username="p150c-view-only-printer",
        role="sales",
        customer_access_mode="all",
        allow_orders_status=False,
    )
    order_id = fixture["supplier_order_id"]
    with TestClient(fixture["app"]) as client:
        _login(client, "p150c-view-only-printer")
        preview = client.get(
            f"/api/requisition/supplier-orders/{order_id}/production-packaging-label-package"
        )
        assert preview.status_code == 200, preview.text
        prepared = client.post(
            f"/api/requisition/supplier-orders/{order_id}/production-packaging-label-jobs",
            json={
                "idempotency_key": "p1-50c-view-only-prepare",
                "plan_fingerprint": preview.json()["plan_fingerprint"],
                "confirmed": True,
            },
        )
        assert prepared.status_code == 200, prepared.text
        confirmed = client.post(
            f"/api/requisition/production-packaging-label-jobs/{prepared.json()['job_id']}/confirm",
            json={
                "idempotency_key": "p1-50c-view-only-confirm",
                "confirmed": True,
            },
        )
        assert confirmed.status_code == 200, confirmed.text

        refresh = client.post(
            f"/api/production/tasks/{task_id}/label-plan-refresh",
            json=_refresh_payload(
                key="p1-50c-view-only-refresh-denied",
                task_version=task_version,
                product_version=product_version,
            ),
        )
        assert refresh.status_code == 403


@pytest.mark.parametrize("operation", ["refresh", "prepare"])
def test_concurrent_exact_idempotency_key_returns_one_winner_receipt(
    production_print_app,
    operation: str,
) -> None:
    """The supported single-worker guard must include commit and replay read."""

    fixture = _production_router_app(production_print_app)
    task_id, task_version, product_version = _configure_refresh_candidate(fixture)
    _enable_one_frozen_plan(fixture)
    order_id = fixture["supplier_order_id"]

    with TestClient(fixture["app"]) as setup_client:
        _login(setup_client, "p132a2-admin")
        preview = setup_client.get(
            f"/api/requisition/supplier-orders/{order_id}/production-packaging-label-package"
        )
        assert preview.status_code == 200, preview.text
        plan_fingerprint = preview.json()["plan_fingerprint"]

    barrier = Barrier(2)

    def send_request() -> tuple[int, dict]:
        with TestClient(fixture["app"]) as client:
            _login(client, "p132a2-admin")
            barrier.wait()
            if operation == "refresh":
                response = client.post(
                    f"/api/production/tasks/{task_id}/label-plan-refresh",
                    json=_refresh_payload(
                        key="p1-50c-concurrent-refresh",
                        task_version=task_version,
                        product_version=product_version,
                    ),
                )
            else:
                response = client.post(
                    f"/api/requisition/supplier-orders/{order_id}/production-packaging-label-jobs",
                    json={
                        "idempotency_key": "p1-50c-concurrent-prepare",
                        "plan_fingerprint": plan_fingerprint,
                        "confirmed": True,
                    },
                )
            return response.status_code, response.json()

    before = _write_fact_snapshot(fixture["session_factory"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = [future.result() for future in [pool.submit(send_request), pool.submit(send_request)]]

    assert [status for status, _body in responses] == [200, 200]
    assert responses[0][1] == responses[1][1]
    after = _write_fact_snapshot(fixture["session_factory"])
    if operation == "refresh":
        assert after["refreshes"] == before["refreshes"] + 1
        assert after["label_refresh_audits"] == before["label_refresh_audits"] + 1
        changed = next(row for row in after["tasks"] if row[0] == task_id)
        original = next(row for row in before["tasks"] if row[0] == task_id)
        assert changed[4] == original[4] + 1
    else:
        assert after["jobs"] == before["jobs"] + 1
        assert after["job_tasks"] == before["job_tasks"] + 1
        assert (
            after["label_job_prepared_audits"]
            == before["label_job_prepared_audits"] + 1
        )
