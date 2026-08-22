from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models.order import OrderItem
from app.models.product import Product
from app.models.production import ProductionTask
from tests.test_p1_32a2_requisition_production_print import (
    _login,
    production_print_app,
)


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
LABEL_PAGE = (ROOT / "static" / "production-packaging-label.html").read_text(
    encoding="utf-8"
)


def _enable_label_tasks(fixture: dict, count: int = 2) -> list[int]:
    with fixture["session_factory"]() as db:
        tasks = list(
            db.scalars(
                select(ProductionTask)
                .join(OrderItem, OrderItem.id == ProductionTask.order_item_id)
                .where(OrderItem.order_id == fixture["sales_order_id"])
                .order_by(ProductionTask.id)
            ).all()
        )
        assert len(tasks) >= count
        first_item = db.get(OrderItem, tasks[0].order_item_id)
        assert first_item is not None
        product = db.get(Product, first_item.product_id)
        assert product is not None
        product.production_label_enabled = True
        product.production_label_units_per_label = 5
        for index, task in enumerate(tasks[:count], start=1):
            task.production_label_enabled_snapshot = True
            task.production_label_units_per_label_snapshot = 5
            task.production_label_total_quantity_snapshot = 20 + index * 5
            task.production_label_count_snapshot = 4 + index
            task.production_label_template_version_snapshot = "current_40x30_v2"
            task.production_label_product_version_snapshot = int(product.version)
        db.commit()
        return [int(task.id) for task in tasks[:count]]


def test_reported_label_action_uses_selected_task_ids_not_purchase_order_all() -> None:
    assert "产品标签按采购单冻结，请勾选该采购单全部" not in INDEX
    assert "production-packaging-label-package?task_ids=" in INDEX
    assert "production-packaging-label.html?id=${encodeURIComponent(group.document_id)}&task_ids=" in INDEX
    assert 'params.get("task_ids")' in LABEL_PAGE


def test_supplier_label_preview_and_job_freeze_only_selected_task(
    production_print_app,
) -> None:
    fixture = production_print_app
    first_task_id, second_task_id = _enable_label_tasks(fixture)
    order_id = fixture["supplier_order_id"]

    with TestClient(fixture["app"]) as client:
        _login(client, "p132a2-admin")
        preview = client.get(
            f"/api/requisition/supplier-orders/{order_id}/production-packaging-label-package",
            params={"task_ids": str(first_task_id)},
        )
        assert preview.status_code == 200, preview.text
        package = preview.json()
        assert [row["production_task_id"] for row in package["plans"]] == [
            first_task_id
        ]
        assert second_task_id not in {
            row["production_task_id"] for row in package["plans"]
        }

        plan = package["plans"][0]
        prepared = client.post(
            f"/api/requisition/supplier-orders/{order_id}/production-packaging-label-jobs",
            json={
                "idempotency_key": "p0-18-one-task-only",
                "plan_fingerprint": package["plan_fingerprint"],
                "confirmed": True,
                "items": [
                    {
                        "production_task_id": first_task_id,
                        "print_label_count": plan["label_count"],
                    }
                ],
            },
        )
        assert prepared.status_code == 200, prepared.text
        frozen = prepared.json()["package"]
        assert [row["production_task_id"] for row in frozen["plans"]] == [
            first_task_id
        ]
        assert frozen["print_summary"]["system_task_count"] == 1


def test_unselected_invalid_task_does_not_block_selected_task_and_mixed_fails_closed(
    production_print_app,
) -> None:
    fixture = production_print_app
    first_task_id, second_task_id = _enable_label_tasks(fixture)
    order_id = fixture["supplier_order_id"]
    with fixture["session_factory"]() as db:
        second = db.get(ProductionTask, second_task_id)
        assert second is not None
        second.production_label_enabled_snapshot = False
        second.production_label_units_per_label_snapshot = None
        second.production_label_total_quantity_snapshot = 0
        second.production_label_count_snapshot = 0
        db.commit()

    with TestClient(fixture["app"]) as client:
        _login(client, "p132a2-admin")
        selected = client.get(
            f"/api/requisition/supplier-orders/{order_id}/production-packaging-label-package",
            params={"task_ids": str(first_task_id)},
        )
        assert selected.status_code == 200, selected.text

        mixed = client.get(
            f"/api/requisition/supplier-orders/{order_id}/production-packaging-label-package",
            params={"task_ids": f"{first_task_id},{second_task_id}"},
        )
        assert mixed.status_code == 409, mixed.text
        detail = mixed.json()["detail"]
        assert detail["code"] == "production_label_review_required"
        assert any(
            f"生产任务 #{second_task_id}" in reason and "未启用" in reason
            for reason in detail["reasons"]
        )

        outside = client.get(
            f"/api/requisition/supplier-orders/{order_id}/production-packaging-label-package",
            params={"task_ids": "999999999"},
        )
        assert outside.status_code == 409, outside.text
        assert "不属于当前报料单" in outside.text
