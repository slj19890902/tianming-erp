from __future__ import annotations

import copy
import json
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.models.audit import OperationLog
from app.models.customer import Customer
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.production import ProductionTask
from app.models.production_label_print import (
    ProductionPackagingLabelLayoutRevision,
)
from app.models.supplier_requisition_order import SupplierRequisitionOrderItem
from app.services.production_packaging_label_layout import default_layout
from tests.test_p1_32a2_requisition_production_print import (
    _login,
    production_print_app,
)


ROOT = Path(__file__).resolve().parents[1]
PRINT_PAGE = ROOT / "static" / "production-packaging-label.html"
CURRENT_TEMPLATE = "current_40x30_v2"


def _changed_layout(*, width_mm: float) -> dict:
    layout = copy.deepcopy(default_layout())
    customer = next(
        element
        for element in layout["elements"]
        if element["id"] == "customer_short_name"
    )
    customer["width_mm"] = width_mm
    customer["x_mm"] = round((40 - width_mm) / 2, 3)
    customer["font_size_mm"] = 3.4
    return layout


def _enable_compact_labels(fixture: dict) -> None:
    with fixture["session_factory"]() as db:
        item = db.get(OrderItem, fixture["order_item_id"])
        assert item is not None
        order = db.get(Order, item.order_id)
        assert order is not None
        customer = db.get(Customer, order.customer_id)
        assert customer is not None
        customer.name = "苏州思迈尔包装有限公司"
        item.customer_name = customer.name
        supplier_item = db.get(
            SupplierRequisitionOrderItem,
            fixture["supplier_item_id"],
        )
        assert supplier_item is not None
        supplier_item.customer_name = customer.name
        product = db.get(Product, item.product_id)
        task = db.scalar(
            select(ProductionTask).where(ProductionTask.order_item_id == item.id)
        )
        assert product is not None and task is not None
        product.production_label_enabled = True
        product.production_label_units_per_label = 5
        task.production_label_enabled_snapshot = True
        task.production_label_units_per_label_snapshot = 5
        task.production_label_total_quantity_snapshot = 23
        task.production_label_count_snapshot = 5
        task.production_label_template_version_snapshot = CURRENT_TEMPLATE
        task.production_label_product_version_snapshot = int(product.version)
        db.commit()


def _save_draft(client: TestClient, layout: dict, key: str, version: int) -> dict:
    response = client.put(
        "/api/requisition/production-packaging-label-layout/admin/draft",
        json={
            "operation_key": key,
            "expected_draft_version": version,
            "layout": layout,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _publish(client: TestClient, state: dict, key: str) -> dict:
    response = client.post(
        "/api/requisition/production-packaging-label-layout/admin/publish",
        json={
            "operation_key": key,
            "expected_draft_version": state["draft"]["version"],
            "expected_release_version": state["published"]["version"],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_layout_admin_is_append_only_versioned_and_fail_closed(
    production_print_app,
) -> None:
    fixture = production_print_app
    with TestClient(fixture["app"]) as client:
        _login(client, "p132a2-sales")
        denied = client.get(
            "/api/requisition/production-packaging-label-layout/admin"
        )
        assert denied.status_code == 403

        _login(client, "p132a2-admin")
        initial = client.get(
            "/api/requisition/production-packaging-label-layout/admin"
        )
        assert initial.status_code == 200, initial.text
        state = initial.json()
        assert state["draft"]["version"] == 0
        assert state["published"]["version"] == 0
        assert state["published"]["layout"] == default_layout()

        invalid = _changed_layout(width_mm=38.4)
        invalid["elements"][0]["x_mm"] = 5
        rejected = client.put(
            "/api/requisition/production-packaging-label-layout/admin/draft",
            json={
                "operation_key": "p1-66b-out-of-paper",
                "expected_draft_version": 0,
                "layout": invalid,
            },
        )
        assert rejected.status_code == 422
        assert "超出40×30" in rejected.text

        qr = _changed_layout(width_mm=36)
        next(item for item in qr["elements"] if item["id"] == "product_qr")[
            "visible"
        ] = True
        qr_rejected = client.put(
            "/api/requisition/production-packaging-label-layout/admin/draft",
            json={
                "operation_key": "p1-66b-qr-source-unavailable",
                "expected_draft_version": 0,
                "layout": qr,
            },
        )
        assert qr_rejected.status_code == 422
        assert "二维码数据源尚未启用" in qr_rejected.text

        layout = _changed_layout(width_mm=36)
        state = _save_draft(client, layout, "p1-66b-draft-1", 0)
        assert state["draft"]["version"] == 1
        assert state["published"]["version"] == 0
        assert state["draft"]["layout"] == layout

        replay = _save_draft(client, layout, "p1-66b-draft-1", 0)
        assert replay["replayed"] is True
        conflict = client.put(
            "/api/requisition/production-packaging-label-layout/admin/draft",
            json={
                "operation_key": "p1-66b-draft-1",
                "expected_draft_version": 0,
                "layout": _changed_layout(width_mm=35),
            },
        )
        assert conflict.status_code == 409

        state = _publish(client, state, "p1-66b-publish-1")
        assert state["published"]["version"] == 1
        assert state["published"]["layout"] == layout
        assert state["draft"]["base_release_version"] == 1

        stale = client.post(
            "/api/requisition/production-packaging-label-layout/admin/publish",
            json={
                "operation_key": "p1-66b-stale-publish",
                "expected_draft_version": 1,
                "expected_release_version": 0,
            },
        )
        assert stale.status_code == 409

        restored = client.post(
            "/api/requisition/production-packaging-label-layout/admin/restore-default",
            json={
                "operation_key": "p1-66b-default-1",
                "expected_draft_version": state["draft"]["version"],
                "expected_release_version": state["published"]["version"],
            },
        )
        assert restored.status_code == 200, restored.text
        state = restored.json()
        assert state["published"]["version"] == 2
        assert state["published"]["layout"] == default_layout()
        assert state["can_rollback"] is True

        rolled_back = client.post(
            "/api/requisition/production-packaging-label-layout/admin/rollback",
            json={
                "operation_key": "p1-66b-rollback-1",
                "expected_draft_version": state["draft"]["version"],
                "expected_release_version": state["published"]["version"],
            },
        )
        assert rolled_back.status_code == 200, rolled_back.text
        state = rolled_back.json()
        assert state["published"]["version"] == 3
        assert state["published"]["layout"] == layout

    with fixture["session_factory"]() as db:
        rows = db.scalar(
            select(func.count()).select_from(
                ProductionPackagingLabelLayoutRevision
            )
        )
        audits = db.scalar(
            select(func.count())
            .select_from(OperationLog)
            .where(
                OperationLog.action_code.like(
                    "production.packaging_label_layout.%"
                )
            )
        )
        assert rows == 7
        assert audits == 4


def test_new_jobs_freeze_released_layout_and_history_does_not_drift(
    production_print_app,
) -> None:
    fixture = production_print_app
    _enable_compact_labels(fixture)
    package_url = (
        "/api/requisition/supplier-orders/"
        f"{fixture['supplier_order_id']}/production-packaging-label-package"
    )
    with TestClient(fixture["app"]) as client:
        _login(client, "p132a2-admin")
        initial = client.get(package_url)
        assert initial.status_code == 200, initial.text
        assert initial.json()["label_layout"]["version"] == 0

        state = _save_draft(client, _changed_layout(width_mm=36), "freeze-d1", 0)
        state = _publish(client, state, "freeze-p1")
        first_package = client.get(package_url)
        assert first_package.status_code == 200, first_package.text
        first_payload = first_package.json()
        assert first_payload["label_layout"]["version"] == 1

        prepared = client.post(
            "/api/requisition/supplier-orders/"
            f"{fixture['supplier_order_id']}/production-packaging-label-jobs",
            json={
                "idempotency_key": "p1-66b-frozen-layout-job",
                "plan_fingerprint": first_payload["plan_fingerprint"],
                "confirmed": True,
            },
        )
        assert prepared.status_code == 200, prepared.text
        job_id = prepared.json()["job_id"]

        state = _save_draft(
            client,
            _changed_layout(width_mm=34),
            "freeze-d2",
            state["draft"]["version"],
        )
        state = _publish(client, state, "freeze-p2")
        assert state["published"]["version"] == 2

        current = client.get(package_url)
        frozen = client.get(
            f"/api/requisition/production-packaging-label-jobs/{job_id}"
        )
        assert current.status_code == 200, current.text
        assert frozen.status_code == 200, frozen.text
        assert current.json()["label_layout"]["version"] == 2
        assert frozen.json()["package"]["label_layout"]["version"] == 1
        assert (
            current.json()["plan_fingerprint"]
            != first_payload["plan_fingerprint"]
        )


def test_layout_audit_failure_rolls_back_the_revision(
    production_print_app,
    monkeypatch,
) -> None:
    from app.api import requisition as requisition_api

    fixture = production_print_app

    def fail_audit(*_args, **_kwargs):
        raise RuntimeError("forced layout audit failure")

    monkeypatch.setattr(requisition_api, "append_audit_event", fail_audit)
    with TestClient(fixture["app"], raise_server_exceptions=False) as client:
        _login(client, "p132a2-admin")
        response = client.put(
            "/api/requisition/production-packaging-label-layout/admin/draft",
            json={
                "operation_key": "p1-66b-audit-rollback",
                "expected_draft_version": 0,
                "layout": _changed_layout(width_mm=36),
            },
        )
        assert response.status_code == 500

    with fixture["session_factory"]() as db:
        assert db.scalar(
            select(func.count()).select_from(
                ProductionPackagingLabelLayoutRevision
            )
        ) == 0


def test_corrupt_published_layout_hash_fails_closed(
    production_print_app,
) -> None:
    fixture = production_print_app
    with fixture["session_factory"]() as db:
        db.add(
            ProductionPackagingLabelLayoutRevision(
                stream="release",
                version=1,
                catalog_version="p1-66b-v1",
                payload_json=json.dumps(default_layout(), ensure_ascii=False),
                payload_hash="e" * 64,
                base_release_version=0,
                operation_kind="publish",
            )
        )
        db.commit()

    with TestClient(fixture["app"]) as client:
        _login(client, "p132a2-admin")
        response = client.get(
            "/api/requisition/production-packaging-label-layout"
        )
        assert response.status_code == 409
        assert "校验失败" in response.text


def test_print_page_exposes_visual_editor_without_arbitrary_qr_or_html() -> None:
    source = PRINT_PAGE.read_text(encoding="utf-8")
    for marker in (
        "编辑标签布局",
        "layoutStage",
        'data-layout-field="x_mm"',
        'data-layout-field="y_mm"',
        'data-layout-field="width_mm"',
        'data-layout-field="font_size_mm"',
        "setPointerCapture",
        "/production-packaging-label-layout/admin/draft",
        "/production-packaging-label-layout/admin/publish",
        "保存草稿",
        "保存并用于下次打印",
        "历史作业不变",
    ):
        assert marker in source
    assert source.count("window.print()") == 1
    assert "二维码数据源尚未启用" in source
    assert "innerHTML:editorLayout" not in source
    assert "contenteditable" not in source.lower()


def test_layout_border_does_not_reduce_the_40mm_by_30mm_editable_canvas() -> None:
    source = PRINT_PAGE.read_text(encoding="utf-8")
    assert ".label-card.layout-driven { position:relative; display:block; overflow:hidden; border:0; }" in source
    assert ".label-card.layout-driven::after" in source
    assert "inset:0;" in source
    assert "border:.3mm solid #000;" in source
    assert "pointer-events:none;" in source
