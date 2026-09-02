from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.models.production import ProductionCompletion, ProductionTask
from app.models.purchase_receipt import (
    IncomingReceiptPurposeAllocation,
    ProductionCompletionReserveConversion,
    ProductionCompletionReserveConversionReversal,
)
from app.models.delivery import Delivery
from app.models.warehouse_inventory import InventoryLot, InventoryReservation
from tests.test_p1_81_receipt_purpose_flow import (
    _business_counts,
    _create_frozen_sources,
    _freeze_receipt_fact,
    _login,
    _p181_published_map_identity,
    _receive,
    _seed_material_and_staging,
)
from tests.test_phase11_requisition import requisition_app


def _include_p0_32_routers(app) -> None:
    from app.api.deliveries import pick_router, router as deliveries_router
    from app.api.production import router as production_router

    app.include_router(deliveries_router, prefix="/api/deliveries")
    app.include_router(pick_router, prefix="/api/delivery-picks")
    app.include_router(production_router, prefix="/api/production")
    assert any(
        getattr(route, "path", None) == "/api/deliveries/pending_items"
        for route in app.routes
    )


def _prepare_600_plus_2(client: TestClient, session_factory):
    source = _create_frozen_sources(
        client,
        session_factory,
        order_quantity=600,
        purchase_total=602,
        order_purpose=600,
        stock_purpose=2,
    )[0]
    frozen = _freeze_receipt_fact(
        client,
        source,
        idempotency_key="p032-price-600-plus-2",
    )
    assert frozen.status_code == 200, frozen.text
    return source, frozen.json()


def _pending_delivery_row(client: TestClient, order_item_id: int = 1) -> dict:
    response = client.get("/api/deliveries/pending_items")
    assert response.status_code == 200, response.text
    return next(
        row
        for row in response.json()["items"]
        if int(row["order_item_id"]) == order_item_id
    )


def test_frozen_over_receipt_requires_explicit_surplus_disposition_without_writes(
    requisition_app,
) -> None:
    app, session_factory = requisition_app
    _include_p0_32_routers(app)
    _seed_material_and_staging(session_factory)
    with TestClient(app) as client:
        _login(client, "admin")
        source, receipt_fact = _prepare_600_plus_2(client, session_factory)
        pending_response = client.get("/api/incoming/pending")
        assert pending_response.status_code == 200, pending_response.text
        pending_row = next(
            row
            for row in pending_response.json()["items"]
            if str(row["item_id"]) == source.route_key
        )
        assert pending_row["surplus_choice_required"] is True
        assert pending_row["surplus_sheet_qty"] == 2
        assert pending_row["finished_disposition_expected_finished_output_qty"] == 602
        assert (
            pending_row[
                "semi_finished_reserve_expected_finished_output_qty"
            ]
            == 600
        )
        baseline = _business_counts(session_factory)
        blocked = _receive(
            client,
            source,
            receipt_fact,
            quantity=602,
            idempotency_key="p032-receive-choice-required",
            overrides={"surplus_disposition": None},
        )
        assert blocked.status_code == 409, blocked.text
        assert blocked.json()["detail"]["code"] == "INCOMING_SURPLUS_DISPOSITION_REQUIRED"
        assert _business_counts(session_factory) == baseline


def test_finished_choice_posts_602_and_delivery_defaults_600_but_allows_602(
    requisition_app,
) -> None:
    app, session_factory = requisition_app
    _include_p0_32_routers(app)
    _seed_material_and_staging(session_factory)
    with TestClient(app) as client:
        _login(client, "admin")
        source, receipt_fact = _prepare_600_plus_2(client, session_factory)
        received = _receive(
            client,
            source,
            receipt_fact,
            quantity=602,
            idempotency_key="p032-receive-all-finished",
            overrides={"surplus_disposition": "finished"},
        )
        assert received.status_code == 200, received.text
        allocation = received.json()["purpose_allocation"]
        assert allocation["surplus_disposition"] == "finished"
        assert allocation["order_sheet_delta"] == 602
        assert allocation["reserve_sheet_delta"] == 0
        assert allocation["theoretical_finished_delta"] == 602
        assert allocation["reserve_inventory_lot_id"] is None
        with session_factory() as session:
            finished_lot = session.get(
                InventoryLot, allocation["finished_inventory_lot_id"]
            )
            assert finished_lot is not None
            assert finished_lot.source_type in {
                "production_surplus",
                "production_completion",
                "transfer",
            }, finished_lot.source_type
            assert (
                int(finished_lot.quantity_reserved),
                int(finished_lot.quantity_available),
            ) == (600, 2)

        pending = _pending_delivery_row(client)
        assert pending["order_remaining_quantity"] == 600
        assert pending["deliverable_quantity"] == 602
        assert pending["over_delivery_quantity"] == 2

        unconfirmed = client.post(
            "/api/deliveries",
            json={
                "customer_id": 1,
                "items": [{"order_item_id": 1, "delivered_quantity": 602}],
            },
        )
        assert unconfirmed.status_code == 409, unconfirmed.text
        confirmed = client.post(
            "/api/deliveries",
            json={
                "customer_id": 1,
                "items": [
                    {
                        "order_item_id": 1,
                        "delivered_quantity": 602,
                        "over_delivery_confirmed": True,
                        "over_delivery_reason": "确认将本次多生产的2只一并送货",
                    }
                ],
            },
        )
        assert confirmed.status_code == 201, confirmed.text
        assert confirmed.json()["items"][0]["over_delivery_quantity"] == 2


def test_reserve_choice_keeps_600_finished_and_duplicate_receipt_is_blocked(
    requisition_app,
) -> None:
    app, session_factory = requisition_app
    _include_p0_32_routers(app)
    _seed_material_and_staging(session_factory)
    with TestClient(app) as client:
        _login(client, "admin")
        source, receipt_fact = _prepare_600_plus_2(client, session_factory)
        received = _receive(
            client,
            source,
            receipt_fact,
            quantity=602,
            idempotency_key="p032-receive-reserve",
            overrides={"surplus_disposition": "semi_finished_reserve"},
        )
        assert received.status_code == 200, received.text
        allocation = received.json()["purpose_allocation"]
        assert allocation["surplus_disposition"] == "semi_finished_reserve"
        assert allocation["order_sheet_delta"] == 600
        assert allocation["reserve_sheet_delta"] == 2
        assert allocation["theoretical_finished_delta"] == 600
        assert allocation["reserve_inventory_lot_id"] is not None
        assert _pending_delivery_row(client)["deliverable_quantity"] == 600

        replay_counts = _business_counts(session_factory)
        replay = _receive(
            client,
            source,
            receipt_fact,
            quantity=602,
            idempotency_key="p032-receive-reserve",
            overrides={"surplus_disposition": "semi_finished_reserve"},
        )
        assert replay.status_code == 200, replay.text
        assert _business_counts(session_factory) == replay_counts

        pending = client.get("/api/incoming/pending")
        assert pending.status_code == 200, pending.text
        assert source.route_key not in {
            str(row["item_id"]) for row in pending.json()["items"]
        }
        duplicate = _receive(
            client,
            source,
            receipt_fact,
            quantity=602,
            idempotency_key="p032-receive-reserve-duplicate",
            overrides={"surplus_disposition": "semi_finished_reserve"},
        )
        assert duplicate.status_code == 409, duplicate.text
        assert duplicate.json()["detail"]["code"] == "INCOMING_SOURCE_ALREADY_FULLY_RECEIVED"


def test_reserve_choice_uses_frozen_route_flute_when_material_lists_supported_flutes(
    requisition_app,
) -> None:
    """A material applicability list is not one physical board flute."""

    from app.models.material import Material
    from app.models.purchase_receipt import PurchaseReceiptFact

    app, session_factory = requisition_app
    _include_p0_32_routers(app)
    _seed_material_and_staging(session_factory)
    with session_factory() as session:
        material = session.scalar(select(Material).where(Material.code == "KAKAK"))
        assert material is not None
        material.flute_type = "AB/BE"
        material.version += 1
        session.commit()

    with TestClient(app) as client:
        _login(client, "admin")
        source = _create_frozen_sources(
            client,
            session_factory,
            order_quantity=200,
            purchase_total=200,
            order_purpose=200,
            stock_purpose=0,
        )[0]
        frozen = _freeze_receipt_fact(
            client,
            source,
            idempotency_key="p032-supported-flute-price",
        )
        assert frozen.status_code == 200, frozen.text
        receipt_fact = frozen.json()
        with session_factory() as session:
            frozen_fact = session.scalar(
                select(PurchaseReceiptFact).where(
                    PurchaseReceiptFact.purchase_purpose_source_snapshot_id
                    == source.purpose_snapshot_id
                )
            )
            assert frozen_fact is not None
            assert frozen_fact.actual_material_flute_type_snapshot == "AB/BE"

        received = _receive(
            client,
            source,
            receipt_fact,
            quantity=302,
            idempotency_key="p032-supported-flute-list",
            overrides={"surplus_disposition": "semi_finished_reserve"},
        )
        assert received.status_code == 200, received.text
        assert received.json()["purpose_allocation"]["reserve_sheet_delta"] == 102
        reserve_lot_id = received.json()["purpose_allocation"][
            "reserve_inventory_lot_id"
        ]
        with session_factory() as session:
            reserve_lot = session.get(InventoryLot, reserve_lot_id)
            assert reserve_lot is not None
            assert reserve_lot.semi_finished_detail is not None
            assert reserve_lot.semi_finished_detail.layer_count == 5
            assert reserve_lot.semi_finished_detail.flute_type == "AB"


def test_actual_quantity_raise_consumes_same_receipt_reserve_and_exposes_602(
    requisition_app,
) -> None:
    app, session_factory = requisition_app
    _include_p0_32_routers(app)
    _seed_material_and_staging(session_factory)
    with TestClient(app) as client:
        _login(client, "admin")
        source, receipt_fact = _prepare_600_plus_2(client, session_factory)
        received = _receive(
            client,
            source,
            receipt_fact,
            quantity=602,
            idempotency_key="p032-receive-then-convert",
            overrides={"surplus_disposition": "semi_finished_reserve"},
        )
        assert received.status_code == 200, received.text
        receipt_item_id = int(received.json()["receipt_item_id"])

        with session_factory() as session:
            allocation = session.scalar(
                select(IncomingReceiptPurposeAllocation).where(
                    IncomingReceiptPurposeAllocation.incoming_receipt_item_id
                    == receipt_item_id
                )
            )
            assert allocation is not None
            completion = session.get(
                ProductionCompletion, allocation.production_completion_id
            )
            reserve_lot = session.get(
                InventoryLot, allocation.semi_finished_inventory_lot_id
            )
            assert completion is not None and reserve_lot is not None
            task = session.get(ProductionTask, completion.task_id)
            finished_lot = session.get(InventoryLot, completion.inventory_lot_id)
            assert task is not None and finished_lot is not None
            versions = (int(task.version), int(finished_lot.version))
            completion_id = int(completion.id)
            reserve_lot_id = int(reserve_lot.id)

        adjusted = client.put(
            f"/api/production/completions/{completion_id}/actual-quantity",
            json={
                "actual_output_quantity": 602,
                "expected_task_version": versions[0],
                "expected_lot_version": versions[1],
                "idempotency_key": "p032-adjust-600-to-602",
            },
        )
        assert adjusted.status_code == 200, adjusted.text
        assert adjusted.json()["completion"]["actual_output_quantity"] == 602
        assert _pending_delivery_row(client)["deliverable_quantity"] == 602

        replay = client.put(
            f"/api/production/completions/{completion_id}/actual-quantity",
            json={
                "actual_output_quantity": 602,
                "expected_task_version": versions[0],
                "expected_lot_version": versions[1],
                "idempotency_key": "p032-adjust-600-to-602",
            },
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["replayed"] is True

    with session_factory() as session:
        reserve_lot = session.get(InventoryLot, reserve_lot_id)
        assert reserve_lot is not None
        assert int(reserve_lot.quantity_available) == 0
        assert int(reserve_lot.quantity_consumed) == 2
        assert int(
            session.scalar(
                select(func.count(ProductionCompletionReserveConversion.id)).where(
                    ProductionCompletionReserveConversion.production_completion_id
                    == completion_id
                )
            )
            or 0
        ) == 1


def test_actual_quantity_raise_above_same_receipt_reserve_rolls_back_without_loss(
    requisition_app,
) -> None:
    app, session_factory = requisition_app
    _include_p0_32_routers(app)
    _seed_material_and_staging(session_factory)
    with TestClient(app) as client:
        _login(client, "admin")
        source, receipt_fact = _prepare_600_plus_2(client, session_factory)
        received = _receive(
            client,
            source,
            receipt_fact,
            quantity=602,
            idempotency_key="p032-receive-insufficient-convert",
            overrides={"surplus_disposition": "semi_finished_reserve"},
        )
        assert received.status_code == 200, received.text
        allocation_payload = received.json()["purpose_allocation"]
        with session_factory() as session:
            allocation = session.get(
                IncomingReceiptPurposeAllocation,
                int(allocation_payload["trace_id"].split(":")[-1]),
            )
            assert allocation is not None
            completion = session.get(
                ProductionCompletion, allocation.production_completion_id
            )
            assert completion is not None
            task = session.get(ProductionTask, completion.task_id)
            finished_lot = session.get(InventoryLot, completion.inventory_lot_id)
            reserve_lot = session.get(
                InventoryLot, allocation.semi_finished_inventory_lot_id
            )
            assert task is not None
            assert finished_lot is not None and reserve_lot is not None
            completion_id = int(completion.id)
            versions = (int(task.version), int(finished_lot.version))
            reserve_lot_id = int(reserve_lot.id)
            baseline_counts = (
                int(reserve_lot.quantity_available),
                int(reserve_lot.quantity_consumed),
                int(completion.actual_output_quantity),
                int(finished_lot.quantity_available),
            )

        blocked = client.put(
            f"/api/production/completions/{completion_id}/actual-quantity",
            json={
                "actual_output_quantity": 603,
                "expected_task_version": versions[0],
                "expected_lot_version": versions[1],
                "idempotency_key": "p032-adjust-600-to-603-insufficient",
            },
        )
        assert blocked.status_code == 409, blocked.text

    with session_factory() as session:
        completion = session.get(ProductionCompletion, completion_id)
        assert completion is not None
        finished_lot = session.get(InventoryLot, completion.inventory_lot_id)
        reserve_lot = session.get(InventoryLot, reserve_lot_id)
        assert finished_lot is not None
        assert reserve_lot is not None
        assert (
            int(reserve_lot.quantity_available),
            int(reserve_lot.quantity_consumed),
            int(completion.actual_output_quantity),
            int(finished_lot.quantity_available),
        ) == baseline_counts
        assert int(
            session.scalar(select(func.count(ProductionCompletionReserveConversion.id)))
            or 0
        ) == 0


def test_actual_quantity_reduce_restores_converted_reserve_and_replays_once(
    requisition_app,
) -> None:
    app, session_factory = requisition_app
    _include_p0_32_routers(app)
    _seed_material_and_staging(session_factory)
    with TestClient(app) as client:
        _login(client, "admin")
        source, receipt_fact = _prepare_600_plus_2(client, session_factory)
        received = _receive(
            client,
            source,
            receipt_fact,
            quantity=602,
            idempotency_key="p032-receive-convert-then-restore",
            overrides={"surplus_disposition": "semi_finished_reserve"},
        )
        allocation_payload = received.json()["purpose_allocation"]
        with session_factory() as session:
            allocation = session.get(
                IncomingReceiptPurposeAllocation,
                int(allocation_payload["trace_id"].split(":")[-1]),
            )
            assert allocation is not None
            completion = session.get(
                ProductionCompletion, allocation.production_completion_id
            )
            reserve_lot = session.get(
                InventoryLot, allocation.semi_finished_inventory_lot_id
            )
            assert completion is not None and reserve_lot is not None
            task = session.get(ProductionTask, completion.task_id)
            finished_lot = session.get(InventoryLot, completion.inventory_lot_id)
            assert task is not None and finished_lot is not None
            completion_id = int(completion.id)
            reserve_lot_id = int(reserve_lot.id)
            raise_versions = (int(task.version), int(finished_lot.version))

        raised = client.put(
            f"/api/production/completions/{completion_id}/actual-quantity",
            json={
                "actual_output_quantity": 602,
                "expected_task_version": raise_versions[0],
                "expected_lot_version": raise_versions[1],
                "idempotency_key": "p032-raise-before-restore",
            },
        )
        assert raised.status_code == 200, raised.text
        with session_factory() as session:
            completion = session.get(ProductionCompletion, completion_id)
            assert completion is not None
            task = session.get(ProductionTask, completion.task_id)
            finished_lot = session.get(InventoryLot, completion.inventory_lot_id)
            assert task is not None and finished_lot is not None
            reduce_versions = (int(task.version), int(finished_lot.version))

        reduced = client.put(
            f"/api/production/completions/{completion_id}/actual-quantity",
            json={
                "actual_output_quantity": 600,
                "expected_task_version": reduce_versions[0],
                "expected_lot_version": reduce_versions[1],
                "idempotency_key": "p032-restore-602-to-600",
            },
        )
        assert reduced.status_code == 200, reduced.text
        assert reduced.json()["completion"]["actual_output_quantity"] == 600
        assert _pending_delivery_row(client)["deliverable_quantity"] == 600
        replay = client.put(
            f"/api/production/completions/{completion_id}/actual-quantity",
            json={
                "actual_output_quantity": 600,
                "expected_task_version": reduce_versions[0],
                "expected_lot_version": reduce_versions[1],
                "idempotency_key": "p032-restore-602-to-600",
            },
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["replayed"] is True

    with session_factory() as session:
        reserve_lot = session.get(InventoryLot, reserve_lot_id)
        assert reserve_lot is not None
        assert (
            int(reserve_lot.quantity_available),
            int(reserve_lot.quantity_consumed),
        ) == (2, 0)
        assert int(
            session.scalar(
                select(
                    func.count(ProductionCompletionReserveConversionReversal.id)
                )
            )
            or 0
        ) == 1


def test_receipt_revert_restores_conversion_then_closes_finished_and_reserve(
    requisition_app,
) -> None:
    app, session_factory = requisition_app
    _include_p0_32_routers(app)
    _seed_material_and_staging(session_factory)
    with TestClient(app) as client:
        _login(client, "admin")
        source, receipt_fact = _prepare_600_plus_2(client, session_factory)
        received = _receive(
            client,
            source,
            receipt_fact,
            quantity=602,
            idempotency_key="p032-receive-before-full-revert",
            overrides={"surplus_disposition": "semi_finished_reserve"},
        )
        receipt_item_id = int(received.json()["receipt_item_id"])
        allocation_payload = received.json()["purpose_allocation"]
        with session_factory() as session:
            allocation = session.get(
                IncomingReceiptPurposeAllocation,
                int(allocation_payload["trace_id"].split(":")[-1]),
            )
            assert allocation is not None
            completion = session.get(
                ProductionCompletion, allocation.production_completion_id
            )
            assert completion is not None
            task = session.get(ProductionTask, completion.task_id)
            finished_lot = session.get(InventoryLot, completion.inventory_lot_id)
            assert task is not None and finished_lot is not None
            versions = (int(task.version), int(finished_lot.version))
            completion_id = int(completion.id)
            finished_lot_id = int(finished_lot.id)
            reserve_lot_id = int(allocation.semi_finished_inventory_lot_id)
        adjusted = client.put(
            f"/api/production/completions/{completion_id}/actual-quantity",
            json={
                "actual_output_quantity": 602,
                "expected_task_version": versions[0],
                "expected_lot_version": versions[1],
                "idempotency_key": "p032-adjust-before-receipt-revert",
            },
        )
        assert adjusted.status_code == 200, adjusted.text
        reverted = client.put(
            f"/api/incoming/receipt-items/{receipt_item_id}/revert",
            json={
                "reason": "测试撤销收料并完整反冲备库转换",
                "idempotency_key": "p032-revert-converted-receipt",
            },
        )
        assert reverted.status_code == 200, reverted.text

    with session_factory() as session:
        completion = session.get(ProductionCompletion, completion_id)
        finished_lot = session.get(InventoryLot, finished_lot_id)
        reserve_lot = session.get(InventoryLot, reserve_lot_id)
        assert completion is not None and completion.status == "reversed"
        assert finished_lot is not None
        assert finished_lot.status == "closed"
        assert int(finished_lot.quantity_available) == 0
        assert int(finished_lot.quantity_reserved) == 0
        assert reserve_lot is not None
        assert reserve_lot.status == "closed"
        assert int(reserve_lot.quantity_available) == 0
        assert int(reserve_lot.quantity_consumed) == 0
        assert int(
            session.scalar(
                select(
                    func.count(ProductionCompletionReserveConversionReversal.id)
                )
            )
            or 0
        ) == 1


def test_completion_never_steals_reserve_from_a_different_receipt_batch(
    requisition_app,
) -> None:
    app, session_factory = requisition_app
    _include_p0_32_routers(app)
    _seed_material_and_staging(session_factory)
    with TestClient(app) as client:
        _login(client, "admin")
        source, receipt_fact = _prepare_600_plus_2(client, session_factory)
        first = _receive(
            client,
            source,
            receipt_fact,
            quantity=600,
            idempotency_key="p032-first-batch-order-only",
            overrides={"surplus_disposition": None},
        )
        assert first.status_code == 200, first.text
        second = _receive(
            client,
            source,
            receipt_fact,
            quantity=2,
            idempotency_key="p032-second-batch-reserve-only",
            overrides={"surplus_disposition": "semi_finished_reserve"},
        )
        assert second.status_code == 200, second.text
        with session_factory() as session:
            allocations = list(
                session.scalars(
                    select(IncomingReceiptPurposeAllocation).order_by(
                        IncomingReceiptPurposeAllocation.id
                    )
                ).all()
            )
            first_allocation, second_allocation = allocations
            completion = session.get(
                ProductionCompletion, first_allocation.production_completion_id
            )
            reserve_lot = session.get(
                InventoryLot, second_allocation.semi_finished_inventory_lot_id
            )
            assert completion is not None and reserve_lot is not None
            task = session.get(ProductionTask, completion.task_id)
            finished_lot = session.get(InventoryLot, completion.inventory_lot_id)
            assert task is not None and finished_lot is not None
            completion_id = int(completion.id)
            versions = (int(task.version), int(finished_lot.version))
            reserve_lot_id = int(reserve_lot.id)
        blocked = client.put(
            f"/api/production/completions/{completion_id}/actual-quantity",
            json={
                "actual_output_quantity": 602,
                "expected_task_version": versions[0],
                "expected_lot_version": versions[1],
                "idempotency_key": "p032-no-cross-batch-conversion",
            },
        )
        assert blocked.status_code == 409, blocked.text
    with session_factory() as session:
        reserve_lot = session.get(InventoryLot, reserve_lot_id)
        assert reserve_lot is not None
        assert (
            int(reserve_lot.quantity_available),
            int(reserve_lot.quantity_consumed),
        ) == (2, 0)


def test_over_delivery_requires_nonblank_reason_and_reconfirms_after_race(
    requisition_app,
) -> None:
    app, session_factory = requisition_app
    _include_p0_32_routers(app)
    _seed_material_and_staging(session_factory)
    with TestClient(app) as client:
        _login(client, "admin")
        source, receipt_fact = _prepare_600_plus_2(client, session_factory)
        received = _receive(
            client,
            source,
            receipt_fact,
            quantity=602,
            idempotency_key="p032-finished-for-delivery-race",
            overrides={"surplus_disposition": "finished"},
        )
        assert received.status_code == 200, received.text
        with session_factory() as session:
            baseline_deliveries = int(
                session.scalar(select(func.count(Delivery.id))) or 0
            )
        blank_reason = client.post(
            "/api/deliveries",
            json={
                "customer_id": 1,
                "items": [
                    {
                        "order_item_id": 1,
                        "delivered_quantity": 602,
                        "over_delivery_confirmed": True,
                        "over_delivery_reason": "   ",
                    }
                ],
            },
        )
        assert blank_reason.status_code == 409, blank_reason.text
        with session_factory() as session:
            assert int(session.scalar(select(func.count(Delivery.id))) or 0) == (
                baseline_deliveries
            )

        first = client.post(
            "/api/deliveries",
            json={"customer_id": 1, "items": [{"order_item_id": 1, "delivered_quantity": 100}]},
        )
        second = client.post(
            "/api/deliveries",
            json={"customer_id": 1, "items": [{"order_item_id": 1, "delivered_quantity": 502}]},
        )
        assert first.status_code == 201, first.text
        assert second.status_code == 201, second.text
        first_dispatch = client.put(f"/api/deliveries/{first.json()['id']}/dispatch")
        assert first_dispatch.status_code == 200, first_dispatch.text
        second_dispatch = client.put(
            f"/api/deliveries/{second.json()['id']}/dispatch"
        )
        assert second_dispatch.status_code == 409, second_dispatch.text
        with session_factory() as session:
            second_row = session.get(Delivery, int(second.json()["id"]))
            assert second_row is not None and second_row.status == "pending"


def test_602_pick_plan_dispatch_and_cancel_preserve_all_physical_stock(
    requisition_app,
) -> None:
    app, session_factory = requisition_app
    _include_p0_32_routers(app)
    _seed_material_and_staging(session_factory)
    with TestClient(app) as client:
        _login(client, "admin")
        source, receipt_fact = _prepare_600_plus_2(client, session_factory)
        received = _receive(
            client,
            source,
            receipt_fact,
            quantity=602,
            idempotency_key="p032-finished-for-pick-chain",
            overrides={"surplus_disposition": "finished"},
        )
        finished_lot_id = int(
            received.json()["purpose_allocation"]["finished_inventory_lot_id"]
        )
        created = client.post(
            "/api/deliveries",
            json={
                "customer_id": 1,
                "items": [
                    {
                        "order_item_id": 1,
                        "delivered_quantity": 602,
                        "over_delivery_confirmed": True,
                        "over_delivery_reason": "客户确认将本次多生产的2只一并送货",
                    }
                ],
            },
        )
        assert created.status_code == 201, created.text
        delivery_id = int(created.json()["id"])
        pick = client.post(f"/api/deliveries/{delivery_id}/pick-task")
        assert pick.status_code == 201, pick.text
        pick_payload = pick.json()
        pick_item = pick_payload["items"][0]
        assert pick_item["location_plan_complete"] is True
        assert sum(
            int(line["pick_quantity"]) for line in pick_item["location_lines"]
        ) == 602
        completed = client.post(
            f"/api/delivery-picks/{pick_payload['id']}/complete-planned"
        )
        assert completed.status_code == 200, completed.text
        dispatched = client.put(f"/api/deliveries/{delivery_id}/dispatch")
        assert dispatched.status_code == 200, dispatched.text
        cancelled = client.put(f"/api/deliveries/{delivery_id}/cancel")
        assert cancelled.status_code == 200, cancelled.text

    with session_factory() as session:
        finished_lot = session.get(InventoryLot, finished_lot_id)
        assert finished_lot is not None
        assert (
            int(finished_lot.quantity_available),
            int(finished_lot.quantity_reserved),
            int(finished_lot.quantity_consumed),
        ) == (2, 600, 0)
        surplus_reservation = session.scalar(
            select(InventoryReservation).where(
                InventoryReservation.order_item_id == 1,
                InventoryReservation.reservation_type
                == "finished_surplus_delivery",
            )
        )
        assert surplus_reservation is not None
        assert surplus_reservation.status == "released"
        assert int(surplus_reservation.released_stock_quantity or 0) == 2
        assert (
            int(surplus_reservation.reserved_stock_quantity or 0)
            - int(surplus_reservation.consumed_stock_quantity or 0)
            - int(surplus_reservation.released_stock_quantity or 0)
        ) == 0

    with TestClient(app) as client:
        _login(client, "admin")
        next_delivery = client.post(
            "/api/deliveries",
            json={
                "customer_id": 1,
                "items": [
                    {
                        "order_item_id": 1,
                        "delivered_quantity": 602,
                        "over_delivery_confirmed": True,
                        "over_delivery_reason": "撤销前一张送货后重新送出全部602只",
                    }
                ],
            },
        )
        assert next_delivery.status_code == 201, next_delivery.text
