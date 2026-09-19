from __future__ import annotations

from decimal import Decimal, ROUND_DOWN
from typing import Any, Sequence

from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session, aliased

from app.models.order import Order, OrderItem
from app.models.purchase_receipt import (
    IncomingReceiptPurposeAllocation,
    IncomingReceiptPurposeReversal,
)
from app.models.product_bom import (
    RequisitionItemBomSource,
    SalesOrderItemBomComponent,
)
from app.models.production import ProductionCompletion
from app.models.requisition import RequisitionItem
from app.models.supplier_requisition_order import (
    PurchasePurposeSourceSnapshot,
    SupplierRequisitionOrder,
    SupplierRequisitionOrderItem,
)
from app.models.warehouse_inventory import InventoryReservation, OrderItemSemiRequirement


def _finished_quantity(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_DOWN))


def _component_identity(
    snapshot: PurchasePurposeSourceSnapshot,
    *,
    bom_component_id: int | None,
    component_product_code: str | None,
    component_product_name: str | None,
) -> tuple[str, str]:
    component_type = str(snapshot.component_type or "whole").strip().lower()
    type_label = {"cover": "盖片", "base": "底片"}.get(component_type)
    if bom_component_id is not None:
        identity = f"bom:{int(bom_component_id)}:{component_type}"
        product_label = str(component_product_name or component_product_code or "").strip()
        if product_label and type_label:
            return identity, f"{product_label}（{type_label}）"
        if product_label:
            return identity, product_label
        return identity, type_label or "BOM组件"
    if component_type in {"cover", "base"}:
        return component_type, type_label or component_type
    return "whole", "整张片料"


def _empty_summary() -> dict[str, Any]:
    return {
        "order_purpose_received_sheet_qty": 0,
        "reserve_purpose_received_sheet_qty": 0,
        "automatic_finished_output_qty": 0,
        "automatic_order_reserved_quantity": 0,
        "automatic_surplus_finished_quantity": 0,
        "current_theoretical_finished_capacity_qty": 0,
        "currently_unposted_finished_capacity_qty": 0,
        "future_planned_finished_capacity_qty": 0,
        "remaining_order_purpose_sheet_qty": 0,
        "waiting_component_labels": [],
        "waiting_component_gap_quantity": 0,
        "component_progress": [],
        "projection_inconsistent": False,
    }


def receipt_purpose_summaries_by_order_item_ids(
    db: Session,
    order_item_ids: Sequence[int],
) -> dict[int, dict[str, Any]]:
    """Return frozen-purpose ownership, component progress and auto output.

    Ownership is the union of a currently valid frozen procurement source and
    any still-active frozen receipt allocation.  This closes the window before
    the first receipt without forgetting already-posted auto inventory when a
    procurement row later changes lifecycle state.
    """

    normalized_ids = sorted({int(item_id) for item_id in order_item_ids})
    if not normalized_ids:
        return {}
    normalized_id_set = set(normalized_ids)
    supplier_sales_item = aliased(OrderItem)
    supplier_sales_order = aliased(Order)
    requisition_sales_item = aliased(OrderItem)
    requisition_sales_order = aliased(Order)
    bom_sales_item = aliased(OrderItem)
    bom_sales_order = aliased(Order)
    snapshot_sales_item = aliased(OrderItem)
    snapshot_sales_order = aliased(Order)
    source_requisition_item = aliased(RequisitionItem)
    snapshot_rows = db.execute(
        select(
            PurchasePurposeSourceSnapshot,
            SupplierRequisitionOrderItem.order_item_id.label("supplier_parent_id"),
            SupplierRequisitionOrderItem.status.label("supplier_item_status"),
            SupplierRequisitionOrderItem.purpose_contract_status.label(
                "supplier_contract_status"
            ),
            SupplierRequisitionOrder.status.label("supplier_header_status"),
            RequisitionItem.order_item_id.label("requisition_parent_id"),
            RequisitionItem.status.label("requisition_item_status"),
            RequisitionItem.purpose_contract_status.label(
                "requisition_contract_status"
            ),
            RequisitionItemBomSource.id.label("bom_source_id"),
            RequisitionItemBomSource.requisition_item_id.label(
                "bom_requisition_item_id"
            ),
            RequisitionItemBomSource.sales_order_item_bom_component_id.label(
                "bom_component_id"
            ),
            RequisitionItemBomSource.component_type.label("bom_component_type"),
            RequisitionItemBomSource.active_guard.label("bom_active_guard"),
            SalesOrderItemBomComponent.sales_order_item_id.label(
                "bom_parent_id"
            ),
            SalesOrderItemBomComponent.snapshot_component_product_code,
            SalesOrderItemBomComponent.snapshot_component_product_name,
            supplier_sales_order.customer_id.label("supplier_customer_id"),
            requisition_sales_order.customer_id.label("requisition_customer_id"),
            bom_sales_order.customer_id.label("bom_customer_id"),
            snapshot_sales_order.customer_id.label("snapshot_source_customer_id"),
            source_requisition_item.id.label("source_requisition_item_id"),
            source_requisition_item.order_item_id.label(
                "source_requisition_parent_id"
            ),
        )
        .outerjoin(
            SupplierRequisitionOrderItem,
            SupplierRequisitionOrderItem.id
            == PurchasePurposeSourceSnapshot.supplier_requisition_order_item_id,
        )
        .outerjoin(
            SupplierRequisitionOrder,
            SupplierRequisitionOrder.id
            == SupplierRequisitionOrderItem.supplier_order_id,
        )
        .outerjoin(
            RequisitionItem,
            RequisitionItem.id
            == PurchasePurposeSourceSnapshot.material_requisition_item_id,
        )
        .outerjoin(
            RequisitionItemBomSource,
            RequisitionItemBomSource.id
            == PurchasePurposeSourceSnapshot.source_bom_requisition_source_id,
        )
        .outerjoin(
            SalesOrderItemBomComponent,
            SalesOrderItemBomComponent.id
            == RequisitionItemBomSource.sales_order_item_bom_component_id,
        )
        .outerjoin(
            supplier_sales_item,
            supplier_sales_item.id == SupplierRequisitionOrderItem.order_item_id,
        )
        .outerjoin(
            supplier_sales_order,
            supplier_sales_order.id == supplier_sales_item.order_id,
        )
        .outerjoin(
            requisition_sales_item,
            requisition_sales_item.id == RequisitionItem.order_item_id,
        )
        .outerjoin(
            requisition_sales_order,
            requisition_sales_order.id == requisition_sales_item.order_id,
        )
        .outerjoin(
            bom_sales_item,
            bom_sales_item.id == SalesOrderItemBomComponent.sales_order_item_id,
        )
        .outerjoin(
            bom_sales_order,
            bom_sales_order.id == bom_sales_item.order_id,
        )
        .outerjoin(
            snapshot_sales_item,
            snapshot_sales_item.id
            == PurchasePurposeSourceSnapshot.source_order_item_id,
        )
        .outerjoin(
            snapshot_sales_order,
            snapshot_sales_order.id == snapshot_sales_item.order_id,
        )
        .outerjoin(
            source_requisition_item,
            source_requisition_item.id
            == PurchasePurposeSourceSnapshot.source_requisition_item_id,
        )
        .where(
            PurchasePurposeSourceSnapshot.order_purpose_sheet_qty > 0,
            or_(
                SupplierRequisitionOrderItem.order_item_id.in_(normalized_ids),
                RequisitionItem.order_item_id.in_(normalized_ids),
                PurchasePurposeSourceSnapshot.source_order_item_id.in_(
                    normalized_ids
                ),
                SalesOrderItemBomComponent.sales_order_item_id.in_(normalized_ids),
                source_requisition_item.order_item_id.in_(normalized_ids),
            ),
        )
        .order_by(PurchasePurposeSourceSnapshot.id)
    ).all()
    if not snapshot_rows:
        return {}

    snapshot_facts: dict[int, dict[str, Any]] = {}
    active_source_snapshot_ids: set[int] = set()
    active_conflict_snapshot_ids: set[int] = set()
    conflict_candidates_by_snapshot_id: dict[int, set[int]] = {}
    for row in snapshot_rows:
        snapshot = row[0]
        (
            supplier_parent_id,
            supplier_item_status,
            supplier_contract_status,
            supplier_header_status,
            requisition_parent_id,
            requisition_item_status,
            requisition_contract_status,
            bom_source_id,
            bom_requisition_item_id,
            bom_component_id,
            bom_component_type,
            bom_active_guard,
            bom_parent_id,
            component_product_code,
            component_product_name,
            supplier_customer_id,
            requisition_customer_id,
            bom_customer_id,
            snapshot_source_customer_id,
            source_requisition_item_id,
            source_requisition_parent_id,
        ) = row[1:]
        snapshot_id = int(snapshot.id)
        source_kind = str(snapshot.source_kind or "").strip()
        snapshot_parent = (
            int(snapshot.source_order_item_id)
            if snapshot.source_order_item_id is not None
            else None
        )
        supplier_parent = (
            int(supplier_parent_id) if supplier_parent_id is not None else None
        )
        requisition_parent = (
            int(requisition_parent_id)
            if requisition_parent_id is not None
            else None
        )
        bom_parent = int(bom_parent_id) if bom_parent_id is not None else None
        source_requisition_parent = (
            int(source_requisition_parent_id)
            if source_requisition_parent_id is not None
            else None
        )
        candidate_parent_ids = {
            candidate_id
            for candidate_id in (
                supplier_parent,
                requisition_parent,
                bom_parent,
                snapshot_parent,
                source_requisition_parent,
            )
            if candidate_id is not None
        }
        requested_candidate_ids = candidate_parent_ids & normalized_id_set
        inconsistent = any(
            customer_id is None or int(customer_id) != int(snapshot.customer_id)
            for candidate_id, customer_id in (
                (supplier_parent, supplier_customer_id),
                (requisition_parent, requisition_customer_id),
                (bom_parent, bom_customer_id),
                (snapshot_parent, snapshot_source_customer_id),
            )
            if candidate_id is not None
        )
        active_source = False
        if snapshot.supplier_requisition_order_item_id is not None:
            parent_id = supplier_parent
            active_source = bool(
                supplier_item_status == "active"
                and supplier_header_status == "confirmed"
                and supplier_contract_status == "frozen"
                and (source_kind != "bom_component" or bom_active_guard == 1)
            )
            if source_kind == "order_item":
                identity_matches = bool(
                    snapshot_parent == parent_id
                    and source_requisition_item_id is None
                    and bom_source_id is None
                )
            elif source_kind == "requisition_item":
                identity_matches = bool(
                    snapshot_parent == parent_id
                    and source_requisition_item_id is not None
                    and source_requisition_parent == parent_id
                    and bom_source_id is None
                )
            elif source_kind == "bom_component":
                identity_matches = bool(
                    snapshot_parent is None
                    and source_requisition_item_id is None
                    and bom_source_id is not None
                    and bom_parent == parent_id
                    and str(bom_component_type or "").strip().lower()
                    == str(snapshot.component_type or "").strip().lower()
                )
            elif source_kind == "direct_supplier_item":
                identity_matches = bool(
                    snapshot_parent is None
                    and source_requisition_item_id is None
                    and bom_source_id is None
                )
            else:
                identity_matches = False
            inconsistent = bool(
                inconsistent or parent_id is None or not identity_matches
            )
        else:
            parent_id = requisition_parent
            active_source = bool(
                requisition_item_status == "有效"
                and requisition_contract_status == "frozen"
                and (source_kind != "bom_component" or bom_active_guard == 1)
            )
            if source_kind == "order_item":
                identity_matches = bool(
                    snapshot_parent == parent_id
                    and source_requisition_item_id is None
                    and bom_source_id is None
                )
            elif source_kind == "requisition_item":
                identity_matches = bool(
                    snapshot_parent == parent_id
                    and source_requisition_item_id is not None
                    and source_requisition_parent == parent_id
                    and bom_source_id is None
                )
            elif source_kind == "bom_component":
                identity_matches = bool(
                    snapshot_parent is None
                    and source_requisition_item_id is None
                    and bom_source_id is not None
                    and bom_requisition_item_id
                    == snapshot.material_requisition_item_id
                    and bom_parent == parent_id
                    and str(bom_component_type or "").strip().lower()
                    == str(snapshot.component_type or "").strip().lower()
                )
            else:
                identity_matches = False
            inconsistent = bool(
                inconsistent or parent_id is None or not identity_matches
            )
        if inconsistent:
            if requested_candidate_ids:
                conflict_candidates_by_snapshot_id[snapshot_id] = (
                    requested_candidate_ids
                )
                if active_source:
                    active_conflict_snapshot_ids.add(snapshot_id)
            continue
        if parent_id is None or parent_id not in normalized_id_set:
            continue
        snapshot_facts[snapshot_id] = {
            "snapshot": snapshot,
            "order_item_id": parent_id,
            "bom_component_id": (
                int(bom_component_id) if bom_component_id is not None else None
            ),
            "component_product_code": component_product_code,
            "component_product_name": component_product_name,
        }
        if active_source:
            active_source_snapshot_ids.add(snapshot_id)

    snapshot_ids = sorted(
        set(snapshot_facts) | set(conflict_candidates_by_snapshot_id)
    )
    if not snapshot_ids:
        return {}
    allocations = list(
        db.scalars(
            select(IncomingReceiptPurposeAllocation)
            .where(
                IncomingReceiptPurposeAllocation.purchase_purpose_source_snapshot_id.in_(
                    snapshot_ids
                ),
                IncomingReceiptPurposeAllocation.status == "posted",
                IncomingReceiptPurposeAllocation.purpose_contract_status_snapshot
                == "frozen",
                ~exists(
                    select(IncomingReceiptPurposeReversal.id).where(
                        IncomingReceiptPurposeReversal.incoming_receipt_purpose_allocation_id
                        == IncomingReceiptPurposeAllocation.id
                    )
                ),
            )
            .order_by(IncomingReceiptPurposeAllocation.id)
        ).all()
    )
    allocations_by_snapshot: dict[int, list[IncomingReceiptPurposeAllocation]] = {}
    for allocation in allocations:
        snapshot_id = int(allocation.purchase_purpose_source_snapshot_id or 0)
        allocations_by_snapshot.setdefault(snapshot_id, []).append(allocation)
    completion_ids = sorted(
        {
            int(allocation.production_completion_id)
            for allocation in allocations
            if allocation.production_completion_id is not None
        }
    )
    completions_by_id = {
        int(completion.id): completion
        for completion in db.scalars(
            select(ProductionCompletion).where(
                ProductionCompletion.id.in_(completion_ids)
            )
        ).all()
    } if completion_ids else {}
    receipt_auto_completions_by_item: dict[int, list[ProductionCompletion]] = {}
    for completion in db.scalars(
        select(ProductionCompletion)
        .where(
            ProductionCompletion.order_item_id.in_(normalized_ids),
            ProductionCompletion.status == "posted",
            ProductionCompletion.origin == "receipt_auto",
        )
        .order_by(ProductionCompletion.id)
    ).all():
        receipt_auto_completions_by_item.setdefault(
            int(completion.order_item_id), []
        ).append(completion)
    semi_credits_by_item_component: dict[tuple[int, str], int] = {}
    for requirement, credited in db.execute(
        select(
            OrderItemSemiRequirement,
            func.coalesce(
                func.sum(
                    InventoryReservation.credited_requirement_quantity
                    - InventoryReservation.released_requirement_quantity
                ),
                0,
            ),
        )
        .join(
            InventoryReservation,
            InventoryReservation.semi_requirement_id == OrderItemSemiRequirement.id,
        )
        .where(
            OrderItemSemiRequirement.order_item_id.in_(normalized_ids),
            InventoryReservation.reservation_type == "semi_order",
            InventoryReservation.status != "cancelled",
        )
        .group_by(OrderItemSemiRequirement.id)
    ).all():
        component_type = str(requirement.component_type or "whole").strip().lower()
        component_type = (
            component_type
            if component_type in {"whole", "cover", "base"}
            else "whole"
        )
        component_key = (
            f"bom:{int(requirement.sales_order_item_bom_component_id)}:{component_type}"
            if requirement.sales_order_item_bom_component_id is not None
            else component_type
        )
        key = (int(requirement.order_item_id), component_key)
        semi_credits_by_item_component[key] = (
            semi_credits_by_item_component.get(key, 0)
            + max(int(credited or 0), 0)
        )
    managed_snapshot_ids = active_source_snapshot_ids | set(allocations_by_snapshot)
    managed_conflict_snapshot_ids = active_conflict_snapshot_ids | (
        set(allocations_by_snapshot) & set(conflict_candidates_by_snapshot_id)
    )

    summaries: dict[int, dict[str, Any]] = {
        order_item_id: {
            **_empty_summary(),
            "projection_inconsistent": True,
        }
        for snapshot_id in managed_conflict_snapshot_ids
        for order_item_id in conflict_candidates_by_snapshot_id.get(snapshot_id, set())
    }
    component_state: dict[int, dict[str, dict[str, Any]]] = {}
    for snapshot_id in sorted(managed_snapshot_ids):
        facts = snapshot_facts.get(snapshot_id)
        if facts is None:
            continue
        snapshot = facts["snapshot"]
        order_item_id = int(facts["order_item_id"])
        summary = summaries.setdefault(order_item_id, _empty_summary())
        component_key, component_label = _component_identity(
            snapshot,
            bom_component_id=facts["bom_component_id"],
            component_product_code=facts["component_product_code"],
            component_product_name=facts["component_product_name"],
        )
        states = component_state.setdefault(order_item_id, {})
        state = states.setdefault(
            component_key,
            {
                "component_key": component_key,
                "component_label": component_label,
                "component_type": str(snapshot.component_type or "whole"),
                "planned_order_sheet_qty": 0,
                "received_order_sheet_qty": 0,
                "reserve_received_sheet_qty": 0,
                "planned_capacity": Decimal("0"),
                "received_capacity": Decimal("0"),
                "pieces_per_finished": 1,
                "semi_reserved_piece_qty": 0,
            },
        )
        pieces_per_finished = max(int(snapshot.pieces_per_finished_snapshot or 1), 1)
        state["pieces_per_finished"] = max(
            int(state["pieces_per_finished"]),
            pieces_per_finished,
        )
        yield_per_sheet = max(int(snapshot.yield_per_sheet_snapshot or 1), 1)
        received_order_sheets = 0
        for allocation in allocations_by_snapshot.get(snapshot_id, []):
            order_sheets = max(
                int(allocation.receipt_order_purpose_sheet_qty or 0),
                0,
            )
            reserve_sheets = max(
                int(allocation.receipt_reserve_purpose_sheet_qty or 0),
                0,
            )
            summary["order_purpose_received_sheet_qty"] += order_sheets
            summary["reserve_purpose_received_sheet_qty"] += reserve_sheets
            output_delta = max(
                int(allocation.finished_output_qty_delta or 0),
                0,
            )
            summary["automatic_finished_output_qty"] += output_delta
            completion = (
                completions_by_id.get(int(allocation.production_completion_id))
                if allocation.production_completion_id is not None
                else None
            )
            if output_delta > 0:
                completion_valid = bool(
                    completion is not None
                    and completion.status == "posted"
                    and completion.origin == "receipt_auto"
                    and int(completion.order_item_id) == order_item_id
                    and int(completion.actual_output_quantity or 0) == output_delta
                    and int(completion.order_reserved_quantity or 0)
                    + int(completion.surplus_finished_quantity or 0)
                    == output_delta
                )
                if not completion_valid:
                    summary["projection_inconsistent"] = True
                else:
                    summary["automatic_order_reserved_quantity"] += max(
                        int(completion.order_reserved_quantity or 0),
                        0,
                    )
                    summary["automatic_surplus_finished_quantity"] += max(
                        int(completion.surplus_finished_quantity or 0),
                        0,
                    )
            elif completion is not None:
                summary["projection_inconsistent"] = True
            received_order_sheets += order_sheets
            state["received_order_sheet_qty"] += order_sheets
            state["reserve_received_sheet_qty"] += reserve_sheets
            state["received_capacity"] += (
                Decimal(order_sheets * yield_per_sheet)
                / Decimal(pieces_per_finished)
            )
        # A superseded/voided source remains part of the physical capacity
        # ledger while one of its allocations is active, but its unreceived
        # frozen plan is no longer a future procurement promise.  Counting the
        # old full plan here would duplicate the replacement source's plan.
        planned_sheets = (
            max(int(snapshot.order_purpose_sheet_qty or 0), 0)
            if snapshot_id in active_source_snapshot_ids
            else received_order_sheets
        )
        state["planned_order_sheet_qty"] += planned_sheets
        state["planned_capacity"] += (
            Decimal(planned_sheets * yield_per_sheet)
            / Decimal(pieces_per_finished)
        )

    from app.models.warehouse_inventory import InventoryLot
    from app.services.unfinished_components import unfinished_reservations
    import json
    unfinished_items={r.order_item_id for r,_,_ in unfinished_reservations(db,normalized_ids)}
    manual_by_item={}
    for completion,encoded,root_product_id in db.execute(select(ProductionCompletion,InventoryLot.cost_snapshot_detail_json,OrderItem.product_id)
            .join(InventoryLot,InventoryLot.id==ProductionCompletion.inventory_lot_id)
            .join(OrderItem,OrderItem.id==ProductionCompletion.order_item_id)
            .where(ProductionCompletion.order_item_id.in_(normalized_ids),ProductionCompletion.status=='posted',ProductionCompletion.origin=='manual')):
        detail=json.loads(encoded or '{}')
        if detail.get('component_processing_confirmation') and detail.get('processing_product_id')==root_product_id:
            manual_by_item.setdefault(completion.order_item_id,[]).append(completion)
    for order_item_id, summary in summaries.items():
        manual_rows=manual_by_item.get(order_item_id,[])
        summary['manual_processing_output_qty']=sum(r.actual_output_quantity for r in manual_rows)
        summary['manual_processing_reserved_qty']=sum(r.order_reserved_quantity for r in manual_rows)
        summary['manual_processing_surplus_qty']=sum(r.surplus_finished_quantity for r in manual_rows)
        summary['requires_component_processing']=order_item_id in unfinished_items
        from app.models.multilevel_bom import OrderBomGraph
        if db.get(OrderBomGraph, order_item_id) is not None:
            from app.services.multilevel_bom_receipt_projection import project_graph_receipts
            project_graph_receipts(db, order_item_id, summary,
                component_state.get(order_item_id, {}).values(), semi_credits_by_item_component)
            continue
        states = list(component_state.get(order_item_id, {}).values())
        component_rows: list[dict[str, Any]] = []
        received_capacities: list[int] = []
        planned_capacities: list[int] = []
        for state in states:
            pieces_per_finished = max(int(state.pop("pieces_per_finished")), 1)
            semi_reserved_pieces = semi_credits_by_item_component.get(
                (order_item_id, str(state["component_key"])),
                0,
            )
            state["semi_reserved_piece_qty"] = semi_reserved_pieces
            semi_capacity = Decimal(semi_reserved_pieces) / Decimal(
                pieces_per_finished
            )
            state["received_capacity"] += semi_capacity
            state["planned_capacity"] += semi_capacity
            received_capacity = _finished_quantity(state.pop("received_capacity"))
            planned_capacity = _finished_quantity(state.pop("planned_capacity"))
            received_capacities.append(received_capacity)
            planned_capacities.append(planned_capacity)
            component_rows.append(
                {
                    **state,
                    "current_finished_capacity_qty": received_capacity,
                    "planned_finished_capacity_qty": planned_capacity,
                    "remaining_order_sheet_qty": max(
                        int(state["planned_order_sheet_qty"])
                        - int(state["received_order_sheet_qty"]),
                        0,
                    ),
                    "over_received_order_sheet_qty": max(
                        int(state["received_order_sheet_qty"])
                        - int(state["planned_order_sheet_qty"]),
                        0,
                    ),
                }
            )
        requires_pairing = bool(
            len(component_rows) > 1
            or any(
                row["component_key"].startswith("bom:")
                or row["component_type"] in {"cover", "base"}
                for row in component_rows
            )
        )
        if requires_pairing:
            theoretical_capacity = min(received_capacities, default=0)
            planned_capacity = min(planned_capacities, default=0)
        else:
            theoretical_capacity = sum(received_capacities)
            planned_capacity = sum(planned_capacities)
        highest_component_capacity = max(received_capacities, default=0)
        minimum_capacity = min(received_capacities, default=0)
        waiting_rows = [
            row
            for row in component_rows
            if requires_pairing
            and int(row["current_finished_capacity_qty"]) == minimum_capacity
            and int(row["remaining_order_sheet_qty"]) > 0
        ]
        for row in component_rows:
            row["is_bottleneck"] = bool(
                requires_pairing
                and int(row["current_finished_capacity_qty"])
                == minimum_capacity
                and int(row["remaining_order_sheet_qty"]) > 0
            )
        posted_auto_completions = receipt_auto_completions_by_item.get(
            order_item_id, []
        )
        if posted_auto_completions:
            posted_output = sum(
                max(int(row.actual_output_quantity or 0), 0)
                for row in posted_auto_completions
            )
            posted_order_reserved = sum(
                max(int(row.order_reserved_quantity or 0), 0)
                for row in posted_auto_completions
            )
            posted_surplus = sum(
                max(int(row.surplus_finished_quantity or 0), 0)
                for row in posted_auto_completions
            )
            if posted_order_reserved + posted_surplus != posted_output:
                summary["projection_inconsistent"] = True
            summary["automatic_finished_output_qty"] = posted_output
            summary["automatic_order_reserved_quantity"] = posted_order_reserved
            summary["automatic_surplus_finished_quantity"] = posted_surplus
        automatic_output = int(summary["automatic_finished_output_qty"])
        combined_output=automatic_output+summary['manual_processing_output_qty']
        unposted_capacity=max(theoretical_capacity-combined_output,0)
        summary['pending_processing_quantity']=unposted_capacity if summary['requires_component_processing'] else 0
        currently_unposted = 0 if summary['requires_component_processing'] else unposted_capacity
        automatic_output_exceeds_capacity = combined_output > theoretical_capacity
        summary.update(
            {
                "current_theoretical_finished_capacity_qty": theoretical_capacity,
                "currently_unposted_finished_capacity_qty": currently_unposted,
                "future_planned_finished_capacity_qty": max(
                    planned_capacity - combined_output,
                    0,
                ),
                "remaining_order_purpose_sheet_qty": sum(
                    int(row["remaining_order_sheet_qty"])
                    for row in component_rows
                ),
                "waiting_component_labels": [
                    str(row["component_label"]) for row in waiting_rows
                ],
                "waiting_component_gap_quantity": max(
                    (
                        highest_component_capacity
                        - int(row["current_finished_capacity_qty"])
                        for row in waiting_rows
                    ),
                    default=0,
                ),
                "component_progress": component_rows,
                "projection_inconsistent": bool(
                    summary.get("projection_inconsistent")
                    or currently_unposted > 0
                    or automatic_output_exceeds_capacity
                ),
            }
        )
    return summaries


def receipt_managed_order_item_ids(
    db: Session,
    order_item_ids: Sequence[int],
) -> set[int]:
    return set(receipt_purpose_summaries_by_order_item_ids(db, order_item_ids))
