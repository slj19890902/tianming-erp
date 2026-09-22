from __future__ import annotations

from pathlib import Path
import re
import shutil
import subprocess

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from test_p1_21d_mobile_production_materials import _login, mobile_production_app


ROOT = Path(__file__).resolve().parents[1]
MOBILE_HTML = (ROOT / "static" / "mobile_erp.html").read_text(encoding="utf-8")


def test_lookup_finds_same_code_orders_respects_scope_and_writes_nothing(
    mobile_production_app,
) -> None:
    app, factory, _ids = mobile_production_app
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.order import Order, OrderItem
    from app.models.production import ProductionCompletion, ProductionTask

    with TestClient(app) as client:
        _login(client, "mobile-workshop")
        with factory() as db:
            before = (
                db.scalar(select(func.count(ProductionTask.id))),
                db.scalar(select(func.count(ProductionCompletion.id))),
                db.scalar(select(func.count(IncomingReceiptItem.id))),
            )
            visible_task_id = db.scalar(
                select(ProductionTask.id)
                .join(OrderItem, OrderItem.id == ProductionTask.order_item_id)
                .join(Order, Order.id == OrderItem.order_id)
                .where(Order.order_number == "MOBILE-VISIBLE")
            )

        response = client.get(
            "/api/mobile/erp/production/pending/lookup",
            params={"q": "MOBILE-PROD-01"},
        )
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "private, no-store"
        payload = response.json()
        assert payload["read_only"] is True
        assert payload["total"] == payload["returned_count"] == 2
        assert {row["order_number"] for row in payload["items"]} == {
            "MOBILE-VISIBLE",
            "MOBILE-OLD",
        }
        assert all(row["status"] == "pending" for row in payload["items"])
        assert all(row["customer_name"] == "匿名车间客户" for row in payload["items"])
        assert "SECRET-SUPPLIER" not in response.text
        assert "unit_price" not in response.text
        assert "cost" not in response.text.lower()

        by_order = client.get(
            "/api/mobile/erp/production/pending/lookup", params={"q": "PO-VISIBLE"}
        )
        assert [row["order_number"] for row in by_order.json()["items"]] == [
            "MOBILE-VISIBLE"
        ]
        by_task = client.get(
            "/api/mobile/erp/production/pending/lookup",
            params={"q": str(visible_task_id)},
        )
        assert by_task.json()["items"][0]["task_id"] == visible_task_id
        hidden = client.get(
            "/api/mobile/erp/production/pending/lookup",
            params={"q": "HIDDEN-PROD-01"},
        )
        assert hidden.json()["total"] == 0
        assert hidden.json()["items"] == []

        with factory() as db:
            after = (
                db.scalar(select(func.count(ProductionTask.id))),
                db.scalar(select(func.count(ProductionCompletion.id))),
                db.scalar(select(func.count(IncomingReceiptItem.id))),
            )
        assert after == before


def test_lookup_requires_auth_and_both_permissions(mobile_production_app) -> None:
    app, _factory, _ids = mobile_production_app
    with TestClient(app) as client:
        unauthorized = client.get(
            "/api/mobile/erp/production/pending/lookup", params={"q": "MOBILE-PROD-01"}
        )
        assert unauthorized.status_code == 401
        _login(client, "mobile-sales")
        forbidden = client.get(
            "/api/mobile/erp/production/pending/lookup", params={"q": "MOBILE-PROD-01"}
        )
        assert forbidden.status_code == 403


def test_scan_ui_is_simple_latest_wins_and_valid_javascript() -> None:
    for text in (
        "扫码或输入存货编码、订单号、任务号",
        "扫码反查",
        "清空结果，返回近期生产",
        "/api/mobile/erp/production/pending/lookup",
        "productionLookupGeneration",
        "productionLookupController?.abort()",
        "没有找到仍待生产的订单",
        "待生产订单反查失败",
    ):
        assert text in MOBILE_HTML
    assert MOBILE_HTML.count(
        "/api/mobile/erp/production/pending/lookup?q=${encodeURIComponent(query)}&limit=50"
    ) == 1
    production = MOBILE_HTML.split("function productionStationCard", 1)[1].split("async function initialize", 1)[0]
    assert "apiPost(" not in production
    assert "fetch(" not in production
    assert 'fetch("/api/auth/logout"' in MOBILE_HTML
    for method in ('method: "PUT"', 'method: "DELETE"'):
        assert method not in production

    node = shutil.which("node")
    if node is None:
        return
    script = re.search(r"<script>\s*(.*?)\s*</script>", MOBILE_HTML, re.S)
    assert script is not None
    result = subprocess.run(
        [node, "--check", "-"],
        input=script.group(1).encode("utf-8"),
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
