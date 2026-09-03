from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select

from app.api.requisition import _composite_physical_group_fingerprint
from app.services.composite_physical_group_receipts import (
    IncomingReceiptError,
    _source_order_count,
    _source_payloads,
    split_group_receipt_quantity,
    stable_component_piece_allocations,
)
from tests.test_p1_150b_composite_physical_group_planning import (
    _group_payload_from_pending,
    _login,
    physical_group_app as _planning_group_app,
)
from tests.test_p1_81_receipt_purpose_flow import (
    _seed_material_and_staging,
    _use_p181_published_map_identity,
)
from tests.test_p1_13c_bom_a3_physical_sources import (
    _incoming_groups as _a3_incoming_groups,
    _receive_group as _a3_receive_group,
    _source_payloads as _a3_source_payloads,
    a3_surround_app,
)


@pytest.fixture()
def receipt_group_app(_planning_group_app, monkeypatch):
    """Extend the planning fixture with real receipt routes and warehouses."""

    from app.api.incoming import router as incoming_router

    app, factory = _planning_group_app
    _use_p181_published_map_identity(monkeypatch)
    _seed_material_and_staging(factory)
    app.include_router(incoming_router, prefix="/api/incoming")
    return app, factory


def test_group_component_output_allocates_net_pieces_once_in_stable_order() -> None:
    allocations = stable_component_piece_allocations(
        available_piece_quantity=4,
        sources=[
            {
                "source_id": 22,
                "source_sequence": 2,
                "net_required_piece_quantity": 1,
                "cumulative_reserved_piece_quantity": 0,
            },
            {
                "source_id": 11,
                "source_sequence": 1,
                "net_required_piece_quantity": 1,
                "cumulative_reserved_piece_quantity": 0,
            },
        ],
    )

    assert [row["source_id"] for row in allocations] == [11, 22]
    assert [
        row["allocated_reserved_component_piece_quantity"]
        for row in allocations
    ] == [1, 1]
    assert sum(
        row["allocated_reserved_component_piece_quantity"]
        for row in allocations
    ) == 2
    # One sheet outputs four physical pieces; only two exact source demands are
    # reserved and the remaining two pieces stay free on the same real lot.
    assert 4 - sum(
        row["allocated_reserved_component_piece_quantity"]
        for row in allocations
    ) == 2


def test_partial_group_receipts_preserve_cumulative_net_demand_conservation() -> None:
    first = stable_component_piece_allocations(
        available_piece_quantity=3,
        sources=[
            {
                "source_id": 1,
                "source_sequence": 1,
                "net_required_piece_quantity": 2,
                "cumulative_reserved_piece_quantity": 0,
            },
            {
                "source_id": 2,
                "source_sequence": 2,
                "net_required_piece_quantity": 3,
                "cumulative_reserved_piece_quantity": 0,
            },
        ],
    )
    before = {
        row["source_id"]: row["cumulative_reserved_piece_quantity_after"]
        for row in first
    }
    second = stable_component_piece_allocations(
        available_piece_quantity=3,
        sources=[
            {
                "source_id": 1,
                "source_sequence": 1,
                "net_required_piece_quantity": 2,
                "cumulative_reserved_piece_quantity": before[1],
            },
            {
                "source_id": 2,
                "source_sequence": 2,
                "net_required_piece_quantity": 3,
                "cumulative_reserved_piece_quantity": before[2],
            },
        ],
    )

    assert [(row["source_id"], row["allocated_reserved_component_piece_quantity"])
            for row in first] == [(1, 2), (2, 1)]
    assert [(row["source_id"], row["allocated_reserved_component_piece_quantity"])
            for row in second] == [(2, 2)]
    assert second[0]["cumulative_reserved_piece_quantity_after"] == 3


def test_order_item_waits_for_every_active_physical_group_source(
    a3_surround_app,
) -> None:
    """Receiving A3 cover/whole must not hide its unreceived base sibling."""

    from app.models.composite_purchase_group import (
        CompositePhysicalGroupReceiptSourceAllocation,
        CompositePhysicalPurchaseGroupSource,
    )
    from app.models.order import OrderItem

    app, factory = a3_surround_app
    with TestClient(app) as client:
        _login(client)
        created = client.post(
            "/api/requisition/batches",
            json={
                "supplier_name": "匿名供应商",
                "items": _a3_source_payloads(
                    client,
                    (1, "cover"),
                    (1, "base"),
                    (2, "whole"),
                ),
            },
        )
        assert created.status_code == 201, created.text

        groups = _a3_incoming_groups(client)
        cover, _ = _a3_receive_group(
            client,
            factory,
            groups["cover"],
            quantity=int(groups["cover"]["planned_quantity"]),
            idempotency_key="p1-150b-a3-cover",
        )
        assert cover.status_code == 200, cover.text
        groups = _a3_incoming_groups(client)
        whole, _ = _a3_receive_group(
            client,
            factory,
            groups["whole"],
            quantity=int(groups["whole"]["planned_quantity"]),
            idempotency_key="p1-150b-a3-whole",
        )
        assert whole.status_code == 200, whole.text

    with factory() as db:
        item = db.get(OrderItem, 1)
        assert item is not None
        assert item.material_status == "pending"
        assert item.requisition_status == "已报料"

        sources = db.scalars(
            select(CompositePhysicalPurchaseGroupSource).order_by(
                CompositePhysicalPurchaseGroupSource.id
            )
        ).all()
        allocations = db.scalars(
            select(CompositePhysicalGroupReceiptSourceAllocation).where(
                CompositePhysicalGroupReceiptSourceAllocation.status == "active"
            )
        ).all()
        reserved_by_source = {
            int(source.id): sum(
                int(allocation.allocated_reserved_component_piece_quantity)
                for allocation in allocations
                if allocation.composite_physical_purchase_group_source_id
                == source.id
            )
            for source in sources
        }
        coverage_by_component = {
            source.component_type_snapshot: reserved_by_source[source.id]
            >= int(source.net_required_piece_quantity)
            for source in sources
        }

    assert coverage_by_component == {
        "cover": True,
        "base": False,
        "whole": True,
    }


def test_group_with_explicit_reserve_fills_order_first_then_raw_reserve() -> None:
    first = split_group_receipt_quantity(
        received_sheet_quantity=8,
        cumulative_order_purpose_before=0,
        order_purpose_sheet_quantity=10,
        reserve_sheet_quantity=2,
        surplus_disposition=None,
    )
    second = split_group_receipt_quantity(
        received_sheet_quantity=4,
        cumulative_order_purpose_before=8,
        order_purpose_sheet_quantity=10,
        reserve_sheet_quantity=2,
        surplus_disposition="semi_finished_reserve",
    )

    assert first == (8, 0)
    assert second == (2, 2)


def test_group_without_reserve_keeps_overreceipt_as_component_output() -> None:
    assert split_group_receipt_quantity(
        received_sheet_quantity=3,
        cumulative_order_purpose_before=10,
        order_purpose_sheet_quantity=10,
        reserve_sheet_quantity=0,
        surplus_disposition="finished",
    ) == (3, 0)

    with pytest.raises(IncomingReceiptError) as caught:
        split_group_receipt_quantity(
            received_sheet_quantity=1,
            cumulative_order_purpose_before=10,
            order_purpose_sheet_quantity=10,
            reserve_sheet_quantity=0,
            surplus_disposition="semi_finished_reserve",
        )
    assert caught.value.status_code == 409


def test_source_order_identity_uses_real_order_and_one_batch_query(
    receipt_group_app,
) -> None:
    from app.models.order import OrderItem

    _app, factory = receipt_group_app

    def source(source_id: int, sequence: int, order_item_id: int) -> SimpleNamespace:
        return SimpleNamespace(
            id=source_id,
            source_key=f"source-{source_id}",
            source_sequence=sequence,
            component_type_snapshot="whole",
            requisition_item_id=source_id,
            order_item_id=order_item_id,
            sales_order_item_bom_component_id=source_id,
            purchase_purpose_source_snapshot_id=None,
            required_piece_quantity=1,
            inventory_reserved_piece_quantity=0,
            net_required_piece_quantity=1,
            allocated_order_purpose_sheet_quantity=0,
            spare_sheet_quantity=0,
        )

    statements: list[str] = []
    with factory() as db:
        first_item = db.get(OrderItem, 1)
        assert first_item is not None
        same_order_sibling = OrderItem(
            order_id=first_item.order_id,
            product_id=first_item.product_id,
            item_order_number="P1-150B-001-02",
            item_sequence=2,
            quantity=1,
            unit_price=Decimal("10"),
            subtotal=Decimal("10"),
            material_status="pending",
            requisition_status="未报料",
            snapshot_product_code="SET-P1-150B-SIBLING",
            snapshot_product_name="同订单第二明细",
            snapshot_spec="1 套",
            snapshot_material="虚拟父件",
            special_process="一开一",
        )
        db.add(same_order_sibling)
        db.flush()
        sibling_item_id = int(same_order_sibling.id)
        sources = [
            source(101, 1, 1),
            source(102, 2, sibling_item_id),
            source(103, 3, 2),
        ]
        engine = db.get_bind()

        def capture_statement(
            _connection,
            _cursor,
            statement,
            _parameters,
            _context,
            _executemany,
        ) -> None:
            if str(statement).lstrip().upper().startswith("SELECT"):
                statements.append(str(statement))

        event.listen(engine, "before_cursor_execute", capture_statement)
        try:
            payloads = _source_payloads(db, sources)
        finally:
            event.remove(engine, "before_cursor_execute", capture_statement)

    assert len(statements) == 1
    assert payloads[0]["order_item_id"] != payloads[1]["order_item_id"]
    assert _source_order_count(payloads[:2]) == 1
    assert _source_order_count(payloads) == 2
    assert payloads[0]["order_id"] == payloads[1]["order_id"]
    assert payloads[0]["order_number"] == payloads[1]["order_number"]
    assert payloads[0]["order_number"] == "P1-150B-001"
    assert payloads[2]["order_number"] == "P1-150B-002"


def _freeze_direct_group_receipt_fact(
    client: TestClient,
    factory,
    group_row: dict,
    *,
    idempotency_key: str,
    unit_price: str = "2.500000",
    expected_latest_receipt_fact_version: int = 0,
) -> dict:
    from app.models.composite_purchase_group import CompositePhysicalPurchaseGroup

    group_id = int(str(group_row["item_id"])[2:])
    with factory() as db:
        group = db.get(CompositePhysicalPurchaseGroup, group_id)
        assert group is not None
        material_id = int(group.material_id)
    response = client.put(
        f"/api/requisition/purchase-sources/cg{group_id}/receipt-facts",
        json={
            "actual_material_id": material_id,
            "unit_price": unit_price,
            "currency": "CNY",
            "price_unit": "per_sheet",
            "tax_included": True,
            "tax_rate": "0.13",
            "purchase_purpose_source_snapshot_id": group_row[
                "purchase_purpose_source_snapshot_id"
            ],
            "purpose_snapshot_version": group_row[
                "expected_purpose_snapshot_version"
            ],
            "receipt_plan_fingerprint": group_row["receipt_plan_fingerprint"],
            "expected_source_version": group_row["expected_source_version"],
            "expected_latest_receipt_fact_version": (
                expected_latest_receipt_fact_version
            ),
            "idempotency_key": idempotency_key,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _group_receive_payload(
    group_row: dict,
    receipt_fact: dict,
    *,
    quantity: int,
    idempotency_key: str,
) -> dict:
    return {
        "received_quantity": quantity,
        "expected_group_version": group_row["expected_group_version"],
        "expected_receipt_fact_version": receipt_fact["receipt_fact_version"],
        "purchase_purpose_source_snapshot_id": group_row[
            "purchase_purpose_source_snapshot_id"
        ],
        "expected_purpose_snapshot_version": group_row[
            "expected_purpose_snapshot_version"
        ],
        "receipt_plan_fingerprint": receipt_fact["receipt_plan_fingerprint"],
        "expected_actual_material_version": receipt_fact[
            "actual_material_version"
        ],
        "actual_material_fingerprint": receipt_fact[
            "actual_material_fingerprint"
        ],
        "idempotency_key": idempotency_key,
    }


def _post_two_source_group_receipt(
    client: TestClient,
    factory,
    *,
    key_prefix: str,
) -> tuple[dict, dict, dict]:
    pending = client.get("/api/requisition/pending")
    assert pending.status_code == 200, pending.text
    created = client.post(
        "/api/requisition/batches",
        json=_group_payload_from_pending(pending.json()["items"][:2]),
    )
    assert created.status_code == 201, created.text
    incoming = client.get("/api/incoming/pending")
    assert incoming.status_code == 200, incoming.text
    group_row = next(
        row
        for row in incoming.json()["items"]
        if str(row["item_id"]).startswith("cg")
    )
    assert group_row["source_order_count"] == 2
    assert len({row["order_id"] for row in group_row["source_items"]}) == 2
    assert len({row["order_number"] for row in group_row["source_items"]}) == 2
    receipt_fact = _freeze_direct_group_receipt_fact(
        client,
        factory,
        group_row,
        idempotency_key=f"{key_prefix}-price",
    )
    incoming = client.get("/api/incoming/pending")
    assert incoming.status_code == 200, incoming.text
    group_row = next(
        row
        for row in incoming.json()["items"]
        if row["item_id"] == group_row["item_id"]
    )
    payload = _group_receive_payload(
        group_row,
        receipt_fact,
        quantity=1,
        idempotency_key=f"{key_prefix}-receipt",
    )
    received = client.put(
        f"/api/incoming/receive/{group_row['item_id']}",
        json=payload,
    )
    assert received.status_code == 200, received.text
    received_history = client.get("/api/incoming/received")
    assert received_history.status_code == 200, received_history.text
    assert [
        row["item_id"] for row in received_history.json()["items"]
    ] == [group_row["item_id"]]
    assert received_history.json()["items"][0]["source_count"] == 2
    assert received_history.json()["items"][0]["source_order_count"] == 2
    assert len(
        {
            row["order_id"]
            for row in received_history.json()["items"][0]["source_items"]
        }
    ) == 2
    return group_row, payload, received.json()


def _convert_group_sources_to_supplier_items(factory) -> list[int]:
    """Mirror the finalized supplier conversion facts used by the receipt anchor."""

    from app.models.composite_purchase_group import (
        CompositePhysicalPurchaseGroup,
        CompositePhysicalPurchaseGroupSource,
    )
    from app.models.customer import Customer
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.requisition import RequisitionItem
    from app.models.supplier_requisition_order import (
        PurchasePurposeSourceSnapshot,
        SupplierRequisitionOrder,
        SupplierRequisitionOrderItem,
    )

    with factory() as db:
        group = db.scalar(select(CompositePhysicalPurchaseGroup))
        assert group is not None
        customer = db.get(Customer, group.customer_id)
        component = db.get(Product, group.component_product_id)
        assert customer is not None and component is not None
        header = SupplierRequisitionOrder(
            order_number="P1-150B-SUPPLIER-CONVERTED",
            supplier_name="P1-150B 供应商",
            material_id=group.material_id,
            layer_count=group.layer_count_snapshot,
            flute_type=group.flute_type_snapshot,
            report_length_mm=group.report_length_mm,
            report_width_mm=group.report_width_mm,
            cutting_mode=group.cutting_mode_snapshot,
            pieces_per_box=1,
            required_piece_qty=group.total_required_piece_quantity,
            total_quantity=group.total_required_piece_quantity,
            stock_deduction_qty=0,
            requisition_qty=group.purchase_sheet_quantity,
            status="confirmed",
            created_by=1,
        )
        db.add(header)
        db.flush()
        supplier_item_ids: list[int] = []
        sources = db.scalars(
            select(CompositePhysicalPurchaseGroupSource).order_by(
                CompositePhysicalPurchaseGroupSource.source_sequence
            )
        ).all()
        for source in sources:
            order_item = db.get(OrderItem, source.order_item_id)
            requisition_item = db.get(RequisitionItem, source.requisition_item_id)
            snapshot = db.get(
                PurchasePurposeSourceSnapshot,
                source.purchase_purpose_source_snapshot_id,
            )
            assert order_item is not None and requisition_item is not None
            assert snapshot is not None
            order = db.get(Order, order_item.order_id)
            assert order is not None
            supplier_item = SupplierRequisitionOrderItem(
                supplier_order_id=header.id,
                order_item_id=order_item.id,
                source_key=snapshot.source_key,
                product_id=component.id,
                material_id=group.material_id,
                material_code_snapshot=group.material_code_snapshot,
                supplier_name_snapshot=header.supplier_name,
                layer_count_snapshot=group.layer_count_snapshot,
                flute_type_snapshot=group.flute_type_snapshot,
                order_number=order.order_number,
                product_code=component.product_code,
                product_name=component.product_name,
                report_length_mm=group.report_length_mm,
                report_width_mm=group.report_width_mm,
                quantity=source.required_piece_quantity,
                stock_deduction_qty=0,
                requisition_qty=source.allocated_order_purpose_sheet_quantity,
                cutting_mode=group.cutting_mode_snapshot,
                pieces_per_box=1,
                required_piece_qty=source.required_piece_quantity,
                customer_name=customer.name,
                delivery_date=order.delivery_date,
                status="active",
                version=1,
                purpose_contract_status="frozen",
            )
            db.add(supplier_item)
            db.flush()
            snapshot.supplier_requisition_order_item_id = supplier_item.id
            snapshot.material_requisition_item_id = None
            requisition_item.status = "supplier_requisition_created"
            order_item.supplier_order_number = header.order_number
            supplier_item_ids.append(int(supplier_item.id))
            if source.allocated_order_purpose_sheet_quantity > 0:
                group.supplier_requisition_order_item_id = supplier_item.id
        assert group.supplier_requisition_order_item_id is not None
        db.commit()
        return supplier_item_ids


def _prepare_supplier_converted_group(
    client: TestClient,
    factory,
) -> tuple[dict, list[int], int]:
    """Create one two-source physical group and its supplier-order contract."""

    from app.models.supplier_requisition_order import SupplierRequisitionOrderItem

    pending = client.get("/api/requisition/pending")
    assert pending.status_code == 200, pending.text
    created = client.post(
        "/api/requisition/batches",
        json=_group_payload_from_pending(pending.json()["items"][:2]),
    )
    assert created.status_code == 201, created.text
    supplier_item_ids = _convert_group_sources_to_supplier_items(factory)
    incoming = client.get("/api/incoming/pending")
    assert incoming.status_code == 200, incoming.text
    group_row = next(
        row
        for row in incoming.json()["items"]
        if str(row["item_id"]).startswith("cg")
    )
    with factory() as db:
        supplier_item = db.get(SupplierRequisitionOrderItem, supplier_item_ids[0])
        assert supplier_item is not None
        supplier_order_id = int(supplier_item.supplier_order_id)
    return group_row, supplier_item_ids, supplier_order_id


def _group_price_payload(
    factory,
    group_row: dict,
    *,
    idempotency_key: str,
) -> dict:
    from app.models.composite_purchase_group import CompositePhysicalPurchaseGroup

    group_id = int(str(group_row["item_id"])[2:])
    with factory() as db:
        group = db.get(CompositePhysicalPurchaseGroup, group_id)
        assert group is not None
        material_id = int(group.material_id)
    return {
        "actual_material_id": material_id,
        "unit_price": "2.500000",
        "currency": "CNY",
        "price_unit": "per_sheet",
        "tax_included": True,
        "tax_rate": "0.13",
        "purchase_purpose_source_snapshot_id": group_row[
            "purchase_purpose_source_snapshot_id"
        ],
        "purpose_snapshot_version": group_row["expected_purpose_snapshot_version"],
        "receipt_plan_fingerprint": group_row["receipt_plan_fingerprint"],
        "expected_source_version": group_row["expected_source_version"],
        "expected_latest_receipt_fact_version": 0,
        "idempotency_key": idempotency_key,
    }


def test_supplier_group_item_void_fails_closed_and_whole_order_voids_every_source(
    receipt_group_app,
) -> None:
    from app.models.composite_purchase_group import (
        CompositePhysicalPurchaseGroup,
        CompositePhysicalPurchaseGroupSource,
    )
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.product_bom import RequisitionItemBomSource
    from app.models.requisition import Requisition, RequisitionItem
    from app.models.supplier_requisition_order import (
        SupplierRequisitionOrder,
        SupplierRequisitionOrderItem,
    )

    app, factory = receipt_group_app
    with TestClient(app) as client:
        _login(client)
        group_row, supplier_item_ids, supplier_order_id = (
            _prepare_supplier_converted_group(client, factory)
        )
        blocked_item = client.put(
            f"/api/requisition/supplier-order-items/{supplier_item_ids[0]}/void",
            json={
                "expected_version": 1,
                "idempotency_key": "p1-150b-group-item-void",
                "confirmed": True,
            },
        )
        assert blocked_item.status_code == 409, blocked_item.text
        assert "不能逐明细撤销" in blocked_item.text

        voided = client.put(
            f"/api/requisition/supplier-orders/{supplier_order_id}/void"
        )
        assert voided.status_code == 200, voided.text

        group_id = int(str(group_row["item_id"])[2:])
        blocked_price = client.put(
            f"/api/requisition/purchase-sources/cg{group_id}/receipt-facts",
            json=_group_price_payload(
                factory,
                group_row,
                idempotency_key="p1-150b-voided-group-price",
            ),
        )
        assert blocked_price.status_code == 404, blocked_price.text
        blocked_receipt = client.put(
            f"/api/incoming/receive/cg{group_id}",
            json={
                "received_quantity": 1,
                "idempotency_key": "p1-150b-voided-group-receipt",
            },
        )
        assert blocked_receipt.status_code == 409, blocked_receipt.text
        assert "已作废" in blocked_receipt.text

    with factory() as db:
        group = db.get(CompositePhysicalPurchaseGroup, group_id)
        supplier_order = db.get(SupplierRequisitionOrder, supplier_order_id)
        supplier_items = db.scalars(
            select(SupplierRequisitionOrderItem).where(
                SupplierRequisitionOrderItem.id.in_(supplier_item_ids)
            )
        ).all()
        sources = db.scalars(
            select(CompositePhysicalPurchaseGroupSource).where(
                CompositePhysicalPurchaseGroupSource.composite_physical_purchase_group_id
                == group_id
            )
        ).all()
        requisition_item_ids = [int(source.requisition_item_id) for source in sources]
        requisition_items = db.scalars(
            select(RequisitionItem).where(
                RequisitionItem.id.in_(requisition_item_ids)
            )
        ).all()
        guards = db.scalars(
            select(RequisitionItemBomSource).where(
                RequisitionItemBomSource.requisition_item_id.in_(requisition_item_ids)
            )
        ).all()
        batch_ids = {int(row.requisition_id) for row in requisition_items}
        batches = db.scalars(
            select(Requisition).where(Requisition.id.in_(batch_ids))
        ).all()

        assert group is not None and group.status == "voided" and group.version == 2
        assert supplier_order is not None and supplier_order.status == "voided"
        assert {row.status for row in supplier_items} == {"active"}
        assert {row.status for row in requisition_items} == {"已取消"}
        assert guards and all(row.active_guard is None for row in guards)
        assert batches and {row.status for row in batches} == {"已取消"}
        assert db.scalar(select(IncomingReceiptItem.id).limit(1)) is None


def test_posted_supplier_group_receipt_blocks_item_and_order_void_with_zero_write(
    receipt_group_app,
) -> None:
    from app.models.composite_purchase_group import (
        CompositePhysicalGroupReceipt,
        CompositePhysicalPurchaseGroup,
        CompositePhysicalPurchaseGroupSource,
    )
    from app.models.product_bom import RequisitionItemBomSource
    from app.models.requisition import RequisitionItem
    from app.models.supplier_requisition_order import (
        SupplierRequisitionOrder,
        SupplierRequisitionOrderItem,
    )

    app, factory = receipt_group_app
    with TestClient(app) as client:
        _login(client)
        group_row, supplier_item_ids, supplier_order_id = (
            _prepare_supplier_converted_group(client, factory)
        )
        receipt_fact = _freeze_direct_group_receipt_fact(
            client,
            factory,
            group_row,
            idempotency_key="p1-150b-void-guard-price",
        )
        pending = client.get("/api/incoming/pending")
        refreshed_group_row = next(
            row
            for row in pending.json()["items"]
            if row["item_id"] == group_row["item_id"]
        )
        received = client.put(
            f"/api/incoming/receive/{group_row['item_id']}",
            json=_group_receive_payload(
                refreshed_group_row,
                receipt_fact,
                quantity=1,
                idempotency_key="p1-150b-void-guard-receipt",
            ),
        )
        assert received.status_code == 200, received.text

        with factory() as db:
            group = db.scalar(select(CompositePhysicalPurchaseGroup))
            header = db.get(SupplierRequisitionOrder, supplier_order_id)
            item = db.get(SupplierRequisitionOrderItem, supplier_item_ids[0])
            sources = db.scalars(select(CompositePhysicalPurchaseGroupSource)).all()
            requisition_items = db.scalars(
                select(RequisitionItem).where(
                    RequisitionItem.id.in_(
                        [int(source.requisition_item_id) for source in sources]
                    )
                )
            ).all()
            guards = db.scalars(
                select(RequisitionItemBomSource).where(
                    RequisitionItemBomSource.requisition_item_id.in_(
                        [int(source.requisition_item_id) for source in sources]
                    )
                )
            ).all()
            before = {
                "group": (group.status, group.version),
                "header": (header.status, header.voided_at),
                "item": (item.status, item.version, item.voided_at),
                "sources": [(row.id, row.status) for row in requisition_items],
                "guards": [(row.id, row.active_guard) for row in guards],
            }

        blocked_item = client.put(
            f"/api/requisition/supplier-order-items/{supplier_item_ids[0]}/void",
            json={
                "expected_version": 1,
                "idempotency_key": "p1-150b-posted-group-item-void",
                "confirmed": True,
            },
        )
        blocked_order = client.put(
            f"/api/requisition/supplier-orders/{supplier_order_id}/void"
        )
        assert blocked_item.status_code == 409, blocked_item.text
        assert blocked_order.status_code == 409, blocked_order.text
        assert "正式收料" in blocked_item.text
        assert "正式收料" in blocked_order.text

    with factory() as db:
        group = db.scalar(select(CompositePhysicalPurchaseGroup))
        header = db.get(SupplierRequisitionOrder, supplier_order_id)
        item = db.get(SupplierRequisitionOrderItem, supplier_item_ids[0])
        sources = db.scalars(select(CompositePhysicalPurchaseGroupSource)).all()
        requisition_items = db.scalars(
            select(RequisitionItem).where(
                RequisitionItem.id.in_(
                    [int(source.requisition_item_id) for source in sources]
                )
            )
        ).all()
        guards = db.scalars(
            select(RequisitionItemBomSource).where(
                RequisitionItemBomSource.requisition_item_id.in_(
                    [int(source.requisition_item_id) for source in sources]
                )
            )
        ).all()
        after = {
            "group": (group.status, group.version),
            "header": (header.status, header.voided_at),
            "item": (item.status, item.version, item.voided_at),
            "sources": [(row.id, row.status) for row in requisition_items],
            "guards": [(row.id, row.active_guard) for row in guards],
        }
        assert after == before
        assert db.scalar(
            select(CompositePhysicalGroupReceipt.id).where(
                CompositePhysicalGroupReceipt.status == "posted"
            )
        ) is not None


@pytest.mark.parametrize("inactive_contract", ["item", "header"])
def test_supplier_group_receipt_rejects_inactive_item_or_header_contract(
    receipt_group_app,
    inactive_contract: str,
) -> None:
    from app.models.composite_purchase_group import (
        CompositePhysicalGroupReceipt,
        CompositePhysicalPurchaseGroup,
    )
    from app.models.supplier_requisition_order import (
        SupplierRequisitionOrder,
        SupplierRequisitionOrderItem,
    )

    app, factory = receipt_group_app
    with TestClient(app) as client:
        _login(client)
        group_row, _supplier_item_ids, supplier_order_id = (
            _prepare_supplier_converted_group(client, factory)
        )
        receipt_fact = _freeze_direct_group_receipt_fact(
            client,
            factory,
            group_row,
            idempotency_key=f"p1-150b-inactive-{inactive_contract}-price",
        )
        pending = client.get("/api/incoming/pending")
        refreshed_group_row = next(
            row
            for row in pending.json()["items"]
            if row["item_id"] == group_row["item_id"]
        )
        with factory() as db:
            group = db.scalar(select(CompositePhysicalPurchaseGroup))
            assert group is not None
            supplier_item = db.get(
                SupplierRequisitionOrderItem,
                int(group.supplier_requisition_order_item_id),
            )
            header = db.get(SupplierRequisitionOrder, supplier_order_id)
            assert supplier_item is not None and header is not None
            if inactive_contract == "item":
                supplier_item.status = "voided"
                supplier_item.version += 1
                supplier_item.voided_at = datetime(2026, 9, 3, 9, 0, 0)
                supplier_item.voided_by = 1
                supplier_item.void_idempotency_key = "p1-150b-inactive-anchor"
                supplier_item.void_request_hash = "0" * 64
            else:
                header.status = "voided"
                header.voided_at = datetime(2026, 9, 3, 9, 0, 0)
            db.commit()

        received = client.put(
            f"/api/incoming/receive/{group_row['item_id']}",
            json=_group_receive_payload(
                refreshed_group_row,
                receipt_fact,
                quantity=1,
                idempotency_key=f"p1-150b-inactive-{inactive_contract}-receipt",
            ),
        )
        assert received.status_code == 409, received.text
        assert received.json()["detail"]["code"] == (
            "COMPOSITE_GROUP_PRICE_ANCHOR_INVALID"
        )

    with factory() as db:
        assert db.scalar(select(CompositePhysicalGroupReceipt.id).limit(1)) is None


def test_direct_group_receipt_posts_one_component_lot_and_exact_source_reservations(
    receipt_group_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.models.composite_purchase_group import (
        CompositePhysicalGroupReceipt,
        CompositePhysicalGroupReceiptSourceAllocation,
        CompositePhysicalPurchaseGroup,
        CompositePhysicalPurchaseGroupSource,
    )
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.production import ProductionCompletion, ProductionTask
    from app.models.requisition import RequisitionItem
    from app.models.warehouse_inventory import InventoryLot, InventoryReservation

    app, factory = receipt_group_app
    with TestClient(app) as client:
        _login(client)
        requisition_pending = client.get("/api/requisition/pending")
        assert requisition_pending.status_code == 200, requisition_pending.text
        planning_rows = requisition_pending.json()["items"][:2]
        created = client.post(
            "/api/requisition/batches",
            json=_group_payload_from_pending(planning_rows),
        )
        assert created.status_code == 201, created.text

        pending = client.get("/api/incoming/pending")
        assert pending.status_code == 200, pending.text
        group_rows = [
            row for row in pending.json()["items"] if str(row["item_id"]).startswith("cg")
        ]
        assert len(group_rows) == 1, pending.text
        group_row = group_rows[0]
        assert group_row["source_count"] == 2
        assert group_row["receipt_fact_ready"] is False
        source_requisition_ids = {
            int(row["requisition_item_id"]) for row in group_row["source_items"]
        }
        assert not any(
            str(row["item_id"]) in {f"r{source_id}" for source_id in source_requisition_ids}
            for row in pending.json()["items"]
        )
        with factory() as db:
            from app.services.production_workflow import (
                create_or_refresh_production_task,
            )

            for source in group_row["source_items"]:
                create_or_refresh_production_task(db, int(source["order_item_id"]))
            db.commit()

        receipt_fact = _freeze_direct_group_receipt_fact(
            client,
            factory,
            group_row,
            idempotency_key="p1-150b-direct-price",
        )
        refreshed = client.get("/api/incoming/pending")
        assert refreshed.status_code == 200, refreshed.text
        group_row = next(
            row
            for row in refreshed.json()["items"]
            if row["item_id"] == group_row["item_id"]
        )
        assert group_row["receipt_fact_ready"] is True
        payload = _group_receive_payload(
            group_row,
            receipt_fact,
            quantity=1,
            idempotency_key="p1-150b-direct-receipt",
        )
        received = client.put(
            f"/api/incoming/receive/{group_row['item_id']}",
            json=payload,
        )
        assert received.status_code == 200, received.text
        assert received.json()["material_status"] == "received"
        assert received.json()["requisition_status"] == "已入库"
        import app.services.composite_physical_group_receipts as group_service

        original_replay = group_service._idempotent_group_receipt
        replay_checks = 0

        def hide_committed_receipt_until_after_group_lock(*args, **kwargs):
            nonlocal replay_checks
            replay_checks += 1
            if replay_checks == 1:
                return None
            return original_replay(*args, **kwargs)

        monkeypatch.setattr(
            group_service,
            "_idempotent_group_receipt",
            hide_committed_receipt_until_after_group_lock,
        )
        replay = client.put(
            f"/api/incoming/receive/{group_row['item_id']}",
            json=payload,
        )
        monkeypatch.setattr(
            group_service,
            "_idempotent_group_receipt",
            original_replay,
        )
        assert replay.status_code == 200, replay.text
        assert replay_checks == 2
        assert replay.json()["receipt_item_id"] == received.json()["receipt_item_id"]
        changed = client.put(
            f"/api/incoming/receive/{group_row['item_id']}",
            json={**payload, "received_quantity": 2},
        )
        assert changed.status_code == 409, changed.text

        first_legacy = min(source_requisition_ids)
        legacy = client.put(
            f"/api/incoming/receive/r{first_legacy}",
            json={"received_quantity": 1, "idempotency_key": "must-not-bypass"},
        )
        assert legacy.status_code == 409, legacy.text
        assert group_row["item_id"] in legacy.text

    with factory() as db:
        group = db.scalar(select(CompositePhysicalPurchaseGroup))
        receipt = db.scalar(select(CompositePhysicalGroupReceipt))
        receipt_item = db.scalar(
            select(IncomingReceiptItem).where(
                IncomingReceiptItem.composite_physical_purchase_group_id.is_not(None)
            )
        )
        assert group is not None and receipt is not None and receipt_item is not None
        assert receipt.received_sheet_quantity == 1
        assert receipt.order_purpose_received_sheet_quantity == 1
        assert receipt.reserve_received_sheet_quantity == 0
        assert receipt.component_output_piece_quantity == 4
        assert receipt.total_material_cost == Decimal("2.500000")
        assert receipt.component_unit_material_cost == Decimal("0.625000")
        lot = db.get(InventoryLot, receipt.component_inventory_lot_id)
        assert lot is not None
        assert lot.source_ref_type == "composite_physical_group_receipt"
        assert lot.source_ref_id == receipt.id
        assert lot.quantity_available == 2
        assert lot.quantity_reserved == 2
        assert lot.estimated_unit_cost_snapshot == Decimal("0.6250")
        assert json.loads(lot.cost_snapshot_detail_json) == {
            "purchase_receipt_fact_id": receipt.purchase_receipt_fact_id,
            "composite_physical_group_receipt_id": receipt.id,
            "composite_inventory_kind": "component_piece",
        }
        allocations = db.scalars(
            select(CompositePhysicalGroupReceiptSourceAllocation).order_by(
                CompositePhysicalGroupReceiptSourceAllocation.allocation_sequence
            )
        ).all()
        assert [row.allocated_reserved_component_piece_quantity for row in allocations] == [1, 1]
        sources = db.scalars(
            select(CompositePhysicalPurchaseGroupSource).order_by(
                CompositePhysicalPurchaseGroupSource.source_sequence
            )
        ).all()
        assert [row.composite_physical_purchase_group_source_id for row in allocations] == [
            row.id for row in sources
        ]
        reservations = db.scalars(
            select(InventoryReservation).where(
                InventoryReservation.id.in_(
                    [row.inventory_reservation_id for row in allocations]
                )
            )
        ).all()
        assert sorted(row.reserved_stock_quantity for row in reservations) == [1, 1]
        requisition_items = db.scalars(
            select(RequisitionItem).where(RequisitionItem.id.in_(source_requisition_ids))
        ).all()
        assert {row.status for row in requisition_items} == {"已入库"}
        tasks = db.scalars(select(ProductionTask).order_by(ProductionTask.id)).all()
        assert len(tasks) == 2
        assert {row.task_role for row in tasks} == {"component_internal"}
        assert {row.status for row in tasks} == {"not_required"}
        assert {row.planned_quantity for row in tasks} == {0}
        assert {row.readiness_basis for row in tasks} == {
            "component_finished_inventory"
        }
        assert all(row.sales_order_item_bom_component_id is not None for row in tasks)
        assert db.scalar(select(ProductionCompletion.id).limit(1)) is None
        assert db.scalar(select(InventoryLot.id).where(InventoryLot.id != lot.id).limit(1)) is None


def test_supplier_converted_group_uses_supplier_price_anchor_and_blocks_every_so_route(
    receipt_group_app,
) -> None:
    from app.models.composite_purchase_group import (
        CompositePhysicalGroupReceipt,
        CompositePhysicalPurchaseGroup,
        CompositePhysicalPurchaseGroupSource,
    )
    from app.models.purchase_receipt import PurchaseReceiptFact
    from app.models.product_bom import SalesOrderItemBomComponent
    from app.models.supplier_requisition_order import PurchasePurposeSourceSnapshot

    app, factory = receipt_group_app
    with TestClient(app) as client:
        _login(client)
        requisition_pending = client.get("/api/requisition/pending")
        assert requisition_pending.status_code == 200, requisition_pending.text
        selected_pending = requisition_pending.json()["items"][:2]
        selected_snapshot_ids = [
            int(row["component_requirements"][0]["snapshot_id"])
            for row in selected_pending
        ]
        with factory() as db:
            components = db.scalars(
                select(SalesOrderItemBomComponent).where(
                    SalesOrderItemBomComponent.id.in_(selected_snapshot_ids)
                )
            ).all()
            assert len(components) == 2
            for component in components:
                component.spare_sheet_quantity = 1
            db.commit()
        requisition_pending = client.get("/api/requisition/pending")
        assert requisition_pending.status_code == 200, requisition_pending.text
        group_payload = _group_payload_from_pending(
            requisition_pending.json()["items"][:2]
        )
        for line in group_payload["items"]:
            line["group_purchase_sheet_qty"] = 3
            line["group_order_purpose_sheet_qty"] = 3
        first_line = group_payload["items"][0]
        first_line["requisition_qty"] = 1
        first_line["purchase_total_sheet_qty"] = 1
        first_line["order_purpose_sheet_qty"] = 1
        last_line = group_payload["items"][-1]
        last_line["requisition_qty"] = 2
        last_line["purchase_total_sheet_qty"] = 2
        last_line["order_purpose_sheet_qty"] = 2
        created = client.post(
            "/api/requisition/batches",
            json=group_payload,
        )
        assert created.status_code == 201, created.text
        supplier_item_ids = _convert_group_sources_to_supplier_items(factory)
        with factory() as db:
            group = db.scalar(select(CompositePhysicalPurchaseGroup))
            assert group is not None
            sources = db.scalars(
                select(CompositePhysicalPurchaseGroupSource).order_by(
                    CompositePhysicalPurchaseGroupSource.source_sequence
                )
            ).all()
            source_snapshots = db.scalars(
                select(PurchasePurposeSourceSnapshot)
                .where(
                    PurchasePurposeSourceSnapshot.id.in_(
                        [
                            row.purchase_purpose_source_snapshot_id
                            for row in sources
                        ]
                    )
                )
                .order_by(PurchasePurposeSourceSnapshot.id)
            ).all()
            assert len(source_snapshots) == 2
            assert group.supplier_requisition_order_item_id == supplier_item_ids[-1]
            assert group.supplier_requisition_order_item_id != (
                source_snapshots[0].supplier_requisition_order_item_id
            )

        pending = client.get("/api/incoming/pending")
        assert pending.status_code == 200, pending.text
        assert len(pending.json()["items"]) == 1
        assert not any(
            str(row["item_id"]).startswith(("r", "so"))
            for row in pending.json()["items"]
        )
        group_row = next(
            row
            for row in pending.json()["items"]
            if str(row["item_id"]).startswith("cg")
        )
        assert group_row["receipt_fact_ready"] is False
        for supplier_item_id in supplier_item_ids:
            blocked = client.put(
                f"/api/incoming/receive/so{supplier_item_id}",
                json={
                    "received_quantity": 1,
                    "idempotency_key": f"blocked-so-{supplier_item_id}",
                },
            )
            assert blocked.status_code == 409, blocked.text
            assert group_row["item_id"] in blocked.text

        receipt_fact = _freeze_direct_group_receipt_fact(
            client,
            factory,
            group_row,
            idempotency_key="p1-150b-supplier-price",
        )
        pending = client.get("/api/incoming/pending")
        group_row = next(
            row
            for row in pending.json()["items"]
            if row["item_id"] == group_row["item_id"]
        )
        received = client.put(
            f"/api/incoming/receive/{group_row['item_id']}",
            json=_group_receive_payload(
                group_row,
                receipt_fact,
                quantity=1,
                idempotency_key="p1-150b-supplier-receipt",
            ),
        )
        assert received.status_code == 200, received.text

    with factory() as db:
        group = db.scalar(select(CompositePhysicalPurchaseGroup))
        fact = db.scalar(select(PurchaseReceiptFact))
        group_receipt = db.scalar(select(CompositePhysicalGroupReceipt))
        assert group is not None and fact is not None and group_receipt is not None
        assert fact.supplier_requisition_order_item_id == (
            group.supplier_requisition_order_item_id
        )
        assert fact.material_requisition_item_id is None
        assert group_receipt.purchase_receipt_fact_id == fact.id


def test_group_without_reserve_overreceipt_all_becomes_component_inventory(
    receipt_group_app,
) -> None:
    from app.models.composite_purchase_group import CompositePhysicalGroupReceipt
    from app.models.warehouse_inventory import InventoryLot

    app, factory = receipt_group_app
    with TestClient(app) as client:
        _login(client)
        requisition_pending = client.get("/api/requisition/pending")
        created = client.post(
            "/api/requisition/batches",
            json=_group_payload_from_pending(
                requisition_pending.json()["items"][:2]
            ),
        )
        assert created.status_code == 201, created.text
        pending = client.get("/api/incoming/pending")
        group_row = next(
            row
            for row in pending.json()["items"]
            if str(row["item_id"]).startswith("cg")
        )
        receipt_fact = _freeze_direct_group_receipt_fact(
            client,
            factory,
            group_row,
            idempotency_key="p1-150b-over-price",
        )
        pending = client.get("/api/incoming/pending")
        group_row = next(
            row
            for row in pending.json()["items"]
            if row["item_id"] == group_row["item_id"]
        )
        invalid_payload = _group_receive_payload(
            group_row,
            receipt_fact,
            quantity=2,
            idempotency_key="p1-150b-over-invalid-reserve",
        )
        invalid_payload["surplus_disposition"] = "semi_finished_reserve"
        invalid = client.put(
            f"/api/incoming/receive/{group_row['item_id']}",
            json=invalid_payload,
        )
        assert invalid.status_code == 409, invalid.text

        payload = _group_receive_payload(
            group_row,
            receipt_fact,
            quantity=2,
            idempotency_key="p1-150b-over-component",
        )
        payload["surplus_disposition"] = "finished"
        received = client.put(
            f"/api/incoming/receive/{group_row['item_id']}",
            json=payload,
        )
        assert received.status_code == 200, received.text
        assert received.json()["variance_type"] == "over"
        allocation = received.json()["purpose_allocation"]
        assert allocation["order_purpose_sheet_qty"] == 2
        assert allocation["reserve_purpose_sheet_qty"] == 0
        assert allocation["component_output_piece_quantity"] == 8

    with factory() as db:
        receipt = db.scalar(select(CompositePhysicalGroupReceipt))
        assert receipt is not None
        assert receipt.received_sheet_quantity == 2
        assert receipt.order_purpose_received_sheet_quantity == 2
        assert receipt.reserve_received_sheet_quantity == 0
        assert receipt.component_output_piece_quantity == 8
        assert receipt.total_material_cost == Decimal("5.000000")
        lot = db.get(InventoryLot, receipt.component_inventory_lot_id)
        assert lot is not None
        assert lot.quantity_reserved == 2
        assert lot.quantity_available == 6
        assert receipt.reserve_inventory_lot_id is None


def test_group_receipt_reversal_releases_every_source_and_closes_only_its_lot(
    receipt_group_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.models.composite_purchase_group import (
        CompositePhysicalGroupReceipt,
        CompositePhysicalGroupReceiptReversal,
        CompositePhysicalGroupReceiptSourceAllocation,
        CompositePhysicalPurchaseGroup,
    )
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.order import Order, OrderItem
    from app.models.requisition import RequisitionItem
    from app.models.user import User
    from app.models.warehouse_inventory import (
        InventoryLot,
        InventoryMovement,
        InventoryReservation,
    )

    app, factory = receipt_group_app
    with TestClient(app) as client:
        _login(client)
        group_row, _payload, received = _post_two_source_group_receipt(
            client,
            factory,
            key_prefix="p1-150b-reverse",
        )
        receipt_item_id = int(received["receipt_item_id"])
        reversed_response = client.put(
            f"/api/incoming/receipt-items/{receipt_item_id}/revert",
            json={"idempotency_key": "p1-150b-reversal"},
        )
        assert reversed_response.status_code == 200, reversed_response.text
        assert reversed_response.json()["receipt_status"] == "reversed"
        import app.services.composite_physical_group_receipts as group_service

        original_replay = group_service._idempotent_group_reversal
        replay_checks = 0

        def hide_committed_reversal_until_after_group_lock(*args, **kwargs):
            nonlocal replay_checks
            replay_checks += 1
            if replay_checks == 1:
                return False
            return original_replay(*args, **kwargs)

        monkeypatch.setattr(
            group_service,
            "_idempotent_group_reversal",
            hide_committed_reversal_until_after_group_lock,
        )
        request_hash = group_service.canonical_purchase_receipt_hash(
            {
                "target_kind": "receipt_item",
                "target_id": receipt_item_id,
                "reason": "撤回来料实收（系统记录）",
            }
        )
        with factory() as db:
            user = db.get(User, 1)
            assert user is not None
            replayed_item = group_service.revert_composite_physical_group_receipt(
                db,
                user=user,
                receipt_item_id=receipt_item_id,
                reason="撤回来料实收（系统记录）",
                idempotency_key="p1-150b-reversal",
                request_hash=request_hash,
            )
            assert replayed_item.id == receipt_item_id
        monkeypatch.setattr(
            group_service,
            "_idempotent_group_reversal",
            original_replay,
        )
        assert replay_checks == 2
        replay = client.put(
            f"/api/incoming/receipt-items/{receipt_item_id}/revert",
            json={"idempotency_key": "p1-150b-reversal"},
        )
        assert replay.status_code == 200, replay.text
        changed = client.put(
            f"/api/incoming/receipt-items/{receipt_item_id}/revert",
            json={
                "idempotency_key": "p1-150b-reversal",
                "reason": "changed payload",
            },
        )
        assert changed.status_code == 409, changed.text
        pending = client.get("/api/incoming/pending")
        assert pending.status_code == 200, pending.text
        restored = [
            row for row in pending.json()["items"] if row["item_id"] == group_row["item_id"]
        ]
        assert len(restored) == 1
        assert restored[0]["cumulative_received_quantity"] == 0

    with factory() as db:
        group = db.scalar(select(CompositePhysicalPurchaseGroup))
        receipt = db.scalar(select(CompositePhysicalGroupReceipt))
        receipt_item = db.get(IncomingReceiptItem, receipt_item_id)
        reversal = db.scalar(select(CompositePhysicalGroupReceiptReversal))
        assert group is not None and receipt is not None
        assert receipt_item is not None and reversal is not None
        assert group.status == "reversed"
        assert receipt.status == "reversed"
        assert receipt_item.status == "reversed"
        assert reversal.reversed_sheet_quantity == 1
        assert reversal.reversed_component_output_piece_quantity == 4
        assert reversal.reversed_reserved_component_piece_quantity == 2
        allocations = db.scalars(
            select(CompositePhysicalGroupReceiptSourceAllocation)
        ).all()
        assert {row.status for row in allocations} == {"reversed"}
        reservations = db.scalars(
            select(InventoryReservation).where(
                InventoryReservation.id.in_(
                    [row.inventory_reservation_id for row in allocations]
                )
            )
        ).all()
        assert {row.status for row in reservations} == {"released"}
        lot = db.get(InventoryLot, receipt.component_inventory_lot_id)
        assert lot is not None
        assert lot.status == "closed"
        assert lot.quantity_available == 0
        assert lot.quantity_reserved == 0
        movement_types = db.scalars(
            select(InventoryMovement.movement_type)
            .where(InventoryMovement.inventory_lot_id == lot.id)
            .order_by(InventoryMovement.id)
        ).all()
        assert movement_types == [
            "manual_in",
            "reserve",
            "reserve",
            "release_reserve",
            "release_reserve",
            "adjust",
        ]
        requisition_items = db.scalars(select(RequisitionItem)).all()
        group_requisition_ids = {
            int(row["requisition_item_id"])
            for row in restored[0]["source_items"]
        }
        assert {
            row.status for row in requisition_items if row.id in group_requisition_ids
        } == {"有效"}
        order_items = db.scalars(
            select(OrderItem).where(
                OrderItem.id.in_(
                    [int(row["order_item_id"]) for row in restored[0]["source_items"]]
                )
            )
        ).all()
        assert {row.material_status for row in order_items} == {"pending"}


@pytest.mark.parametrize(
    "downstream_change",
    ["source_consumed", "surplus_reserved", "lot_adjusted", "order_delivered"],
)
def test_group_receipt_reversal_blocks_every_material_downstream_change(
    receipt_group_app,
    downstream_change: str,
) -> None:
    from app.models.composite_purchase_group import (
        CompositePhysicalGroupReceipt,
        CompositePhysicalGroupReceiptReversal,
        CompositePhysicalGroupReceiptSourceAllocation,
        CompositePhysicalPurchaseGroupSource,
    )
    from app.models.order import Order, OrderItem
    from app.models.warehouse_inventory import InventoryLot, InventoryReservation
    from app.services.composite_physical_group_receipts import (
        _reserve_component_piece_inventory,
    )
    from app.services.warehouse_inventory import mutate_lot

    app, factory = receipt_group_app
    with TestClient(app) as client:
        _login(client)
        _group_row, _payload, received = _post_two_source_group_receipt(
            client,
            factory,
            key_prefix=f"p1-150b-block-{downstream_change}",
        )
        receipt_item_id = int(received["receipt_item_id"])
        with factory() as db:
            receipt = db.scalar(select(CompositePhysicalGroupReceipt))
            assert receipt is not None
            lot = db.get(InventoryLot, receipt.component_inventory_lot_id)
            assert lot is not None
            allocations = db.scalars(
                select(CompositePhysicalGroupReceiptSourceAllocation).order_by(
                    CompositePhysicalGroupReceiptSourceAllocation.allocation_sequence
                )
            ).all()
            assert allocations
            if downstream_change == "source_consumed":
                reservation = db.get(
                    InventoryReservation,
                    allocations[0].inventory_reservation_id,
                )
                assert reservation is not None
                reservation.consumed_stock_quantity = 1
                reservation.consumed_requirement_quantity = 1
                reservation.status = "consumed"
                lot.quantity_reserved -= 1
                lot.quantity_consumed += 1
                lot.version += 1
            elif downstream_change == "surplus_reserved":
                source = db.get(
                    CompositePhysicalPurchaseGroupSource,
                    allocations[0].composite_physical_purchase_group_source_id,
                )
                assert source is not None
                order_item = db.get(OrderItem, source.order_item_id)
                assert order_item is not None
                order = db.get(Order, order_item.order_id)
                assert order is not None
                _reserve_component_piece_inventory(
                    db,
                    lot=lot,
                    source=source,
                    order=order,
                    quantity=1,
                    operator_id=1,
                    idempotency_key=f"p1-150b-extra-{receipt.id}",
                )
            elif downstream_change == "lot_adjusted":
                mutate_lot(
                    db,
                    lot_id=lot.id,
                    operation="adjust",
                    expected_version=lot.version,
                    operator_id=1,
                    quantity=1,
                    reason="P1-150B downstream adjustment",
                    idempotency_key=f"p1-150b-adjust-{receipt.id}",
                )
            else:
                source = db.get(
                    CompositePhysicalPurchaseGroupSource,
                    allocations[0].composite_physical_purchase_group_source_id,
                )
                assert source is not None
                order_item = db.get(OrderItem, source.order_item_id)
                assert order_item is not None
                order_item.delivered_quantity = 1
                order = db.get(Order, order_item.order_id)
                assert order is not None
                order.status = "partially_delivered"
            db.commit()

        rejected = client.put(
            f"/api/incoming/receipt-items/{receipt_item_id}/revert",
            json={"idempotency_key": f"p1-150b-reject-{downstream_change}"},
        )
        assert rejected.status_code == 409, rejected.text

    with factory() as db:
        receipt = db.scalar(select(CompositePhysicalGroupReceipt))
        lot = db.get(InventoryLot, receipt.component_inventory_lot_id)
        assert receipt is not None and lot is not None
        assert receipt.status == "posted"
        assert lot.status == "active"
        assert db.scalar(select(CompositePhysicalGroupReceiptReversal.id)) is None


def test_order_sources_finish_before_explicit_reserve_and_each_partial_receipt_is_independent(
    receipt_group_app,
) -> None:
    from app.models.composite_purchase_group import (
        CompositePhysicalGroupReceipt,
        CompositePhysicalGroupReceiptSourceAllocation,
        CompositePhysicalPurchaseGroup,
    )
    from app.models.order import Order, OrderItem
    from app.models.production import ProductionTask
    from app.models.purchase_receipt import PurchaseReceiptFact
    from app.models.requisition import RequisitionItem
    from app.models.warehouse_inventory import InventoryLot
    from app.services.production_workflow import create_or_refresh_production_task

    app, factory = receipt_group_app
    with TestClient(app) as client:
        _login(client)
        pending = client.get("/api/requisition/pending")
        assert pending.status_code == 200, pending.text
        payload = _group_payload_from_pending(pending.json()["items"][:2])
        for line in payload["items"]:
            line["group_purchase_sheet_qty"] = 2
            line["group_stock_purpose_sheet_qty"] = 1
        last_line = payload["items"][-1]
        last_line["requisition_qty"] = 2
        last_line["purchase_total_sheet_qty"] = 2
        last_line["stock_purpose_sheet_qty"] = 1
        created = client.post("/api/requisition/batches", json=payload)
        assert created.status_code == 201, created.text

        incoming = client.get("/api/incoming/pending")
        assert incoming.status_code == 200, incoming.text
        group_row = next(
            row
            for row in incoming.json()["items"]
            if str(row["item_id"]).startswith("cg")
        )
        assert group_row["planned_quantity"] == 2
        assert group_row["order_purpose_sheet_quantity"] == 1
        assert group_row["reserve_sheet_quantity"] == 1
        order_item_ids = {
            int(row["order_item_id"]) for row in group_row["source_items"]
        }
        requisition_item_ids = {
            int(row["requisition_item_id"]) for row in group_row["source_items"]
        }
        with factory() as db:
            for item_id in order_item_ids:
                create_or_refresh_production_task(db, item_id)
            db.commit()
        receipt_fact = _freeze_direct_group_receipt_fact(
            client,
            factory,
            group_row,
            idempotency_key="p1-150b-partial-price",
        )
        incoming = client.get("/api/incoming/pending")
        group_row = next(
            row
            for row in incoming.json()["items"]
            if row["item_id"] == group_row["item_id"]
        )
        first = client.put(
            f"/api/incoming/receive/{group_row['item_id']}",
            json=_group_receive_payload(
                group_row,
                receipt_fact,
                quantity=1,
                idempotency_key="p1-150b-partial-order",
            ),
        )
        assert first.status_code == 200, first.text
        assert first.json()["material_status"] == "pending"
        assert first.json()["requisition_status"] == "已报料"

        # The one order-purpose sheet outputs four pieces and covers both
        # sources. Missing reserve stock must not keep either order/task open.
        with factory() as db:
            group = db.scalar(select(CompositePhysicalPurchaseGroup))
            assert group is not None and group.status == "partially_received"
            assert {
                row.status
                for row in db.scalars(
                    select(RequisitionItem).where(
                        RequisitionItem.id.in_(requisition_item_ids)
                    )
                ).all()
            } == {"已入库"}
            assert {
                row.material_status
                for row in db.scalars(
                    select(OrderItem).where(OrderItem.id.in_(order_item_ids))
                ).all()
            } == {"received"}
            tasks = db.scalars(
                select(ProductionTask).where(
                    ProductionTask.order_item_id.in_(order_item_ids)
                )
            ).all()
            assert len(tasks) == 2
            assert {row.status for row in tasks} == {"not_required"}
            source_progress_before_reserve = {
                int(row.id): (
                    row.material_status,
                    row.requisition_status,
                    row.material_received_at,
                    row.material_received_by,
                )
                for row in db.scalars(
                    select(OrderItem).where(OrderItem.id.in_(order_item_ids))
                ).all()
            }
            task_progress_before_reserve = {
                int(row.id): (
                    row.status,
                    int(row.planned_quantity),
                    int(row.version),
                    row.updated_at,
                )
                for row in tasks
            }
            for item in db.scalars(
                select(OrderItem).where(OrderItem.id.in_(order_item_ids))
            ).all():
                item.delivered_quantity = item.quantity
                order = db.get(Order, item.order_id)
                assert order is not None
                order.status = "partially_delivered"
            db.commit()

        still_pending = client.get("/api/incoming/pending")
        assert still_pending.status_code == 200, still_pending.text
        reserve_row = next(
            row
            for row in still_pending.json()["items"]
            if row["item_id"] == group_row["item_id"]
        )
        assert reserve_row["remaining_quantity"] == 1
        assert reserve_row["remaining_order_purpose_sheet_qty"] == 0
        assert reserve_row["remaining_reserve_purpose_sheet_qty"] == 1
        assert reserve_row["expected_finished_output_qty"] == 0
        assert reserve_row["source_count"] == 2
        assert reserve_row["material_status"] == "pending"
        assert reserve_row["requisition_status"] == "已报料"
        second_receipt_fact = _freeze_direct_group_receipt_fact(
            client,
            factory,
            reserve_row,
            idempotency_key="p1-150b-partial-price-v2",
            unit_price="3.500000",
            expected_latest_receipt_fact_version=receipt_fact[
                "receipt_fact_version"
            ],
        )
        refreshed_pending = client.get("/api/incoming/pending")
        assert refreshed_pending.status_code == 200, refreshed_pending.text
        reserve_row = next(
            row
            for row in refreshed_pending.json()["items"]
            if row["item_id"] == group_row["item_id"]
        )
        assert reserve_row["expected_receipt_fact_version"] == 2
        second = client.put(
            f"/api/incoming/receive/{reserve_row['item_id']}",
            json=_group_receive_payload(
                reserve_row,
                second_receipt_fact,
                quantity=1,
                idempotency_key="p1-150b-partial-reserve",
            ),
        )
        assert second.status_code == 200, second.text
        assert second.json()["purpose_allocation"]["order_purpose_sheet_qty"] == 0
        assert second.json()["purpose_allocation"]["reserve_purpose_sheet_qty"] == 1

        reversed_second = client.put(
            f"/api/incoming/receipt-items/{second.json()['receipt_item_id']}/revert",
            json={"idempotency_key": "p1-150b-reverse-second-only"},
        )
        assert reversed_second.status_code == 200, reversed_second.text

    with factory() as db:
        receipts = db.scalars(
            select(CompositePhysicalGroupReceipt).order_by(
                CompositePhysicalGroupReceipt.receipt_sequence
            )
        ).all()
        assert len(receipts) == 2
        assert [row.status for row in receipts] == ["posted", "reversed"]
        assert [row.component_output_piece_quantity for row in receipts] == [4, 0]
        assert [row.reserve_received_sheet_quantity for row in receipts] == [0, 1]
        assert [row.actual_unit_price_per_sheet for row in receipts] == [
            Decimal("2.500000"),
            Decimal("3.500000"),
        ]
        assert [row.total_material_cost for row in receipts] == [
            Decimal("2.500000"),
            Decimal("3.500000"),
        ]
        facts = db.scalars(
            select(PurchaseReceiptFact).order_by(
                PurchaseReceiptFact.receipt_fact_version
            )
        ).all()
        assert len(facts) == 2
        assert [row.purchase_receipt_fact_id for row in receipts] == [
            facts[0].id,
            facts[1].id,
        ]
        assert len(
            db.scalars(select(CompositePhysicalGroupReceiptSourceAllocation)).all()
        ) == 2
        first_lot = db.get(InventoryLot, receipts[0].component_inventory_lot_id)
        second_lot = db.get(InventoryLot, receipts[1].reserve_inventory_lot_id)
        assert first_lot is not None and first_lot.status == "active"
        assert first_lot.quantity_available == 2
        assert first_lot.quantity_reserved == 2
        assert first_lot.estimated_unit_cost_snapshot == Decimal("0.6250")
        assert json.loads(first_lot.cost_snapshot_detail_json or "{}") == {
            "composite_inventory_kind": "component_piece",
            "composite_physical_group_receipt_id": receipts[0].id,
            "purchase_receipt_fact_id": facts[0].id,
        }
        assert second_lot is not None and second_lot.status == "closed"
        assert second_lot.quantity_available == 0
        assert second_lot.estimated_unit_cost_snapshot == Decimal("3.5000")
        assert json.loads(second_lot.cost_snapshot_detail_json or "{}") == {
            "composite_inventory_kind": "reserve_sheet",
            "composite_physical_group_receipt_id": receipts[1].id,
            "purchase_receipt_fact_id": facts[1].id,
        }
        group = db.scalar(select(CompositePhysicalPurchaseGroup))
        assert group is not None and group.status == "partially_received"
        final_order_items = db.scalars(
            select(OrderItem).where(OrderItem.id.in_(order_item_ids))
        ).all()
        assert {
            int(row.id): (
                row.material_status,
                row.requisition_status,
                row.material_received_at,
                row.material_received_by,
            )
            for row in final_order_items
        } == source_progress_before_reserve
        assert {int(row.delivered_quantity) for row in final_order_items} == {1}
        assert {
            db.get(Order, row.order_id).status for row in final_order_items
        } == {"partially_delivered"}
        final_tasks = db.scalars(
            select(ProductionTask).where(
                ProductionTask.order_item_id.in_(order_item_ids)
            )
        ).all()
        assert {
            int(row.id): (
                row.status,
                int(row.planned_quantity),
                int(row.version),
                row.updated_at,
            )
            for row in final_tasks
        } == task_progress_before_reserve


def test_covered_delivered_source_does_not_block_later_source_or_unrelated_reversal(
    receipt_group_app,
) -> None:
    from app.models.composite_purchase_group import (
        CompositePhysicalGroupReceipt,
        CompositePhysicalGroupReceiptSourceAllocation,
        CompositePhysicalPurchaseGroupSource,
    )
    from app.models.order import Order, OrderItem
    from app.models.product_bom import SalesOrderItemBomComponent

    app, factory = receipt_group_app
    with factory() as db:
        snapshots = db.scalars(
            select(SalesOrderItemBomComponent)
            .where(SalesOrderItemBomComponent.sales_order_item_id.in_([1, 2]))
            .order_by(SalesOrderItemBomComponent.sales_order_item_id)
        ).all()
        assert len(snapshots) == 2
        for snapshot in snapshots:
            snapshot.quantity_per_set = Decimal("4")
            snapshot.required_piece_quantity = Decimal("4")
        db.commit()

    with TestClient(app) as client:
        _login(client)
        pending = client.get("/api/requisition/pending")
        assert pending.status_code == 200, pending.text
        planning_rows = pending.json()["items"][:2]
        components = [row["component_requirements"][0] for row in planning_rows]
        physical_group_key = components[0]["physical_group_key"]
        assert {row["physical_group_key"] for row in components} == {
            physical_group_key
        }
        group_fingerprint = _composite_physical_group_fingerprint(
            physical_group_key,
            [row["physical_source_fingerprint"] for row in components],
        )
        payload_items = []
        ordered_sources = sorted(
            zip(planning_rows, components, strict=True),
            key=lambda pair: int(pair[0]["item_id"]),
        )
        for index, (planning_row, component) in enumerate(ordered_sources):
            allocated_sheet_quantity = 2 if index == len(ordered_sources) - 1 else 0
            payload_items.append(
                {
                    "order_item_id": planning_row["item_id"],
                    "bom_snapshot_id": component["snapshot_id"],
                    "component_type": component["component_type"],
                    "physical_group_key": physical_group_key,
                    "group_fingerprint": group_fingerprint,
                    "source_fingerprint": component[
                        "physical_source_fingerprint"
                    ],
                    "group_purchase_sheet_qty": 2,
                    "group_order_purpose_sheet_qty": 2,
                    "group_stock_purpose_sheet_qty": 0,
                    "requisition_qty": allocated_sheet_quantity,
                    "purchase_total_sheet_qty": allocated_sheet_quantity,
                    "order_purpose_sheet_qty": allocated_sheet_quantity,
                    "stock_purpose_sheet_qty": 0,
                    "purpose_plan_version": 1,
                    "purpose_plan_fingerprint": group_fingerprint,
                    "cardboard_len": component["report_length_mm"],
                    "cardboard_width": component["report_width_mm"],
                    "special_process": component["cutting_mode"],
                }
            )
        created = client.post(
            "/api/requisition/batches",
            json={
                "request_key": "p1-150b-staggered-source-receipt",
                "supplier_name": "P1-150B 供应商",
                "items": payload_items,
            },
        )
        assert created.status_code == 201, created.text

        pending_incoming = client.get("/api/incoming/pending")
        assert pending_incoming.status_code == 200, pending_incoming.text
        group_row = next(
            row
            for row in pending_incoming.json()["items"]
            if str(row["item_id"]).startswith("cg")
        )
        assert group_row["planned_quantity"] == 2
        first_fact = _freeze_direct_group_receipt_fact(
            client,
            factory,
            group_row,
            idempotency_key="p1-150b-staggered-price-1",
        )
        group_row = next(
            row
            for row in client.get("/api/incoming/pending").json()["items"]
            if row["item_id"] == group_row["item_id"]
        )
        first = client.put(
            f"/api/incoming/receive/{group_row['item_id']}",
            json=_group_receive_payload(
                group_row,
                first_fact,
                quantity=1,
                idempotency_key="p1-150b-staggered-receipt-1",
            ),
        )
        assert first.status_code == 200, first.text

        with factory() as db:
            first_receipt = db.scalar(
                select(CompositePhysicalGroupReceipt).where(
                    CompositePhysicalGroupReceipt.receipt_sequence == 1
                )
            )
            assert first_receipt is not None
            first_allocation = db.scalar(
                select(CompositePhysicalGroupReceiptSourceAllocation).where(
                    CompositePhysicalGroupReceiptSourceAllocation.composite_physical_group_receipt_id
                    == first_receipt.id
                )
            )
            assert first_allocation is not None
            first_source = db.get(
                CompositePhysicalPurchaseGroupSource,
                first_allocation.composite_physical_purchase_group_source_id,
            )
            assert first_source is not None
            first_order_item = db.get(OrderItem, first_source.order_item_id)
            assert first_order_item is not None
            first_order = db.get(Order, first_order_item.order_id)
            assert first_order is not None
            assert first_allocation.allocated_reserved_component_piece_quantity == 4
            first_order_item.delivered_quantity = first_order_item.quantity
            first_order.status = "partially_delivered"
            db.commit()
            first_receipt_item_id = int(first_receipt.incoming_receipt_item_id)

        remaining = client.get("/api/incoming/pending")
        assert remaining.status_code == 200, remaining.text
        group_row = next(
            row
            for row in remaining.json()["items"]
            if row["item_id"] == group_row["item_id"]
        )
        assert group_row["remaining_order_purpose_sheet_qty"] == 1
        second_fact = _freeze_direct_group_receipt_fact(
            client,
            factory,
            group_row,
            idempotency_key="p1-150b-staggered-price-2",
            expected_latest_receipt_fact_version=first_fact["receipt_fact_version"],
        )
        group_row = next(
            row
            for row in client.get("/api/incoming/pending").json()["items"]
            if row["item_id"] == group_row["item_id"]
        )
        second = client.put(
            f"/api/incoming/receive/{group_row['item_id']}",
            json=_group_receive_payload(
                group_row,
                second_fact,
                quantity=1,
                idempotency_key="p1-150b-staggered-receipt-2",
            ),
        )
        assert second.status_code == 200, second.text

        reversed_second = client.put(
            f"/api/incoming/receipt-items/{second.json()['receipt_item_id']}/revert",
            json={"idempotency_key": "p1-150b-staggered-reverse-2"},
        )
        assert reversed_second.status_code == 200, reversed_second.text
        blocked_first = client.put(
            f"/api/incoming/receipt-items/{first_receipt_item_id}/revert",
            json={"idempotency_key": "p1-150b-staggered-reverse-1"},
        )
        assert blocked_first.status_code == 409, blocked_first.text
        assert "该收料批次分配的来源订单已有发货事实" in blocked_first.text

    with factory() as db:
        receipts = db.scalars(
            select(CompositePhysicalGroupReceipt).order_by(
                CompositePhysicalGroupReceipt.receipt_sequence
            )
        ).all()
        assert [row.status for row in receipts] == ["posted", "reversed"]
