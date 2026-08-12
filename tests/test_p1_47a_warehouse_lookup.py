from __future__ import annotations

from datetime import date

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.models.audit import OperationLog
from app.services.warehouse_inventory import manual_finished_in
from test_floor3_locations_api import _login, floor3_app


def _inbound(
    db,
    *,
    location_id: int,
    customer_id: int,
    product_id: int,
    quantity: int,
    key: str,
) -> None:
    manual_finished_in(
        db,
        customer_id=customer_id,
        product_id=product_id,
        location_id=location_id,
        quantity=quantity,
        stock_date=date(2026, 8, 12),
        source_type="stocktake",
        remarks="P1-47A统一查货测试",
        operator_id=None,
        idempotency_key=key,
    )


def test_p1_47a_unified_lookup_matches_business_terms_and_keeps_scope_read_only(
    floor3_app,
) -> None:
    app, ids, factory = floor3_app
    with factory() as db:
        _inbound(
            db,
            location_id=ids["formal_location"],
            customer_id=ids["tianhua"],
            product_id=ids["products"][0],
            quantity=5,
            key="p1-47a-lookup-1f",
        )
        _inbound(
            db,
            location_id=ids["rack_location"],
            customer_id=ids["tianhua"],
            product_id=ids["products"][0],
            quantity=8,
            key="p1-47a-lookup-3f",
        )
        _inbound(
            db,
            location_id=ids["legacy_floor3_location"],
            customer_id=ids["other"],
            product_id=ids["other_product"],
            quantity=7,
            key="p1-47a-lookup-hidden",
        )
        db.commit()

    with TestClient(app) as client:
        _login(client, "floor3-scoped")
        with factory() as db:
            before_logs = db.scalar(select(func.count()).select_from(OperationLog))

        expected_ids: set[int] | None = None
        for keyword in ("天华", "TH", "2130101", "同名纸箱", "101*80*50mm"):
            response = client.get(
                "/api/warehouse/twin-operations/locate",
                params={"search_type": "finished", "keyword": keyword},
            )
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["resources"] == []
            assert payload["inventory_result_count"] == 2
            assert {item["floor_code"] for item in payload["items"]} == {"1F", "3F"}
            assert {item["customer_id"] for item in payload["items"]} == {ids["tianhua"]}
            current_ids = {item["lot_id"] for item in payload["items"]}
            expected_ids = expected_ids or current_ids
            assert current_ids == expected_ids

        hidden = client.get(
            "/api/warehouse/twin-operations/locate",
            params={"search_type": "finished", "keyword": "QT"},
        )
        assert hidden.status_code == 200, hidden.text
        assert hidden.json()["items"] == []

        with factory() as db:
            after_logs = db.scalar(select(func.count()).select_from(OperationLog))
        assert after_logs == before_logs
