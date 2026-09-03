from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
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
from app.services.production_packaging_label_layout import (
    ProductionPackagingLabelLayoutError,
    default_layout,
    layout_hash,
    normalize_layout,
)
from tests.test_p1_32a2_requisition_production_print import (
    _login,
    production_print_app,
)


ROOT = Path(__file__).resolve().parents[1]
PRINT_PAGE = ROOT / "static" / "production-packaging-label.html"
CURRENT_TEMPLATE = "current_40x30_v2"


def _function_source(
    source: str,
    function_name: str,
    next_function_name: str,
) -> str:
    match = re.search(
        rf"function\s+{re.escape(function_name)}\s*\([^)]*\)\s*\{{"
        rf"(?P<body>.*?)\n\s*function\s+{re.escape(next_function_name)}\s*\(",
        source,
        flags=re.DOTALL,
    )
    assert match, f"missing function boundary: {function_name} -> {next_function_name}"
    return match.group("body")


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


def _layout_element(layout: dict, element_id: str) -> dict:
    return next(
        element for element in layout["elements"] if element["id"] == element_id
    )


def test_registered_elements_can_be_hidden_and_quantity_suffix_is_controlled() -> None:
    layout = copy.deepcopy(default_layout())
    _layout_element(layout, "product_name")["visible"] = False
    quantity = _layout_element(layout, "quantity")
    quantity["fixed_suffix"] = "只"

    normalized = normalize_layout(layout)
    assert _layout_element(normalized, "product_name")["visible"] is False
    assert _layout_element(normalized, "quantity")["fixed_suffix"] == "只"

    for element_id in (
        "customer_short_name",
        "product_code",
        "product_name",
        "specification",
        "quantity",
    ):
        hidden_layout = copy.deepcopy(default_layout())
        _layout_element(hidden_layout, element_id)["visible"] = False
        assert _layout_element(normalize_layout(hidden_layout), element_id)[
            "visible"
        ] is False

    quantity["fixed_suffix"] = ""
    assert _layout_element(normalize_layout(layout), "quantity")["fixed_suffix"] == ""
    quantity["fixed_suffix"] = "张/包"
    assert _layout_element(normalize_layout(layout), "quantity")["fixed_suffix"] == "张/包"

    missing = copy.deepcopy(layout)
    missing["elements"] = [
        element for element in missing["elements"] if element["id"] != "product_name"
    ]
    with pytest.raises(ProductionPackagingLabelLayoutError, match="布局缺少已登记元素"):
        normalize_layout(missing)

    wrong_element = copy.deepcopy(layout)
    _layout_element(wrong_element, "product_name")["fixed_suffix"] = "只"
    with pytest.raises(ProductionPackagingLabelLayoutError, match="只有数量元素"):
        normalize_layout(wrong_element)

    for invalid in (
        None,
        "<script>",
        "{quantity}",
        "http://a",
        "alert(1)",
        "data:x",
        "只\n捆",
        "固定说明超过八个字",
    ):
        invalid_layout = copy.deepcopy(layout)
        _layout_element(invalid_layout, "quantity")["fixed_suffix"] = invalid
        with pytest.raises(ProductionPackagingLabelLayoutError, match="固定说明"):
            normalize_layout(invalid_layout)

    unknown = copy.deepcopy(layout)
    _layout_element(unknown, "quantity")["custom_html"] = "<b>50</b>"
    with pytest.raises(ProductionPackagingLabelLayoutError, match="未登记的布局字段"):
        normalize_layout(unknown)

    unknown_root = copy.deepcopy(layout)
    unknown_root["template_html"] = "<b>任意模板</b>"
    with pytest.raises(ProductionPackagingLabelLayoutError, match="未登记字段"):
        normalize_layout(unknown_root)

    overlap = copy.deepcopy(default_layout())
    first = _layout_element(overlap, "product_name")
    second = _layout_element(overlap, "specification")
    first.update(
        x_mm=second["x_mm"],
        y_mm=second["y_mm"],
        width_mm=second["width_mm"],
        height_mm=second["height_mm"],
    )
    with pytest.raises(ProductionPackagingLabelLayoutError, match="产品名称与规格不能重叠"):
        normalize_layout(overlap)
    first["visible"] = False
    assert _layout_element(normalize_layout(overlap), "product_name")["visible"] is False


def test_legacy_layout_without_suffix_keeps_original_hash_and_default_meaning() -> None:
    legacy = copy.deepcopy(default_layout())
    _layout_element(legacy, "quantity").pop("fixed_suffix", None)
    original_hash = layout_hash(legacy)

    normalized = normalize_layout(legacy)

    assert "fixed_suffix" not in _layout_element(normalized, "quantity")
    assert layout_hash(normalized) == original_hash
    assert original_hash == "7c91732722199dce52242253b2d5347ba7714729400f202a3a50cd8f597910b4"


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


def test_layout_writes_require_admin_and_reject_unregistered_fields(
    production_print_app,
) -> None:
    fixture = production_print_app
    payload = {
        "operation_key": "p1-104-permission-extra",
        "expected_draft_version": 0,
        "layout": default_layout(),
    }
    endpoint = "/api/requisition/production-packaging-label-layout/admin/draft"
    with TestClient(fixture["app"]) as client:
        unauthenticated = client.put(endpoint, json=payload)
        assert unauthenticated.status_code == 401

        _login(client, "p132a2-sales")
        forbidden = client.put(endpoint, json=payload)
        assert forbidden.status_code == 403

        _login(client, "p132a2-admin")
        extra_element = copy.deepcopy(payload)
        extra_element["operation_key"] = "p1-104-extra-element"
        extra_element["layout"]["elements"][0]["html"] = "<b>任意文字</b>"
        rejected_element = client.put(endpoint, json=extra_element)
        assert rejected_element.status_code == 422
        assert "extra_forbidden" in rejected_element.text

        extra_root = copy.deepcopy(payload)
        extra_root["operation_key"] = "p1-104-extra-root"
        extra_root["layout"]["template_html"] = "<b>任意模板</b>"
        rejected_root = client.put(endpoint, json=extra_root)
        assert rejected_root.status_code == 422
        assert "extra_forbidden" in rejected_root.text


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
        initial_payload = initial.json()
        assert initial_payload["label_layout"]["version"] == 0
        assert "fixed_suffix" not in _layout_element(
            initial_payload["label_layout"]["layout"], "quantity"
        )
        legacy_prepared = client.post(
            "/api/requisition/supplier-orders/"
            f"{fixture['supplier_order_id']}/production-packaging-label-jobs",
            json={
                "idempotency_key": "p1-104-legacy-default-layout-job",
                "plan_fingerprint": initial_payload["plan_fingerprint"],
                "confirmed": True,
            },
        )
        assert legacy_prepared.status_code == 200, legacy_prepared.text
        legacy_job_id = legacy_prepared.json()["job_id"]

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
        legacy_frozen = client.get(
            f"/api/requisition/production-packaging-label-jobs/{legacy_job_id}"
        )
        assert legacy_frozen.status_code == 200, legacy_frozen.text
        assert legacy_frozen.json()["package"]["label_layout"]["version"] == 0
        assert "fixed_suffix" not in _layout_element(
            legacy_frozen.json()["package"]["label_layout"]["layout"],
            "quantity",
        )
        assert (
            current.json()["plan_fingerprint"]
            != first_payload["plan_fingerprint"]
        )


def test_hidden_element_and_fixed_suffix_are_frozen_without_deleting_business_data(
    production_print_app,
) -> None:
    fixture = production_print_app
    _enable_compact_labels(fixture)
    package_url = (
        "/api/requisition/supplier-orders/"
        f"{fixture['supplier_order_id']}/production-packaging-label-package"
    )
    first_layout = copy.deepcopy(default_layout())
    _layout_element(first_layout, "product_name")["visible"] = False
    _layout_element(first_layout, "quantity")["fixed_suffix"] = "只"

    with TestClient(fixture["app"]) as client:
        _login(client, "p132a2-admin")
        state = _save_draft(client, first_layout, "p1-104-freeze-d1", 0)
        state = _publish(client, state, "p1-104-freeze-p1")
        first_package = client.get(package_url)
        assert first_package.status_code == 200, first_package.text
        first_payload = first_package.json()
        assert _layout_element(
            first_payload["label_layout"]["layout"], "product_name"
        )["visible"] is False
        assert _layout_element(
            first_payload["label_layout"]["layout"], "quantity"
        )["fixed_suffix"] == "只"
        assert all(label["product_name"] for label in first_payload["labels"])
        assert [label["quantity"] for label in first_payload["labels"]] == [
            5,
            5,
            5,
            5,
            3,
        ]

        prepared = client.post(
            "/api/requisition/supplier-orders/"
            f"{fixture['supplier_order_id']}/production-packaging-label-jobs",
            json={
                "idempotency_key": "p1-104-frozen-hidden-suffix-job",
                "plan_fingerprint": first_payload["plan_fingerprint"],
                "confirmed": True,
            },
        )
        assert prepared.status_code == 200, prepared.text
        job_id = prepared.json()["job_id"]

        second_layout = copy.deepcopy(first_layout)
        _layout_element(second_layout, "product_name")["visible"] = True
        _layout_element(second_layout, "quantity")["fixed_suffix"] = ""
        state = _save_draft(
            client,
            second_layout,
            "p1-104-freeze-d2",
            state["draft"]["version"],
        )
        _publish(client, state, "p1-104-freeze-p2")

        current = client.get(package_url)
        frozen = client.get(
            f"/api/requisition/production-packaging-label-jobs/{job_id}"
        )
        assert current.status_code == 200, current.text
        assert frozen.status_code == 200, frozen.text
        assert _layout_element(
            current.json()["label_layout"]["layout"], "quantity"
        )["fixed_suffix"] == ""
        assert _layout_element(
            frozen.json()["package"]["label_layout"]["layout"], "quantity"
        )["fixed_suffix"] == "只"
        assert _layout_element(
            frozen.json()["package"]["label_layout"]["layout"], "product_name"
        )["visible"] is False


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


def test_stored_legacy_layout_without_suffix_keeps_hash_and_remains_readable(
    production_print_app,
) -> None:
    fixture = production_print_app
    legacy = copy.deepcopy(default_layout())
    _layout_element(legacy, "quantity").pop("fixed_suffix", None)
    legacy_product = _layout_element(legacy, "product_name")
    legacy_specification = _layout_element(legacy, "specification")
    legacy_product.update(
        x_mm=legacy_specification["x_mm"],
        y_mm=legacy_specification["y_mm"],
        width_mm=legacy_specification["width_mm"],
        height_mm=legacy_specification["height_mm"],
    )
    with fixture["session_factory"]() as db:
        db.add(
            ProductionPackagingLabelLayoutRevision(
                stream="release",
                version=1,
                catalog_version="p1-66b-v1",
                payload_json=json.dumps(
                    legacy,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                payload_hash=layout_hash(legacy),
                base_release_version=0,
                operation_kind="publish",
            )
        )
        db.commit()

    with TestClient(fixture["app"]) as client:
        _login(client, "p132a2-admin")
        response = client.get(
            "/api/requisition/production-packaging-label-layout/admin"
        )
        assert response.status_code == 200, response.text
        published = response.json()["published"]
        assert published["layout_hash"] == layout_hash(legacy)
        assert "fixed_suffix" not in _layout_element(
            published["layout"], "quantity"
        )


def test_layout_renderer_uses_saved_suffix_and_omits_hidden_elements() -> None:
    source = PRINT_PAGE.read_text(encoding="utf-8")
    suffix_body = _function_source(
        source,
        "quantityFixedSuffix",
        "validFixedSuffix",
    )
    value_body = _function_source(
        source,
        "layoutElementValue",
        "layoutDrivenLabelHtml",
    )
    renderer_body = _function_source(
        source,
        "layoutDrivenLabelHtml",
        "legacyLabelHtml",
    )
    node = shutil.which("node")
    assert node is not None
    script = f"""
const escapeHtml = (value) => String(value ?? "")
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#039;");
const requiredText = (value) => typeof value === "string" && value.trim() ? value.trim() : "";
const DEFAULT_QUANTITY_FIXED_SUFFIX = "只/捆";
const hasOwn = (value, key) => Object.prototype.hasOwnProperty.call(value, key);
function quantityFixedSuffix(element) {{{suffix_body}
function layoutElementValue(label, elementId) {{{value_body}
function layoutDrivenLabelHtml(label, layout) {{{renderer_body}
const label = {{
  label_number:1,
  label_count:1,
  customer_short_name:"思迈",
  product_code:"SM-01",
  product_name:"不应显示的产品名称",
  specification:"380×260×220mm",
  quantity:50,
}};
const base = {{
  elements:[
    {{id:"product_name",kind:"text",visible:false,x_mm:0,y_mm:0,width_mm:10,height_mm:5,font_size_mm:2,font_weight:400,text_align:"left"}},
    {{id:"quantity",kind:"text",visible:true,x_mm:0,y_mm:5,width_mm:20,height_mm:8,font_size_mm:6,font_weight:900,text_align:"center"}},
  ],
}};
const oldLayout = JSON.parse(JSON.stringify(base));
const unitLayout = JSON.parse(JSON.stringify(base));
unitLayout.elements[1].fixed_suffix = "只";
const numberOnlyLayout = JSON.parse(JSON.stringify(base));
numberOnlyLayout.elements[1].fixed_suffix = "";
process.stdout.write(JSON.stringify({{
  legacy:layoutDrivenLabelHtml(label, oldLayout),
  unit:layoutDrivenLabelHtml(label, unitLayout),
  numberOnly:layoutDrivenLabelHtml(label, numberOnlyLayout),
}}));
"""
    result = subprocess.run(
        [node, "-e", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    rendered = json.loads(result.stdout)
    assert "不应显示的产品名称" not in rendered["legacy"]
    assert '<span class="layout-unit">只/捆</span>' in rendered["legacy"]
    assert '<span class="layout-unit">只</span>' in rendered["unit"]
    assert ">50<" in rendered["numberOnly"]
    assert "layout-unit" not in rendered["numberOnly"]


def test_editor_actions_hide_restore_and_edit_only_the_fixed_suffix() -> None:
    source = PRINT_PAGE.read_text(encoding="utf-8")
    update_body = _function_source(
        source,
        "updateQuantityFixedSuffix",
        "toggleCurrentLayoutElementVisibility",
    )
    toggle_body = _function_source(
        source,
        "toggleCurrentLayoutElementVisibility",
        "layoutMutationKey",
    )
    node = shutil.which("node")
    assert node is not None
    script = f"""
const DEFAULT_QUANTITY_FIXED_SUFFIX = "只/捆";
const MAX_FIXED_SUFFIX_LENGTH = 8;
const LAYOUT_ELEMENT_LABELS = {{quantity:"数量（只/捆）"}};
const hasOwn = (value, key) => Object.prototype.hasOwnProperty.call(value, key);
function quantityFixedSuffix(element) {{
  if (!element || element.id !== "quantity") return "";
  return hasOwn(element, "fixed_suffix") ? String(element.fixed_suffix) : DEFAULT_QUANTITY_FIXED_SUFFIX;
}}
function validFixedSuffix(value) {{
  return typeof value === "string" && value === value.trim() && value.length <= MAX_FIXED_SUFFIX_LENGTH && !/[\\u0000-\\u001f<>{{}}]/u.test(value);
}}
const element = {{id:"quantity",kind:"text",visible:true,fixed_suffix:"只/捆"}};
const editorLayout = {{elements:[element]}};
const currentEditorElement = () => element;
const layoutFixedSuffix = {{value:""}};
let layoutOperationBusy = false;
let layoutDrag = {{active:true}};
let rendered = 0;
const statuses = [];
const renderLayoutEditor = () => {{ rendered += 1; }};
const showLayoutStatus = (message, kind="") => statuses.push({{message,kind}});
const showEditorLayoutMutationStatus = (message) => showLayoutStatus(message);
const findVisibleLayoutOverlap = () => null;
function updateQuantityFixedSuffix(rawValue) {{{update_body}
function toggleCurrentLayoutElementVisibility() {{{toggle_body}
updateQuantityFixedSuffix("只");
toggleCurrentLayoutElementVisibility();
updateQuantityFixedSuffix("");
const hiddenState = {{visible:element.visible,suffix:element.fixed_suffix}};
toggleCurrentLayoutElementVisibility();
updateQuantityFixedSuffix("");
process.stdout.write(JSON.stringify({{hiddenState,visible:element.visible,suffix:element.fixed_suffix,layoutDrag,rendered,statuses}}));
"""
    result = subprocess.run(
        [node, "-e", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    state = json.loads(result.stdout)
    assert state["hiddenState"] == {"visible": False, "suffix": "只"}
    assert state["visible"] is True
    assert state["suffix"] == ""
    assert state["layoutDrag"] is None
    assert state["rendered"] == 4
    assert any("业务数据仍保留" in item["message"] for item in state["statuses"])


def test_print_page_exposes_visual_editor_without_arbitrary_qr_or_html() -> None:
    source = PRINT_PAGE.read_text(encoding="utf-8")
    for marker in (
        "编辑标签布局",
        "layoutStage",
        'data-layout-field="x_mm"',
        'data-layout-field="y_mm"',
        'data-layout-field="width_mm"',
        'data-layout-field="font_size_mm"',
        'id="layoutVisibilityButton"',
        'id="layoutFixedSuffix"',
        'id="layoutClearFixedSuffixButton"',
        "删除显示（可恢复）",
        "恢复显示",
        "已隐藏（可恢复）",
        "数量后固定说明",
        "留空时只打印实际数量",
        'element.visible || element.kind === "qr"',
        "checkVisibleOverlap:true",
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


def test_layout_has_no_outer_border_and_keeps_content_dividers() -> None:
    source = PRINT_PAGE.read_text(encoding="utf-8")
    assert ".label-card.layout-driven { position:relative; display:block; overflow:hidden; border:0; }" in source
    assert ".label-card.layout-driven::after" not in source
    assert "border:.3mm solid #000;" not in source
    assert ".customer-row { border-bottom:.25mm solid #000; }" in source
    assert ".detail-row { border-bottom:.2mm solid #6b7280; }" in source
    assert ".code-row {" in source and "border-bottom:.25mm solid #000;" in source
