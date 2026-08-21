from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.services import production_label_strategy
from app.services import production_packaging_label
from tests.test_p1_32a2_requisition_production_print import (
    _login,
    production_print_app,
)


ROOT = Path(__file__).resolve().parents[1]
PRINT_PAGE = ROOT / "static" / "production-packaging-label.html"
INDEX_PAGE = ROOT / "static" / "index.html"

LEGACY_TEMPLATE = "legacy_65x45_v1"
ORIGINAL_40X30_TEMPLATE = "current_40x30_v1"
COMPACT_40X30_TEMPLATE = "current_40x30_v2"


def _function_source(source: str, function_name: str, next_function_name: str) -> str:
    match = re.search(
        rf"function\s+{re.escape(function_name)}\s*\([^)]*\)\s*\{{"
        rf"(?P<body>.*?)\n\s*function\s+{re.escape(next_function_name)}\s*\(",
        source,
        flags=re.DOTALL,
    )
    assert match, f"missing function boundary: {function_name} -> {next_function_name}"
    return match.group("body")


def test_current_template_is_a_new_immutable_compact_version() -> None:
    assert (
        production_label_strategy.CURRENT_PRODUCTION_LABEL_TEMPLATE_VERSION
        == COMPACT_40X30_TEMPLATE
    )
    assert production_packaging_label.template_dimensions(COMPACT_40X30_TEMPLATE) == {
        "width_mm": 40,
        "height_mm": 30,
    }
    assert {
        LEGACY_TEMPLATE,
        ORIGINAL_40X30_TEMPLATE,
        COMPACT_40X30_TEMPLATE,
    }.issubset(production_packaging_label.ALLOWED_TEMPLATE_VERSIONS)


def test_customer_chinese_short_name_is_manual_and_fail_closed() -> None:
    project = getattr(production_packaging_label, "_customer_label_fields", None)
    assert callable(project)
    assert project(COMPACT_40X30_TEMPLATE, "思迈包装") == {
        "customer_short_name": "思迈包装"
    }
    with pytest.raises(
        production_packaging_label.ProductionPackagingLabelError,
        match="客户资料.*中文简称",
    ):
        project(COMPACT_40X30_TEMPLATE, None)


def test_only_v2_projection_adds_the_manual_customer_short_name() -> None:
    project = getattr(
        production_packaging_label,
        "_customer_label_fields",
        None,
    )
    assert callable(project)
    assert project(LEGACY_TEMPLATE, "人工简称不会写入旧模板") == {}
    assert project(ORIGINAL_40X30_TEMPLATE, "人工简称不会写入旧模板") == {}
    assert project(COMPACT_40X30_TEMPLATE, "思迈包装") == {
        "customer_short_name": "思迈包装"
    }


def test_compact_renderer_has_values_only_and_quantity_unit() -> None:
    source = PRINT_PAGE.read_text(encoding="utf-8")
    compact = _function_source(source, "compactLabelHtml", "legacyLabelHtml")
    legacy = _function_source(source, "legacyLabelHtml", "labelHtml")
    router = _function_source(source, "labelHtml", "fitElement")

    assert "customer_short_name" in compact
    assert "product_code" in compact
    assert "product_name" in compact
    assert "specification" in compact
    assert "customer_name" not in compact
    for heading in ("客户名称", "存货编码", "产品名称", "规格", "本标签数量"):
        assert heading not in compact
        assert heading in legacy
    assert "只/捆" in compact
    assert "quantity-number" in compact
    assert "CURRENT_TEMPLATE_VERSION" in router
    assert "compactLabelHtml(label)" in router
    row_definition = re.search(
        r'html\[data-template="current_40x30_v2"\]\s+\.label-card\s*\{'
        r'[^}]*grid-template-rows:(?P<rows>[^;]+);',
        source,
    )
    assert row_definition
    row_heights = [
        float(value)
        for value in re.findall(r"([0-9.]+)mm", row_definition.group("rows"))
    ]
    assert sum(row_heights) == pytest.approx(29.4)

    node = shutil.which("node")
    assert node is not None
    script = f"""
const escapeHtml = (value) => String(value ?? "")
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#039;");
function compactLabelHtml(label) {{{compact}
const html = compactLabelHtml({json.dumps({
        'label_number': 1,
        'label_count': 1,
        'customer_short_name': '思迈',
        'product_code': 'SM-380-01',
        'product_name': '三层瓦楞外箱',
        'specification': '380×260×220mm',
        'quantity': 5,
    }, ensure_ascii=False)});
process.stdout.write(html);
"""
    result = subprocess.run(
        [node, "-e", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    assert all(
        value in result.stdout
        for value in ("思迈", "SM-380-01", "三层瓦楞外箱", "380×260×220mm", "5", "只/捆")
    )
    for heading in ("客户名称", "存货编码", "产品名称", "规格", "本标签数量"):
        assert heading not in result.stdout


def test_old_template_renderer_and_template_versions_remain_available() -> None:
    print_source = PRINT_PAGE.read_text(encoding="utf-8")
    index_source = INDEX_PAGE.read_text(encoding="utf-8")
    assert ORIGINAL_40X30_TEMPLATE in print_source
    assert LEGACY_TEMPLATE in print_source
    assert COMPACT_40X30_TEMPLATE in print_source
    assert ORIGINAL_40X30_TEMPLATE in index_source
    assert LEGACY_TEMPLATE in index_source
    assert COMPACT_40X30_TEMPLATE in index_source


def test_v2_package_freezes_manual_short_name_without_changing_v1_payload_shape(
    production_print_app,
) -> None:
    from app.models.customer import Customer
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.production import ProductionTask
    from app.models.supplier_requisition_order import SupplierRequisitionOrderItem

    fixture = production_print_app
    with fixture["session_factory"]() as db:
        item = db.get(OrderItem, fixture["order_item_id"])
        assert item is not None
        order = db.get(Order, item.order_id)
        assert order is not None
        customer = db.get(Customer, order.customer_id)
        assert customer is not None
        customer.name = "苏州思迈尔包装有限公司"
        customer.chinese_short_name = "思迈包装"
        item.customer_name = customer.name
        supplier_item = db.get(
            SupplierRequisitionOrderItem,
            fixture["supplier_item_id"],
        )
        assert supplier_item is not None
        supplier_item.customer_name = customer.name
        product = db.get(Product, item.product_id)
        assert product is not None
        task = db.scalar(
            select(ProductionTask).where(ProductionTask.order_item_id == item.id)
        )
        assert task is not None
        product.production_label_enabled = True
        product.production_label_units_per_label = 5
        task.production_label_enabled_snapshot = True
        task.production_label_units_per_label_snapshot = 5
        task.production_label_total_quantity_snapshot = 23
        task.production_label_count_snapshot = 5
        task.production_label_template_version_snapshot = COMPACT_40X30_TEMPLATE
        task.production_label_product_version_snapshot = int(product.version)
        db.commit()
        task_id = int(task.id)

    endpoint = (
        "/api/requisition/supplier-orders/"
        f"{fixture['supplier_order_id']}/production-packaging-label-package"
    )
    with TestClient(fixture["app"]) as client:
        _login(client, "p132a2-admin")
        compact_response = client.get(endpoint)
        assert compact_response.status_code == 200, compact_response.text
        compact_package = compact_response.json()
        assert compact_package["template_version"] == COMPACT_40X30_TEMPLATE
        assert [label["quantity"] for label in compact_package["labels"]] == [
            5,
            5,
            5,
            5,
            3,
        ]
        assert {label["customer_short_name"] for label in compact_package["labels"]} == {
            "思迈包装"
        }
        assert compact_package["plans"][0]["customer_short_name"] == "思迈包装"

        with fixture["session_factory"]() as db:
            task = db.get(ProductionTask, task_id)
            assert task is not None
            task.production_label_template_version_snapshot = ORIGINAL_40X30_TEMPLATE
            db.commit()

        original_response = client.get(endpoint)
        assert original_response.status_code == 200, original_response.text
        original_package = original_response.json()
        assert original_package["template_version"] == ORIGINAL_40X30_TEMPLATE
        assert "customer_short_name" not in original_package["plans"][0]
        assert all(
            "customer_short_name" not in label
            for label in original_package["labels"]
        )

        prepared = client.post(
            "/api/requisition/supplier-orders/"
            f"{fixture['supplier_order_id']}/production-packaging-label-jobs",
            json={
                "idempotency_key": "p1-66a-original-v1-frozen-job",
                "plan_fingerprint": original_package["plan_fingerprint"],
                "confirmed": True,
            },
        )
        assert prepared.status_code == 200, prepared.text
        job_id = int(prepared.json()["job_id"])
        confirmed = client.post(
            f"/api/requisition/production-packaging-label-jobs/{job_id}/confirm",
            json={
                "idempotency_key": "p1-66a-original-v1-confirmed-print",
                "confirmed": True,
            },
        )
        assert confirmed.status_code == 200, confirmed.text

        with fixture["session_factory"]() as db:
            task = db.get(ProductionTask, task_id)
            assert task is not None
            task.production_label_template_version_snapshot = COMPACT_40X30_TEMPLATE
            task.version += 1
            db.commit()

        frozen = client.get(
            f"/api/requisition/production-packaging-label-jobs/{job_id}"
        )
        assert frozen.status_code == 200, frozen.text
        assert frozen.json()["template_version"] == ORIGINAL_40X30_TEMPLATE
        assert frozen.json()["package"]["template_version"] == ORIGINAL_40X30_TEMPLATE
        assert all(
            "customer_short_name" not in label
            for label in frozen.json()["package"]["labels"]
        )


def test_v2_package_fails_closed_when_customer_has_no_manual_chinese_short_name(
    production_print_app,
) -> None:
    from app.models.customer import Customer
    from app.models.order import Order
    from app.models.order import OrderItem
    from app.models.product import Product
    from app.models.production import ProductionTask
    from app.models.supplier_requisition_order import SupplierRequisitionOrderItem

    fixture = production_print_app
    with fixture["session_factory"]() as db:
        item = db.get(OrderItem, fixture["order_item_id"])
        assert item is not None
        order = db.get(Order, item.order_id)
        assert order is not None
        customer = db.get(Customer, order.customer_id)
        assert customer is not None
        customer.name = "苏州思迈尔包装有限公司"
        customer.chinese_short_name = None
        product = db.get(Product, item.product_id)
        assert product is not None
        task = db.scalar(
            select(ProductionTask).where(ProductionTask.order_item_id == item.id)
        )
        supplier_item = db.get(
            SupplierRequisitionOrderItem,
            fixture["supplier_item_id"],
        )
        assert task is not None and supplier_item is not None
        supplier_item.customer_name = customer.name
        product.production_label_enabled = True
        product.production_label_units_per_label = 5
        task.production_label_enabled_snapshot = True
        task.production_label_units_per_label_snapshot = 5
        task.production_label_total_quantity_snapshot = 23
        task.production_label_count_snapshot = 5
        task.production_label_template_version_snapshot = COMPACT_40X30_TEMPLATE
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
    assert any("客户资料" in reason and "中文简称" in reason for reason in detail["reasons"])
