"""R02: one trusted source must remain traceable through delivery.

This test deliberately uses the real order, requisition, incoming-receipt, and
delivery HTTP entry points.  The only direct database setup is synthetic master
data and warehouse locations required to make those entry points usable.
"""

from __future__ import annotations

from copy import deepcopy

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from tests.test_t02_order_import_source_identity import _payload, _ready_product
from tests.test_p1_81_receipt_purpose_flow import (
    _create_frozen_sources,
    _freeze_receipt_fact,
    _receive,
    _seed_material_and_staging,
    _use_p181_published_map_identity,
)
from tests.test_semi_finished_order_reservation import b1_app, login


def test_r02_one_trusted_source_traces_order_purchase_receipts_inventory_and_delivery(
    b1_app, monkeypatch
) -> None:
    """Verify the 500/600/300+300/300 chain without joining unrelated fixtures."""

    from app.api.deliveries import router as deliveries_router
    from app.api.incoming import router as incoming_router
    from app.api.deliveries import _delivery_remaining_quantity
    from app.models.delivery import DeliveryItem
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.order import Order, OrderItem
    from app.models.order_import_source import OrderImportSource, OrderImportSourceLine
    from app.models.production import ProductionCompletion
    from app.models.purchase_receipt import IncomingReceiptPurposeAllocation
    from app.models.supplier_requisition_order import PurchasePurposeSourceSnapshot
    from app.models.warehouse_inventory import InventoryLot, InventoryMovement

    app, factory = b1_app
    app.include_router(incoming_router, prefix="/api/incoming")
    app.include_router(deliveries_router, prefix="/api/deliveries")
    _use_p181_published_map_identity(monkeypatch)
    _ready_product(factory)
    # Synthetic supplier master is required by the real requisition save API;
    # it is not a purchase, receipt, inventory, production, or delivery fact.
    from app.models.supplier import Supplier
    from app.services.supplier_master import normalize_supplier_identity

    with factory() as db:
        supplier_name = "苏州纸板供应商"
        db.add(
            Supplier(
                standard_name=supplier_name,
                normalized_name=normalize_supplier_identity(supplier_name),
                display_name=supplier_name,
                is_active=True,
            )
        )
        db.commit()

    source_hash = "e" * 64
    with TestClient(app) as client:
        login(client, "sales")
        create_payload = _payload(
            app, source_hash, "r02-source-create", quantity=500, customer_po="R02-SAME-SOURCE"
        )
        created = client.post("/api/orders", json=create_payload)
        assert created.status_code == 201, created.text
        order_id = int(created.json()["id"])

        replay_payload = deepcopy(create_payload)
        replay_payload["idempotency_key"] = "r02-source-replay"
        replay = client.post("/api/orders", json=replay_payload)
        assert replay.status_code == 201, replay.text
        assert int(replay.json()["id"]) == order_id
        assert replay.json()["source_replay"] is True

        changed_payload = deepcopy(replay_payload)
        changed_payload["idempotency_key"] = "r02-source-changed-payload"
        changed_payload["items"][0]["quantity"] = 501
        changed = client.post("/api/orders", json=changed_payload)
        assert changed.status_code == 409

    # This helper only makes synthetic material and physical-location master
    # facts. The order itself above remains the source-created business fact.
    _seed_material_and_staging(factory)

    with TestClient(app) as client:
        login(client, "admin")
        frozen_source = _create_frozen_sources(
            client,
            factory,
            order_quantity=500,
            purchase_total=600,
            order_purpose=500,
            stock_purpose=100,
        )[0]
        assert (
            frozen_source.order_purpose_sheet_qty,
            frozen_source.reserve_purpose_sheet_qty,
        ) == (500, 100)
        frozen_fact = _freeze_receipt_fact(
            client, frozen_source, idempotency_key="r02-price-fact"
        )
        assert frozen_fact.status_code == 200, frozen_fact.text

        first_receipt = _receive(
            client,
            frozen_source,
            frozen_fact.json(),
            quantity=300,
            idempotency_key="r02-receive-first",
        )
        assert first_receipt.status_code == 200, first_receipt.text
        first_replay = _receive(
            client,
            frozen_source,
            frozen_fact.json(),
            quantity=300,
            idempotency_key="r02-receive-first",
        )
        assert first_replay.status_code == 200, first_replay.text
        assert first_replay.json()["receipt_item_id"] == first_receipt.json()["receipt_item_id"]

        second_receipt = _receive(
            client,
            frozen_source,
            frozen_fact.json(),
            quantity=300,
            idempotency_key="r02-receive-second",
            overrides={"surplus_disposition": "semi_finished_reserve"},
        )
        assert second_receipt.status_code == 200, second_receipt.text

        with factory() as db:
            source = db.scalar(
                select(OrderImportSource).where(OrderImportSource.source_hash == source_hash)
            )
            assert source is not None and source.order_id == order_id
            source_line = db.scalar(
                select(OrderImportSourceLine).where(OrderImportSourceLine.source_id == source.id)
            )
            assert source_line is not None and source_line.order_item_id is not None
            order_item_id = int(source_line.order_item_id)
            order = db.get(Order, order_id)
            item = db.get(OrderItem, order_item_id)
            assert order is not None and item is not None
            assert (order.customer_po, int(item.quantity)) == ("R02-SAME-SOURCE", 500)

            purchase_snapshot = db.get(
                PurchasePurposeSourceSnapshot, frozen_source.purpose_snapshot_id
            )
            assert purchase_snapshot is not None
            assert purchase_snapshot.source_order_item_id == order_item_id
            assert (
                purchase_snapshot.order_purpose_sheet_qty,
                purchase_snapshot.reserve_purpose_sheet_qty,
            ) == (500, 100)

            allocations = list(
                db.scalars(
                    select(IncomingReceiptPurposeAllocation)
                    .where(
                        IncomingReceiptPurposeAllocation.purchase_purpose_source_snapshot_id
                        == purchase_snapshot.id
                    )
                    .order_by(IncomingReceiptPurposeAllocation.id)
                )
            )
            assert len(allocations) == 2
            assert [
                (
                    allocation.receipt_total_sheet_qty,
                    allocation.receipt_order_purpose_sheet_qty,
                    allocation.receipt_reserve_purpose_sheet_qty,
                    allocation.cumulative_total_sheet_qty_after,
                )
                for allocation in allocations
            ] == [(300, 300, 0, 300), (300, 200, 100, 600)]
            assert all(allocation.source_order_item_id == order_item_id for allocation in allocations)
            assert all(allocation.purchase_receipt_fact_id for allocation in allocations)
            assert allocations[0].production_completion_id is not None
            assert allocations[1].production_completion_id is not None
            assert allocations[0].finished_inventory_lot_id is not None
            assert allocations[1].finished_inventory_lot_id is not None
            assert allocations[1].semi_finished_inventory_lot_id is not None

            completions = list(
                db.scalars(
                    select(ProductionCompletion).where(
                        ProductionCompletion.order_item_id == order_item_id
                    )
                )
            )
            assert [completion.origin for completion in completions] == [
                "receipt_auto",
                "receipt_auto",
            ]
            assert sum(int(completion.actual_output_quantity) for completion in completions) == 500

            receipt_items = list(
                db.scalars(
                    select(IncomingReceiptItem).where(
                        IncomingReceiptItem.supplier_order_item_id
                        == frozen_source.supplier_item_id
                    )
                )
            )
            assert {item.id for item in receipt_items} == {
                allocation.incoming_receipt_item_id for allocation in allocations
            }

        delivery = client.post(
            "/api/deliveries",
            json={
                "customer_id": 1,
                "items": [{"order_item_id": order_item_id, "delivered_quantity": 300}],
            },
        )
        assert delivery.status_code == 201, delivery.text
        delivery_id = int(delivery.json()["id"])
        dispatched = client.put(f"/api/deliveries/{delivery_id}/dispatch")
        assert dispatched.status_code == 200, dispatched.text

        with factory() as db:
            item = db.get(OrderItem, order_item_id)
            assert item is not None
            active_lots = list(
                db.scalars(select(InventoryLot).where(InventoryLot.status == "active"))
            )
            semi_sheets = sum(
                int(lot.quantity_available) + int(lot.quantity_reserved)
                for lot in active_lots
                if lot.inventory_type == "semi_finished"
            )
            finished = sum(
                int(lot.quantity_available) + int(lot.quantity_reserved)
                for lot in active_lots
                if lot.inventory_type == "finished"
            )
            assert (
                semi_sheets,
                finished,
                int(item.delivered_quantity or 0),
                int(item.quantity) - int(item.delivered_quantity or 0),
                int(_delivery_remaining_quantity(db, item)),
            ) == (100, 200, 300, 200, 200)

            delivery_item = db.scalar(
                select(DeliveryItem).where(DeliveryItem.delivery_id == delivery_id)
            )
            assert delivery_item is not None and delivery_item.order_item_id == order_item_id
            delivery_movements = list(
                db.scalars(
                    select(InventoryMovement).where(
                        InventoryMovement.related_delivery_id == delivery_id
                    )
                )
            )
            assert delivery_movements
            assert all(movement.related_order_item_id == order_item_id for movement in delivery_movements)

            # Reverse trace: delivery -> order item -> imported source line ->
            # source occurrence, and the receipt allocations use the same item.
            assert source_line.order_item_id == delivery_item.order_item_id
            assert source.order_id == item.order_id
            assert all(allocation.source_order_item_id == delivery_item.order_item_id for allocation in allocations)

            before_blocked_revert = (
                int(item.delivered_quantity or 0),
                int(
                    db.scalar(
                        select(func.count(IncomingReceiptPurposeAllocation.id))
                    )
                    or 0
                ),
            )

        blocked_revert = client.put(
            f"/api/incoming/receipt-items/{second_receipt.json()['receipt_item_id']}/revert",
            json={},
        )
        assert blocked_revert.status_code == 409, blocked_revert.text

        with factory() as db:
            item = db.get(OrderItem, order_item_id)
            assert item is not None
            after_blocked_revert = (
                int(item.delivered_quantity or 0),
                int(
                    db.scalar(
                        select(func.count(IncomingReceiptPurposeAllocation.id))
                    )
                    or 0
                ),
            )
            assert after_blocked_revert == before_blocked_revert
