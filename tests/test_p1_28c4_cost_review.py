from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from tests.test_phase5_orders import _login, _payload, order_api_app


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static/index.html").read_text(encoding="utf-8")


def _single_item_payload(customer_po: str, *, unit_price: str = "1") -> dict:
    payload = _payload()
    payload["customer_po"] = customer_po
    payload["items"] = [
        {"product_id": 1, "quantity": 100, "unit_price": unit_price}
    ]
    return payload


def test_cost_review_reuses_health_boundaries_and_stays_read_only(
    order_api_app,
    monkeypatch,
) -> None:
    from app.api import orders as orders_api
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

    created: dict[str, dict] = {}
    with TestClient(app) as client:
        _login(client)
        for code, unit_price in (
            ("LOSS", "1"),
            ("VERY-LOW", "1"),
            ("REVIEW", "1"),
            ("SALE-MISSING", "0"),
            ("HEALTHY", "1"),
            ("CANCELLED", "1"),
            ("OLD", "1"),
            ("COMPLETED", "1"),
        ):
            response = client.post(
                "/api/orders",
                json=_single_item_payload(
                    f"PO-COST-REVIEW-{code}", unit_price=unit_price
                ),
            )
            assert response.status_code == 201, response.text
            created[code] = response.json()

        incomplete_payload = _payload()
        incomplete_payload["customer_po"] = "PO-COST-REVIEW-INCOMPLETE"
        incomplete_payload["items"] = [incomplete_payload["items"][1]]
        incomplete = client.post("/api/orders", json=incomplete_payload)
        assert incomplete.status_code == 201, incomplete.text

        target_costs = {
            "LOSS": Decimal("101"),
            "VERY-LOW": Decimal("86"),
            "REVIEW": Decimal("85"),
            "SALE-MISSING": Decimal("75"),
            "HEALTHY": Decimal("75"),
            "CANCELLED": Decimal("101"),
            "COMPLETED": Decimal("101"),
        }
        with session_factory() as session:
            for code, total in target_costs.items():
                item_id = int(created[code]["items"][0]["id"])
                snapshot = session.scalar(
                    select(SalesOrderItemEstimatedCostSnapshot)
                    .where(
                        SalesOrderItemEstimatedCostSnapshot.sales_order_item_id
                        == item_id
                    )
                    .order_by(
                        SalesOrderItemEstimatedCostSnapshot.snapshot_version.desc()
                    )
                )
                assert snapshot is not None
                snapshot.calculation_status = "calculated"
                snapshot.estimated_order_total_cost = total
                snapshot.estimated_unit_total_cost = total / Decimal("100")
            cancelled_order = session.get(Order, created["CANCELLED"]["id"])
            assert cancelled_order is not None
            cancelled_order.status = "cancelled"
            old_item_id = int(created["OLD"]["items"][0]["id"])
            session.execute(
                delete(SalesOrderItemEstimatedCostSnapshot).where(
                    SalesOrderItemEstimatedCostSnapshot.sales_order_item_id
                    == old_item_id
                )
            )
            session.commit()
            snapshot_count = session.scalar(
                select(func.count()).select_from(
                    SalesOrderItemEstimatedCostSnapshot
                )
            )

        original_builder = orders_api.build_order_business_statuses

        def mark_completed(db, orders, **kwargs):
            projections = original_builder(db, orders, **kwargs)
            for order in orders:
                if order.customer_po == "PO-COST-REVIEW-COMPLETED":
                    projections[int(order.id)]["business_status"] = "completed"
            return projections

        monkeypatch.setattr(
            orders_api,
            "build_order_business_statuses",
            mark_completed,
        )

        response = client.get("/api/orders/cost-review", params={"limit": 20})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["evaluated_items"] == 5
        assert body["total_items"] == 4
        assert body["returned_items"] == 4
        assert body["truncated"] is False
        assert [row["health_code"] for row in body["items"]] == [
            "estimated_loss",
            "very_low",
            "review",
            "sale_missing",
        ]
        assert [row["health_version"] for row in body["items"]] == [
            "p1-28c2-health-v1"
        ] * 4
        assert {row["customer_po"] for row in body["items"]} == {
            "PO-COST-REVIEW-LOSS",
            "PO-COST-REVIEW-VERY-LOW",
            "PO-COST-REVIEW-REVIEW",
            "PO-COST-REVIEW-SALE-MISSING",
        }
        assert "PO-COST-REVIEW-HEALTHY" not in {
            row["customer_po"] for row in body["items"]
        }
        summary = {row["code"]: row["count"] for row in body["summary"]}
        assert summary == {
            "estimated_loss": 1,
            "very_low": 1,
            "review": 1,
            "sale_missing": 1,
            "healthy": 1,
        }
        assert "实际" in body["scope_label"]

        limited = client.get("/api/orders/cost-review", params={"limit": 2})
        assert limited.status_code == 200, limited.text
        assert limited.json()["total_items"] == 4
        assert limited.json()["returned_items"] == 2
        assert limited.json()["truncated"] is True

        paged = client.get("/api/orders/cost-review", params={"page":2,"page_size":2}).json()
        assert [row["health_code"] for row in paged["items"]] == ["review","sale_missing"]
        assert paged["page_count"] == 2 and paged["all_items"] == 4
        assert paged["truncated"] is False
        selected = client.get("/api/orders/cost-review", params={"health":"sale_missing","page_size":1}).json()
        assert selected["total_items"] == 1 and selected["all_items"] == 4
        assert selected["items"][0]["health_code"] == "sale_missing"
        assert "15" in selected["threshold_label"] and "25" in selected["threshold_label"]
        assert client.get("/api/orders/cost-review",params={"keyword":"VERY-LOW","page_size":1}).json()["total_items"] == 1
        assert client.get("/api/orders/cost-review",params={"keyword":"%","page_size":1}).json()["total_items"] == 0
        assert client.get("/api/orders/cost-review",params={"health":"healthy"}).status_code == 422
        assert client.get("/api/orders/cost-review",params={"page":999,"page_size":2}).json()["page"] == 2

        _login(client, "sales")
        forbidden = client.get("/api/orders/cost-review")
        assert forbidden.status_code == 403
        assert "PO-COST-REVIEW-LOSS" not in forbidden.text

        _login(client, "finance")
        empty_scope = client.get("/api/orders/cost-review")
        assert empty_scope.status_code == 200, empty_scope.text
        assert empty_scope.json()["total_items"] == 0

        with session_factory() as session:
            finance = session.scalar(select(User).where(User.username == "finance"))
            assert finance is not None
            session.add(UserCustomerScope(user_id=finance.id, customer_id=1))
            session.commit()
        allowed_scope = client.get("/api/orders/cost-review")
        assert allowed_scope.status_code == 200, allowed_scope.text
        assert allowed_scope.json()["total_items"] == 4

    with session_factory() as session:
        assert session.scalar(
            select(func.count()).select_from(SalesOrderItemEstimatedCostSnapshot)
        ) == snapshot_count


def test_cost_review_ui_is_one_lazy_read_only_cost_check_panel() -> None:
    for text in (
        "预计成本与毛利提醒",
        "待补资料",
        "利润复核",
        "仅供内部复核，不是实际利润",
        'axios.get("/api/orders/cost-review"',
    ):
        assert text in INDEX
    assert INDEX.count('@click="openCostGaps">预计成本与毛利提醒</button>') == 1
    assert "Promise.all([" in INDEX
    assert "switchCostPanel('review')" in INDEX
    assert "loadCostReview" in INDEX
    assert "costReviewState.summary" in INDEX
    assert "row.health_label" in INDEX
    assert "openCostGapOrder" in INDEX
    assert "保存利润" not in INDEX
    assert "自动调价" not in INDEX
