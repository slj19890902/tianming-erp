from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from test_p1_33c3_external_packaging_purchase_confirmation import purchase_app


__all__ = ["purchase_app"]


def _login(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/auth/login", json={"username": username, "password": "123456"}
    )
    assert response.status_code == 200, response.text


def _confirm(client: TestClient, order_id: int) -> dict:
    preview_response = client.get(
        f"/api/orders/{order_id}/external-packaging-purchase"
    )
    assert preview_response.status_code == 200, preview_response.text
    preview = preview_response.json()
    lines = []
    for index, row in enumerate(preview["items"]):
        lines.append(
            {
                "order_component_id": row["order_component_id"],
                "candidate_id": row["default_candidate_id"],
                "purchase_quantity": row["suggested_purchase_quantity"],
            }
        )
    response = client.post(
        f"/api/orders/{order_id}/external-packaging-purchase/confirm",
        json={"idempotency_key": "receiving-purchase", "lines": lines},
    )
    assert response.status_code == 200, response.text
    return response.json()["confirmation"]


def _pending(client: TestClient) -> dict:
    response = client.get("/api/external-packaging-purchases/pending-receipts")
    assert response.status_code == 200, response.text
    return response.json()


def _root_line(overview: dict) -> tuple[dict, dict]:
    for purchase in overview["purchase_orders"]:
        for line in purchase["items"]:
            if line["purchase_unit"] == "根":
                return purchase, line
    raise AssertionError("missing root purchase line")


def test_partial_then_complete_receipt_is_idempotent_and_creates_no_inventory(
    purchase_app: FastAPI,
) -> None:
    order_id = purchase_app.state.fixture["order_id"]
    with TestClient(purchase_app) as client:
        _login(client, "purchase-admin")
        _confirm(client, order_id)
        purchase, line = _root_line(_pending(client))
        assert line["ordered_quantity"] == "408"
        assert line["received_quantity"] == "0"
        assert line["remaining_quantity"] == "408"
        assert "unit_price" not in line
        assert "total_amount" not in line

        payload = {
            "idempotency_key": "receipt-root-40",
            "lines": [
                {
                    "purchase_item_id": line["purchase_item_id"],
                    "received_quantity": "40",
                }
            ],
        }
        first = client.post(
            f"/api/external-packaging-purchases/{purchase['id']}/receipts",
            json=payload,
        )
        retry = client.post(
            f"/api/external-packaging-purchases/{purchase['id']}/receipts",
            json=payload,
        )
        assert first.status_code == retry.status_code == 200
        assert first.json()["created"] is True
        assert retry.json()["created"] is False
        assert first.json()["receipt"]["items"][0] == {
            "purchase_item_id": line["purchase_item_id"],
            "received_quantity": "40",
            "purchase_unit": "根",
        }

        changed = json.loads(json.dumps(payload))
        changed["lines"][0]["received_quantity"] = "39"
        conflict = client.post(
            f"/api/external-packaging-purchases/{purchase['id']}/receipts",
            json=changed,
        )
        assert conflict.status_code == 409
        assert "其他内容" in conflict.text

        purchase_after, line_after = _root_line(_pending(client))
        assert purchase_after["status"] == "partially_received"
        assert line_after["received_quantity"] == "40"
        assert line_after["remaining_quantity"] == "368"

        over = client.post(
            f"/api/external-packaging-purchases/{purchase['id']}/receipts",
            json={
                "idempotency_key": "receipt-root-over",
                "lines": [
                    {
                        "purchase_item_id": line["purchase_item_id"],
                        "received_quantity": "369",
                    }
                ],
            },
        )
        assert over.status_code == 409
        assert "最多可收 368 根" in over.text

        complete = client.post(
            f"/api/external-packaging-purchases/{purchase['id']}/receipts",
            json={
                "idempotency_key": "receipt-root-60",
                "lines": [
                    {
                        "purchase_item_id": line["purchase_item_id"],
                        "received_quantity": "368",
                    }
                ],
            },
        )
        assert complete.status_code == 200, complete.text
        assert complete.json()["created"] is True
        assert all(
            row["purchase_item_id"] != line["purchase_item_id"]
            for order in _pending(client)["purchase_orders"]
            for row in order["items"]
        )

    with purchase_app.state.session_factory() as db:
        counts = {
            table: db.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
            for table in (
                "external_packaging_receipts",
                "external_packaging_receipt_items",
                "finished_goods_inventory_details",
                "semi_finished_inventory_details",
                "inventory_movements",
                "incoming_receipts",
            )
        }
        assert counts["external_packaging_receipts"] == 2
        assert counts["external_packaging_receipt_items"] == 2
        assert all(
            counts[name] == 0
            for name in (
                "finished_goods_inventory_details",
                "semi_finished_inventory_details",
                "inventory_movements",
                "incoming_receipts",
            )
        )


def test_partial_order_receives_open_target_and_later_target_closure_is_precise(
    purchase_app: FastAPI,
) -> None:
    from app.models.order import Order, OrderItem

    order_id = purchase_app.state.fixture["order_id"]
    with TestClient(purchase_app) as client:
        _login(client, "purchase-admin")
        _confirm(client, order_id)
        purchase, line = _root_line(_pending(client))

        with purchase_app.state.session_factory() as db:
            order = db.get(Order, order_id)
            target = db.scalar(
                select(OrderItem).where(OrderItem.order_id == order_id)
            )
            assert order is not None and target is not None
            order.status = "partially_delivered"
            db.commit()

        payload = {
            "idempotency_key": "p1101-partial-external-receipt",
            "lines": [
                {
                    "purchase_item_id": line["purchase_item_id"],
                    "received_quantity": "1",
                }
            ],
        }
        first = client.post(
            f"/api/external-packaging-purchases/{purchase['id']}/receipts",
            json=payload,
        )
        assert first.status_code == 200, first.text
        assert first.json()["created"] is True

        with purchase_app.state.session_factory() as db:
            target = db.scalar(
                select(OrderItem).where(OrderItem.order_id == order_id)
            )
            assert target is not None
            target.delivered_quantity = target.quantity
            db.commit()

        assert _pending(client)["purchase_orders"] == []
        replay = client.post(
            f"/api/external-packaging-purchases/{purchase['id']}/receipts",
            json=payload,
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["created"] is False

        blocked_payload = json.loads(json.dumps(payload))
        blocked_payload["idempotency_key"] = "p1101-closed-target-external-receipt"
        blocked = client.post(
            f"/api/external-packaging-purchases/{purchase['id']}/receipts",
            json=blocked_payload,
        )
        assert blocked.status_code == 409, blocked.text
        assert "订单明细已全部送货" in blocked.text


def test_multi_line_receipt_is_atomic_and_original_units_are_separate(
    purchase_app: FastAPI,
) -> None:
    order_id = purchase_app.state.fixture["order_id"]
    with TestClient(purchase_app) as client:
        _login(client, "purchase-admin")
        _confirm(client, order_id)
        overview = _pending(client)
        purchase = next(row for row in overview["purchase_orders"] if len(row["items"]) == 2)
        first, second = purchase["items"]
        response = client.post(
            f"/api/external-packaging-purchases/{purchase['id']}/receipts",
            json={
                "idempotency_key": "receipt-atomic-over",
                "lines": [
                    {"purchase_item_id": first["purchase_item_id"], "received_quantity": "1"},
                    {
                        "purchase_item_id": second["purchase_item_id"],
                        "received_quantity": str(float(second["remaining_quantity"]) + 1),
                    },
                ],
            },
        )
        assert response.status_code == 409

    with purchase_app.state.session_factory() as db:
        assert db.execute(text("SELECT COUNT(*) FROM external_packaging_receipts")).scalar_one() == 0
        assert db.execute(text("SELECT COUNT(*) FROM external_packaging_receipt_items")).scalar_one() == 0


def test_receiving_permissions_and_frontend_contract(purchase_app: FastAPI) -> None:
    order_id = purchase_app.state.fixture["order_id"]
    with TestClient(purchase_app) as client:
        _login(client, "purchase-admin")
        _confirm(client, order_id)
        purchase, line = _root_line(_pending(client))
        _login(client, "purchase-sales")
        assert client.get(
            "/api/external-packaging-purchases/pending-receipts"
        ).status_code == 403
        response = client.post(
            f"/api/external-packaging-purchases/{purchase['id']}/receipts",
            json={
                "idempotency_key": "sales-denied",
                "lines": [
                    {"purchase_item_id": line["purchase_item_id"], "received_quantity": "1"}
                ],
            },
        )
        assert response.status_code == 403

    source = Path("static/index.html").read_text(encoding="utf-8")
    assert "外购包装待收" in source
    assert "确认本次收料" in source
    assert "这里只记实收，不增加纸板或成品库存" in source
    assert "/api/external-packaging-purchases/pending-receipts" in source
    assert "receiveExternalPurchase" in source
