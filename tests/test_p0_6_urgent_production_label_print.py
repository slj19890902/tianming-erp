from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.test_p1_32a2_requisition_production_print import (
    _login,
    production_print_app,
)
from tests.test_p1_50c_task_label_plan_template import _production_router_app


ROOT = Path(__file__).resolve().parents[1]
TASK_PRINT = (ROOT / "static" / "requisition-production-print.html").read_text(
    encoding="utf-8"
)


def test_receipt_card_keeps_packaging_label_entry_and_uses_supplier_order_id() -> None:
    assert 'labelButton.hidden = batchMode;' in TASK_PRINT
    assert "labelButton.hidden = receiptMode || batchMode" not in TASK_PRINT
    assert "loadedPackage.supplier_order_id" in TASK_PRINT
    assert "production-packaging-label.html?id=${encodeURIComponent(supplierOrderId)}" in TASK_PRINT
    assert "packageData.review_required === true" not in TASK_PRINT


def test_a4_layout_adaptation_keeps_independent_packaging_labels_available() -> None:
    render_start = TASK_PRINT.index("async function render(packageData)")
    label_actions = TASK_PRINT.index(
        "applyProductionLabelActions(packageData)",
        render_start,
    )
    qr_wait = TASK_PRINT.index("await waitForProductQrImages()", render_start)
    overflow_gate = TASK_PRINT.index("const initialOverflow = overflowingCards()", render_start)
    helper_start = TASK_PRINT.index("function applyProductionLabelActions(packageData)")
    helper_end = TASK_PRINT.index("async function waitForProductQrImages()", helper_start)
    label_count = TASK_PRINT.index(
        "const labelCount = Number(packageData.production_label_count || 0)",
        helper_start,
    )
    label_gate = TASK_PRINT.index(
        "labelButton.disabled = batchMode || receiptBatchMode || labelCount <= 0",
        helper_start,
    )
    refresh_gate = TASK_PRINT.index(
        "labelRefreshButton.hidden = batchMode || receiptBatchMode || !refreshAction.options.length",
        helper_start,
    )

    assert label_count < label_gate < helper_end
    assert refresh_gate < helper_end
    assert label_actions < qr_wait < overflow_gate
    assert "printButton.disabled = true;\n          return;" not in TASK_PRINT[overflow_gate:]


def test_existing_v1_labels_offer_only_the_explicit_compact_upgrade(tmp_path: Path) -> None:
    helper = re.search(
        r"(function productionLabelRefreshAction\(packageData\) \{[\s\S]*?\n      \})\n\n      function applyProductionLabelActions",
        TASK_PRINT,
    )
    assert helper is not None
    assert "升级到无抬头中文简称版" in TASK_PRINT
    assert "labelCount > 0 || !refreshable.length" not in TASK_PRINT

    target = tmp_path / "p0-6-label-refresh-action.js"
    target.write_text(
        helper.group(1)
        + """
const assert = require("assert");
const v1 = {task_id:246, can_refresh:true, current_enabled:true, frozen_template_version:"current_40x30_v1"};
const v2 = {task_id:999, can_refresh:true, current_enabled:true, frozen_template_version:"current_40x30_v2"};
const blockedV1 = {task_id:248, can_refresh:false, current_enabled:true, frozen_template_version:"current_40x30_v1"};
const upgrade = productionLabelRefreshAction({production_label_count:44, production_label_refresh_options:[v1, v2, blockedV1]});
assert.strictEqual(upgrade.kind, "upgrade_compact");
assert.deepStrictEqual(upgrade.options.map((row) => row.task_id), [246]);
const current = productionLabelRefreshAction({production_label_count:44, production_label_refresh_options:[v2]});
assert.strictEqual(current.kind, "none");
assert.deepStrictEqual(current.options, []);
const firstEnable = productionLabelRefreshAction({production_label_count:0, production_label_refresh_options:[v2]});
assert.strictEqual(firstEnable.kind, "refresh");
assert.deepStrictEqual(firstEnable.options.map((row) => row.task_id), [999]);
""",
        encoding="utf-8",
    )
    import subprocess

    subprocess.run(["node", str(target)], check=True)


def test_existing_v1_label_count_projects_an_audited_upgrade_option(
    production_print_app,
) -> None:
    from app.models.product import Product
    from app.models.production import ProductionTask

    fixture = production_print_app
    _production_router_app(fixture)
    with fixture["session_factory"]() as db:
        product = db.get(Product, fixture["product_id"])
        task = db.scalar(
            select(ProductionTask).where(
                ProductionTask.order_item_id == fixture["order_item_id"]
            )
        )
        assert product is not None and task is not None
        product.production_label_enabled = True
        product.production_label_units_per_label = 50
        product.version = int(product.version) + 1
        task.production_label_enabled_snapshot = True
        task.production_label_units_per_label_snapshot = 50
        task.production_label_total_quantity_snapshot = 200
        task.production_label_count_snapshot = 4
        task.production_label_template_version_snapshot = "current_40x30_v1"
        task.production_label_product_version_snapshot = int(product.version)
        db.commit()
        task_id = int(task.id)

    with TestClient(fixture["app"]) as client:
        _login(client, "p132a2-admin")
        response = client.get(
            f"/api/requisition/supplier-orders/{fixture['supplier_order_id']}/production-print-package"
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["production_label_count"] == 4
    option = next(
        row
        for row in payload["production_label_refresh_options"]
        if row["task_id"] == task_id
    )
    assert option["can_refresh"] is True
    assert option["frozen_template_version"] == "current_40x30_v1"


def test_task_card_offers_explicit_audited_refresh_instead_of_current_product_fallback() -> None:
    assert 'id="labelRefreshButton"' in TASK_PRINT
    assert "/api/production/tasks/${encodeURIComponent(row.task_id)}/label-plan-refresh" in TASK_PRINT
    assert "expected_task_version:row.expected_task_version" in TASK_PRINT
    assert "expected_product_version:row.expected_product_version" in TASK_PRINT
    assert "confirmed_not_started:true" in TASK_PRINT
    assert "confirmed_no_prior_print:true" in TASK_PRINT
    assert "window.confirm(" in TASK_PRINT


def test_old_disabled_task_projects_refresh_and_becomes_printable(
    production_print_app,
) -> None:
    from app.models.product import Product
    from app.models.production import ProductionTask

    fixture = production_print_app
    _production_router_app(fixture)
    with fixture["session_factory"]() as db:
        product = db.get(Product, fixture["product_id"])
        task = db.scalar(
            select(ProductionTask).where(
                ProductionTask.order_item_id == fixture["order_item_id"]
            )
        )
        assert product is not None and task is not None
        product.production_label_enabled = True
        product.production_label_units_per_label = 50
        product.version = int(product.version) + 1
        task.production_label_enabled_snapshot = False
        task.production_label_units_per_label_snapshot = None
        task.production_label_total_quantity_snapshot = 0
        task.production_label_count_snapshot = 0
        db.commit()
        task_id = int(task.id)
        task_version = int(task.version)
        product_version = int(product.version)

    with TestClient(fixture["app"]) as client:
        _login(client, "p132a2-admin")
        before = client.get(
            f"/api/requisition/supplier-orders/{fixture['supplier_order_id']}/production-print-package"
        )
        assert before.status_code == 200
        before_payload = before.json()
        assert before_payload["production_label_count"] == 0
        option = next(
            row
            for row in before_payload["production_label_refresh_options"]
            if row["task_id"] == task_id
        )
        assert option["can_refresh"] is True
        assert option["expected_task_version"] == task_version
        assert option["expected_product_version"] == product_version
        assert "frozen_template_version" in option

        refreshed = client.post(
            f"/api/production/tasks/{task_id}/label-plan-refresh",
            json={
                "idempotency_key": "p0-6-urgent-refresh",
                "expected_task_version": task_version,
                "expected_product_version": product_version,
                "confirmed_not_started": True,
                "confirmed_no_prior_print": True,
            },
        )
        assert refreshed.status_code == 200, refreshed.text

        after = client.get(
            f"/api/requisition/supplier-orders/{fixture['supplier_order_id']}/production-print-package"
        )
        assert after.status_code == 200
        after_payload = after.json()
        assert after_payload["production_label_count"] > 0
        assert after_payload["production_label_task_count"] == 1


def test_inline_script_is_valid_javascript(tmp_path: Path) -> None:
    scripts = re.findall(r"<script[^>]*>([\s\S]*?)</script>", TASK_PRINT)
    assert len(scripts) == 1
    target = tmp_path / "p0-6-production-print-inline.js"
    target.write_text(scripts[0], encoding="utf-8")
    import subprocess

    subprocess.run(["node", "--check", str(target)], check=True)
