from __future__ import annotations

import hashlib
import hmac
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Iterable, Sequence

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from app.models.delivery import Delivery, DeliveryItem
from app.models.finance import ReturnReceipt, ReturnReceiptItem
from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem
from app.models.order import Order
from app.models.order_external_packaging import SalesOrderItemExternalComponent
from app.models.product import Product
from app.models.product_bom import SalesOrderItemBomComponent
from app.models.production import ProductionCompletion, ProductionTask
from app.models.purchase_receipt import (
    IncomingReceiptPurposeAllocation,
    IncomingReceiptPurposeReversal,
)
from app.models.requisition import RequisitionItem
from app.models.supplier_requisition_order import (
    PurchasePurposeSourceSnapshot,
    SupplierRequisitionOrderItem,
)
from app.models.warehouse_inventory import (
    DeliveryInventoryAllocation,
    FinishedGoodsInventoryDetail,
    InventoryLot,
    InventoryMovement,
    InventoryReservation,
)
from app.services.order_business_status import (
    MANAGEMENT_ORDER_STATUSES,
    build_order_business_statuses,
)
from app.services.printing_colors import PrintingColorError, normalize_printing_colors
from app.services.production_station_routing import (
    PRODUCTION_STATION_ROUTING_RULE_VERSION,
    production_station_memberships,
)


REPORT_SCHEMA_VERSION = "p0-15-v2"
TERMINAL_ORDER_STATUSES = frozenset(
    {"completed", "archived", "closed", "dead", "cancelled"}
)
FINDING_CODES = frozenset(
    {
        "P015_INCOMING_WITHOUT_PRODUCTION_TASK",
        "P015_PENDING_PRODUCTION_WITHOUT_INPUT_FACT",
        "P015_RECEIPT_NOT_PROJECTED_TO_PRODUCTION_TASK",
        "P015_LEGACY_RECEIVED_STATUS_TRACE_GAP",
        "P015_COMPLETION_WITHOUT_ACTIVE_FINISHED_LOT",
        "P015_COMPLETION_WITHOUT_INVENTORY_MOVEMENT",
        "P015_FINISHED_STOCK_NOT_DELIVERABLE",
        "P015_DELIVERY_INVENTORY_QUANTITY_MISMATCH",
        "P015_TRACE_LINK_BROKEN",
        "P015_COMMON_BOX_PERSISTED_CONTRACT_INVALID",
        "P015_COMMON_BOX_PRINT_COLOR_INVALID",
        "P015_COMMON_BOX_VERSION_SNAPSHOT_DRIFT",
        "P015_DUPLICATE_RECEIPT_FINISHED_OUTPUT",
        "P015_COMPLETION_INPUT_EXCEEDS_EFFECTIVE_RECEIPT",
        "P015_RECEIVED_AWAITING_MANUAL_PRODUCTION",
        "P015_ORDER_STATUS_SNAPSHOT_DIVERGENCE",
        "P015_PURCHASE_PURPOSE_ALLOCATION_UNBALANCED",
        "P015_RECEIPT_PURPOSE_ALLOCATION_UNBALANCED",
        "P015_RECEIPT_AUTO_FINISHED_MISMATCH",
        "P015_RESERVE_PURPOSE_GENERATED_ORDER_FINISHED",
    }
)


@dataclass(frozen=True)
class ChainFinding:
    code: str
    severity: str
    order_ref: str
    order_item_ref: str | None
    summary: str
    evidence: dict
    focus_match: dict | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _chunks(values: Sequence[int], size: int = 800) -> Iterable[list[int]]:
    for offset in range(0, len(values), size):
        yield list(values[offset : offset + size])


def _anonymous_ref(kind: str, value: int, key: bytes) -> str:
    digest = hmac.new(
        key,
        f"{kind}:{int(value)}".encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    return f"{kind.upper()}-{digest[:12].upper()}"


def _normalized_focus_terms(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            text.casefold()
            for value in values
            if (text := str(value or "").strip())
        )
    )


def _focus_match(
    order: Order,
    item,
    terms: Sequence[str],
    *,
    key: bytes,
) -> dict | None:
    if not terms:
        return None
    candidates = {
        "order_number": order.order_number,
        "customer_po": order.customer_po,
        "item_order_number": item.item_order_number if item is not None else None,
        "product_code": item.snapshot_product_code if item is not None else None,
        "product_name": item.snapshot_product_name if item is not None else None,
    }
    normalized = {
        field: str(value).strip()
        for field, value in candidates.items()
        if value is not None and str(value).strip()
    }
    matched_terms = {
        term
        for term in terms
        if any(term in value.casefold() for value in normalized.values())
    }
    if not matched_terms:
        return None
    matched_fields = sorted(
        field
        for field, value in normalized.items()
        if any(term in value.casefold() for term in matched_terms)
    )
    return {
        "matched": True,
        "matched_fields": matched_fields,
        "focus_tokens": sorted(
            "FOCUS-"
            + hmac.new(key, term.encode("utf-8"), hashlib.sha256)
            .hexdigest()[:12]
            .upper()
            for term in matched_terms
        ),
    }


def _finding(
    *,
    code: str,
    severity: str,
    order: Order,
    item,
    key: bytes,
    summary: str,
    evidence: dict,
    focus_terms: Sequence[str],
) -> ChainFinding:
    return ChainFinding(
        code=code,
        severity=severity,
        order_ref=_anonymous_ref("order", int(order.id), key),
        order_item_ref=(
            _anonymous_ref("item", int(item.id), key) if item is not None else None
        ),
        summary=summary,
        evidence=evidence,
        focus_match=_focus_match(order, item, focus_terms, key=key),
    )


def _posted_receipt_rows(
    db: Session, item_ids: Sequence[int]
) -> dict[int, list[dict]]:
    result: dict[int, list[dict]] = defaultdict(list)
    for chunk in _chunks(item_ids):
        rows = db.execute(
            select(
                IncomingReceiptItem.id,
                IncomingReceiptItem.order_item_id,
                IncomingReceiptItem.receipt_id,
                IncomingReceiptItem.requisition_id,
                IncomingReceiptItem.requisition_item_id,
                IncomingReceiptItem.supplier_order_id,
                IncomingReceiptItem.supplier_order_item_id,
                IncomingReceiptItem.received_quantity,
                IncomingReceiptItem.planned_quantity,
                IncomingReceiptItem.resolution_action,
                IncomingReceiptItem.received_inventory_lot_id,
                IncomingReceiptItem.surplus_inventory_lot_id,
            )
            .join(IncomingReceipt, IncomingReceipt.id == IncomingReceiptItem.receipt_id)
            .where(
                IncomingReceiptItem.order_item_id.in_(chunk),
                IncomingReceipt.status == "posted",
                IncomingReceiptItem.status == "posted",
            )
        ).mappings()
        for row in rows:
            result[int(row["order_item_id"])].append(dict(row))
    return result


def _task_rows(db: Session, item_ids: Sequence[int]) -> dict[int, dict[int | None, dict]]:
    result: dict[int, dict[int | None, dict]] = defaultdict(dict)
    for chunk in _chunks(item_ids):
        for row in db.execute(
            select(
                ProductionTask.id,
                ProductionTask.order_item_id,
                ProductionTask.sales_order_item_bom_component_id,
                ProductionTask.status,
                ProductionTask.production_label_enabled_snapshot,
                ProductionTask.production_label_units_per_label_snapshot,
                ProductionTask.print_content_snapshot,
                SalesOrderItemBomComponent.is_die_cut.label("component_is_die_cut"),
                SalesOrderItemBomComponent.snapshot_component_box_style.label(
                    "component_box_style"
                ),
            )
            .outerjoin(
                SalesOrderItemBomComponent,
                SalesOrderItemBomComponent.id
                == ProductionTask.sales_order_item_bom_component_id,
            )
            .where(ProductionTask.order_item_id.in_(chunk))
        ).mappings():
            component_id = row["sales_order_item_bom_component_id"]
            result[int(row["order_item_id"])][
                int(component_id) if component_id is not None else None
            ] = dict(row)
    return result


def _required_components(
    db: Session, item_ids: Sequence[int]
) -> dict[int, dict[int, int]]:
    result: dict[int, dict[int, int]] = defaultdict(dict)
    for chunk in _chunks(item_ids):
        for item_id, component_id, component_product_id in db.execute(
            select(
                SalesOrderItemBomComponent.sales_order_item_id,
                SalesOrderItemBomComponent.id,
                SalesOrderItemBomComponent.component_product_id,
            ).where(
                SalesOrderItemBomComponent.sales_order_item_id.in_(chunk),
                SalesOrderItemBomComponent.is_required.is_(True),
            )
        ):
            result[int(item_id)][int(component_id)] = int(component_product_id)
    return result


def _required_external_components(
    db: Session, item_ids: Sequence[int]
) -> dict[int, set[int]]:
    result: dict[int, set[int]] = defaultdict(set)
    for chunk in _chunks(item_ids):
        for item_id, component_id in db.execute(
            select(
                SalesOrderItemExternalComponent.sales_order_item_id,
                SalesOrderItemExternalComponent.id,
            ).where(
                SalesOrderItemExternalComponent.sales_order_item_id.in_(chunk),
                SalesOrderItemExternalComponent.is_required.is_(True),
            )
        ):
            result[int(item_id)].add(int(component_id))
    return result


def _completion_rows(db: Session, item_ids: Sequence[int]) -> dict[int, list[dict]]:
    result: dict[int, list[dict]] = defaultdict(list)
    for chunk in _chunks(item_ids):
        for row in db.execute(
            select(
                ProductionCompletion.id,
                ProductionCompletion.order_item_id,
                ProductionCompletion.task_id,
                ProductionCompletion.inventory_lot_id,
                ProductionCompletion.actual_output_quantity,
                ProductionCompletion.material_input_quantity,
                ProductionCompletion.completion_type,
                ProductionCompletion.origin,
                ProductionCompletion.stock_quantity,
                ProductionCompletion.direct_delivery_quantity,
            ).where(
                ProductionCompletion.order_item_id.in_(chunk),
                ProductionCompletion.status == "posted",
            )
        ).mappings():
            result[int(row["order_item_id"])].append(dict(row))
    return result


def _lots_by_id(db: Session, lot_ids: Sequence[int]) -> dict[int, dict]:
    result: dict[int, dict] = {}
    for chunk in _chunks(sorted(set(lot_ids))):
        for row in db.execute(
            select(
                InventoryLot.id,
                InventoryLot.inventory_type,
                InventoryLot.source_type,
                InventoryLot.source_ref_type,
                InventoryLot.source_ref_id,
                InventoryLot.status,
                InventoryLot.quantity_available,
                InventoryLot.quantity_reserved,
                InventoryLot.quantity_consumed,
                InventoryLot.quantity_damaged,
                InventoryLot.quantity_scrapped,
            ).where(InventoryLot.id.in_(chunk))
        ).mappings():
            result[int(row["id"])] = dict(row)
    return result


def _lot_and_movement_maps(
    db: Session, completion_rows: dict[int, list[dict]]
) -> tuple[dict[int, dict], dict[int, list[dict]], dict[int, list[int]]]:
    completion_ids = sorted(
        int(row["id"])
        for rows in completion_rows.values()
        for row in rows
    )
    lot_ids = sorted(
        int(row["inventory_lot_id"])
        for rows in completion_rows.values()
        for row in rows
        if row["inventory_lot_id"] is not None
    )
    lots: dict[int, dict] = {}
    movements: dict[int, list[dict]] = defaultdict(list)
    completion_source_lots: dict[int, list[int]] = defaultdict(list)
    lots.update(_lots_by_id(db, lot_ids))
    for chunk in _chunks(lot_ids):
        for row in db.execute(
            select(
                InventoryMovement.id,
                InventoryMovement.inventory_lot_id,
                InventoryMovement.movement_type,
                InventoryMovement.quantity,
                InventoryMovement.related_order_id,
                InventoryMovement.related_order_item_id,
                InventoryMovement.related_delivery_id,
            ).where(InventoryMovement.inventory_lot_id.in_(chunk))
        ).mappings():
            movements[int(row["inventory_lot_id"])].append(dict(row))
    for chunk in _chunks(completion_ids):
        for completion_id, lot_id in db.execute(
            select(InventoryLot.source_ref_id, InventoryLot.id).where(
                InventoryLot.source_ref_type == "production_completion",
                InventoryLot.source_ref_id.in_(chunk),
            )
        ):
            completion_source_lots[int(completion_id)].append(int(lot_id))
    return lots, movements, completion_source_lots


def _delivery_rows(db: Session, item_ids: Sequence[int]) -> tuple[dict[int, list[dict]], dict[int, list[dict]]]:
    deliveries: dict[int, list[dict]] = defaultdict(list)
    delivery_item_ids: list[int] = []
    for chunk in _chunks(item_ids):
        for row in db.execute(
            select(
                DeliveryItem.id,
                DeliveryItem.delivery_id,
                DeliveryItem.order_item_id,
                DeliveryItem.delivered_quantity,
            )
            .join(Delivery, Delivery.id == DeliveryItem.delivery_id)
            .where(
                DeliveryItem.order_item_id.in_(chunk),
                Delivery.status == "dispatched",
                DeliveryItem.source_type == "order",
            )
        ).mappings():
            record = dict(row)
            deliveries[int(row["order_item_id"])].append(record)
            delivery_item_ids.append(int(row["id"]))
    allocations: dict[int, list[dict]] = defaultdict(list)
    for chunk in _chunks(sorted(delivery_item_ids)):
        for row in db.execute(
            select(
                DeliveryInventoryAllocation.id,
                DeliveryInventoryAllocation.delivery_item_id,
                DeliveryInventoryAllocation.consume_movement_id,
                DeliveryInventoryAllocation.consumed_stock_quantity,
                DeliveryInventoryAllocation.credited_requirement_quantity,
                DeliveryInventoryAllocation.reversed_stock_quantity,
                DeliveryInventoryAllocation.reversed_requirement_quantity,
                DeliveryInventoryAllocation.status,
            ).where(DeliveryInventoryAllocation.delivery_item_id.in_(chunk))
        ).mappings():
            allocations[int(row["delivery_item_id"])].append(dict(row))
    return deliveries, allocations


def _effective_delivery_quantities(
    db: Session, delivery_item_ids: Sequence[int]
) -> dict[int, int]:
    result: dict[int, int] = {}
    for chunk in _chunks(sorted(set(delivery_item_ids))):
        for delivery_item_id, actual_quantity in db.execute(
            select(
                ReturnReceiptItem.delivery_item_id,
                ReturnReceiptItem.actual_received_quantity,
            )
            .join(
                ReturnReceipt,
                ReturnReceipt.id == ReturnReceiptItem.return_receipt_id,
            )
            .where(
                ReturnReceiptItem.delivery_item_id.in_(chunk),
                ReturnReceipt.status == "confirmed",
            )
        ):
            result[int(delivery_item_id)] = max(int(actual_quantity or 0), 0)
    return result


def _movement_by_id(db: Session, movement_ids: Sequence[int]) -> dict[int, dict]:
    result: dict[int, dict] = {}
    for chunk in _chunks(sorted(set(movement_ids))):
        for row in db.execute(
            select(
                InventoryMovement.id,
                InventoryMovement.inventory_lot_id,
                InventoryMovement.movement_type,
                InventoryMovement.quantity,
                InventoryMovement.related_delivery_id,
                InventoryMovement.related_order_item_id,
            ).where(InventoryMovement.id.in_(chunk))
        ).mappings():
            result[int(row["id"])] = dict(row)
    return result


def _reservation_rows(
    db: Session, item_ids: Sequence[int]
) -> tuple[dict[int, list[dict]], dict[int, dict], dict[int, dict]]:
    reservations: dict[int, list[dict]] = defaultdict(list)
    lot_ids: list[int] = []
    for chunk in _chunks(item_ids):
        for row in db.execute(
            select(
                InventoryReservation.id,
                InventoryReservation.order_id,
                InventoryReservation.order_item_id,
                InventoryReservation.sales_order_item_bom_component_id,
                InventoryReservation.inventory_lot_id,
                InventoryReservation.reservation_type,
                InventoryReservation.reserved_stock_quantity,
                InventoryReservation.credited_requirement_quantity,
                InventoryReservation.consumed_stock_quantity,
                InventoryReservation.released_stock_quantity,
                InventoryReservation.status,
            ).where(
                InventoryReservation.order_item_id.in_(chunk),
                InventoryReservation.status.in_(("active", "partial")),
            )
        ).mappings():
            record = dict(row)
            reservations[int(row["order_item_id"])].append(record)
            lot_ids.append(int(row["inventory_lot_id"]))
    lots = _lots_by_id(db, lot_ids)
    details: dict[int, dict] = {}
    for chunk in _chunks(sorted(set(lot_ids))):
        for row in db.execute(
            select(
                FinishedGoodsInventoryDetail.inventory_lot_id,
                FinishedGoodsInventoryDetail.owner_customer_id,
                FinishedGoodsInventoryDetail.is_general,
                FinishedGoodsInventoryDetail.product_id,
            ).where(FinishedGoodsInventoryDetail.inventory_lot_id.in_(chunk))
        ).mappings():
            details[int(row["inventory_lot_id"])] = dict(row)
    return reservations, lots, details


def _finished_details_by_lot(
    db: Session, lot_ids: Sequence[int]
) -> dict[int, dict]:
    result: dict[int, dict] = {}
    for chunk in _chunks(sorted(set(lot_ids))):
        for row in db.execute(
            select(
                FinishedGoodsInventoryDetail.inventory_lot_id,
                FinishedGoodsInventoryDetail.owner_customer_id,
                FinishedGoodsInventoryDetail.is_general,
                FinishedGoodsInventoryDetail.product_id,
            ).where(FinishedGoodsInventoryDetail.inventory_lot_id.in_(chunk))
        ).mappings():
            result[int(row["inventory_lot_id"])] = dict(row)
    return result


def _product_rows(db: Session, product_ids: Sequence[int]) -> dict[int, dict]:
    result: dict[int, dict] = {}
    for chunk in _chunks(sorted(set(product_ids))):
        for row in db.execute(
            select(
                Product.id,
                Product.pieces_per_box,
                Product.printing_plate_mode,
                Product.printing_plate_1_id,
                Product.printing_plate_2_id,
                Product.printing_plate_3_id,
                Product.production_label_enabled,
                Product.production_label_units_per_label,
                Product.print_content,
                Product.printing_colors,
                Product.box_category,
                Product.box_style,
                Product.is_virtual_composite_parent,
            ).where(Product.id.in_(chunk))
        ).mappings():
            result[int(row["id"])] = dict(row)
    return result


def _formal_purpose_rows(
    db: Session,
    item_ids: Sequence[int],
) -> tuple[dict[int, list[dict]], dict[tuple[str, int], dict]]:
    """Load every formal paperboard source for the unfinished item set."""

    by_item: dict[int, list[dict]] = defaultdict(list)
    by_key: dict[tuple[str, int], dict] = {}
    for chunk in _chunks(item_ids):
        supplier_rows = db.execute(
            select(
                SupplierRequisitionOrderItem.id,
                SupplierRequisitionOrderItem.order_item_id,
                SupplierRequisitionOrderItem.requisition_qty,
                SupplierRequisitionOrderItem.purpose_contract_status,
                SupplierRequisitionOrderItem.status,
            ).where(SupplierRequisitionOrderItem.order_item_id.in_(chunk))
        ).mappings()
        for row in supplier_rows:
            record = {
                **dict(row),
                "formal_kind": "supplier_item",
                "formal_item_id": int(row["id"]),
            }
            item_id = int(row["order_item_id"])
            by_item[item_id].append(record)
            by_key[("supplier_item", int(row["id"]))] = record

        requisition_rows = db.execute(
            select(
                RequisitionItem.id,
                RequisitionItem.order_item_id,
                RequisitionItem.requisition_qty,
                RequisitionItem.purpose_contract_status,
                RequisitionItem.status,
            ).where(RequisitionItem.order_item_id.in_(chunk))
        ).mappings()
        for row in requisition_rows:
            record = {
                **dict(row),
                "formal_kind": "requisition_item",
                "formal_item_id": int(row["id"]),
            }
            item_id = int(row["order_item_id"])
            by_item[item_id].append(record)
            by_key[("requisition_item", int(row["id"]))] = record
    return by_item, by_key


def _formal_source_is_active(row: dict) -> bool:
    status = str(row.get("status") or "").strip().casefold()
    if row.get("formal_kind") == "supplier_item":
        return status == "active"
    return status not in {"作废", "void", "voided", "cancelled", "canceled"}


def _purchase_purpose_rows(
    db: Session,
    formal_by_key: dict[tuple[str, int], dict],
) -> tuple[dict[int, list[dict]], dict[int, dict]]:
    """Load immutable P1-80 snapshots without per-item queries."""

    supplier_ids = sorted(
        formal_id
        for (kind, formal_id) in formal_by_key
        if kind == "supplier_item"
    )
    requisition_ids = sorted(
        formal_id
        for (kind, formal_id) in formal_by_key
        if kind == "requisition_item"
    )
    snapshots_by_item: dict[int, list[dict]] = defaultdict(list)
    snapshots_by_id: dict[int, dict] = {}
    all_ids = [("supplier_item", value) for value in supplier_ids] + [
        ("requisition_item", value) for value in requisition_ids
    ]
    for offset in range(0, len(all_ids), 800):
        batch = all_ids[offset : offset + 800]
        batch_supplier = [value for kind, value in batch if kind == "supplier_item"]
        batch_requisition = [
            value for kind, value in batch if kind == "requisition_item"
        ]
        filters = []
        if batch_supplier:
            filters.append(
                PurchasePurposeSourceSnapshot.supplier_requisition_order_item_id.in_(
                    batch_supplier
                )
            )
        if batch_requisition:
            filters.append(
                PurchasePurposeSourceSnapshot.material_requisition_item_id.in_(
                    batch_requisition
                )
            )
        if not filters:
            continue
        rows = db.execute(
            select(
                PurchasePurposeSourceSnapshot.id,
                PurchasePurposeSourceSnapshot.supplier_requisition_order_item_id,
                PurchasePurposeSourceSnapshot.material_requisition_item_id,
                PurchasePurposeSourceSnapshot.source_kind,
                PurchasePurposeSourceSnapshot.source_key,
                PurchasePurposeSourceSnapshot.customer_id,
                PurchasePurposeSourceSnapshot.component_type,
                PurchasePurposeSourceSnapshot.purchase_sheet_qty,
                PurchasePurposeSourceSnapshot.order_purpose_sheet_qty,
                PurchasePurposeSourceSnapshot.reserve_purpose_sheet_qty,
                PurchasePurposeSourceSnapshot.group_authoritative_order_sheet_qty_snapshot,
                PurchasePurposeSourceSnapshot.snapshot_version,
            ).where(or_(*filters))
        ).mappings()
        for row in rows:
            supplier_id = row["supplier_requisition_order_item_id"]
            requisition_id = row["material_requisition_item_id"]
            formal_key = (
                ("supplier_item", int(supplier_id))
                if supplier_id is not None
                else ("requisition_item", int(requisition_id))
            )
            formal = formal_by_key.get(formal_key)
            if formal is None:
                continue
            record = {
                **dict(row),
                "formal_kind": formal_key[0],
                "formal_item_id": formal_key[1],
                "order_item_id": int(formal["order_item_id"]),
                "formal_requisition_qty": int(formal["requisition_qty"] or 0),
                "formal_purpose_contract_status": str(
                    formal["purpose_contract_status"] or "legacy_unset"
                ),
                "formal_status": str(formal["status"] or ""),
            }
            snapshots_by_item[int(formal["order_item_id"])].append(record)
            snapshots_by_id[int(row["id"])] = record
    return snapshots_by_item, snapshots_by_id


def _receipt_purpose_rows(
    db: Session,
    order_item_ids: Sequence[int],
) -> tuple[dict[int, list[dict]], set[int]]:
    by_receipt_item: dict[int, list[dict]] = defaultdict(list)
    allocation_ids: list[int] = []
    for chunk in _chunks(sorted(set(order_item_ids))):
        rows = db.execute(
            select(
                IncomingReceiptPurposeAllocation.id,
                IncomingReceiptPurposeAllocation.incoming_receipt_item_id,
                IncomingReceiptPurposeAllocation.purpose_contract_status_snapshot,
                IncomingReceiptPurposeAllocation.purchase_purpose_source_snapshot_id,
                IncomingReceiptPurposeAllocation.purchase_receipt_fact_id,
                IncomingReceiptPurposeAllocation.supplier_requisition_order_item_id,
                IncomingReceiptPurposeAllocation.material_requisition_item_id,
                IncomingReceiptPurposeAllocation.source_kind,
                IncomingReceiptPurposeAllocation.source_key,
                IncomingReceiptPurposeAllocation.customer_id,
                IncomingReceiptPurposeAllocation.component_type,
                IncomingReceiptPurposeAllocation.order_purpose_plan_sheet_qty_snapshot,
                IncomingReceiptPurposeAllocation.reserve_purpose_plan_sheet_qty_snapshot,
                IncomingReceiptPurposeAllocation.receipt_total_sheet_qty,
                IncomingReceiptPurposeAllocation.receipt_order_purpose_sheet_qty,
                IncomingReceiptPurposeAllocation.receipt_reserve_purpose_sheet_qty,
                IncomingReceiptPurposeAllocation.cumulative_total_sheet_qty_before,
                IncomingReceiptPurposeAllocation.cumulative_total_sheet_qty_after,
                IncomingReceiptPurposeAllocation.cumulative_order_purpose_sheet_qty_before,
                IncomingReceiptPurposeAllocation.cumulative_order_purpose_sheet_qty_after,
                IncomingReceiptPurposeAllocation.cumulative_reserve_purpose_sheet_qty_before,
                IncomingReceiptPurposeAllocation.cumulative_reserve_purpose_sheet_qty_after,
                IncomingReceiptPurposeAllocation.finished_output_qty_before,
                IncomingReceiptPurposeAllocation.finished_output_qty_after,
                IncomingReceiptPurposeAllocation.finished_output_qty_delta,
                IncomingReceiptPurposeAllocation.production_completion_id,
                IncomingReceiptPurposeAllocation.finished_inventory_lot_id,
                IncomingReceiptPurposeAllocation.semi_finished_inventory_lot_id,
                IncomingReceiptPurposeAllocation.initial_semi_inventory_movement_id,
                IncomingReceiptPurposeAllocation.status,
            )
            .join(
                IncomingReceiptItem,
                IncomingReceiptItem.id
                == IncomingReceiptPurposeAllocation.incoming_receipt_item_id,
            )
            .where(IncomingReceiptItem.order_item_id.in_(chunk))
        ).mappings()
        for row in rows:
            record = dict(row)
            allocation_id = int(row["id"])
            allocation_ids.append(allocation_id)
            by_receipt_item[int(row["incoming_receipt_item_id"])].append(record)
    reversed_ids: set[int] = set()
    for chunk in _chunks(sorted(set(allocation_ids))):
        reversed_ids.update(
            int(value)
            for value in db.scalars(
                select(
                    IncomingReceiptPurposeReversal.incoming_receipt_purpose_allocation_id
                ).where(
                    IncomingReceiptPurposeReversal.incoming_receipt_purpose_allocation_id.in_(
                        chunk
                    )
                )
            ).all()
        )
    return by_receipt_item, reversed_ids


def audit_incomplete_order_chains(
    db: Session,
    *,
    anonymization_key: bytes,
    focus_terms: Sequence[str] = (),
    generated_at: datetime | None = None,
) -> dict:
    """Read every unfinished order and return anonymous, fact-based findings.

    The function never flushes or commits.  It deliberately treats a received
    line that still awaits the current manual production confirmation as work
    in progress, not as an inventory defect.
    """

    if not anonymization_key:
        raise ValueError("anonymization_key must not be empty")
    normalized_focus = _normalized_focus_terms(focus_terms)
    orders = list(
        db.scalars(
            select(Order)
            .options(selectinload(Order.items))
            .where(Order.status.not_in(TERMINAL_ORDER_STATUSES))
            .order_by(Order.id)
        ).unique()
    )
    item_ids = [int(item.id) for order in orders for item in order.items]
    projections = build_order_business_statuses(
        db,
        orders,
        include_delivery=True,
        include_finance=True,
    )
    receipts = _posted_receipt_rows(db, item_ids)
    tasks = _task_rows(db, item_ids)
    required_components = _required_components(db, item_ids)
    required_external_components = _required_external_components(db, item_ids)
    completions = _completion_rows(db, item_ids)
    lots, lot_movements, completion_source_lots = _lot_and_movement_maps(
        db, completions
    )
    completion_finished_details = _finished_details_by_lot(db, list(lots))
    deliveries, allocations = _delivery_rows(db, item_ids)
    effective_delivery_quantities = _effective_delivery_quantities(
        db,
        [
            int(row["id"])
            for rows in deliveries.values()
            for row in rows
        ],
    )
    reservations, reservation_lots, finished_details = _reservation_rows(db, item_ids)
    products = _product_rows(
        db,
        [int(item.product_id) for order in orders for item in order.items],
    )
    formal_purpose_sources, formal_purpose_by_key = _formal_purpose_rows(
        db, item_ids
    )
    purchase_purpose_snapshots, purchase_purpose_by_id = _purchase_purpose_rows(
        db, formal_purpose_by_key
    )
    receipt_purpose_rows, reversed_receipt_purpose_ids = _receipt_purpose_rows(
        db, item_ids
    )
    active_receipt_purpose_rows: dict[int, list[dict]] = defaultdict(list)
    for receipt_item_id, rows in receipt_purpose_rows.items():
        active_receipt_purpose_rows[receipt_item_id] = [
            row
            for row in rows
            if int(row["id"]) not in reversed_receipt_purpose_ids
        ]
    receipt_lots = _lots_by_id(
        db,
        [
            int(row["received_inventory_lot_id"])
            for rows in receipts.values()
            for row in rows
            if row["received_inventory_lot_id"] is not None
        ],
    )
    purpose_inventory_lots = _lots_by_id(
        db,
        [
            int(lot_id)
            for rows in active_receipt_purpose_rows.values()
            for row in rows
            for lot_id in (
                row["finished_inventory_lot_id"],
                row["semi_finished_inventory_lot_id"],
            )
            if lot_id is not None
        ],
    )
    purpose_initial_movements = _movement_by_id(
        db,
        [
            int(row["initial_semi_inventory_movement_id"])
            for rows in active_receipt_purpose_rows.values()
            for row in rows
            if row["initial_semi_inventory_movement_id"] is not None
        ],
    )
    completions_by_id = {
        int(row["id"]): row
        for rows in completions.values()
        for row in rows
    }
    allocation_movement_ids = [
        int(row["consume_movement_id"])
        for rows in allocations.values()
        for row in rows
    ]
    allocation_movements = _movement_by_id(db, allocation_movement_ids)
    reserved_by_lot: Counter[int] = Counter()
    for rows in reservations.values():
        for reservation in rows:
            if reservation["reservation_type"] == "finished_order":
                reserved_by_lot[int(reservation["inventory_lot_id"])] += max(
                    int(reservation["reserved_stock_quantity"] or 0)
                    - int(reservation["consumed_stock_quantity"] or 0)
                    - int(reservation["released_stock_quantity"] or 0),
                    0,
                )

    findings: list[ChainFinding] = []
    scanned_items = 0
    workstation_eligible_tasks = 0
    workstation_station_counts: Counter[str] = Counter()
    workstation_dual_route_tasks = 0
    workstation_unrouted_tasks = 0
    workstation_routes: list[dict] = []
    active_formal_purpose_source_count = sum(
        _formal_source_is_active(row)
        for rows in formal_purpose_sources.values()
        for row in rows
    )
    purchase_purpose_snapshot_count = sum(
        len(rows) for rows in purchase_purpose_snapshots.values()
    )
    active_receipt_purpose_allocation_count = sum(
        len(rows) for rows in active_receipt_purpose_rows.values()
    )
    reversed_receipt_purpose_allocation_count = len(
        reversed_receipt_purpose_ids
    )
    for order in orders:
        projection = projections[int(order.id)]
        if (
            order.status not in MANAGEMENT_ORDER_STATUSES
            and str(order.status) != str(projection["business_status"])
        ):
            findings.append(
                _finding(
                    code="P015_ORDER_STATUS_SNAPSHOT_DIVERGENCE",
                    severity="review",
                    order=order,
                    item=None,
                    key=anonymization_key,
                    summary="订单保存状态与当前业务事实投影不同，需核对但不能自动改状态。",
                    evidence={
                        "persisted_status": str(order.status),
                        "derived_status": str(projection["business_status"]),
                    },
                    focus_terms=normalized_focus,
                )
            )
        for item in order.items:
            if item.is_force_closed or int(item.delivered_quantity or 0) >= int(item.quantity):
                continue
            scanned_items += 1
            item_id = int(item.id)
            item_receipts = receipts.get(item_id, [])
            has_receipt = bool(item_receipts)
            item_tasks = tasks.get(item_id, {})
            component_products = required_components.get(item_id, {})
            components = set(component_products)
            external_components = required_external_components.get(item_id, set())
            product = products.get(int(item.product_id))
            item_formal_sources = formal_purpose_sources.get(item_id, [])
            item_purpose_snapshots = purchase_purpose_snapshots.get(item_id, [])
            snapshots_by_formal: dict[tuple[str, int], list[dict]] = defaultdict(list)
            for snapshot in item_purpose_snapshots:
                snapshots_by_formal[
                    (snapshot["formal_kind"], int(snapshot["formal_item_id"]))
                ].append(snapshot)
            for formal in item_formal_sources:
                if not _formal_source_is_active(formal):
                    continue
                formal_key = (
                    str(formal["formal_kind"]),
                    int(formal["formal_item_id"]),
                )
                linked_snapshots = snapshots_by_formal.get(formal_key, [])
                marker = str(
                    formal.get("purpose_contract_status") or "legacy_unset"
                )
                marker_reasons: list[str] = []
                if marker == "frozen" and len(linked_snapshots) != 1:
                    marker_reasons.append("frozen_source_snapshot_count_mismatch")
                if marker == "legacy_unset" and linked_snapshots:
                    marker_reasons.append("legacy_source_has_frozen_snapshot")
                if marker_reasons:
                    findings.append(
                        _finding(
                            code="P015_PURCHASE_PURPOSE_ALLOCATION_UNBALANCED",
                            severity="error",
                            order=order,
                            item=item,
                            key=anonymization_key,
                            summary="正式纸板来源的采购用途标记与不可变快照不一致。",
                            evidence={
                                "formal_kind": formal_key[0],
                                "formal_item_id": formal_key[1],
                                "purpose_contract_status": marker,
                                "snapshot_count": len(linked_snapshots),
                                "invalid_reasons": marker_reasons,
                            },
                            focus_terms=normalized_focus,
                        )
                    )
            for snapshot in item_purpose_snapshots:
                purchase_qty = int(snapshot["purchase_sheet_qty"] or 0)
                order_qty = int(snapshot["order_purpose_sheet_qty"] or 0)
                reserve_qty = int(snapshot["reserve_purpose_sheet_qty"] or 0)
                authoritative_qty = int(
                    snapshot["group_authoritative_order_sheet_qty_snapshot"] or 0
                )
                invalid_reasons: list[str] = []
                if order_qty + reserve_qty != purchase_qty:
                    invalid_reasons.append("purpose_sum_mismatch")
                if order_qty > authoritative_qty:
                    invalid_reasons.append("order_purpose_exceeds_authority")
                if purchase_qty != int(snapshot["formal_requisition_qty"] or 0):
                    invalid_reasons.append("formal_purchase_quantity_mismatch")
                if str(snapshot["formal_purpose_contract_status"]) != "frozen":
                    invalid_reasons.append("formal_source_not_frozen")
                if int(snapshot["customer_id"] or 0) != int(order.customer_id):
                    invalid_reasons.append("customer_scope_mismatch")
                if invalid_reasons:
                    findings.append(
                        _finding(
                            code="P015_PURCHASE_PURPOSE_ALLOCATION_UNBALANCED",
                            severity="error",
                            order=order,
                            item=item,
                            key=anonymization_key,
                            summary="采购总张、订单用途与客户片料备库用途不守恒。",
                            evidence={
                                "purpose_snapshot_id": int(snapshot["id"]),
                                "formal_kind": snapshot["formal_kind"],
                                "formal_item_id": int(snapshot["formal_item_id"]),
                                "purchase_sheet_qty": purchase_qty,
                                "order_purpose_sheet_qty": order_qty,
                                "reserve_purpose_sheet_qty": reserve_qty,
                                "authoritative_order_sheet_qty": authoritative_qty,
                                "invalid_reasons": invalid_reasons,
                            },
                            focus_terms=normalized_focus,
                        )
                    )
            for component_id, task_row in item_tasks.items():
                if str(task_row["status"]) != "pending":
                    continue
                is_component = component_id is not None
                memberships = production_station_memberships(
                    print_content_snapshot=task_row["print_content_snapshot"],
                    box_style=(
                        task_row["component_box_style"]
                        if is_component
                        else product["box_style"] if product else None
                    ),
                    die_cut_required=(
                        bool(task_row["component_is_die_cut"])
                        if is_component
                        else bool(product and product["box_category"] == "die_cut")
                    ),
                )
                workstation_eligible_tasks += 1
                workstation_station_counts.update(memberships)
                if len(memberships) > 1:
                    workstation_dual_route_tasks += 1
                if not memberships:
                    workstation_unrouted_tasks += 1
                workstation_routes.append(
                    {
                        "task_ref": _anonymous_ref(
                            "production_task", int(task_row["id"]), anonymization_key
                        ),
                        "order_item_ref": _anonymous_ref(
                            "item", item_id, anonymization_key
                        ),
                        "component_ref": (
                            _anonymous_ref(
                                "bom_component", int(component_id), anonymization_key
                            )
                            if component_id is not None
                            else None
                        ),
                        "source_kind": "bom_component" if is_component else "regular",
                        "die_cut_required": (
                            bool(task_row["component_is_die_cut"])
                            if is_component
                            else bool(
                                product and product["box_category"] == "die_cut"
                            )
                        ),
                        "stations": sorted(memberships),
                        "focus_match": _focus_match(
                            order,
                            item,
                            normalized_focus,
                            key=anonymization_key,
                        ),
                    }
                )
            missing_task_keys: list[str] = []
            if item.supply_mode_snapshot != "external_purchase":
                if components:
                    missing_task_keys = [
                        f"component:{component_id}"
                        for component_id in sorted(components - set(item_tasks))
                    ]
                elif (
                    not external_components
                    and not bool(
                        product and product["is_virtual_composite_parent"]
                    )
                    and None not in item_tasks
                ):
                    missing_task_keys = ["regular"]
            if has_receipt and missing_task_keys:
                findings.append(
                    _finding(
                        code="P015_INCOMING_WITHOUT_PRODUCTION_TASK",
                        severity="error",
                        order=order,
                        item=item,
                        key=anonymization_key,
                        summary="已有正式来料事实，但缺少应持续存在的生产任务。",
                        evidence={
                            "missing_task_keys": missing_task_keys,
                            "posted_receipt_count": len(item_receipts),
                            "supply_mode": item.supply_mode_snapshot,
                        },
                        focus_terms=normalized_focus,
                    )
                )
            if has_receipt and any(
                str(row["status"]) == "waiting_material"
                for row in item_tasks.values()
            ):
                findings.append(
                    _finding(
                        code="P015_RECEIPT_NOT_PROJECTED_TO_PRODUCTION_TASK",
                        severity="review",
                        order=order,
                        item=item,
                        key=anonymization_key,
                        summary="已有正式收料，但至少一个生产任务仍停留在待料。",
                        evidence={
                            "waiting_task_ids": sorted(
                                int(row["id"])
                                for row in item_tasks.values()
                                if str(row["status"]) == "waiting_material"
                            ),
                            "posted_receipt_count": len(item_receipts),
                        },
                        focus_terms=normalized_focus,
                    )
                )
            if not has_receipt and str(item.material_status) == "received":
                findings.append(
                    _finding(
                        code="P015_LEGACY_RECEIVED_STATUS_TRACE_GAP",
                        severity="review",
                        order=order,
                        item=item,
                        key=anonymization_key,
                        summary="明细保存为已来料，但没有正式收料事实可追溯。",
                        evidence={"persisted_material_status": "received"},
                        focus_terms=normalized_focus,
                    )
                )
            item_active_purpose_allocations = [
                allocation
                for receipt in item_receipts
                for allocation in active_receipt_purpose_rows.get(
                    int(receipt["id"]), []
                )
            ]
            has_frozen_formal_receipt = False
            for receipt in item_receipts:
                receipt_formals = []
                if receipt["supplier_order_item_id"] is not None:
                    formal = formal_purpose_by_key.get(
                        ("supplier_item", int(receipt["supplier_order_item_id"]))
                    )
                    if formal is not None:
                        receipt_formals.append(formal)
                if receipt["requisition_item_id"] is not None:
                    formal = formal_purpose_by_key.get(
                        ("requisition_item", int(receipt["requisition_item_id"]))
                    )
                    if formal is not None:
                        receipt_formals.append(formal)
                frozen_receipt_source = any(
                    str(row.get("purpose_contract_status") or "legacy_unset")
                    == "frozen"
                    for row in receipt_formals
                )
                has_frozen_formal_receipt = (
                    has_frozen_formal_receipt or frozen_receipt_source
                )
                active_rows = active_receipt_purpose_rows.get(int(receipt["id"]), [])
                allocation_contract_invalid = (
                    frozen_receipt_source and len(active_rows) != 1
                ) or (
                    not frozen_receipt_source
                    and any(
                        str(row["purpose_contract_status_snapshot"]) == "frozen"
                        for row in active_rows
                    )
                )
                if allocation_contract_invalid:
                    findings.append(
                        _finding(
                            code="P015_RECEIPT_PURPOSE_ALLOCATION_UNBALANCED",
                            severity="error",
                            order=order,
                            item=item,
                            key=anonymization_key,
                            summary="正式收料来源与本次用途分流事实不一致。",
                            evidence={
                                "receipt_item_id": int(receipt["id"]),
                                "frozen_formal_source": frozen_receipt_source,
                                "active_purpose_allocation_count": len(active_rows),
                                "trace_kind": "receipt_purpose_contract_mismatch",
                            },
                            focus_terms=normalized_focus,
                        )
                    )
                for allocation in active_rows:
                    invalid_reasons: list[str] = []
                    total_qty = int(allocation["receipt_total_sheet_qty"] or 0)
                    order_qty = int(
                        allocation["receipt_order_purpose_sheet_qty"] or 0
                    )
                    reserve_qty = int(
                        allocation["receipt_reserve_purpose_sheet_qty"] or 0
                    )
                    if total_qty != int(receipt["received_quantity"] or 0):
                        invalid_reasons.append("receipt_quantity_mismatch")
                    if order_qty + reserve_qty != total_qty:
                        invalid_reasons.append("receipt_purpose_sum_mismatch")
                    if (
                        int(allocation["cumulative_total_sheet_qty_after"] or 0)
                        != int(allocation["cumulative_total_sheet_qty_before"] or 0)
                        + total_qty
                    ):
                        invalid_reasons.append("cumulative_total_mismatch")
                    if (
                        int(
                            allocation[
                                "cumulative_order_purpose_sheet_qty_after"
                            ]
                            or 0
                        )
                        != int(
                            allocation[
                                "cumulative_order_purpose_sheet_qty_before"
                            ]
                            or 0
                        )
                        + order_qty
                    ):
                        invalid_reasons.append("cumulative_order_mismatch")
                    if (
                        int(
                            allocation[
                                "cumulative_reserve_purpose_sheet_qty_after"
                            ]
                            or 0
                        )
                        != int(
                            allocation[
                                "cumulative_reserve_purpose_sheet_qty_before"
                            ]
                            or 0
                        )
                        + reserve_qty
                    ):
                        invalid_reasons.append("cumulative_reserve_mismatch")
                    snapshot_id = allocation[
                        "purchase_purpose_source_snapshot_id"
                    ]
                    snapshot = (
                        purchase_purpose_by_id.get(int(snapshot_id))
                        if snapshot_id is not None
                        else None
                    )
                    if (
                        str(allocation["purpose_contract_status_snapshot"])
                        != "frozen"
                        or snapshot is None
                        or allocation["purchase_receipt_fact_id"] is None
                    ):
                        invalid_reasons.append("frozen_contract_link_missing")
                    if snapshot is not None:
                        if int(snapshot["order_item_id"]) != item_id:
                            invalid_reasons.append("purpose_snapshot_order_mismatch")
                        if int(snapshot["customer_id"] or 0) != int(
                            allocation["customer_id"] or 0
                        ):
                            invalid_reasons.append("purpose_customer_mismatch")
                        if int(snapshot["order_purpose_sheet_qty"] or 0) != int(
                            allocation["order_purpose_plan_sheet_qty_snapshot"]
                            or 0
                        ):
                            invalid_reasons.append("order_plan_snapshot_mismatch")
                        if int(snapshot["reserve_purpose_sheet_qty"] or 0) != int(
                            allocation["reserve_purpose_plan_sheet_qty_snapshot"]
                            or 0
                        ):
                            invalid_reasons.append("reserve_plan_snapshot_mismatch")
                    if invalid_reasons:
                        findings.append(
                            _finding(
                                code="P015_RECEIPT_PURPOSE_ALLOCATION_UNBALANCED",
                                severity="error",
                                order=order,
                                item=item,
                                key=anonymization_key,
                                summary="本次收料的订单用途、片料备库用途或累计数量不守恒。",
                                evidence={
                                    "receipt_item_id": int(receipt["id"]),
                                    "purpose_allocation_id": int(allocation["id"]),
                                    "receipt_total_sheet_qty": total_qty,
                                    "receipt_order_purpose_sheet_qty": order_qty,
                                    "receipt_reserve_purpose_sheet_qty": reserve_qty,
                                    "invalid_reasons": invalid_reasons,
                                },
                                focus_terms=normalized_focus,
                            )
                        )

                    finished_delta = int(
                        allocation["finished_output_qty_delta"] or 0
                    )
                    if reserve_qty > 0 and order_qty == 0 and finished_delta > 0:
                        findings.append(
                            _finding(
                                code="P015_RESERVE_PURPOSE_GENERATED_ORDER_FINISHED",
                                severity="error",
                                order=order,
                                item=item,
                                key=anonymization_key,
                                summary="仅片料备库用途的收料错误增加了当前订单成品。",
                                evidence={
                                    "receipt_item_id": int(receipt["id"]),
                                    "purpose_allocation_id": int(allocation["id"]),
                                    "reserve_sheet_qty": reserve_qty,
                                    "finished_output_qty_delta": finished_delta,
                                },
                                focus_terms=normalized_focus,
                            )
                        )
                    completion_id = allocation["production_completion_id"]
                    completion = (
                        completions_by_id.get(int(completion_id))
                        if completion_id is not None
                        else None
                    )
                    completion_invalid = (
                        (finished_delta == 0 and completion_id is not None)
                        or (finished_delta > 0 and completion is None)
                        or (
                            completion is not None
                            and (
                                str(completion["origin"]) != "receipt_auto"
                                or int(completion["order_item_id"]) != item_id
                                or int(completion["actual_output_quantity"] or 0)
                                != finished_delta
                                or int(completion["inventory_lot_id"] or 0)
                                != int(allocation["finished_inventory_lot_id"] or 0)
                            )
                        )
                    )
                    if completion_invalid:
                        findings.append(
                            _finding(
                                code="P015_RECEIPT_AUTO_FINISHED_MISMATCH",
                                severity="error",
                                order=order,
                                item=item,
                                key=anonymization_key,
                                summary="收料用途分流与自动成品完工事实不一致。",
                                evidence={
                                    "receipt_item_id": int(receipt["id"]),
                                    "purpose_allocation_id": int(allocation["id"]),
                                    "production_completion_id": completion_id,
                                    "finished_output_qty_delta": finished_delta,
                                },
                                focus_terms=normalized_focus,
                            )
                        )
                    reserve_lot_id = allocation[
                        "semi_finished_inventory_lot_id"
                    ]
                    reserve_movement_id = allocation[
                        "initial_semi_inventory_movement_id"
                    ]
                    reserve_lot = (
                        purpose_inventory_lots.get(int(reserve_lot_id))
                        if reserve_lot_id is not None
                        else None
                    )
                    reserve_movement = (
                        purpose_initial_movements.get(int(reserve_movement_id))
                        if reserve_movement_id is not None
                        else None
                    )
                    reserve_links_invalid = (
                        reserve_qty == 0
                        and (reserve_lot_id is not None or reserve_movement_id is not None)
                    ) or (
                        reserve_qty > 0
                        and (
                            reserve_lot is None
                            or reserve_lot["inventory_type"] != "semi_finished"
                            or reserve_movement is None
                            or reserve_movement["movement_type"] != "manual_in"
                            or int(reserve_movement["inventory_lot_id"] or 0)
                            != int(reserve_lot_id or 0)
                            or int(reserve_movement["quantity"] or 0) != reserve_qty
                        )
                    )
                    if reserve_links_invalid:
                        findings.append(
                            _finding(
                                code="P015_RECEIPT_PURPOSE_ALLOCATION_UNBALANCED",
                                severity="error",
                                order=order,
                                item=item,
                                key=anonymization_key,
                                summary="片料备库用途缺少数量一致的半成品批次或入库流水。",
                                evidence={
                                    "receipt_item_id": int(receipt["id"]),
                                    "purpose_allocation_id": int(allocation["id"]),
                                    "reserve_sheet_qty": reserve_qty,
                                    "semi_finished_inventory_lot_id": reserve_lot_id,
                                    "initial_semi_inventory_movement_id": reserve_movement_id,
                                    "trace_kind": "reserve_inventory_link_mismatch",
                                },
                                focus_terms=normalized_focus,
                            )
                        )

            allocations_by_snapshot: dict[int, list[dict]] = defaultdict(list)
            for allocation in item_active_purpose_allocations:
                snapshot_id = allocation["purchase_purpose_source_snapshot_id"]
                if snapshot_id is not None:
                    allocations_by_snapshot[int(snapshot_id)].append(allocation)
            for snapshot_id, rows in allocations_by_snapshot.items():
                expected_total = expected_order = expected_reserve = 0
                for allocation in sorted(rows, key=lambda row: int(row["id"])):
                    chain_mismatch = (
                        int(allocation["cumulative_total_sheet_qty_before"] or 0)
                        != expected_total
                        or int(
                            allocation[
                                "cumulative_order_purpose_sheet_qty_before"
                            ]
                            or 0
                        )
                        != expected_order
                        or int(
                            allocation[
                                "cumulative_reserve_purpose_sheet_qty_before"
                            ]
                            or 0
                        )
                        != expected_reserve
                    )
                    expected_total = int(
                        allocation["cumulative_total_sheet_qty_after"] or 0
                    )
                    expected_order = int(
                        allocation["cumulative_order_purpose_sheet_qty_after"] or 0
                    )
                    expected_reserve = int(
                        allocation["cumulative_reserve_purpose_sheet_qty_after"] or 0
                    )
                    if chain_mismatch:
                        findings.append(
                            _finding(
                                code="P015_RECEIPT_PURPOSE_ALLOCATION_UNBALANCED",
                                severity="error",
                                order=order,
                                item=item,
                                key=anonymization_key,
                                summary="同一采购用途快照的多次收料累计链不连续。",
                                evidence={
                                    "purpose_snapshot_id": snapshot_id,
                                    "purpose_allocation_id": int(allocation["id"]),
                                    "trace_kind": "receipt_purpose_cumulative_gap",
                                },
                                focus_terms=normalized_focus,
                            )
                        )

            receipt_auto_output = sum(
                int(row["actual_output_quantity"] or 0)
                for row in completions.get(item_id, [])
                if str(row["origin"]) == "receipt_auto"
            )
            allocated_auto_output = sum(
                int(row["finished_output_qty_delta"] or 0)
                for row in item_active_purpose_allocations
            )
            if receipt_auto_output != allocated_auto_output:
                findings.append(
                    _finding(
                        code="P015_RECEIPT_AUTO_FINISHED_MISMATCH",
                        severity="error",
                        order=order,
                        item=item,
                        key=anonymization_key,
                        summary="当前有效自动完工数量与收料用途分流累计不一致。",
                        evidence={
                            "receipt_auto_finished_quantity": receipt_auto_output,
                            "allocated_finished_quantity": allocated_auto_output,
                            "trace_kind": "receipt_auto_total_mismatch",
                        },
                        focus_terms=normalized_focus,
                    )
                )

            if (
                has_receipt
                and not completions.get(item_id)
                and not missing_task_keys
                and not has_frozen_formal_receipt
            ):
                findings.append(
                    _finding(
                        code="P015_RECEIVED_AWAITING_MANUAL_PRODUCTION",
                        severity="info",
                        order=order,
                        item=item,
                        key=anonymization_key,
                        summary="已收料且任务存在，当前仍等待生产确认；这不是库存缺失。",
                        evidence={
                            "posted_receipt_count": len(item_receipts),
                            "task_statuses": sorted(
                                str(row["status"]) for row in item_tasks.values()
                            ),
                        },
                        focus_terms=normalized_focus,
                    )
                )
            for receipt in item_receipts:
                missing_links = [
                    field
                    for field in (
                        "requisition_id",
                        "requisition_item_id",
                        "supplier_order_id",
                        "supplier_order_item_id",
                    )
                    if receipt[field] is None
                ]
                if missing_links:
                    findings.append(
                        _finding(
                            code="P015_TRACE_LINK_BROKEN",
                            severity="review",
                            order=order,
                            item=item,
                            key=anonymization_key,
                            summary="正式收料事实缺少部分报料或供应商采购追溯关联。",
                            evidence={
                                "receipt_item_id": int(receipt["id"]),
                                "missing_links": missing_links,
                            },
                            focus_terms=normalized_focus,
                        )
                    )
                received_lot_id = receipt["received_inventory_lot_id"]
                if received_lot_id is not None:
                    lot = receipt_lots.get(int(received_lot_id))
                    if lot is not None and lot["inventory_type"] == "finished":
                        findings.append(
                            _finding(
                                code="P015_TRACE_LINK_BROKEN",
                                severity="error",
                                order=order,
                                item=item,
                                key=anonymization_key,
                                summary="订单纸板收料直接指向成品库存，可能绕过生产完工链。",
                                evidence={
                                    "receipt_item_id": int(receipt["id"]),
                                    "inventory_lot_id": int(lot["id"]),
                                    "trace_kind": "order_receipt_direct_finished_lot",
                                },
                                focus_terms=normalized_focus,
                            )
                        )
            for completion in completions.get(item_id, []):
                completion_id = int(completion["id"])
                lot_id = completion["inventory_lot_id"]
                lot = lots.get(int(lot_id)) if lot_id is not None else None
                completion_task = next(
                    (
                        row
                        for row in item_tasks.values()
                        if int(row["id"]) == int(completion["task_id"])
                    ),
                    None,
                )
                if completion_task is None:
                    findings.append(
                        _finding(
                            code="P015_TRACE_LINK_BROKEN",
                            severity="error",
                            order=order,
                            item=item,
                            key=anonymization_key,
                            summary="正式完工关联的生产任务不属于同一订单明细。",
                            evidence={
                                "trace_kind": "completion_task_item_mismatch",
                                "completion_id": completion_id,
                                "task_id": int(completion["task_id"]),
                            },
                            focus_terms=normalized_focus,
                        )
                    )
                source_lot_ids = completion_source_lots.get(completion_id, [])
                if len(source_lot_ids) > 1:
                    findings.append(
                        _finding(
                            code="P015_DUPLICATE_RECEIPT_FINISHED_OUTPUT",
                            severity="error",
                            order=order,
                            item=item,
                            key=anonymization_key,
                            summary="同一生产完工事实生成了多条成品批次。",
                            evidence={
                                "completion_id": completion_id,
                                "inventory_lot_ids": sorted(source_lot_ids),
                            },
                            focus_terms=normalized_focus,
                        )
                    )
                finished_detail = completion_finished_details.get(int(lot_id or 0))
                invalid_lot = (
                    lot is None
                    or lot["inventory_type"] != "finished"
                    or lot["source_ref_type"] != "production_completion"
                    or int(lot["source_ref_id"] or 0) != completion_id
                    or finished_detail is None
                    or (
                        not bool(finished_detail and finished_detail["is_general"])
                        and int(
                            (finished_detail or {}).get("owner_customer_id") or 0
                        )
                        != int(order.customer_id)
                    )
                    or (
                        completion_task is not None
                        and completion_task[
                            "sales_order_item_bom_component_id"
                        ]
                        is None
                        and int((finished_detail or {}).get("product_id") or 0)
                        != int(item.product_id)
                    )
                    or (
                        lot["status"] == "closed"
                        and not deliveries.get(item_id)
                    )
                )
                if invalid_lot:
                    findings.append(
                        _finding(
                            code="P015_COMPLETION_WITHOUT_ACTIVE_FINISHED_LOT",
                            severity="error",
                            order=order,
                            item=item,
                            key=anonymization_key,
                            summary="正式完工缺少与其一一对应的成品库存批次。",
                            evidence={
                                "completion_id": completion_id,
                                "inventory_lot_id": lot_id,
                            },
                            focus_terms=normalized_focus,
                        )
                    )
                elif not any(
                    movement["movement_type"] == "manual_in"
                    and int(movement["quantity"] or 0)
                    == int(completion["actual_output_quantity"] or 0)
                    and int(movement["related_order_id"] or 0) == int(order.id)
                    and int(movement["related_order_item_id"] or 0) == item_id
                    for movement in lot_movements.get(int(lot_id), [])
                ):
                    findings.append(
                        _finding(
                            code="P015_COMPLETION_WITHOUT_INVENTORY_MOVEMENT",
                            severity="error",
                            order=order,
                            item=item,
                            key=anonymization_key,
                            summary="正式完工成品批次缺少数量一致的入库流水。",
                            evidence={
                                "completion_id": completion_id,
                                "inventory_lot_id": int(lot_id),
                                "expected_quantity": int(
                                    completion["actual_output_quantity"] or 0
                                ),
                            },
                            focus_terms=normalized_focus,
                        )
                    )
                manual_in_count = sum(
                    movement["movement_type"] == "manual_in"
                    for movement in lot_movements.get(int(lot_id or 0), [])
                )
                if manual_in_count > 1:
                    findings.append(
                        _finding(
                            code="P015_DUPLICATE_RECEIPT_FINISHED_OUTPUT",
                            severity="error",
                            order=order,
                            item=item,
                            key=anonymization_key,
                            summary="同一完工批次存在多条初始成品入库流水。",
                            evidence={
                                "completion_id": completion_id,
                                "inventory_lot_id": lot_id,
                                "manual_in_count": manual_in_count,
                            },
                            focus_terms=normalized_focus,
                        )
                    )
            item_completions = completions.get(item_id, [])
            primary_completion_ids = sorted(
                int(row["id"])
                for row in item_completions
                if row["completion_type"] == "primary"
            )
            if len(primary_completion_ids) > 1:
                findings.append(
                    _finding(
                        code="P015_DUPLICATE_RECEIPT_FINISHED_OUTPUT",
                        severity="error",
                        order=order,
                        item=item,
                        key=anonymization_key,
                        summary="同一订单明细存在多条有效主完工事实。",
                        evidence={"primary_completion_ids": primary_completion_ids},
                        focus_terms=normalized_focus,
                    )
                )
            if has_receipt and None in item_tasks and item_completions:
                received_total = sum(
                    int(row["received_quantity"] or 0) for row in item_receipts
                )
                latest_receipt = max(item_receipts, key=lambda row: int(row["id"]))
                planned = max(
                    int(latest_receipt["planned_quantity"] or item.quantity or 0), 0
                )
                action = latest_receipt["resolution_action"]
                allowed_input = (
                    received_total
                    if action == "all_to_production"
                    else min(received_total, planned)
                )
                used_input = sum(
                    int(row["material_input_quantity"] or 0)
                    for row in item_completions
                )
                if used_input > allowed_input:
                    findings.append(
                        _finding(
                            code="P015_COMPLETION_INPUT_EXCEEDS_EFFECTIVE_RECEIPT",
                            severity="error",
                            order=order,
                            item=item,
                            key=anonymization_key,
                            summary="正式完工累计投入超过当前有效来料可用于生产的数量。",
                            evidence={
                                "received_quantity": received_total,
                                "allowed_production_input": allowed_input,
                                "completion_material_input": used_input,
                                "latest_resolution_action": action,
                            },
                            focus_terms=normalized_focus,
                        )
                    )
            for delivery_item in deliveries.get(item_id, []):
                delivery_item_id = int(delivery_item["id"])
                rows = allocations.get(delivery_item_id, [])
                if not rows:
                    findings.append(
                        _finding(
                            code="P015_TRACE_LINK_BROKEN",
                            severity="review",
                            order=order,
                            item=item,
                            key=anonymization_key,
                            summary="正式发货明细没有可追溯的库存分配；历史直送需人工核对。",
                            evidence={
                                "delivery_id": int(delivery_item["delivery_id"]),
                                "delivery_item_id": delivery_item_id,
                                "trace_kind": "dispatch_without_inventory_allocation",
                                "delivered_quantity": int(
                                    delivery_item["delivered_quantity"] or 0
                                ),
                            },
                            focus_terms=normalized_focus,
                        )
                    )
                    continue
                credited = sum(
                    int(row["credited_requirement_quantity"] or 0)
                    - int(row["reversed_requirement_quantity"] or 0)
                    for row in rows
                )
                delivered = effective_delivery_quantities.get(
                    delivery_item_id,
                    int(delivery_item["delivered_quantity"] or 0),
                )
                if credited != delivered:
                    findings.append(
                        _finding(
                            code="P015_DELIVERY_INVENTORY_QUANTITY_MISMATCH",
                            severity="error",
                            order=order,
                            item=item,
                            key=anonymization_key,
                            summary="发货数量与库存分配计入的订单需求数量不平。",
                            evidence={
                                "delivery_id": int(delivery_item["delivery_id"]),
                                "delivery_item_id": delivery_item_id,
                                "effective_delivered_quantity": delivered,
                                "net_credited_requirement_quantity": credited,
                            },
                            focus_terms=normalized_focus,
                        )
                    )
                for allocation in rows:
                    movement = allocation_movements.get(
                        int(allocation["consume_movement_id"])
                    )
                    if (
                        movement is None
                        or movement["movement_type"] != "consume"
                        or int(movement["quantity"] or 0)
                        != int(allocation["consumed_stock_quantity"] or 0)
                        or int(movement["related_delivery_id"] or 0)
                        != int(delivery_item["delivery_id"])
                        or int(movement["related_order_item_id"] or 0) != item_id
                    ):
                        findings.append(
                            _finding(
                                code="P015_TRACE_LINK_BROKEN",
                                severity="error",
                                order=order,
                                item=item,
                                key=anonymization_key,
                                summary="发货库存分配与实际扣减流水不一致。",
                                evidence={
                                    "delivery_item_id": delivery_item_id,
                                    "allocation_id": int(allocation["id"]),
                                    "consume_movement_id": int(
                                        allocation["consume_movement_id"]
                                    ),
                                },
                                focus_terms=normalized_focus,
                            )
                        )
            item_projection = projection["items"].get(item_id, {})
            mismatch = item_projection.get("business_status_evidence", {}).get(
                "delivery_counter_mismatch"
            )
            if mismatch:
                findings.append(
                    _finding(
                        code="P015_DELIVERY_INVENTORY_QUANTITY_MISMATCH",
                        severity="error",
                        order=order,
                        item=item,
                        key=anonymization_key,
                        summary="订单明细保存的已送数量与正式发货/回单事实不一致。",
                        evidence=dict(mismatch),
                        focus_terms=normalized_focus,
                    )
                )
            if (
                item_projection.get("business_status") == "pending_production"
                and not has_receipt
                and not any(
                    row["reservation_type"] in {"semi_order", "semi_requisition"}
                    and int(row["reserved_stock_quantity"] or 0)
                    > int(row["consumed_stock_quantity"] or 0)
                    + int(row["released_stock_quantity"] or 0)
                    for row in reservations.get(item_id, [])
                )
            ):
                findings.append(
                    _finding(
                        code="P015_PENDING_PRODUCTION_WITHOUT_INPUT_FACT",
                        severity="error",
                        order=order,
                        item=item,
                        key=anonymization_key,
                        summary="事实投影为待生产，但没有有效收料或半成品投入预占。",
                        evidence={
                            "persisted_material_status": str(item.material_status),
                            "derived_status": "pending_production",
                        },
                        focus_terms=normalized_focus,
                    )
                )
            for reservation in reservations.get(item_id, []):
                if reservation["reservation_type"] != "finished_order":
                    continue
                remaining_reserved = max(
                    int(reservation["reserved_stock_quantity"] or 0)
                    - int(reservation["consumed_stock_quantity"] or 0)
                    - int(reservation["released_stock_quantity"] or 0),
                    0,
                )
                if remaining_reserved <= 0:
                    continue
                lot_id = int(reservation["inventory_lot_id"])
                lot = reservation_lots.get(lot_id)
                detail = finished_details.get(lot_id)
                component_id = reservation[
                    "sales_order_item_bom_component_id"
                ]
                expected_product_id = (
                    component_products.get(int(component_id))
                    if component_id is not None
                    else int(item.product_id)
                )
                invalid = (
                    lot is None
                    or lot["inventory_type"] != "finished"
                    or lot["status"] != "active"
                    or int(lot["quantity_reserved"] or 0)
                    < int(reserved_by_lot[lot_id])
                    or detail is None
                    or expected_product_id is None
                    or int(detail["product_id"] or 0)
                    != int(expected_product_id or 0)
                    or (
                        not bool(detail["is_general"])
                        and int(detail["owner_customer_id"] or 0)
                        != int(order.customer_id)
                    )
                )
                if invalid:
                    findings.append(
                        _finding(
                            code="P015_FINISHED_STOCK_NOT_DELIVERABLE",
                            severity="error",
                            order=order,
                            item=item,
                            key=anonymization_key,
                            summary="有效成品预占仍有余额，但其批次身份或库存余额无法支持送货。",
                            evidence={
                                "reservation_id": int(reservation["id"]),
                                "inventory_lot_id": lot_id,
                                "remaining_reserved_quantity": remaining_reserved,
                            },
                            focus_terms=normalized_focus,
                        )
                    )
            if product is not None:
                label_enabled = bool(product["production_label_enabled"])
                units = product["production_label_units_per_label"]
                plate_ids = [
                    product["printing_plate_1_id"],
                    product["printing_plate_2_id"],
                    product["printing_plate_3_id"],
                ]
                invalid_reasons: list[str] = []
                if label_enabled and (units is None or int(units) <= 0):
                    invalid_reasons.append("label_enabled_without_positive_units")
                if not label_enabled and units is not None:
                    invalid_reasons.append("label_disabled_with_units")
                if product["printing_plate_mode"] == "no_plate" and any(
                    value is not None for value in plate_ids
                ):
                    invalid_reasons.append("no_plate_with_binding")
                if (
                    item.supply_mode_snapshot != "external_purchase"
                    and product["pieces_per_box"] is not None
                    and int(product["pieces_per_box"]) <= 0
                ):
                    invalid_reasons.append("non_positive_pieces_per_box")
                if invalid_reasons:
                    findings.append(
                        _finding(
                            code="P015_COMMON_BOX_PERSISTED_CONTRACT_INVALID",
                            severity="error",
                            order=order,
                            item=item,
                            key=anonymization_key,
                            summary="产品主档存在自相矛盾的生产或标签持久事实。",
                            evidence={"invalid_reasons": invalid_reasons},
                            focus_terms=normalized_focus,
                        )
                    )
                if product["printing_plate_mode"] == "no_plate":
                    try:
                        normalize_printing_colors(
                            product["print_content"], product["printing_colors"]
                        )
                    except PrintingColorError as exc:
                        findings.append(
                            _finding(
                                code="P015_COMMON_BOX_PRINT_COLOR_INVALID",
                                severity="review",
                                order=order,
                                item=item,
                                key=anonymization_key,
                                summary="当前产品的直接印刷颜色不满足已保存的印刷情况。",
                                evidence={"validation_message": str(exc)},
                                focus_terms=normalized_focus,
                            )
                        )
                if not has_receipt:
                    drift_task_ids = sorted(
                        int(row["id"])
                        for row in item_tasks.values()
                        if bool(row["production_label_enabled_snapshot"])
                        != label_enabled
                        or row["production_label_units_per_label_snapshot"] != units
                    )
                    if drift_task_ids:
                        findings.append(
                            _finding(
                                code="P015_COMMON_BOX_VERSION_SNAPSHOT_DRIFT",
                                severity="review",
                                order=order,
                                item=item,
                                key=anonymization_key,
                                summary="首次收料前，生产任务标签快照与当前产品主档不同。",
                                evidence={"production_task_ids": drift_task_ids},
                                focus_terms=normalized_focus,
                            )
                        )

    findings.sort(
        key=lambda row: (
            row.code,
            row.order_ref,
            row.order_item_ref or "",
            str(row.evidence),
        )
    )
    unknown_codes = sorted({row.code for row in findings} - FINDING_CODES)
    if unknown_codes:
        raise RuntimeError(
            "unregistered P0-15 finding codes: " + ", ".join(unknown_codes)
        )
    severity_counts = Counter(row.severity for row in findings)
    code_counts = Counter(row.code for row in findings)
    focus_findings = sum(row.focus_match is not None for row in findings)
    focus_tokens = sorted(
        "FOCUS-"
        + hmac.new(anonymization_key, term.encode("utf-8"), hashlib.sha256)
        .hexdigest()[:12]
        .upper()
        for term in normalized_focus
    )
    workstation_routes.sort(
        key=lambda row: (
            row["order_item_ref"],
            row["task_ref"],
            row["component_ref"] or "",
        )
    )
    focus_routes = sum(row["focus_match"] is not None for row in workstation_routes)
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": (
            generated_at or datetime.now(timezone.utc)
        ).isoformat(timespec="seconds"),
        "scope": {
            "unfinished_order_count": len(orders),
            "unfinished_order_item_count": scanned_items,
            "focus_term_count": len(normalized_focus),
            "focus_tokens": focus_tokens,
        },
        "coverage": {
            "purchase_purpose_allocation": {
                "status": "evaluated",
                "active_formal_source_count": int(
                    active_formal_purpose_source_count
                ),
                "purpose_snapshot_count": int(purchase_purpose_snapshot_count),
            },
            "receipt_auto_finished": {
                "status": "evaluated",
                "active_purpose_allocation_count": int(
                    active_receipt_purpose_allocation_count
                ),
                "reversed_purpose_allocation_count": int(
                    reversed_receipt_purpose_allocation_count
                ),
            },
            "workstation_membership": {
                "status": "evaluated",
                "rule_version": PRODUCTION_STATION_ROUTING_RULE_VERSION,
                "eligible_task_count": workstation_eligible_tasks,
                "station_task_counts": {
                    station: int(workstation_station_counts.get(station, 0))
                    for station in ("printing", "die_cut")
                },
                "dual_route_task_count": workstation_dual_route_tasks,
                "unrouted_task_count": workstation_unrouted_tasks,
            },
            "common_box_api_round_trip": {
                "status": "evaluated_by_regression_contract",
                "reason": "anonymous_api_round_trip_tests_plus_persisted_fact_scan",
            },
            "warehouse_map_stocktake_chain": {
                "status": "evaluated_by_regression_contract",
                "reason": "p0_14_p1_85_p1_86_p1_87_isolated_contracts",
            },
            "current_chain_facts": {"status": "evaluated"},
        },
        "finding_code_registry": sorted(FINDING_CODES),
        "workstation_routes": workstation_routes,
        "summary": {
            "scan_complete": True,
            "finding_count": len(findings),
            "focus_finding_count": focus_findings,
            "focus_route_count": focus_routes,
            "severity_counts": dict(sorted(severity_counts.items())),
            "code_counts": dict(sorted(code_counts.items())),
        },
        "findings": [row.to_dict() for row in findings],
    }
