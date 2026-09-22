"""Read-path optimization guards using new synthetic, isolated databases."""
from __future__ import annotations

import json
import os
from pathlib import Path
from time import perf_counter

from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload
import pytest

from test_phase3_scale_reliability import _phase3_scale_app
from app.services import order_business_status as status_service
from app.models.order import Order
from tests.test_semi_finished_order_reservation import (
    b1_app, add_semi_lot, login, post_order, order_item, semi_plan,
)


def _get(client, *, customer_id=None):
    params = {"scope": "active", "detail_level": "summary", "page": 1, "page_size": 20}
    if customer_id is not None:
        params["customer_id"] = customer_id
    response = client.get("/api/orders", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_unfiltered_active_list_reuses_same_scoped_snapshot(tmp_path):
    fixture = _phase3_scale_app(tmp_path, (100,))
    app, engine, customer_ids = next(fixture)
    statements = []
    def capture(_c, _cursor, sql, _params, _context, _many):
        statements.append(sql)
    event.listen(engine, "before_cursor_execute", capture)
    try:
        status_service._clear_active_status_badge_cache()
        with TestClient(app) as client:
            cold = _get(client)
            statements.clear()
            warm = _get(client)
            warm_count = len(statements)
            filtered = _get(client, customer_id=customer_ids[100])
        assert cold == warm == filtered
        assert cold["total"] == cold["unfinished_total"] == 100
        assert warm_count <= 12, f"unfiltered warm read rebuilt status: {warm_count} SQL"
    finally:
        event.remove(engine, "before_cursor_execute", capture)
        fixture.close()


def test_cold_projection_avoids_loading_non_page_order_item_details(tmp_path, monkeypatch):
    fixture = _phase3_scale_app(tmp_path, (5000,))
    app, engine, customer_ids = next(fixture)
    statements = []
    def capture(_c, _cursor, sql, params, _context, _many):
        statements.append((sql, params))
    event.listen(engine, "before_cursor_execute", capture)
    if os.environ.get("TM_AUDIT_PROFILE"):
        import cProfile
        import pstats
        from app.api import orders as orders_api
        original = orders_api.active_order_status_badge_snapshot
        def profiled(*args, **kwargs):
            with cProfile.Profile() as profile:
                result = original(*args, **kwargs)
            if not result[2]:
                folder = Path(os.environ["TM_PHASE3_ASTRA_RESULT_DIR"])
                folder.mkdir(parents=True, exist_ok=True)
                with (folder / "projection-profile.txt").open("w", encoding="utf-8") as output:
                    pstats.Stats(profile, stream=output).sort_stats("cumulative").print_stats(35)
            return result
        monkeypatch.setattr(orders_api, "active_order_status_badge_snapshot", profiled)
    try:
        measurements = []
        with TestClient(app) as client:
            for mode in ("cold", "hot"):
                if mode == "cold":
                    status_service._clear_active_status_badge_cache()
                statements.clear()
                start = perf_counter()
                payload = _get(client, customer_id=customer_ids[5000])
                elapsed_ms = (perf_counter() - start) * 1000
                measurements.append({"mode": mode, "elapsed_ms": elapsed_ms, "sql_count": len(statements)})
                if mode == "cold":
                    # Page summaries need quantity/status, not thousands of
                    # drawing/material/price snapshot columns for hidden rows.
                    cold_item_selects = [(s, p) for s, p in statements if "FROM sales_order_items" in s]
        target = os.environ.get("TM_PHASE3_ASTRA_RESULT_DIR")
        if target:
            folder = Path(target); folder.mkdir(parents=True, exist_ok=True)
            (folder / "order-projection-5000.json").write_text(json.dumps(measurements, indent=2), encoding="utf-8")
        assert payload["total"] == payload["unfinished_total"] == 5000
        assert len(payload["items"]) == 20
        assert cold_item_selects
        wide = [(s, p) for s, p in cold_item_selects if "sales_order_items.drawing_file" in s.split("FROM", 1)[0]]
        assert len(wide) <= 1 and all(len(p) <= 20 for _, p in wide), (
            f"full detail must be limited to the visible page, found {len(wide)} batches"
        )
        # Compare every field of every projected item to the unchanged rule
        # engine with fully loaded ORM facts, not just totals or timing.
        with Session(engine) as db:
            orders = list(db.scalars(select(Order).options(selectinload(Order.items))))
            expected = status_service.build_order_business_statuses(db, orders)
            snapshot, _, _ = status_service.active_order_status_badge_snapshot(
                db, scoped_customer_ids=None, include_finance=True,
            )
            assert snapshot.projections == expected
    finally:
        event.remove(engine, "before_cursor_execute", capture)
        fixture.close()


@pytest.mark.parametrize("style,reserved", [("衬板", 5), ("普通箱", 5), ("衬板", 3)])
def test_lightweight_status_inputs_preserve_direct_liner_eligibility(b1_app, style, reserved):
    from app.models.product import Product
    from app.models.warehouse_inventory import InventoryLot
    app, factory = b1_app
    lot_id, version = add_semi_lot(factory, quantity=12, key="audit-liner")
    with factory() as db:
        product = db.get(Product, 1)
        product.box_style = style
        product.crease_type = "净料"
        db.get(InventoryLot, lot_id).semi_finished_detail.crease_type = "净料"
        db.commit()
    with TestClient(app) as client:
        login(client)
        response = post_order(client, [order_item(1, 5, {"semi": [semi_plan(lot_id, version, reserved)]})], "AUDIT-LINER")
    assert response.status_code == 201, response.text
    with factory() as db:
        full = list(db.scalars(select(Order).options(selectinload(Order.items))))
        lightweight = status_service._status_read_rows(db, db.execute(select(Order.id, Order.status)).all())
        assert status_service.build_order_business_statuses(db, lightweight) == status_service.build_order_business_statuses(db, full)
