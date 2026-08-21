from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.api.deliveries import (
    _PendingDeliveryReadContext,
    _collect_delivery_lines,
    _pending_query,
)
from app.api.orders import (
    OrderStatusRequest,
    WorkflowRollbackRequest,
    rollback_order_workflow,
    update_order_status,
)
from app.models.external_packaging_purchase import (
    ExternalPackagingPurchaseCancellation,
    ExternalPackagingPurchaseItem,
)
from app.models.order import Order, OrderItem
from app.services.external_packaging_receiving import build_external_receiving_overview
from app.services.external_packaging_purchase import (
    ExternalPurchaseContractError,
    build_external_purchase_preview,
    build_external_purchase_print,
    get_external_purchase_summaries_by_order_ids,
    get_external_purchase_summary,
    list_external_purchase_routing_rows,
)
from app.services.order_business_status import build_order_business_statuses
from test_p1_33c3_external_packaging_purchase_confirmation import purchase_app
from test_p1_33c5_external_packaging_receiving import _confirm, _login, _pending


__all__ = ["purchase_app"]


def _order_and_item(db, order_id: int) -> tuple[Order, OrderItem]:
    order = db.get(Order, order_id)
    item = db.scalar(select(OrderItem).where(OrderItem.order_id == order_id))
    assert order is not None and item is not None
    return order, item


def _make_pure_external_snapshot(item: OrderItem) -> None:
    item.supply_mode_snapshot = "external_purchase"
    item.external_packaging_category_code_snapshot = "paper_corner_guard"
    item.external_packaging_specification_json_snapshot = (
        '{"length_mm":780,"side_a_mm":50,"side_b_mm":50,"thickness_mm":5}'
    )
    item.external_packaging_specification_summary_snapshot = "780×50×50×5mm"
    item.external_packaging_purchase_unit_snapshot = "根"
    item.external_packaging_candidate_snapshot_json = "[]"
    item.external_packaging_product_version_snapshot = 1
    item.external_packaging_order_quantity_basis_snapshot = Decimal("1")
    item.external_packaging_purchase_quantity_basis_snapshot = Decimal("1")
    item.external_packaging_quantity_per_finished_unit_snapshot = Decimal("1")


def test_external_purchase_moves_order_to_pending_incoming(
    purchase_app,
) -> None:
    order_id = purchase_app.state.fixture["order_id"]
    with TestClient(purchase_app) as client:
        _login(client, "purchase-admin")
        _confirm(client, order_id)

    with purchase_app.state.session_factory() as db:
        order, item = _order_and_item(db, order_id)
        _make_pure_external_snapshot(item)
        projection = build_order_business_statuses(db, [order])[order_id]
        assert projection["business_status"] == "pending_incoming"
        assert projection["items"][item.id]["business_status"] == "pending_incoming"
        assert (
            projection["items"][item.id]["business_status_evidence"]["basis"]
            == "confirmed_external_packaging_purchase"
        )


def test_purchase_quantity_cannot_be_lower_than_frozen_requirement(
    purchase_app,
) -> None:
    order_id = purchase_app.state.fixture["order_id"]
    with TestClient(purchase_app) as client:
        _login(client, "purchase-admin")
        preview = client.get(
            f"/api/orders/{order_id}/external-packaging-purchase"
        ).json()
        lines = [
            {
                "order_component_id": row["order_component_id"],
                "candidate_id": row["default_candidate_id"],
                "purchase_quantity": (
                    "2" if index == 0 else row["suggested_purchase_quantity"]
                ),
            }
            for index, row in enumerate(preview["items"])
        ]
        response = client.post(
            f"/api/orders/{order_id}/external-packaging-purchase/confirm",
            json={"idempotency_key": "p1-54-under-purchase", "lines": lines},
        )
        assert response.status_code == 422
        assert "不能低于冻结需求" in response.text


def test_pure_external_packaging_receipt_goes_directly_to_pending_delivery(
    purchase_app,
) -> None:
    order_id = purchase_app.state.fixture["order_id"]
    with TestClient(purchase_app) as client:
        _login(client, "purchase-admin")
        _confirm(client, order_id)
        overview = _pending(client)
        for purchase in overview["purchase_orders"]:
            response = client.post(
                f"/api/external-packaging-purchases/{purchase['id']}/receipts",
                json={
                    "idempotency_key": f"p1-54-receipt-{purchase['id']}",
                    "lines": [
                        {
                            "purchase_item_id": row["purchase_item_id"],
                            "received_quantity": row["remaining_quantity"],
                        }
                        for row in purchase["items"]
                    ],
                },
            )
            assert response.status_code == 200, response.text

    with purchase_app.state.session_factory() as db:
        order, item = _order_and_item(db, order_id)
        _make_pure_external_snapshot(item)
        projection = build_order_business_statuses(db, [order])[order_id]
        assert projection["business_status"] == "pending_delivery"
        assert projection["items"][item.id]["business_status"] == "pending_delivery"
        assert (
            projection["items"][item.id]["business_status_evidence"]["basis"]
            == "external_packaging_received"
        )

        pending_ids = {
            int(row._mapping["order_item_id"])
            for row in db.execute(_pending_query())
        }
        assert item.id in pending_ids
        rows = list(db.execute(_pending_query()))
        context = _PendingDeliveryReadContext(db, rows)
        assert context.is_fast(item)
        assert context.remaining_quantity(db, item) == item.quantity

        from app.api.deliveries import DeliveryLineCreate
        from app.models.user import User

        admin = db.scalar(select(User).where(User.username == "purchase-admin"))
        built, total, warnings = _collect_delivery_lines(
            db,
            customer_id=order.customer_id,
            lines=[
                DeliveryLineCreate(
                    order_item_id=item.id,
                    delivered_quantity=item.quantity,
                )
            ],
            user=admin,
        )
        assert [(row.id, line.delivered_quantity) for row, line in built] == [
            (item.id, item.quantity)
        ]
        assert total == item.quantity
        assert warnings == []


def test_mixed_bom_requires_all_receipts_and_completed_production_for_delivery(
    purchase_app,
) -> None:
    from datetime import datetime

    from app.api.deliveries import DeliveryLineCreate, pending_delivery_items
    from app.models.production import (
        ProductionCompletion,
        ProductionCompletionBatch,
        ProductionTask,
    )
    from app.models.supplier_requisition_order import (
        SupplierRequisitionOrder,
        SupplierRequisitionOrderItem,
    )
    from app.models.user import User

    order_id = purchase_app.state.fixture["order_id"]
    with TestClient(purchase_app) as client:
        _login(client, "purchase-admin")
        _confirm(client, order_id)

    with purchase_app.state.session_factory() as db:
        admin = db.scalar(select(User).where(User.username == "purchase-admin"))
        order, item = _order_and_item(db, order_id)
        item.supply_mode_snapshot = "mixed_bom"
        item.material_status = "received"

        supplier_order = SupplierRequisitionOrder(
            order_number=f"SR-MIX-{order_id}",
            supplier_name="paperboard-supplier",
            total_quantity=item.quantity,
            requisition_qty=item.quantity,
            status="confirmed",
        )
        db.add(supplier_order)
        db.flush()
        db.add(
            SupplierRequisitionOrderItem(
                supplier_order_id=supplier_order.id,
                order_item_id=item.id,
                order_number=order.order_number,
                product_code=item.snapshot_product_code,
                product_name=item.snapshot_product_name,
                quantity=item.quantity,
                requisition_qty=item.quantity,
            )
        )
        db.flush()

        # Paperboard may arrive before a production task is created.  That
        # must not let a mixed-BOM row leak into the delivery candidate list
        # while its external-packaging component is still unreceived.
        projection = build_order_business_statuses(db, [order])[order_id]
        assert projection["business_status"] == "pending_incoming"
        pending_ids = {
            int(row["order_item_id"])
            for row in pending_delivery_items(db=db, user=admin)["items"]
        }
        assert item.id not in pending_ids

        task = ProductionTask(
            order_item_id=item.id,
            status="completed",
            planned_quantity=item.quantity,
            ordered_quantity_snapshot=item.quantity,
            material_received_quantity=item.quantity,
            material_input_quantity=item.quantity,
            finished_coverage_snapshot=0,
            output_factor=1,
            version=1,
        )
        db.add(task)
        db.flush()

        # A prematurely completed production task must not bypass the
        # unreceived external-packaging gate.
        projection = build_order_business_statuses(db, [order])[order_id]
        assert projection["business_status"] == "pending_incoming"
        assert projection["items"][item.id]["business_status"] == "pending_incoming"
        pending_ids = {
            int(row["order_item_id"])
            for row in pending_delivery_items(db=db, user=admin)["items"]
        }
        assert item.id not in pending_ids
        with pytest.raises(HTTPException) as unreceived_error:
            _collect_delivery_lines(
                db,
                customer_id=order.customer_id,
                lines=[
                    DeliveryLineCreate(
                        order_item_id=item.id,
                        delivered_quantity=item.quantity,
                    )
                ],
                user=admin,
            )
        assert unreceived_error.value.status_code == 409

        # Continue through the normal sequence: production is still pending
        # while the two incoming paths are completed.
        task.status = "pending"
        db.commit()

    with TestClient(purchase_app) as client:
        _login(client, "purchase-admin")
        overview = _pending(client)
        for purchase in overview["purchase_orders"]:
            response = client.post(
                f"/api/external-packaging-purchases/{purchase['id']}/receipts",
                json={
                    "idempotency_key": f"p1-54-mixed-receipt-{purchase['id']}",
                    "lines": [
                        {
                            "purchase_item_id": row["purchase_item_id"],
                            "received_quantity": row["remaining_quantity"],
                        }
                        for row in purchase["items"]
                    ],
                },
            )
            assert response.status_code == 200, response.text

    with purchase_app.state.session_factory() as db:
        admin = db.scalar(select(User).where(User.username == "purchase-admin"))
        order, item = _order_and_item(db, order_id)
        task = db.scalar(
            select(ProductionTask).where(ProductionTask.order_item_id == item.id)
        )
        assert task is not None

        projection = build_order_business_statuses(db, [order])[order_id]
        assert projection["business_status"] == "pending_production"
        assert projection["items"][item.id]["business_status"] == "pending_production"
        pending_ids = {
            int(row["order_item_id"])
            for row in pending_delivery_items(db=db, user=admin)["items"]
        }
        assert item.id not in pending_ids
        with pytest.raises(HTTPException) as production_error:
            _collect_delivery_lines(
                db,
                customer_id=order.customer_id,
                lines=[
                    DeliveryLineCreate(
                        order_item_id=item.id,
                        delivered_quantity=item.quantity,
                    )
                ],
                user=admin,
            )
        assert production_error.value.status_code == 400

        task.status = "completed"
        completion_batch = ProductionCompletionBatch(
            idempotency_key=f"p1-54-mixed-completion-{item.id}",
            request_hash="a" * 64,
            item_count=1,
            completed_by=admin.id,
            completed_at=datetime(2026, 8, 13, 10, 0),
        )
        db.add(completion_batch)
        db.flush()
        db.add(
            ProductionCompletion(
                batch_id=completion_batch.id,
                task_id=task.id,
                order_item_id=item.id,
                expected_version=task.version,
                quantity=item.quantity,
                completion_type="primary",
                material_input_quantity=item.quantity,
                planned_output_quantity=item.quantity,
                actual_output_quantity=item.quantity,
                defective_quantity=0,
                order_reserved_quantity=item.quantity,
                direct_delivery_quantity=item.quantity,
                stock_quantity=0,
                surplus_finished_quantity=0,
                initial_disposition="direct",
                status="posted",
                completed_by=admin.id,
                completed_at=datetime(2026, 8, 13, 10, 0),
            )
        )
        db.flush()
        projection = build_order_business_statuses(db, [order])[order_id]
        assert projection["business_status"] == "pending_delivery"
        assert projection["items"][item.id]["business_status"] == "pending_delivery"
        pending_ids = {
            int(row["order_item_id"])
            for row in pending_delivery_items(db=db, user=admin)["items"]
        }
        assert item.id in pending_ids

        built, total, warnings = _collect_delivery_lines(
            db,
            customer_id=order.customer_id,
            lines=[
                DeliveryLineCreate(
                    order_item_id=item.id,
                    delivered_quantity=item.quantity,
                )
            ],
            user=admin,
        )
        assert [(row.id, line.delivered_quantity) for row, line in built] == [
            (item.id, item.quantity)
        ]
        assert total == item.quantity
        assert warnings == []


def test_historical_composite_mixed_bom_draft_cannot_dispatch_before_external_receipt(
    purchase_app,
) -> None:
    from datetime import date, datetime
    from decimal import Decimal

    from app.api.deliveries import dispatch_delivery
    from app.models.delivery import Delivery, DeliveryItem
    from app.models.product import Product
    from app.models.product_bom import (
        BomComponentDirectDeliveryAllocation,
        SalesOrderItemBomComponent,
    )
    from app.models.production import (
        ProductionCompletion,
        ProductionCompletionBatch,
        ProductionTask,
    )
    from app.models.user import User

    order_id = purchase_app.state.fixture["order_id"]
    with TestClient(purchase_app) as client:
        _login(client, "purchase-admin")
        _confirm(client, order_id)

    with purchase_app.state.session_factory() as db:
        admin = db.scalar(select(User).where(User.username == "purchase-admin"))
        order, item = _order_and_item(db, order_id)
        item.supply_mode_snapshot = "mixed_bom"
        item.material_status = "received"

        component = Product(
            customer_id=order.customer_id,
            product_code=f"P1-54-COMPONENT-{order_id}",
            customer_material_code=f"P1-54-COMPONENT-{order_id}",
            product_name="P1-54 组合内件",
            unit="只",
            box_category="normal",
            is_internal_component=True,
        )
        db.add(component)
        db.flush()
        snapshot = SalesOrderItemBomComponent(
            sales_order_item_id=item.id,
            component_product_id=component.id,
            parent_product_version=1,
            component_product_version=1,
            snapshot_schema_version=3,
            order_set_quantity=item.quantity,
            quantity_per_set=Decimal("1"),
            required_piece_quantity=Decimal(item.quantity),
            display_order=1,
            internal_component_code="P1-54-COMPONENT",
            is_die_cut=False,
            spare_sheet_quantity=0,
            display_mode="show_on_delivery",
            is_required=True,
            snapshot_component_product_code=component.product_code,
            snapshot_component_product_name=component.product_name,
            snapshot_component_spec="组合内件规格",
            snapshot_component_box_category="normal",
            snapshot_component_default_cutting_mode="一开一",
        )
        db.add(snapshot)
        db.flush()
        task = ProductionTask(
            order_item_id=item.id,
            sales_order_item_bom_component_id=snapshot.id,
            task_role="component_internal",
            status="completed",
            planned_quantity=item.quantity,
            ordered_quantity_snapshot=item.quantity,
            material_received_quantity=item.quantity,
            material_input_quantity=item.quantity,
            finished_coverage_snapshot=0,
            output_factor=1,
            version=1,
        )
        db.add(task)
        db.flush()
        completion_batch = ProductionCompletionBatch(
            idempotency_key=f"p1-54-composite-dispatch-{item.id}",
            request_hash="b" * 64,
            item_count=1,
            completed_by=admin.id,
            completed_at=datetime(2026, 8, 13, 11, 0),
        )
        db.add(completion_batch)
        db.flush()
        db.add(
            ProductionCompletion(
                batch_id=completion_batch.id,
                task_id=task.id,
                order_item_id=item.id,
                expected_version=task.version,
                quantity=item.quantity,
                completion_type="primary",
                material_input_quantity=item.quantity,
                planned_output_quantity=item.quantity,
                actual_output_quantity=item.quantity,
                defective_quantity=0,
                order_reserved_quantity=item.quantity,
                direct_delivery_quantity=item.quantity,
                stock_quantity=0,
                surplus_finished_quantity=0,
                initial_disposition="direct",
                status="posted",
                completed_by=admin.id,
                completed_at=datetime(2026, 8, 13, 11, 0),
            )
        )
        delivery = Delivery(
            delivery_number=f"P1-54-HISTORICAL-{order_id}",
            customer_id=order.customer_id,
            delivery_date=date(2026, 8, 13),
            source_mode="order",
            status="pending",
            total_quantity=item.quantity,
            created_by=admin.id,
        )
        db.add(delivery)
        db.flush()
        delivery_item = DeliveryItem(
            delivery_id=delivery.id,
            source_type="order",
            order_item_id=item.id,
            delivered_quantity=item.quantity,
            ordered_quantity_snapshot=item.quantity,
            order_remaining_snapshot=item.quantity,
        )
        db.add(delivery_item)
        db.commit()
        delivery_id = delivery.id
        delivery_item_id = delivery_item.id
        item_id = item.id

        with pytest.raises(HTTPException) as dispatch_error:
            dispatch_delivery(delivery_id, db=db, user=admin)
        assert dispatch_error.value.status_code == 409

        db.expire_all()
        blocked_delivery = db.get(Delivery, delivery_id)
        blocked_item = db.get(OrderItem, item_id)
        assert blocked_delivery.status == "pending"
        assert blocked_delivery.dispatched_at is None
        assert blocked_delivery.dispatched_by is None
        assert blocked_delivery.ever_dispatched_at is None
        assert blocked_item.delivered_quantity == 0
        assert db.scalar(
            select(func.count())
            .select_from(BomComponentDirectDeliveryAllocation)
            .where(
                BomComponentDirectDeliveryAllocation.delivery_item_id
                == delivery_item_id
            )
        ) == 0


def test_workflow_rollback_cancels_unreceived_external_purchase_and_hides_pending(
    purchase_app,
) -> None:
    order_id = purchase_app.state.fixture["order_id"]
    with TestClient(purchase_app) as client:
        _login(client, "purchase-admin")
        confirmed = _confirm(client, order_id)
    purchase_ids = {int(row["id"]) for row in confirmed["purchase_orders"]}

    with purchase_app.state.session_factory() as db:
        from app.models.user import User

        admin = db.scalar(select(User).where(User.username == "purchase-admin"))
        result = rollback_order_workflow(
            order_id,
            WorkflowRollbackRequest(reason="P1-54 无实收撤回"),
            db=db,
            user=admin,
        )
        assert result["business_status"] == "pending_material"
        cancelled_ids = set(
            db.scalars(
                select(ExternalPackagingPurchaseCancellation.purchase_order_id)
            ).all()
        )
        assert cancelled_ids == purchase_ids
        assert build_external_receiving_overview(
            db, visible_customer_ids=None
        )["purchase_orders"] == []
        assert list_external_purchase_routing_rows(
            db, visible_customer_ids=None
        ) == []


def test_cancelled_status_cancels_unreceived_purchase_and_blocks_receipt(
    purchase_app,
) -> None:
    order_id = purchase_app.state.fixture["order_id"]
    with TestClient(purchase_app) as client:
        _login(client, "purchase-admin")
        confirmed = _confirm(client, order_id)
    purchase = confirmed["purchase_orders"][0]

    from app.models.user import User

    with purchase_app.state.session_factory() as db:
        admin = db.scalar(select(User).where(User.username == "purchase-admin"))
        result = update_order_status(
            order_id,
            OrderStatusRequest(status="cancelled"),
            db=db,
            user=admin,
        )
        assert result["business_status"] == "cancelled"
        assert build_external_receiving_overview(
            db, visible_customer_ids=None
        )["purchase_orders"] == []

    with TestClient(purchase_app) as client:
        _login(client, "purchase-admin")
        response = client.post(
            f"/api/external-packaging-purchases/{purchase['id']}/receipts",
            json={
                "idempotency_key": "p1-54-cancelled-receipt",
                "lines": [
                    {
                        "purchase_item_id": purchase["items"][0]["id"],
                        "received_quantity": "1",
                    }
                ],
            },
        )
        assert response.status_code == 409
        assert "作废" in response.text or "终止" in response.text
        print_response = client.get(
            f"/api/external-packaging-purchases/{purchase['id']}/print"
        )
        assert print_response.status_code == 409
        assert "禁止继续打印" in print_response.text


def test_historical_cancelled_order_without_cancellation_fact_is_hidden_and_blocked(
    purchase_app,
) -> None:
    """The on-site ghost record must disappear before its one-row repair is approved."""

    order_id = purchase_app.state.fixture["order_id"]
    with TestClient(purchase_app) as client:
        _login(client, "purchase-admin")
        confirmed = _confirm(client, order_id)
    purchase_id = int(confirmed["purchase_orders"][0]["id"])

    with purchase_app.state.session_factory() as db:
        order = db.get(Order, order_id)
        assert order is not None
        order.status = "cancelled"
        db.commit()

    with purchase_app.state.session_factory() as db:
        assert db.scalar(
            select(func.count()).select_from(
                ExternalPackagingPurchaseCancellation
            )
        ) == 0
        assert get_external_purchase_summary(db, order_id)["status"] == "cancelled"
        assert (
            get_external_purchase_summaries_by_order_ids(db, [order_id])[order_id][
                "status"
            ]
            == "cancelled"
        )
        assert build_external_receiving_overview(
            db, visible_customer_ids=None
        )["purchase_orders"] == []
        assert list_external_purchase_routing_rows(
            db, visible_customer_ids=None
        ) == []
        assert build_external_purchase_preview(db, order_id)["status"] == "cancelled"
        with pytest.raises(ExternalPurchaseContractError, match="终止"):
            build_external_purchase_print(db, purchase_id)

    with TestClient(purchase_app) as client:
        _login(client, "purchase-admin")
        response = client.post(
            f"/api/external-packaging-purchases/{purchase_id}/receipts",
            json={
                "idempotency_key": "p1-54-historical-cancelled-receipt",
                "lines": [
                    {
                        "purchase_item_id": confirmed["purchase_orders"][0][
                            "items"
                        ][0]["id"],
                        "received_quantity": "1",
                    }
                ],
            },
        )
        assert response.status_code == 409
        assert "终止" in response.text


def test_rollback_and_cancelled_status_reject_received_external_purchase(
    purchase_app,
) -> None:
    order_id = purchase_app.state.fixture["order_id"]
    with TestClient(purchase_app) as client:
        _login(client, "purchase-admin")
        _confirm(client, order_id)
        purchase = _pending(client)["purchase_orders"][0]
        line = purchase["items"][0]
        response = client.post(
            f"/api/external-packaging-purchases/{purchase['id']}/receipts",
            json={
                "idempotency_key": "p1-54-partial-receipt",
                "lines": [
                    {
                        "purchase_item_id": line["purchase_item_id"],
                        "received_quantity": "1",
                    }
                ],
            },
        )
        assert response.status_code == 200, response.text

    from app.models.user import User

    with purchase_app.state.session_factory() as db:
        admin = db.scalar(select(User).where(User.username == "purchase-admin"))
        with pytest.raises(HTTPException, match="已有实收") as rollback_error:
            rollback_order_workflow(
                order_id,
                WorkflowRollbackRequest(reason="P1-54 有实收撤回"),
                db=db,
                user=admin,
            )
        assert rollback_error.value.status_code == 409

    with purchase_app.state.session_factory() as db:
        admin = db.scalar(select(User).where(User.username == "purchase-admin"))
        with pytest.raises(HTTPException, match="已有实收") as status_error:
            update_order_status(
                order_id,
                OrderStatusRequest(status="cancelled"),
                db=db,
                user=admin,
            )
        assert status_error.value.status_code == 409
        assert db.get(Order, order_id).status != "cancelled"


def _audit_count(db, order_id: int, action_code: str) -> int:
    from app.models.audit import OperationLog

    return int(
        db.scalar(
            select(func.count())
            .select_from(OperationLog)
            .where(
                OperationLog.entity_type == "order",
                OperationLog.entity_id == order_id,
                OperationLog.action_code == action_code,
            )
        )
        or 0
    )


def test_repeat_rollback_and_cancel_are_idempotent_and_terminal_order_cannot_revive(
    purchase_app,
) -> None:
    order_id = purchase_app.state.fixture["order_id"]
    with TestClient(purchase_app) as client:
        _login(client, "purchase-admin")
        _confirm(client, order_id)

    from app.models.user import User

    with purchase_app.state.session_factory() as db:
        admin = db.scalar(select(User).where(User.username == "purchase-admin"))
        first = rollback_order_workflow(
            order_id,
            WorkflowRollbackRequest(reason="P1-54 幂等撤回"),
            db=db,
            user=admin,
        )
        second = rollback_order_workflow(
            order_id,
            WorkflowRollbackRequest(reason="P1-54 幂等撤回"),
            db=db,
            user=admin,
        )
        assert first["business_status"] == second["business_status"] == "pending_material"
        assert _audit_count(db, order_id, "order.workflow_rollback") == 1
        assert db.scalar(
            select(func.count()).select_from(ExternalPackagingPurchaseCancellation)
        ) == 2

        cancelled = update_order_status(
            order_id,
            OrderStatusRequest(status="cancelled"),
            db=db,
            user=admin,
        )
        repeated = update_order_status(
            order_id,
            OrderStatusRequest(status="cancelled"),
            db=db,
            user=admin,
        )
        assert cancelled["business_status"] == repeated["business_status"] == "cancelled"
        assert _audit_count(db, order_id, "order.status_change") == 1

        after_terminal_rollback = rollback_order_workflow(
            order_id,
            WorkflowRollbackRequest(reason="不得复活终止订单"),
            db=db,
            user=admin,
        )
        assert after_terminal_rollback["business_status"] == "cancelled"
        assert db.get(Order, order_id).status == "cancelled"
        assert _audit_count(db, order_id, "order.workflow_rollback") == 1


def test_close_blocks_unreceived_purchase_but_allows_fully_received_purchase(
    purchase_app,
) -> None:
    order_id = purchase_app.state.fixture["order_id"]
    with TestClient(purchase_app) as client:
        _login(client, "purchase-admin")
        _confirm(client, order_id)

    from app.models.user import User

    with purchase_app.state.session_factory() as db:
        admin = db.scalar(select(User).where(User.username == "purchase-admin"))
        with pytest.raises(HTTPException, match="未收齐") as pending_error:
            update_order_status(
                order_id,
                OrderStatusRequest(status="closed"),
                db=db,
                user=admin,
            )
        assert pending_error.value.status_code == 409

    with TestClient(purchase_app) as client:
        _login(client, "purchase-admin")
        overview = _pending(client)
        for purchase in overview["purchase_orders"]:
            response = client.post(
                f"/api/external-packaging-purchases/{purchase['id']}/receipts",
                json={
                    "idempotency_key": f"p1-54-close-{purchase['id']}",
                    "lines": [
                        {
                            "purchase_item_id": row["purchase_item_id"],
                            "received_quantity": row["remaining_quantity"],
                        }
                        for row in purchase["items"]
                    ],
                },
            )
            assert response.status_code == 200, response.text

    with purchase_app.state.session_factory() as db:
        admin = db.scalar(select(User).where(User.username == "purchase-admin"))
        result = update_order_status(
            order_id,
            OrderStatusRequest(status="closed"),
            db=db,
            user=admin,
        )
        assert result["business_status"] == "closed"
        summary = get_external_purchase_summary(db, order_id)
        list_summary = get_external_purchase_summaries_by_order_ids(
            db, [order_id]
        )[order_id]
        assert summary["status"] == list_summary["status"] == "confirmed"
        assert summary["receipt_status"] == "received"
        assert summary["purchase_numbers"]
        preview = build_external_purchase_preview(db, order_id)
        assert preview["status"] == "confirmed"
        assert {
            row["purchase_number"]
            for row in preview["confirmation"]["purchase_orders"]
        } == set(summary["purchase_numbers"])
        purchase_id = summary["purchase_orders"][0]["id"]
        assert build_external_purchase_print(db, purchase_id)["purchase_number"]

        archived = update_order_status(
            order_id,
            OrderStatusRequest(status="archived"),
            db=db,
            user=admin,
        )
        assert archived["business_status"] == "archived"
        assert get_external_purchase_summary(db, order_id)["status"] == "confirmed"
        assert build_external_purchase_preview(db, order_id)["status"] == "confirmed"
        assert build_external_purchase_print(db, purchase_id)["purchase_number"]


def test_cancelled_purchase_history_blocks_single_item_delete_with_409(
    purchase_app,
) -> None:
    from app.models.user import User

    order_id = purchase_app.state.fixture["order_id"]
    with TestClient(purchase_app) as client:
        _login(client, "purchase-admin")
        _confirm(client, order_id)

    with purchase_app.state.session_factory() as db:
        admin = db.scalar(select(User).where(User.username == "purchase-admin"))
        order, item = _order_and_item(db, order_id)
        db.add(
            OrderItem(
                order_id=order.id,
                product_id=item.product_id,
                item_order_number=f"{order.order_number}-002",
                item_sequence=2,
                quantity=1,
                unit_price=item.unit_price,
                subtotal=item.unit_price,
                snapshot_product_name="保留明细",
                snapshot_product_code="P1-54-KEEP",
                material_status="pending",
                requisition_status="未报料",
            )
        )
        db.commit()
        rollback_order_workflow(
            order_id,
            WorkflowRollbackRequest(reason="P1-54 删除历史门禁"),
            db=db,
            user=admin,
        )

        from app.api.orders import delete_order_item

        with pytest.raises(HTTPException, match="外购包材采购历史") as error:
            delete_order_item(item.id, db=db, user=admin)
        assert error.value.status_code == 409
        assert db.get(OrderItem, item.id) is not None
        assert db.scalar(
            select(func.count())
            .select_from(ExternalPackagingPurchaseItem)
            .where(ExternalPackagingPurchaseItem.sales_order_item_id == item.id)
        )
