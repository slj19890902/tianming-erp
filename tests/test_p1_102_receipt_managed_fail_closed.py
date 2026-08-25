from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from test_p1_81_receipt_purpose_flow import (
    _create_frozen_sources,
    _seed_material_and_staging,
)
from test_phase11_requisition import (
    _add_pending_candidate,
    _login,
    requisition_app,
)


def test_active_frozen_source_identity_conflict_blocks_every_candidate(
    requisition_app,
) -> None:
    from app.api.production import router as production_router
    from app.models.customer import Customer
    from app.models.production import ProductionCompletion
    from app.models.supplier_requisition_order import PurchasePurposeSourceSnapshot
    from app.services.production_workflow import ensure_receipt_auto_main_task
    from app.services.receipt_managed_production import (
        receipt_purpose_summaries_by_order_item_ids,
    )

    app, session_factory = requisition_app
    app.include_router(production_router, prefix="/api/production")
    _seed_material_and_staging(session_factory)
    conflicting_source_item_id = _add_pending_candidate(
        session_factory,
        902,
        product_code="P1102-CONFLICT-CANDIDATE",
        product_name="匿名冲突候选纸箱",
        quantity=30,
        material_code="KAKAK",
        layer_count=5,
        flute_type="AB",
    )

    with TestClient(app) as client:
        _login(client, "admin")
        source = _create_frozen_sources(
            client,
            session_factory,
            order_quantity=30,
            purchase_total=30,
            order_purpose=30,
            stock_purpose=0,
        )[0]

        with session_factory() as session:
            conflicting_customer = Customer(
                customer_number=902,
                customer_code="P1102-CONFLICT",
                name="匿名冻结来源冲突客户",
                payment_term_days=0,
                credit_limit=Decimal("0"),
            )
            session.add(conflicting_customer)
            session.flush()
            snapshot = session.get(
                PurchasePurposeSourceSnapshot,
                source.purpose_snapshot_id,
            )
            assert snapshot is not None
            # Simulate a corrupt active frozen source whose formal supplier
            # parent, source parent and frozen customer no longer identify one
            # sales line.  Every requested candidate must fail closed.
            snapshot.source_order_item_id = conflicting_source_item_id
            snapshot.customer_id = conflicting_customer.id
            tasks = {
                order_item_id: ensure_receipt_auto_main_task(
                    session,
                    order_item_id=order_item_id,
                )
                for order_item_id in (1, conflicting_source_item_id)
            }
            for task in tasks.values():
                task.status = "pending"
                task.planned_quantity = 30
                task.material_received_quantity = 30
                task.material_input_quantity = 30
                task.readiness_basis = "legacy_material_received"
            session.commit()
            task_facts = {
                order_item_id: (int(task.id), int(task.version))
                for order_item_id, task in tasks.items()
            }

        with session_factory() as session:
            summaries = receipt_purpose_summaries_by_order_item_ids(
                session,
                [1, conflicting_source_item_id],
            )
        assert set(summaries) == {1, conflicting_source_item_id}
        for summary in summaries.values():
            assert summary["projection_inconsistent"] is True
            assert summary["component_progress"] == []

        pending = client.get(
            "/api/production/tasks",
            params={"status": "pending", "page": 1, "page_size": 25},
        )
        assert pending.status_code == 200, pending.text
        rows_by_item_id = {
            int(item["order_item_id"]): item for item in pending.json()["items"]
        }
        for order_item_id in (1, conflicting_source_item_id):
            row = rows_by_item_id[order_item_id]
            assert int(row["id"]) == task_facts[order_item_id][0]
            assert row["receipt_purpose_managed"] is True
            assert row["completion_actionable"] is False
            assert (
                row["completion_block_code"]
                == "receipt_auto_projection_inconsistent"
            )

        blocked_task_id, blocked_task_version = task_facts[
            conflicting_source_item_id
        ]
        blocked = client.post(
            "/api/production/completion-batches",
            json={
                "idempotency_key": "p1102-conflict-manual-completion",
                "items": [
                    {
                        "task_id": blocked_task_id,
                        "expected_version": blocked_task_version,
                        "disposition": "direct",
                        "material_input_quantity": 1,
                        "actual_output_quantity": 1,
                        "defective_quantity": 0,
                        "direct_delivery_quantity": 1,
                    }
                ],
            },
        )
        assert blocked.status_code == 409, blocked.text

    with session_factory() as session:
        assert (
            int(session.scalar(select(func.count(ProductionCompletion.id))) or 0)
            == 0
        )


def test_pairing_zero_capacity_lists_every_waiting_bottleneck(
    requisition_app,
) -> None:
    from app.services.production_workflow import _receipt_managed_completion_block
    from app.services.receipt_managed_production import (
        receipt_purpose_summaries_by_order_item_ids,
    )

    app, session_factory = requisition_app
    _seed_material_and_staging(session_factory)
    with TestClient(app) as client:
        _login(client, "admin")
        _create_frozen_sources(
            client,
            session_factory,
            order_quantity=30,
            purchase_total=30,
            order_purpose=30,
            stock_purpose=0,
            composite=True,
        )

    with session_factory() as session:
        summary = receipt_purpose_summaries_by_order_item_ids(session, [1])[1]

    assert len(summary["waiting_component_labels"]) == 2
    assert summary["waiting_component_gap_quantity"] == 0
    assert {
        row["component_type"]
        for row in summary["component_progress"]
        if row["is_bottleneck"]
    } == {"cover", "base"}
    code, message = _receipt_managed_completion_block(summary, automatic_output=0)
    assert code == "receipt_auto_managed"
    assert "后续来料" in message
    assert "当前没有尚未结转" not in message
