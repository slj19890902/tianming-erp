from __future__ import annotations

import json
from datetime import date
import re
import shutil
import subprocess

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select

from tests.test_p1_32a2_requisition_production_print import (
    _login,
    production_print_app,
)
from tests.test_p1_50c_task_label_plan_template import _production_router_app


def _pending_task(fixture: dict) -> tuple[int, int, int]:
    from app.models.order import OrderItem
    from app.models.product import Product
    from app.models.production import ProductionTask

    with fixture["session_factory"]() as db:
        task = db.scalar(
            select(ProductionTask).where(
                ProductionTask.order_item_id == fixture["order_item_id"]
            )
        )
        item = db.get(OrderItem, fixture["order_item_id"])
        product = db.get(Product, fixture["product_id"])
        assert task is not None and item is not None and product is not None
        task.status = "pending"
        task.planned_quantity = int(item.quantity)
        task.material_received_quantity = int(item.quantity)
        task.material_input_quantity = int(item.quantity)
        product.box_style = "A1"
        product.box_category = "normal"
        product.print_content = "双色印刷"
        product.printing_colors = "黑,绿"
        product.production_process = "打钉"
        product.version = 9
        db.commit()
        return int(task.id), int(task.version), int(product.version)


def _business_counts(fixture: dict) -> dict[str, int]:
    from app.models.delivery import DeliveryItem
    from app.models.order import Order, OrderItem
    from app.models.production import ProductionCompletion, ProductionTask
    from app.models.warehouse_inventory import InventoryLot

    with fixture["session_factory"]() as db:
        return {
            "orders": int(db.scalar(select(func.count()).select_from(Order)) or 0),
            "items": int(db.scalar(select(func.count()).select_from(OrderItem)) or 0),
            "tasks": int(db.scalar(select(func.count()).select_from(ProductionTask)) or 0),
            "completions": int(
                db.scalar(select(func.count()).select_from(ProductionCompletion)) or 0
            ),
            "inventory_lots": int(
                db.scalar(select(func.count()).select_from(InventoryLot)) or 0
            ),
            "delivery_items": int(
                db.scalar(select(func.count()).select_from(DeliveryItem)) or 0
            ),
        }


def test_admin_previews_and_refreshes_one_pending_task_without_quantity_writes(
    production_print_app,
) -> None:
    fixture = _production_router_app(production_print_app)
    task_id, task_version, product_version = _pending_task(fixture)
    before = _business_counts(fixture)

    with TestClient(fixture["app"]) as client:
        _login(client, "p132a2-admin")
        preview = client.get(
            f"/api/production/tasks/{task_id}/profile-refresh-preview"
        )
        assert preview.status_code == 200, preview.text
        data = preview.json()
        assert data["eligible"] is True
        assert data["task_id"] == task_id
        assert data["expected_task_version"] == task_version
        assert data["expected_source_version"] == product_version
        assert data["after"]["box_style"] == "A1"
        assert data["after"]["needs_die_cut"] is False
        assert data["after"]["production_process"] == "打钉"
        assert data["after"]["printing_colors"] == ["黑", "绿"]
        assert {row["field"] for row in data["changes"]} >= {
            "box_style",
            "production_process",
            "printing_colors",
        }

        saved = client.post(
            f"/api/production/tasks/{task_id}/profile-refresh",
            json={
                "idempotency_key": "p1-92-refresh-black-green",
                "expected_task_version": task_version,
                "expected_source_version": product_version,
                "preview_fingerprint": data["preview_fingerprint"],
                "confirmed": True,
            },
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["after"] == data["after"]
        assert saved.json()["replayed"] is False

    after = _business_counts(fixture)
    assert after == before
    with fixture["session_factory"]() as db:
        from app.models.production import ProductionTask
        from app.models.production_profile_refresh import ProductionTaskProfileRefresh

        task = db.get(ProductionTask, task_id)
        receipt = db.scalar(
            select(ProductionTaskProfileRefresh).where(
                ProductionTaskProfileRefresh.task_id == task_id
            )
        )
        assert task is not None and receipt is not None
        assert int(task.version) == task_version + 1
        assert task.production_box_style_snapshot == "A1"
        assert task.production_needs_die_cut_snapshot is False
        assert task.production_process_snapshot == "打钉"
        assert json.loads(task.printing_colors_snapshot) == ["黑", "绿"]
        assert receipt.operator_id is not None
        assert json.loads(receipt.before_snapshot_json) == data["before"]
        assert json.loads(receipt.after_snapshot_json) == data["after"]


def test_refresh_rejects_stale_source_version_and_non_admin(
    production_print_app,
) -> None:
    fixture = _production_router_app(production_print_app)
    task_id, task_version, product_version = _pending_task(fixture)

    with TestClient(fixture["app"]) as client:
        _login(client, "p132a2-admin")
        preview = client.get(
            f"/api/production/tasks/{task_id}/profile-refresh-preview"
        )
        assert preview.status_code == 200, preview.text
        stale = client.post(
            f"/api/production/tasks/{task_id}/profile-refresh",
            json={
                "idempotency_key": "p1-92-stale-source",
                "expected_task_version": task_version,
                "expected_source_version": product_version + 1,
                "preview_fingerprint": preview.json()["preview_fingerprint"],
                "confirmed": True,
            },
        )
        assert stale.status_code == 409

        client.cookies.clear()
        _login(client, "p132a2-sales")
        denied = client.get(
            f"/api/production/tasks/{task_id}/profile-refresh-preview"
        )
        assert denied.status_code == 403


def test_refresh_is_idempotent_and_rejects_reused_key_with_different_request(
    production_print_app,
) -> None:
    fixture = _production_router_app(production_print_app)
    task_id, task_version, product_version = _pending_task(fixture)

    with TestClient(fixture["app"]) as client:
        _login(client, "p132a2-admin")
        preview = client.get(
            f"/api/production/tasks/{task_id}/profile-refresh-preview"
        ).json()
        payload = {
            "idempotency_key": "p1-92-idempotent-refresh",
            "expected_task_version": task_version,
            "expected_source_version": product_version,
            "preview_fingerprint": preview["preview_fingerprint"],
            "confirmed": True,
        }
        first = client.post(
            f"/api/production/tasks/{task_id}/profile-refresh", json=payload
        )
        replay = client.post(
            f"/api/production/tasks/{task_id}/profile-refresh", json=payload
        )
        assert first.status_code == replay.status_code == 200
        assert first.json()["replayed"] is False
        assert replay.json()["replayed"] is True
        conflicting = client.post(
            f"/api/production/tasks/{task_id}/profile-refresh",
            json={**payload, "expected_source_version": product_version + 1},
        )
        assert conflicting.status_code == 409

    with fixture["session_factory"]() as db:
        from app.models.production_profile_refresh import ProductionTaskProfileRefresh

        assert (
            db.scalar(
                select(func.count()).select_from(ProductionTaskProfileRefresh)
            )
            == 1
        )


def test_frozen_profile_controls_station_route_until_explicit_refresh(
    production_print_app,
) -> None:
    from app.api.mobile_erp import router as mobile_erp_router
    from app.models.product import Product
    from app.models.production import ProductionTask
    from app.services.production_station_routing import production_station_memberships
    from app.services.production_workflow import list_production_station_task_ids

    fixture = _production_router_app(production_print_app)
    task_id, task_version, product_version = _pending_task(fixture)
    with TestClient(fixture["app"]) as client:
        _login(client, "p132a2-admin")
        preview = client.get(
            f"/api/production/tasks/{task_id}/profile-refresh-preview"
        ).json()
        saved = client.post(
            f"/api/production/tasks/{task_id}/profile-refresh",
            json={
                "idempotency_key": "p1-92-freeze-route",
                "expected_task_version": task_version,
                "expected_source_version": product_version,
                "preview_fingerprint": preview["preview_fingerprint"],
                "confirmed": True,
            },
        )
        assert saved.status_code == 200, saved.text

    with fixture["session_factory"]() as db:
        task = db.get(ProductionTask, task_id)
        product = db.get(Product, fixture["product_id"])
        assert task is not None and product is not None
        printing_ids, _, _ = list_production_station_task_ids(
            db,
            allowed_customer_ids=None,
            station="printing",
            page=1,
            page_size=100,
        )
        die_ids, _, _ = list_production_station_task_ids(
            db,
            allowed_customer_ids=None,
            station="die_cut",
            page=1,
            page_size=100,
        )
        assert task_id in printing_ids
        assert task_id not in die_ids

        # Live master-data edits must not silently reroute an existing task.
        product.box_style = "衬板"
        product.box_category = "die_cut"
        product.print_content = "无印刷"
        product.printing_colors = None
        product.version += 1
        db.commit()
        printing_ids, _, _ = list_production_station_task_ids(
            db,
            allowed_customer_ids=None,
            station="printing",
            page=1,
            page_size=100,
        )
        die_ids, _, _ = list_production_station_task_ids(
            db,
            allowed_customer_ids=None,
            station="die_cut",
            page=1,
            page_size=100,
        )
        assert task_id in printing_ids
        assert task_id not in die_ids

    if not any(
        getattr(route, "path", "") == "/api/mobile/erp/production/tasks"
        for route in fixture["app"].routes
    ):
        fixture["app"].include_router(mobile_erp_router, prefix="/api/mobile/erp")
    with TestClient(fixture["app"]) as client:
        _login(client, "p132a2-admin")
        desktop = client.get(
            "/api/production/tasks",
            params={"status": "pending", "page": 1, "page_size": 200},
        )
        mobile = client.get(
            "/api/mobile/erp/production/tasks",
            params={"station": "printing", "page": 1, "page_size": 20},
        )
        mobile_die = client.get(
            "/api/mobile/erp/production/tasks",
            params={"station": "die_cut", "page": 1, "page_size": 20},
        )
        scanner = client.get(
            "/api/mobile/erp/production/pending/lookup",
            params={"q": "P132A2-PARENT"},
        )
        assert (
            desktop.status_code
            == mobile.status_code
            == mobile_die.status_code
            == scanner.status_code
            == 200
        )
        desktop_printing_ids = {
            int(row["id"])
            for row in desktop.json()["items"]
            if "printing"
            in production_station_memberships(
                print_content_snapshot=row.get("print_content"),
                box_style=row.get("box_style"),
                die_cut_required=bool(row.get("needs_die_cut")),
            )
        }
        mobile_ids = {int(row["task_id"]) for row in mobile.json()["items"]}
        mobile_die_ids = {
            int(row["task_id"]) for row in mobile_die.json()["items"]
        }
        scanner_ids = {int(row["task_id"]) for row in scanner.json()["items"]}
        assert desktop_printing_ids == mobile_ids == {task_id}
        assert task_id not in mobile_die_ids
        assert scanner_ids == {task_id}
        desktop_task = next(
            row for row in desktop.json()["items"] if row["id"] == task_id
        )
        assert desktop_task["needs_die_cut"] is False
        for payload in (
            desktop_task,
            mobile.json()["items"][0],
            scanner.json()["items"][0],
        ):
            assert payload["printing_colors"] == ["黑", "绿"]


@pytest.mark.parametrize("box_style", ["模切内盒", "异形箱"])
def test_new_die_cut_task_profile_freezes_explicit_route_fact(
    production_print_app,
    box_style: str,
) -> None:
    from app.models.order import OrderItem
    from app.models.product import Product
    from app.services.production_station_routing import production_station_memberships
    from app.services.production_task_profile import new_task_profile_snapshot

    fixture = _production_router_app(production_print_app)
    with fixture["session_factory"]() as db:
        item = db.get(OrderItem, fixture["order_item_id"])
        product = db.get(Product, fixture["product_id"])
        assert item is not None and product is not None
        product.box_style = box_style
        product.box_category = "die_cut"
        product.print_content = "无印刷"
        snapshot = new_task_profile_snapshot(db, product, item=item)
        assert snapshot["production_box_style_snapshot"] == box_style
        assert snapshot["production_needs_die_cut_snapshot"] is True
        assert production_station_memberships(
            print_content_snapshot=product.print_content,
            box_style=snapshot["production_box_style_snapshot"],
            die_cut_required=snapshot["production_needs_die_cut_snapshot"],
        ) == {"die_cut"}


def _add_completion_fact(fixture: dict, task_id: int) -> None:
    from app.core.time_contract import utc_now_naive
    from app.models.production import (
        ProductionCompletion,
        ProductionCompletionBatch,
        ProductionTask,
    )

    with fixture["session_factory"]() as db:
        task = db.get(ProductionTask, task_id)
        assert task is not None
        batch = ProductionCompletionBatch(
            idempotency_key=f"p1-92-completion-{task_id}",
            request_hash="a" * 64,
            item_count=1,
            completed_by=1,
            completed_at=utc_now_naive(),
        )
        db.add(batch)
        db.flush()
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


def _add_delivery_fact(fixture: dict) -> None:
    from app.models.delivery import Delivery, DeliveryItem
    from app.models.order import Order, OrderItem

    with fixture["session_factory"]() as db:
        item = db.get(OrderItem, fixture["order_item_id"])
        assert item is not None
        order = db.get(Order, item.order_id)
        assert order is not None
        delivery = Delivery(
            delivery_number="P1-92-DOWNSTREAM",
            customer_id=order.customer_id,
            delivery_date=date.today(),
            source_mode="order",
            status="pending",
            total_quantity=1,
        )
        db.add(delivery)
        db.flush()
        db.add(
            DeliveryItem(
                delivery_id=delivery.id,
                source_type="order",
                order_item_id=fixture["order_item_id"],
                delivered_quantity=1,
            )
        )
        db.commit()


def _add_printed_label_fact(fixture: dict, task_id: int) -> None:
    from app.core.time_contract import utc_now_naive
    from app.models.production import ProductionTask
    from app.models.production_label_print import (
        ProductionPackagingLabelPrintJob,
        ProductionPackagingLabelPrintJobTask,
    )

    with fixture["session_factory"]() as db:
        task = db.get(ProductionTask, task_id)
        assert task is not None
        job = ProductionPackagingLabelPrintJob(
            supplier_order_id=fixture["supplier_order_id"],
            idempotency_key=f"p1-92-printed-{task_id}",
            request_hash="b" * 64,
            operator_id=1,
            template_version="current_40x30_v2",
            plan_fingerprint="c" * 64,
            payload_json="{}",
            payload_hash="d" * 64,
            status="printed",
            printed_confirmation_key=f"p1-92-confirm-{task_id}",
            printed_by_user_id=1,
            printed_at=utc_now_naive(),
        )
        db.add(job)
        db.flush()
        db.add(
            ProductionPackagingLabelPrintJobTask(
                print_job_id=job.id,
                production_task_id=task.id,
                production_task_version=task.version,
                product_id=fixture["product_id"],
                product_version=9,
                template_version="current_40x30_v2",
                snapshot_json="{}",
            )
        )
        db.commit()


def test_refresh_preview_fails_closed_after_completion_delivery_or_print(
    production_print_app,
) -> None:
    fixture = _production_router_app(production_print_app)
    task_id, _, _ = _pending_task(fixture)

    for add_fact, expected in (
        (_add_completion_fact, "完工或撤销历史"),
        (_add_delivery_fact, "送货单事实"),
        (_add_printed_label_fact, "生产任务单或产品标签"),
    ):
        # Each fact is cumulative; every later preview must still fail closed.
        if add_fact in {_add_completion_fact, _add_printed_label_fact}:
            add_fact(fixture, task_id)
        else:
            add_fact(fixture)
        with TestClient(fixture["app"]) as client:
            _login(client, "p132a2-admin")
            preview = client.get(
                f"/api/production/tasks/{task_id}/profile-refresh-preview"
            )
            assert preview.status_code == 200, preview.text
            assert preview.json()["eligible"] is False
            assert any(expected in row for row in preview.json()["block_reasons"])


def test_profile_refresh_frontend_is_admin_single_task_preview_confirm() -> None:
    from pathlib import Path

    html = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    for marker in (
        "旧任务资料待核对",
        "核对 / 刷新资料",
        "任务当前冻结值",
        "刷新后值",
        "确认刷新这一款",
        "/profile-refresh-preview",
        "/profile-refresh",
    ):
        assert marker in html
    assert 'v-if="canAdmin"' in html
    assert "确认批量刷新" not in html


def test_profile_refresh_frontend_javascript_is_syntax_valid(tmp_path) -> None:
    from pathlib import Path

    node = shutil.which("node")
    assert node is not None
    html = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    scripts = [
        script
        for script in re.findall(
            r"<script(?:\s[^>]*)?>(.*?)</script>", html, re.DOTALL
        )
        if script.strip()
    ]
    assert len(scripts) == 1
    target = tmp_path / "p1-92-index.js"
    target.write_text(scripts[0], encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr
