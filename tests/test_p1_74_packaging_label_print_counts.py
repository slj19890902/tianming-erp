from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select

from app.api.requisition import ProductionPackagingLabelJobRequest
from app.services.production_packaging_label import (
    ProductionPackagingLabelError,
    apply_packaging_label_print_counts,
)
from tests.test_p1_32a2_requisition_production_print import (
    _login,
    production_print_app,
)
from tests.test_p1_50c_task_label_plan_template import _enable_one_frozen_plan


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


def test_request_contract_is_ordered_unique_and_rejects_boolean_counts() -> None:
    accepted = ProductionPackagingLabelJobRequest.model_validate(
        {
            "idempotency_key": "p174-one",
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
                "idempotency_key": "p174-duplicate",
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
                "idempotency_key": "p174-bool",
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
            f"/api/requisition/supplier-orders/{order_id}/production-packaging-label-package"
        )
        assert preview.status_code == 200, preview.text
        plan = preview.json()["plans"][0]
        payload = {
            "idempotency_key": "p1-74-reduced-five-to-four",
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
        assert frozen["system_label_count"] == plan["label_count"] == 5
        assert [label["quantity"] for label in frozen["labels"]] == [5, 5, 5, 5]
        assert frozen["print_summary"]["print_label_count"] == 4
        assert frozen["print_summary"]["system_label_count"] == 5

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
                "items": [
                    {"production_task_id": task_id, "print_label_count": 3}
                ],
            },
        )
        assert changed.status_code == 409

        all_zero = client.post(
            f"/api/requisition/supplier-orders/{order_id}/production-packaging-label-jobs",
            json={
                **payload,
                "idempotency_key": "p1-74-all-zero",
                "items": [
                    {"production_task_id": task_id, "print_label_count": 0}
                ],
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
