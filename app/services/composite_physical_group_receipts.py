"""P1-150B physical-group receipt, component stock and reversal workflow.

The physical purchase group is the receipt authority.  Its immutable sources
remain useful for order traceability, but they may never be received one by
one: one physical sheet is costed once, cut once and then reserved to source
orders in the group's frozen order.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
import hashlib
import hmac
import json
from types import SimpleNamespace
from typing import Any, Iterable

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session, selectinload

from app.core.time_contract import beijing_today, utc_now_naive
from app.models.composite_purchase_group import (
    CompositePhysicalGroupReceipt,
    CompositePhysicalGroupReceiptReversal,
    CompositePhysicalGroupReceiptSourceAllocation,
    CompositePhysicalPurchaseGroup,
    CompositePhysicalPurchaseGroupSource,
)
from app.models.customer import Customer
from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem
from app.models.material import Material
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.purchase_receipt import PurchaseReceiptFact
from app.models.requisition import Requisition, RequisitionItem
from app.models.user import User
from app.models.supplier_requisition_order import (
    PurchasePurposeSourceSnapshot,
    SupplierRequisitionOrder,
    SupplierRequisitionOrderItem,
)
from app.models.warehouse_inventory import (
    InventoryLot,
    InventoryLotTransfer,
    InventoryMovement,
    InventoryReservation,
)
from app.services.audit_log import append_audit_event
from app.services.incoming_receipts import (
    IncomingReceiptError,
    _all_expected_bom_sources_received,
    _number as _incoming_number,
    _open_requisition_status,
)
from app.services.order_status_policy import (
    order_item_forward_block_message,
    order_item_forward_block_reason,
)
from app.services.production_workflow import (
    ProductionWorkflowError,
    has_dispatched_delivery_facts,
    lock_order_rows_for_production_transition,
    receipt_auto_finished_location_projection,
    refresh_existing_production_task,
    refresh_order_production_status,
)
from app.services.purchase_receipt_facts import (
    MONEY_QUANTUM,
    calculate_per_sheet_cost,
    canonical_purchase_receipt_hash,
    material_calculation_fingerprint,
)
from app.services.receipt_purpose_distribution import (
    ReceiptPurposeFlowError,
    resolve_receipt_purpose_context,
)
from app.services.supplier_monthly_settlement import (
    SupplierSettlementError,
    assert_receipt_item_not_in_confirmed_statement,
)
from app.services.warehouse_inventory import (
    WarehouseInventoryError,
    _balances,
    _movement,
    _number as _warehouse_number,
    automatic_raw_material_staging_location,
    manual_finished_in,
    manual_semi_finished_in,
    mutate_lot,
    release_finished_reservation,
)


GROUP_ROUTE_PREFIX = "cg"
GROUP_LOT_REF_TYPE = "composite_physical_group_receipt"
GROUP_RECEIVABLE_STATUSES = {"active", "partially_received", "received", "reversed"}
_MONEY = MONEY_QUANTUM


def _money(value: Decimal | int | str) -> Decimal:
    return Decimal(value).quantize(_MONEY, rounding=ROUND_HALF_UP)


def is_composite_physical_group_key(item_key: int | str) -> bool:
    text = str(item_key or "").strip().lower()
    return text.startswith(GROUP_ROUTE_PREFIX) and text[2:].isdigit()


def composite_physical_group_id(item_key: int | str) -> int | None:
    if not is_composite_physical_group_key(item_key):
        return None
    return int(str(item_key).strip()[2:])


def composite_physical_group_route(group_id: int) -> str:
    return f"{GROUP_ROUTE_PREFIX}{int(group_id)}"


def _source_rows(
    db: Session, group_id: int
) -> list[CompositePhysicalPurchaseGroupSource]:
    return list(
        db.scalars(
            select(CompositePhysicalPurchaseGroupSource)
            .where(
                CompositePhysicalPurchaseGroupSource.composite_physical_purchase_group_id
                == int(group_id)
            )
            .order_by(
                CompositePhysicalPurchaseGroupSource.source_sequence,
                CompositePhysicalPurchaseGroupSource.id,
            )
        ).all()
    )


def _anchor_source(
    sources: Iterable[CompositePhysicalPurchaseGroupSource],
) -> CompositePhysicalPurchaseGroupSource:
    ordered = sorted(sources, key=lambda row: (int(row.source_sequence), int(row.id)))
    if not ordered:
        raise IncomingReceiptError("物理采购组缺少来源明细，已停止收料", 409)
    return next(
        (
            row
            for row in ordered
            if int(row.allocated_order_purpose_sheet_quantity or 0) > 0
            and row.purchase_purpose_source_snapshot_id is not None
        ),
        next(
            (
                row
                for row in ordered
                if row.purchase_purpose_source_snapshot_id is not None
            ),
            ordered[0],
        ),
    )


def _receipt_contract_anchor(
    db: Session,
    *,
    group: CompositePhysicalPurchaseGroup,
    sources: list[CompositePhysicalPurchaseGroupSource],
) -> tuple[
    CompositePhysicalPurchaseGroupSource,
    SupplierRequisitionOrderItem | RequisitionItem,
    PurchasePurposeSourceSnapshot,
]:
    """Resolve the one authoritative price contract, supplier conversion first."""

    if group.supplier_requisition_order_item_id is not None:
        snapshots = {
            int(snapshot.id): snapshot
            for snapshot in db.scalars(
                select(PurchasePurposeSourceSnapshot).where(
                    PurchasePurposeSourceSnapshot.id.in_(
                        [
                            int(row.purchase_purpose_source_snapshot_id)
                            for row in sources
                            if row.purchase_purpose_source_snapshot_id is not None
                        ]
                    )
                )
            ).all()
        }
        anchor = next(
            (
                row
                for row in sources
                if row.purchase_purpose_source_snapshot_id is not None
                and (
                    snapshot := snapshots.get(
                        int(row.purchase_purpose_source_snapshot_id)
                    )
                )
                is not None
                and snapshot.supplier_requisition_order_item_id
                == int(group.supplier_requisition_order_item_id)
            ),
            None,
        )
        if anchor is None:
            raise IncomingReceiptError(
                "物理采购组供应商转换锚点与组内来源不一致，已停止收料。",
                409,
                code="COMPOSITE_GROUP_PRICE_ANCHOR_INVALID",
            )
        snapshot = snapshots[int(anchor.purchase_purpose_source_snapshot_id)]
        source = db.get(
            SupplierRequisitionOrderItem,
            int(group.supplier_requisition_order_item_id),
        )
        header = (
            db.get(SupplierRequisitionOrder, int(source.supplier_order_id))
            if source is not None
            else None
        )
        if (
            source is None
            or source.status != "active"
            or header is None
            or header.status != "confirmed"
            or snapshot.supplier_requisition_order_item_id != source.id
            or snapshot.material_requisition_item_id is not None
        ):
            raise IncomingReceiptError(
                "物理采购组供应商转换锚点与用途快照不一致，已停止收料。",
                409,
                code="COMPOSITE_GROUP_PRICE_ANCHOR_INVALID",
            )
        return anchor, source, snapshot
    anchor = _anchor_source(sources)
    snapshot_id = anchor.purchase_purpose_source_snapshot_id
    if snapshot_id is None:
        raise IncomingReceiptError(
            "物理采购组缺少价格锚点用途快照，已停止收料。",
            409,
            code="COMPOSITE_GROUP_PRICE_ANCHOR_INVALID",
        )
    snapshot = db.get(PurchasePurposeSourceSnapshot, int(snapshot_id))
    if snapshot is None:
        raise IncomingReceiptError(
            "物理采购组价格锚点用途快照不存在，已停止收料。",
            409,
            code="COMPOSITE_GROUP_PRICE_ANCHOR_INVALID",
        )
    source = db.get(RequisitionItem, int(anchor.requisition_item_id))
    if (
        source is None
        or snapshot.material_requisition_item_id != source.id
        or snapshot.supplier_requisition_order_item_id is not None
    ):
        raise IncomingReceiptError(
            "物理采购组报料锚点与用途快照不一致，已停止收料。",
            409,
            code="COMPOSITE_GROUP_PRICE_ANCHOR_INVALID",
        )
    return anchor, source, snapshot


def _group_contract_fields(
    db: Session,
    *,
    group: CompositePhysicalPurchaseGroup,
    sources: list[CompositePhysicalPurchaseGroupSource],
) -> dict[str, Any]:
    """Serialize the live, supplier-first receipt contract for one cg row."""

    try:
        _anchor, source, snapshot = _receipt_contract_anchor(
            db, group=group, sources=sources
        )
    except IncomingReceiptError as error:
        return {
            "source_key": composite_physical_group_route(group.id),
            "purpose_status": "frozen",
            "receipt_fact_ready": False,
            "purpose_issue": str(error),
        }
    fact_filter = (
        PurchaseReceiptFact.supplier_requisition_order_item_id == source.id
        if isinstance(source, SupplierRequisitionOrderItem)
        else PurchaseReceiptFact.material_requisition_item_id == source.id
    )
    fact = db.scalar(
        select(PurchaseReceiptFact)
        .where(
            fact_filter,
            PurchaseReceiptFact.purchase_purpose_source_snapshot_id == snapshot.id,
        )
        .order_by(
            PurchaseReceiptFact.receipt_fact_version.desc(),
            PurchaseReceiptFact.id.desc(),
        )
    )
    material = db.get(Material, fact.actual_material_id) if fact is not None else None
    fact_ready = bool(
        fact is not None
        and int(fact.expected_source_version) == int(source.version)
        and int(fact.purpose_snapshot_version) == int(snapshot.snapshot_version)
        and fact.receipt_plan_fingerprint == snapshot.preview_fingerprint
        and material is not None
        and bool(material.is_active)
        and int(material.version) == int(fact.actual_material_version)
        and material_calculation_fingerprint(material)
        == fact.actual_material_fingerprint
    )
    result: dict[str, Any] = {
        "source_key": composite_physical_group_route(group.id),
        "purchase_purpose_source_snapshot_id": int(snapshot.id),
        "expected_source_version": int(source.version),
        "expected_purpose_snapshot_version": int(snapshot.snapshot_version),
        "receipt_plan_fingerprint": snapshot.preview_fingerprint,
        "purpose_status": "frozen",
        "receipt_fact_ready": fact_ready,
        "latest_receipt_fact_version": (
            int(fact.receipt_fact_version) if fact is not None else 0
        ),
        "expected_receipt_fact_version": (
            int(fact.receipt_fact_version) if fact_ready else None
        ),
        "expected_actual_material_version": (
            int(fact.actual_material_version) if fact_ready else None
        ),
        "actual_material_fingerprint": (
            fact.actual_material_fingerprint if fact_ready else None
        ),
        "actual_material_id": fact.actual_material_id if fact_ready else None,
        "reported_material_code": group.material_code_snapshot,
        "formal_material_code": group.material_code_snapshot,
        "actual_material_code": (
            fact.actual_material_code_snapshot if fact_ready else group.material_code_snapshot
        ),
        "actual_material_flute_type": (
            fact.actual_material_flute_type_snapshot
            if fact_ready
            else group.flute_type_snapshot
        ),
    }
    if not fact_ready:
        result["purpose_issue"] = "请先确认该物理采购组的实际材质和正式采购价格"
    else:
        result["purpose_issue"] = None
    return result


def group_for_legacy_receipt_route(
    db: Session, item_key: int | str
) -> CompositePhysicalPurchaseGroup | None:
    """Resolve an old per-source receipt route that now belongs to a group."""

    text = str(item_key or "").strip().lower()
    if text.startswith("r") and text[1:].isdigit():
        group_id = db.scalar(
            select(
                CompositePhysicalPurchaseGroupSource.composite_physical_purchase_group_id
            ).where(
                CompositePhysicalPurchaseGroupSource.requisition_item_id
                == int(text[1:])
            )
        )
    elif text.startswith("so") and text[2:].isdigit():
        group_id = db.scalar(
            select(
                CompositePhysicalPurchaseGroupSource.composite_physical_purchase_group_id
            )
            .join(
                PurchasePurposeSourceSnapshot,
                PurchasePurposeSourceSnapshot.id
                == CompositePhysicalPurchaseGroupSource.purchase_purpose_source_snapshot_id,
            )
            .where(
                PurchasePurposeSourceSnapshot.supplier_requisition_order_item_id
                == int(text[2:])
            )
        )
    else:
        return None
    return db.get(CompositePhysicalPurchaseGroup, int(group_id)) if group_id else None


def require_group_receipt_route(db: Session, item_key: int | str) -> None:
    group = group_for_legacy_receipt_route(db, item_key)
    if group is None:
        return
    route = composite_physical_group_route(group.id)
    raise IncomingReceiptError(
        f"该来源属于同一物理采购组，必须改用 {route} 一次确认实际材质、价格和实收张数。",
        409,
        code="COMPOSITE_PHYSICAL_GROUP_RECEIPT_REQUIRED",
    )


def group_customer_id_for_route(db: Session, item_key: int | str) -> int | None:
    group_id = composite_physical_group_id(item_key)
    if group_id is None:
        return None
    return db.scalar(
        select(CompositePhysicalPurchaseGroup.customer_id).where(
            CompositePhysicalPurchaseGroup.id == group_id
        )
    )


def group_customer_id_for_receipt_item(
    db: Session, receipt_item_id: int
) -> int | None:
    return db.scalar(
        select(CompositePhysicalPurchaseGroup.customer_id)
        .join(
            IncomingReceiptItem,
            IncomingReceiptItem.composite_physical_purchase_group_id
            == CompositePhysicalPurchaseGroup.id,
        )
        .where(IncomingReceiptItem.id == int(receipt_item_id))
    )


def _active_receipt_totals(
    db: Session, group_id: int
) -> tuple[int, int, int, int]:
    row = db.execute(
        select(
            func.coalesce(
                func.sum(CompositePhysicalGroupReceipt.received_sheet_quantity), 0
            ),
            func.coalesce(
                func.sum(
                    CompositePhysicalGroupReceipt.order_purpose_received_sheet_quantity
                ),
                0,
            ),
            func.coalesce(
                func.sum(CompositePhysicalGroupReceipt.reserve_received_sheet_quantity),
                0,
            ),
            func.coalesce(
                func.sum(
                    CompositePhysicalGroupReceipt.component_output_piece_quantity
                ),
                0,
            ),
        ).where(
            CompositePhysicalGroupReceipt.composite_physical_purchase_group_id
            == int(group_id),
            CompositePhysicalGroupReceipt.status == "posted",
        )
    ).one()
    return tuple(int(value or 0) for value in row)  # type: ignore[return-value]


def split_group_receipt_quantity(
    *,
    received_sheet_quantity: int,
    cumulative_order_purpose_before: int,
    order_purpose_sheet_quantity: int,
    reserve_sheet_quantity: int,
    surplus_disposition: str | None,
) -> tuple[int, int]:
    """Return the immutable order/reserve split for one group receipt.

    A group with an explicit reserve plan keeps that intent: sheets first fill
    the order-purpose target and every later sheet is raw-board reserve.  A
    group without such intent keeps the ordinary over-receipt contract and all
    valid sheets are cut into component inventory.
    """

    quantity = int(received_sheet_quantity)
    if quantity <= 0:
        raise IncomingReceiptError("入库数量必须大于0", 400)
    disposition = str(surplus_disposition or "").strip() or None
    remaining_order = max(
        int(order_purpose_sheet_quantity) - int(cumulative_order_purpose_before),
        0,
    )
    if int(reserve_sheet_quantity) > 0:
        if disposition == "finished":
            raise IncomingReceiptError(
                "该物理采购组已冻结备库用途，订单用途收足后的纸板必须进入片料备库。",
                409,
                code="COMPOSITE_GROUP_SURPLUS_DISPOSITION_INVALID",
            )
        if disposition not in {None, "semi_finished_reserve"}:
            raise IncomingReceiptError(
                "多收片料用途无效，请刷新后重试。",
                409,
                code="COMPOSITE_GROUP_SURPLUS_DISPOSITION_INVALID",
            )
        order_delta = min(quantity, remaining_order)
        return order_delta, quantity - order_delta
    if disposition == "semi_finished_reserve":
        raise IncomingReceiptError(
            "该物理采购组没有冻结备库用途，实收纸板必须全部分切为真实组件。",
            409,
            code="COMPOSITE_GROUP_SURPLUS_DISPOSITION_INVALID",
        )
    if disposition not in {None, "finished"}:
        raise IncomingReceiptError(
            "多收片料用途无效，请刷新后重试。",
            409,
            code="COMPOSITE_GROUP_SURPLUS_DISPOSITION_INVALID",
        )
    return quantity, 0


def stable_component_piece_allocations(
    *,
    available_piece_quantity: int,
    sources: Iterable[dict[str, int]],
) -> list[dict[str, int]]:
    """Allocate only each source's still-uncovered net physical-piece demand."""

    remaining_output = max(int(available_piece_quantity), 0)
    result: list[dict[str, int]] = []
    for source in sorted(
        sources, key=lambda row: (int(row["source_sequence"]), int(row["source_id"]))
    ):
        target = max(int(source["net_required_piece_quantity"]), 0)
        before = max(int(source["cumulative_reserved_piece_quantity"]), 0)
        if before > target:
            raise IncomingReceiptError(
                "物理采购组来源累计组件预占超过冻结净需求，已停止收料。",
                409,
                code="COMPOSITE_GROUP_ALLOCATION_INVALID",
            )
        allocated = min(max(target - before, 0), remaining_output)
        if allocated > 0:
            result.append(
                {
                    "source_id": int(source["source_id"]),
                    "source_sequence": int(source["source_sequence"]),
                    "net_required_piece_quantity": target,
                    "cumulative_reserved_piece_quantity_before": before,
                    "allocated_reserved_component_piece_quantity": allocated,
                    "cumulative_reserved_piece_quantity_after": before + allocated,
                }
            )
            remaining_output -= allocated
    return result


def _source_order_identities(
    db: Session,
    sources: Iterable[CompositePhysicalPurchaseGroupSource],
) -> dict[int, tuple[int, str]]:
    """Load every frozen source's real order identity in one query."""

    order_item_ids = sorted({int(source.order_item_id) for source in sources})
    if not order_item_ids:
        return {}
    rows = db.execute(
        select(OrderItem.id, Order.id, Order.order_number)
        .join(Order, Order.id == OrderItem.order_id)
        .where(OrderItem.id.in_(order_item_ids))
    ).all()
    identities = {
        int(order_item_id): (int(order_id), str(order_number))
        for order_item_id, order_id, order_number in rows
    }
    if len(identities) != len(order_item_ids):
        raise IncomingReceiptError("物理采购组来源订单关联缺失，已停止收料", 409)
    return identities


def _source_payload(
    source: CompositePhysicalPurchaseGroupSource,
    *,
    order_identity: tuple[int, str],
) -> dict[str, Any]:
    order_id, order_number = order_identity
    result = {
        "group_source_id": int(source.id),
        "source_key": source.source_key,
        "source_sequence": int(source.source_sequence),
        "component_type": source.component_type_snapshot,
        "requisition_item_id": int(source.requisition_item_id),
        "order_item_id": int(source.order_item_id),
        "order_id": int(order_id),
        "order_number": str(order_number),
        "sales_order_item_bom_component_id": int(
            source.sales_order_item_bom_component_id
        ),
        "purchase_purpose_source_snapshot_id": (
            int(source.purchase_purpose_source_snapshot_id)
            if source.purchase_purpose_source_snapshot_id is not None
            else None
        ),
        "required_piece_quantity": int(source.required_piece_quantity),
        "inventory_reserved_piece_quantity": int(
            source.inventory_reserved_piece_quantity
        ),
        "net_required_piece_quantity": int(source.net_required_piece_quantity),
        "allocated_order_purpose_sheet_quantity": int(
            source.allocated_order_purpose_sheet_quantity
        ),
        "spare_sheet_quantity": int(source.spare_sheet_quantity),
    }
    return result


def _source_payloads(
    db: Session,
    sources: Iterable[CompositePhysicalPurchaseGroupSource],
    *,
    order_identities: dict[int, tuple[int, str]] | None = None,
) -> list[dict[str, Any]]:
    ordered = list(sources)
    identities = (
        _source_order_identities(db, ordered)
        if order_identities is None
        else order_identities
    )
    return [
        _source_payload(
            source,
            order_identity=identities[int(source.order_item_id)],
        )
        for source in ordered
    ]


def _source_order_count(source_items: Iterable[dict[str, Any]]) -> int:
    return len(
        {
            int(source["order_id"])
            for source in source_items
            if source.get("order_id") is not None
        }
    )


def _group_pending_fields(
    db: Session,
    *,
    group: CompositePhysicalPurchaseGroup,
    sources: list[CompositePhysicalPurchaseGroupSource],
    source_order_identities: dict[int, tuple[int, str]] | None = None,
) -> dict[str, Any]:
    before_total, before_order, before_reserve, before_output = _active_receipt_totals(
        db, group.id
    )
    remaining = max(int(group.purchase_sheet_quantity) - before_total, 0)
    order_delta, reserve_delta = (
        split_group_receipt_quantity(
            received_sheet_quantity=remaining,
            cumulative_order_purpose_before=before_order,
            order_purpose_sheet_quantity=int(group.order_purpose_sheet_quantity),
            reserve_sheet_quantity=int(group.reserve_sheet_quantity),
            surplus_disposition=None,
        )
        if remaining > 0
        else (0, 0)
    )
    source_items = _source_payloads(
        db,
        sources,
        order_identities=source_order_identities,
    )
    result = {
        "item_id": composite_physical_group_route(group.id),
        "source_key": composite_physical_group_route(group.id),
        "composite_physical_purchase_group_id": int(group.id),
        "expected_group_version": int(group.version),
        "group_key": group.group_key,
        "source_count": int(group.source_count),
        "source_items": source_items,
        "source_order_count": _source_order_count(source_items),
        "planned_quantity": int(group.purchase_sheet_quantity),
        "requisition_qty": int(group.purchase_sheet_quantity),
        "cumulative_received_quantity": before_total,
        "remaining_quantity": remaining,
        "incoming_quantity": remaining,
        # These are the cg pending-row execution states, not mutations of any
        # source order.  A pure-reserve synthetic row remains actionable after
        # every source order has reached received/delivered.
        "material_status": "pending",
        "requisition_status": "已报料",
        "variance_quantity": before_total - int(group.purchase_sheet_quantity),
        "variance_type": (
            "matched"
            if before_total == int(group.purchase_sheet_quantity)
            else "short"
            if before_total < int(group.purchase_sheet_quantity)
            else "over"
        ),
        "group_status": group.status,
        "yield_per_sheet": int(group.yield_per_sheet),
        "net_required_piece_quantity": int(group.net_required_piece_quantity),
        "spare_sheet_quantity": int(group.spare_sheet_quantity),
        "order_purpose_sheet_quantity": int(group.order_purpose_sheet_quantity),
        "reserve_sheet_quantity": int(group.reserve_sheet_quantity),
        "expected_order_purpose_sheet_qty": order_delta,
        "expected_reserve_purpose_sheet_qty": reserve_delta,
        "remaining_order_purpose_sheet_qty": max(
            int(group.order_purpose_sheet_quantity) - before_order, 0
        ),
        "remaining_reserve_purpose_sheet_qty": max(
            int(group.reserve_sheet_quantity) - before_reserve, 0
        ),
        "expected_finished_output_qty": order_delta * int(group.yield_per_sheet),
        "cumulative_component_output_piece_quantity": before_output,
        "surplus_sheet_qty": reserve_delta,
        "surplus_choice_required": False,
        "finished_disposition_expected_order_purpose_sheet_qty": (
            remaining if int(group.reserve_sheet_quantity) == 0 else order_delta
        ),
        "finished_disposition_expected_reserve_purpose_sheet_qty": reserve_delta,
        "semi_finished_reserve_expected_order_purpose_sheet_qty": order_delta,
        "semi_finished_reserve_expected_reserve_purpose_sheet_qty": reserve_delta,
        "purpose_status": "frozen",
        "resolution_status": "not_required",
        "resolution_action": None,
        "pending_receipt_item_id": None,
    }
    result.update(_group_contract_fields(db, group=group, sources=sources))
    return result


def _synthetic_group_anchor_row(
    db: Session,
    *,
    group: CompositePhysicalPurchaseGroup,
    sources: list[CompositePhysicalPurchaseGroupSource],
) -> dict[str, Any]:
    anchor = _anchor_source(sources)
    item = db.get(OrderItem, anchor.order_item_id)
    order = db.get(Order, item.order_id) if item is not None else None
    customer = db.get(Customer, group.customer_id)
    product = db.get(Product, group.component_product_id)
    requisition_item = db.get(RequisitionItem, anchor.requisition_item_id)
    requisition = db.get(Requisition, group.requisition_id)
    return {
        "item_id": f"r{anchor.requisition_item_id}",
        "order_item_id": anchor.order_item_id,
        "order_id": order.id if order is not None else None,
        "requisition_item_id": anchor.requisition_item_id,
        "supplier_order_item_id": group.supplier_requisition_order_item_id,
        "customer_id": group.customer_id,
        "customer_name": customer.name if customer is not None else "",
        "customer_short_name": (
            customer.chinese_short_name if customer is not None else None
        ),
        "customer_code": customer.customer_code if customer is not None else None,
        "order_number": order.order_number if order is not None else None,
        "customer_po": order.customer_po if order is not None else None,
        "created_at": group.created_at,
        "delivery_date": order.delivery_date if order is not None else None,
        "product_id": product.id if product is not None else group.component_product_id,
        "product_code": product.product_code if product is not None else None,
        "product_name": product.product_name if product is not None else None,
        "specification": (
            requisition_item.specification_snapshot
            if requisition_item is not None
            else None
        ),
        "material": group.material_code_snapshot,
        "layer_count": group.layer_count_snapshot,
        "flute_type": group.flute_type_snapshot,
        "cardboard_len": group.report_length_mm,
        "cardboard_width": group.report_width_mm,
        "snapshot_crease_type": group.crease_type_snapshot,
        "snapshot_crease_left_mm": group.crease_left_mm,
        "snapshot_crease_middle_mm": group.crease_middle_mm,
        "snapshot_crease_right_mm": group.crease_right_mm,
        "snapshot_supplier_name": (
            requisition.supplier_name if requisition is not None else None
        ),
        "requisition_qty": group.purchase_sheet_quantity,
        "quantity": item.quantity if item is not None else 0,
    }


def _group_orders_remain_receivable(
    db: Session, sources: list[CompositePhysicalPurchaseGroupSource]
) -> bool:
    reserved_by_source = _current_source_reserved_quantities(
        db, [int(source.id) for source in sources]
    )
    receivable_order_item_ids = {
        int(source.order_item_id)
        for source in sources
        if int(reserved_by_source.get(int(source.id), 0))
        < int(source.net_required_piece_quantity)
    }
    # Remaining frozen spare/cutting sheets do not belong to an uncovered
    # order demand and must stay receivable after all sources are fulfilled.
    if not receivable_order_item_ids:
        return True
    items = {
        item.id: item
        for item in db.scalars(
            select(OrderItem).where(
                OrderItem.id.in_(receivable_order_item_ids)
            )
        ).all()
    }
    orders = {
        order.id: order
        for order in db.scalars(
            select(Order).where(
                Order.id.in_([item.order_id for item in items.values()])
            )
        ).all()
    }
    if len(items) != len(receivable_order_item_ids):
        return False
    for item in items.values():
        order = orders.get(item.order_id)
        if order is None:
            return False
        if order_item_forward_block_reason(
            order_status=order.status,
            ordered_quantity=item.quantity,
            delivered_quantity=item.delivered_quantity,
            is_force_closed=item.is_force_closed,
        ) is not None:
            return False
    return True


def coalesce_pending_group_rows(
    db: Session,
    rows: list[dict],
    *,
    visible_customer_ids: set[int] | None = None,
) -> list[dict]:
    """Replace all old per-source pending rows with one authoritative cg row."""

    group_query = (
        select(CompositePhysicalPurchaseGroup)
        .options(selectinload(CompositePhysicalPurchaseGroup.sources))
        .where(
            CompositePhysicalPurchaseGroup.status.in_(GROUP_RECEIVABLE_STATUSES)
        )
    )
    if visible_customer_ids is not None:
        if not visible_customer_ids:
            return rows
        group_query = group_query.where(
            CompositePhysicalPurchaseGroup.customer_id.in_(visible_customer_ids)
        )
    groups = list(
        db.scalars(
            group_query.order_by(
                CompositePhysicalPurchaseGroup.created_at.desc(),
                CompositePhysicalPurchaseGroup.id.desc(),
            )
        ).unique().all()
    )
    if not groups:
        return rows

    purpose_snapshot_ids = {
        int(source.purchase_purpose_source_snapshot_id)
        for group in groups
        for source in group.sources
        if source.purchase_purpose_source_snapshot_id is not None
    }
    purpose_snapshots: dict[int, PurchasePurposeSourceSnapshot] = {}
    if purpose_snapshot_ids:
        purpose_snapshots = {
            int(snapshot.id): snapshot
            for snapshot in db.scalars(
                select(PurchasePurposeSourceSnapshot).where(
                    PurchasePurposeSourceSnapshot.id.in_(purpose_snapshot_ids)
                )
            ).all()
        }

    group_by_requisition: dict[int, CompositePhysicalPurchaseGroup] = {}
    group_by_supplier: dict[int, CompositePhysicalPurchaseGroup] = {}
    pending_groups: dict[int, CompositePhysicalPurchaseGroup] = {}
    sources_by_group: dict[int, list[CompositePhysicalPurchaseGroupSource]] = {}
    for group in groups:
        sources = sorted(
            group.sources,
            key=lambda source: (int(source.source_sequence), int(source.id)),
        )
        before_total, before_order, _, _ = _active_receipt_totals(db, group.id)
        if (
            before_total >= int(group.purchase_sheet_quantity)
            or (
                before_order < int(group.order_purpose_sheet_quantity)
                and not _group_orders_remain_receivable(db, sources)
            )
        ):
            continue
        pending_groups[int(group.id)] = group
        sources_by_group[int(group.id)] = sources
        if group.supplier_requisition_order_item_id is not None:
            group_by_supplier[int(group.supplier_requisition_order_item_id)] = group
        for source in sources:
            group_by_requisition[int(source.requisition_item_id)] = group
            snapshot = purpose_snapshots.get(
                int(source.purchase_purpose_source_snapshot_id)
            )
            if (
                snapshot is not None
                and snapshot.supplier_requisition_order_item_id is not None
            ):
                group_by_supplier[
                    int(snapshot.supplier_requisition_order_item_id)
                ] = group

    if not pending_groups:
        return rows
    source_order_identities = _source_order_identities(
        db,
        [
            source
            for sources in sources_by_group.values()
            for source in sources
        ],
    )
    rows_by_group: dict[int, list[dict]] = {}
    group_for_row: dict[int, CompositePhysicalPurchaseGroup] = {}
    for row in rows:
        group = None
        if row.get("requisition_item_id") is not None:
            group = group_by_requisition.get(int(row["requisition_item_id"]))
        if group is None and row.get("supplier_order_item_id") is not None:
            group = group_by_supplier.get(int(row["supplier_order_item_id"]))
        if group is None or int(group.id) not in pending_groups:
            continue
        group_for_row[id(row)] = group
        rows_by_group.setdefault(int(group.id), []).append(row)

    result: list[dict] = []
    emitted: set[int] = set()
    for row in rows:
        group = group_for_row.get(id(row))
        if group is None:
            result.append(row)
            continue
        group_id = int(group.id)
        if group_id in emitted:
            continue
        candidates = rows_by_group[group_id]
        anchor_source = _anchor_source(sources_by_group[group_id])
        anchor = next(
            (
                candidate
                for candidate in candidates
                if candidate.get("requisition_item_id") is not None
                and int(candidate["requisition_item_id"])
                == int(anchor_source.requisition_item_id)
            ),
            candidates[0],
        )
        merged = dict(anchor)
        merged.update(
            _group_pending_fields(
                db,
                group=group,
                sources=sources_by_group[group_id],
                source_order_identities=source_order_identities,
            )
        )
        # The price endpoint and receive endpoint both address the group.  The
        # selected anchor fields expose the latest immutable price fact for the
        # next receipt batch; earlier batches keep their own fact snapshots.
        merged["source_key"] = composite_physical_group_route(group_id)
        result.append(merged)
        emitted.add(group_id)
    for group_id, group in pending_groups.items():
        if group_id in emitted:
            continue
        sources = sources_by_group[group_id]
        merged = _synthetic_group_anchor_row(db, group=group, sources=sources)
        merged.update(
            _group_pending_fields(
                db,
                group=group,
                sources=sources,
                source_order_identities=source_order_identities,
            )
        )
        result.append(merged)
    return result


def without_composite_group_source_history_rows(
    db: Session, rows: list[dict]
) -> list[dict]:
    """Remove legacy per-source duplicates of authoritative group receipts."""

    requisition_ids = {
        int(row["requisition_item_id"])
        for row in rows
        if row.get("requisition_item_id") is not None
    }
    supplier_ids = {
        int(row["supplier_order_item_id"])
        for row in rows
        if row.get("supplier_order_item_id") is not None
    }
    grouped_requisition_ids: set[int] = set()
    grouped_supplier_ids: set[int] = set()
    if requisition_ids:
        grouped_requisition_ids = {
            int(value)
            for value in db.scalars(
                select(CompositePhysicalPurchaseGroupSource.requisition_item_id).where(
                    CompositePhysicalPurchaseGroupSource.requisition_item_id.in_(
                        requisition_ids
                    )
                )
            ).all()
        }
    if supplier_ids:
        grouped_supplier_ids = {
            int(value)
            for value in db.scalars(
                select(
                    PurchasePurposeSourceSnapshot.supplier_requisition_order_item_id
                )
                .join(
                    CompositePhysicalPurchaseGroupSource,
                    CompositePhysicalPurchaseGroupSource.purchase_purpose_source_snapshot_id
                    == PurchasePurposeSourceSnapshot.id,
                )
                .where(
                    PurchasePurposeSourceSnapshot.supplier_requisition_order_item_id.in_(
                        supplier_ids
                    )
                )
            ).all()
            if value is not None
        }
    return [
        row
        for row in rows
        if (
            row.get("requisition_item_id") is None
            or int(row["requisition_item_id"]) not in grouped_requisition_ids
        )
        and (
            row.get("supplier_order_item_id") is None
            or int(row["supplier_order_item_id"]) not in grouped_supplier_ids
        )
    ]


def _receipt_request_hash(
    *,
    group_id: int,
    received_quantity: int | None,
    surplus_disposition: str | None,
    expected_group_version: int | None,
    expected_receipt_fact_version: int | None,
    purchase_purpose_source_snapshot_id: int | None,
    expected_purpose_snapshot_version: int | None,
    receipt_plan_fingerprint: str | None,
    expected_actual_material_version: int | None,
    actual_material_fingerprint: str | None,
) -> str:
    return canonical_purchase_receipt_hash(
        {
            "group_id": int(group_id),
            "received_quantity": received_quantity,
            "surplus_disposition": (
                str(surplus_disposition or "").strip() or None
            ),
            "expected_group_version": expected_group_version,
            "expected_receipt_fact_version": expected_receipt_fact_version,
            "purchase_purpose_source_snapshot_id": (
                purchase_purpose_source_snapshot_id
            ),
            "expected_purpose_snapshot_version": expected_purpose_snapshot_version,
            "receipt_plan_fingerprint": receipt_plan_fingerprint,
            "expected_actual_material_version": expected_actual_material_version,
            "actual_material_fingerprint": actual_material_fingerprint,
        }
    )


def _idempotent_group_receipt(
    db: Session,
    *,
    group_id: int,
    idempotency_key: str,
    request_hash: str,
    actor_id: int,
) -> IncomingReceiptItem | None:
    existing = db.scalar(
        select(CompositePhysicalGroupReceipt).where(
            CompositePhysicalGroupReceipt.idempotency_key == idempotency_key
        )
    )
    if existing is None:
        collision = db.scalar(
            select(IncomingReceipt.id).where(
                IncomingReceipt.idempotency_key == idempotency_key
            )
        )
        if collision is not None:
            raise IncomingReceiptError(
                "该收料幂等键已用于其他收料业务对象。",
                409,
                code="COMPOSITE_GROUP_RECEIPT_IDEMPOTENCY_CONFLICT",
            )
        return None
    if (
        int(existing.composite_physical_purchase_group_id) != int(group_id)
        or int(existing.created_by) != int(actor_id)
        or not hmac.compare_digest(str(existing.request_hash), str(request_hash))
    ):
        raise IncomingReceiptError(
            "同一物理组收料幂等键不能由不同操作者、业务对象或载荷重放。",
            409,
            code="COMPOSITE_GROUP_RECEIPT_IDEMPOTENCY_CONFLICT",
        )
    item = db.get(IncomingReceiptItem, existing.incoming_receipt_item_id)
    if item is None:
        raise IncomingReceiptError("组收料幂等事实关联不完整，已停止重放。", 409)
    return item


def _reserve_component_piece_inventory(
    db: Session,
    *,
    lot: InventoryLot,
    source: CompositePhysicalPurchaseGroupSource,
    order: Order,
    quantity: int,
    operator_id: int,
    idempotency_key: str,
) -> InventoryReservation:
    if quantity <= 0:
        raise IncomingReceiptError("组收料组件预占数量必须大于0", 409)
    before = _balances(lot)
    now = utc_now_naive()
    result = db.execute(
        update(InventoryLot)
        .where(
            InventoryLot.id == lot.id,
            InventoryLot.version == lot.version,
            InventoryLot.inventory_type == "finished",
            InventoryLot.status == "active",
            InventoryLot.quantity_available >= quantity,
        )
        .values(
            quantity_available=InventoryLot.quantity_available - quantity,
            quantity_reserved=InventoryLot.quantity_reserved + quantity,
            version=InventoryLot.version + 1,
            last_movement_at=now,
        )
    )
    if result.rowcount != 1:
        raise IncomingReceiptError(
            "组收料组件库存数量或版本已变化，请刷新后重试。",
            409,
            code="COMPOSITE_GROUP_COMPONENT_RESERVATION_CONFLICT",
        )
    reservation = InventoryReservation(
        reservation_number=_warehouse_number("CGRS"),
        inventory_lot_id=lot.id,
        reservation_type="finished_order",
        order_id=order.id,
        order_item_id=source.order_item_id,
        sales_order_item_bom_component_id=source.sales_order_item_bom_component_id,
        reserved_stock_quantity=quantity,
        credited_requirement_quantity=quantity,
        yield_factor=1,
        status="active",
        warning_codes="[]",
        reserved_by=operator_id,
        reserved_at=now,
        idempotency_key=idempotency_key,
    )
    db.add(reservation)
    db.flush()
    db.expire(lot)
    refreshed = db.get(InventoryLot, lot.id)
    if refreshed is None:
        raise IncomingReceiptError("组收料组件库存批次不存在", 409)
    _movement(
        db,
        lot=refreshed,
        movement_type="reserve",
        quantity=quantity,
        before=before,
        operator_id=operator_id,
        reason="物理组收料按冻结来源预占真实组件",
        idempotency_key=idempotency_key,
        reservation_id=reservation.id,
        related_order_id=order.id,
        related_order_item_id=source.order_item_id,
    )
    db.flush()
    return reservation


def _current_source_reserved_quantities(
    db: Session, source_ids: list[int]
) -> dict[int, int]:
    if not source_ids:
        return {}
    return {
        int(source_id): int(quantity or 0)
        for source_id, quantity in db.execute(
            select(
                CompositePhysicalGroupReceiptSourceAllocation.composite_physical_purchase_group_source_id,
                func.coalesce(
                    func.sum(
                        CompositePhysicalGroupReceiptSourceAllocation.allocated_reserved_component_piece_quantity
                    ),
                    0,
                ),
            )
            .where(
                CompositePhysicalGroupReceiptSourceAllocation.composite_physical_purchase_group_source_id.in_(
                    source_ids
                ),
                CompositePhysicalGroupReceiptSourceAllocation.status == "active",
            )
            .group_by(
                CompositePhysicalGroupReceiptSourceAllocation.composite_physical_purchase_group_source_id
            )
        ).all()
    }


def _sync_group_source_progress(
    db: Session,
    *,
    sources: list[CompositePhysicalPurchaseGroupSource],
    operator_id: int,
) -> None:
    now = utc_now_naive()
    reserved_by_source = _current_source_reserved_quantities(
        db, [source.id for source in sources]
    )
    requisition_items = {
        row.id: row
        for row in db.scalars(
            select(RequisitionItem).where(
                RequisitionItem.id.in_([source.requisition_item_id for source in sources])
            )
        ).all()
    }
    for source in sources:
        row = requisition_items.get(source.requisition_item_id)
        if row is None:
            raise IncomingReceiptError("物理采购组来源报料明细不存在", 409)
        source_covered = int(reserved_by_source.get(source.id, 0)) >= int(
            source.net_required_piece_quantity
        )
        row.status = (
            "已入库" if source_covered else _open_requisition_status(db, row)
        )

    order_item_ids = sorted({int(source.order_item_id) for source in sources})
    all_group_sources = db.scalars(
        select(CompositePhysicalPurchaseGroupSource)
        .join(
            CompositePhysicalPurchaseGroup,
            CompositePhysicalPurchaseGroup.id
            == CompositePhysicalPurchaseGroupSource.composite_physical_purchase_group_id,
        )
        .where(
            CompositePhysicalPurchaseGroupSource.order_item_id.in_(order_item_ids),
            CompositePhysicalPurchaseGroup.status != "voided",
        )
        .order_by(
            CompositePhysicalPurchaseGroupSource.order_item_id,
            CompositePhysicalPurchaseGroupSource.id,
        )
    ).all()
    all_reserved_by_source = _current_source_reserved_quantities(
        db, [source.id for source in all_group_sources]
    )
    group_sources_by_order_item: dict[
        int, list[CompositePhysicalPurchaseGroupSource]
    ] = {}
    for source in all_group_sources:
        group_sources_by_order_item.setdefault(int(source.order_item_id), []).append(
            source
        )
    for item_id in order_item_ids:
        item = db.get(OrderItem, item_id)
        if item is None:
            raise IncomingReceiptError("物理采购组来源订单明细不存在", 409)
        sibling_group_sources = group_sources_by_order_item.get(item_id, [])
        group_sources_closed = bool(sibling_group_sources) and all(
            int(all_reserved_by_source.get(source.id, 0))
            >= int(source.net_required_piece_quantity)
            for source in sibling_group_sources
        )
        closed = (
            _all_expected_bom_sources_received(db, item) is True
            and group_sources_closed
        )
        if closed:
            item.material_status = "received"
            item.requisition_status = "已入库"
            item.material_received_at = now
            item.material_received_by = operator_id
        else:
            item.material_status = "pending"
            if item.requisition_status == "已入库":
                item.requisition_status = "已报料"
            item.material_received_at = None
            item.material_received_by = None
        try:
            refresh_existing_production_task(db, item.id)
        except ProductionWorkflowError as error:
            raise IncomingReceiptError(str(error), error.status_code) from error
        refresh_order_production_status(db, item.order_id)
    db.flush()


def receive_composite_physical_group(
    db: Session,
    *,
    user: User,
    item_key: int | str,
    received_quantity: int | None,
    surplus_disposition: str | None,
    expected_group_version: int | None,
    expected_receipt_fact_version: int | None,
    purchase_purpose_source_snapshot_id: int | None,
    expected_purpose_snapshot_version: int | None,
    receipt_plan_fingerprint: str | None,
    expected_actual_material_version: int | None,
    actual_material_fingerprint: str | None,
    idempotency_key: str | None,
    audit_context: dict[str, object] | None = None,
) -> IncomingReceiptItem:
    group_id = composite_physical_group_id(item_key)
    if group_id is None:
        raise IncomingReceiptError("物理采购组收料ID无效", 400)
    key = str(idempotency_key or "").strip()
    if not key:
        raise IncomingReceiptError(
            "物理采购组收料必须提交非空幂等键。",
            409,
            code="INCOMING_IDEMPOTENCY_KEY_REQUIRED",
        )
    request_hash = _receipt_request_hash(
        group_id=group_id,
        received_quantity=received_quantity,
        surplus_disposition=surplus_disposition,
        expected_group_version=expected_group_version,
        expected_receipt_fact_version=expected_receipt_fact_version,
        purchase_purpose_source_snapshot_id=purchase_purpose_source_snapshot_id,
        expected_purpose_snapshot_version=expected_purpose_snapshot_version,
        receipt_plan_fingerprint=receipt_plan_fingerprint,
        expected_actual_material_version=expected_actual_material_version,
        actual_material_fingerprint=actual_material_fingerprint,
    )
    replay = _idempotent_group_receipt(
        db,
        group_id=group_id,
        idempotency_key=key,
        request_hash=request_hash,
        actor_id=user.id,
    )
    if replay is not None:
        return replay

    group = db.scalar(
        select(CompositePhysicalPurchaseGroup)
        .where(CompositePhysicalPurchaseGroup.id == group_id)
        .with_for_update()
    )
    if group is None:
        raise IncomingReceiptError("物理采购组不存在", 404)
    replay = _idempotent_group_receipt(
        db,
        group_id=group_id,
        idempotency_key=key,
        request_hash=request_hash,
        actor_id=user.id,
    )
    if replay is not None:
        return replay
    if group.status not in GROUP_RECEIVABLE_STATUSES:
        raise IncomingReceiptError("该物理采购组已作废，不能继续收料", 409)
    if expected_group_version is None or int(expected_group_version) != int(group.version):
        raise IncomingReceiptError(
            "物理采购组版本已变化，请刷新后重试。",
            409,
            code="COMPOSITE_GROUP_VERSION_STALE",
        )

    sources = _source_rows(db, group.id)
    before_total, before_order, before_reserve, before_output = (
        _active_receipt_totals(db, group.id)
    )
    remaining_plan = max(int(group.purchase_sheet_quantity) - before_total, 0)
    quantity = int(remaining_plan if received_quantity is None else received_quantity)
    if quantity <= 0:
        raise IncomingReceiptError("入库数量必须大于0", 400)
    order_delta, reserve_delta = split_group_receipt_quantity(
        received_sheet_quantity=quantity,
        cumulative_order_purpose_before=before_order,
        order_purpose_sheet_quantity=int(group.order_purpose_sheet_quantity),
        reserve_sheet_quantity=int(group.reserve_sheet_quantity),
        surplus_disposition=surplus_disposition,
    )
    order_items = {
        item.id: item
        for item in db.scalars(
            select(OrderItem).where(
                OrderItem.id.in_([source.order_item_id for source in sources])
            )
        ).all()
    }
    order_ids = sorted({int(item.order_id) for item in order_items.values()})
    try:
        locked_orders = lock_order_rows_for_production_transition(db, order_ids)
    except ProductionWorkflowError as error:
        raise IncomingReceiptError(str(error), error.status_code) from error
    reserved_by_source = _current_source_reserved_quantities(
        db, [int(source.id) for source in sources]
    )
    for source in sources:
        item = order_items.get(source.order_item_id)
        order = locked_orders.get(item.order_id) if item is not None else None
        if item is None or order is None:
            raise IncomingReceiptError("物理采购组来源订单不存在", 409)
        source_still_needs_output = int(
            reserved_by_source.get(int(source.id), 0)
        ) < int(source.net_required_piece_quantity)
        if order_delta > 0 and source_still_needs_output:
            block = order_item_forward_block_reason(
                order_status=order.status,
                ordered_quantity=item.quantity,
                delivered_quantity=item.delivered_quantity,
                is_force_closed=item.is_force_closed,
            )
            if block is not None:
                raise IncomingReceiptError(
                    order_item_forward_block_message(
                        block, action="物理组收料", order_status=order.status
                    ),
                    409,
                    code="ORDER_ITEM_RECEIPT_BLOCKED",
                )

    anchor, contract_source, contract_snapshot = _receipt_contract_anchor(
        db, group=group, sources=sources
    )
    if (
        purchase_purpose_source_snapshot_id is None
        or int(purchase_purpose_source_snapshot_id)
        != int(contract_snapshot.id)
    ):
        raise IncomingReceiptError(
            "物理采购组价格锚点已变化，请刷新后重试。",
            409,
            code="COMPOSITE_GROUP_PRICE_ANCHOR_STALE",
        )
    try:
        purpose_context = resolve_receipt_purpose_context(
            db,
            target=SimpleNamespace(
                supplier_order_item=(
                    contract_source
                    if isinstance(contract_source, SupplierRequisitionOrderItem)
                    else None
                ),
                requisition_item=(
                    contract_source
                    if isinstance(contract_source, RequisitionItem)
                    else None
                ),
            ),
            expected_receipt_fact_version=expected_receipt_fact_version,
            purchase_purpose_source_snapshot_id=purchase_purpose_source_snapshot_id,
            expected_purpose_snapshot_version=expected_purpose_snapshot_version,
            receipt_plan_fingerprint=receipt_plan_fingerprint,
            expected_actual_material_version=expected_actual_material_version,
            actual_material_fingerprint=actual_material_fingerprint,
        )
    except ReceiptPurposeFlowError as error:
        raise IncomingReceiptError(
            str(error), error.status_code, code=error.code
        ) from error
    if purpose_context is None:
        raise IncomingReceiptError(
            "物理采购组缺少冻结采购用途和实际价格事实。",
            409,
            code="PURCHASE_RECEIPT_FACT_REQUIRED",
        )
    fact: PurchaseReceiptFact = purpose_context.receipt_fact

    after_total = before_total + quantity
    after_order = before_order + order_delta
    after_reserve = before_reserve + reserve_delta
    component_output = order_delta * int(group.yield_per_sheet)
    after_output = before_output + component_output
    sheet_cost = _money(
        calculate_per_sheet_cost(
            unit_price=fact.unit_price,
            price_unit=fact.price_unit,
            tax_included=fact.tax_included,
            tax_rate=fact.tax_rate,
            report_length_mm=group.report_length_mm,
            report_width_mm=group.report_width_mm,
        )
    )
    order_cost = _money(sheet_cost * Decimal(order_delta))
    reserve_cost = _money(sheet_cost * Decimal(reserve_delta))
    total_cost = _money(sheet_cost * Decimal(quantity))
    component_unit_cost = (
        _money(order_cost / Decimal(component_output))
        if component_output > 0
        else None
    )

    now = utc_now_naive()
    variance = after_total - int(group.purchase_sheet_quantity)
    receipt = IncomingReceipt(
        receipt_number=_incoming_number("IR"),
        status="posted",
        received_at=now,
        received_by=user.id,
        idempotency_key=key,
        remarks=None,
    )
    receipt_item = IncomingReceiptItem(
        order_id=None,
        order_item_id=None,
        requisition_id=None,
        requisition_item_id=None,
        supplier_order_id=None,
        supplier_order_item_id=None,
        stock_replenishment_item_id=None,
        composite_physical_purchase_group_id=group.id,
        planned_quantity=int(group.purchase_sheet_quantity),
        received_quantity=quantity,
        cumulative_received_quantity=after_total,
        variance_quantity=variance,
        variance_type=(
            "matched" if variance == 0 else "short" if variance < 0 else "over"
        ),
        resolution_status="not_required",
        resolution_action=(
            "transfer_to_semi_inventory"
            if reserve_delta > 0
            else "all_to_production"
            if after_total > int(group.purchase_sheet_quantity)
            else None
        ),
        status="posted",
    )
    receipt.items.append(receipt_item)
    db.add(receipt)
    db.flush()

    component_lot: InventoryLot | None = None
    reserve_lot: InventoryLot | None = None
    try:
        if component_output > 0:
            projection = receipt_auto_finished_location_projection(
                db, customer_id=group.customer_id
            )
            if not bool(projection.get("ready")) or projection.get("location_id") is None:
                raise IncomingReceiptError(
                    str(projection.get("issue") or "真实组件待送库位不可用"),
                    409,
                    code="COMPOSITE_GROUP_COMPONENT_LOCATION_UNAVAILABLE",
                )
            component_lot = manual_finished_in(
                db,
                customer_id=group.customer_id,
                product_id=group.component_product_id,
                location_id=int(projection["location_id"]),
                quantity=component_output,
                stock_date=beijing_today(),
                source_type="purchase_surplus",
                remarks="物理采购组收料自动形成真实组件",
                operator_id=user.id,
                idempotency_key=f"cpgr-component:{key}",
                source_ref_type="incoming_receipt_item",
                source_ref_id=receipt_item.id,
                movement_reason="物理采购组收料形成真实组件",
                expected_layout_version=(
                    int(projection["layout_version"])
                    if projection.get("layout_version") is not None
                    else None
                ),
            )
            if component_lot.finished_detail is not None:
                component_lot.finished_detail.material_code_snapshot = (
                    fact.actual_material_code_snapshot
                )
                component_lot.finished_detail.flute_type_snapshot = (
                    fact.actual_material_flute_type_snapshot
                    or group.flute_type_snapshot
                )
        if reserve_delta > 0:
            location = automatic_raw_material_staging_location(
                db,
                repair_operator_id=user.id,
                repair_idempotency_key=f"cpgr-staging:{key}",
                require_floor3_left=True,
            )
            requisition = db.get(Requisition, group.requisition_id)
            reserve_lot = manual_semi_finished_in(
                db,
                location_id=location.id,
                quantity=reserve_delta,
                stock_date=beijing_today(),
                source_type="purchase_reserve",
                material_code=fact.actual_material_code_snapshot,
                layer_count=int(
                    fact.actual_material_layer_count_snapshot
                    or group.layer_count_snapshot
                ),
                flute_type=(
                    fact.actual_material_flute_type_snapshot
                    or group.flute_type_snapshot
                ),
                board_length_mm=int(group.report_length_mm),
                board_width_mm=int(group.report_width_mm),
                sheet_type=group.sheet_type_snapshot,
                component_type="whole",
                pieces_per_box=1,
                stock_yield_per_sheet=int(group.yield_per_sheet),
                supplier_name=(
                    str(requisition.supplier_name or "").strip() or None
                    if requisition is not None
                    else None
                ),
                customer_id=group.customer_id,
                crease_type=group.crease_type_snapshot,
                crease_left_mm=group.crease_left_mm,
                crease_middle_mm=group.crease_middle_mm,
                crease_right_mm=group.crease_right_mm,
                cutting_note=group.cutting_mode_snapshot,
                remarks="物理采购组收料按冻结用途进入客户备库",
                operator_id=user.id,
                idempotency_key=f"cpgr-reserve:{key}",
                source_ref_type="incoming_receipt_item",
                source_ref_id=receipt_item.id,
                material_id=fact.actual_material_id,
                movement_reason="物理采购组收料进入客户备库",
                allow_raw_material_staging=True,
                expected_layout_version=(
                    int(location.floor3_layout.version)
                    if location.floor3_layout is not None
                    else None
                ),
                customer_generic_eligible=True,
                internal_name=(
                    f"{group.material_code_snapshot} "
                    f"{group.report_length_mm}x{group.report_width_mm} 组备库"
                ),
            )
    except WarehouseInventoryError as error:
        raise IncomingReceiptError(str(error), error.status_code) from error

    receipt_sequence = int(
        db.scalar(
            select(
                func.coalesce(
                    func.max(CompositePhysicalGroupReceipt.receipt_sequence), 0
                )
            ).where(
                CompositePhysicalGroupReceipt.composite_physical_purchase_group_id
                == group.id
            )
        )
        or 0
    ) + 1
    group_receipt = CompositePhysicalGroupReceipt(
        composite_physical_purchase_group_id=group.id,
        incoming_receipt_item_id=receipt_item.id,
        receipt_sequence=receipt_sequence,
        purchase_receipt_fact_id=fact.id,
        component_inventory_lot_id=(component_lot.id if component_lot else None),
        reserve_inventory_lot_id=(reserve_lot.id if reserve_lot else None),
        group_version_snapshot=int(group.version),
        received_sheet_quantity=quantity,
        order_purpose_received_sheet_quantity=order_delta,
        reserve_received_sheet_quantity=reserve_delta,
        cumulative_received_sheet_quantity_before=before_total,
        cumulative_received_sheet_quantity_after=after_total,
        cumulative_order_purpose_sheet_quantity_before=before_order,
        cumulative_order_purpose_sheet_quantity_after=after_order,
        cumulative_reserve_sheet_quantity_before=before_reserve,
        cumulative_reserve_sheet_quantity_after=after_reserve,
        yield_per_sheet_snapshot=int(group.yield_per_sheet),
        component_output_piece_quantity=component_output,
        cumulative_component_output_piece_quantity_before=before_output,
        cumulative_component_output_piece_quantity_after=after_output,
        actual_unit_price_per_sheet=sheet_cost,
        component_unit_material_cost=component_unit_cost,
        order_purpose_material_cost=order_cost,
        reserve_material_cost=reserve_cost,
        total_material_cost=total_cost,
        currency_snapshot=fact.currency,
        status="posted",
        version=1,
        idempotency_key=key,
        request_hash=request_hash,
        created_by=user.id,
    )
    db.add(group_receipt)
    db.flush()

    detail_common = {
        "purchase_receipt_fact_id": int(fact.id),
        "composite_physical_group_receipt_id": int(group_receipt.id),
    }
    if component_lot is not None:
        component_lot.source_ref_type = GROUP_LOT_REF_TYPE
        component_lot.source_ref_id = group_receipt.id
        component_lot.estimated_unit_cost_snapshot = component_unit_cost
        component_lot.estimated_square_price_snapshot = None
        component_lot.estimated_cost_area_m2_snapshot = None
        component_lot.cost_snapshot_source = "purchase_receipt_actual"
        component_lot.cost_snapshot_detail_json = json.dumps(
            {
                **detail_common,
                "composite_inventory_kind": "component_piece",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        component_lot.cost_snapshot_at = now
        receipt_item.received_inventory_lot_id = component_lot.id
    if reserve_lot is not None:
        reserve_lot.source_ref_type = GROUP_LOT_REF_TYPE
        reserve_lot.source_ref_id = group_receipt.id
        reserve_lot.estimated_unit_cost_snapshot = sheet_cost
        reserve_lot.estimated_square_price_snapshot = None
        reserve_lot.estimated_cost_area_m2_snapshot = None
        reserve_lot.cost_snapshot_source = "purchase_receipt_actual"
        reserve_lot.cost_snapshot_detail_json = json.dumps(
            {
                **detail_common,
                "composite_inventory_kind": "reserve_sheet",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        reserve_lot.cost_snapshot_at = now
        receipt_item.surplus_inventory_lot_id = reserve_lot.id
    db.flush()

    current_by_source = _current_source_reserved_quantities(
        db, [source.id for source in sources]
    )
    allocations = stable_component_piece_allocations(
        available_piece_quantity=component_output,
        sources=[
            {
                "source_id": int(source.id),
                "source_sequence": int(source.source_sequence),
                "net_required_piece_quantity": int(source.net_required_piece_quantity),
                "cumulative_reserved_piece_quantity": int(
                    current_by_source.get(source.id, 0)
                ),
            }
            for source in sources
        ],
    )
    source_by_id = {int(source.id): source for source in sources}
    if allocations and component_lot is None:
        raise IncomingReceiptError("组收料组件预占缺少真实组件库存", 409)
    for allocation_sequence, allocation_data in enumerate(allocations, start=1):
        source = source_by_id[allocation_data["source_id"]]
        item = order_items.get(source.order_item_id)
        order = locked_orders.get(item.order_id) if item is not None else None
        if item is None or order is None or component_lot is None:
            raise IncomingReceiptError("组收料来源订单或组件库存不存在", 409)
        reservation_key = (
            f"cgr:{group_receipt.id}:{source.id}:"
            f"{hashlib.sha256(key.encode('utf-8')).hexdigest()[:12]}"
        )
        reservation = _reserve_component_piece_inventory(
            db,
            lot=component_lot,
            source=source,
            order=order,
            quantity=allocation_data[
                "allocated_reserved_component_piece_quantity"
            ],
            operator_id=user.id,
            idempotency_key=reservation_key,
        )
        allocation_hash = canonical_purchase_receipt_hash(
            {
                "group_receipt_id": int(group_receipt.id),
                **allocation_data,
                "inventory_reservation_id": int(reservation.id),
            }
        )
        db.add(
            CompositePhysicalGroupReceiptSourceAllocation(
                composite_physical_group_receipt_id=group_receipt.id,
                composite_physical_purchase_group_source_id=source.id,
                allocation_sequence=allocation_sequence,
                order_id=order.id,
                order_item_id=item.id,
                sales_order_item_bom_component_id=(
                    source.sales_order_item_bom_component_id
                ),
                inventory_reservation_id=reservation.id,
                source_net_required_piece_quantity_snapshot=int(
                    source.net_required_piece_quantity
                ),
                allocated_reserved_component_piece_quantity=allocation_data[
                    "allocated_reserved_component_piece_quantity"
                ],
                source_cumulative_reserved_piece_quantity_before=allocation_data[
                    "cumulative_reserved_piece_quantity_before"
                ],
                source_cumulative_reserved_piece_quantity_after=allocation_data[
                    "cumulative_reserved_piece_quantity_after"
                ],
                status="active",
                version=1,
                idempotency_key=f"cpgr-allocation:{group_receipt.id}:{source.id}",
                request_hash=allocation_hash,
                created_by=user.id,
            )
        )
    db.flush()

    next_status = (
        "received"
        if after_total >= int(group.purchase_sheet_quantity)
        else "partially_received"
    )
    result = db.execute(
        update(CompositePhysicalPurchaseGroup)
        .where(
            CompositePhysicalPurchaseGroup.id == group.id,
            CompositePhysicalPurchaseGroup.version == int(expected_group_version),
        )
        .values(
            status=next_status,
            version=CompositePhysicalPurchaseGroup.version + 1,
            updated_by=user.id,
            updated_at=now,
        )
    )
    if result.rowcount != 1:
        raise IncomingReceiptError(
            "物理采购组版本已变化，请刷新后重试。",
            409,
            code="COMPOSITE_GROUP_VERSION_STALE",
        )
    db.expire(group)
    group = db.get(CompositePhysicalPurchaseGroup, group.id)
    if group is None:
        raise IncomingReceiptError("物理采购组不存在", 409)
    if allocations:
        _sync_group_source_progress(
            db,
            sources=sources,
            operator_id=user.id,
        )
    context = audit_context or {}
    customer = db.get(Customer, group.customer_id)
    append_audit_event(
        db,
        request=context.get("request"),
        actor=user,
        event_category="business",
        result="success",
        source=str(context.get("source") or "web"),
        module_code="incoming",
        action_code="incoming.composite_group_receive",
        legacy_action="RECEIVE_COMPOSITE_GROUP",
        resource="CompositePhysicalGroupReceipt",
        entity_type="composite_physical_group_receipt",
        entity_id=group_receipt.id,
        object_ref=f"composite-group-receipt:{group_receipt.id}",
        customer_id=group.customer_id,
        customer_name=customer.name if customer is not None else None,
        batch_id=(
            str(context["batch_id"])
            if context.get("batch_id") is not None
            else None
        ),
        description="物理采购组来料实收",
        details={
            "group_id": group.id,
            "incoming_receipt_item_id": receipt_item.id,
            "received_sheet_quantity": quantity,
            "order_purpose_received_sheet_quantity": order_delta,
            "reserve_received_sheet_quantity": reserve_delta,
            "component_output_piece_quantity": component_output,
            "reserved_component_piece_quantity": sum(
                row["allocated_reserved_component_piece_quantity"]
                for row in allocations
            ),
            "purchase_receipt_fact_id": fact.id,
        },
    )
    db.flush()
    return receipt_item


def _receipt_allocations(
    db: Session, receipt_id: int
) -> list[CompositePhysicalGroupReceiptSourceAllocation]:
    return list(
        db.scalars(
            select(CompositePhysicalGroupReceiptSourceAllocation)
            .where(
                CompositePhysicalGroupReceiptSourceAllocation.composite_physical_group_receipt_id
                == int(receipt_id)
            )
            .order_by(
                CompositePhysicalGroupReceiptSourceAllocation.allocation_sequence,
                CompositePhysicalGroupReceiptSourceAllocation.id,
            )
        ).all()
    )


def _lot_transferred(db: Session, lot_id: int) -> bool:
    return (
        db.scalar(
            select(InventoryLotTransfer.id)
            .where(
                or_(
                    InventoryLotTransfer.source_lot_id == int(lot_id),
                    InventoryLotTransfer.target_lot_id == int(lot_id),
                )
            )
            .limit(1)
        )
        is not None
    )


def _assert_pristine_component_lot(
    db: Session,
    *,
    receipt: CompositePhysicalGroupReceipt,
    allocations: list[CompositePhysicalGroupReceiptSourceAllocation],
) -> InventoryLot | None:
    if receipt.component_inventory_lot_id is None:
        if allocations or int(receipt.component_output_piece_quantity) != 0:
            raise IncomingReceiptError("组收料组件库存追溯不完整，禁止撤销", 409)
        return None
    lot = db.get(InventoryLot, receipt.component_inventory_lot_id)
    expected_reservation_ids = {int(row.inventory_reservation_id) for row in allocations}
    reservations = list(
        db.scalars(
            select(InventoryReservation).where(
                InventoryReservation.inventory_lot_id == int(receipt.component_inventory_lot_id)
            )
        ).all()
    )
    if lot is None or lot.source_ref_type != GROUP_LOT_REF_TYPE or lot.source_ref_id != receipt.id:
        raise IncomingReceiptError("组收料组件库存追溯不完整，禁止撤销", 409)
    if {int(row.id) for row in reservations} != expected_reservation_ids:
        raise IncomingReceiptError("本次组件余片已被后续预占，不能撤销收料", 409)
    reserved_total = 0
    for allocation in allocations:
        reservation = next(
            (row for row in reservations if row.id == allocation.inventory_reservation_id),
            None,
        )
        expected = int(allocation.allocated_reserved_component_piece_quantity)
        if (
            allocation.status != "active"
            or reservation is None
            or reservation.status != "active"
            or int(reservation.reserved_stock_quantity) != expected
            or int(reservation.credited_requirement_quantity or 0) != expected
            or int(reservation.consumed_stock_quantity or 0) != 0
            or int(reservation.released_stock_quantity or 0) != 0
            or int(reservation.consumed_requirement_quantity or 0) != 0
            or int(reservation.released_requirement_quantity or 0) != 0
            or reservation.order_id != allocation.order_id
            or reservation.order_item_id != allocation.order_item_id
            or reservation.sales_order_item_bom_component_id
            != allocation.sales_order_item_bom_component_id
        ):
            raise IncomingReceiptError("组来源组件预占已消耗或变化，不能撤销收料", 409)
        reserved_total += expected
    movements = list(
        db.scalars(
            select(InventoryMovement)
            .where(InventoryMovement.inventory_lot_id == lot.id)
            .order_by(InventoryMovement.id)
        ).all()
    )
    reserve_movement_ids = {
        int(row.reservation_id)
        for row in movements
        if row.movement_type == "reserve" and row.reservation_id is not None
    }
    if (
        lot.status != "active"
        or lot.inventory_type != "finished"
        or int(lot.quantity_available) + int(lot.quantity_reserved)
        != int(receipt.component_output_piece_quantity)
        or int(lot.quantity_reserved) != reserved_total
        or int(lot.quantity_consumed) != 0
        or int(lot.quantity_damaged) != 0
        or int(lot.quantity_scrapped) != 0
        or len([row for row in movements if row.movement_type == "manual_in"]) != 1
        or len(movements) != 1 + len(allocations)
        or reserve_movement_ids != expected_reservation_ids
        or _lot_transferred(db, lot.id)
    ):
        raise IncomingReceiptError(
            "本次组件库存或余片已被移动、调整、消耗或处理，不能撤销收料",
            409,
        )
    return lot


def _assert_pristine_reserve_lot(
    db: Session, receipt: CompositePhysicalGroupReceipt
) -> InventoryLot | None:
    if receipt.reserve_inventory_lot_id is None:
        if int(receipt.reserve_received_sheet_quantity) != 0:
            raise IncomingReceiptError("组收料备库追溯不完整，禁止撤销", 409)
        return None
    lot = db.get(InventoryLot, receipt.reserve_inventory_lot_id)
    movements = (
        list(
            db.scalars(
                select(InventoryMovement).where(
                    InventoryMovement.inventory_lot_id == int(receipt.reserve_inventory_lot_id)
                )
            ).all()
        )
        if lot is not None
        else []
    )
    has_reservation = (
        db.scalar(
            select(InventoryReservation.id)
            .where(
                InventoryReservation.inventory_lot_id
                == int(receipt.reserve_inventory_lot_id)
            )
            .limit(1)
        )
        is not None
    )
    if (
        lot is None
        or lot.source_ref_type != GROUP_LOT_REF_TYPE
        or lot.source_ref_id != receipt.id
        or lot.status != "active"
        or lot.inventory_type != "semi_finished"
        or int(lot.quantity_available) != int(receipt.reserve_received_sheet_quantity)
        or int(lot.quantity_reserved) != 0
        or int(lot.quantity_consumed) != 0
        or int(lot.quantity_damaged) != 0
        or int(lot.quantity_scrapped) != 0
        or has_reservation
        or len(movements) != 1
        or movements[0].movement_type != "manual_in"
        or _lot_transferred(db, lot.id)
    ):
        raise IncomingReceiptError(
            "本次备库纸板已被预占、移动、调整、消耗或处理，不能撤销收料",
            409,
        )
    return lot


def _idempotent_group_reversal(
    db: Session,
    *,
    group_receipt_id: int,
    idempotency_key: str,
    request_hash: str,
    actor_id: int,
) -> bool:
    prior = db.scalar(
        select(CompositePhysicalGroupReceiptReversal).where(
            CompositePhysicalGroupReceiptReversal.composite_physical_group_receipt_id
            == int(group_receipt_id)
        )
    )
    if prior is None:
        return False
    if (
        prior.idempotency_key != idempotency_key
        or int(prior.reversed_by) != int(actor_id)
        or not hmac.compare_digest(prior.request_hash, request_hash)
    ):
        raise IncomingReceiptError(
            "该物理组收料已由不同操作者或载荷撤销。",
            409,
            code="INCOMING_REVERSAL_IDEMPOTENCY_CONFLICT",
        )
    return True


def revert_composite_physical_group_receipt(
    db: Session,
    *,
    user: User,
    receipt_item_id: int,
    reason: str,
    idempotency_key: str,
    request_hash: str,
    audit_context: dict[str, object] | None = None,
) -> IncomingReceiptItem:
    clean_reason = str(reason or "").strip() or "撤回来料实收（系统记录）"
    receipt_item = db.get(IncomingReceiptItem, int(receipt_item_id))
    if receipt_item is None:
        raise IncomingReceiptError("来料实收记录不存在", 404)
    if receipt_item.composite_physical_purchase_group_id is None:
        raise IncomingReceiptError("该来料不是物理组收料", 409)
    group_receipt = db.scalar(
        select(CompositePhysicalGroupReceipt).where(
            CompositePhysicalGroupReceipt.incoming_receipt_item_id == receipt_item.id
        )
    )
    if group_receipt is None:
        raise IncomingReceiptError("物理组收料事实关联不完整", 409)
    if _idempotent_group_reversal(
        db,
        group_receipt_id=group_receipt.id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        actor_id=user.id,
    ):
        return receipt_item

    group = db.scalar(
        select(CompositePhysicalPurchaseGroup)
        .where(
            CompositePhysicalPurchaseGroup.id
            == group_receipt.composite_physical_purchase_group_id
        )
        .with_for_update()
    )
    if group is None:
        raise IncomingReceiptError("物理采购组不存在", 409)
    group_receipt = db.scalar(
        select(CompositePhysicalGroupReceipt)
        .where(
            CompositePhysicalGroupReceipt.incoming_receipt_item_id
            == receipt_item.id
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if group_receipt is None:
        raise IncomingReceiptError("物理组收料事实关联不完整", 409)
    receipt_item = db.scalar(
        select(IncomingReceiptItem)
        .where(IncomingReceiptItem.id == int(receipt_item_id))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if receipt_item is None:
        raise IncomingReceiptError("来料实收记录不存在", 404)
    if _idempotent_group_reversal(
        db,
        group_receipt_id=group_receipt.id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        actor_id=user.id,
    ):
        return receipt_item
    sources = _source_rows(db, group.id)
    order_items = {
        item.id: item
        for item in db.scalars(
            select(OrderItem).where(
                OrderItem.id.in_([source.order_item_id for source in sources])
            )
        ).all()
    }
    order_ids = sorted({int(item.order_id) for item in order_items.values()})
    try:
        locked_orders = lock_order_rows_for_production_transition(db, order_ids)
    except ProductionWorkflowError as error:
        raise IncomingReceiptError(str(error), error.status_code) from error
    if receipt_item.status != "posted" or group_receipt.status != "posted":
        raise IncomingReceiptError("该物理组收料已经撤销", 409)
    try:
        assert_receipt_item_not_in_confirmed_statement(db, receipt_item.id)
    except SupplierSettlementError as error:
        raise IncomingReceiptError(
            error.message, error.status_code, code=error.code
        ) from error
    allocations = _receipt_allocations(db, group_receipt.id)
    source_order_item_ids = {
        int(source.id): int(source.order_item_id) for source in sources
    }
    allocated_order_items = [
        order_items[order_item_id]
        for order_item_id in sorted(
            {
                source_order_item_ids.get(
                    int(allocation.composite_physical_purchase_group_source_id)
                )
                for allocation in allocations
                if allocation.status == "active"
                and int(
                    allocation.allocated_reserved_component_piece_quantity or 0
                )
                > 0
            }
            - {None}
        )
        if order_item_id in order_items
    ]
    if int(group_receipt.component_output_piece_quantity) > 0 and (
        any(
            int(item.delivered_quantity or 0) > 0
            for item in allocated_order_items
        )
        or has_dispatched_delivery_facts(
            db, [int(item.id) for item in allocated_order_items]
        )
    ):
        raise IncomingReceiptError("该收料批次分配的来源订单已有发货事实，禁止撤销收料", 409)
    for item in order_items.values():
        order = locked_orders.get(item.order_id)
        if order is None:
            raise IncomingReceiptError("物理采购组来源订单不存在", 409)

    component_lot = _assert_pristine_component_lot(
        db, receipt=group_receipt, allocations=allocations
    )
    reserve_lot = _assert_pristine_reserve_lot(db, group_receipt)
    now = utc_now_naive()
    reversed_reserved = 0
    try:
        for allocation in allocations:
            quantity = int(allocation.allocated_reserved_component_piece_quantity)
            release_finished_reservation(
                db,
                reservation_id=allocation.inventory_reservation_id,
                operator_id=user.id,
                release_reason=clean_reason,
                idempotency_key=(
                    f"cpgr-release:{group_receipt.id}:{allocation.id}"
                ),
                allow_downstream=True,
            )
            allocation.status = "reversed"
            allocation.version = int(allocation.version) + 1
            allocation.reversed_by = user.id
            allocation.reversed_at = now
            reversed_reserved += quantity
        if component_lot is not None:
            db.expire(component_lot)
            component_lot = db.get(InventoryLot, component_lot.id)
            if component_lot is None or int(component_lot.quantity_reserved) != 0:
                raise IncomingReceiptError("组收料组件预占释放不完整", 409)
            component_lot = mutate_lot(
                db,
                lot_id=component_lot.id,
                operation="adjust",
                expected_version=component_lot.version,
                operator_id=user.id,
                quantity=-int(component_lot.quantity_available),
                reason=clean_reason,
                idempotency_key=f"cpgr-reverse-component:{group_receipt.id}",
            )
            component_lot.status = "closed"
        if reserve_lot is not None:
            reserve_lot = mutate_lot(
                db,
                lot_id=reserve_lot.id,
                operation="adjust",
                expected_version=reserve_lot.version,
                operator_id=user.id,
                quantity=-int(reserve_lot.quantity_available),
                reason=clean_reason,
                idempotency_key=f"cpgr-reverse-reserve:{group_receipt.id}",
            )
            reserve_lot.status = "closed"
    except WarehouseInventoryError as error:
        raise IncomingReceiptError(str(error), error.status_code) from error

    version_before = int(group_receipt.version)
    group_receipt.status = "reversed"
    group_receipt.version = version_before + 1
    receipt_item.status = "reversed"
    receipt_item.reversal_reason = clean_reason
    receipt_item.reversed_by = user.id
    receipt_item.reversed_at = now
    receipt_item.receipt.status = "reversed"
    receipt_item.receipt.reversal_reason = clean_reason
    receipt_item.receipt.reversed_by = user.id
    receipt_item.receipt.reversed_at = now
    db.add(
        CompositePhysicalGroupReceiptReversal(
            composite_physical_group_receipt_id=group_receipt.id,
            receipt_version_before=version_before,
            receipt_version_after=version_before + 1,
            reversed_sheet_quantity=int(group_receipt.received_sheet_quantity),
            reversed_component_output_piece_quantity=int(
                group_receipt.component_output_piece_quantity
            ),
            reversed_reserved_component_piece_quantity=reversed_reserved,
            reason=clean_reason,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            reversed_by=user.id,
            reversed_at=now,
        )
    )
    db.flush()

    remaining_total, _, _, _ = _active_receipt_totals(db, group.id)
    if remaining_total >= int(group.purchase_sheet_quantity):
        next_status = "received"
    elif remaining_total > 0:
        next_status = "partially_received"
    else:
        next_status = "reversed"
    expected_group_version = int(group.version)
    result = db.execute(
        update(CompositePhysicalPurchaseGroup)
        .where(
            CompositePhysicalPurchaseGroup.id == group.id,
            CompositePhysicalPurchaseGroup.version == expected_group_version,
        )
        .values(
            status=next_status,
            version=CompositePhysicalPurchaseGroup.version + 1,
            updated_by=user.id,
            updated_at=now,
        )
    )
    if result.rowcount != 1:
        raise IncomingReceiptError("物理采购组版本已变化，请刷新后重试", 409)
    db.expire(group)
    group = db.get(CompositePhysicalPurchaseGroup, group.id)
    if group is None:
        raise IncomingReceiptError("物理采购组不存在", 409)
    if allocations:
        _sync_group_source_progress(
            db,
            sources=sources,
            operator_id=user.id,
        )
    context = audit_context or {}
    customer = db.get(Customer, group.customer_id)
    append_audit_event(
        db,
        request=context.get("request"),
        actor=user,
        event_category="business",
        result="success",
        source=str(context.get("source") or "web"),
        module_code="incoming",
        action_code="incoming.composite_group_revert",
        legacy_action="REVERT_COMPOSITE_GROUP",
        resource="CompositePhysicalGroupReceipt",
        entity_type="composite_physical_group_receipt",
        entity_id=group_receipt.id,
        object_ref=f"composite-group-receipt:{group_receipt.id}",
        customer_id=group.customer_id,
        customer_name=customer.name if customer is not None else None,
        description="撤销物理采购组来料实收",
        details={
            "group_id": group.id,
            "incoming_receipt_item_id": receipt_item.id,
            "reversed_sheet_quantity": group_receipt.received_sheet_quantity,
            "reversed_component_output_piece_quantity": (
                group_receipt.component_output_piece_quantity
            ),
            "reversed_reserved_component_piece_quantity": reversed_reserved,
        },
    )
    db.flush()
    return receipt_item


def _group_receipt_for_item(
    db: Session, receipt_item_id: int
) -> CompositePhysicalGroupReceipt | None:
    return db.scalar(
        select(CompositePhysicalGroupReceipt).where(
            CompositePhysicalGroupReceipt.incoming_receipt_item_id
            == int(receipt_item_id)
        )
    )


def group_receipt_response(
    db: Session,
    receipt_item: IncomingReceiptItem,
    *,
    can_view_cost: bool,
    sources: list[CompositePhysicalPurchaseGroupSource] | None = None,
    source_order_identities: dict[int, tuple[int, str]] | None = None,
) -> dict[str, Any]:
    group_id = receipt_item.composite_physical_purchase_group_id
    if group_id is None:
        raise IncomingReceiptError("该收料不是物理采购组收料", 409)
    group = db.get(CompositePhysicalPurchaseGroup, group_id)
    group_receipt = _group_receipt_for_item(db, receipt_item.id)
    if group is None or group_receipt is None:
        raise IncomingReceiptError("物理采购组收料事实关联不完整", 409)
    sources = list(sources) if sources is not None else _source_rows(db, group.id)
    anchor = _anchor_source(sources)
    anchor_item = db.get(OrderItem, anchor.order_item_id)
    anchor_order = db.get(Order, anchor_item.order_id) if anchor_item is not None else None
    product = db.get(Product, group.component_product_id)
    customer = db.get(Customer, group.customer_id)
    allocations = _receipt_allocations(db, group_receipt.id)
    reservation_ids = {
        int(row.inventory_reservation_id) for row in allocations
    }
    reservations = {
        int(row.id): row
        for row in db.scalars(
            select(InventoryReservation).where(
                InventoryReservation.id.in_(reservation_ids)
            )
        ).all()
    } if reservation_ids else {}
    source_by_id = {int(source.id): source for source in sources}
    active_total, _, _, _ = _active_receipt_totals(db, group.id)
    remaining_quantity = max(
        int(group.purchase_sheet_quantity) - active_total,
        0,
    )
    source_items = _source_payloads(
        db,
        sources,
        order_identities=source_order_identities,
    )
    source_order_count = _source_order_count(source_items)
    source_allocations = []
    for allocation in allocations:
        source = source_by_id.get(
            int(allocation.composite_physical_purchase_group_source_id)
        )
        reservation = reservations.get(int(allocation.inventory_reservation_id))
        source_allocations.append(
            {
                "allocation_id": int(allocation.id),
                "group_source_id": int(
                    allocation.composite_physical_purchase_group_source_id
                ),
                "source_sequence": (
                    int(source.source_sequence) if source is not None else None
                ),
                "order_id": int(allocation.order_id),
                "order_item_id": int(allocation.order_item_id),
                "sales_order_item_bom_component_id": int(
                    allocation.sales_order_item_bom_component_id
                ),
                "inventory_reservation_id": int(allocation.inventory_reservation_id),
                "reserved_component_piece_quantity": int(
                    allocation.allocated_reserved_component_piece_quantity
                ),
                "reservation_status": (
                    reservation.status if reservation is not None else None
                ),
                "status": allocation.status,
            }
        )
    purpose_allocation: dict[str, Any] = {
        "group_receipt_id": int(group_receipt.id),
        "purchase_receipt_fact_id": int(group_receipt.purchase_receipt_fact_id),
        "receipt_sequence": int(group_receipt.receipt_sequence),
        "order_purpose_sheet_qty": int(
            group_receipt.order_purpose_received_sheet_quantity
        ),
        "reserve_purpose_sheet_qty": int(
            group_receipt.reserve_received_sheet_quantity
        ),
        "component_output_piece_quantity": int(
            group_receipt.component_output_piece_quantity
        ),
        "component_inventory_lot_id": group_receipt.component_inventory_lot_id,
        "reserve_inventory_lot_id": group_receipt.reserve_inventory_lot_id,
        "source_allocations": source_allocations,
        "status": group_receipt.status,
    }
    if can_view_cost:
        purpose_allocation.update(
            {
                "sheet_cost": group_receipt.actual_unit_price_per_sheet,
                "order_cost": group_receipt.order_purpose_material_cost,
                "reserve_cost": group_receipt.reserve_material_cost,
                "receipt_total_cost": group_receipt.total_material_cost,
                "component_unit_material_cost": (
                    group_receipt.component_unit_material_cost
                ),
                "currency": group_receipt.currency_snapshot,
            }
        )
    return {
        "item_id": composite_physical_group_route(group.id),
        "composite_physical_purchase_group_id": int(group.id),
        "group_receipt_id": int(group_receipt.id),
        "group_key": group.group_key,
        "expected_group_version": int(group.version),
        "receipt_id": int(receipt_item.receipt_id),
        "receipt_item_id": int(receipt_item.id),
        "receipt_number": receipt_item.receipt.receipt_number,
        "receipt_status": receipt_item.status,
        "idempotency_key": receipt_item.receipt.idempotency_key,
        "planned_quantity": int(group.purchase_sheet_quantity),
        "received_quantity": int(receipt_item.received_quantity),
        "cumulative_received_quantity": int(
            group_receipt.cumulative_received_sheet_quantity_after
        ),
        "remaining_quantity": remaining_quantity,
        "incoming_quantity": int(receipt_item.received_quantity),
        "material_status": (
            "pending" if remaining_quantity > 0 else "received"
        ),
        "requisition_status": (
            "已报料" if remaining_quantity > 0 else "已入库"
        ),
        "variance_quantity": int(receipt_item.variance_quantity),
        "variance_type": receipt_item.variance_type,
        "resolution_status": receipt_item.resolution_status,
        "resolution_action": receipt_item.resolution_action,
        "received_inventory_lot_id": receipt_item.received_inventory_lot_id,
        "surplus_inventory_lot_id": receipt_item.surplus_inventory_lot_id,
        "purpose_status": "frozen",
        "purpose_allocation": purpose_allocation,
        "source_count": int(group.source_count),
        "source_items": source_items,
        "source_order_count": source_order_count,
        "customer_id": int(group.customer_id),
        "customer_name": customer.name if customer is not None else "",
        "order_id": anchor_order.id if anchor_order is not None else None,
        "order_item_id": anchor.order_item_id,
        "order_number": (
            f"{source_order_count}个订单"
            if source_order_count > 1
            else source_items[0]["order_number"]
            if source_items
            else anchor_order.order_number
            if anchor_order is not None
            else None
        ),
        "product_id": group.component_product_id,
        "product_code": product.product_code if product is not None else None,
        "product_name": product.product_name if product is not None else None,
        "material": group.material_code_snapshot,
        "cardboard_len": group.report_length_mm,
        "cardboard_width": group.report_width_mm,
        "flute_type": group.flute_type_snapshot,
        "layer_count": group.layer_count_snapshot,
        "component_type": "physical_group",
        "yield_per_sheet": int(group.yield_per_sheet),
        "order_purpose_sheet_quantity": int(group.order_purpose_sheet_quantity),
        "reserve_sheet_quantity": int(group.reserve_sheet_quantity),
        "purchase_sheet_quantity": int(group.purchase_sheet_quantity),
        "component_output_piece_quantity": int(
            group_receipt.component_output_piece_quantity
        ),
        "material_received_at": receipt_item.receipt.received_at,
        "material_received_by": receipt_item.receipt.received_by,
        "created_at": receipt_item.created_at,
    }


def group_receipt_history_rows(
    db: Session,
    *,
    received_since,
    include_reversed: bool,
    visible_customer_ids: set[int] | None,
    can_view_cost: bool,
) -> list[dict[str, Any]]:
    query = (
        select(IncomingReceiptItem)
        .join(
            CompositePhysicalGroupReceipt,
            CompositePhysicalGroupReceipt.incoming_receipt_item_id
            == IncomingReceiptItem.id,
        )
        .join(IncomingReceipt, IncomingReceipt.id == IncomingReceiptItem.receipt_id)
        .join(
            CompositePhysicalPurchaseGroup,
            CompositePhysicalPurchaseGroup.id
            == IncomingReceiptItem.composite_physical_purchase_group_id,
        )
        .where(IncomingReceipt.received_at >= received_since)
        .order_by(IncomingReceipt.received_at.desc(), IncomingReceiptItem.id.desc())
    )
    if not include_reversed:
        query = query.where(IncomingReceiptItem.status == "posted")
    if visible_customer_ids is not None:
        query = query.where(
            CompositePhysicalPurchaseGroup.customer_id.in_(visible_customer_ids)
        )
    receipt_items = list(db.scalars(query).all())
    group_ids = {
        int(row.composite_physical_purchase_group_id)
        for row in receipt_items
        if row.composite_physical_purchase_group_id is not None
    }
    source_rows = (
        list(
            db.scalars(
                select(CompositePhysicalPurchaseGroupSource)
                .where(
                    CompositePhysicalPurchaseGroupSource.composite_physical_purchase_group_id.in_(
                        group_ids
                    )
                )
                .order_by(
                    CompositePhysicalPurchaseGroupSource.composite_physical_purchase_group_id,
                    CompositePhysicalPurchaseGroupSource.source_sequence,
                    CompositePhysicalPurchaseGroupSource.id,
                )
            ).all()
        )
        if group_ids
        else []
    )
    sources_by_group: dict[int, list[CompositePhysicalPurchaseGroupSource]] = {}
    for source in source_rows:
        sources_by_group.setdefault(
            int(source.composite_physical_purchase_group_id), []
        ).append(source)
    source_order_identities = _source_order_identities(db, source_rows)
    return [
        group_receipt_response(
            db,
            row,
            can_view_cost=can_view_cost,
            sources=sources_by_group.get(
                int(row.composite_physical_purchase_group_id or 0), []
            ),
            source_order_identities=source_order_identities,
        )
        for row in receipt_items
    ]
