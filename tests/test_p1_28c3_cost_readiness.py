from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from app.services.order_cost_readiness import (
    classify_cost_gaps,
    load_cost_missing_items,
)
from tests.test_phase5_orders import _login, _payload, order_api_app


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static/index.html").read_text(encoding="utf-8")


def test_cost_gap_categories_are_stable_and_unknown_text_is_kept() -> None:
    missing = [
        "父件缺少有效平方成本",
        "父件缺少报料长宽",
        "父件缺少有效供应商材质",
        "该箱型加工费规则待完善",
        "印刷颜色数量待确认，未计额外颜色费",
        "外购件采购成本未纳入本版预计总成本",
        "子件需求数量无效",
        "材料成本快照缺失",
        "需要人工核对的未知资料",
    ]
    categories = classify_cost_gaps(missing)
    assert [row["code"] for row in categories] == [
        "supplier_price",
        "report_dimensions",
        "supplier_material",
        "processing_rule",
        "printing_colors",
        "external_purchase_cost",
        "required_quantity",
        "material_snapshot",
        "other",
    ]
    assert categories[-1]["details"] == ["需要人工核对的未知资料"]
    assert load_cost_missing_items("not-json") == ["成本资料需要核对"]


def test_cost_readiness_is_read_only_current_and_permissioned(
    order_api_app,
    monkeypatch,
) -> None:
    from app.models.access_control import UserCustomerScope
    from app.models.order import Order
    from app.models.order_estimated_cost_snapshot import (
        SalesOrderItemEstimatedCostSnapshot,
    )
    from app.models.product import Product
    from app.models.user import User
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
        assert product is not None
        product.box_style = "A1"
        product.print_content = "单色印刷"
        product.report_length_mm = 500
        product.report_width_mm = 400
        finance = session.scalar(select(User).where(User.username == "finance"))
        assert finance is not None
        finance.customer_access_mode = "selected"
        session.commit()

    with TestClient(app) as client:
        _login(client)

        incomplete_payload = _payload()
        incomplete_payload["customer_po"] = "PO-COST-GAP-ACTIVE"
        incomplete_payload["items"] = [incomplete_payload["items"][1]]
        active = client.post("/api/orders", json=incomplete_payload)
        assert active.status_code == 201, active.text

        second_active_payload = _payload()
        second_active_payload["customer_po"] = "PO-COST-GAP-ACTIVE-2"
        second_active_payload["items"] = [second_active_payload["items"][1]]
        second_active = client.post("/api/orders", json=second_active_payload)
        assert second_active.status_code == 201, second_active.text

        cancelled_payload = _payload()
        cancelled_payload["customer_po"] = "PO-COST-GAP-CANCELLED"
        cancelled_payload["items"] = [cancelled_payload["items"][1]]
        cancelled = client.post("/api/orders", json=cancelled_payload)
        assert cancelled.status_code == 201, cancelled.text

        old_payload = _payload()
        old_payload["customer_po"] = "PO-COST-GAP-OLD"
        old_payload["items"] = [old_payload["items"][1]]
        old = client.post("/api/orders", json=old_payload)
        assert old.status_code == 201, old.text

        complete_payload = _payload()
        complete_payload["customer_po"] = "PO-COST-COMPLETE"
        complete_payload["items"] = [complete_payload["items"][0]]
        complete = client.post("/api/orders", json=complete_payload)
        assert complete.status_code == 201, complete.text
        assert complete.json()["items"][0]["estimated_total_cost_status"] == "calculated"

        with session_factory() as session:
            cancelled_order = session.get(Order, cancelled.json()["id"])
            assert cancelled_order is not None
            cancelled_order.status = "cancelled"
            old_item_id = int(old.json()["items"][0]["id"])
            session.execute(
                delete(SalesOrderItemEstimatedCostSnapshot).where(
                    SalesOrderItemEstimatedCostSnapshot.sales_order_item_id
                    == old_item_id
                )
            )
            snapshot_count = session.scalar(
                select(func.count()).select_from(
                    SalesOrderItemEstimatedCostSnapshot
                )
            )
            session.commit()

        response = client.get("/api/orders/cost-readiness", params={"limit": 20})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["total_items"] == 2
        assert body["returned_items"] == 2
        assert body["truncated"] is False
        assert {row["order_id"] for row in body["items"]} == {
            active.json()["id"],
            second_active.json()["id"],
        }
        assert {row["customer_po"] for row in body["items"]} == {
            "PO-COST-GAP-ACTIVE",
            "PO-COST-GAP-ACTIVE-2",
        }
        assert "estimated_order_total_cost" not in body["items"][0]
        category_codes = {row["code"] for row in body["categories"]}
        assert {"report_dimensions", "supplier_material", "processing_rule"} <= category_codes
        limited = client.get("/api/orders/cost-readiness", params={"limit": 1})
        assert limited.status_code == 200, limited.text
        assert limited.json()["total_items"] == 2
        assert limited.json()["returned_items"] == 1
        assert limited.json()["truncated"] is True

        _login(client, "sales")
        forbidden = client.get("/api/orders/cost-readiness")
        assert forbidden.status_code == 403
        assert "PO-COST-GAP-ACTIVE" not in forbidden.text

        _login(client, "finance")
        empty_scope = client.get("/api/orders/cost-readiness")
        assert empty_scope.status_code == 200, empty_scope.text
        assert empty_scope.json()["total_items"] == 0

        with session_factory() as session:
            finance = session.scalar(select(User).where(User.username == "finance"))
            assert finance is not None
            session.add(UserCustomerScope(user_id=finance.id, customer_id=1))
            session.commit()
        allowed_scope = client.get("/api/orders/cost-readiness")
        assert allowed_scope.status_code == 200, allowed_scope.text
        assert allowed_scope.json()["total_items"] == 2

    with session_factory() as session:
        assert session.scalar(
            select(func.count()).select_from(SalesOrderItemEstimatedCostSnapshot)
        ) == snapshot_count


def test_cost_gap_ui_is_lazy_compact_and_has_no_write_action() -> None:
    for text in (
        "成本缺口",
        "成本资料缺口",
        "仅统计已有预计成本快照的当前订单；旧订单不回填",
        "需要补齐",
        "查看订单",
        'axios.get("/api/orders/cost-readiness"',
    ):
        assert text in INDEX
    assert 'v-if="canViewCosts" ref="costGapTrigger" class="btn" @click="openCostGaps"' in INDEX
    assert "成本与利润提醒" in INDEX
    assert "/api/orders/cost-readiness" in INDEX
    assert "openCostGapOrder" in INDEX
