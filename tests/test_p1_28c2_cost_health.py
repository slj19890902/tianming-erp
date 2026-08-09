from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.services.order_estimated_cost_snapshot import classify_estimated_cost_health
from tests.test_phase5_orders import _login, _payload, order_api_app


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static/index.html").read_text(encoding="utf-8")


def _snapshot(*, status: str = "calculated", total: str | None = "70"):
    return SimpleNamespace(
        calculation_status=status,
        estimated_order_total_cost=(Decimal(total) if total is not None else None),
    )


def test_cost_health_uses_conservative_decimal_boundaries() -> None:
    incomplete = classify_estimated_cost_health(
        _snapshot(status="partial", total=None), "100"
    )
    assert incomplete["estimated_cost_health_code"] == "cost_incomplete"
    assert incomplete["estimated_margin_rate"] is None

    sale_missing = classify_estimated_cost_health(_snapshot(), "0")
    assert sale_missing["estimated_cost_health_code"] == "sale_missing"

    assert classify_estimated_cost_health(_snapshot(total="101"), "100")[
        "estimated_cost_health_code"
    ] == "estimated_loss"
    assert classify_estimated_cost_health(_snapshot(total="86"), "100")[
        "estimated_cost_health_code"
    ] == "very_low"
    assert classify_estimated_cost_health(_snapshot(total="85"), "100")[
        "estimated_cost_health_code"
    ] == "review"
    assert classify_estimated_cost_health(_snapshot(total="75"), "100")[
        "estimated_cost_health_code"
    ] == "healthy"


def test_health_is_advisory_and_ui_uses_plain_chinese() -> None:
    result = classify_estimated_cost_health(_snapshot(total="70"), "100")
    assert result["estimated_cost_health_blocks_save"] is False
    assert result["estimated_cost_health_version"] == "p1-28c2-health-v1"
    assert result["estimated_gross_profit"] == "30.00"
    assert result["estimated_margin_rate"] == "0.3000"
    for text in (
        "成本资料待完善",
        "预计亏损",
        "利润空间很低",
        "建议复核",
        "预计正常",
        "按订单录入售价试算，预计而非实际利润",
    ):
        assert text in (INDEX + Path(ROOT / "app/services/order_estimated_cost_snapshot.py").read_text(encoding="utf-8"))


def test_cost_health_is_returned_only_to_cost_permission(
    order_api_app, monkeypatch
) -> None:
    from app.models.product import Product
    from app.services import order_material_cost

    monkeypatch.setattr(
        order_material_cost,
        "get_effective_material_price",
        lambda _db, **kwargs: {
            "base_price": "2.0000",
            "flute_delta": "0",
            "effective_price": "2.0000",
            "rule_id": None,
            "supplier_name": kwargs.get("supplier_name"),
            "layer_count": kwargs.get("layer_count"),
            "flute_type": kwargs.get("flute_type"),
        },
    )
    app, session_factory = order_api_app
    with session_factory() as session:
        product = session.get(Product, 1)
        product.box_style = "A1"
        product.print_content = "单色印刷"
        product.report_length_mm = 500
        product.report_width_mm = 400
        session.commit()
    payload = _payload()
    payload["items"] = [payload["items"][0]]
    with TestClient(app) as client:
        _login(client)
        created = client.post("/api/orders", json=payload)
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["items"][0]["estimated_cost_health_version"] == "p1-28c2-health-v1"
        order_id = body["id"]

        _login(client, "sales")
        detail = client.get(f"/api/orders/{order_id}")
        assert detail.status_code == 200, detail.text
        assert "estimated_cost_health_code" not in detail.json()["items"][0]
