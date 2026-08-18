from __future__ import annotations

import base64
import json
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.test_p1_32a2_requisition_production_print import (
    _login,
    production_print_app,
)


ROOT = Path(__file__).resolve().parents[1]
PRINT_PAGE = (ROOT / "static" / "requisition-production-print.html").read_text(
    encoding="utf-8"
)
PRODUCT_PAGE = (ROOT / "static" / "mobile_product_live.html").read_text(
    encoding="utf-8"
)
MAIN = (ROOT / "app" / "main.py").read_text(encoding="utf-8")


def _decode_qr(data_url: str) -> str:
    import cv2
    import numpy as np

    raw = base64.b64decode(data_url.split(",", 1)[1])
    image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    decoded, _points, _straight = cv2.QRCodeDetector().detectAndDecode(image)
    return decoded


def test_formal_product_qr_is_stable_and_contains_no_dynamic_business_fact(
    production_print_app,
) -> None:
    from app.models.product import Product
    from app.models.production import ProductionTask

    order_id = production_print_app["supplier_order_id"]
    with TestClient(production_print_app["app"]) as client:
        _login(client, "p132a2-admin")
        first = client.get(
            f"/api/requisition/supplier-orders/{order_id}/production-print-package"
        )
        assert first.status_code == 200, first.text
        card = first.json()["cards"][0]
        assert card["joining_method"] in {"粘贴", "打钉", "无需结合"}
        assert "待确认" not in card["joining_method"]
        assert card["product_id"] == production_print_app["product_id"]
        qr = card["product_qr"]
        assert qr["product_id"] == production_print_app["product_id"]
        assert qr["lookup_url"].endswith(
            f"/P/{production_print_app['product_id']}"
        )
        assert "?" not in qr["lookup_url"]
        assert _decode_qr(qr["qr_data_url"]) == qr["lookup_url"]
        serialized = json.dumps(qr, ensure_ascii=False)
        for forbidden in (
            "planned_quantity",
            "status",
            "token",
            "order_id",
            "order_item_id",
            "task_id",
            "quantity",
        ):
            assert forbidden not in serialized

        with production_print_app["session_factory"]() as db:
            product = db.get(Product, production_print_app["product_id"])
            product.product_name = "修改后的当前产品名称"
            product.version += 1
            task = db.scalar(
                select(ProductionTask).where(
                    ProductionTask.order_item_id
                    == production_print_app["order_item_id"]
                )
            )
            task.version += 1
            db.commit()
        refreshed = client.get(
            f"/api/requisition/supplier-orders/{order_id}/production-print-package"
        )
        assert refreshed.status_code == 200, refreshed.text
        assert refreshed.json()["cards"][0]["product_qr"] == qr


def test_current_product_overview_is_scoped_permissioned_and_price_free(
    production_print_app,
) -> None:
    from app.api.mobile_erp import router as mobile_router
    from app.models.access_control import UserPermissionOverride
    from app.models.product import Product
    from app.models.user import User

    app = production_print_app["app"]
    app.include_router(mobile_router, prefix="/api/mobile/erp")
    product_id = production_print_app["product_id"]

    with TestClient(app) as client:
        _login(client, "p132a2-admin")
        response = client.get(
            f"/api/mobile/erp/products/{product_id}/production-overview"
        )
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "private, no-store, max-age=0"
        assert response.headers["x-robots-tag"] == "noindex, nofollow"
        body = response.json()
        assert body["view"] == "current_product"
        assert body["view_label"] == "当前资料"
        assert body["read_only"] is True
        assert body["product"]["id"] == product_id
        assert body["product"]["joining_method"] in {
            "粘贴",
            "打钉",
            "无需结合",
        }
        assert body["current_tasks"]["items"]
        serialized = json.dumps(body, ensure_ascii=False, sort_keys=True)
        for forbidden in (
            "sale_unit_price",
            "sale_unit_price_no_tax",
            "cost_unit_price",
            "board_price",
            "suggested_price",
            "supplier_name",
            "quote_price",
            "billing_note",
            "tax_no",
        ):
            assert forbidden not in serialized

    with TestClient(app) as client:
        _login(client, "p132a2-sales")
        denied = client.get(
            f"/api/mobile/erp/products/{product_id}/production-overview"
        )
        assert denied.status_code == 403

    with production_print_app["session_factory"]() as db:
        restricted = db.scalar(select(User).where(User.username == "p132a2-sales"))
        db.add_all(
            [
                UserPermissionOverride(
                    user_id=restricted.id,
                    permission_code="orders.view",
                    is_allowed=True,
                ),
                UserPermissionOverride(
                    user_id=restricted.id,
                    permission_code="production.printing.view",
                    is_allowed=True,
                ),
            ]
        )
        db.commit()
    with TestClient(app) as client:
        _login(client, "p132a2-sales")
        hidden = client.get(
            f"/api/mobile/erp/products/{product_id}/production-overview"
        )
        assert hidden.status_code == 404
        assert hidden.headers["cache-control"] == "private, no-store, max-age=0"

    with production_print_app["session_factory"]() as db:
        product = db.get(Product, product_id)
        product.is_active = False
        db.commit()
    with TestClient(app) as client:
        _login(client, "p132a2-admin")
        inactive = client.get(
            f"/api/mobile/erp/products/{product_id}/production-overview"
        )
        assert inactive.status_code == 404


def test_product_scan_page_and_task_paper_are_read_only_and_fail_closed() -> None:
    from app.api.mobile_erp import _product_joining_summary
    from app.main import create_app

    assert _product_joining_summary("粘箱") == ("粘贴", None)
    assert _product_joining_summary("钉箱") == ("打钉", None)
    assert _product_joining_summary(None) == ("无需结合", None)
    conflict, warning = _product_joining_summary("粘箱，钉箱")
    assert conflict == "结合方式冲突"
    assert warning and "请先" in warning

    assert 'route.path == "/P/{product_id}"' in MAIN
    assert '"/P/{product_id}"' in MAIN
    with TestClient(create_app()) as client:
        page = client.get("/P/1")
        post = client.post("/P/1")
    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    assert page.headers["cache-control"] == "private, no-store, max-age=0"
    assert post.status_code == 405

    for marker in (
        "产品当前资料",
        "/api/mobile/erp/products/${productId}/production-overview",
        "当前资料",
        "当前相关任务摘要",
        "刷新后读取最新主档，不覆盖历史任务纸面",
        "登录已失效",
    ):
        assert marker in PRODUCT_PAGE
    for forbidden in (
        "sale_unit_price",
        "cost_unit_price",
        "board_price",
        "supplier_name",
        'method:"POST"',
        'method:"PUT"',
        'method:"DELETE"',
    ):
        assert forbidden not in PRODUCT_PAGE

    assert "productQrHtml(card)" in PRINT_PAGE
    assert 'alt="扫码查看当前产品资料"' in PRINT_PAGE
    assert "扫码看当前资料</span>" not in PRINT_PAGE
    assert "waitForProductQrImages" in PRINT_PAGE
    assert "card.status_label" not in PRINT_PAGE[
        PRINT_PAGE.index("function cardHtml") : PRINT_PAGE.index(
            "function applyMode", PRINT_PAGE.index("function cardHtml")
        )
    ]
    assert 'card.joining_method || "无需结合"' in PRINT_PAGE
    assert "结合方式待确认" not in PRINT_PAGE


def test_temporary_or_inactive_product_does_not_receive_a_fabricated_qr(
    production_print_app,
) -> None:
    from app.models.product import Product
    from app.models.supplier_requisition_order import SupplierRequisitionOrder
    from app.services.requisition_production_print import (
        build_supplier_requisition_production_package,
    )

    with production_print_app["session_factory"]() as db:
        product = db.get(Product, production_print_app["product_id"])
        product.is_active = False
        order = db.get(
            SupplierRequisitionOrder,
            production_print_app["supplier_order_id"],
        )
        db.flush()
        package = build_supplier_requisition_production_package(db, order)
    card = package["cards"][0]
    assert card["product_id"] is None
    assert card["product_qr"] is None
    assert card["product_qr_unavailable_reason"] == "当前产品已停用，不生成二维码"
