"""P1-81 receipt-purpose distribution, automatic posting and exact reversal.

All functions participate in the caller's transaction.  They never commit.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
import json
from typing import Any

from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.core.time_contract import beijing_today, utc_now_naive
from app.models.customer import Customer
from app.models.incoming_receipt import IncomingReceiptItem
from app.models.material import Material
from app.models.order import OrderItem
from app.models.product_bom import RequisitionItemBomSource
from app.models.purchase_receipt import (
    IncomingReceiptPurposeAllocation,
    IncomingReceiptPurposeReversal,
    PurchaseReceiptFact,
)
from app.models.requisition import Requisition, RequisitionItem
from app.models.supplier_requisition_order import (
    PurchasePurposeSourceSnapshot,
    SupplierRequisitionOrder,
    SupplierRequisitionOrderItem,
)
from app.models.warehouse_inventory import InventoryLot, InventoryMovement
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
    manual_semi_finished_in,
    mutate_lot,
)


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
    after_order = after_total if reserve_plan == 0 else min(after_total, order_plan)
    after_reserve = after_total - after_order
    order_delta = after_order - before_order
    reserve_delta = after_reserve - before_reserve
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
            location_issue_markers = ("待送区", "暂存", "FIN-", "地堆位置")
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
            location = automatic_raw_material_staging_location(db)
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
            code = (
                "RESERVE_STAGING_LOCATION_UNAVAILABLE"
                if "暂存" in str(error) or "A1" in str(error) or "库位" in str(error)
                else "RESERVE_INVENTORY_POSTING_FAILED"
            )
            raise ReceiptPurposeFlowError(code, str(error), error.status_code) from error

    request_hash = canonical_purchase_receipt_hash(
        {
            "actor_id": operator_id,
            "incoming_receipt_item_id": receipt_item.id,
            "purchase_purpose_source_snapshot_id": snapshot.id,
            "purchase_receipt_fact_id": fact.id,
            "receipt_total_sheet_qty": quantity,
            "receipt_order_purpose_sheet_qty": order_delta,
            "receipt_reserve_purpose_sheet_qty": reserve_delta,
            "receipt_plan_fingerprint": fact.receipt_plan_fingerprint,
        }
    )
    customer = db.get(Customer, snapshot.customer_id)
    allocation = IncomingReceiptPurposeAllocation(
        incoming_receipt_item_id=receipt_item.id,
        purpose_contract_status_snapshot="frozen",
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
    return {
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
        "finished_location_name": (
            finished_lot.location.location_name if finished_lot is not None else None
        ),
        "reserve_location_name": (
            reserve_lot.location.location_name if reserve_lot is not None else None
        ),
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
    lots = {
        row.id: row
        for row in db.scalars(
            select(InventoryLot)
            .options(selectinload(InventoryLot.location))
            .where(InventoryLot.id.in_(lot_ids))
        ).all()
    } if lot_ids else {}
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
            "finished_location_name": finished_lot.location.location_name if finished_lot is not None else None,
            "reserve_location_name": reserve_lot.location.location_name if reserve_lot is not None else None,
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

    if allocation.production_completion_id is not None:
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
