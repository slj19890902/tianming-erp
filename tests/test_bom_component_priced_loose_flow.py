"""One commercial order: loose stock, material conversion, receipts and settlement."""
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models.product import Product
from app.models.user import User
from app.models.order import OrderItem
from app.models.warehouse_inventory import InventoryLot
from app.services.multilevel_bom_orders import read_compiled_order_bom
from tests.test_multilevel_bom_receipt_flow import seed_graph, read_purchase_sources
from tests.test_multilevel_bom_master import save
from tests.test_n039_composite_bom_requisition import composite_requisition_app, _login, _component_payload
from tests.test_p1_81_receipt_purpose_flow import _p181_published_map_identity, _freeze_receipt_fact, _receive
from tests.test_bom_commercial_settlement import _dispatch, _confirm_receipt


def test_component_order_loose_stock_to_receipts_delivery_and_statement(
    composite_requisition_app, _p181_published_map_identity,
):
    from app.api.deliveries import router as delivery_router
    from app.api.finance import router as finance_router
    from app.api import products as products_api
    from fastapi.encoders import jsonable_encoder
    from app.core.time_contract import beijing_today
    from app.services.production_workflow import _receipt_auto_finished_ground_target
    from app.services.warehouse_inventory import manual_finished_in
    from app.models.finance import Statement
    from app.models.multilevel_bom import BomAssembly
    from app.services.multilevel_bom_requirements import read_graph_requirements

    app, factory = composite_requisition_app
    app.include_router(delivery_router, prefix="/api/deliveries")
    app.include_router(finance_router, prefix="/api/finance")
    app.include_router(products_api.router, prefix="/api/master/products")
    material_id, _ = seed_graph(factory, separate=True, quantity=100,
                               cutting_modes={2: "一开四", 3: "一开二"}, finished_slot_count=16)
    lots = {}
    with factory() as db:
        parent = db.get(Product, 1)
        fields = products_api._product_payload_snapshot(parent)
        fields.update(product_code="ADMIN-LOOSE-100", customer_material_code="ADMIN-LOOSE-100",
                      combination_mode="component_priced", composite_fulfillment_mode="component_delivery")
        actor = db.get(User, 1)
        for pid, qty in [(2, 20), (3, 50)]:
            product = db.get(Product, pid)
            product.flute_type = product.material.flute_type
            product.splice_mode = "single"
            save(db, actor, pid, "manufactured", [])
            target = _receipt_auto_finished_ground_target(db, claim=True, customer_id=1, product_id=pid)
            lot = manual_finished_in(db, customer_id=1, product_id=pid,
                location_id=target.location.id, quantity=qty+(10 if pid == 2 else 30), stock_date=beijing_today(),
                source_type="manual", remarks="隔离零散库存", operator_id=1,
                idempotency_key=f"priced-loose-{pid}", expected_layout_version=target.layout_version)
            lots[pid] = (lot.id, lot.version, qty)
        db.commit()
    with TestClient(app) as client:
        _login(client)
        configured = client.post("/api/master/products/with-bom", json=jsonable_encoder({
            "product": fields, "bom": {"expected_version": 1, "inventory_mode": "separate",
                "material_mode": "expand_children", "delivery_mode": "components", "components": [
                    {"component_product_id": pid, "quantity_per_set": qty, "inventory_relation": "accompany"}
                    for pid, qty in [(2, 3), (3, 4)]]}}))
        assert configured.status_code == 201, configured.text
        parent = configured.json()["product"]
        assert parent["combination_mode"] == "component_priced"
        parent_id, parent_name = parent["id"], parent["product_name"]
        reopened = client.get(f"/api/master/products/{parent_id}/bom")
        assert reopened.status_code == 200, reopened.text
        assert {row["component_product_id"]: Decimal(str(row["quantity_per_set"]))
                for row in reopened.json()["components"]} == {2: 3, 3: 4}
        other = client.post("/api/orders", json={"customer_id": 1,
            "order_date": beijing_today().isoformat(), "items": [
                {"product_id": pid, "quantity": qty, "unit_price": "1"}
                for pid, qty in [(2, 10), (3, 30)]]})
        assert other.status_code == 201, other.text
        for row in other.json()["items"]:
            pid = row["product_id"]
            lid, version, qty = lots[pid]
            reserved = client.post("/api/warehouse/finished/reservations", json={
                "order_item_id": row["id"], "inventory_lot_id": lid, "quantity": row["quantity"],
                "expected_version": version, "idempotency_key": f"protected-{pid}",
                "warning_acknowledged_codes": [],
            })
            assert reserved.status_code == 200, reserved.text
            with factory() as db:
                lots[pid] = (lid, db.get(InventoryLot, lid).version, qty)
        from app.models.warehouse_inventory import InventoryReservation
        from app.services.multilevel_bom_cutover_review import _row
        protected_ids = [row["id"] for row in other.json()["items"]]
        with factory() as db:
            protected = {row.id: _row(row) for row in db.scalars(select(InventoryReservation)
                         .where(InventoryReservation.order_item_id.in_(protected_ids)))}
        created = client.post("/api/orders", json={"customer_id": 1,
            "order_date": beijing_today().isoformat(), "items": [dict(
                product_id=pid, quantity=qty, unit_price=price,
                combination_mode_snapshot="component_priced", combination_role="priced_component",
                combination_group_key="loose-100", combination_parent_product_id=parent_id,
                combination_parent_name_snapshot=parent_name, combination_set_quantity_snapshot=100,
                combination_quantity_per_set_snapshot=ratio,
            ) for pid, qty, price, ratio in [(2, 300, "1.25", 3), (3, 400, "0.80", 4)]]})
        assert created.status_code == 201, created.text
        items = {row["product_id"]: row["id"] for row in created.json()["items"]}
        requisitions = []
        for pid, iid in items.items():
            lid, version, qty = lots[pid]
            with factory() as db:
                import json
                from app.services.finished_stock_identity import order_product_basis
                actual = json.loads(db.get(InventoryLot, lid).finished_detail.physical_basis_json)
                expected = json.loads(order_product_basis(db, iid, pid))
                assert actual == expected, {key: (actual.get(key), expected.get(key))
                                            for key in actual if actual.get(key) != expected.get(key)}
            reservation_payload = {
                "order_item_id": iid, "inventory_lot_id": lid, "quantity": qty,
                "expected_version": version, "idempotency_key": f"priced-reserve-{pid}",
                "warning_acknowledged_codes": [],
            }
            rejected = client.post("/api/warehouse/finished/reservations", json={
                **reservation_payload, "quantity": qty+1, "idempotency_key": f"excess-{pid}"})
            assert rejected.status_code == 409, rejected.text
            reserved = client.post("/api/warehouse/finished/reservations", json=reservation_payload)
            assert reserved.status_code == 200, reserved.text
            replay = client.post("/api/warehouse/finished/reservations", json=reservation_payload)
            assert replay.status_code == 200, replay.text
            with factory() as db:
                requirements = read_graph_requirements(db, iid)
                assert requirements.plan.materials[0].purchase_sheets == (70 if pid == 2 else 175)
                source = next(row for row in read_compiled_order_bom(db, iid).snapshots
                              if row.component_product_id == pid)
                requisitions.append({**_component_payload(source.id), "order_item_id": iid,
                    "special_process": "一开四" if pid == 2 else "一开二"})
        saved = client.post("/api/requisition/batches", json={"request_key": "priced-loose-material",
            "supplier_name": "苏州纸板供应商", "items": requisitions})
        assert saved.status_code == 201, saved.text
        for index, source in enumerate(read_purchase_sources(factory, material_id)):
            fact = _freeze_receipt_fact(client, source, idempotency_key=f"priced-fact-{index}", unit_price="0.1234")
            assert fact.status_code == 200, fact.text
            first = source.order_purpose_sheet_qty // 2
            for batch, qty in enumerate([first, source.order_purpose_sheet_qty - first]):
                received = _receive(client, source, fact.json(), quantity=qty,
                                    idempotency_key=f"priced-receipt-{index}-{batch}")
                assert received.status_code == 200, received.text
                replay = _receive(client, source, fact.json(), quantity=qty,
                                  idempotency_key=f"priced-receipt-{index}-{batch}")
                assert replay.status_code == 200, replay.text
        with factory() as db:
            assert list(db.scalars(select(BomAssembly))) == []
            for pid, iid in items.items():
                from app.models.warehouse_inventory import InventoryReservation
                rows = list(db.scalars(select(InventoryReservation).where(InventoryReservation.order_item_id == iid)))
                assert sum(row.reserved_stock_quantity-row.consumed_stock_quantity-row.released_stock_quantity
                           for row in rows) == (300 if pid == 2 else 400)
        first_delivery = _dispatch(client, 1, [(items[2], 120), (items[3], 100)])
        second = _dispatch(client, 1, [(items[2], 180), (items[3], 300)])
        cancelled = client.put(f"/api/deliveries/{second['id']}/cancel")
        assert cancelled.status_code == 200, cancelled.text
        with factory() as db:
            assert db.get(OrderItem, items[2]).delivered_quantity == 120
            assert db.get(OrderItem, items[3]).delivered_quantity == 100
        cancelled = client.put(f"/api/deliveries/{first_delivery['id']}/cancel")
        assert cancelled.status_code == 200, cancelled.text
        replay = client.put(f"/api/deliveries/{first_delivery['id']}/cancel")
        assert replay.status_code == 409, replay.text
        assert "无需取消" in replay.json()["detail"]
        with factory() as db:
            for pid, iid in items.items():
                assert db.get(OrderItem, iid).delivered_quantity == 0
                rows = list(db.scalars(select(InventoryReservation).where(InventoryReservation.order_item_id == iid)))
                assert sum(row.reserved_stock_quantity-row.consumed_stock_quantity-row.released_stock_quantity
                           for row in rows) == (300 if pid == 2 else 400)
        first_delivery = _dispatch(client, 1, [(items[2], 120), (items[3], 100)])
        second = _dispatch(client, 1, [(items[2], 180), (items[3], 300)])
        _confirm_receipt(client, first_delivery)
        _confirm_receipt(client, second)
        statement = client.post("/api/finance/statements", json={"customer_id": 1,
            "statement_month": beijing_today().strftime("%Y-%m"),
            "delivery_ids": [first_delivery["id"], second["id"]]})
        assert statement.status_code == 201, statement.text
        with factory() as db:
            assert db.get(Statement, statement.json()["id"]).total_receivable == Decimal("695.00")
            assert {row.id: _row(row) for row in db.scalars(select(InventoryReservation)
                    .where(InventoryReservation.order_item_id.in_(protected_ids)))} == protected
