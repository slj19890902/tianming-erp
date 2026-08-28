"""P1-81 receipt-purpose distribution, automatic posting and exact reversal.

All functions participate in the caller's transaction.  They never commit.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN, ROUND_HALF_UP
from hashlib import sha256
import json
from typing import Any

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import Session

from app.core.time_contract import beijing_today, utc_now_naive
from app.models.customer import Customer
from app.models.incoming_receipt import IncomingReceiptItem
from app.models.material import Material
from app.models.order import OrderItem
from app.models.product_bom import RequisitionItemBomSource
from app.models.purchase_receipt import (
    IncomingReceiptPurposeAllocation,
    IncomingReceiptPurposeReversal,
    ProductionCompletionReserveConversion,
    ProductionCompletionReserveConversionReversal,
    PurchaseReceiptFact,
)
from app.models.requisition import Requisition, RequisitionItem
from app.models.supplier_requisition_order import (
    PurchasePurposeSourceSnapshot,
    SupplierRequisitionOrder,
    SupplierRequisitionOrderItem,
)
from app.models.warehouse_inventory import (
    InventoryLot,
    InventoryMovement,
    WarehouseArea,
    WarehouseFloor,
    WarehouseLocation,
)
from app.services.production_workflow import (
    ProductionWorkflowError,
    ensure_receipt_auto_main_task,
    post_automatic_receipt_completion,
    reverse_automatic_receipt_completion,
)
from app.services.purchase_receipt_facts import (
    calculate_per_sheet_cost,
    canonical_purchase_receipt_hash,
    material_calculation_fingerprint,
)
from app.services.warehouse_inventory import (
    WarehouseInventoryError,
    automatic_raw_material_staging_location,
    consume_available_semi_finished_lot,
    manual_semi_finished_in,
    mutate_lot,
    restore_consumed_semi_finished_lot,
)
from app.services.warehouse_location_address import employee_location_name


MONEY = Decimal("0.0001")


class ReceiptPurposeFlowError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class ReceiptPurposeContext:
    source: SupplierRequisitionOrderItem | RequisitionItem
    snapshot: PurchasePurposeSourceSnapshot
    receipt_fact: PurchaseReceiptFact


@dataclass(frozen=True, slots=True)
class ReserveConversionPosting:
    receipt_purpose_allocation_id: int
    semi_finished_inventory_lot_id: int
    semi_consume_movement_id: int
    converted_sheet_quantity: int
    finished_quantity_delta: int
    supported_finished_quantity_before: int
    supported_finished_quantity_after: int
    idempotency_key: str
    request_hash: str


@dataclass(frozen=True, slots=True)
class ReserveConversionReversalPosting:
    production_completion_reserve_conversion_id: int
    semi_finished_inventory_lot_id: int
    semi_reverse_movement_id: int
    restored_sheet_quantity: int
    reversed_finished_quantity_delta: int
    supported_finished_quantity_before: int
    supported_finished_quantity_after: int
    idempotency_key: str
    request_hash: str


def _formal_source(target: Any) -> SupplierRequisitionOrderItem | RequisitionItem | None:
    if target.supplier_order_item is not None:
        return target.supplier_order_item
    if target.requisition_item is not None:
        return target.requisition_item
    return None


def _snapshot_filter(source: SupplierRequisitionOrderItem | RequisitionItem):
    if isinstance(source, SupplierRequisitionOrderItem):
        return (
            PurchasePurposeSourceSnapshot.supplier_requisition_order_item_id
            == source.id
        )
    return PurchasePurposeSourceSnapshot.material_requisition_item_id == source.id


def resolve_receipt_purpose_context(
    db: Session,
    *,
    target: Any,
    expected_receipt_fact_version: int | None,
    purchase_purpose_source_snapshot_id: int | None,
    expected_purpose_snapshot_version: int | None,
    receipt_plan_fingerprint: str | None,
    expected_actual_material_version: int | None,
    actual_material_fingerprint: str | None,
) -> ReceiptPurposeContext | None:
    """Return a verified modern contract or None for a genuine legacy source."""

    source = _formal_source(target)
    if source is None:
        return None
    snapshots = list(
        db.scalars(
            select(PurchasePurposeSourceSnapshot)
            .where(_snapshot_filter(source))
            .order_by(PurchasePurposeSourceSnapshot.id)
        ).all()
    )
    marker = str(getattr(source, "purpose_contract_status", "legacy_unset"))
    if marker == "legacy_unset":
        if snapshots:
            raise ReceiptPurposeFlowError(
                "PURCHASE_PURPOSE_SNAPSHOT_INVALID",
                "采购来源已有用途快照但正式标记缺失，已停止收料。",
            )
        return None
    if marker != "frozen" or purchase_purpose_source_snapshot_id is None:
        raise ReceiptPurposeFlowError(
            "PURCHASE_PURPOSE_SNAPSHOT_INVALID",
            "正式采购用途快照缺失、重复或损坏，已停止收料。",
        )
    matching = [
        row for row in snapshots
        if int(row.id) == int(purchase_purpose_source_snapshot_id)
    ]
    if len(matching) != 1:
        raise ReceiptPurposeFlowError(
            "PURCHASE_PURPOSE_SNAPSHOT_INVALID",
            "指定采购用途快照不存在、重复或不属于本报料明细，已停止收料。",
        )
    snapshot = matching[0]
    if snapshot.source_key != str(getattr(source, "source_key", None) or snapshot.source_key):
        raise ReceiptPurposeFlowError(
            "PURCHASE_PURPOSE_SNAPSHOT_INVALID",
            "正式采购来源与用途快照不一致，已停止收料。",
        )
    facts = list(
        db.scalars(
            select(PurchaseReceiptFact)
            .where(
                PurchaseReceiptFact.supplier_requisition_order_item_id
                == (
                    source.id
                    if isinstance(source, SupplierRequisitionOrderItem)
                    else None
                ),
                PurchaseReceiptFact.material_requisition_item_id
                == (source.id if isinstance(source, RequisitionItem) else None),
                PurchaseReceiptFact.purchase_purpose_source_snapshot_id == snapshot.id,
            )
            .order_by(PurchaseReceiptFact.receipt_fact_version.desc())
        ).all()
    )
    if not facts:
        raise ReceiptPurposeFlowError(
            "PURCHASE_RECEIPT_FACT_REQUIRED",
            "请先在电脑端确认实际材质和正式采购价格，再办理收料。",
        )
    fact = facts[0]
    if (
        expected_receipt_fact_version is None
        or int(expected_receipt_fact_version) != int(fact.receipt_fact_version)
        or int(fact.expected_source_version) != int(source.version)
    ):
        raise ReceiptPurposeFlowError(
            "PURCHASE_RECEIPT_FACT_STALE",
            "正式采购价格或报料来源版本已变化，请刷新后重试。",
        )
    if (
        expected_purpose_snapshot_version is None
        or int(expected_purpose_snapshot_version) != int(snapshot.snapshot_version)
        or int(fact.purpose_snapshot_version) != int(snapshot.snapshot_version)
    ):
        raise ReceiptPurposeFlowError(
            "PURCHASE_PURPOSE_SNAPSHOT_STALE",
            "采购用途版本已变化，请刷新后重试。",
        )
    submitted_fingerprint = str(receipt_plan_fingerprint or "").strip().lower()
    if (
        len(submitted_fingerprint) != 64
        or submitted_fingerprint != str(fact.receipt_plan_fingerprint).lower()
        or fact.purchase_purpose_source_snapshot_id != snapshot.id
    ):
        raise ReceiptPurposeFlowError(
            "INCOMING_RECEIPT_PLAN_TAMPERED",
            "收料用途指纹不一致，请刷新后重试。",
        )
    material = db.get(Material, fact.actual_material_id)
    if material is None or not material.is_active:
        raise ReceiptPurposeFlowError(
            "ACTUAL_MATERIAL_INVALID",
            "已确认的实际材质不存在或已停用，已停止收料。",
        )
    submitted_material_fingerprint = str(actual_material_fingerprint or "").strip().lower()
    live_material_fingerprint = material_calculation_fingerprint(material)
    if (
        expected_actual_material_version is None
        or int(expected_actual_material_version) != int(fact.actual_material_version)
        or int(material.version) != int(fact.actual_material_version)
        or len(submitted_material_fingerprint) != 64
        or submitted_material_fingerprint != str(fact.actual_material_fingerprint).lower()
        or live_material_fingerprint != str(fact.actual_material_fingerprint).lower()
    ):
        raise ReceiptPurposeFlowError(
            "ACTUAL_MATERIAL_FACT_STALE",
            "实际材质主档版本、楞型或层数已变化，请重新确认材质和价格后再收料。",
        )
    return ReceiptPurposeContext(source=source, snapshot=snapshot, receipt_fact=fact)


def _active_allocation_statement():
    return select(IncomingReceiptPurposeAllocation).where(
        ~exists(
            select(IncomingReceiptPurposeReversal.id).where(
                IncomingReceiptPurposeReversal.incoming_receipt_purpose_allocation_id
                == IncomingReceiptPurposeAllocation.id
            )
        )
    )


def _active_source_allocations(
    db: Session,
    snapshot_id: int,
) -> list[IncomingReceiptPurposeAllocation]:
    return list(
        db.scalars(
            _active_allocation_statement()
            .where(
                IncomingReceiptPurposeAllocation.purchase_purpose_source_snapshot_id
                == snapshot_id
            )
            .order_by(IncomingReceiptPurposeAllocation.id)
        ).all()
    )


def receipt_purpose_source_totals(
    db: Session,
    snapshot_id: int,
) -> tuple[int, int, int]:
    """Return active received, finished-purpose and reserve-purpose sheets."""

    rows = _active_source_allocations(db, int(snapshot_id))
    return (
        sum(int(row.receipt_total_sheet_qty or 0) for row in rows),
        sum(int(row.receipt_order_purpose_sheet_qty or 0) for row in rows),
        sum(int(row.receipt_reserve_purpose_sheet_qty or 0) for row in rows),
    )


def _order_item_snapshots(
    db: Session,
    order_item_id: int,
) -> list[PurchasePurposeSourceSnapshot]:
    return list(
        db.scalars(
            select(PurchasePurposeSourceSnapshot)
            .outerjoin(
                SupplierRequisitionOrderItem,
                SupplierRequisitionOrderItem.id
                == PurchasePurposeSourceSnapshot.supplier_requisition_order_item_id,
            )
            .outerjoin(
                RequisitionItem,
                RequisitionItem.id
                == PurchasePurposeSourceSnapshot.material_requisition_item_id,
            )
            .where(
                or_(
                    SupplierRequisitionOrderItem.order_item_id == order_item_id,
                    RequisitionItem.order_item_id == order_item_id,
                )
            )
            .order_by(PurchasePurposeSourceSnapshot.id)
        ).unique().all()
    )


def _active_order_item_allocations(
    db: Session,
    order_item_id: int,
) -> list[IncomingReceiptPurposeAllocation]:
    return list(
        db.scalars(
            _active_allocation_statement()
            .outerjoin(
                SupplierRequisitionOrderItem,
                SupplierRequisitionOrderItem.id
                == IncomingReceiptPurposeAllocation.supplier_requisition_order_item_id,
            )
            .outerjoin(
                RequisitionItem,
                RequisitionItem.id
                == IncomingReceiptPurposeAllocation.material_requisition_item_id,
            )
            .where(
                or_(
                    SupplierRequisitionOrderItem.order_item_id == order_item_id,
                    RequisitionItem.order_item_id == order_item_id,
                )
            )
            .order_by(IncomingReceiptPurposeAllocation.id)
        ).unique().all()
    )


def _component_key(db: Session, snapshot: PurchasePurposeSourceSnapshot) -> str:
    if snapshot.source_bom_requisition_source_id is not None:
        bom_source = db.get(
            RequisitionItemBomSource, snapshot.source_bom_requisition_source_id
        )
        if bom_source is None:
            raise ReceiptPurposeFlowError(
                "BOM_COMPONENT_IDENTITY_MISSING",
                "组合产品报料来源缺少稳定BOM组件身份，已停止收料。",
            )
        snapshot_component = str(snapshot.component_type or "").strip().lower()
        source_component = str(bom_source.component_type or "").strip().lower()
        if snapshot_component != source_component:
            raise ReceiptPurposeFlowError(
                "BOM_COMPONENT_IDENTITY_MISMATCH",
                "组合产品用途快照与稳定BOM组件类型不一致，已停止收料。",
            )
        return (
            f"bom:{bom_source.sales_order_item_bom_component_id}:"
            f"{source_component}"
        )
    component = str(snapshot.component_type or "whole").strip().lower()
    return component if component in {"whole", "cover", "base"} else "whole"


def _finished_capacity(
    db: Session,
    snapshots: list[PurchasePurposeSourceSnapshot],
    order_sheet_deltas: dict[int, int],
) -> int:
    by_component: dict[str, Decimal] = {}
    for snapshot in snapshots:
        key = _component_key(db, snapshot)
        sheets = max(int(order_sheet_deltas.get(snapshot.id, 0)), 0)
        pieces_per_finished = max(int(snapshot.pieces_per_finished_snapshot or 1), 1)
        capacity = (
            Decimal(sheets * int(snapshot.yield_per_sheet_snapshot or 1))
            / Decimal(pieces_per_finished)
        )
        by_component[key] = by_component.get(key, Decimal("0")) + capacity
    if not by_component:
        return 0
    has_required_components = (
        any(key.startswith("bom:") for key in by_component)
        or {"cover", "base"}.intersection(by_component)
    )
    value = min(by_component.values()) if has_required_components else sum(by_component.values())
    return int(value.to_integral_value(rounding=ROUND_DOWN))


def receipt_purpose_finished_capacity(
    db: Session,
    snapshots: list[PurchasePurposeSourceSnapshot],
    order_sheets_by_snapshot: dict[int, int],
) -> int:
    """Project authoritative finished capacity without posting inventory."""

    return _finished_capacity(db, snapshots, order_sheets_by_snapshot)


def _conversion_key(raw: str) -> str:
    value = str(raw).strip()
    if len(value) <= 120:
        return value
    digest = sha256(value.encode("utf-8")).hexdigest()[:24]
    return f"{value[:95]}:{digest}"


def _component_capacities(
    db: Session,
    snapshots: list[PurchasePurposeSourceSnapshot],
    sheets_by_snapshot: dict[int, int],
) -> dict[str, Decimal]:
    capacities: dict[str, Decimal] = {}
    for snapshot in snapshots:
        key = _component_key(db, snapshot)
        sheets = max(int(sheets_by_snapshot.get(int(snapshot.id), 0)), 0)
        per_sheet = Decimal(max(int(snapshot.yield_per_sheet_snapshot or 1), 1)) / Decimal(
            max(int(snapshot.pieces_per_finished_snapshot or 1), 1)
        )
        capacities[key] = capacities.get(key, Decimal("0")) + Decimal(sheets) * per_sheet
    return capacities


def consume_receipt_reserve_for_completion_adjustment(
    db: Session,
    *,
    production_completion_id: int,
    desired_completion_quantity: int,
    operator_id: int | None,
    idempotency_key: str,
) -> list[ReserveConversionPosting]:
    """Convert receipt reserve sheets only when receipt-auto output needs them.

    Planning is completed before the first write.  Every physical sheet
    consumption then uses a versioned inventory update and an immutable
    conversion fact created by the production caller in the same transaction.
    """

    from app.models.production import ProductionCompletion

    completion = db.get(ProductionCompletion, int(production_completion_id))
    if completion is None or completion.status != "posted":
        raise ReceiptPurposeFlowError(
            "PRODUCTION_COMPLETION_INVALID",
            "生产完工记录不存在或已撤销，不能把备库片料转为成品。",
        )
    if completion.origin != "receipt_auto":
        return []
    order_item_id = int(completion.order_item_id)
    snapshots = _order_item_snapshots(db, order_item_id)
    allocations = _active_order_item_allocations(db, order_item_id)
    allocation_ids = [int(row.id) for row in allocations]
    if not snapshots or not allocations:
        raise ReceiptPurposeFlowError(
            "RECEIPT_PURPOSE_SUPPORT_MISSING",
            "该完工记录缺少可核对的来料用途事实，已停止修改成品数量。",
        )
    sheets_by_snapshot: dict[int, int] = {}
    for allocation in allocations:
        snapshot_id = int(allocation.purchase_purpose_source_snapshot_id or 0)
        sheets_by_snapshot[snapshot_id] = sheets_by_snapshot.get(snapshot_id, 0) + int(
            allocation.receipt_order_purpose_sheet_qty or 0
        )
    owner_allocation = next(
        (
            row
            for row in allocations
            if int(row.production_completion_id or 0) == int(completion.id)
        ),
        None,
    )
    if owner_allocation is None:
        raise ReceiptPurposeFlowError(
            "RECEIPT_PURPOSE_SUPPORT_MISSING",
            "该完工记录缺少对应的本次来料用途事实，不能自动使用其它批次备库。",
        )
    receipt_item_ids = [int(row.incoming_receipt_item_id) for row in allocations]
    receipt_batch_by_item_id = {
        int(row.id): int(row.receipt_id)
        for row in db.scalars(
            select(IncomingReceiptItem).where(
                IncomingReceiptItem.id.in_(receipt_item_ids)
            )
        ).all()
    }
    owner_receipt_batch_id = receipt_batch_by_item_id.get(
        int(owner_allocation.incoming_receipt_item_id)
    )
    if owner_receipt_batch_id is None:
        raise ReceiptPurposeFlowError(
            "RECEIPT_PURPOSE_SUPPORT_MISSING",
            "该完工记录缺少稳定的来料批次关联，不能自动使用备库片料。",
        )
    prior_conversions = list(
        db.scalars(
            select(ProductionCompletionReserveConversion).where(
                ProductionCompletionReserveConversion.receipt_purpose_allocation_id.in_(
                    allocation_ids
                )
            )
        ).all()
    )
    conversion_ids = [int(row.id) for row in prior_conversions]
    reversed_by_conversion: dict[int, int] = {}
    if conversion_ids:
        for conversion_id, restored in db.execute(
            select(
                ProductionCompletionReserveConversionReversal.production_completion_reserve_conversion_id,
                func.coalesce(
                    func.sum(
                        ProductionCompletionReserveConversionReversal.restored_sheet_quantity
                    ),
                    0,
                ),
            )
            .where(
                ProductionCompletionReserveConversionReversal.production_completion_reserve_conversion_id.in_(
                    conversion_ids
                )
            )
            .group_by(
                ProductionCompletionReserveConversionReversal.production_completion_reserve_conversion_id
            )
        ).all():
            reversed_by_conversion[int(conversion_id)] = int(restored or 0)
    allocation_by_id = {int(row.id): row for row in allocations}
    for conversion in prior_conversions:
        allocation = allocation_by_id.get(int(conversion.receipt_purpose_allocation_id))
        if allocation is None:
            continue
        snapshot_id = int(allocation.purchase_purpose_source_snapshot_id or 0)
        active_converted = max(
            int(conversion.converted_sheet_quantity or 0)
            - reversed_by_conversion.get(int(conversion.id), 0),
            0,
        )
        sheets_by_snapshot[snapshot_id] = (
            sheets_by_snapshot.get(snapshot_id, 0) + active_converted
        )
    supported_before = _finished_capacity(db, snapshots, sheets_by_snapshot)
    other_actual = int(
        db.scalar(
            select(
                func.coalesce(func.sum(ProductionCompletion.actual_output_quantity), 0)
            ).where(
                ProductionCompletion.order_item_id == order_item_id,
                ProductionCompletion.status == "posted",
                ProductionCompletion.origin == "receipt_auto",
                ProductionCompletion.id != completion.id,
            )
        )
        or 0
    )
    desired_total = other_actual + int(desired_completion_quantity)
    if desired_total <= supported_before:
        return []

    snapshots_by_id = {int(row.id): row for row in snapshots}
    candidates: list[
        tuple[
            IncomingReceiptPurposeAllocation,
            PurchasePurposeSourceSnapshot,
            InventoryLot,
            str,
            Decimal,
        ]
    ] = []
    for allocation in allocations:
        if (
            allocation.surplus_disposition != "semi_finished_reserve"
            or allocation.semi_finished_inventory_lot_id is None
            or receipt_batch_by_item_id.get(int(allocation.incoming_receipt_item_id))
            != owner_receipt_batch_id
        ):
            continue
        snapshot = snapshots_by_id.get(
            int(allocation.purchase_purpose_source_snapshot_id or 0)
        )
        lot = db.get(InventoryLot, int(allocation.semi_finished_inventory_lot_id))
        if snapshot is None or lot is None or int(lot.quantity_available or 0) <= 0:
            continue
        per_sheet = Decimal(max(int(snapshot.yield_per_sheet_snapshot or 1), 1)) / Decimal(
            max(int(snapshot.pieces_per_finished_snapshot or 1), 1)
        )
        candidates.append(
            (allocation, snapshot, lot, _component_key(db, snapshot), per_sheet)
        )
    candidates.sort(key=lambda row: int(row[0].id))

    planned: list[
        tuple[
            IncomingReceiptPurposeAllocation,
            PurchasePurposeSourceSnapshot,
            InventoryLot,
            int,
        ]
    ] = []
    projected_sheets = dict(sheets_by_snapshot)
    component_capacities = _component_capacities(db, snapshots, projected_sheets)
    required_components = bool(
        any(key.startswith("bom:") for key in component_capacities)
        or {"cover", "base"}.intersection(component_capacities)
    )

    def plan_from_candidates(
        rows: list[
            tuple[
                IncomingReceiptPurposeAllocation,
                PurchasePurposeSourceSnapshot,
                InventoryLot,
                str,
                Decimal,
            ]
        ],
        missing_capacity: Decimal,
    ) -> Decimal:
        missing = max(missing_capacity, Decimal("0"))
        for allocation, snapshot, lot, _key, per_sheet in rows:
            if missing <= 0:
                break
            available = max(int(lot.quantity_available or 0), 0)
            needed = int((missing / per_sheet).to_integral_value(rounding=ROUND_CEILING))
            take = min(available, needed)
            if take <= 0:
                continue
            planned.append((allocation, snapshot, lot, take))
            projected_sheets[int(snapshot.id)] = projected_sheets.get(int(snapshot.id), 0) + take
            missing -= Decimal(take) * per_sheet
        return max(missing, Decimal("0"))

    target_capacity = Decimal(desired_total)
    if required_components:
        for component_key in sorted(component_capacities):
            missing = target_capacity - component_capacities.get(
                component_key, Decimal("0")
            )
            if missing <= 0:
                continue
            remaining = plan_from_candidates(
                [row for row in candidates if row[3] == component_key], missing
            )
            if remaining > 0:
                raise ReceiptPurposeFlowError(
                    "RECEIPT_RESERVE_INSUFFICIENT_FOR_FINISHED_ADJUSTMENT",
                    "同一来料批次的备库片料不足，不能把成品实收修改到该数量。",
                )
    else:
        current_capacity = sum(component_capacities.values(), Decimal("0"))
        remaining = plan_from_candidates(candidates, target_capacity - current_capacity)
        if remaining > 0:
            raise ReceiptPurposeFlowError(
                "RECEIPT_RESERVE_INSUFFICIENT_FOR_FINISHED_ADJUSTMENT",
                "同一来料批次的备库片料不足，不能把成品实收修改到该数量。",
            )

    supported_after_plan = _finished_capacity(db, snapshots, projected_sheets)
    if supported_after_plan < desired_total:
        raise ReceiptPurposeFlowError(
            "RECEIPT_RESERVE_INSUFFICIENT_FOR_FINISHED_ADJUSTMENT",
            f"备库片料换算后最多支持 {supported_after_plan} 个成品，不能修改为 {desired_total}。",
        )
    order_item = db.get(OrderItem, order_item_id)
    if order_item is None:
        raise ReceiptPurposeFlowError(
            "ORDER_ITEM_MISSING", "生产完工关联的订单明细不存在，已停止修改。"
        )

    postings: list[ReserveConversionPosting] = []
    running_sheets = dict(sheets_by_snapshot)
    for allocation, snapshot, lot, take in planned:
        step_before = _finished_capacity(db, snapshots, running_sheets)
        running_sheets[int(snapshot.id)] = running_sheets.get(int(snapshot.id), 0) + take
        step_after = _finished_capacity(db, snapshots, running_sheets)
        movement_key = _conversion_key(
            f"{idempotency_key}:receipt-reserve:{int(allocation.id)}"
        )
        try:
            movement = consume_available_semi_finished_lot(
                db,
                lot_id=int(lot.id),
                quantity=int(take),
                expected_version=int(lot.version),
                operator_id=operator_id,
                idempotency_key=movement_key,
                related_order_id=int(order_item.order_id),
                related_order_item_id=order_item_id,
                reason="生产实收增加，自动扣减同批次片料备库转作成品原料",
            )
        except WarehouseInventoryError as error:
            raise ReceiptPurposeFlowError(
                "RECEIPT_RESERVE_CONSUME_FAILED", str(error), error.status_code
            ) from error
        request_hash = canonical_purchase_receipt_hash(
            {
                "production_completion_id": int(completion.id),
                "receipt_purpose_allocation_id": int(allocation.id),
                "semi_finished_inventory_lot_id": int(lot.id),
                "converted_sheet_quantity": int(take),
                "desired_completion_quantity": int(desired_completion_quantity),
                "supported_finished_quantity_before": int(step_before),
                "supported_finished_quantity_after": int(step_after),
                "idempotency_key": movement_key,
            }
        )
        postings.append(
            ReserveConversionPosting(
                receipt_purpose_allocation_id=int(allocation.id),
                semi_finished_inventory_lot_id=int(lot.id),
                semi_consume_movement_id=int(movement.id),
                converted_sheet_quantity=int(take),
                finished_quantity_delta=int(step_after - step_before),
                supported_finished_quantity_before=int(step_before),
                supported_finished_quantity_after=int(step_after),
                idempotency_key=movement_key,
                request_hash=request_hash,
            )
        )
    return postings


def restore_receipt_reserve_after_completion_reduction(
    db: Session,
    *,
    production_completion_id: int,
    desired_completion_quantity: int,
    operator_id: int | None,
    idempotency_key: str,
    reason_type: str,
) -> list[ReserveConversionReversalPosting]:
    """Restore only the converted sheets no longer needed by finished output."""

    from app.models.production import ProductionCompletion

    if reason_type not in {"actual_adjustment", "completion_reversal"}:
        raise ReceiptPurposeFlowError(
            "RECEIPT_RESERVE_RESTORE_REASON_INVALID",
            "备库恢复原因无效，操作已停止。",
            400,
        )
    completion = db.get(ProductionCompletion, int(production_completion_id))
    if completion is None or completion.status != "posted":
        raise ReceiptPurposeFlowError(
            "PRODUCTION_COMPLETION_INVALID",
            "生产完工记录不存在或已撤销，不能恢复备库片料。",
        )
    if completion.origin != "receipt_auto":
        return []
    order_item_id = int(completion.order_item_id)
    desired = max(int(desired_completion_quantity), 0)
    snapshots = _order_item_snapshots(db, order_item_id)
    allocations = _active_order_item_allocations(db, order_item_id)
    allocation_by_id = {int(row.id): row for row in allocations}
    if not snapshots or not allocation_by_id:
        raise ReceiptPurposeFlowError(
            "RECEIPT_PURPOSE_SUPPORT_MISSING",
            "该完工记录缺少可核对的来料用途事实，不能自动恢复备库。",
        )
    conversions = list(
        db.scalars(
            select(ProductionCompletionReserveConversion)
            .where(
                ProductionCompletionReserveConversion.receipt_purpose_allocation_id.in_(
                    list(allocation_by_id)
                )
            )
            .order_by(ProductionCompletionReserveConversion.id)
        ).all()
    )
    if not conversions:
        return []
    conversion_ids = [int(row.id) for row in conversions]
    restored_by_conversion: dict[int, int] = {}
    for conversion_id, restored in db.execute(
        select(
            ProductionCompletionReserveConversionReversal.production_completion_reserve_conversion_id,
            func.coalesce(
                func.sum(
                    ProductionCompletionReserveConversionReversal.restored_sheet_quantity
                ),
                0,
            ),
        )
        .where(
            ProductionCompletionReserveConversionReversal.production_completion_reserve_conversion_id.in_(
                conversion_ids
            )
        )
        .group_by(
            ProductionCompletionReserveConversionReversal.production_completion_reserve_conversion_id
        )
    ).all():
        restored_by_conversion[int(conversion_id)] = int(restored or 0)

    sheets_by_snapshot: dict[int, int] = {}
    for allocation in allocations:
        snapshot_id = int(allocation.purchase_purpose_source_snapshot_id or 0)
        sheets_by_snapshot[snapshot_id] = (
            sheets_by_snapshot.get(snapshot_id, 0)
            + int(allocation.receipt_order_purpose_sheet_qty or 0)
        )
    active_by_conversion: dict[int, int] = {}
    for conversion in conversions:
        allocation = allocation_by_id.get(
            int(conversion.receipt_purpose_allocation_id)
        )
        if allocation is None:
            continue
        active = max(
            int(conversion.converted_sheet_quantity or 0)
            - restored_by_conversion.get(int(conversion.id), 0),
            0,
        )
        active_by_conversion[int(conversion.id)] = active
        snapshot_id = int(allocation.purchase_purpose_source_snapshot_id or 0)
        sheets_by_snapshot[snapshot_id] = sheets_by_snapshot.get(snapshot_id, 0) + active

    other_actual = int(
        db.scalar(
            select(
                func.coalesce(func.sum(ProductionCompletion.actual_output_quantity), 0)
            ).where(
                ProductionCompletion.order_item_id == order_item_id,
                ProductionCompletion.status == "posted",
                ProductionCompletion.origin == "receipt_auto",
                ProductionCompletion.id != completion.id,
            )
        )
        or 0
    )
    desired_total = other_actual + desired
    supported_before = _finished_capacity(db, snapshots, sheets_by_snapshot)
    if supported_before < desired_total:
        raise ReceiptPurposeFlowError(
            "RECEIPT_PURPOSE_SUPPORT_MISSING",
            "当前来料用途最多支持的成品数少于拟保留成品数，不能自动恢复备库。",
        )

    planned: list[
        tuple[
            ProductionCompletionReserveConversion,
            IncomingReceiptPurposeAllocation,
            InventoryLot,
            int,
            int,
            int,
        ]
    ] = []
    projected_sheets = dict(sheets_by_snapshot)
    for conversion in reversed(conversions):
        if int(conversion.production_completion_id) != int(completion.id):
            continue
        active = active_by_conversion.get(int(conversion.id), 0)
        if active <= 0:
            continue
        allocation = allocation_by_id.get(
            int(conversion.receipt_purpose_allocation_id)
        )
        lot = db.get(InventoryLot, int(conversion.semi_finished_inventory_lot_id))
        if allocation is None or lot is None:
            raise ReceiptPurposeFlowError(
                "RECEIPT_RESERVE_RESTORE_SUPPORT_MISSING",
                "备库转换关联的用途事实或片料批次不存在，不能自动恢复。",
            )
        snapshot_id = int(allocation.purchase_purpose_source_snapshot_id or 0)
        step_before = _finished_capacity(db, snapshots, projected_sheets)
        low, high = 0, active
        while low < high:
            midpoint = (low + high + 1) // 2
            candidate_sheets = dict(projected_sheets)
            candidate_sheets[snapshot_id] = max(
                candidate_sheets.get(snapshot_id, 0) - midpoint,
                0,
            )
            if _finished_capacity(db, snapshots, candidate_sheets) >= desired_total:
                low = midpoint
            else:
                high = midpoint - 1
        take = low
        if take <= 0:
            continue
        projected_sheets[snapshot_id] = max(
            projected_sheets.get(snapshot_id, 0) - take,
            0,
        )
        step_after = _finished_capacity(db, snapshots, projected_sheets)
        planned.append(
            (conversion, allocation, lot, take, step_before, step_after)
        )

    required_by_lot: dict[int, int] = {}
    for _conversion, _allocation, lot, take, _before, _after in planned:
        required_by_lot[int(lot.id)] = required_by_lot.get(int(lot.id), 0) + take
    for lot_id, required in required_by_lot.items():
        lot = db.get(InventoryLot, lot_id)
        if (
            lot is None
            or lot.inventory_type != "semi_finished"
            or lot.status != "active"
            or int(lot.quantity_consumed or 0) < required
        ):
            raise ReceiptPurposeFlowError(
                "RECEIPT_RESERVE_RESTORE_FAILED",
                "备库片料状态或已消耗数量发生变化，不能自动恢复。",
            )
    order_item = db.get(OrderItem, order_item_id)
    if order_item is None:
        raise ReceiptPurposeFlowError(
            "ORDER_ITEM_MISSING",
            "生产完工关联的订单明细不存在，已停止恢复。",
        )

    postings: list[ReserveConversionReversalPosting] = []
    for conversion, allocation, lot, take, step_before, step_after in planned:
        refreshed_lot = db.get(InventoryLot, int(lot.id))
        if refreshed_lot is None:
            raise ReceiptPurposeFlowError(
                "RECEIPT_RESERVE_RESTORE_FAILED",
                "备库片料批次在恢复前消失，操作已停止。",
            )
        movement_key = _conversion_key(
            f"{idempotency_key}:receipt-reserve-restore:{int(conversion.id)}"
        )
        try:
            movement = restore_consumed_semi_finished_lot(
                db,
                lot_id=int(refreshed_lot.id),
                quantity=int(take),
                expected_version=int(refreshed_lot.version),
                original_consume_movement_id=int(conversion.semi_consume_movement_id),
                operator_id=operator_id,
                idempotency_key=movement_key,
                related_order_id=int(order_item.order_id),
                related_order_item_id=order_item_id,
                reason=(
                    "成品实收减少，自动恢复此前转作成品原料的备库片料"
                    if reason_type == "actual_adjustment"
                    else "撤销收料自动完工，恢复此前转作成品原料的备库片料"
                ),
            )
        except WarehouseInventoryError as error:
            raise ReceiptPurposeFlowError(
                "RECEIPT_RESERVE_RESTORE_FAILED",
                str(error),
                error.status_code,
            ) from error
        request_hash = canonical_purchase_receipt_hash(
            {
                "production_completion_id": int(completion.id),
                "production_completion_reserve_conversion_id": int(conversion.id),
                "receipt_purpose_allocation_id": int(allocation.id),
                "semi_finished_inventory_lot_id": int(lot.id),
                "restored_sheet_quantity": int(take),
                "desired_completion_quantity": desired,
                "supported_finished_quantity_before": int(step_before),
                "supported_finished_quantity_after": int(step_after),
                "reason_type": reason_type,
                "idempotency_key": movement_key,
            }
        )
        postings.append(
            ReserveConversionReversalPosting(
                production_completion_reserve_conversion_id=int(conversion.id),
                semi_finished_inventory_lot_id=int(lot.id),
                semi_reverse_movement_id=int(movement.id),
                restored_sheet_quantity=int(take),
                reversed_finished_quantity_delta=int(step_before - step_after),
                supported_finished_quantity_before=int(step_before),
                supported_finished_quantity_after=int(step_after),
                idempotency_key=movement_key,
                request_hash=request_hash,
            )
        )
    return postings


def _source_dimensions(
    source: SupplierRequisitionOrderItem | RequisitionItem,
) -> tuple[int, int]:
    if isinstance(source, SupplierRequisitionOrderItem):
        length = source.report_length_mm
        width = source.report_width_mm
    else:
        length = source.cardboard_len
        width = source.cardboard_width
    try:
        normalized_length = int(Decimal(str(length)))
        normalized_width = int(Decimal(str(width)))
    except Exception as error:
        raise ReceiptPurposeFlowError(
            "PURCHASE_SOURCE_DIMENSION_INVALID",
            "正式采购来源缺少有效报料长宽。",
        ) from error
    if normalized_length <= 0 or normalized_width <= 0:
        raise ReceiptPurposeFlowError(
            "PURCHASE_SOURCE_DIMENSION_INVALID",
            "正式采购来源缺少有效报料长宽。",
        )
    return normalized_length, normalized_width


def _source_supplier_name(
    db: Session,
    source: SupplierRequisitionOrderItem | RequisitionItem,
) -> str | None:
    if isinstance(source, SupplierRequisitionOrderItem):
        if source.supplier_name_snapshot:
            return source.supplier_name_snapshot
        header = db.get(SupplierRequisitionOrder, source.supplier_order_id)
    else:
        header = db.get(Requisition, source.requisition_id)
    return str(header.supplier_name or "").strip() or None if header else None


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY, rounding=ROUND_HALF_UP)


def _initial_movement(db: Session, lot_id: int) -> InventoryMovement:
    movement = db.scalar(
        select(InventoryMovement)
        .where(InventoryMovement.inventory_lot_id == lot_id)
        .order_by(InventoryMovement.id)
    )
    if movement is None:
        raise ReceiptPurposeFlowError(
            "RESERVE_INVENTORY_POSTING_INCOMPLETE",
            "片料库存缺少入库流水，已停止收料。",
        )
    return movement


def post_receipt_purpose_allocation(
    db: Session,
    *,
    target: Any,
    receipt_item: IncomingReceiptItem,
    context: ReceiptPurposeContext,
    operator_id: int,
    idempotency_key: str,
    surplus_disposition: str,
) -> IncomingReceiptPurposeAllocation:
    snapshot = context.snapshot
    fact = context.receipt_fact
    source = context.source
    quantity = int(receipt_item.received_quantity or 0)
    if quantity <= 0:
        raise ReceiptPurposeFlowError(
            "INCOMING_QUANTITY_INVALID", "实收数量必须大于0。", 400
        )
    prior_source = _active_source_allocations(db, snapshot.id)
    before_total = sum(int(row.receipt_total_sheet_qty) for row in prior_source)
    before_order = sum(
        int(row.receipt_order_purpose_sheet_qty) for row in prior_source
    )
    before_reserve = sum(
        int(row.receipt_reserve_purpose_sheet_qty) for row in prior_source
    )
    after_total = before_total + quantity
    order_plan = int(snapshot.order_purpose_sheet_qty or 0)
    reserve_plan = int(snapshot.reserve_purpose_sheet_qty or 0)
    disposition = str(surplus_disposition or "not_applicable").strip()
    if disposition not in {
        "not_applicable",
        "finished",
        "semi_finished_reserve",
    }:
        raise ReceiptPurposeFlowError(
            "INCOMING_SURPLUS_DISPOSITION_INVALID",
            "多收片料用途无效，请刷新后重新选择。",
        )
    remaining_order = max(order_plan - before_order, 0)
    base_order_delta = min(quantity, remaining_order)
    surplus_delta = quantity - base_order_delta
    if surplus_delta > 0:
        if disposition == "not_applicable":
            raise ReceiptPurposeFlowError(
                "INCOMING_SURPLUS_DISPOSITION_REQUIRED",
                f"本次实收比订单成品用途多 {surplus_delta} 片，请先选择用途。",
            )
        if disposition == "finished":
            order_delta = quantity
            reserve_delta = 0
        else:
            order_delta = base_order_delta
            reserve_delta = surplus_delta
    else:
        if disposition != "not_applicable":
            raise ReceiptPurposeFlowError(
                "INCOMING_SURPLUS_DISPOSITION_INVALID",
                "本次没有多收片料，不能提交多收片料用途。",
            )
        order_delta = quantity
        reserve_delta = 0
    after_order = before_order + order_delta
    after_reserve = before_reserve + reserve_delta
    if order_delta < 0 or reserve_delta < 0:
        raise ReceiptPurposeFlowError(
            "PURCHASE_PURPOSE_CUMULATIVE_INVALID",
            "累计收料用途无法保持单调，请先核对历史事实。",
        )

    order_item_id = int(target.order_item.id)
    snapshots = _order_item_snapshots(db, order_item_id)
    active_item_allocations = _active_order_item_allocations(db, order_item_id)
    before_sheets: dict[int, int] = {}
    for row in active_item_allocations:
        sid = int(row.purchase_purpose_source_snapshot_id or 0)
        before_sheets[sid] = before_sheets.get(sid, 0) + int(
            row.receipt_order_purpose_sheet_qty
        )
    after_sheets = dict(before_sheets)
    after_sheets[snapshot.id] = after_sheets.get(snapshot.id, 0) + order_delta
    finished_before = _finished_capacity(db, snapshots, before_sheets)
    finished_after = _finished_capacity(db, snapshots, after_sheets)
    finished_delta = finished_after - finished_before
    if finished_delta < 0:
        raise ReceiptPurposeFlowError(
            "AUTOMATIC_FINISHED_QUANTITY_INVALID",
            "自动成品累计数量不能倒退。",
        )

    board_length, board_width = _source_dimensions(source)
    sheet_cost = _money(
        calculate_per_sheet_cost(
            unit_price=fact.unit_price,
            price_unit=fact.price_unit,
            tax_included=fact.tax_included,
            tax_rate=fact.tax_rate,
            report_length_mm=board_length,
            report_width_mm=board_width,
        )
    )
    order_cost = _money(sheet_cost * Decimal(order_delta))
    reserve_cost = _money(sheet_cost * Decimal(reserve_delta))
    total_cost = _money(order_cost + reserve_cost)
    prior_unallocated_cost = _money(
        sum(
            (Decimal(row.order_purpose_cost or 0) for row in active_item_allocations),
            Decimal("0"),
        )
        - sum(
            (Decimal(row.capitalized_cost or 0) for row in active_item_allocations),
            Decimal("0"),
        )
    )
    capitalized_cost = (
        _money(max(prior_unallocated_cost, Decimal("0")) + order_cost)
        if finished_delta > 0
        else Decimal("0.0000")
    )
    material_input_before = sum(before_sheets.values())
    material_input_after = sum(after_sheets.values())

    completion = None
    ensure_receipt_auto_main_task(db, order_item_id=order_item_id)
    if finished_delta > 0:
        try:
            completion = post_automatic_receipt_completion(
                db,
                order_item_id=order_item_id,
                previous_theoretical_quantity=finished_before,
                new_theoretical_quantity=finished_after,
                material_input_delta=max(order_delta, 1),
                material_input_cumulative=max(material_input_after, 1),
                operator_id=operator_id,
                idempotency_key=f"p181-completion:{idempotency_key}",
                capitalized_material_cost=capitalized_cost,
                cost_detail={
                    "purchase_receipt_fact_id": fact.id,
                    "purchase_purpose_source_snapshot_id": snapshot.id,
                    "currency": fact.currency,
                    "sheet_cost": str(sheet_cost),
                },
            )
        except (ProductionWorkflowError, WarehouseInventoryError) as error:
            message = str(error)
            location_issue_markers = (
                "待送区",
                "暂存",
                "FIN-",
                "地堆位置",
                "一楼原料区域",
                "真实排位",
            )
            code = (
                "AUTO_FINISHED_LOCATION_UNAVAILABLE"
                if any(marker in message for marker in location_issue_markers)
                else "AUTOMATIC_FINISHED_POSTING_FAILED"
            )
            raise ReceiptPurposeFlowError(code, message, error.status_code) from error

    reserve_lot: InventoryLot | None = None
    reserve_movement: InventoryMovement | None = None
    if reserve_delta > 0:
        try:
            location = automatic_raw_material_staging_location(
                db,
                repair_operator_id=operator_id,
                repair_idempotency_key=f"receipt-purpose:{idempotency_key}",
                require_floor3_left=True,
            )
            order_item = db.get(OrderItem, order_item_id)
            reserve_lot = manual_semi_finished_in(
                db,
                location_id=location.id,
                quantity=reserve_delta,
                stock_date=beijing_today(),
                source_type="purchase_reserve",
                material_code=fact.actual_material_code_snapshot,
                layer_count=int(
                    fact.actual_material_layer_count_snapshot
                    or order_item.layer_count
                    or 0
                ),
                flute_type=str(
                    fact.actual_material_flute_type_snapshot
                    or order_item.flute_type
                    or ""
                ),
                board_length_mm=board_length,
                board_width_mm=board_width,
                sheet_type="raw_board",
                component_type=snapshot.component_type,
                pieces_per_box=max(int(snapshot.pieces_per_finished_snapshot or 1), 1),
                stock_yield_per_sheet=max(int(snapshot.yield_per_sheet_snapshot or 1), 1),
                supplier_name=_source_supplier_name(db, source),
                customer_id=snapshot.customer_id,
                crease_type=getattr(order_item, "snapshot_crease_type", None),
                crease_left_mm=getattr(order_item, "snapshot_crease_left_mm", None),
                crease_middle_mm=getattr(order_item, "snapshot_crease_middle_mm", None),
                crease_right_mm=getattr(order_item, "snapshot_crease_right_mm", None),
                cutting_note=getattr(order_item, "requisition_remark", None),
                remarks="采购用途分配：客户通用片料备库",
                operator_id=operator_id,
                idempotency_key=f"p181-reserve:{idempotency_key}",
                source_ref_type="incoming_receipt_item",
                source_ref_id=receipt_item.id,
                material_id=fact.actual_material_id,
                movement_reason="采购收料按冻结用途进入客户通用片料库存",
                allow_raw_material_staging=True,
                customer_generic_eligible=True,
                internal_name=(
                    f"{snapshot.customer_name_snapshot or '客户'} "
                    f"{board_length}x{board_width} 通用备料"
                ),
                expected_layout_version=(
                    int(location.floor3_layout.version)
                    if location.floor3_layout is not None
                    else None
                ),
            )
            reserve_lot.estimated_unit_cost_snapshot = sheet_cost
            reserve_lot.estimated_square_price_snapshot = None
            reserve_lot.estimated_cost_area_m2_snapshot = None
            reserve_lot.cost_snapshot_source = "purchase_receipt_actual"
            reserve_lot.cost_snapshot_detail_json = json.dumps(
                {
                    "purchase_receipt_fact_id": fact.id,
                    "currency": fact.currency,
                    "sheet_cost": str(sheet_cost),
                    "reserve_cost": str(reserve_cost),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            reserve_lot.cost_snapshot_at = utc_now_naive()
            reserve_movement = _initial_movement(db, reserve_lot.id)
        except WarehouseInventoryError as error:
            message = str(error)
            code = (
                "RESERVE_STAGING_LOCATION_UNAVAILABLE"
                if any(
                    marker in message
                    for marker in (
                        "暂存",
                        "A1",
                        "库位",
                        "三楼左区",
                        "一楼原料区域",
                        "真实排位",
                    )
                )
                else "RESERVE_INVENTORY_POSTING_FAILED"
            )
            raise ReceiptPurposeFlowError(code, message, error.status_code) from error

    request_hash = canonical_purchase_receipt_hash(
        {
            "actor_id": operator_id,
            "incoming_receipt_item_id": receipt_item.id,
            "purchase_purpose_source_snapshot_id": snapshot.id,
            "purchase_receipt_fact_id": fact.id,
            "receipt_total_sheet_qty": quantity,
            "receipt_order_purpose_sheet_qty": order_delta,
            "receipt_reserve_purpose_sheet_qty": reserve_delta,
            "surplus_disposition": disposition,
            "receipt_plan_fingerprint": fact.receipt_plan_fingerprint,
        }
    )
    customer = db.get(Customer, snapshot.customer_id)
    allocation = IncomingReceiptPurposeAllocation(
        incoming_receipt_item_id=receipt_item.id,
        purpose_contract_status_snapshot="frozen",
        surplus_disposition=disposition,
        purchase_purpose_source_snapshot_id=snapshot.id,
        purchase_receipt_fact_id=fact.id,
        supplier_requisition_order_item_id=(
            source.id if isinstance(source, SupplierRequisitionOrderItem) else None
        ),
        material_requisition_item_id=(
            source.id if isinstance(source, RequisitionItem) else None
        ),
        source_kind=snapshot.source_kind,
        source_key=snapshot.source_key,
        source_order_item_id=snapshot.source_order_item_id,
        source_requisition_item_id=snapshot.source_requisition_item_id,
        source_bom_requisition_source_id=snapshot.source_bom_requisition_source_id,
        customer_id=snapshot.customer_id,
        customer_name_snapshot=(
            customer.name if customer is not None else snapshot.customer_name_snapshot
        ),
        component_type=snapshot.component_type,
        order_purpose_plan_sheet_qty_snapshot=order_plan,
        reserve_purpose_plan_sheet_qty_snapshot=reserve_plan,
        receipt_total_sheet_qty=quantity,
        receipt_order_purpose_sheet_qty=order_delta,
        receipt_reserve_purpose_sheet_qty=reserve_delta,
        cumulative_total_sheet_qty_before=before_total,
        cumulative_total_sheet_qty_after=after_total,
        cumulative_order_purpose_sheet_qty_before=before_order,
        cumulative_order_purpose_sheet_qty_after=after_order,
        cumulative_reserve_purpose_sheet_qty_before=before_reserve,
        cumulative_reserve_purpose_sheet_qty_after=after_reserve,
        finished_output_qty_before=finished_before,
        finished_output_qty_after=finished_after,
        finished_output_qty_delta=finished_delta,
        sheet_cost=sheet_cost,
        order_purpose_cost=order_cost,
        reserve_purpose_cost=reserve_cost,
        total_cost=total_cost,
        capitalized_cost=capitalized_cost,
        production_completion_id=(completion.id if completion is not None else None),
        finished_inventory_lot_id=(
            completion.inventory_lot_id if completion is not None else None
        ),
        semi_finished_inventory_lot_id=(
            reserve_lot.id if reserve_lot is not None else None
        ),
        initial_semi_inventory_movement_id=(
            reserve_movement.id if reserve_movement is not None else None
        ),
        status="posted",
        version=1,
        request_hash=request_hash,
        created_by=operator_id,
    )
    db.add(allocation)
    db.flush()
    return allocation


def _receipt_lots_with_location_names(
    db: Session,
    lot_ids: set[int],
) -> tuple[dict[int, InventoryLot], dict[int, str]]:
    """Load receipt lots and employee names in one bounded query.

    Receipt serialization needs only the stable floor/area naming projection,
    not map geometry, ground-layout or publication readiness.  Joining the
    formal floor and area masters here keeps custom ``area_name`` changes live
    without invoking the heavier map projection loader.
    """

    if not lot_ids:
        return {}, {}
    location_sequences = select(
        WarehouseLocation.id.label("location_id"),
        func.row_number()
        .over(
            partition_by=(
                WarehouseLocation.warehouse_floor,
                func.upper(func.trim(WarehouseLocation.area_code)),
            ),
            order_by=(
                WarehouseLocation.sort_order,
                WarehouseLocation.location_code,
                WarehouseLocation.id,
            ),
        )
        .label("area_sequence"),
    ).subquery()
    rows = db.execute(
        select(
            InventoryLot,
            WarehouseLocation,
            WarehouseArea,
            WarehouseFloor,
            location_sequences.c.area_sequence,
        )
        .select_from(InventoryLot)
        .outerjoin(
            WarehouseLocation,
            WarehouseLocation.id == InventoryLot.warehouse_location_id,
        )
        .outerjoin(
            WarehouseFloor,
            WarehouseFloor.floor_number == WarehouseLocation.warehouse_floor,
        )
        .outerjoin(
            WarehouseArea,
            and_(
                WarehouseArea.floor_id == WarehouseFloor.id,
                func.upper(func.trim(WarehouseArea.area_code))
                == func.upper(func.trim(WarehouseLocation.area_code)),
            ),
        )
        .outerjoin(
            location_sequences,
            location_sequences.c.location_id == WarehouseLocation.id,
        )
        .where(InventoryLot.id.in_(lot_ids))
    ).all()
    lots: dict[int, InventoryLot] = {}
    names: dict[int, str] = {}
    for lot, location, area, floor, area_sequence in rows:
        lots[int(lot.id)] = lot
        if location is not None:
            names[int(lot.id)] = employee_location_name(
                location,
                area=area,
                floor=floor,
                area_sequence=(int(area_sequence) if area_sequence else None),
            )
    return lots, names


def serialize_receipt_purpose_allocation(
    db: Session,
    allocation: IncomingReceiptPurposeAllocation,
) -> dict[str, Any]:
    finished_lot = (
        db.get(InventoryLot, allocation.finished_inventory_lot_id)
        if allocation.finished_inventory_lot_id is not None
        else None
    )
    if finished_lot is None:
        receipt_item = db.get(IncomingReceiptItem, allocation.incoming_receipt_item_id)
        if receipt_item is not None and receipt_item.order_item_id is not None:
            latest_finished_allocation = db.scalar(
                _active_allocation_statement()
                .join(
                    IncomingReceiptItem,
                    IncomingReceiptItem.id
                    == IncomingReceiptPurposeAllocation.incoming_receipt_item_id,
                )
                .where(
                    IncomingReceiptItem.order_item_id == receipt_item.order_item_id,
                    IncomingReceiptPurposeAllocation.finished_inventory_lot_id.is_not(None),
                )
                .order_by(IncomingReceiptPurposeAllocation.id.desc())
                .limit(1)
            )
            if latest_finished_allocation is not None:
                finished_lot = db.get(
                    InventoryLot,
                    latest_finished_allocation.finished_inventory_lot_id,
                )
    reserve_lot = (
        db.get(InventoryLot, allocation.semi_finished_inventory_lot_id)
        if allocation.semi_finished_inventory_lot_id is not None
        else None
    )
    receipt_fact = db.get(PurchaseReceiptFact, allocation.purchase_receipt_fact_id)
    if receipt_fact is None:
        raise ReceiptPurposeFlowError(
            "PURCHASE_RECEIPT_FACT_REQUIRED",
            "收料用途分配关联的正式采购事实不存在。",
        )
    _lots, location_names = _receipt_lots_with_location_names(
        db,
        {
            int(lot.id)
            for lot in (finished_lot, reserve_lot)
            if lot is not None
        },
    )

    def projected_location_name(lot: InventoryLot | None) -> str | None:
        return location_names.get(int(lot.id)) if lot is not None else None

    return {
        "surplus_disposition": allocation.surplus_disposition,
        "order_sheet_delta": allocation.receipt_order_purpose_sheet_qty,
        "reserve_sheet_delta": allocation.receipt_reserve_purpose_sheet_qty,
        "order_sheet_cumulative": allocation.cumulative_order_purpose_sheet_qty_after,
        "reserve_sheet_cumulative": allocation.cumulative_reserve_purpose_sheet_qty_after,
        "theoretical_finished_delta": allocation.finished_output_qty_delta,
        "theoretical_finished_cumulative": allocation.finished_output_qty_after,
        "reserve_planned_sheet_qty": allocation.reserve_purpose_plan_sheet_qty_snapshot,
        "reserve_actual_sheet_qty": allocation.cumulative_reserve_purpose_sheet_qty_after,
        "reserve_variance_sheet_qty": (
            allocation.cumulative_reserve_purpose_sheet_qty_after
            - int(allocation.reserve_purpose_plan_sheet_qty_snapshot or 0)
        ),
        "finished_inventory_lot_id": allocation.finished_inventory_lot_id,
        "reserve_inventory_lot_id": allocation.semi_finished_inventory_lot_id,
        "finished_location_name": projected_location_name(finished_lot),
        "reserve_location_name": projected_location_name(reserve_lot),
        "sheet_cost": allocation.sheet_cost,
        "order_cost": allocation.order_purpose_cost,
        "reserve_cost": allocation.reserve_purpose_cost,
        "receipt_total_cost": allocation.total_cost,
        "currency": receipt_fact.currency,
        "trace_id": f"receipt-purpose:{allocation.id}",
    }


def serialize_receipt_purpose_allocations(
    db: Session,
    allocations: list[IncomingReceiptPurposeAllocation],
) -> dict[int, dict[str, Any]]:
    """Batch serializer used by batch receive; query count is bounded."""

    if not allocations:
        return {}
    receipt_items = {
        row.id: row
        for row in db.scalars(
            select(IncomingReceiptItem).where(
                IncomingReceiptItem.id.in_(
                    [row.incoming_receipt_item_id for row in allocations]
                )
            )
        ).all()
    }
    order_ids = {
        int(row.order_item_id)
        for row in receipt_items.values()
        if row.order_item_id is not None
    }
    latest_finished_by_order: dict[int, IncomingReceiptPurposeAllocation] = {}
    if order_ids:
        candidates = db.execute(
            select(IncomingReceiptPurposeAllocation, IncomingReceiptItem)
            .join(
                IncomingReceiptItem,
                IncomingReceiptItem.id
                == IncomingReceiptPurposeAllocation.incoming_receipt_item_id,
            )
            .where(
                IncomingReceiptItem.order_item_id.in_(order_ids),
                IncomingReceiptPurposeAllocation.finished_inventory_lot_id.is_not(None),
                ~exists(
                    select(IncomingReceiptPurposeReversal.id).where(
                        IncomingReceiptPurposeReversal.incoming_receipt_purpose_allocation_id
                        == IncomingReceiptPurposeAllocation.id
                    )
                ),
            )
            .order_by(IncomingReceiptPurposeAllocation.id)
        ).all()
        for candidate, receipt_item in candidates:
            if receipt_item.order_item_id is not None:
                latest_finished_by_order[int(receipt_item.order_item_id)] = candidate
    lot_ids: set[int] = set()
    fact_ids = {int(row.purchase_receipt_fact_id) for row in allocations}
    finished_lot_by_allocation: dict[int, int | None] = {}
    for allocation in allocations:
        finished_id = allocation.finished_inventory_lot_id
        receipt_item = receipt_items.get(allocation.incoming_receipt_item_id)
        if finished_id is None and receipt_item is not None and receipt_item.order_item_id is not None:
            latest = latest_finished_by_order.get(int(receipt_item.order_item_id))
            finished_id = latest.finished_inventory_lot_id if latest is not None else None
        finished_lot_by_allocation[allocation.id] = finished_id
        if finished_id is not None:
            lot_ids.add(int(finished_id))
        if allocation.semi_finished_inventory_lot_id is not None:
            lot_ids.add(int(allocation.semi_finished_inventory_lot_id))
    lots, location_names = _receipt_lots_with_location_names(db, lot_ids)

    def projected_location_name(lot: InventoryLot | None) -> str | None:
        return location_names.get(int(lot.id)) if lot is not None else None

    facts = {
        row.id: row
        for row in db.scalars(
            select(PurchaseReceiptFact).where(PurchaseReceiptFact.id.in_(fact_ids))
        ).all()
    }
    result: dict[int, dict[str, Any]] = {}
    for allocation in allocations:
        fact = facts.get(allocation.purchase_receipt_fact_id)
        if fact is None:
            raise ReceiptPurposeFlowError(
                "PURCHASE_RECEIPT_FACT_REQUIRED",
                "收料用途分配关联的正式采购事实不存在。",
            )
        finished_lot = lots.get(finished_lot_by_allocation[allocation.id])
        reserve_lot = lots.get(allocation.semi_finished_inventory_lot_id)
        result[allocation.id] = {
            "order_sheet_delta": allocation.receipt_order_purpose_sheet_qty,
            "reserve_sheet_delta": allocation.receipt_reserve_purpose_sheet_qty,
            "order_sheet_cumulative": allocation.cumulative_order_purpose_sheet_qty_after,
            "reserve_sheet_cumulative": allocation.cumulative_reserve_purpose_sheet_qty_after,
            "theoretical_finished_delta": allocation.finished_output_qty_delta,
            "theoretical_finished_cumulative": allocation.finished_output_qty_after,
            "reserve_planned_sheet_qty": allocation.reserve_purpose_plan_sheet_qty_snapshot,
            "reserve_actual_sheet_qty": allocation.cumulative_reserve_purpose_sheet_qty_after,
            "reserve_variance_sheet_qty": allocation.cumulative_reserve_purpose_sheet_qty_after - int(allocation.reserve_purpose_plan_sheet_qty_snapshot or 0),
            "finished_inventory_lot_id": allocation.finished_inventory_lot_id,
            "reserve_inventory_lot_id": allocation.semi_finished_inventory_lot_id,
            "finished_location_name": projected_location_name(finished_lot),
            "reserve_location_name": projected_location_name(reserve_lot),
            "sheet_cost": allocation.sheet_cost,
            "order_cost": allocation.order_purpose_cost,
            "reserve_cost": allocation.reserve_purpose_cost,
            "receipt_total_cost": allocation.total_cost,
            "currency": fact.currency,
            "trace_id": f"receipt-purpose:{allocation.id}",
        }
    return result


def reverse_receipt_purpose_allocation(
    db: Session,
    *,
    receipt_item: IncomingReceiptItem,
    operator_id: int,
    reason: str,
    idempotency_key: str,
) -> IncomingReceiptPurposeReversal:
    allocation = db.scalar(
        _active_allocation_statement().where(
            IncomingReceiptPurposeAllocation.incoming_receipt_item_id
            == receipt_item.id
        )
    )
    if allocation is None:
        raise ReceiptPurposeFlowError(
            "INCOMING_PURPOSE_ALLOCATION_MISSING",
            "收料用途分配事实不存在或已经撤销。",
        )
    newer = db.scalar(
        _active_allocation_statement()
        .join(
            IncomingReceiptItem,
            IncomingReceiptItem.id
            == IncomingReceiptPurposeAllocation.incoming_receipt_item_id,
        )
        .where(
            IncomingReceiptItem.order_item_id == receipt_item.order_item_id,
            IncomingReceiptPurposeAllocation.id > allocation.id,
        )
        .limit(1)
    )
    if newer is not None:
        raise ReceiptPurposeFlowError(
            "INCOMING_REVERSAL_NOT_LATEST",
            "该订单明细存在更晚的用途收料，请先撤销最新一笔。",
        )

    completion_reversed = False
    if int(allocation.production_completion_id or 0) > 0:
        remaining_material_input = sum(
            int(row.receipt_order_purpose_sheet_qty)
            for row in _active_order_item_allocations(db, receipt_item.order_item_id)
            if row.id != allocation.id
        )
        try:
            reverse_automatic_receipt_completion(
                db,
                completion_id=int(allocation.production_completion_id),
                remaining_theoretical_quantity=allocation.finished_output_qty_before,
                remaining_material_input_quantity=remaining_material_input,
                operator_id=operator_id,
                reason=reason,
            )
            completion_reversed = True
        except ProductionWorkflowError as error:
            raise ReceiptPurposeFlowError(
                "AUTOMATIC_FINISHED_REVERSAL_FAILED",
                str(error),
                error.status_code,
            ) from error

    compensation_movement: InventoryMovement | None = None
    reserve_lot: InventoryLot | None = None
    if allocation.semi_finished_inventory_lot_id is not None:
        reserve_lot = db.get(InventoryLot, allocation.semi_finished_inventory_lot_id)
        expected = int(allocation.receipt_reserve_purpose_sheet_qty)
        if (
            reserve_lot is None
            or reserve_lot.status != "active"
            or int(reserve_lot.quantity_available) != expected
            or int(reserve_lot.quantity_reserved) != 0
            or int(reserve_lot.quantity_consumed) != 0
            or int(reserve_lot.quantity_damaged) != 0
            or int(reserve_lot.quantity_scrapped) != 0
        ):
            raise ReceiptPurposeFlowError(
                "RESERVE_INVENTORY_ALREADY_USED",
                "本次片料库存已被预占、使用、移动或调整，不能撤销收料。",
            )
        try:
            mutate_lot(
                db,
                lot_id=reserve_lot.id,
                operation="adjust",
                expected_version=reserve_lot.version,
                operator_id=operator_id,
                quantity=-expected,
                reason=reason,
                idempotency_key=f"p181-reverse-reserve:{receipt_item.id}",
            )
            reserve_lot.status = "closed"
            compensation_movement = db.scalar(
                select(InventoryMovement).where(
                    InventoryMovement.idempotency_key
                    == f"p181-reverse-reserve:{receipt_item.id}"
                )
            )
        except WarehouseInventoryError as error:
            raise ReceiptPurposeFlowError(
                "RESERVE_INVENTORY_REVERSAL_FAILED",
                str(error),
                error.status_code,
            ) from error

    if allocation.production_completion_id is not None and not completion_reversed:
        remaining_material_input = sum(
            int(row.receipt_order_purpose_sheet_qty)
            for row in _active_order_item_allocations(db, receipt_item.order_item_id)
            if row.id != allocation.id
        )
        try:
            reverse_automatic_receipt_completion(
                db,
                completion_id=allocation.production_completion_id,
                remaining_theoretical_quantity=allocation.finished_output_qty_before,
                remaining_material_input_quantity=remaining_material_input,
                operator_id=operator_id,
                reason=reason,
            )
        except ProductionWorkflowError as error:
            raise ReceiptPurposeFlowError(
                "AUTOMATIC_FINISHED_REVERSAL_FAILED",
                str(error),
                error.status_code,
            ) from error

    reversal = IncomingReceiptPurposeReversal(
        incoming_receipt_purpose_allocation_id=allocation.id,
        incoming_receipt_item_id=receipt_item.id,
        reversed_production_completion_id=allocation.production_completion_id,
        reversed_finished_inventory_lot_id=allocation.finished_inventory_lot_id,
        reversed_semi_finished_inventory_lot_id=allocation.semi_finished_inventory_lot_id,
        compensation_inventory_movement_id=(
            compensation_movement.id if compensation_movement is not None else None
        ),
        cumulative_total_sheet_qty_before=allocation.cumulative_total_sheet_qty_after,
        cumulative_total_sheet_qty_after=allocation.cumulative_total_sheet_qty_before,
        cumulative_order_purpose_sheet_qty_before=(
            allocation.cumulative_order_purpose_sheet_qty_after
        ),
        cumulative_order_purpose_sheet_qty_after=(
            allocation.cumulative_order_purpose_sheet_qty_before
        ),
        cumulative_reserve_purpose_sheet_qty_before=(
            allocation.cumulative_reserve_purpose_sheet_qty_after
        ),
        cumulative_reserve_purpose_sheet_qty_after=(
            allocation.cumulative_reserve_purpose_sheet_qty_before
        ),
        request_hash=canonical_purchase_receipt_hash(
            {
                "allocation_id": allocation.id,
                "receipt_item_id": receipt_item.id,
                "operator_id": operator_id,
                "reason": reason,
                "idempotency_key": str(idempotency_key or "").strip(),
            }
        ),
        reversed_by=operator_id,
    )
    db.add(reversal)
    db.flush()
    return reversal


def serialize_receipt_purpose_reversal(
    db: Session,
    reversal: IncomingReceiptPurposeReversal,
) -> dict[str, Any]:
    allocation = db.get(
        IncomingReceiptPurposeAllocation,
        reversal.incoming_receipt_purpose_allocation_id,
    )
    if allocation is None:
        raise ReceiptPurposeFlowError(
            "INCOMING_PURPOSE_ALLOCATION_MISSING",
            "收料用途分配事实不存在。",
        )
    active_source = _active_source_allocations(
        db, int(allocation.purchase_purpose_source_snapshot_id)
    )
    snapshots = _order_item_snapshots(db, allocation.incoming_receipt_item.order_item_id)
    active_item = _active_order_item_allocations(
        db, allocation.incoming_receipt_item.order_item_id
    )
    sheet_map: dict[int, int] = {}
    for row in active_item:
        sid = int(row.purchase_purpose_source_snapshot_id or 0)
        sheet_map[sid] = sheet_map.get(sid, 0) + int(
            row.receipt_order_purpose_sheet_qty
        )
    return {
        "valid_received_cumulative": sum(
            int(row.receipt_total_sheet_qty) for row in active_source
        ),
        "order_sheet_cumulative": sum(
            int(row.receipt_order_purpose_sheet_qty) for row in active_source
        ),
        "reserve_sheet_cumulative": sum(
            int(row.receipt_reserve_purpose_sheet_qty) for row in active_source
        ),
        "theoretical_finished_cumulative": _finished_capacity(db, snapshots, sheet_map),
        "trace_id": f"receipt-purpose-reversal:{reversal.id}",
    }
