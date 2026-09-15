from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.requisition import ProductionPackagingLabelJobRequest
from app.core.database import create_sqlite_engine
from app.models import Base
from app.models.user import User
from app.services.production_label_operations import (
    ProductionLabelOperationError,
    prepare_composite_packaging_label_job,
)
from app.services.production_packaging_label import (
    ProductionPackagingLabelError,
    apply_packaging_label_print_counts,
    build_composite_requisition_packaging_label_package,
    build_supplier_requisition_packaging_label_package,
    combine_supplier_requisition_packaging_label_packages,
)
from tests.test_p1_32a2_requisition_production_print import (
    _login,
    production_print_app,
)
from tests.test_p1_50c_task_label_plan_template import _enable_one_frozen_plan
from tests.test_p1_79_composite_fulfillment_mode import _seed_label_facts


ROOT = Path(__file__).resolve().parents[1]
PAGE = (ROOT / "static" / "production-packaging-label.html").read_text(
    encoding="utf-8"
)


def _package() -> dict:
    first_quantities = [5, 5, 5, 5, 3]
    second_quantities = [10, 10, 10]
    plans = [
        {
            "production_task_id": 101,
            "production_task_version": 4,
            "product_id": 11,
            "product_version": 8,
            "template_version": "current_40x30_v2",
            "total_quantity": 23,
            "units_per_label": 5,
            "label_count": 5,
        },
        {
            "production_task_id": 202,
            "production_task_version": 7,
            "product_id": 22,
            "product_version": 9,
            "template_version": "current_40x30_v2",
            "total_quantity": 30,
            "units_per_label": 10,
            "label_count": 3,
        },
    ]
    labels = []
    for task_id, quantities in ((101, first_quantities), (202, second_quantities)):
        for number, quantity in enumerate(quantities, start=1):
            labels.append(
                {
                    "production_task_id": task_id,
                    "label_number": number,
                    "label_count": len(quantities),
                    "quantity": quantity,
                    "units_per_label": plans[0 if task_id == 101 else 1][
                        "units_per_label"
                    ],
                }
            )
    return {
        "template_version": "current_40x30_v2",
        "plan_fingerprint": "f" * 64,
        "production_task_count": 2,
        "label_count": 8,
        "plans": plans,
        "labels": labels,
        "printable": True,
        "review_required": False,
    }


def test_reduced_count_prints_only_full_bundle_labels_and_zero_skips_task() -> None:
    frozen = apply_packaging_label_print_counts(_package(), {101: 4, 202: 0})

    assert frozen["production_task_count"] == 1
    assert frozen["label_count"] == 4
    assert frozen["system_label_count"] == 8
    assert [row["quantity"] for row in frozen["labels"]] == [5, 5, 5, 5]
    assert frozen["plans"][0]["print_label_count"] == 4
    assert frozen["plans"][0]["system_label_count"] == 5
    assert frozen["print_summary"] == {
        "printed_task_count": 1,
        "print_label_count": 4,
        "system_task_count": 2,
        "system_label_count": 8,
    }


def test_full_count_keeps_remainder_and_is_frozen_for_history() -> None:
    frozen = apply_packaging_label_print_counts(_package(), {101: 5, 202: 3})

    assert [row["quantity"] for row in frozen["labels"][:5]] == [5, 5, 5, 5, 3]
    assert frozen["label_count"] == 8
    assert frozen["print_selection"] == [
        {"production_task_id": 101, "print_label_count": 5, "system_label_count": 5},
        {"production_task_id": 202, "print_label_count": 3, "system_label_count": 3},
    ]


def test_composite_job_tasks_follow_the_selected_product_plans() -> None:
    package = _package()
    package["job_tasks"] = [
        {"production_task_id": 101},
        {"production_task_id": 202},
    ]
    frozen = apply_packaging_label_print_counts(package, {101: 0, 202: 2})

    assert frozen["production_task_count"] == 1
    assert frozen["job_tasks"] == [{"production_task_id": 202}]
    assert [row["production_task_id"] for row in frozen["labels"]] == [202, 202]


@pytest.mark.parametrize(
    ("counts", "message"),
    [
        ({101: 0, 202: 0}, "本次未选择"),
        ({101: 6, 202: 0}, "0～5"),
        ({101: -1, 202: 0}, "0～5"),
        ({101: 1}, "任务清单"),
        ({101: 1.5, 202: 0}, "必须为整数"),
        ({101: True, 202: 0}, "必须为整数"),
    ],
)
def test_invalid_or_empty_print_count_selection_fails_closed(
    counts: dict,
    message: str,
) -> None:
    with pytest.raises(ProductionPackagingLabelError, match=message):
        apply_packaging_label_print_counts(_package(), counts)


def test_request_contract_is_unique_and_rejects_boolean_counts() -> None:
    accepted = ProductionPackagingLabelJobRequest.model_validate(
        {
            "idempotency_key": "p183b-one",
            "plan_fingerprint": "a" * 64,
            "confirmed": True,
            "items": [
                {"production_task_id": 101, "print_label_count": 4},
                {"production_task_id": 202, "print_label_count": 0},
            ],
        }
    )
    assert [row.print_label_count for row in accepted.items or []] == [4, 0]

    with pytest.raises(ValidationError, match="不能重复"):
        ProductionPackagingLabelJobRequest.model_validate(
            {
                "idempotency_key": "p183b-duplicate",
                "plan_fingerprint": "b" * 64,
                "confirmed": True,
                "items": [
                    {"production_task_id": 101, "print_label_count": 1},
                    {"production_task_id": 101, "print_label_count": 1},
                ],
            }
        )
    with pytest.raises(ValidationError, match="必须为整数"):
        ProductionPackagingLabelJobRequest.model_validate(
            {
                "idempotency_key": "p183b-bool",
                "plan_fingerprint": "c" * 64,
                "confirmed": True,
                "items": [{"production_task_id": 101, "print_label_count": True}],
            }
        )


def test_print_page_exposes_per_task_counts_shortcuts_and_frozen_summary() -> None:
    for marker in (
        'id="printPlanPanel"',
        'id="allPlannedButton"',
        'id="allZeroButton"',
        'class="print-count-input"',
        "function parsePrintCount(value, maximum)",
        "function selectedPrintPackage()",
        "print_label_count:parsePrintCount",
        "本次未选择需要打印的标签",
        "补打数量不会自动扩大",
        'method:"POST"',
        "plan_fingerprint:fingerprint",
        "items,",
        'params.get("ids")',
        "/api/requisition/supplier-order-label-batches/package?order_ids=",
        "/api/requisition/supplier-order-label-batches/production-packaging-label-jobs",
        "/api/requisition/supplier-order-label-batches/confirm",
        "job_ids:jobIds",
    ):
        assert marker in PAGE

    assert '.print-plan-panel,.layout-editor { display:none !important; }' in PAGE


def test_api_freezes_reduced_count_replays_exactly_and_never_changes_task(
    production_print_app,
) -> None:
    from app.models.production import ProductionTask
    from app.models.production_label_print import ProductionPackagingLabelPrintJob

    fixture = production_print_app
    task_id, _task_version, _product_version = _enable_one_frozen_plan(fixture)
    order_id = fixture["supplier_order_id"]
    with fixture["session_factory"]() as db:
        before = db.get(ProductionTask, task_id)
        before_snapshot = (
            before.version,
            before.production_label_total_quantity_snapshot,
            before.production_label_units_per_label_snapshot,
            before.production_label_count_snapshot,
        )
    with TestClient(fixture["app"]) as client:
        _login(client, "p132a2-admin")
        preview = client.get(
            f"/api/requisition/supplier-orders/{order_id}/production-packaging-label-package",
            params={"item_ids": str(fixture["supplier_item_id"])},
        )
        assert preview.status_code == 200, preview.text
        plan = preview.json()["plans"][0]
        payload = {
            "idempotency_key": "p1-83b-reduced-five-to-four",
            "plan_fingerprint": preview.json()["plan_fingerprint"],
            "confirmed": True,
            "items": [
                {
                    "production_task_id": task_id,
                    "print_label_count": 4,
                }
            ],
        }
        prepared = client.post(
            f"/api/requisition/supplier-orders/{order_id}/production-packaging-label-jobs",
            json=payload,
        )
        assert prepared.status_code == 200, prepared.text
        frozen = prepared.json()["package"]
        assert frozen["label_count"] == 4
        assert frozen["system_label_count"] == plan["label_count"]
        assert plan["label_count"] > 4
        assert [label["quantity"] for label in frozen["labels"]] == [5, 5, 5, 5]
        assert frozen["print_summary"]["print_label_count"] == 4
        assert frozen["print_summary"]["system_label_count"] == plan["label_count"]

        replay = client.post(
            f"/api/requisition/supplier-orders/{order_id}/production-packaging-label-jobs",
            json=payload,
        )
        assert replay.status_code == 200
        assert replay.json() == prepared.json()

        changed = client.post(
            f"/api/requisition/supplier-orders/{order_id}/production-packaging-label-jobs",
            json={
                **payload,
                "items": [{"production_task_id": task_id, "print_label_count": 3}],
            },
        )
        assert changed.status_code == 409

        all_zero = client.post(
            f"/api/requisition/supplier-orders/{order_id}/production-packaging-label-jobs",
            json={
                **payload,
                "idempotency_key": "p1-83b-all-zero",
                "items": [{"production_task_id": task_id, "print_label_count": 0}],
            },
        )
        assert all_zero.status_code == 409
        assert "本次未选择" in all_zero.text

    with fixture["session_factory"]() as db:
        after = db.get(ProductionTask, task_id)
        assert (
            after.version,
            after.production_label_total_quantity_snapshot,
            after.production_label_units_per_label_snapshot,
            after.production_label_count_snapshot,
        ) == before_snapshot
        assert db.scalar(select(func.count(ProductionPackagingLabelPrintJob.id))) == 1


def test_supplier_label_preview_names_disabled_product_instead_of_silently_skipping(
    production_print_app,
) -> None:
    from app.models.order import OrderItem
    from app.models.product import Product
    from app.models.production import ProductionTask

    fixture = production_print_app
    task_id, _task_version, _product_version = _enable_one_frozen_plan(fixture)
    with fixture["session_factory"]() as db:
        task = db.get(ProductionTask, task_id)
        assert task is not None
        item = db.get(OrderItem, task.order_item_id)
        assert item is not None
        product = db.get(Product, item.product_id)
        assert product is not None
        product.production_label_enabled = False
        product.production_label_units_per_label = None
        db.commit()

    with TestClient(fixture["app"]) as client:
        _login(client, "p132a2-admin")
        response = client.get(
            "/api/requisition/supplier-order-label-batches/package"
            f"?order_ids={fixture['supplier_order_id']}"
        )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "production_label_no_eligible_items"
    assert detail["excluded_items"][0]["resolution"].startswith("到常用箱")
    assert any("P132A2" in reason for reason in detail["reasons"]), detail
    assert any("常用箱未启用打印标签" in reason for reason in detail["reasons"])


def test_supplier_label_package_keeps_enabled_items_and_reports_disabled_items(
    production_print_app,
) -> None:
    from app.models.order import OrderItem
    from app.models.product import Product
    from app.models.supplier_requisition_order import SupplierRequisitionOrder

    fixture = production_print_app
    with fixture["session_factory"]() as db:
        order = db.get(SupplierRequisitionOrder, fixture["supplier_order_id"])
        assert order is not None
        supplier_items = sorted(order.items, key=lambda row: int(row.id))
        order_items = {
            int(item.order_item_id): db.get(OrderItem, int(item.order_item_id))
            for item in supplier_items
        }
        original = db.get(Product, fixture["product_id"])
        assert original is not None
        original.production_label_enabled = True
        original.production_label_units_per_label = 5
        disabled = Product(
            customer_id=original.customer_id,
            product_code="P038-LABEL-DISABLED",
            customer_material_code="P038-LABEL-DISABLED",
            product_name="未启用标签测试箱",
            material_id=original.material_id,
            production_label_enabled=False,
        )
        db.add(disabled)
        db.flush()
        disabled_order_item = order_items[sorted(order_items)[-1]]
        assert disabled_order_item is not None
        disabled_order_item.product_id = int(disabled.id)
        for supplier_item in supplier_items:
            if int(supplier_item.order_item_id) == int(disabled_order_item.id):
                supplier_item.product_id = int(disabled.id)
                supplier_item.product_code = disabled.product_code
                supplier_item.product_name = disabled.product_name
        selected_ids = {int(item.id) for item in supplier_items}
        db.commit()

        package = build_supplier_requisition_packaging_label_package(
            db,
            order,
            selected_supplier_item_ids=selected_ids,
        )
        combined = combine_supplier_requisition_packaging_label_packages([package])

    assert package["printable"] is True
    assert package["review_required"] is False
    assert package["label_count"] > 0
    assert len(package["excluded_items"]) == 1
    assert package["excluded_items"][0]["product_code"] == "P038-LABEL-DISABLED"
    assert "常用箱未启用打印标签" in package["excluded_items"][0]["reason"]
    assert combined["printable"] is True
    assert combined["review_required"] is False
    assert combined["label_count"] == package["label_count"]
    assert len(combined["excluded_items"]) == 1


def test_supplier_label_package_all_disabled_has_only_explicit_exclusions(
    production_print_app,
) -> None:
    from app.models.product import Product
    from app.models.supplier_requisition_order import SupplierRequisitionOrder

    fixture = production_print_app
    with fixture["session_factory"]() as db:
        product = db.get(Product, fixture["product_id"])
        assert product is not None
        product.production_label_enabled = False
        product.production_label_units_per_label = None
        order = db.get(SupplierRequisitionOrder, fixture["supplier_order_id"])
        assert order is not None
        selected_ids = {int(item.id) for item in order.items}
        db.commit()

        package = build_supplier_requisition_packaging_label_package(
            db,
            order,
            selected_supplier_item_ids=selected_ids,
        )
        combined = combine_supplier_requisition_packaging_label_packages([package])

    assert package["printable"] is False
    assert package["review_required"] is False
    assert package["labels"] == []
    assert package["excluded_items"]
    assert all(
        "常用箱未启用打印标签" in item["reason"]
        for item in package["excluded_items"]
    )
    assert combined["printable"] is False
    assert combined["review_required"] is False
    assert combined["review_messages"] == []
    assert combined["excluded_items"]


def test_explicit_supplier_item_without_production_task_stops_single_and_batch(
    production_print_app,
) -> None:
    """A broken task chain is a hard error, never a label-policy exclusion."""

    from app.models.product import Product
    from app.models.production import ProductionTask
    from app.models.supplier_requisition_order import SupplierRequisitionOrder

    fixture = production_print_app
    with fixture["session_factory"]() as db:
        order = db.get(SupplierRequisitionOrder, fixture["supplier_order_id"])
        product = db.get(Product, fixture["product_id"])
        assert order is not None and product is not None
        product.production_label_enabled = True
        product.production_label_units_per_label = 5
        supplier_items = sorted(order.items, key=lambda row: int(row.id))
        missing_item = supplier_items[0]
        valid_item = next(
            row
            for row in supplier_items
            if int(row.order_item_id) != int(missing_item.order_item_id)
        )
        missing_task = db.scalar(
            select(ProductionTask).where(
                ProductionTask.order_item_id == missing_item.order_item_id
            )
        )
        assert missing_task is not None
        db.delete(missing_task)
        db.commit()
        missing_item_id = int(missing_item.id)
        valid_item_id = int(valid_item.id)

        with pytest.raises(ProductionPackagingLabelError, match="缺少有效生产任务"):
            build_supplier_requisition_packaging_label_package(
                db,
                order,
                selected_supplier_item_ids={missing_item_id},
            )
        with pytest.raises(ProductionPackagingLabelError, match="缺少有效生产任务"):
            build_supplier_requisition_packaging_label_package(
                db,
                order,
                selected_supplier_item_ids={missing_item_id, valid_item_id},
            )

    with TestClient(fixture["app"]) as client:
        _login(client, "p132a2-admin")
        single = client.get(
            f"/api/requisition/supplier-orders/{fixture['supplier_order_id']}/"
            "production-packaging-label-package",
            params={"item_ids": str(missing_item_id)},
        )
        mixed_batch = client.get(
            "/api/requisition/supplier-order-label-batches/package",
            params={
                "order_ids": str(fixture["supplier_order_id"]),
                "item_ids": f"{valid_item_id},{missing_item_id}",
            },
        )

    assert single.status_code == mixed_batch.status_code == 409
    assert "缺少有效生产任务" in single.text
    assert "缺少有效生产任务" in mixed_batch.text


def test_composite_label_package_excludes_disabled_component_without_job_task(
    tmp_path: Path,
) -> None:
    from app.models.product import Product

    engine = create_sqlite_engine(tmp_path / "p0-38-composite-exclusion.sqlite3")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            requisition, _order_item, items = _seed_label_facts(db)
            children = list(
                db.scalars(
                    select(Product)
                    .where(Product.is_internal_component.is_(True))
                    .order_by(Product.id)
                )
            )
            assert len(children) == 2
            children[1].production_label_enabled = False
            children[1].production_label_units_per_label = None
            db.commit()

            package = build_composite_requisition_packaging_label_package(
                db,
                requisition,
                selected_item_ids={int(item.id) for item in items},
            )

        assert package["printable"] is True
        assert package["review_required"] is False
        assert len(package["plans"]) == 1
        assert len(package["job_tasks"]) == 1
        assert len(package["excluded_items"]) == 1
        assert package["excluded_items"][0]["product_name"] == "组合子件乙"
        assert "未启用打印标签" in package["excluded_items"][0]["reason"]
        assert int(package["job_tasks"][0]["product_id"]) == int(children[0].id)
    finally:
        engine.dispose()


def test_current_common_box_label_setting_prints_selected_item_without_task_refresh(
    production_print_app,
) -> None:
    from app.models.order import OrderItem
    from app.models.product import Product
    from app.models.production import ProductionTask
    from app.models.production_label_print import ProductionLabelPlanRefresh

    fixture = production_print_app
    with fixture["session_factory"]() as db:
        task = db.scalar(
            select(ProductionTask)
            .join(OrderItem, OrderItem.id == ProductionTask.order_item_id)
            .where(OrderItem.id == fixture["order_item_id"])
        )
        assert task is not None
        product = db.get(Product, fixture["product_id"])
        assert product is not None
        product.production_label_enabled = True
        product.production_label_units_per_label = 7
        product.version = int(product.version) + 1
        task.production_label_enabled_snapshot = False
        task.production_label_units_per_label_snapshot = None
        task.production_label_total_quantity_snapshot = 0
        task.production_label_count_snapshot = 0
        db.commit()
        task_id = int(task.id)

    with TestClient(fixture["app"]) as client:
        _login(client, "p132a2-admin")
        preview = client.get(
            f"/api/requisition/supplier-orders/{fixture['supplier_order_id']}"
            "/production-packaging-label-package",
            params={"item_ids": str(fixture["supplier_item_id"])},
        )
        assert preview.status_code == 200, preview.text
        package = preview.json()
        assert [row["production_task_id"] for row in package["plans"]] == [task_id]
        assert package["plans"][0]["label_policy_source"] == "product_master_current"
        assert package["plans"][0]["units_per_label"] == 7
        assert package["plans"][0]["template_version"] == "current_40x30_v2"

        prepared = client.post(
            f"/api/requisition/supplier-orders/{fixture['supplier_order_id']}"
            "/production-packaging-label-jobs",
            json={
                "idempotency_key": "p1-live-common-box-selected-item",
                "plan_fingerprint": package["plan_fingerprint"],
                "confirmed": True,
                "items": [
                    {
                        "production_task_id": task_id,
                        "print_label_count": 1,
                    }
                ],
            },
        )
        assert prepared.status_code == 200, prepared.text
        assert prepared.json()["package"]["label_count"] == 1

    with fixture["session_factory"]() as db:
        task = db.get(ProductionTask, task_id)
        assert task is not None
        assert task.production_label_enabled_snapshot is False
        assert task.production_label_total_quantity_snapshot == 0
        assert db.scalar(select(func.count(ProductionLabelPlanRefresh.id))) == 0


def test_completed_received_task_keeps_printable_recorded_label_quantity(
    production_print_app,
) -> None:
    from app.models.production import ProductionTask
    from app.models.product import Product
    from app.models.supplier_requisition_order import SupplierRequisitionOrder

    fixture = production_print_app
    with fixture["session_factory"]() as db:
        task = db.scalar(select(ProductionTask).where(
            ProductionTask.order_item_id == fixture["order_item_id"]
        ))
        product = db.get(Product, fixture["product_id"])
        order = db.get(SupplierRequisitionOrder, fixture["supplier_order_id"])
        assert task is not None and product is not None and order is not None
        product.production_label_enabled = True
        product.production_label_units_per_label = 5
        task.status = "completed"
        task.planned_quantity = 23
        task.ordered_quantity_snapshot = 23
        task.production_label_enabled_snapshot = True
        task.production_label_units_per_label_snapshot = 5
        task.production_label_total_quantity_snapshot = 23
        task.production_label_count_snapshot = 5
        db.commit()
        task_id = int(task.id)

        package = build_supplier_requisition_packaging_label_package(
            db, order, selected_supplier_item_ids={fixture["supplier_item_id"]}
        )

    assert package["review_required"] is False
    assert package["printable"] is True
    assert package["plans"][0]["production_task_id"] == task_id
    assert package["plans"][0]["total_quantity"] == 23
    assert [label["quantity"] for label in package["labels"]] == [5, 5, 5, 5, 3]

    with TestClient(fixture["app"]) as client:
        _login(client, "p132a2-admin")
        result = client.post(
            f"/api/requisition/supplier-orders/{fixture['supplier_order_id']}"
            "/production-packaging-label-jobs",
            json={
                "idempotency_key": "completed-received-task-label-job",
                "plan_fingerprint": package["plan_fingerprint"],
                "confirmed": True,
                "items": [{"production_task_id": task_id, "print_label_count": 5}],
            },
        )
        assert result.status_code == 200, result.text
        assert result.json()["package"]["label_count"] == 5


def test_supplier_label_batch_freezes_two_orders_and_confirms_them_atomically(
    production_print_app,
) -> None:
    from app.models.product import Product
    from app.models.production import ProductionTask
    from app.models.production_label_print import ProductionPackagingLabelPrintJob
    from app.models.supplier_requisition_order import SupplierRequisitionOrder

    fixture = production_print_app
    with fixture["session_factory"]() as db:
        first = db.get(SupplierRequisitionOrder, fixture["supplier_order_id"])
        assert first is not None
        second = SupplierRequisitionOrder(
            order_number="SRO-P183B-BATCH-002",
            supplier_name=first.supplier_name,
            material_id=first.material_id,
            layer_count=first.layer_count,
            flute_type=first.flute_type,
            report_length_mm=first.report_length_mm,
            report_width_mm=first.report_width_mm,
            total_quantity=first.total_quantity,
            requisition_qty=first.requisition_qty,
            stock_deduction_qty=first.stock_deduction_qty,
            required_piece_qty=first.required_piece_qty,
            status="confirmed",
            created_by=first.created_by,
        )
        db.add(second)
        db.flush()
        for item in sorted(first.items, key=lambda row: row.id)[-2:]:
            item.supplier_order = second

        product = db.get(Product, fixture["product_id"])
        assert product is not None
        product.production_label_enabled = True
        product.production_label_units_per_label = 5
        for index, task in enumerate(
            db.scalars(select(ProductionTask).order_by(ProductionTask.id)), start=1
        ):
            task.production_label_enabled_snapshot = True
            task.production_label_units_per_label_snapshot = 5
            task.production_label_total_quantity_snapshot = 10 + index
            task.production_label_count_snapshot = 3
            task.production_label_template_version_snapshot = "current_40x30_v2"
            task.production_label_product_version_snapshot = int(product.version)
        db.commit()
        second_id = int(second.id)

    order_ids = sorted([fixture["supplier_order_id"], second_id])
    with TestClient(fixture["app"]) as client:
        _login(client, "p132a2-admin")
        preview = client.get(
            "/api/requisition/supplier-order-label-batches/package",
            params={"order_ids": ",".join(str(value) for value in order_ids)},
        )
        assert preview.status_code == 200, preview.text
        package = preview.json()
        assert package["supplier_order_ids"] == order_ids
        assert len(package["plans"]) >= 2

        payload = {
            "idempotency_key": "p1-83b-two-supplier-orders",
            "plan_fingerprint": package["plan_fingerprint"],
            "confirmed": True,
            "order_ids": order_ids,
            "items": [
                {
                    "production_task_id": int(plan["production_task_id"]),
                    "print_label_count": 1,
                }
                for plan in package["plans"]
            ],
        }
        prepared = client.post(
            "/api/requisition/supplier-order-label-batches/production-packaging-label-jobs",
            json=payload,
        )
        assert prepared.status_code == 200, prepared.text
        prepared_payload = prepared.json()
        assert len(prepared_payload["jobs"]) == 2
        assert prepared_payload["package"]["label_count"] == len(package["plans"])
        job_ids = sorted(int(row["job_id"]) for row in prepared_payload["jobs"])

        replay = client.post(
            "/api/requisition/supplier-order-label-batches/production-packaging-label-jobs",
            json=payload,
        )
        assert replay.status_code == 200, replay.text
        assert sorted(int(row["job_id"]) for row in replay.json()["jobs"]) == job_ids
        assert replay.json()["replayed"] is True

        confirmation = {
            "idempotency_key": "p1-83b-two-supplier-orders-confirmed",
            "confirmed": True,
            "job_ids": job_ids,
        }
        confirmed = client.post(
            "/api/requisition/supplier-order-label-batches/confirm",
            json=confirmation,
        )
        assert confirmed.status_code == 200, confirmed.text
        assert {row["status"] for row in confirmed.json()["jobs"]} == {"printed"}

    with fixture["session_factory"]() as db:
        jobs = list(
            db.scalars(
                select(ProductionPackagingLabelPrintJob).where(
                    ProductionPackagingLabelPrintJob.id.in_(job_ids)
                )
            )
        )
        assert len(jobs) == 2
        assert {job.status for job in jobs} == {"printed"}


def test_composite_parent_label_job_uses_same_count_freeze_and_keeps_evidence(
    tmp_path: Path,
) -> None:
    engine = create_sqlite_engine(tmp_path / "p1-83b-composite.sqlite3")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            user = User(
                username="p1-83b-composite-admin",
                password_hash="pytest-only",
                role="admin",
                real_name="组合标签管理员",
                display_name="组合标签管理员",
                is_active=True,
                must_change_password=False,
            )
            db.add(user)
            db.flush()
            requisition, order_item, items = _seed_label_facts(db)
            order_item.composite_fulfillment_mode_snapshot = "parent_delivery"
            order_item.product.production_label_enabled = True
            order_item.product.production_label_units_per_label = 50
            order_item.parent_production_label_enabled_snapshot = True
            order_item.parent_production_label_units_per_label_snapshot = 50
            order_item.parent_production_label_template_version_snapshot = (
                "current_40x30_v2"
            )
            from app.services.production_packaging_label import (
                build_composite_requisition_packaging_label_package,
            )

            preview = build_composite_requisition_packaging_label_package(
                db,
                requisition,
                selected_item_ids={item.id for item in items},
            )
            plan = preview["plans"][0]
            prepared = prepare_composite_packaging_label_job(
                db,
                requisition=requisition,
                selected_item_ids={item.id for item in items},
                idempotency_key="p1-83b-parent-35",
                expected_plan_fingerprint=preview["plan_fingerprint"],
                requested_print_counts={int(plan["production_task_id"]): 35},
                operator_id=user.id,
            )
            db.flush()
            assert prepared.package["system_label_count"] == 36
            assert prepared.package["label_count"] == 35
            assert prepared.package["print_summary"]["printed_task_count"] == 1
            assert len(prepared.package["job_tasks"]) == 2
            from app.models.production_label_print import (
                ProductionPackagingLabelPrintJobTask,
            )

            assert db.scalar(
                select(func.count(ProductionPackagingLabelPrintJobTask.id)).where(
                    ProductionPackagingLabelPrintJobTask.print_job_id
                    == prepared.job.id
                )
            ) == 2

            replay = prepare_composite_packaging_label_job(
                db,
                requisition=requisition,
                selected_item_ids={item.id for item in items},
                idempotency_key="p1-83b-parent-35",
                expected_plan_fingerprint=preview["plan_fingerprint"],
                requested_print_counts={int(plan["production_task_id"]): 35},
                operator_id=user.id,
            )
            assert replay.replayed is True
            assert replay.package == prepared.package

            with pytest.raises(ProductionLabelOperationError, match="幂等键"):
                prepare_composite_packaging_label_job(
                    db,
                    requisition=requisition,
                    selected_item_ids={item.id for item in items},
                    idempotency_key="p1-83b-parent-35",
                    expected_plan_fingerprint=preview["plan_fingerprint"],
                    requested_print_counts={int(plan["production_task_id"]): 34},
                    operator_id=user.id,
                )
    finally:
        engine.dispose()
