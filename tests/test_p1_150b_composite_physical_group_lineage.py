from __future__ import annotations

from datetime import datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.test_p1_150b_composite_physical_group_receipts import (
    _login,
    _post_two_source_group_receipt,
    receipt_group_app,
)
from tests.test_p1_150b_composite_physical_group_planning import (
    physical_group_app as _planning_group_app,
)


def test_group_receipt_is_visible_in_every_source_trace_and_cost_lineage(
    receipt_group_app,
) -> None:
    from app.models.composite_purchase_group import (
        CompositePhysicalGroupReceipt,
        CompositePhysicalPurchaseGroupSource,
    )
    from app.models.customer import Customer
    from app.models.order import Order, OrderItem
    from app.models.warehouse_inventory import InventoryLot
    from app.services.incomplete_order_chain_audit import (
        audit_incomplete_order_chains,
    )
    from app.services.material_cost_lineage import (
        resolve_lot_actual_material_cost,
    )
    from app.services.order_document_trace import (
        build_order_item_document_trace,
    )

    app, factory = receipt_group_app
    with TestClient(app) as client:
        _login(client)
        group_row, _payload, received = _post_two_source_group_receipt(
            client,
            factory,
            key_prefix="p1-150b-lineage",
        )

    with factory() as db:
        group_receipt = db.get(
            CompositePhysicalGroupReceipt,
            db.scalar(
                select(CompositePhysicalGroupReceipt.id).where(
                    CompositePhysicalGroupReceipt.incoming_receipt_item_id
                    == int(received["receipt_item_id"])
                )
            ),
        )
        assert group_receipt is not None
        component_lot = db.get(
            InventoryLot,
            group_receipt.component_inventory_lot_id,
        )
        assert component_lot is not None

        resolved = resolve_lot_actual_material_cost(db, component_lot)
        assert resolved is not None
        assert resolved.purpose_allocation is None
        assert resolved.composite_group_receipt is not None
        assert resolved.composite_group_receipt.id == group_receipt.id
        assert resolved.purchase_fact.id == group_receipt.purchase_receipt_fact_id
        assert resolved.unit_material_cost == group_receipt.component_unit_material_cost

        sources = list(
            db.scalars(
                select(CompositePhysicalPurchaseGroupSource).order_by(
                    CompositePhysicalPurchaseGroupSource.source_sequence
                )
            ).all()
        )
        assert len(sources) == 2
        for source in sources:
            item = db.get(OrderItem, source.order_item_id)
            assert item is not None
            order = db.get(Order, item.order_id)
            customer = db.get(Customer, order.customer_id) if order is not None else None
            assert order is not None and customer is not None
            trace = build_order_item_document_trace(
                db,
                order=order,
                item=item,
                customer_name=customer.name,
                display_order_number=order.order_number,
                permissions={
                    "requisition.view",
                    "incoming.view",
                    "production.view",
                    "warehouse.view",
                    "cost.view",
                },
            )
            receipt_events = [
                event
                for event in trace["events"]
                if event["source_type"] == "composite_physical_group_receipt"
            ]
            assert len(receipt_events) == 1
            event = receipt_events[0]
            assert event["source_id"] == group_receipt.id
            assert event["quantity"] == group_receipt.received_sheet_quantity
            assert event["details"]["composite_physical_purchase_group_id"] == int(
                str(group_row["item_id"])[2:]
            )
            assert event["details"]["purchase_receipt_fact_id"] == (
                group_receipt.purchase_receipt_fact_id
            )
            source_details = event["details"]["sources"]
            assert [row["group_source_id"] for row in source_details] == [source.id]

        report = audit_incomplete_order_chains(
            db,
            anonymization_key=b"p1-150b-read-only-audit-key",
            generated_at=datetime(2026, 9, 3, 12, 0, 0),
        )
        assert not any(
            finding["code"] == "P015_TRACE_LINK_BROKEN"
            and finding.get("evidence", {}).get("receipt_item_id")
            == int(received["receipt_item_id"])
            for finding in report["findings"]
        )
        assert not any(
            finding["code"] == "P015_RECEIPT_PURPOSE_ALLOCATION_UNBALANCED"
            and finding.get("evidence", {}).get(
                "composite_physical_group_receipt_id"
            )
            == group_receipt.id
            for finding in report["findings"]
        )
