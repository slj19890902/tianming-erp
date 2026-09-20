"""Merged procurement: opt-in scope, explicit lot choice and retry safety."""
import json
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select, event

from app.models.warehouse_goods import WarehouseGoodsProfile
from app.models.product import Product
from app.models.warehouse_inventory import InventoryLot, InventoryReservation
from app.models.audit import OperationLog
from test_semi_finished_order_reservation import b1_app, login, post_order, order_item, add_semi_lot


def make_pending(client, key="UI-PENDING"):
    response = post_order(client, [order_item(1, 20, None, key)], key)
    assert response.status_code == 201, response.text
    return response.json()["items"][0]["id"]


def preview(client, item_id):
    response = client.post("/api/requisition/supplier-orders/preview-from-pending-selection", json={
        "selections": [{"type": "order_item", "order_item_id": item_id,
                        "supplier_name": "B1-SUPPLIER", "report_length_mm": 800, "report_width_mm": 600}]})
    assert response.status_code == 200, response.text
    return response.json()["supplier_groups"][0]["lines"][0]["source_items"][0]["late_semi_inventory_options"][0]


def test_default_sql_bound_manual_paged_and_read_only(b1_app):
    app, factory = b1_app
    with TestClient(app) as client:
        login(client)
        item_id = make_pending(client)
        own, _ = add_semi_lot(factory, quantity=8, key="ui-own")
        general = [add_semi_lot(factory, quantity=3, key=f"ui-general-{n}", customer_id=None, bind_product=False)[0] for n in range(12)]
        other, _ = add_semi_lot(factory, quantity=5, key="ui-other", customer_id=2, bind_product=False)
        queries = []
        engine = factory.kw["bind"]
        def observe(conn, cursor, statement, parameters, context, many):
            if statement.lstrip().startswith("SELECT") and "JOIN semi_finished_inventory_details" in statement:
                queries.append(statement)
        event.listen(engine, "before_cursor_execute", observe)
        try:
            option = preview(client, item_id)
        finally:
            event.remove(engine, "before_cursor_execute", observe)
        candidates = option["recommended_candidates"] + option["review_candidates"]
        assert {row["lot_id"] for row in candidates} == {own}
        assert queries and all("bound_customers" in sql for sql in queries)
        seen = set()
        for page in (1, 2):
            response = client.get(f"/api/requisition/pending/{item_id}/semi-inventory-options", params={"page": page})
            assert response.status_code == 200, response.text
            assert len(response.json()["candidates"]) <= 10
            for row in response.json()["candidates"]:
                seen.add(row["lot_id"])
                if row["lot_id"] == other:
                    assert row["selectable"] is False
        assert set(general + [own, other]) <= seen
        with factory() as db:
            assert not db.scalars(select(InventoryReservation)).all()
            assert db.get(InventoryLot, own).quantity_available == 8


def test_explicit_lot_full_cover_replay_and_changed_payload_rejected(b1_app):
    app, factory = b1_app
    with TestClient(app) as client:
        login(client)
        item_id = make_pending(client)
        chosen, version = add_semi_lot(factory, quantity=20, key="ui-chosen")
        untouched, _ = add_semi_lot(factory, quantity=30, key="ui-untouched")
        payload = dict(order_item_id=item_id, component_type="whole", requested_requirement_quantity=20,
                       lots=[dict(lot_id=chosen, expected_version=version)], idempotency_key="ui-retry")
        url = "/api/requisition/semi-inventory/reserve-from-pending"
        first = client.post(url, json=payload)
        assert first.status_code == 200, first.text
        again = client.post(url, json=payload)
        assert again.status_code == 200 and again.json() == first.json(), again.text
        different = client.post(url, json={**payload, "requested_requirement_quantity": 19})
        assert different.status_code == 409
        with factory() as db:
            assert db.get(InventoryLot, untouched).quantity_available == 30
            assert db.get(InventoryLot, chosen).quantity_reserved == 20
            assert len(db.scalars(select(InventoryReservation)).all()) == 1
            assert len(db.scalars(select(OperationLog).where(OperationLog.action == "RESERVE_LATE_SEMI_INVENTORY_FROM_REQUISITION")).all()) == 1


def test_cut_stock_is_confirmed_as_requisition_material_plan_without_purchase(b1_app):
    app, factory = b1_app
    with factory() as db:
        product = db.get(Product, 1)
        assert product is not None
        product.box_style = "衬板"
        product.length_mm = product.report_length_mm = 1000
        product.width_mm = product.report_width_mm = 200
        db.commit()
    chosen, version = add_semi_lot(
        factory,
        quantity=10,
        key="ui-cut-material-plan",
        length=1120,
        width=440,
    )
    with factory() as db:
        db.add(
            WarehouseGoodsProfile(
                lot_id=chosen,
                data_json=json.dumps(
                    {
                        "scope": "customers",
                        "customer_ids": [1],
                        "product_ids": [1],
                        "processing": "cut",
                        "material_confidence": "confirmed",
                        "verified_material_id": None,
                        "material_code": "A416D",
                        "face_paper": "kraft",
                        "mold_tool_id": None,
                    }
                ),
            )
        )
        db.commit()
    with TestClient(app) as client:
        login(client)
        item_id = make_pending(client, "UI-CUT-MATERIAL-PLAN")
        options = client.get(
            f"/api/requisition/pending/{item_id}/semi-inventory-options"
        )
        assert options.status_code == 200, options.text
        candidate = next(
            row for row in options.json()["candidates"] if row["lot_id"] == chosen
        )
        assert candidate["requires_requisition_cut_plan"] is True
        assert candidate["handling_stage"] == "requisition"
        assert candidate["cut_plan"]["yield_factor"] == 2
        response = client.post(
            "/api/requisition/semi-inventory/reserve-from-pending",
            json={
                "order_item_id": item_id,
                "component_type": "whole",
                "requested_requirement_quantity": 20,
                "lots": [{"lot_id": chosen, "expected_version": version}],
                "override": bool(candidate["signature_differences"]),
                "warning_acknowledged_codes": candidate["warning_codes"],
                "idempotency_key": "ui-cut-material-plan-reserve",
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["material_plan_confirmed"] is True
        assert response.json()["requisition_qty"] == 0
    with factory() as db:
        lot = db.get(InventoryLot, chosen)
        assert lot is not None and lot.quantity_available == 0 and lot.quantity_reserved == 10
        reservation = db.scalar(select(InventoryReservation).where(InventoryReservation.order_item_id == item_id))
        assert reservation is not None and reservation.cut_plan_json is not None
        assert json.loads(reservation.cut_plan_json)["yield_factor"] == 2


def test_stale_version_and_audit_failure_rollback(b1_app, monkeypatch):
    app, factory = b1_app
    with TestClient(app, raise_server_exceptions=False) as client:
        login(client)
        item_id = make_pending(client)
        chosen, version = add_semi_lot(factory, quantity=20, key="ui-stale")
        with factory() as db:
            db.get(InventoryLot, chosen).version += 1
            db.commit()
        payload = dict(order_item_id=item_id, component_type="whole", requested_requirement_quantity=10,
                       lots=[dict(lot_id=chosen, expected_version=version)], idempotency_key="ui-stale-retry")
        url = "/api/requisition/semi-inventory/reserve-from-pending"
        assert client.post(url, json=payload).status_code == 409
        def fail(*args, **kwargs):
            raise RuntimeError("injected audit failure")
        monkeypatch.setattr("app.api.requisition._audit", fail)
        payload["lots"][0]["expected_version"] = version + 1
        assert client.post(url, json=payload).status_code == 500
        with factory() as db:
            assert db.get(InventoryLot, chosen).quantity_available == 20
            assert not db.scalars(select(InventoryReservation)).all()


def test_manual_query_respects_customer_permission(b1_app):
    from app.models.user import User
    from app.models.access_control import UserCustomerScope
    app, factory = b1_app
    with TestClient(app) as client:
        login(client)
        item_id = make_pending(client)
        own, _ = add_semi_lot(factory, quantity=8, key="scoped-own")
        other, _ = add_semi_lot(factory, quantity=5, key="scoped-other", customer_id=2, bind_product=False)
        with factory() as db:
            user = db.scalar(select(User).where(User.username == "sales"))
            user.customer_access_mode = "selected"
            db.add(UserCustomerScope(user_id=user.id, customer_id=1))
            db.commit()
        client.cookies.clear()
        login(client, "sales")
        response = client.get(f"/api/requisition/pending/{item_id}/semi-inventory-options")
        assert response.status_code == 200, response.text
        ids = {row["lot_id"] for row in response.json()["candidates"]}
        assert own in ids and other not in ids


def test_concurrent_same_key_reserves_once(b1_app):
    from concurrent.futures import ThreadPoolExecutor
    app, factory = b1_app
    with TestClient(app) as client:
        login(client)
        item_id = make_pending(client)
        chosen, version = add_semi_lot(factory, quantity=20, key="concurrent-chosen")
        payload = dict(order_item_id=item_id, component_type="whole", requested_requirement_quantity=20,
                       lots=[dict(lot_id=chosen, expected_version=version)], idempotency_key="concurrent-key")
        cookies = dict(client.cookies)
        def send():
            with TestClient(app) as worker:
                worker.cookies.update(cookies)
                return worker.post("/api/requisition/semi-inventory/reserve-from-pending", json=payload)
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(lambda _: send(), range(2)))
        assert [r.status_code for r in results] == [200, 200], [r.text for r in results]
        assert results[0].json() == results[1].json()
        with factory() as db:
            assert db.get(InventoryLot, chosen).quantity_reserved == 20
            assert len(db.scalars(select(InventoryReservation)).all()) == 1


def test_insufficient_selected_quantity_does_not_partially_reserve(b1_app):
    app, factory = b1_app
    with TestClient(app) as client:
        login(client)
        item_id = make_pending(client)
        lot, version = add_semi_lot(factory, quantity=5, key="ui-insufficient")
        response = client.post("/api/requisition/semi-inventory/reserve-from-pending", json={
            "order_item_id": item_id, "component_type": "whole", "requested_requirement_quantity": 10,
            "lots": [{"lot_id": lot, "expected_version": version}], "idempotency_key": "ui-insufficient-key"})
        assert response.status_code == 409, response.text
        with factory() as db:
            assert db.get(InventoryLot, lot).quantity_available == 5
            assert not db.scalars(select(InventoryReservation)).all()


def test_frontend_behavior():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(["node", "tests/requisition_deduction_ui.cjs"], cwd=root, capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
