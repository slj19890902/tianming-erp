from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
import json
from math import ceil
from typing import Literal, Sequence

from sqlalchemy import String, and_, case, cast, exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, aliased, selectinload

from app.core.time_contract import (
    beijing_date_bounds_utc_naive,
    beijing_today,
    utc_naive_to_api,
    utc_now_naive,
)
from app.models.customer import Customer
from app.models.delivery import Delivery, DeliveryItem
from app.models.incoming_receipt import IncomingReceiptItem
from app.models.mold_tool import MoldTool
from app.models.order import Order, OrderItem
from app.models.purchase_receipt import (
    IncomingReceiptPurposeAllocation,
    IncomingReceiptPurposeReversal,
)
from app.models.product import Product
from app.models.printing_plate import PrintingPlate
from app.models.product_bom import (
    BomComponentDirectDeliveryAllocation,
    RequisitionItemBomSource,
    SalesOrderItemBomComponent,
)
from app.models.production import (
    ProductionCompletion,
    ProductionCompletionBatch,
    ProductionStockTransfer,
    ProductionTask,
)
from app.models.user import User
from app.models.warehouse_inventory import (
    InventoryLot,
    InventoryLocationMovement,
    InventoryMovement,
    InventoryPallet,
    InventoryPalletItem,
    InventoryReservation,
    OrderItemSemiRequirement,
    WarehouseArea,
    WarehouseAreaStoragePolicy,
    WarehouseFloor,
    WarehouseGroundLayoutPlan,
    WarehouseGroundLayoutSlot,
    WarehouseGroundOccupancy,
    WarehouseGroundOccupancySlot,
    WarehouseGroundPlacementMutation,
    WarehouseLocation,
)
from app.services.production_station_routing import production_station_memberships
from app.services.production_task_profile import (
    new_task_profile_snapshot,
    resolved_task_profile,
)
from app.services.composite_bom_workflow import (
    CompositeBomWorkflowError,
    component_available_quantity,
    effective_component_demands,
    is_composite_order_item,
    kit_availability,
)
from app.services.location_candidates import (
    claim_active_placed_location,
    has_space_ledger,
    location_has_live_inventory,
    list_operational_locations,
    operational_location_issue,
)
from app.services.production_label_strategy import (
    ProductionLabelStrategyError,
    build_new_task_production_label_snapshot,
)
from app.services.printing_colors import parse_printing_colors
from app.services.product_specification import resolved_product_specification
from app.services.requisition_quantities import cutting_factor
from app.services.warehouse_inventory import (
    WarehouseInventoryError,
    active_finished_reserved_qty,
    inventory_fifo_order_columns,
    manual_finished_in,
    release_finished_reservation,
    reserve_completed_finished_inventory,
)
from app.services.warehouse_inventory import _balances, _movement
from app.services.warehouse_location_address import employee_location_name


Disposition = Literal["direct", "stock"]
CompletionType = Literal["primary", "supplemental"]

WAITING_MATERIAL = "waiting_material"
PENDING = "pending"
COMPLETED = "completed"
NOT_REQUIRED = "not_required"
READY_TASK_STATUSES = frozenset({COMPLETED, NOT_REQUIRED})
PRODUCTION_STATIONS = frozenset({"printing", "die_cut"})
PRODUCIBLE_ORDER_STATUSES = frozenset(
    {"pending_confirmation", "pending_production", "production"}
)
MUTABLE_ORDER_STATUSES = frozenset(
    {*PRODUCIBLE_ORDER_STATUSES, "pending_delivery"}
)
TEMPORARY_LOCATION_CODES = frozenset(
    [*(f"F12-P{number:02d}" for number in range(1, 9))]
    + [*(f"F34-P{number:02d}" for number in range(1, 4))]
)
DIRECT_DELIVERY_STAGING_LOCATION_CODE = "F1-DISPATCH-01"
DIRECT_DISPATCH_PALLET_KEY_PREFIX = "PRODUCTION_COMPLETION"
RECEIPT_FIN_STAGING_AREA_CODES = ("FIN-001", "FIN-002", "FIN-003")
_PRINTING_PLATE_COUNTS = {
    "单色印刷": 1,
    "双色印刷": 2,
    "三色印刷": 3,
    # Historical common-box records used this wording for three plates.
    "多色印刷": 3,
}
_PRINTING_PLATE_COUNT_LABELS = {1: "单色", 2: "双色", 3: "三色"}


class ProductionWorkflowError(ValueError):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def _claim_production_destination(
    db: Session,
    location_id: int,
    *,
    expected_layout_version: int | None = None,
) -> None:
    try:
        claimed = claim_active_placed_location(
            db,
            location_id,
            expected_layout_version=expected_layout_version,
        )
    except OperationalError as error:
        raise ProductionWorkflowError(
            "目标库位正在被其他入库、移位或布局操作使用，请稍后重试", 409
        ) from error
    if not claimed:
        raise ProductionWorkflowError(
            "目标库位已停用、尚未完成空间放置或地图状态已变化，请刷新后重试",
            409,
        )


def _validate_task_status_quantity(status: str, planned_quantity: int) -> None:
    """Reject an invalid task state before the database constraint does.

    This keeps receiving failures in the business-error path, where the whole
    receipt transaction is rolled back with an actionable 409 response.
    """

    quantity = int(planned_quantity or 0)
    if status in {PENDING, COMPLETED} and quantity <= 0:
        raise ProductionWorkflowError(
            "实收纸板换算后的生产数量不足1只，无法进入待生产；"
            "请核对实收数量、开料方式和单双拼设置",
            409,
        )
    if status in {WAITING_MATERIAL, NOT_REQUIRED} and quantity != 0:
        raise ProductionWorkflowError(
            "生产任务状态与计划数量不一致，本次操作未保存；请刷新后重试",
            409,
        )


def is_production_task_status_quantity_conflict(error: IntegrityError) -> bool:
    """Return true only for the production-task status/quantity constraint."""

    original = getattr(error, "orig", None)
    diagnostic = getattr(original, "diag", None)
    if (
        getattr(diagnostic, "constraint_name", None)
        == "ck_production_tasks_status_quantity"
    ):
        return True
    return "ck_production_tasks_status_quantity" in str(original or error)


def _new_task_printing_snapshot(db: Session, product: Product | None) -> dict:
    """Freeze common-box printing setup once when a production task is created."""

    if product is None or product.printing_plate_mode != "plate":
        colors = (
            parse_printing_colors(product.printing_colors)
            if product is not None
            else []
        )
        return {
            "printing_plate_mode_snapshot": "no_plate",
            "print_content_snapshot": product.print_content if product is not None else None,
            "printing_colors_snapshot": json.dumps(colors, ensure_ascii=False),
            "printing_plate_codes_snapshot": "[]",
            "printing_plate_details_snapshot": "[]",
            "plate_alignment_value_mm_snapshot": None,
            "plate_mount_value_mm_snapshot": None,
            "machine_set_length_mm_snapshot": None,
            "machine_set_width_mm_snapshot": None,
            "machine_set_height_mm_snapshot": None,
        }
    plate_slots = (
        product.printing_plate_1_id,
        product.printing_plate_2_id,
        product.printing_plate_3_id,
    )
    required_plate_count = _PRINTING_PLATE_COUNTS.get(
        str(product.print_content or "").strip()
    )
    if (
        required_plate_count is None
        or any(plate_slots[index] is None for index in range(required_plate_count))
        or any(
            plate_slots[index] is not None
            for index in range(required_plate_count, len(plate_slots))
        )
    ):
        raise ProductionWorkflowError(
            "常用箱印刷色数与挂板顺序不一致，请先核对后再建生产任务",
            409,
        )
    plate_ids = [int(plate_slots[index]) for index in range(required_plate_count)]
    plates = {
        row.id: row
        for row in db.scalars(
            select(PrintingPlate).where(PrintingPlate.id.in_(plate_ids))
        ).all()
    }
    if (
        len(plate_ids) != len(set(plate_ids))
        or any(plate_id not in plates for plate_id in plate_ids)
    ):
        raise ProductionWorkflowError("常用箱挂板资料不完整，请先核对挂板后再建生产任务", 409)
    selected_plates = [plates[plate_id] for plate_id in plate_ids]
    if any(
        plate.status != "active" or plate.customer_id != product.customer_id
        for plate in selected_plates
    ):
        raise ProductionWorkflowError("常用箱挂板已停用或不属于当前客户，请先核对", 409)
    codes = [plate.plate_code for plate in selected_plates]
    details = [
        {
            "plate_code": plate.plate_code,
            "plate_name": plate.plate_name,
            "color_name": plate.color_name,
        }
        for plate in selected_plates
    ]
    return {
        "printing_plate_mode_snapshot": "plate",
        "print_content_snapshot": product.print_content,
        "printing_colors_snapshot": json.dumps(
            [plate.color_name for plate in selected_plates],
            ensure_ascii=False,
        ),
        "printing_plate_codes_snapshot": json.dumps(codes, ensure_ascii=False),
        "printing_plate_details_snapshot": json.dumps(details, ensure_ascii=False),
        "plate_alignment_value_mm_snapshot": product.plate_alignment_value_mm,
        "plate_mount_value_mm_snapshot": product.plate_mount_value_mm,
        "machine_set_length_mm_snapshot": product.machine_set_length_mm,
        "machine_set_width_mm_snapshot": product.machine_set_width_mm,
        "machine_set_height_mm_snapshot": product.machine_set_height_mm,
    }


def _new_task_label_snapshot(
    product: Product | None,
    *,
    total_quantity: int,
) -> dict[str, object]:
    try:
        return build_new_task_production_label_snapshot(
            product,
            total_quantity=total_quantity,
        )
    except ProductionLabelStrategyError as error:
        raise ProductionWorkflowError(str(error), 409) from error


def _task_printing_snapshot(task: ProductionTask) -> dict:
    try:
        codes = json.loads(task.printing_plate_codes_snapshot or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        codes = []
    if not isinstance(codes, list):
        codes = []
    try:
        details = json.loads(task.printing_plate_details_snapshot or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        details = []
    if not isinstance(details, list):
        details = []
    safe_details = [
        {
            "plate_code": str(value.get("plate_code") or "").strip(),
            "plate_name": str(value.get("plate_name") or "").strip() or None,
            "color_name": str(value.get("color_name") or "").strip(),
        }
        for value in details
        if isinstance(value, dict) and str(value.get("plate_code") or "").strip()
    ]
    colors_frozen = task.printing_colors_snapshot is not None
    try:
        colors = json.loads(task.printing_colors_snapshot or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        colors = []
    if not isinstance(colors, list):
        colors = []
    safe_colors = [str(value).strip() for value in colors if str(value).strip()]
    if not colors_frozen and task.printing_plate_mode_snapshot == "plate":
        # P1-16D-4 already froze plate colours inside the legacy detail JSON.
        safe_colors = [
            str(value.get("color_name") or "").strip()
            for value in safe_details
            if str(value.get("color_name") or "").strip()
        ]
        colors_frozen = bool(safe_details)
    content = str(task.print_content_snapshot or "").strip()
    no_print = content in {"", "无印刷", "无", "否", "不印刷"}
    plate_count = len(safe_details) or len(
        [value for value in codes if str(value).strip()]
    )
    plate_count_label = _PRINTING_PLATE_COUNT_LABELS.get(
        plate_count,
        "颜色数未冻结",
    )
    printing_situation = (
        "无印刷"
        if no_print
        else f"挂板印刷（{plate_count_label}）"
        if task.printing_plate_mode_snapshot == "plate"
        else content
    )
    return {
        "print_content": task.print_content_snapshot,
        "printing_situation": printing_situation,
        "printing_plate_mode": task.printing_plate_mode_snapshot or "no_plate",
        "printing_plate_codes": [str(value) for value in codes if str(value).strip()],
        "printing_plates": safe_details,
        "printing_colors": safe_colors,
        "printing_colors_frozen": colors_frozen,
        "plate_alignment_value_mm": task.plate_alignment_value_mm_snapshot,
        "plate_mount_value_mm": task.plate_mount_value_mm_snapshot,
        "machine_set_length_mm": task.machine_set_length_mm_snapshot,
        "machine_set_width_mm": task.machine_set_width_mm_snapshot,
        "machine_set_height_mm": task.machine_set_height_mm_snapshot,
        "printing_instruction": (
            "无需印刷"
            if (task.print_content_snapshot or "").strip() in {"", "无印刷", "无", "否", "不印刷"}
            else (
                "按挂板编号安装并核对机器设定值"
                if task.printing_plate_mode_snapshot == "plate"
                else "不挂板：按图纸核对印刷内容，注意印刷尺寸偏差"
            )
        ),
    }


def _annotate_printing_plate_current_locations(
    db: Session,
    rows: Sequence[dict],
) -> None:
    """Attach live plate locations without changing the immutable task snapshot."""

    codes = {
        str(plate.get("plate_code") or "").strip()
        for row in rows
        for plate in (row.get("printing_plates") or [])
        if isinstance(plate, dict) and str(plate.get("plate_code") or "").strip()
    }
    if not codes:
        return
    locations = {
        str(code): location
        for code, location in db.execute(
            select(PrintingPlate.plate_code, PrintingPlate.rack_location).where(
                PrintingPlate.plate_code.in_(codes)
            )
        ).all()
    }
    for row in rows:
        for plate in row.get("printing_plates") or []:
            if not isinstance(plate, dict):
                continue
            plate["current_location"] = locations.get(
                str(plate.get("plate_code") or "").strip()
            )


@dataclass(frozen=True)
class CompletionCommand:
    task_id: int
    expected_version: int
    disposition: Disposition
    completion_type: CompletionType = "primary"
    material_input_quantity: int | None = None
    actual_output_quantity: int | None = None
    defective_quantity: int | None = None
    direct_delivery_quantity: int | None = None
    location_id: int | None = None
    expected_layout_version: int | None = None
    pallet_id: int | None = None
    pallet_code: str | None = None
    remarks: str | None = None


@dataclass(frozen=True)
class StockTransferCommand:
    location_id: int
    idempotency_key: str
    expected_layout_version: int | None = None
    pallet_id: int | None = None
    pallet_code: str | None = None
    remarks: str | None = None


@dataclass(frozen=True)
class CompletionBatchResult:
    batch: ProductionCompletionBatch
    completions: tuple[ProductionCompletion, ...]
    replayed: bool


@dataclass(frozen=True)
class StockTransferResult:
    transfer: ProductionStockTransfer
    replayed: bool


@dataclass(frozen=True)
class CompletionReversalResult:
    completion: ProductionCompletion
    transfer: ProductionStockTransfer | None
    inventory_lot_id: int | None
    reversed_semi_movement_ids: tuple[int, ...]


def _stable_key(*parts: object, max_length: int = 100) -> str:
    raw = ":".join(str(part).strip() for part in parts)
    if len(raw) <= max_length:
        return raw
    digest = sha256(raw.encode("utf-8")).hexdigest()[:24]
    return f"{raw[: max_length - 25]}:{digest}"


def _normalized_text(value: str | None) -> str | None:
    normalized = (value or "").strip()
    return normalized or None


def _canonical_hash(payload: dict) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def cutting_output_factor(value: str | None) -> int:
    """Return the immutable order-line sheet-to-product output factor."""
    return cutting_factor(value)


def production_pieces_per_box(item: OrderItem) -> int:
    """Return how many produced pieces are required for one deliverable box."""
    splice_mode = (item.snapshot_splice_mode or "").strip().lower()
    if splice_mode != "double":
        return 1
    snapshot = int(item.snapshot_pieces_per_box or 0)
    if snapshot > 0:
        return snapshot
    return 2


def production_output_quantity(
    material_input_quantity: int,
    output_factor: int,
    pieces_per_box: int,
) -> int:
    """Convert material sheets to finished boxes without mixing the two units."""
    input_quantity = max(int(material_input_quantity or 0), 0)
    factor = max(int(output_factor or 1), 1)
    pieces = max(int(pieces_per_box or 1), 1)
    return input_quantity * factor // pieces


def production_input_quantity(
    finished_quantity: int,
    output_factor: int,
    pieces_per_box: int,
) -> int:
    """Return the minimum material sheets needed for a finished-box target."""
    target = max(int(finished_quantity or 0), 0)
    factor = max(int(output_factor or 1), 1)
    pieces = max(int(pieces_per_box or 1), 1)
    return ceil(target * pieces / factor)


def normalized_completion_output(
    item: OrderItem,
    completion: ProductionCompletion,
) -> int:
    """Read old double-splice bug records in finished-box units.

    The buggy writer stored ``input * cutting factor`` as both planned and
    actual output, ignoring the frozen pieces-per-box snapshot.  Detect only
    that exact legacy signature so valid historical completion facts keep
    their recorded quantities.
    """
    recorded = max(int(completion.actual_output_quantity or 0), 0)
    pieces_per_box = production_pieces_per_box(item)
    if pieces_per_box <= 1:
        return recorded
    material_input = max(int(completion.material_input_quantity or 0), 0)
    factor = cutting_output_factor(item.special_process)
    buggy_piece_output = material_input * factor
    if (
        int(completion.planned_output_quantity or 0) == buggy_piece_output
        and recorded == buggy_piece_output
    ):
        return production_output_quantity(
            material_input,
            factor,
            pieces_per_box,
        )
    return recorded


def _material_quantity_facts(db: Session, item: OrderItem) -> tuple[int, int]:
    """Return (received, allowed production input) from posted receipt facts.

    Over-receipt contributes beyond the purchase plan only when the latest
    resolved decision explicitly says all_to_production.  Surplus transferred
    to semi-finished inventory is deliberately excluded.
    """
    rows = db.scalars(
        select(IncomingReceiptItem)
        .where(
            IncomingReceiptItem.order_item_id == item.id,
            IncomingReceiptItem.status == "posted",
        )
        .order_by(IncomingReceiptItem.id)
    ).all()
    if not rows:
        return 0, 0
    received = sum(int(row.received_quantity or 0) for row in rows)
    latest = rows[-1]
    planned = max(int(latest.planned_quantity or item.quantity or 0), 0)
    if latest.resolution_action == "all_to_production":
        material_input = received
    elif latest.resolution_action == "transfer_to_semi_inventory":
        material_input = min(received, planned)
    else:
        material_input = received if received <= planned else planned
    return received, max(material_input, 0)


def completion_batch_request_hash(
    idempotency_key: str,
    commands: Sequence[CompletionCommand],
) -> str:
    items = []
    for command in sorted(commands, key=lambda row: row.task_id):
        item = asdict(command)
        item["remarks"] = _normalized_text(command.remarks)
        item["pallet_code"] = _normalized_text(command.pallet_code)
        items.append(item)
    return _canonical_hash(
        {"idempotency_key": idempotency_key.strip(), "items": items}
    )


def stock_transfer_request_hash(
    completion_id: int,
    command: StockTransferCommand,
) -> str:
    payload = asdict(command)
    payload["idempotency_key"] = command.idempotency_key.strip()
    payload["remarks"] = _normalized_text(command.remarks)
    payload["pallet_code"] = _normalized_text(command.pallet_code)
    return _canonical_hash({"completion_id": completion_id, **payload})


def has_production_completion_facts(
    db: Session,
    order_item_ids: Sequence[int],
) -> bool:
    normalized = sorted({int(value) for value in order_item_ids if int(value) > 0})
    if not normalized:
        return False
    return (
        db.scalar(
            select(ProductionCompletion.id)
            .where(
                ProductionCompletion.order_item_id.in_(normalized),
                ProductionCompletion.status == "posted",
            )
            .limit(1)
        )
        is not None
    )


def lock_order_rows_for_production_transition(
    db: Session,
    order_ids: Sequence[int],
) -> dict[int, Order]:
    """Serialize production facts with order termination, deletion, and rollback."""
    normalized = sorted({int(value) for value in order_ids if int(value) > 0})
    if not normalized:
        return {}
    for order_id in normalized:
        claimed = db.execute(
            update(Order)
            .where(Order.id == order_id)
            .values(status=Order.status)
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:
            raise ProductionWorkflowError("订单不存在或已被删除，不能继续生产操作", 409)
    db.flush()
    locked: dict[int, Order] = {}
    for order_id in normalized:
        order = db.get(Order, order_id)
        if order is None:
            raise ProductionWorkflowError("订单不存在或已被删除，不能继续生产操作", 409)
        db.refresh(order)
        locked[order_id] = order
    return locked


def _dispatched_delivery_order_item_ids(
    db: Session,
    order_item_ids: Sequence[int],
) -> set[int]:
    normalized = sorted(
        {int(value) for value in order_item_ids if int(value) > 0}
    )
    if not normalized:
        return set()
    return set(
        db.scalars(
            select(DeliveryItem.order_item_id)
            .join(Delivery, Delivery.id == DeliveryItem.delivery_id)
            .where(
                DeliveryItem.order_item_id.in_(normalized),
                Delivery.status == "dispatched",
            )
            .distinct()
        ).all()
    )


def has_dispatched_delivery_facts(
    db: Session,
    order_item_ids: Sequence[int],
) -> bool:
    return bool(_dispatched_delivery_order_item_ids(db, order_item_ids))


def _semi_inventory_covers(db: Session, order_item_id: int) -> bool:
    # Local import keeps the delivery inventory module free to call the
    # production-facts helper without creating a module import cycle.
    from app.services.semi_finished_inventory import inventory_fully_covers_order_item

    return inventory_fully_covers_order_item(db, order_item_id)


def _component_task_rows(
    db: Session,
    order_item_id: int,
) -> list[tuple[ProductionTask, SalesOrderItemBomComponent]]:
    """Return component tasks in immutable BOM display order."""
    return list(
        db.execute(
            select(ProductionTask, SalesOrderItemBomComponent)
            .join(
                SalesOrderItemBomComponent,
                SalesOrderItemBomComponent.id
                == ProductionTask.sales_order_item_bom_component_id,
            )
            .where(ProductionTask.order_item_id == order_item_id)
            .order_by(
                SalesOrderItemBomComponent.display_order,
                SalesOrderItemBomComponent.id,
            )
        ).all()
    )


def _component_semi_inventory_fully_covers(
    db: Session,
    *,
    snapshot_id: int,
    required_piece_quantity: int,
) -> bool:
    from app.services.semi_finished_inventory import (
        active_semi_requirement_credited_quantity,
    )

    requirement = db.scalar(
        select(OrderItemSemiRequirement).where(
            OrderItemSemiRequirement.sales_order_item_bom_component_id
            == snapshot_id
        )
    )
    return (
        requirement is not None
        and active_semi_requirement_credited_quantity(db, requirement.id)
        >= required_piece_quantity
    )


def _refresh_composite_production_tasks(
    db: Session,
    item: OrderItem,
    *,
    create_if_missing: bool,
) -> list[ProductionTask]:
    """Refresh every BOM snapshot independently, in component piece units.

    The parent sales-order item remains measured in sets.  Its BOM snapshots
    are the execution anchors, so one component's material coverage or
    completion never changes a sibling component's planned piece quantity.
    """
    try:
        demands = effective_component_demands(db, item.id)
    except CompositeBomWorkflowError as error:
        raise ProductionWorkflowError(str(error), 409) from error
    tasks: list[ProductionTask] = []
    now = utc_now_naive()
    for demand in demands:
        snapshot = db.get(SalesOrderItemBomComponent, demand.snapshot_id)
        if snapshot is None:
            raise ProductionWorkflowError("订单组件快照不存在", 409)
        output_factor = cutting_output_factor(
            snapshot.snapshot_component_default_cutting_mode
        )
        task = db.scalar(
            select(ProductionTask).where(
                ProductionTask.sales_order_item_bom_component_id == demand.snapshot_id
            )
        )
        initial_coverage: int | None = None
        if task is None:
            if not create_if_missing:
                continue
            initial_coverage = component_available_quantity(db, demand.snapshot_id)
            component_product = db.get(Product, snapshot.component_product_id)
            task = ProductionTask(
                order_item_id=item.id,
                sales_order_item_bom_component_id=demand.snapshot_id,
                task_role="component_internal",
                status=WAITING_MATERIAL,
                planned_quantity=0,
                finished_coverage_snapshot=0,
                ordered_quantity_snapshot=int(item.quantity or 0),
                material_received_quantity=0,
                material_input_quantity=0,
                output_factor=output_factor,
                readiness_basis=None,
                ready_at=None,
                version=1,
                **_new_task_printing_snapshot(db, component_product),
                **new_task_profile_snapshot(
                    db,
                    component_product,
                    item=item,
                    component_snapshot=snapshot,
                ),
                **_new_task_label_snapshot(
                    component_product,
                    total_quantity=max(
                        demand.required_piece_quantity - initial_coverage,
                        0,
                    ),
                ),
            )
            db.add(task)
            db.flush()

        # A completion is immutable.  Its task stays completed even if the
        # parent material flag later changes during a separate correction.
        if task.status == COMPLETED:
            tasks.append(task)
            continue

        coverage = (
            initial_coverage
            if initial_coverage is not None
            else component_available_quantity(db, demand.snapshot_id)
        )
        production_needed = max(demand.required_piece_quantity - coverage, 0)
        semi_inventory_ready = _component_semi_inventory_fully_covers(
            db,
            snapshot_id=demand.snapshot_id,
            required_piece_quantity=production_needed,
        )
        if coverage >= demand.required_piece_quantity:
            next_status = NOT_REQUIRED
            planned_quantity = 0
            material_input_quantity = 0
            readiness_basis = "component_finished_inventory"
        elif item.material_status == "received" or semi_inventory_ready:
            next_status = PENDING
            planned_quantity = production_needed
            material_input_quantity = ceil(
                planned_quantity / max(output_factor, 1)
            )
            readiness_basis = (
                "component_semi_finished_inventory"
                if semi_inventory_ready
                else "component_material_received"
            )
        else:
            next_status = WAITING_MATERIAL
            planned_quantity = 0
            material_input_quantity = 0
            readiness_basis = None

        _validate_task_status_quantity(next_status, planned_quantity)
        changed = (
            task.status != next_status
            or int(task.planned_quantity or 0) != planned_quantity
            or int(task.finished_coverage_snapshot or 0) != coverage
            or task.readiness_basis != readiness_basis
            or int(task.material_received_quantity or 0)
            != material_input_quantity
            or int(task.material_input_quantity or 0)
            != material_input_quantity
            or int(task.output_factor or 1) != output_factor
        )
        if changed:
            was_ready = task.status in READY_TASK_STATUSES
            is_ready = next_status in READY_TASK_STATUSES
            task.status = next_status
            task.planned_quantity = planned_quantity
            task.finished_coverage_snapshot = coverage
            task.material_received_quantity = material_input_quantity
            task.material_input_quantity = material_input_quantity
            task.output_factor = output_factor
            task.readiness_basis = readiness_basis
            task.ready_at = (
                task.ready_at if was_ready and is_ready else (now if is_ready else None)
            )
            task.version = int(task.version or 0) + 1
        tasks.append(task)
    db.flush()
    return tasks


def refresh_production_task(
    db: Session,
    order_item_id: int,
    *,
    create_if_missing: bool = False,
) -> ProductionTask | None:
    item = db.get(OrderItem, order_item_id)
    if item is None:
        raise ProductionWorkflowError("订单明细不存在", 404)
    if item.supply_mode_snapshot == "external_purchase":
        return db.scalar(
            select(ProductionTask).where(
                ProductionTask.order_item_id == item.id,
                ProductionTask.sales_order_item_bom_component_id.is_(None),
            )
        )
    if is_composite_order_item(db, item.id):
        tasks = _refresh_composite_production_tasks(
            db,
            item,
            create_if_missing=create_if_missing,
        )
        refresh_order_production_status(db, item.order_id)
        return tasks[0] if tasks else None
    task = db.scalar(
        select(ProductionTask).where(
            ProductionTask.order_item_id == item.id,
            ProductionTask.sales_order_item_bom_component_id.is_(None),
        )
    )
    initial_finished_coverage: int | None = None
    if task is None:
        if not create_if_missing:
            return None
        product = db.get(Product, item.product_id)
        order_quantity = int(item.quantity or 0)
        if order_quantity <= 0:
            raise ProductionWorkflowError("订单明细数量必须大于0", 409)
        initial_finished_coverage = min(
            max(active_finished_reserved_qty(db, item.id), 0),
            order_quantity,
        )
        task = ProductionTask(
            order_item_id=item.id,
            task_role="order_main",
            status=WAITING_MATERIAL,
            planned_quantity=0,
            finished_coverage_snapshot=0,
            ordered_quantity_snapshot=int(item.quantity or 0),
            material_received_quantity=0,
            material_input_quantity=0,
            output_factor=cutting_output_factor(item.special_process),
            readiness_basis=None,
            ready_at=None,
            version=1,
            **_new_task_printing_snapshot(db, product),
            **new_task_profile_snapshot(db, product, item=item),
            **_new_task_label_snapshot(
                product,
                total_quantity=max(order_quantity - initial_finished_coverage, 0),
            ),
        )
        db.add(task)
        db.flush()

    if task.status == COMPLETED or has_production_completion_facts(db, [item.id]):
        return task

    order_quantity = int(item.quantity or 0)
    if order_quantity <= 0:
        raise ProductionWorkflowError("订单明细数量必须大于0", 409)
    finished_coverage = (
        initial_finished_coverage
        if initial_finished_coverage is not None
        else min(
            max(active_finished_reserved_qty(db, item.id), 0),
            order_quantity,
        )
    )
    received_quantity, material_input_quantity = _material_quantity_facts(db, item)
    output_factor = cutting_output_factor(item.special_process)
    pieces_per_box = production_pieces_per_box(item)
    now = utc_now_naive()
    if finished_coverage >= order_quantity:
        next_status = NOT_REQUIRED
        planned_quantity = 0
        readiness_basis = "finished_inventory"
    elif item.material_status == "received":
        next_status = PENDING
        if material_input_quantity > 0:
            # Modern receipt facts already contain only the physical sheets
            # assigned to production after finished-goods coverage reduced the
            # requisition.  Subtracting that coverage again would double-count
            # it (for example: order 50, finished 30, receipt 20 -> plan 0).
            planned_quantity = production_output_quantity(
                material_input_quantity,
                output_factor,
                pieces_per_box,
            )
            readiness_basis = "incoming_receipt"
        else:
            planned_quantity = order_quantity - finished_coverage
            material_input_quantity = production_input_quantity(
                planned_quantity,
                output_factor,
                pieces_per_box,
            )
            received_quantity = material_input_quantity
            readiness_basis = "material_received"
    elif _semi_inventory_covers(db, item.id):
        next_status = PENDING
        planned_quantity = order_quantity - finished_coverage
        readiness_basis = "semi_finished_inventory"
    else:
        next_status = WAITING_MATERIAL
        planned_quantity = 0
        readiness_basis = None

    _validate_task_status_quantity(next_status, planned_quantity)
    state_changed = (
        task.status != next_status
        or int(task.planned_quantity or 0) != planned_quantity
        or int(task.finished_coverage_snapshot or 0) != finished_coverage
        or task.readiness_basis != readiness_basis
        or int(task.ordered_quantity_snapshot or 0) != order_quantity
        or int(task.material_received_quantity or 0) != received_quantity
        or int(task.material_input_quantity or 0) != material_input_quantity
        or int(task.output_factor or 1) != output_factor
    )
    if not state_changed:
        return task

    was_ready = task.status in {PENDING, NOT_REQUIRED}
    is_ready = next_status in {PENDING, NOT_REQUIRED}
    task.status = next_status
    task.planned_quantity = planned_quantity
    task.finished_coverage_snapshot = finished_coverage
    task.ordered_quantity_snapshot = order_quantity
    task.material_received_quantity = received_quantity
    task.material_input_quantity = material_input_quantity
    task.output_factor = output_factor
    task.readiness_basis = readiness_basis
    task.ready_at = task.ready_at if was_ready and is_ready else (now if is_ready else None)
    task.version = int(task.version or 0) + 1
    db.flush()
    refresh_order_production_status(db, item.order_id)
    return task


def create_or_refresh_production_task(
    db: Session,
    order_item_id: int,
) -> ProductionTask | None:
    return refresh_production_task(db, order_item_id, create_if_missing=True)


def refresh_existing_production_task(
    db: Session,
    order_item_id: int | None,
) -> ProductionTask | None:
    if order_item_id is None:
        return None
    return refresh_production_task(db, order_item_id, create_if_missing=False)


def refresh_order_production_status(db: Session, order_id: int) -> Order | None:
    order = db.get(Order, order_id)
    if order is None or order.status not in MUTABLE_ORDER_STATUSES:
        return order
    items = list(
        db.scalars(
            select(OrderItem).where(OrderItem.order_id == order.id).order_by(OrderItem.id)
        ).all()
    )
    if not items:
        return order

    item_states: list[str] = []
    for item in items:
        if is_composite_order_item(db, item.id):
            receipt_auto_main_task = db.scalar(
                select(ProductionTask).where(
                    ProductionTask.order_item_id == item.id,
                    ProductionTask.sales_order_item_bom_component_id.is_(None),
                    ProductionTask.task_role == "order_main",
                )
            )
            if receipt_auto_main_task is not None and _has_receipt_managed_frozen_source(
                db, order_item_ids=[item.id]
            ):
                item_states.append(receipt_auto_main_task.status)
                continue
            component_rows = _component_task_rows(db, item.id)
            demands = effective_component_demands(db, item.id)
            required_snapshot_ids = {
                demand.snapshot_id for demand in demands if demand.is_required
            }
            required_rows = [
                (task, snapshot)
                for task, snapshot in component_rows
                if snapshot.is_required
            ]
            if not required_snapshot_ids:
                item_states.append(NOT_REQUIRED)
            elif {snapshot.id for _task, snapshot in required_rows} != required_snapshot_ids:
                # A required snapshot without a task is still waiting; this is
                # expected before the caller creates component tasks.
                item_states.append(WAITING_MATERIAL)
            elif all(task.status in READY_TASK_STATUSES for task, _ in required_rows):
                item_states.append(COMPLETED)
            elif any(task.status == COMPLETED for task, _ in required_rows):
                # At least one required component is done, but the parent
                # remains un-deliverable until every required component can
                # form a set.
                item_states.append("component_in_progress")
            else:
                item_states.append(PENDING)
            continue

        task = db.scalar(
            select(ProductionTask).where(
                ProductionTask.order_item_id == item.id,
                ProductionTask.sales_order_item_bom_component_id.is_(None),
            )
        )
        if task is None:
            return order
        item_states.append(task.status)

    if all(state in READY_TASK_STATUSES for state in item_states):
        next_status = "pending_delivery"
    elif any(state in {COMPLETED, "component_in_progress"} for state in item_states):
        next_status = "production"
    else:
        next_status = "pending_production"
    if order.status != next_status:
        order.status = next_status
        db.flush()
    return order


def production_ready_quantity(db: Session, order_item: OrderItem | int) -> int:
    item = db.get(OrderItem, order_item) if isinstance(order_item, int) else order_item
    if item is None:
        raise ProductionWorkflowError("订单明细不存在", 404)
    if item.supply_mode_snapshot == "external_purchase":
        return db.scalar(
            select(ProductionTask).where(
                ProductionTask.order_item_id == item.id,
                ProductionTask.sales_order_item_bom_component_id.is_(None),
            )
        )
    receipt_auto_completions = db.scalars(
        select(ProductionCompletion).where(
            ProductionCompletion.order_item_id == item.id,
            ProductionCompletion.status == "posted",
            ProductionCompletion.origin == "receipt_auto",
        )
    ).all()
    if receipt_auto_completions:
        completion_quantity = sum(
            normalized_completion_output(item, completion)
            for completion in receipt_auto_completions
        )
        return max(completion_quantity, 0)
    if is_composite_order_item(db, item.id):
        try:
            return int(kit_availability(db, item.id)["available_sets"])
        except CompositeBomWorkflowError as error:
            raise ProductionWorkflowError(str(error), 409) from error
    completions = db.scalars(
        select(ProductionCompletion).where(
            ProductionCompletion.order_item_id == item.id,
            ProductionCompletion.status == "posted",
        )
    ).all()
    completion_quantity = sum(
        normalized_completion_output(item, completion)
        for completion in completions
    )
    if completion_quantity > 0:
        task = db.scalar(
            select(ProductionTask).where(
                ProductionTask.order_item_id == item.id,
                ProductionTask.sales_order_item_bom_component_id.is_(None),
            )
        )
        prior_inventory = int(task.finished_coverage_snapshot or 0) if task else 0
        return max(prior_inventory + completion_quantity, 0)
    return max(active_finished_reserved_qty(db, item.id), 0)


def _current_pallet(db: Session, location_id: int) -> InventoryPallet | None:
    return db.scalar(
        select(InventoryPallet).where(
            InventoryPallet.location_id == location_id,
            InventoryPallet.is_current.is_(True),
        )
    )


def _production_stock_location(
    db: Session,
    location_id: int | None,
    *,
    pallet_id: int | None,
    allowed_existing_pallet_id: int | None = None,
) -> WarehouseLocation:
    if location_id is None:
        raise ProductionWorkflowError("库存完工必须选择成品库位", 400)
    location = db.get(WarehouseLocation, location_id)
    if location is None:
        raise ProductionWorkflowError("成品库位不存在", 404)
    if not has_space_ledger(db) and (
        location.source_version != "V11"
        or location.warehouse_floor != 3
    ):
        raise ProductionWorkflowError(
            "生产完工库存仅允许进入已启用的三楼成品或共用库位",
            409,
        )
    if getattr(location, "placement_status", None) == "unplaced":
        raise ProductionWorkflowError(
            "该成品库位尚未完成空间放置，不能办理生产完工入库",
            409,
        )
    issue = operational_location_issue(
        db,
        location,
        warehouse_types={"finished", "shared"},
    )
    if issue:
        raise ProductionWorkflowError(
            f"{issue}，不能办理生产完工入库",
            409,
        )
    if location.location_code == DIRECT_DELIVERY_STAGING_LOCATION_CODE:
        raise ProductionWorkflowError(
            "一楼待送区只供直接待送使用，不能作为一般生产入库库位",
            409,
        )
    if (
        location_has_live_inventory(db, location.id)
        and allowed_existing_pallet_id is None
    ):
        raise ProductionWorkflowError("所选库位已有活动库存，请选择空位", 409)
    pallet = _current_pallet(db, location.id)
    if pallet is not None:
        item_exists = db.scalar(
            select(InventoryPalletItem.id)
            .where(InventoryPalletItem.pallet_id == pallet.id)
            .limit(1)
        )
        if (
            pallet_id != pallet.id
            or (
                item_exists is not None
                and allowed_existing_pallet_id != pallet.id
            )
        ):
            raise ProductionWorkflowError("所选三楼库位已占用，请选择空位", 409)
    elif pallet_id is not None:
        raise ProductionWorkflowError("指定栈板不在所选三楼库位", 409)
    return location


@dataclass(frozen=True)
class ReceiptAutoFinishedGroundTarget:
    plan: WarehouseGroundLayoutPlan
    slot: WarehouseGroundLayoutSlot
    location: WarehouseLocation
    layout_version: int
    capacity_warning: str | None


def _receipt_auto_finished_ground_targets(
    db: Session,
) -> list[ReceiptAutoFinishedGroundTarget]:
    plans = list(
        db.scalars(
            select(WarehouseGroundLayoutPlan)
            .join(WarehouseArea, WarehouseArea.id == WarehouseGroundLayoutPlan.area_id)
            .join(WarehouseFloor, WarehouseFloor.id == WarehouseArea.floor_id)
            .where(
                WarehouseFloor.floor_number == 1,
                WarehouseFloor.construction_status == "enabled",
                WarehouseArea.area_code.in_(RECEIPT_FIN_STAGING_AREA_CODES),
                WarehouseArea.construction_status == "enabled",
                WarehouseGroundLayoutPlan.status == "published",
            )
            .order_by(
                case(
                    *[
                        (WarehouseArea.area_code == code, index)
                        for index, code in enumerate(RECEIPT_FIN_STAGING_AREA_CODES)
                    ],
                    else_=len(RECEIPT_FIN_STAGING_AREA_CODES),
                ),
                WarehouseGroundLayoutPlan.id,
            )
            .options(
                selectinload(WarehouseGroundLayoutPlan.area).selectinload(
                    WarehouseArea.storage_policy
                ),
                selectinload(WarehouseGroundLayoutPlan.area).selectinload(
                    WarehouseArea.floor
                ),
                selectinload(WarehouseGroundLayoutPlan.slots)
                .selectinload(WarehouseGroundLayoutSlot.location)
                .selectinload(WarehouseLocation.floor3_layout),
            )
        ).unique()
    )
    if not plans:
        raise ProductionWorkflowError(
            "一楼成品待送区尚未发布地堆排位；请在区域规划中为 FIN-001～003 至少发布一个地堆排位",
            409,
        )

    valid_plan_found = False
    targets: list[ReceiptAutoFinishedGroundTarget] = []
    for plan in plans:
        policy = plan.area.storage_policy
        try:
            allowed_types = (
                json.loads(policy.allowed_inventory_types_json)
                if policy is not None
                else None
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            allowed_types = None
        if (
            policy is None
            or policy.status != "published"
            or policy.storage_layout not in {"pallet_ground", "mixed"}
            or policy.published_map_revision != plan.published_map_revision
            or not plan.published_map_revision
            or not isinstance(allowed_types, list)
            or "finished" not in {str(value).strip() for value in allowed_types}
        ):
            continue
        valid_plan_found = True
        for slot in sorted(plan.slots, key=lambda row: (row.route_sequence, row.id)):
            location = slot.location
            layout = location.floor3_layout
            if layout is None:
                continue
            issue = operational_location_issue(
                db,
                location,
                warehouse_types={"finished", "shared"},
                pallet_storage_only=True,
                require_published=True,
                require_map_geometry=True,
                required_inventory_type="finished",
                require_empty=True,
                # Capacity is advisory for automatic receipt completion.  The
                # physical slot itself must still be empty and formally mapped.
                capacity_source_location_id=location.id,
            )
            if issue:
                continue
            capacity_issue = operational_location_issue(
                db,
                location,
                warehouse_types={"finished", "shared"},
                pallet_storage_only=True,
                require_published=True,
                require_map_geometry=True,
                required_inventory_type="finished",
                require_empty=True,
            )
            targets.append(
                ReceiptAutoFinishedGroundTarget(
                    plan=plan,
                    slot=slot,
                    location=location,
                    layout_version=int(layout.version),
                    capacity_warning=(
                        capacity_issue
                        if capacity_issue == "该区域已达到现场确认的栈板容量"
                        else None
                    ),
                )
            )
    if targets:
        return targets
    if not valid_plan_found:
        raise ProductionWorkflowError(
            "FIN-001～003 的地堆排位与区域发布版本不一致；请重新核对并发布地堆排位",
            409,
        )
    raise ProductionWorkflowError(
        "FIN-001～003 当前没有可用的已发布空地堆位置；请先腾空或发布新的真实位置",
        409,
    )


def _receipt_auto_finished_ground_target(
    db: Session,
    *,
    claim: bool,
) -> ReceiptAutoFinishedGroundTarget:
    targets = _receipt_auto_finished_ground_targets(db)
    if not claim:
        return targets[0]
    for target in targets:
        try:
            claimed = claim_active_placed_location(
                db,
                target.location.id,
                expected_layout_version=target.layout_version,
            )
        except OperationalError as error:
            raise ProductionWorkflowError(
                "一楼待送位置正在被其他入库或地图操作使用，请稍后重试",
                409,
            ) from error
        if not claimed:
            continue
        issue = operational_location_issue(
            db,
            target.location,
            warehouse_types={"finished", "shared"},
            pallet_storage_only=True,
            require_published=True,
            require_map_geometry=True,
            required_inventory_type="finished",
            require_empty=True,
            capacity_source_location_id=target.location.id,
        )
        if issue is None:
            return target
    raise ProductionWorkflowError(
        "FIN-001～003 的可用位置刚刚发生变化，请刷新后重新确认收料",
        409,
    )


def _production_direct_staging_location(
    db: Session,
    *,
    require_receipt_ready: bool = False,
) -> WarehouseLocation:
    location = db.scalar(
        select(WarehouseLocation).where(
            WarehouseLocation.location_code == DIRECT_DELIVERY_STAGING_LOCATION_CODE
        )
    )
    if location is None:
        raise ProductionWorkflowError(
            "一楼待送区尚未建立，请先完成数据库升级后再确认直接待送",
            409,
        )
    issue = operational_location_issue(
        db,
        location,
        warehouse_types={"finished", "shared"},
        pallet_storage_only=require_receipt_ready,
        require_published=require_receipt_ready,
        require_map_geometry=require_receipt_ready,
        required_inventory_type="finished" if require_receipt_ready else None,
        # The combined staging area intentionally accepts several system
        # pallets at one area-level location.  Passing the same location as
        # capacity source keeps a full-area result as a warning rather than a
        # hard block while every publication/type/geometry gate still runs.
        capacity_source_location_id=(location.id if require_receipt_ready else None),
    )
    if issue:
        raise ProductionWorkflowError(f"一楼待送区不可用：{issue}", 409)
    if (
        location.warehouse_floor != 1
        or str(location.area_code or "").strip().upper() != "DISPATCH"
        or location.storage_type != "temporary_aisle"
        or (
            not require_receipt_ready
            and location.source_version != "P1-25C"
        )
    ):
        raise ProductionWorkflowError(
            "一楼待送区与正式 F1-DISPATCH-01 定义不一致，请先核对仓库台账",
            409,
        )
    return location


def receipt_auto_finished_location_projection(db: Session) -> dict[str, object]:
    """Return the authoritative employee-safe real FIN destination preview."""

    try:
        target = _receipt_auto_finished_ground_target(db, claim=False)
    except ProductionWorkflowError as error:
        return {
            "ready": False,
            "location_name": None,
            "layout_version": None,
            "capacity_warning": None,
            "issue": str(error),
        }
    return {
        "ready": True,
        "location_name": target.location.location_name,
        "layout_version": target.layout_version,
        "capacity_warning": target.capacity_warning,
        "issue": None,
    }


def _direct_dispatch_pallet_occupancy_key(completion_id: int) -> str:
    return f"{DIRECT_DISPATCH_PALLET_KEY_PREFIX}:{completion_id}"


def _direct_dispatch_pallet_code(completion_id: int) -> str:
    return f"PLT-F1-PC-{completion_id:010d}"


def _bind_direct_completion_lots_to_system_pallet(
    db: Session,
    *,
    completion: ProductionCompletion,
    order: Order,
    lots: Sequence[InventoryLot],
    location: WarehouseLocation,
    operator_id: int | None,
    ground_target: ReceiptAutoFinishedGroundTarget | None = None,
) -> InventoryPallet:
    """Bind one direct-production detail to one formal ERP system pallet.

    The first-floor dispatch area is an area-level staging location rather than
    a one-pallet physical slot.  ``location_occupancy_key`` keeps the existing
    one-pallet-per-location rule for ordinary locations while allowing one
    independently traceable pallet for each direct completion in this area.
    """

    is_legacy_dispatch = location.location_code == DIRECT_DELIVERY_STAGING_LOCATION_CODE
    if ground_target is not None:
        if (
            ground_target.location.id != location.id
            or str(location.area_code or "").strip().upper()
            not in RECEIPT_FIN_STAGING_AREA_CODES
        ):
            raise ProductionWorkflowError("收料自动成品的真实 FIN 地堆位置不一致", 409)
    elif not is_legacy_dispatch:
        raise ProductionWorkflowError("直接待送系统栈板缺少真实 FIN 地堆目标", 409)
    occupancy_key = _direct_dispatch_pallet_occupancy_key(completion.id)
    existing = db.scalar(
        select(InventoryPallet).where(
            InventoryPallet.location_id == location.id,
            InventoryPallet.location_occupancy_key == occupancy_key,
            InventoryPallet.is_current.is_(True),
        )
    )
    normalized_lots = sorted({lot.id: lot for lot in lots}.values(), key=lambda row: row.id)
    if not normalized_lots:
        raise ProductionWorkflowError("直接待送完工未生成成品库存批次，不能建立系统栈板", 409)
    for lot in normalized_lots:
        if (
            lot.inventory_type != "finished"
            or lot.status != "active"
            or lot.warehouse_location_id != location.id
            or lot.source_ref_type != "production_completion"
            or int(lot.source_ref_id or 0) != completion.id
            or lot.finished_detail is None
        ):
            raise ProductionWorkflowError("直接待送库存批次与本次完工事实不一致", 409)

    if existing is not None:
        linked_ids = {
            int(row.inventory_lot_id)
            for row in existing.items
            if row.inventory_lot_id is not None
        }
        expected_ids = {int(lot.id) for lot in normalized_lots}
        if linked_ids != expected_ids:
            raise ProductionWorkflowError("直接待送系统栈板幂等事实不一致", 409)
        if ground_target is not None:
            occupancy = db.scalar(
                select(WarehouseGroundOccupancy).where(
                    WarehouseGroundOccupancy.pallet_id == existing.id,
                    WarehouseGroundOccupancy.primary_location_id == location.id,
                    WarehouseGroundOccupancy.status == "active",
                )
            )
            if occupancy is None:
                raise ProductionWorkflowError("真实 FIN 地堆占用幂等事实不完整", 409)
        return existing
    if any(lot.pallet_item is not None for lot in normalized_lots):
        raise ProductionWorkflowError("直接待送库存批次已绑定其他栈板", 409)

    pallet = InventoryPallet(
        pallet_code=_direct_dispatch_pallet_code(completion.id),
        location_id=location.id,
        location_occupancy_key=occupancy_key,
        status="active",
        is_current=True,
        needs_relocation=True,
        remarks=f"生产完工明细 {completion.id} 直接待送系统栈板",
        created_by=operator_id,
        updated_by=operator_id,
    )
    db.add(pallet)
    db.flush()
    total_physical_quantity = 0
    for lot in normalized_lots:
        detail = lot.finished_detail
        assert detail is not None
        physical_quantity = (
            int(lot.quantity_available or 0)
            + int(lot.quantity_reserved or 0)
            + int(lot.quantity_damaged or 0)
        )
        if physical_quantity <= 0:
            raise ProductionWorkflowError("直接待送系统栈板数量必须大于0", 409)
        total_physical_quantity += physical_quantity
        db.add(
            InventoryPalletItem(
                pallet_id=pallet.id,
                inventory_lot_id=lot.id,
                customer_id=detail.owner_customer_id,
                product_id=detail.product_id,
                inventory_code=detail.inventory_code_snapshot,
                order_no=order.order_number,
                customer_name_snapshot=detail.owner_customer_name_snapshot,
                product_name=detail.product_name_snapshot,
                item_type="finished",
                quantity=physical_quantity,
                unit=lot.unit,
                match_status="matched",
                remarks=f"生产完工明细 {completion.id} 自动归栈",
                created_by=operator_id,
            )
        )
    now = utc_now_naive()
    db.add(
        InventoryLocationMovement(
            pallet_id=pallet.id,
            from_location_id=None,
            to_location_id=location.id,
            movement_type="create",
            operator_id=operator_id,
            moved_at=now,
            idempotency_key=_stable_key(
                "production-completion", completion.id, "direct-pallet"
            ),
            confirmed_at=now,
            pallet_version_before=None,
            pallet_version_after=1,
            remarks=f"生产完工明细 {completion.id} 直接待送自动建立系统栈板",
        )
    )
    if ground_target is not None:
        if operator_id is None:
            raise ProductionWorkflowError("真实 FIN 地堆占用缺少操作人", 409)
        occupied = db.scalar(
            select(WarehouseGroundOccupancySlot.id)
            .join(WarehouseGroundOccupancy)
            .where(
                WarehouseGroundOccupancySlot.location_id == location.id,
                WarehouseGroundOccupancySlot.status == "active",
                WarehouseGroundOccupancy.status == "active",
            )
            .limit(1)
        )
        if occupied is not None:
            raise ProductionWorkflowError("真实 FIN 地堆位置已被占用，请刷新后重试", 409)
        occupancy = WarehouseGroundOccupancy(
            pallet_id=pallet.id,
            primary_location_id=location.id,
            customer_id=order.customer_id,
            product_id=normalized_lots[0].finished_detail.product_id,
            footprint_kind="single",
            capacity_quantity=total_physical_quantity,
            status="active",
            version=1,
            created_by=operator_id,
        )
        db.add(occupancy)
        db.flush()
        db.add(
            WarehouseGroundOccupancySlot(
                occupancy_id=occupancy.id,
                location_id=location.id,
                slot_sequence=1,
                status="active",
            )
        )
        db.add(
            WarehouseGroundPlacementMutation(
                idempotency_key=_stable_key(
                    "incoming-auto", completion.id, "fin-ground-placement"
                ),
                request_hash=_canonical_hash(
                    {
                        "completion_id": completion.id,
                        "lot_ids": [lot.id for lot in normalized_lots],
                        "plan_id": ground_target.plan.id,
                        "slot_id": ground_target.slot.id,
                        "location_id": location.id,
                        "layout_version": ground_target.layout_version,
                        "quantity": total_physical_quantity,
                    }
                ),
                actor_user_id=operator_id,
                operation="finished_inbound",
                source_lot_id=None,
                result_lot_id=normalized_lots[0].id,
                occupancy_id=occupancy.id,
            )
        )
    db.flush()
    return pallet


def list_temporary_locations(db: Session) -> list[dict]:
    locations = list_operational_locations(
        db,
        warehouse_types={"finished", "shared"},
    )
    locations = [
        candidate
        for candidate in locations
        if candidate.location.location_code
        != DIRECT_DELIVERY_STAGING_LOCATION_CODE
    ]
    if not has_space_ledger(db):
        locations = [
            candidate
            for candidate in locations
            if candidate.location.source_version == "V11"
            and candidate.location.warehouse_floor == 3
        ]
    result: list[dict] = []
    for candidate in locations:
        location = candidate.location
        pallet = _current_pallet(db, location.id)
        occupied = candidate.occupied
        result.append(
            {
                "id": location.id,
                "warehouse_floor": location.warehouse_floor,
                "floor_id": candidate.floor.id if candidate.floor else None,
                "floor_code": (
                    candidate.floor.floor_code if candidate.floor else None
                ),
                "floor_name": (
                    candidate.floor.floor_name if candidate.floor else None
                ),
                "area_id": candidate.area.id if candidate.area else None,
                "area_code": location.area_code,
                "area_name": candidate.area.area_name if candidate.area else None,
                "location_code": location.location_code,
                "location_name": employee_location_name(location),
                "current_address_name": employee_location_name(location),
                "employee_location_name": employee_location_name(location),
                "layout_version": (
                    int(location.floor3_layout.version)
                    if location.floor3_layout is not None
                    else None
                ),
                "pallet_id": pallet.id if pallet is not None else None,
                "pallet_code": pallet.pallet_code if pallet is not None else None,
                "is_empty": not occupied,
                "is_temporary": bool(location.is_temporary),
                "storage_type": location.storage_type,
                "location_kind": (
                    "temporary" if location.is_temporary else "fixed"
                ),
                "occupancy_label": (
                    "空闲"
                    if not occupied
                    else "已占用"
                ),
            }
        )
    return result


def _consume_completion_semi_reservations(
    db: Session,
    *,
    completion: ProductionCompletion,
    task: ProductionTask,
    item: OrderItem,
    planned_quantity: int,
    operator_id: int | None,
) -> None:
    from app.services.semi_finished_inventory import consume_semi_finished_reservation

    requirement_query = select(OrderItemSemiRequirement).where(
        OrderItemSemiRequirement.order_item_id == item.id
    )
    if task.sales_order_item_bom_component_id is not None:
        # A component may consume only a semi-finished reservation bound to
        # the same BOM snapshot; parent-level reservations never cover it.
        requirement_query = requirement_query.where(
            OrderItemSemiRequirement.sales_order_item_bom_component_id
            == task.sales_order_item_bom_component_id
        )
    requirements = db.scalars(
        requirement_query.order_by(
            OrderItemSemiRequirement.component_type,
            OrderItemSemiRequirement.id,
        )
    ).all()
    require_full = task.readiness_basis in {
        "semi_finished_inventory",
        "component_semi_finished_inventory",
    }
    if task.sales_order_item_bom_component_id is not None and not requirements:
        if require_full:
            raise ProductionWorkflowError(
                "组件客户备料需求已不存在，请刷新生产任务后重试",
                409,
            )
        return
    product = db.get(Product, item.product_id)
    box_style = str(product.box_style or "") if product is not None else ""
    expected_components = (
        {"cover", "base"}
        if "天地盖" in box_style or "A3" in box_style.upper()
        else {"whole"}
    )
    by_component = {row.component_type: row for row in requirements}
    if require_full and not expected_components.issubset(by_component):
        missing = "、".join(sorted(expected_components - set(by_component)))
        raise ProductionWorkflowError(
            f"半成品齐套任务缺少{missing}组件需求，无法完成生产完工", 409
        )
    for requirement in requirements:
        reservations = db.scalars(
            select(InventoryReservation)
            .join(InventoryLot, InventoryLot.id == InventoryReservation.inventory_lot_id)
            .where(
                InventoryReservation.order_item_id == item.id,
                InventoryReservation.semi_requirement_id == requirement.id,
                InventoryReservation.reservation_type == "semi_order",
                InventoryReservation.status != "cancelled",
                InventoryReservation.reserved_stock_quantity
                > InventoryReservation.consumed_stock_quantity
                + InventoryReservation.released_stock_quantity,
            )
            .order_by(
                *inventory_fifo_order_columns(),
                InventoryReservation.id,
            )
        ).all()
        if not reservations:
            if require_full and requirement.component_type in expected_components:
                raise ProductionWorkflowError(
                    f"{requirement.component_type}半成品预占不存在，无法完成生产完工",
                    409,
                )
            continue
        target_pieces = planned_quantity * max(int(requirement.pieces_per_box or 1), 1)
        remaining_pieces = target_pieces
        for reservation in reservations:
            if remaining_pieces <= 0:
                break
            available_credit = max(
                int(reservation.credited_requirement_quantity or 0)
                - int(reservation.consumed_requirement_quantity or 0)
                - int(reservation.released_requirement_quantity or 0),
                0,
            )
            if available_credit <= 0:
                continue
            yield_factor = max(int(reservation.yield_factor or 1), 1)
            available_stock = (
                int(reservation.reserved_stock_quantity or 0)
                - int(reservation.consumed_stock_quantity or 0)
                - int(reservation.released_stock_quantity or 0)
            )
            stock_quantity = min(
                available_stock,
                ceil(min(remaining_pieces, available_credit) / yield_factor),
            )
            if stock_quantity <= 0:
                continue
            lot = db.get(InventoryLot, reservation.inventory_lot_id)
            if lot is None:
                raise ProductionWorkflowError("半成品库存批次不存在", 409)
            before_credit = int(reservation.consumed_requirement_quantity or 0)
            mutation = consume_semi_finished_reservation(
                db,
                reservation_id=reservation.id,
                stock_quantity=stock_quantity,
                expected_version=lot.version,
                operator_id=operator_id,
                idempotency_key=_stable_key(
                    "production-completion", completion.id, "semi", reservation.id
                ),
                delivery_item_id=None,
                reason="生产完工消耗半成品预占",
            )
            consumed_credit = (
                int(mutation.reservation.consumed_requirement_quantity or 0)
                - before_credit
            )
            remaining_pieces -= consumed_credit
        if (
            require_full
            and requirement.component_type in expected_components
            and remaining_pieces > 0
        ):
            raise ProductionWorkflowError(
                f"{requirement.component_type}半成品预占余额不足，无法完成生产完工",
                409,
            )


def _reserve_component_completion_lot(
    db: Session,
    *,
    completion: ProductionCompletion,
    order: Order,
    item: OrderItem,
    snapshot_id: int,
    lot: InventoryLot,
    operator_id: int | None,
    idempotency_key: str,
) -> InventoryReservation:
    """Reserve a just-created component lot for its immutable BOM snapshot.

    The legacy helper checks the parent product, which is intentionally wrong
    for a component.  This narrow variant preserves the same inventory
    movement and reservation invariants while binding both the parent order
    item and the component snapshot.
    """
    existing = db.scalar(
        select(InventoryReservation).where(
            InventoryReservation.idempotency_key == idempotency_key
        )
    )
    if existing is not None:
        if (
            existing.order_item_id != item.id
            or existing.inventory_lot_id != lot.id
            or existing.sales_order_item_bom_component_id != snapshot_id
        ):
            raise ProductionWorkflowError("组件完工库存预占幂等标识冲突", 409)
        return existing
    quantity = int(completion.quantity or 0)
    if quantity <= 0 or int(lot.quantity_available or 0) != quantity:
        raise ProductionWorkflowError("组件完工库存数量与完工事实不一致", 409)
    before = _balances(lot)
    expected_version = int(lot.version or 0)
    now = utc_now_naive()
    updated = db.execute(
        update(InventoryLot)
        .where(
            InventoryLot.id == lot.id,
            InventoryLot.version == expected_version,
            InventoryLot.quantity_available == quantity,
            InventoryLot.inventory_type == "finished",
            InventoryLot.status == "active",
        )
        .values(
            quantity_available=InventoryLot.quantity_available - quantity,
            quantity_reserved=InventoryLot.quantity_reserved + quantity,
            version=InventoryLot.version + 1,
            last_movement_at=now,
        )
    )
    if updated.rowcount != 1:
        raise ProductionWorkflowError("组件完工库存数量或版本已变化，请刷新后重试", 409)
    reservation = InventoryReservation(
        reservation_number=_stable_key("CPRS", completion.id, snapshot_id, max_length=50),
        inventory_lot_id=lot.id,
        reservation_type="finished_order",
        order_id=order.id,
        order_item_id=item.id,
        sales_order_item_bom_component_id=snapshot_id,
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
    refreshed_lot = db.get(InventoryLot, lot.id)
    assert refreshed_lot is not None
    _movement(
        db,
        lot=refreshed_lot,
        movement_type="reserve",
        quantity=quantity,
        before=before,
        operator_id=operator_id,
        reason="复合 BOM 组件生产完工自动预占",
        idempotency_key=idempotency_key,
        reservation_id=reservation.id,
        related_order_id=order.id,
        related_order_item_id=item.id,
    )
    db.flush()
    return reservation


def _parent_delivery_storage_target(
    db: Session,
    *,
    order_item_id: int,
) -> tuple[int | None, int | None]:
    """Return the one physical target already used by a parent-delivery kit.

    Component lots remain the quantity ledger, but a parent-delivery order is
    one physical finished product.  Once its first component is stored, later
    component completions must join the same location and, when present, the
    same system pallet.  This keeps the physical map truthful without creating
    a duplicate parent inventory lot.
    """

    rows = db.execute(
        select(
            InventoryLot.warehouse_location_id,
            InventoryPalletItem.pallet_id,
        )
        .join(
            InventoryReservation,
            InventoryReservation.inventory_lot_id == InventoryLot.id,
        )
        .outerjoin(
            InventoryPalletItem,
            InventoryPalletItem.inventory_lot_id == InventoryLot.id,
        )
        .where(
            InventoryReservation.order_item_id == order_item_id,
            InventoryReservation.sales_order_item_bom_component_id.is_not(None),
            InventoryReservation.status.in_(("active", "partial")),
            InventoryReservation.reserved_stock_quantity
            > InventoryReservation.consumed_stock_quantity
            + InventoryReservation.released_stock_quantity,
            InventoryLot.status.in_(("active", "frozen")),
            InventoryLot.source_type != "production_completion",
        )
    ).all()
    if not rows:
        return None, None
    location_ids = {int(location_id) for location_id, _pallet_id in rows}
    if len(location_ids) != 1:
        raise ProductionWorkflowError(
            "父件交付的历史子件已分散在多个库位，请先在移货模式合并到同一位置后再继续完工",
            409,
        )
    pallet_ids = {
        int(pallet_id) for _location_id, pallet_id in rows if pallet_id is not None
    }
    if len(pallet_ids) > 1:
        raise ProductionWorkflowError(
            "父件交付的历史子件已分散在多块栈板，请先合并为同一栈板后再继续完工",
            409,
        )
    return next(iter(location_ids)), next(iter(pallet_ids), None)


def _stock_completion_lot(
    db: Session,
    *,
    completion: ProductionCompletion,
    task: ProductionTask,
    order: Order,
    item: OrderItem,
    command: CompletionCommand | StockTransferCommand,
    operator_id: int | None,
    idempotency_prefix: str,
    location_id_override: int | None = None,
    receipt_ground_target: ReceiptAutoFinishedGroundTarget | None = None,
    source_type: str = "production_surplus",
    movement_reason: str = "生产完工入库",
) -> InventoryLot:
    snapshot = (
        db.get(SalesOrderItemBomComponent, task.sales_order_item_bom_component_id)
        if task.sales_order_item_bom_component_id is not None
        else None
    )
    pallet_id = command.pallet_id
    require_empty_pallet = True
    existing_location_id: int | None = None
    existing_pallet_id: int | None = None
    is_parent_delivery_component = (
        snapshot is not None
        and (item.composite_fulfillment_mode_snapshot or "component_delivery")
        == "parent_delivery"
    )
    if is_parent_delivery_component:
        existing_location_id, existing_pallet_id = _parent_delivery_storage_target(
            db,
            order_item_id=item.id,
        )
        if existing_pallet_id is not None:
            if pallet_id is not None and int(pallet_id) != existing_pallet_id:
                raise ProductionWorkflowError(
                    "父件交付的同一套产品必须归入同一栈板；请选择已存子件所在栈板",
                    409,
                )
            pallet_id = existing_pallet_id
            require_empty_pallet = False
        elif existing_location_id is not None:
            require_empty_pallet = False
    if location_id_override is not None:
        if receipt_ground_target is not None:
            if (
                getattr(completion, "origin", "manual") != "receipt_auto"
                or receipt_ground_target.location.id != location_id_override
            ):
                raise ProductionWorkflowError(
                    "收料自动成品的真实 FIN 位置校验失败，请刷新后重试",
                    409,
                )
            location = receipt_ground_target.location
        else:
            location = _production_direct_staging_location(db)
            if location.id != location_id_override:
                raise ProductionWorkflowError("一楼待送区库位已变化，请刷新后重试", 409)
    else:
        location = _production_stock_location(
            db,
            command.location_id,
            pallet_id=pallet_id,
            allowed_existing_pallet_id=existing_pallet_id,
        )
    product_id = snapshot.component_product_id if snapshot is not None else item.product_id
    if is_parent_delivery_component:
        if existing_location_id is not None and existing_location_id != location.id:
            raise ProductionWorkflowError(
                "父件交付的同一套产品必须存放在同一库位；请选择已存子件所在位置",
                409,
            )
    is_transfer = isinstance(command, StockTransferCommand)
    stock_quantity = (
        int(completion.quantity)
        if is_transfer or source_type == "production_completion"
        else int(completion.stock_quantity)
    )
    lot = manual_finished_in(
        db,
        customer_id=order.customer_id,
        product_id=product_id,
        location_id=location.id,
        quantity=stock_quantity,
        stock_date=beijing_today(),
        source_type=source_type,
        source_ref_type="production_completion",
        source_ref_id=completion.id,
        remarks=_normalized_text(command.remarks),
        operator_id=operator_id,
        idempotency_key=_stable_key(idempotency_prefix, "finished-in"),
        pallet_id=None if location_id_override is not None else pallet_id,
        pallet_code=(
            None
            if location_id_override is not None
            else _normalized_text(command.pallet_code)
        ),
        require_empty_pallet=require_empty_pallet,
        movement_reason=movement_reason,
        expected_layout_version=(
            int(location.floor3_layout.version)
            if location_id_override is not None
            and location.floor3_layout is not None
            else command.expected_layout_version
        ),
    )
    if snapshot is None:
        reserve_quantity = max(
            int(completion.order_reserved_quantity)
            - (
                0
                if is_transfer or source_type == "production_completion"
                else int(completion.direct_delivery_quantity)
            ),
            0,
        )
        if reserve_quantity > 0:
            reserve_completed_finished_inventory(
                db,
                order_item_id=item.id,
                inventory_lot_id=lot.id,
                quantity=reserve_quantity,
                expected_version=int(lot.version),
                operator_id=operator_id,
                idempotency_key=_stable_key(idempotency_prefix, "finished-reserve"),
            )
    else:
        _reserve_component_completion_lot(
            db,
            completion=completion,
            order=order,
            item=item,
            snapshot_id=snapshot.id,
            lot=lot,
            operator_id=operator_id,
            idempotency_key=_stable_key(idempotency_prefix, "component-finished-reserve"),
        )
    return lot


def _validate_commands(commands: Sequence[CompletionCommand]) -> None:
    if not commands:
        raise ProductionWorkflowError("完工批次至少包含一条生产任务")
    task_ids = [command.task_id for command in commands]
    if any(task_id <= 0 for task_id in task_ids):
        raise ProductionWorkflowError("生产任务编号无效")
    if len(set(task_ids)) != len(task_ids):
        raise ProductionWorkflowError("同一生产任务不能在批次中重复提交")
    for command in commands:
        if command.expected_version <= 0:
            raise ProductionWorkflowError("生产任务版本必须大于0")
        if command.disposition not in {"direct", "stock"}:
            raise ProductionWorkflowError("完工去向必须明确选择 direct 或 stock")
        if command.completion_type not in {"primary", "supplemental"}:
            raise ProductionWorkflowError("生产确认类型无效")
        if command.material_input_quantity is not None and command.material_input_quantity <= 0:
            raise ProductionWorkflowError("本次实际投入必须大于0")
        if command.actual_output_quantity is not None and command.actual_output_quantity <= 0:
            raise ProductionWorkflowError("本次实际合格产量必须大于0")
        if command.defective_quantity is not None and command.defective_quantity < 0:
            raise ProductionWorkflowError("次品或损耗不能小于0")
        if command.direct_delivery_quantity is not None and command.direct_delivery_quantity < 0:
            raise ProductionWorkflowError("直接待送数量不能小于0")
        if command.disposition == "direct" and command.location_id is None and any(
            value is not None
            for value in (command.pallet_id, command.pallet_code)
        ):
            raise ProductionWorkflowError("直接送货完工不能填写库存货位或栈板")
        if command.disposition == "stock" and command.location_id is None:
            raise ProductionWorkflowError("库存完工必须选择三楼成品库位")


def _replay_completion_batch(
    db: Session,
    *,
    batch: ProductionCompletionBatch,
    request_hash: str,
) -> CompletionBatchResult:
    if batch.request_hash != request_hash:
        raise ProductionWorkflowError("同一幂等键对应的完工内容不一致", 409)
    rows = db.scalars(
        select(ProductionCompletion)
        .where(ProductionCompletion.batch_id == batch.id)
        .order_by(ProductionCompletion.id)
    ).all()
    return CompletionBatchResult(batch, tuple(rows), True)


def _has_receipt_managed_frozen_source(
    db: Session,
    *,
    order_item_ids: Sequence[int],
) -> bool:
    """Return whether P1-81 already owns production for any sales line.

    Once a frozen-purpose receipt has posted, its order-level finished output is
    derived from the immutable cumulative receipt allocation.  Allowing the
    legacy manual completion path for either the main task or an internal BOM
    component task would post the same material a second time.
    """

    normalized_ids = sorted({int(item_id) for item_id in order_item_ids})
    if not normalized_ids:
        return False
    allocation_id = db.scalar(
        select(IncomingReceiptPurposeAllocation.id)
        .outerjoin(
            RequisitionItemBomSource,
            RequisitionItemBomSource.id
            == IncomingReceiptPurposeAllocation.source_bom_requisition_source_id,
        )
        .outerjoin(
            SalesOrderItemBomComponent,
            SalesOrderItemBomComponent.id
            == RequisitionItemBomSource.sales_order_item_bom_component_id,
        )
        .where(
            IncomingReceiptPurposeAllocation.status == "posted",
            IncomingReceiptPurposeAllocation.purpose_contract_status_snapshot
            == "frozen",
            ~exists(
                select(IncomingReceiptPurposeReversal.id).where(
                    IncomingReceiptPurposeReversal.incoming_receipt_purpose_allocation_id
                    == IncomingReceiptPurposeAllocation.id
                )
            ),
            or_(
                IncomingReceiptPurposeAllocation.source_order_item_id.in_(
                    normalized_ids
                ),
                SalesOrderItemBomComponent.sales_order_item_id.in_(normalized_ids),
            ),
        )
        .limit(1)
    )
    return allocation_id is not None


def complete_production_batch(
    db: Session,
    *,
    idempotency_key: str,
    commands: Sequence[CompletionCommand],
    operator_id: int | None,
) -> CompletionBatchResult:
    key = idempotency_key.strip()
    if not key or len(key) > 120:
        raise ProductionWorkflowError("幂等键长度必须为1到120个字符")
    _validate_commands(commands)
    request_hash = completion_batch_request_hash(key, commands)
    existing_batch = db.scalar(
        select(ProductionCompletionBatch).where(
            ProductionCompletionBatch.idempotency_key == key
        )
    )
    if existing_batch is not None:
        return _replay_completion_batch(
            db, batch=existing_batch, request_hash=request_hash
        )

    destination_versions: dict[int, set[int | None]] = {}
    for command in commands:
        if command.location_id is not None:
            destination_versions.setdefault(
                int(command.location_id), set()
            ).add(command.expected_layout_version)
    for location_id in sorted(destination_versions):
        versions = destination_versions[location_id]
        if len(versions) != 1:
            raise ProductionWorkflowError(
                "同一目标库位的地图版本不一致，请刷新后重试",
                409,
            )
        _claim_production_destination(
            db,
            location_id,
            expected_layout_version=next(iter(versions)),
        )

    task_ids = [command.task_id for command in commands]
    initial_rows = db.execute(
        select(ProductionTask, OrderItem, Order)
        .join(OrderItem, OrderItem.id == ProductionTask.order_item_id)
        .join(Order, Order.id == OrderItem.order_id)
        .where(ProductionTask.id.in_(task_ids))
        .order_by(ProductionTask.id)
    ).all()
    if len(initial_rows) != len(task_ids):
        raise ProductionWorkflowError("生产任务不存在或已被删除", 404)
    lock_order_rows_for_production_transition(
        db,
        [order.id for _task, _item, order in initial_rows],
    )
    concurrent_batch = db.scalar(
        select(ProductionCompletionBatch).where(
            ProductionCompletionBatch.idempotency_key == key
        )
    )
    if concurrent_batch is not None:
        return _replay_completion_batch(
            db, batch=concurrent_batch, request_hash=request_hash
        )
    rows = db.execute(
        select(ProductionTask, OrderItem, Order)
        .join(OrderItem, OrderItem.id == ProductionTask.order_item_id)
        .join(Order, Order.id == OrderItem.order_id)
        .where(ProductionTask.id.in_(task_ids))
        .order_by(ProductionTask.id)
        .execution_options(populate_existing=True)
    ).all()
    if len(rows) != len(task_ids):
        raise ProductionWorkflowError("生产任务或关联订单已被删除，不能确认完工", 409)
    if _has_receipt_managed_frozen_source(
        db,
        order_item_ids=[item.id for _task, item, _order in rows],
    ):
        raise ProductionWorkflowError(
            "该订单已由冻结收料用途自动形成成品，不能再手工确认生产完工",
            409,
        )
    by_task = {task.id: (task, item, order) for task, item, order in rows}
    if len({order.customer_id for _, _, order in rows}) != 1:
        raise ProductionWorkflowError("一个完工批次只能包含同一客户的生产任务", 409)

    prepared: dict[int, dict[str, int | str]] = {}
    for command in commands:
        task, item, order = by_task[command.task_id]
        is_component_task = task.sales_order_item_bom_component_id is not None
        allowed_statuses = (
            PRODUCIBLE_ORDER_STATUSES
            if command.completion_type == "primary"
            else MUTABLE_ORDER_STATUSES
        )
        expected_task_status = (
            PENDING if command.completion_type == "primary" else COMPLETED
        )
        if order.status not in allowed_statuses:
            raise ProductionWorkflowError(
                "订单当前状态不允许继续生产完工，请刷新后重试", 409
            )
        if item.is_force_closed:
            raise ProductionWorkflowError("订单明细已强制关闭，不能继续生产确认", 409)
        if task.status != expected_task_status:
            raise ProductionWorkflowError(
                "主生产确认仅允许待完工任务；补充确认仅允许已完工任务",
                409,
            )
        if int(task.version) != command.expected_version:
            raise ProductionWorkflowError("生产任务版本已变化，请刷新后重试", 409)
        if command.completion_type == "primary" and int(item.delivered_quantity or 0) > 0:
            raise ProductionWorkflowError("订单明细已送货，不能再执行主生产确认", 409)
        if command.completion_type == "primary":
            existing_primary = db.scalar(
                select(ProductionCompletion.id)
                .where(
                    ProductionCompletion.task_id == task.id,
                    ProductionCompletion.status == "posted",
                    ProductionCompletion.completion_type == "primary",
                )
                .limit(1)
            )
            if existing_primary is not None:
                raise ProductionWorkflowError("该生产任务已存在主完工事实", 409)

        received_now, allowed_input_now = _material_quantity_facts(db, item)
        factor = cutting_output_factor(item.special_process)
        pieces_per_box = production_pieces_per_box(item)
        if is_component_task:
            component_snapshot = db.get(
                SalesOrderItemBomComponent,
                task.sales_order_item_bom_component_id,
            )
            if component_snapshot is None:
                raise ProductionWorkflowError("订单组件快照不存在", 409)
            factor = cutting_output_factor(
                component_snapshot.snapshot_component_default_cutting_mode
            )
            pieces_per_box = 1
            component_input = ceil(
                max(int(task.planned_quantity or 0), 0) / max(factor, 1)
            )
            received_now = max(
                int(task.material_received_quantity or 0),
                component_input,
            )
            allowed_input_now = max(
                int(task.material_input_quantity or 0),
                component_input,
            )
        if allowed_input_now <= 0:
            allowed_input_now = int(task.material_input_quantity or 0)
        if allowed_input_now <= 0:
            allowed_input_now = production_input_quantity(
                max(int(task.planned_quantity or 0), 0),
                factor,
                pieces_per_box,
            )
            received_now = max(received_now, allowed_input_now)
        prior_input = int(
            db.scalar(
                select(
                    func.coalesce(
                        func.sum(ProductionCompletion.material_input_quantity),
                        0,
                    )
                ).where(
                    ProductionCompletion.task_id == task.id,
                    ProductionCompletion.status == "posted",
                )
            )
            or 0
        )
        available_input = max(allowed_input_now - prior_input, 0)
        material_input = int(
            command.material_input_quantity
            if command.material_input_quantity is not None
            else available_input
        )
        if material_input <= 0 or material_input > available_input:
            raise ProductionWorkflowError(
                f"本次实际投入超过可投入余额 {available_input}",
                409,
            )
        planned_output = production_output_quantity(
            material_input,
            factor,
            pieces_per_box,
        )
        if planned_output <= 0:
            raise ProductionWorkflowError(
                f"本次实际投入不足以组成 1 个成品；每箱需要 {pieces_per_box} 片",
                409,
            )
        actual_output = int(
            command.actual_output_quantity
            if command.actual_output_quantity is not None
            else planned_output
        )
        if actual_output <= 0 or actual_output > planned_output:
            raise ProductionWorkflowError(
                f"实际合格产量不能超过理论产量 {planned_output}",
                409,
            )
        defective_quantity = int(
            command.defective_quantity
            if command.defective_quantity is not None
            else planned_output - actual_output
        )
        if actual_output + defective_quantity != planned_output:
            raise ProductionWorkflowError(
                "实际合格产量与次品/损耗之和必须等于理论产量",
                409,
            )
        coverage_target_quantity = int(item.quantity or 0)
        if is_component_task:
            component_demand = next(
                (
                    row
                    for row in effective_component_demands(db, item.id)
                    if row.snapshot_id
                    == task.sales_order_item_bom_component_id
                ),
                None,
            )
            if component_demand is None:
                raise ProductionWorkflowError("订单组件需求快照不存在", 409)
            coverage_target_quantity = int(
                component_demand.required_piece_quantity
            )
            if command.completion_type == "primary":
                current_finished_coverage = min(
                    max(
                        component_available_quantity(
                            db,
                            task.sales_order_item_bom_component_id,
                        ),
                        0,
                    ),
                    coverage_target_quantity,
                )
                if current_finished_coverage != int(
                    task.finished_coverage_snapshot or 0
                ):
                    raise ProductionWorkflowError(
                        "组件成品库存抵扣已变化，请刷新生产任务后重试",
                        409,
                    )
        prior_order_coverage = int(task.finished_coverage_snapshot or 0) + int(
            db.scalar(
                select(
                    func.coalesce(
                        func.sum(ProductionCompletion.order_reserved_quantity),
                        0,
                    )
                ).where(
                    ProductionCompletion.task_id == task.id,
                    ProductionCompletion.status == "posted",
                )
            )
            or 0
        )
        order_coverage = min(
            actual_output,
            max(coverage_target_quantity - prior_order_coverage, 0),
        )
        if command.disposition == "stock":
            direct_quantity = 0
            stock_quantity = actual_output
            stored_disposition = "stock"
            resolved_pallet_id = command.pallet_id
            allowed_existing_pallet_id = None
            if (
                is_component_task
                and (item.composite_fulfillment_mode_snapshot or "component_delivery")
                == "parent_delivery"
            ):
                existing_location_id, existing_pallet_id = (
                    _parent_delivery_storage_target(db, order_item_id=item.id)
                )
                if (
                    existing_location_id is not None
                    and existing_location_id != command.location_id
                ):
                    raise ProductionWorkflowError(
                        "父件交付的同一套产品必须存放在同一库位；请选择已存子件所在位置",
                        409,
                    )
                if existing_pallet_id is not None:
                    if (
                        resolved_pallet_id is not None
                        and int(resolved_pallet_id) != existing_pallet_id
                    ):
                        raise ProductionWorkflowError(
                            "父件交付的同一套产品必须归入同一栈板；请选择已存子件所在栈板",
                            409,
                        )
                    resolved_pallet_id = existing_pallet_id
                    allowed_existing_pallet_id = existing_pallet_id
            location = _production_stock_location(
                db,
                command.location_id,
                pallet_id=resolved_pallet_id,
                allowed_existing_pallet_id=allowed_existing_pallet_id,
            )
        else:
            if (
                is_component_task
                and (item.composite_fulfillment_mode_snapshot or "component_delivery")
                == "parent_delivery"
            ):
                raise ProductionWorkflowError(
                    "父件交付必须先让全部子件在同一位置齐套入库，不能把单个子件直接转入待送区",
                    409,
                )
            if command.location_id is not None:
                raise ProductionWorkflowError(
                    "直接待送整批自动进入一楼待送区，请刷新页面后重试",
                    409,
                )
            direct_quantity = actual_output
            stock_quantity = 0
            stored_disposition = "direct"
            location = _production_direct_staging_location(db)
        prepared[task.id] = {
            "received": received_now,
            "allowed_input": allowed_input_now,
            "factor": factor,
            "pieces_per_box": pieces_per_box,
            "input": material_input,
            "planned": planned_output,
            "actual": actual_output,
            "defective": defective_quantity,
            "order_coverage": order_coverage,
            "direct": direct_quantity,
            "stock": stock_quantity,
            "stored_disposition": stored_disposition,
            "location_id": location.id,
            "expected_status": expected_task_status,
        }

    now = utc_now_naive()
    batch = ProductionCompletionBatch(
        idempotency_key=key,
        request_hash=request_hash,
        item_count=len(commands),
        completed_by=operator_id,
        completed_at=now,
    )
    try:
        with db.begin_nested():
            db.add(batch)
            db.flush()
    except IntegrityError:
        concurrent_batch = db.scalar(
            select(ProductionCompletionBatch).where(
                ProductionCompletionBatch.idempotency_key == key
            )
        )
        if concurrent_batch is None:
            raise
        return _replay_completion_batch(
            db, batch=concurrent_batch, request_hash=request_hash
        )

    completions: list[ProductionCompletion] = []
    affected_order_ids: set[int] = set()
    for command in sorted(commands, key=lambda row: row.task_id):
        task, item, order = by_task[command.task_id]
        facts = prepared[task.id]
        actual_output = int(facts["actual"])
        completion = ProductionCompletion(
            batch_id=batch.id,
            task_id=task.id,
            order_item_id=item.id,
            expected_version=command.expected_version,
            quantity=actual_output,
            completion_type=command.completion_type,
            material_input_quantity=int(facts["input"]),
            planned_output_quantity=int(facts["planned"]),
            actual_output_quantity=actual_output,
            defective_quantity=int(facts["defective"]),
            order_reserved_quantity=int(facts["order_coverage"]),
            direct_delivery_quantity=int(facts["direct"]),
            stock_quantity=int(facts["stock"]),
            surplus_finished_quantity=actual_output - int(facts["order_coverage"]),
            initial_disposition=str(facts["stored_disposition"]),
            warehouse_location_id=(
                int(facts["location_id"])
            ),
            inventory_lot_id=None,
            remarks=_normalized_text(command.remarks),
            completed_by=operator_id,
            completed_at=now,
        )
        db.add(completion)
        db.flush()
        if str(facts["stored_disposition"]) in {"direct", "stock"}:
            is_direct_staging = str(facts["stored_disposition"]) == "direct"
            lot = _stock_completion_lot(
                db,
                completion=completion,
                task=task,
                order=order,
                item=item,
                command=command,
                operator_id=operator_id,
                idempotency_prefix=_stable_key("production-completion", completion.id),
                location_id_override=(
                    int(facts["location_id"]) if is_direct_staging else None
                ),
                source_type=(
                    "production_completion" if is_direct_staging else "production_surplus"
                ),
                movement_reason=(
                    "生产完工整批进入一楼待送区"
                    if is_direct_staging
                    else "生产完工入库"
                ),
            )
            completion.inventory_lot_id = lot.id
            if is_direct_staging:
                _bind_direct_completion_lots_to_system_pallet(
                    db,
                    completion=completion,
                    order=order,
                    lots=[lot],
                    location=location,
                    operator_id=operator_id,
                )
        _consume_completion_semi_reservations(
            db,
            completion=completion,
            task=task,
            item=item,
            # Consume the paper that was actually put into production.  Using
            # only qualified output would leave defective sheets falsely
            # available in inventory.
            planned_quantity=int(facts["planned"]),
            operator_id=operator_id,
        )
        result = db.execute(
            update(ProductionTask)
            .where(
                ProductionTask.id == task.id,
                ProductionTask.version == command.expected_version,
                ProductionTask.status == str(facts["expected_status"]),
            )
            .values(
                status=COMPLETED,
                version=ProductionTask.version + 1,
                ordered_quantity_snapshot=int(item.quantity or 0),
                material_received_quantity=int(facts["received"]),
                material_input_quantity=int(facts["allowed_input"]),
                output_factor=int(facts["factor"]),
                planned_quantity=production_output_quantity(
                    int(facts["allowed_input"]),
                    int(facts["factor"]),
                    int(facts["pieces_per_box"]),
                ),
            )
        )
        if result.rowcount != 1:
            raise ProductionWorkflowError("生产任务版本已变化，请刷新后重试", 409)
        affected_order_ids.add(order.id)
        completions.append(completion)
    db.flush()
    for order_id in sorted(affected_order_ids):
        refresh_order_production_status(db, order_id)
    return CompletionBatchResult(batch, tuple(completions), False)


def transfer_direct_completion_to_stock(
    db: Session,
    *,
    completion_id: int,
    command: StockTransferCommand,
    operator_id: int | None,
) -> StockTransferResult:
    key = command.idempotency_key.strip()
    if not key or len(key) > 120:
        raise ProductionWorkflowError("幂等键长度必须为1到120个字符")
    request_hash = stock_transfer_request_hash(completion_id, command)
    repeated = db.scalar(
        select(ProductionStockTransfer).where(
            ProductionStockTransfer.idempotency_key == key
        )
    )
    if repeated is not None:
        if repeated.completion_id != completion_id or repeated.request_hash != request_hash:
            raise ProductionWorkflowError("同一幂等键对应的转库存内容不一致", 409)
        return StockTransferResult(repeated, True)

    if command.location_id is not None:
        _claim_production_destination(
            db,
            int(command.location_id),
            expected_layout_version=command.expected_layout_version,
        )

    completion = db.get(ProductionCompletion, completion_id)
    if completion is None:
        raise ProductionWorkflowError("生产完工记录不存在", 404)
    if completion.status != "posted":
        raise ProductionWorkflowError("生产完工记录已经撤销，不能再转入库存", 409)
    if completion.initial_disposition != "direct":
        raise ProductionWorkflowError("只有直接送货完工记录可以转库存", 409)
    existing_transfer = db.scalar(
        select(ProductionStockTransfer).where(
            ProductionStockTransfer.completion_id == completion.id,
            ProductionStockTransfer.status == "posted",
        )
    )
    if existing_transfer is not None:
        raise ProductionWorkflowError("该完工记录已转入库存，不能重复操作", 409)
    task = db.get(ProductionTask, completion.task_id)
    if task is None:
        raise ProductionWorkflowError("完工记录关联生产任务不存在", 409)
    if task.sales_order_item_bom_component_id is not None:
        consumed_direct_quantity = int(
            db.scalar(
                select(
                    func.coalesce(
                        func.sum(
                            BomComponentDirectDeliveryAllocation.consumed_quantity
                            - BomComponentDirectDeliveryAllocation.reversed_quantity
                        ),
                        0,
                    )
                ).where(
                    BomComponentDirectDeliveryAllocation.production_completion_id
                    == completion.id,
                    BomComponentDirectDeliveryAllocation.status.in_(("active", "partial")),
                )
            )
            or 0
        )
        if consumed_direct_quantity > 0:
            raise ProductionWorkflowError(
                "该组件完工数量已用于组套送货，不能再转入库存",
                409,
            )
    item = db.get(OrderItem, completion.order_item_id)
    if item is None:
        raise ProductionWorkflowError("完工记录关联订单明细不存在", 409)
    order = lock_order_rows_for_production_transition(db, [item.order_id])[item.order_id]
    claimed = db.execute(
        update(OrderItem)
        .where(
            OrderItem.id == item.id,
            OrderItem.delivered_quantity == 0,
        )
        .values(delivered_quantity=OrderItem.delivered_quantity)
        .execution_options(synchronize_session=False)
    )
    db.flush()
    db.expire(item)
    item = db.get(OrderItem, completion.order_item_id)
    if item is None:
        raise ProductionWorkflowError("完工记录关联订单明细不存在", 409)
    if order.status not in MUTABLE_ORDER_STATUSES or item.is_force_closed:
        raise ProductionWorkflowError(
            "订单或明细已结案，不能再把直接送货完工转入库存", 409
        )
    if has_dispatched_delivery_facts(db, [item.id]):
        raise ProductionWorkflowError("已经发生真实发货，不能整批转库存", 409)
    if claimed.rowcount != 1 or int(item.delivered_quantity or 0) != 0:
        raise ProductionWorkflowError("订单明细已产生送货数量，不能转入库存", 409)
    existing_transfer = db.scalar(
        select(ProductionStockTransfer).where(
            ProductionStockTransfer.completion_id == completion.id,
            ProductionStockTransfer.status == "posted",
        )
    )
    if existing_transfer is not None:
        raise ProductionWorkflowError("该完工记录已转入库存，不能重复操作", 409)
    target_location = _production_stock_location(
        db, command.location_id, pallet_id=command.pallet_id
    )
    if completion.inventory_lot_id is not None:
        lot = db.get(InventoryLot, completion.inventory_lot_id)
        if (
            lot is None
            or lot.status != "active"
            or lot.source_type != "production_completion"
            or lot.source_ref_type != "production_completion"
            or int(lot.source_ref_id or 0) != completion.id
        ):
            raise ProductionWorkflowError("一楼待送区成品批次已失效，不能转库存", 409)
        direct_pallet_item = lot.pallet_item
        direct_pallet = (
            direct_pallet_item.pallet if direct_pallet_item is not None else None
        )
        expected_occupancy_key = _direct_dispatch_pallet_occupancy_key(
            completion.id
        )
        if direct_pallet_item is not None and (
            direct_pallet is None
            or not direct_pallet.is_current
            or direct_pallet.location_id != lot.warehouse_location_id
            or direct_pallet.location_occupancy_key != expected_occupancy_key
        ):
            raise ProductionWorkflowError("一楼待送系统栈板状态异常，不能转库存", 409)
        if lot.warehouse_location_id == target_location.id:
            raise ProductionWorkflowError("目标库位与当前库位相同", 409)
        before = _balances(lot)
        expected_lot_version = int(lot.version)
        updated = db.execute(
            update(InventoryLot)
            .where(
                InventoryLot.id == lot.id,
                InventoryLot.version == expected_lot_version,
            )
            .values(
                warehouse_location_id=target_location.id,
                version=InventoryLot.version + 1,
                last_movement_at=utc_now_naive(),
            )
        )
        if updated.rowcount != 1:
            raise ProductionWorkflowError("待送成品已被其他操作修改，请刷新后重试", 409)
        db.expire(lot)
        lot = db.get(InventoryLot, completion.inventory_lot_id)
        assert lot is not None
        if direct_pallet_item is not None and direct_pallet is not None:
            from app.services.floor3_locations import clear_pallet

            db.delete(direct_pallet_item)
            db.flush()
            clear_pallet(
                db,
                pallet_id=direct_pallet.id,
                expected_version=int(direct_pallet.version),
                remarks="一楼直接待送完工整批转入正式库存位",
                operator_id=operator_id,
                idempotency_key=_stable_key(
                    "production-transfer", completion.id, key, "clear-direct-pallet"
                ),
            )
            from app.services.warehouse_ground_slots import (
                release_ground_occupancy_for_pallet,
            )

            release_ground_occupancy_for_pallet(
                db,
                pallet_id=int(direct_pallet.id),
                operator_id=operator_id,
            )
        if target_location.source_version == "V11":
            from app.services.floor3_locations import bind_finished_lot_to_floor3_pallet

            bind_finished_lot_to_floor3_pallet(
                db,
                lot=lot,
                operator_id=operator_id,
                pallet_id=command.pallet_id,
                pallet_code=_normalized_text(command.pallet_code),
                require_empty_pallet=True,
            )
        _movement(
            db,
            lot=lot,
            movement_type="adjust",
            quantity=0,
            before=before,
            operator_id=operator_id,
            reason="一楼待送区整批转入正式库位",
            remarks=_normalized_text(command.remarks),
            idempotency_key=_stable_key("production-transfer", completion.id, key, "move"),
            related_order_id=order.id,
            related_order_item_id=item.id,
        )
        completion.warehouse_location_id = target_location.id
    else:
        lot = _stock_completion_lot(
            db,
            completion=completion,
            task=task,
            order=order,
            item=item,
            command=command,
            operator_id=operator_id,
            idempotency_prefix=_stable_key("production-transfer", completion.id, key),
        )
    transfer = ProductionStockTransfer(
        completion_id=completion.id,
        warehouse_location_id=command.location_id,
        inventory_lot_id=lot.id,
        idempotency_key=key,
        request_hash=request_hash,
        transferred_by=operator_id,
        transferred_at=utc_now_naive(),
    )
    db.add(transfer)
    db.flush()
    return StockTransferResult(transfer, False)


def _reverse_completion_semi_consumption(
    db: Session,
    *,
    completion: ProductionCompletion,
    operator_id: int | None,
) -> tuple[int, ...]:
    from app.services.semi_finished_inventory import reverse_semi_finished_consumption

    prefix = _stable_key("production-completion", completion.id, "semi", "")
    movements = db.scalars(
        select(InventoryMovement)
        .where(
            InventoryMovement.movement_type == "consume",
            InventoryMovement.related_order_item_id == completion.order_item_id,
            InventoryMovement.idempotency_key.like(f"{prefix}%"),
        )
        .order_by(InventoryMovement.id.desc())
    ).all()
    reversed_ids: list[int] = []
    for movement in movements:
        if movement.reservation_id is None:
            raise ProductionWorkflowError(
                "生产完工半成品消耗缺少预占关联，不能自动回退", 409
            )
        reservation = db.get(InventoryReservation, movement.reservation_id)
        if reservation is None:
            raise ProductionWorkflowError("生产完工半成品预占不存在，不能自动回退", 409)
        if int(reservation.consumed_stock_quantity or 0) != int(movement.quantity or 0):
            raise ProductionWorkflowError(
                "该半成品预占在生产完工后又发生了其他消耗，不能自动回退", 409
            )
        lot = db.get(InventoryLot, reservation.inventory_lot_id)
        if lot is None:
            raise ProductionWorkflowError("生产完工消耗的半成品批次不存在", 409)
        mutation = reverse_semi_finished_consumption(
            db,
            reservation_id=reservation.id,
            stock_quantity=int(movement.quantity),
            expected_version=int(lot.version),
            operator_id=operator_id,
            idempotency_key=_stable_key(
                "production-completion-reversal", completion.id, "semi", reservation.id
            ),
        )
        reversed_ids.append(mutation.movement.id)
    return tuple(reversed_ids)


def _reverse_completion_finished_lot(
    db: Session,
    *,
    completion: ProductionCompletion,
    lot_id: int,
    operator_id: int | None,
    reason: str,
) -> None:
    lot = db.get(InventoryLot, lot_id)
    if lot is None:
        raise ProductionWorkflowError("生产完工成品库存批次不存在", 409)
    if (
        lot.source_ref_type != "production_completion"
        or int(lot.source_ref_id or 0) != completion.id
        or lot.inventory_type != "finished"
        or lot.status != "active"
    ):
        raise ProductionWorkflowError("关联批次已不是有效的生产完工入库，不能回退", 409)
    stock_quantity = int(completion.stock_quantity or completion.quantity or 0)
    if (
        any(
            int(value or 0) > 0
            for value in (
                lot.quantity_consumed,
                lot.quantity_damaged,
                lot.quantity_scrapped,
            )
        )
        or int(lot.quantity_available or 0) + int(lot.quantity_reserved or 0)
        != stock_quantity
    ):
        raise ProductionWorkflowError(
            "成品库存已被使用、调整、报损或数量发生变化，不能回退生产确认", 409
        )
    movements = db.scalars(
        select(InventoryMovement)
        .where(InventoryMovement.inventory_lot_id == lot.id)
        .order_by(InventoryMovement.id)
    ).all()
    if not movements or any(row.movement_type not in {"manual_in", "reserve"} for row in movements):
        raise ProductionWorkflowError("成品库存已经发生后续业务流水，不能回退生产确认", 409)
    reservations = db.scalars(
        select(InventoryReservation).where(
            InventoryReservation.inventory_lot_id == lot.id,
            InventoryReservation.reservation_type == "finished_order",
        )
    ).all()
    if len(reservations) > 1:
        raise ProductionWorkflowError("生产完工成品预占记录异常，不能自动回退", 409)
    reservation = reservations[0] if reservations else None
    if reservation is not None and (
        int(reservation.consumed_stock_quantity or 0) != 0
        or int(reservation.released_stock_quantity or 0) != 0
        or int(reservation.reserved_stock_quantity or 0)
        != int(lot.quantity_reserved or 0)
    ):
        raise ProductionWorkflowError("生产完工成品预占已发生后续变化，不能自动回退", 409)
    pallet = lot.pallet_item.pallet if lot.pallet_item is not None else None
    if pallet is not None and db.scalar(
        select(InventoryLocationMovement.id)
        .where(
            InventoryLocationMovement.pallet_id == pallet.id,
            InventoryLocationMovement.movement_type == "move",
        )
        .limit(1)
    ) is not None:
        raise ProductionWorkflowError("该成品入库后已经移过库位，不能自动回退", 409)

    if reservation is not None:
        release_finished_reservation(
            db,
            reservation_id=reservation.id,
            operator_id=operator_id,
            release_reason=f"撤销生产完工入库：{reason}",
            idempotency_key=_stable_key(
                "production-completion-reversal", completion.id, "release-finished"
            ),
            allow_downstream=True,
            allow_production_reversal=True,
        )
    db.expire(lot)
    lot = db.get(InventoryLot, lot_id)
    assert lot is not None
    if int(lot.quantity_available or 0) != stock_quantity or int(lot.quantity_reserved or 0) != 0:
        raise ProductionWorkflowError("释放成品预占后的库存数量异常，已终止回退", 409)
    before = _balances(lot)
    now = utc_now_naive()
    updated = db.execute(
        update(InventoryLot)
        .where(
            InventoryLot.id == lot.id,
            InventoryLot.version == lot.version,
            InventoryLot.quantity_available == stock_quantity,
            InventoryLot.quantity_reserved == 0,
        )
        .values(
            quantity_available=0,
            status="closed",
            version=InventoryLot.version + 1,
            last_movement_at=now,
            remarks=(f"{lot.remarks or ''}\n撤销生产完工入库：{reason}").strip(),
        )
    )
    if updated.rowcount != 1:
        raise ProductionWorkflowError("成品库存已被其他操作修改，请刷新后重试", 409)
    db.expire(lot)
    lot = db.get(InventoryLot, lot_id)
    assert lot is not None
    _movement(
        db,
        lot=lot,
        movement_type="adjust",
        quantity=stock_quantity,
        before=before,
        operator_id=operator_id,
        reason=f"撤销生产完工入库：{reason}",
        idempotency_key=_stable_key(
            "production-completion-reversal", completion.id, "close-finished"
        ),
        related_order_item_id=completion.order_item_id,
    )
    if pallet is not None and pallet.is_current:
        from app.services.floor3_locations import clear_pallet

        other_balance = db.scalar(
            select(
                func.coalesce(
                    func.sum(
                        InventoryLot.quantity_available
                        + InventoryLot.quantity_reserved
                        + InventoryLot.quantity_damaged
                    ),
                    0,
                )
            )
            .join(InventoryPalletItem, InventoryPalletItem.inventory_lot_id == InventoryLot.id)
            .where(InventoryPalletItem.pallet_id == pallet.id)
        )
        if int(other_balance or 0) == 0:
            clear_pallet(
                db,
                pallet_id=pallet.id,
                expected_version=int(pallet.version),
                remarks=f"撤销生产完工入库：{reason}",
                operator_id=operator_id,
            )
            from app.services.warehouse_ground_slots import (
                release_ground_occupancy_for_pallet,
            )

            release_ground_occupancy_for_pallet(
                db,
                pallet_id=int(pallet.id),
                operator_id=operator_id,
            )


def reverse_production_completion(
    db: Session,
    *,
    completion_id: int,
    operator_id: int | None,
    reason: str | None,
) -> CompletionReversalResult:
    normalized_reason = (reason or "").strip() or "撤销生产确认（系统记录）"
    completion = db.get(ProductionCompletion, completion_id)
    if completion is None:
        raise ProductionWorkflowError("生产完工记录不存在", 404)
    if completion.status != "posted":
        raise ProductionWorkflowError("该生产完工记录已经撤销，不能重复操作", 409)
    task = db.get(ProductionTask, completion.task_id)
    item = db.get(OrderItem, completion.order_item_id)
    if task is None or item is None:
        raise ProductionWorkflowError("生产完工关联任务或订单明细不存在", 409)
    order = lock_order_rows_for_production_transition(db, [item.order_id])[item.order_id]
    if order.status not in MUTABLE_ORDER_STATUSES or item.is_force_closed:
        raise ProductionWorkflowError("订单已经结档、作废或强制关闭，不能撤销生产确认", 409)
    if int(item.delivered_quantity or 0) > 0 or has_dispatched_delivery_facts(db, [item.id]):
        raise ProductionWorkflowError("订单已经发货，请先撤销发货后再回退生产确认", 409)
    if task.status != COMPLETED:
        raise ProductionWorkflowError("生产任务当前不是已完工状态，不能撤销", 409)
    if completion.completion_type == "primary" and db.scalar(
        select(ProductionCompletion.id)
        .where(
            ProductionCompletion.task_id == task.id,
            ProductionCompletion.completion_type == "supplemental",
            ProductionCompletion.status == "posted",
        )
        .limit(1)
    ) is not None:
        raise ProductionWorkflowError("请先撤销补充生产确认，再撤销主生产确认", 409)
    direct_allocation = db.scalar(
        select(BomComponentDirectDeliveryAllocation.id)
        .where(
            BomComponentDirectDeliveryAllocation.production_completion_id == completion.id,
            BomComponentDirectDeliveryAllocation.status.in_(("active", "partial")),
        )
        .limit(1)
    )
    if direct_allocation is not None:
        raise ProductionWorkflowError("该生产组件已经用于组合送货，不能撤销生产确认", 409)
    transfer = db.scalar(
        select(ProductionStockTransfer).where(
            ProductionStockTransfer.completion_id == completion.id,
            ProductionStockTransfer.status == "posted",
        )
    )
    lot_id = transfer.inventory_lot_id if transfer is not None else completion.inventory_lot_id
    if lot_id is not None:
        _reverse_completion_finished_lot(
            db,
            completion=completion,
            lot_id=lot_id,
            operator_id=operator_id,
            reason=normalized_reason,
        )
    reversed_semi_ids = _reverse_completion_semi_consumption(
        db,
        completion=completion,
        operator_id=operator_id,
    )
    now = utc_now_naive()
    if transfer is not None:
        transfer.status = "reversed"
        transfer.reversed_by = operator_id
        transfer.reversed_at = now
        transfer.reversal_reason = normalized_reason
    completion.status = "reversed"
    completion.reversed_by = operator_id
    completion.reversed_at = now
    completion.reversal_reason = normalized_reason
    remaining_posted = db.scalar(
        select(ProductionCompletion.id)
        .where(
            ProductionCompletion.task_id == task.id,
            ProductionCompletion.status == "posted",
            ProductionCompletion.id != completion.id,
        )
        .limit(1)
    )
    task.status = COMPLETED if remaining_posted is not None else PENDING
    task.version = int(task.version) + 1
    db.flush()
    refresh_order_production_status(db, order.id)
    return CompletionReversalResult(
        completion=completion,
        transfer=transfer,
        inventory_lot_id=lot_id,
        reversed_semi_movement_ids=reversed_semi_ids,
    )


def _ensure_receipt_auto_main_task(
    db: Session,
    *,
    item: OrderItem,
    product: Product,
) -> ProductionTask:
    """Return the single employee-visible task used by automatic receipts.

    Composite component tasks remain internal execution records.  They are not
    reused as the order-level task because doing so would give one sales line
    several employee QR identities.
    """

    task = db.scalar(
        select(ProductionTask).where(
            ProductionTask.order_item_id == item.id,
            ProductionTask.sales_order_item_bom_component_id.is_(None),
        )
    )
    if task is not None:
        if hasattr(task, "task_role"):
            task.task_role = "order_main"
        return task
    order_quantity = int(item.quantity or 0)
    if order_quantity <= 0:
        raise ProductionWorkflowError("订单明细数量必须大于0，不能自动形成成品", 409)
    task = ProductionTask(
        order_item_id=item.id,
        sales_order_item_bom_component_id=None,
        task_role="order_main",
        status=WAITING_MATERIAL,
        planned_quantity=0,
        finished_coverage_snapshot=0,
        ordered_quantity_snapshot=order_quantity,
        material_received_quantity=0,
        material_input_quantity=0,
        output_factor=cutting_output_factor(item.special_process),
        readiness_basis=None,
        ready_at=None,
        version=1,
        **_new_task_printing_snapshot(db, product),
        **new_task_profile_snapshot(db, product, item=item),
        **_new_task_label_snapshot(product, total_quantity=order_quantity),
    )
    db.add(task)
    db.flush()
    return task


def ensure_receipt_auto_main_task(
    db: Session,
    *,
    order_item_id: int,
) -> ProductionTask:
    """Ensure the one employee-visible task exists even before finished output.

    A3/BOM receipts can have a zero finished increment until all required
    components arrive.  The persistent order-level task must nevertheless exist
    from the first frozen-purpose receipt; component tasks remain internal.
    """

    item = db.get(OrderItem, int(order_item_id))
    if item is None:
        raise ProductionWorkflowError("自动收料关联订单明细不存在", 409)
    product = db.get(Product, item.product_id)
    if product is None or not product.is_active:
        raise ProductionWorkflowError("订单常用箱不存在或已停用，不能建立生产任务", 409)
    return _ensure_receipt_auto_main_task(db, item=item, product=product)


def post_automatic_receipt_completion(
    db: Session,
    *,
    order_item_id: int,
    previous_theoretical_quantity: int,
    new_theoretical_quantity: int,
    material_input_delta: int,
    material_input_cumulative: int,
    operator_id: int | None,
    idempotency_key: str,
    capitalized_material_cost: Decimal,
    cost_detail: dict[str, object],
) -> ProductionCompletion | None:
    """Post one receipt-derived finished increment through the existing ledger.

    The caller owns the surrounding receipt transaction and has already
    validated the immutable purpose, material and price facts.  This function
    never commits.
    """

    before = max(int(previous_theoretical_quantity or 0), 0)
    after = max(int(new_theoretical_quantity or 0), 0)
    if after < before:
        raise ProductionWorkflowError("自动完工累计数量不能倒退", 409)
    delta = after - before
    if delta == 0:
        return None
    if material_input_delta <= 0 or material_input_cumulative <= 0:
        raise ProductionWorkflowError("自动完工缺少有效的订单用途来料张数", 409)
    key = str(idempotency_key or "").strip()
    if not key or len(key) > 120:
        raise ProductionWorkflowError("自动完工幂等键无效", 409)

    item = db.get(OrderItem, order_item_id)
    if item is None:
        raise ProductionWorkflowError("自动完工关联订单明细不存在", 409)
    order = lock_order_rows_for_production_transition(db, [item.order_id])[item.order_id]
    if order.status not in MUTABLE_ORDER_STATUSES or item.is_force_closed:
        raise ProductionWorkflowError("订单已结档、作废或强制关闭，不能自动形成成品", 409)
    product = db.get(Product, item.product_id)
    if product is None or not product.is_active:
        raise ProductionWorkflowError("订单常用箱不存在或已停用，不能自动形成成品", 409)
    task = _ensure_receipt_auto_main_task(db, item=item, product=product)
    payload = {
        "order_item_id": item.id,
        "task_id": task.id,
        "before": before,
        "after": after,
        "material_input_delta": int(material_input_delta),
        "material_input_cumulative": int(material_input_cumulative),
        "capitalized_material_cost": str(capitalized_material_cost),
        "cost_detail": cost_detail,
    }
    request_hash = _canonical_hash(payload)
    existing_batch = db.scalar(
        select(ProductionCompletionBatch).where(
            ProductionCompletionBatch.idempotency_key == key
        )
    )
    if existing_batch is not None:
        replay = _replay_completion_batch(
            db,
            batch=existing_batch,
            request_hash=request_hash,
        )
        if len(replay.completions) != 1:
            raise ProductionWorkflowError("自动完工幂等事实不完整", 409)
        return replay.completions[0]

    ground_target = _receipt_auto_finished_ground_target(db, claim=True)
    location = ground_target.location
    existing_posted = db.scalar(
        select(ProductionCompletion.id)
        .where(
            ProductionCompletion.task_id == task.id,
            ProductionCompletion.status == "posted",
        )
        .limit(1)
    )
    completion_type: CompletionType = (
        "primary" if existing_posted is None else "supplemental"
    )
    now = utc_now_naive()
    batch = ProductionCompletionBatch(
        idempotency_key=key,
        request_hash=request_hash,
        item_count=1,
        completed_by=operator_id,
        completed_at=now,
    )
    db.add(batch)
    db.flush()
    order_reserved = min(delta, max(int(item.quantity or 0) - before, 0))
    completion = ProductionCompletion(
        batch_id=batch.id,
        task_id=task.id,
        order_item_id=item.id,
        expected_version=max(int(task.version or 1), 1),
        quantity=delta,
        completion_type=completion_type,
        origin="receipt_auto",
        material_input_quantity=int(material_input_delta),
        planned_output_quantity=delta,
        actual_output_quantity=delta,
        defective_quantity=0,
        order_reserved_quantity=order_reserved,
        direct_delivery_quantity=delta,
        stock_quantity=0,
        surplus_finished_quantity=delta - order_reserved,
        initial_disposition="direct",
        warehouse_location_id=location.id,
        inventory_lot_id=None,
        remarks="收料后按冻结订单用途自动形成理论成品",
        completed_by=operator_id,
        completed_at=now,
    )
    db.add(completion)
    db.flush()
    command = CompletionCommand(
        task_id=task.id,
        expected_version=max(int(task.version or 1), 1),
        disposition="direct",
        completion_type=completion_type,
        material_input_quantity=int(material_input_delta),
        actual_output_quantity=delta,
        direct_delivery_quantity=delta,
        remarks="收料自动成品进入合并一楼成品暂存区",
    )
    lot = _stock_completion_lot(
        db,
        completion=completion,
        task=task,
        order=order,
        item=item,
        command=command,
        operator_id=operator_id,
        idempotency_prefix=_stable_key("incoming-auto", key),
        location_id_override=location.id,
        receipt_ground_target=ground_target,
        source_type="production_completion",
        movement_reason="订单用途来料自动形成成品并进入真实一楼成品待送位置",
    )
    completion.inventory_lot_id = lot.id
    _bind_direct_completion_lots_to_system_pallet(
        db,
        completion=completion,
        order=order,
        lots=[lot],
        location=location,
        operator_id=operator_id,
        ground_target=ground_target,
    )
    capitalized = Decimal(str(capitalized_material_cost or 0)).quantize(
        Decimal("0.0001"), rounding=ROUND_HALF_UP
    )
    if capitalized < 0:
        raise ProductionWorkflowError("自动成品成本不能小于0", 409)
    lot.estimated_unit_cost_snapshot = (
        capitalized / Decimal(delta)
    ).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    lot.estimated_square_price_snapshot = None
    lot.estimated_cost_area_m2_snapshot = None
    lot.cost_snapshot_source = "purchase_receipt_actual"
    lot.cost_snapshot_detail_json = json.dumps(
        {
            **cost_detail,
            "capitalized_material_cost": str(capitalized),
            "finished_quantity": delta,
            "formula": "cumulative uncapitalized order-purpose cost / finished increment",
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    lot.cost_snapshot_at = now

    task.status = COMPLETED if after >= int(item.quantity or 0) else PENDING
    task.planned_quantity = max(int(item.quantity or 0), after, 1)
    task.finished_coverage_snapshot = min(after, int(item.quantity or 0))
    task.ordered_quantity_snapshot = int(item.quantity or 0)
    task.material_received_quantity = int(material_input_cumulative)
    task.material_input_quantity = int(material_input_cumulative)
    task.readiness_basis = "automatic_receipt"
    task.ready_at = task.ready_at or now
    task.version = max(int(task.version or 1), 1) + 1
    db.flush()
    refresh_order_production_status(db, order.id)
    return completion


def reverse_automatic_receipt_completion(
    db: Session,
    *,
    completion_id: int,
    remaining_theoretical_quantity: int,
    remaining_material_input_quantity: int,
    operator_id: int | None,
    reason: str | None,
) -> CompletionReversalResult:
    """Reverse the latest receipt-derived completion inside receipt rollback."""

    completion = db.get(ProductionCompletion, completion_id)
    if completion is None or completion.status != "posted":
        raise ProductionWorkflowError("关联的自动完工事实不存在或已撤销", 409)
    if getattr(completion, "origin", "manual") != "receipt_auto":
        raise ProductionWorkflowError("关联完工不是收料自动事实，不能由收料撤销", 409)
    task = db.get(ProductionTask, completion.task_id)
    item = db.get(OrderItem, completion.order_item_id)
    if task is None or item is None:
        raise ProductionWorkflowError("自动完工关联任务或订单明细不存在", 409)
    order = lock_order_rows_for_production_transition(db, [item.order_id])[item.order_id]
    if order.status not in MUTABLE_ORDER_STATUSES or item.is_force_closed:
        raise ProductionWorkflowError("订单已结档、作废或强制关闭，不能撤销收料自动完工", 409)
    if int(item.delivered_quantity or 0) > 0 or has_dispatched_delivery_facts(db, [item.id]):
        raise ProductionWorkflowError("订单已经发货，请先撤销发货后再撤销来料", 409)
    later = db.scalar(
        select(ProductionCompletion.id)
        .where(
            ProductionCompletion.task_id == task.id,
            ProductionCompletion.status == "posted",
            ProductionCompletion.origin == "receipt_auto",
            ProductionCompletion.id > completion.id,
        )
        .limit(1)
    )
    if later is not None:
        raise ProductionWorkflowError("存在更晚的自动完工，请先撤销最新一笔来料", 409)
    lot_id = completion.inventory_lot_id
    if lot_id is not None:
        _reverse_completion_finished_lot(
            db,
            completion=completion,
            lot_id=lot_id,
            operator_id=operator_id,
            reason=(reason or "").strip() or "撤销来料自动完工",
        )
    now = utc_now_naive()
    completion.status = "reversed"
    completion.reversed_by = operator_id
    completion.reversed_at = now
    completion.reversal_reason = (reason or "").strip() or "撤销来料自动完工"
    remaining = max(int(remaining_theoretical_quantity or 0), 0)
    if remaining <= 0:
        task.status = WAITING_MATERIAL
        task.planned_quantity = 0
        task.ready_at = None
        task.readiness_basis = None
    else:
        task.status = COMPLETED if remaining >= int(item.quantity or 0) else PENDING
        task.planned_quantity = max(int(item.quantity or 0), remaining, 1)
        task.ready_at = task.ready_at or now
        task.readiness_basis = "automatic_receipt"
    task.finished_coverage_snapshot = min(remaining, int(item.quantity or 0))
    task.material_received_quantity = max(int(remaining_material_input_quantity or 0), 0)
    task.material_input_quantity = max(int(remaining_material_input_quantity or 0), 0)
    task.version = max(int(task.version or 1), 1) + 1
    db.flush()
    refresh_order_production_status(db, order.id)
    return CompletionReversalResult(
        completion=completion,
        transfer=None,
        inventory_lot_id=lot_id,
        reversed_semi_movement_ids=(),
    )


def _task_query(db: Session, allowed_customer_ids: set[int] | None):
    query = (
        select(ProductionTask, OrderItem, Order, Customer, Product)
        .join(OrderItem, OrderItem.id == ProductionTask.order_item_id)
        .join(Order, Order.id == OrderItem.order_id)
        .join(Customer, Customer.id == Order.customer_id)
        .join(Product, Product.id == OrderItem.product_id)
    )
    if allowed_customer_ids is not None:
        query = query.where(Order.customer_id.in_(allowed_customer_ids))
    return query


def _filtered_task_query(
    db: Session,
    *,
    allowed_customer_ids: set[int] | None,
    status: str | None,
):
    main_task = aliased(ProductionTask)
    main_task_exists = exists().where(
        main_task.order_item_id == ProductionTask.order_item_id,
        main_task.task_role == "order_main",
    )
    query = _task_query(db, allowed_customer_ids).where(
        Order.status.in_(MUTABLE_ORDER_STATUSES),
        OrderItem.is_force_closed.is_(False),
        or_(
            ProductionTask.task_role == "order_main",
            ~main_task_exists,
        ),
    )
    if status:
        query = query.where(ProductionTask.status == status)
    return query


def list_production_task_dashboard_rows(
    db: Session,
    *,
    allowed_customer_ids: set[int] | None,
    status: str = PENDING,
) -> list[dict]:
    """Return the exact pending-task identities needed by the dashboard.

    The production page deliberately keeps using :func:`list_production_tasks`.
    This read-only projection shares that function's customer, mutable-order,
    force-close and task-status filters, but avoids every BOM, material,
    printing, inventory and location serializer used by the full page payload.

    ``delivery_date`` and ``created_at`` remain ``None`` because the legacy full
    task payload does not expose either field.  Keeping those values unchanged
    preserves the dashboard todo ordering and message contract while the SQL
    ordering continues to use the order delivery date exactly as before.
    """

    component_code = SalesOrderItemBomComponent.snapshot_component_product_code
    parent_code = func.coalesce(
        func.nullif(OrderItem.snapshot_product_code, ""),
        Product.product_code,
    )
    product_code = case(
        (
            ProductionTask.sales_order_item_bom_component_id.is_not(None),
            component_code,
        ),
        else_=parent_code,
    ).label("product_code")
    query = (
        _filtered_task_query(
            db,
            allowed_customer_ids=allowed_customer_ids,
            status=status,
        )
        .outerjoin(
            SalesOrderItemBomComponent,
            SalesOrderItemBomComponent.id
            == ProductionTask.sales_order_item_bom_component_id,
        )
        .with_only_columns(
            ProductionTask.id.label("id"),
            Order.customer_id.label("customer_id"),
            Customer.name.label("customer_name"),
            Order.order_number.label("order_number"),
            product_code,
        )
        .order_by(Order.delivery_date, Order.id, OrderItem.id, ProductionTask.id)
    )
    return [
        {
            "id": int(row.id),
            "customer_id": int(row.customer_id),
            "customer_name": row.customer_name,
            "order_number": row.order_number,
            "product_code": row.product_code,
            "delivery_date": None,
            "created_at": None,
        }
        for row in db.execute(query).mappings().all()
    ]


def _item_product_snapshot(item: OrderItem, product: Product) -> dict:
    is_die_cut = product.box_category == "die_cut"
    mold = product.mold_tool if is_die_cut else None
    return {
        "product_id": item.product_id,
        "product_code": item.snapshot_product_code or product.product_code,
        "product_name": item.snapshot_product_name or product.product_name,
        "specification": resolved_product_specification(item.snapshot_spec, product),
        "material": item.snapshot_material,
        "flute": item.flute_type,
        "special_process": item.special_process,
        "production_process": product.production_process,
        "production_notes": item.snapshot_production_notes,
        "mold_name": mold.mold_name if mold is not None else None,
        "mold_location": mold.rack_location if mold is not None else None,
    }


def _task_product_snapshot(
    db: Session,
    *,
    task: ProductionTask,
    item: OrderItem,
    parent_product: Product,
) -> dict:
    """Expose the actual component being produced without changing parent sets."""
    snapshot_id = task.sales_order_item_bom_component_id
    if snapshot_id is None:
        profile = resolved_task_profile(
            db,
            task=task,
            item=item,
            product=parent_product,
            component=None,
        )
        frozen_mold = (
            db.get(MoldTool, int(profile["mold_tool_id"]))
            if profile.get("mold_tool_id") is not None
            else None
        )
        return {
            **_item_product_snapshot(item, parent_product),
            **_task_printing_snapshot(task),
            "box_style": profile.get("box_style"),
            "needs_die_cut": bool(profile.get("needs_die_cut")),
            "special_process": profile.get("cutting_mode"),
            "production_process": profile.get("production_process"),
            "production_notes": profile.get("production_notes"),
            "drawing_reference": profile.get("drawing_reference"),
            "mold_tool_id": profile.get("mold_tool_id"),
            "mold_code": profile.get("mold_tool_code"),
            "mold_name": profile.get("mold_tool_name"),
            "mold_location": (
                frozen_mold.rack_location if frozen_mold is not None else None
            ),
            "production_profile_schema_version": profile.get("schema_version"),
            "production_profile_source_version": profile.get(
                "source_product_version"
            ),
            "current_product_version": int(parent_product.version),
            "current_product_production_label_enabled": bool(
                parent_product.production_label_enabled
            ),
            "current_product_production_label_units_per_label": (
                parent_product.production_label_units_per_label
            ),
            "is_component_task": False,
            "bom_component_snapshot_id": None,
            "production_quantity_unit": "sets",
        }
    snapshot = db.get(SalesOrderItemBomComponent, snapshot_id)
    component = (
        db.get(Product, snapshot.component_product_id) if snapshot is not None else None
    )
    sibling_snapshots = db.scalars(
        select(SalesOrderItemBomComponent)
        .where(
            SalesOrderItemBomComponent.sales_order_item_id == item.id,
            SalesOrderItemBomComponent.is_required.is_(True),
        )
        .order_by(
            SalesOrderItemBomComponent.display_order,
            SalesOrderItemBomComponent.id,
        )
    ).all()
    profile = resolved_task_profile(
        db,
        task=task,
        item=item,
        product=component or parent_product,
        component=snapshot,
    )
    frozen_mold = (
        db.get(MoldTool, int(profile["mold_tool_id"]))
        if profile.get("mold_tool_id") is not None
        else None
    )
    return {
        "product_id": snapshot.component_product_id if snapshot is not None else None,
        "product_code": (
            snapshot.snapshot_component_product_code
            if snapshot is not None
            else parent_product.product_code
        ),
        "product_name": (
            snapshot.snapshot_component_product_name
            if snapshot is not None
            else parent_product.product_name
        ),
        "specification": resolved_product_specification(
            snapshot.snapshot_component_spec if snapshot is not None else item.snapshot_spec,
            component or parent_product,
        ),
        "material": (
            snapshot.snapshot_component_material
            if snapshot is not None
            else item.snapshot_material
        ),
        "flute": (
            snapshot.snapshot_component_flute_type
            if snapshot is not None
            else item.flute_type
        ),
        "box_style": profile.get("box_style"),
        "needs_die_cut": bool(profile.get("needs_die_cut")),
        "special_process": profile.get("cutting_mode"),
        "production_process": profile.get("production_process"),
        "production_notes": profile.get("production_notes"),
        "drawing_reference": profile.get("drawing_reference"),
        "mold_tool_id": profile.get("mold_tool_id"),
        "mold_code": profile.get("mold_tool_code"),
        "mold_name": profile.get("mold_tool_name"),
        "mold_location": (
            frozen_mold.rack_location if frozen_mold is not None else None
        ),
        **_task_printing_snapshot(task),
        "production_profile_schema_version": profile.get("schema_version"),
        "production_profile_source_version": profile.get("source_product_version"),
        "is_component_task": True,
        "bom_component_snapshot_id": snapshot_id,
        "component_quantity_per_set": (
            int(snapshot.quantity_per_set) if snapshot is not None else None
        ),
        "production_quantity_unit": "pieces",
        "component_product_found": component is not None,
        "composite_parent": {
            "product_code": item.snapshot_product_code,
            "product_name": item.snapshot_product_name,
            "set_quantity": int(item.quantity or 0),
            "fulfillment_mode": (
                item.composite_fulfillment_mode_snapshot
                or "component_delivery"
            ),
        },
        "composite_siblings": [
            {
                "bom_component_snapshot_id": sibling.id,
                "product_code": sibling.snapshot_component_product_code,
                "product_name": sibling.snapshot_component_product_name,
                "required_piece_quantity": int(
                    sibling.required_piece_quantity or 0
                ),
                "quantity_per_set": int(sibling.quantity_per_set or 0),
                "is_current": sibling.id == snapshot_id,
            }
            for sibling in sibling_snapshots
        ],
        "current_product_version": (
            int(component.version) if component is not None else None
        ),
        "current_product_production_label_enabled": (
            bool(component.production_label_enabled)
            if component is not None
            else None
        ),
        "current_product_production_label_units_per_label": (
            component.production_label_units_per_label
            if component is not None
            else None
        ),
    }


def _active_customer_board_preparation_sources(
    db: Session,
    *,
    task: ProductionTask,
    item: OrderItem,
) -> list[dict]:
    query = (
        select(InventoryReservation, InventoryLot)
        .join(
            InventoryLot,
            InventoryLot.id == InventoryReservation.inventory_lot_id,
        )
        .where(
            InventoryReservation.order_item_id == item.id,
            InventoryReservation.reservation_type == "semi_order",
            InventoryReservation.status.notin_(("cancelled", "released", "consumed")),
            InventoryReservation.reserved_stock_quantity
            > InventoryReservation.consumed_stock_quantity
            + InventoryReservation.released_stock_quantity,
            InventoryLot.inventory_type == "semi_finished",
        )
    )
    if task.sales_order_item_bom_component_id is None:
        query = query.where(
            InventoryReservation.sales_order_item_bom_component_id.is_(None)
        )
    else:
        query = query.where(
            InventoryReservation.sales_order_item_bom_component_id
            == task.sales_order_item_bom_component_id
        )
    rows = db.execute(
        query.order_by(
            *inventory_fifo_order_columns(),
            InventoryReservation.id,
        )
    ).all()
    result: list[dict] = []
    for reservation, lot in rows:
        detail = lot.semi_finished_detail
        location = lot.location
        if detail is None or detail.owner_customer_id is None:
            continue
        remaining_sheets = max(
            int(reservation.reserved_stock_quantity or 0)
            - int(reservation.consumed_stock_quantity or 0)
            - int(reservation.released_stock_quantity or 0),
            0,
        )
        remaining_pieces = max(
            int(reservation.credited_requirement_quantity or 0)
            - int(reservation.consumed_requirement_quantity or 0)
            - int(reservation.released_requirement_quantity or 0),
            0,
        )
        if remaining_sheets <= 0 or remaining_pieces <= 0:
            continue
        result.append(
            {
                "reservation_id": reservation.id,
                "inventory_lot_id": lot.id,
                "lot_number": lot.lot_number,
                "source_ref_type": lot.source_ref_type,
                "source_ref_id": lot.source_ref_id,
                "location_code": location.location_code,
                "location_name": employee_location_name(location),
                "current_address_name": employee_location_name(location),
                "employee_location_name": employee_location_name(location),
                "remaining_sheet_quantity": remaining_sheets,
                "remaining_product_quantity": remaining_pieces,
                "stock_yield_per_sheet": int(
                    detail.stock_yield_per_sheet or reservation.yield_factor or 1
                ),
                "display_name": "客户专用纸板备料",
            }
        )
    return result


@dataclass(frozen=True)
class _PendingProductionReadContext:
    """Read-only fast-path eligibility for ordinary pending tasks.

    This context is intentionally conservative.  It only bypasses the legacy
    per-row serializer when batched preflight proves that no fact which the
    detailed production workflow reads exists for the order line.  Components,
    double-splice orders, receipts, completions and every reservation remain on
    the existing exact path.
    """

    fast_order_item_ids: frozenset[int]

    def is_fast_path(
        self,
        *,
        task: ProductionTask,
        item: OrderItem,
        product: Product,
    ) -> bool:
        return (
            task.sales_order_item_bom_component_id is None
            and item.id in self.fast_order_item_ids
            and product.box_category != "die_cut"
            and (item.snapshot_splice_mode or "").strip().lower() != "double"
        )


def _pending_production_read_context(
    db: Session,
    rows: Sequence[tuple[ProductionTask, OrderItem, Order, Customer, Product]],
) -> _PendingProductionReadContext:
    """Batch-prove which rows have no detailed workflow facts to calculate.

    The four preflight reads deliberately use existence only.  A positive
    result is never simplified: it sends the row through the original
    serializer, preserving component demand, receipt variance, completion,
    finished-reservation and customer-board calculations exactly.
    """

    candidate_item_ids = {
        item.id
        for task, item, _order, _customer, product in rows
        if task.sales_order_item_bom_component_id is None
        and product.box_category != "die_cut"
        and (item.snapshot_splice_mode or "").strip().lower() != "double"
    }
    if not candidate_item_ids:
        return _PendingProductionReadContext(frozenset())

    candidate_task_ids = {
        task.id
        for task, item, _order, _customer, product in rows
        if item.id in candidate_item_ids
    }
    component_item_ids = set(
        db.scalars(
            select(SalesOrderItemBomComponent.sales_order_item_id).where(
                SalesOrderItemBomComponent.sales_order_item_id.in_(candidate_item_ids)
            )
        ).all()
    )
    receipt_item_ids = set(
        db.scalars(
            select(IncomingReceiptItem.order_item_id).where(
                IncomingReceiptItem.order_item_id.in_(candidate_item_ids),
                IncomingReceiptItem.status == "posted",
            )
        ).all()
    )
    completion_task_ids = set(
        db.scalars(
            select(ProductionCompletion.task_id).where(
                ProductionCompletion.task_id.in_(candidate_task_ids)
            )
        ).all()
    )
    reservation_item_ids = set(
        db.scalars(
            select(InventoryReservation.order_item_id).where(
                InventoryReservation.order_item_id.in_(candidate_item_ids)
            )
        ).all()
    )
    complex_item_ids = (
        component_item_ids
        | receipt_item_ids
        | reservation_item_ids
        | {
            task.order_item_id
            for task, item, _order, _customer, _product in rows
            if task.id in completion_task_ids and item.id in candidate_item_ids
        }
    )
    return _PendingProductionReadContext(
        frozenset(candidate_item_ids - complex_item_ids)
    )


def _ordinary_pending_task_fast_payload(
    *,
    task: ProductionTask,
    item: OrderItem,
    order: Order,
    customer: Customer,
    product: Product,
) -> dict:
    """Exact serializer result when preflight proves all detailed facts absent."""

    target_quantity = int(item.quantity or 0)
    factor = cutting_output_factor(item.special_process)
    pieces_per_box = production_pieces_per_box(item)
    material_input = max(int(task.material_input_quantity or 0), 0)
    finished_coverage = int(task.finished_coverage_snapshot or 0)
    return {
        "id": task.id,
        "order_item_id": item.id,
        "order_id": order.id,
        "order_number": order.order_number,
        "item_order_number": item.item_order_number,
        "customer_id": order.customer_id,
        "customer_name": customer.name,
        **_item_product_snapshot(item, product),
        **_task_printing_snapshot(task),
        "is_component_task": False,
        "bom_component_snapshot_id": None,
        "production_quantity_unit": "sets",
        "order_quantity": target_quantity,
        "parent_order_quantity": int(item.quantity),
        "component_required_quantity": None,
        "delivered_quantity": int(item.delivered_quantity or 0),
        "material_status": item.material_status,
        "status": task.status,
        "planned_quantity": int(task.planned_quantity),
        "ordered_quantity": target_quantity,
        "material_received_quantity": max(
            0,
            int(task.material_received_quantity or 0),
        ),
        "material_input_quantity": material_input,
        "available_material_input_quantity": material_input,
        "output_factor": factor,
        "pieces_per_box": pieces_per_box,
        "planned_output_quantity": production_output_quantity(
            material_input,
            factor,
            pieces_per_box,
        ),
        "actual_output_quantity": 0,
        "order_reserved_quantity": min(finished_coverage, target_quantity),
        "surplus_finished_quantity": max(finished_coverage - target_quantity, 0),
        "can_supplement": False,
        "finished_coverage_snapshot": finished_coverage,
        "readiness_basis": task.readiness_basis,
        "ready_at": utc_naive_to_api(task.ready_at) if task.ready_at else None,
        "version": int(task.version),
        "production_label_enabled_snapshot": bool(
            task.production_label_enabled_snapshot
        ),
        "production_label_units_per_label_snapshot": (
            task.production_label_units_per_label_snapshot
        ),
        "production_label_total_quantity_snapshot": int(
            task.production_label_total_quantity_snapshot or 0
        ),
        "production_label_count_snapshot": int(
            task.production_label_count_snapshot or 0
        ),
        "production_label_template_version_snapshot": (
            task.production_label_template_version_snapshot
        ),
        "production_label_product_version_snapshot": (
            task.production_label_product_version_snapshot
        ),
        "current_product_version": int(product.version),
        "current_product_production_label_enabled": bool(
            product.production_label_enabled
        ),
        "current_product_production_label_units_per_label": (
            product.production_label_units_per_label
        ),
        "production_ready_quantity": 0,
        "customer_board_preparation_sources": [],
    }


def _receipt_purpose_summaries_by_order_item_ids(
    db: Session,
    order_item_ids: Sequence[int],
) -> dict[int, dict[str, int]]:
    normalized_ids = sorted({int(item_id) for item_id in order_item_ids})
    if not normalized_ids:
        return {}
    rows = db.execute(
        select(
            IncomingReceiptPurposeAllocation,
            SalesOrderItemBomComponent.sales_order_item_id,
        )
        .outerjoin(
            RequisitionItemBomSource,
            RequisitionItemBomSource.id
            == IncomingReceiptPurposeAllocation.source_bom_requisition_source_id,
        )
        .outerjoin(
            SalesOrderItemBomComponent,
            SalesOrderItemBomComponent.id
            == RequisitionItemBomSource.sales_order_item_bom_component_id,
        )
        .where(
            IncomingReceiptPurposeAllocation.status == "posted",
            IncomingReceiptPurposeAllocation.purpose_contract_status_snapshot
            == "frozen",
            ~exists(
                select(IncomingReceiptPurposeReversal.id).where(
                    IncomingReceiptPurposeReversal.incoming_receipt_purpose_allocation_id
                    == IncomingReceiptPurposeAllocation.id
                )
            ),
            or_(
                IncomingReceiptPurposeAllocation.source_order_item_id.in_(
                    normalized_ids
                ),
                SalesOrderItemBomComponent.sales_order_item_id.in_(normalized_ids),
            ),
        )
    ).all()
    result: dict[int, dict[str, int]] = {}
    for allocation, component_order_item_id in rows:
        order_item_id = (
            int(allocation.source_order_item_id)
            if allocation.source_order_item_id is not None
            else int(component_order_item_id)
            if component_order_item_id is not None
            else None
        )
        if order_item_id is None:
            continue
        summary = result.setdefault(
            order_item_id,
            {
                "order_purpose_received_sheet_qty": 0,
                "reserve_purpose_received_sheet_qty": 0,
                "automatic_finished_output_qty": 0,
            },
        )
        summary["order_purpose_received_sheet_qty"] += int(
            allocation.receipt_order_purpose_sheet_qty or 0
        )
        summary["reserve_purpose_received_sheet_qty"] += int(
            allocation.receipt_reserve_purpose_sheet_qty or 0
        )
        summary["automatic_finished_output_qty"] += int(
            allocation.finished_output_qty_delta or 0
        )
    return result


def list_production_tasks(
    db: Session,
    *,
    allowed_customer_ids: set[int] | None,
    status: str | None = None,
    page: int | None = None,
    page_size: int | None = None,
    task_ids: Sequence[int] | None = None,
) -> list[dict]:
    query = _filtered_task_query(
        db,
        allowed_customer_ids=allowed_customer_ids,
        status=status,
    ).order_by(Order.delivery_date, Order.id, OrderItem.id, ProductionTask.id)
    if task_ids is not None:
        normalized_task_ids = [int(task_id) for task_id in task_ids]
        if not normalized_task_ids:
            return []
        query = query.where(ProductionTask.id.in_(normalized_task_ids))
    if page is not None and page_size is not None:
        query = query.offset((page - 1) * page_size).limit(page_size)
    rows = db.execute(query).all()
    receipt_purpose_summaries = _receipt_purpose_summaries_by_order_item_ids(
        db,
        [item.id for _task, item, _order, _customer, _product in rows],
    )
    pending_context = (
        _pending_production_read_context(db, rows)
        if status == PENDING
        else _PendingProductionReadContext(frozenset())
    )
    result: list[dict] = []
    for task, item, order, customer, product in rows:
        if pending_context.is_fast_path(task=task, item=item, product=product):
            result.append(
                _ordinary_pending_task_fast_payload(
                    task=task,
                    item=item,
                    order=order,
                    customer=customer,
                    product=product,
                )
            )
            continue
        is_component_task = task.sales_order_item_bom_component_id is not None
        component_demand = None
        if is_component_task:
            component_demand = next(
                (
                    row
                    for row in effective_component_demands(db, item.id)
                    if row.snapshot_id
                    == task.sales_order_item_bom_component_id
                ),
                None,
            )
            if component_demand is None:
                raise ProductionWorkflowError("订单组件需求快照不存在", 409)
            received_now = int(task.material_received_quantity or 0)
            allowed_input_now = int(task.material_input_quantity or 0)
            factor = max(int(task.output_factor or 1), 1)
            target_quantity = int(component_demand.required_piece_quantity)
        else:
            received_now, allowed_input_now = _material_quantity_facts(db, item)
            factor = cutting_output_factor(item.special_process)
            pieces_per_box = production_pieces_per_box(item)
            target_quantity = int(item.quantity or 0)
        if is_component_task:
            pieces_per_box = 1
        posted_input = int(
            db.scalar(
                select(
                    func.coalesce(
                        func.sum(ProductionCompletion.material_input_quantity),
                        0,
                    )
                ).where(
                    ProductionCompletion.task_id == task.id,
                    ProductionCompletion.status == "posted",
                )
            )
            or 0
        )
        posted_output = int(
            db.scalar(
                select(
                    func.coalesce(
                        func.sum(ProductionCompletion.actual_output_quantity),
                        0,
                    )
                ).where(
                    ProductionCompletion.task_id == task.id,
                    ProductionCompletion.status == "posted",
                )
            )
            or 0
        )
        material_input = max(
            allowed_input_now,
            int(task.material_input_quantity or 0),
        )
        available_input = max(material_input - posted_input, 0)
        result.append({
            "id": task.id,
            "order_item_id": item.id,
            "order_id": order.id,
            "order_number": order.order_number,
            "item_order_number": item.item_order_number,
            "customer_id": order.customer_id,
            "customer_name": customer.name,
            **_task_product_snapshot(
                db,
                task=task,
                item=item,
                parent_product=product,
            ),
            "order_quantity": target_quantity,
            "parent_order_quantity": int(item.quantity),
            "component_required_quantity": (
                target_quantity if is_component_task else None
            ),
            "delivered_quantity": int(item.delivered_quantity or 0),
            "material_status": item.material_status,
            "status": task.status,
            "planned_quantity": int(task.planned_quantity),
            "ordered_quantity": target_quantity,
            "material_received_quantity": max(
                received_now,
                int(task.material_received_quantity or 0),
            ),
            "material_input_quantity": material_input,
            "available_material_input_quantity": available_input,
            "output_factor": factor,
            "pieces_per_box": pieces_per_box,
            "planned_output_quantity": production_output_quantity(
                material_input,
                factor,
                pieces_per_box,
            ),
            "actual_output_quantity": posted_output,
            "order_reserved_quantity": min(
                posted_output + int(task.finished_coverage_snapshot or 0),
                target_quantity,
            ),
            "surplus_finished_quantity": max(
                posted_output
                + int(task.finished_coverage_snapshot or 0)
                - target_quantity,
                0,
            ),
            "can_supplement": task.status == COMPLETED and available_input > 0,
            "finished_coverage_snapshot": int(task.finished_coverage_snapshot),
            "readiness_basis": task.readiness_basis,
            "ready_at": utc_naive_to_api(task.ready_at) if task.ready_at else None,
            "version": int(task.version),
            "production_label_enabled_snapshot": bool(
                task.production_label_enabled_snapshot
            ),
            "production_label_units_per_label_snapshot": (
                task.production_label_units_per_label_snapshot
            ),
            "production_label_total_quantity_snapshot": int(
                task.production_label_total_quantity_snapshot or 0
            ),
            "production_label_count_snapshot": int(
                task.production_label_count_snapshot or 0
            ),
            "production_label_template_version_snapshot": (
                task.production_label_template_version_snapshot
            ),
            "production_label_product_version_snapshot": (
                task.production_label_product_version_snapshot
            ),
            "production_ready_quantity": production_ready_quantity(db, item),
            "customer_board_preparation_sources": (
                _active_customer_board_preparation_sources(
                    db,
                    task=task,
                    item=item,
                )
            ),
        })
    for row in result:
        row["receipt_purpose_summary"] = receipt_purpose_summaries.get(
            int(row["order_item_id"]),
            {
                "order_purpose_received_sheet_qty": 0,
                "reserve_purpose_received_sheet_qty": 0,
                "automatic_finished_output_qty": 0,
            },
        )
    _annotate_printing_plate_current_locations(db, result)
    return result


def list_production_station_task_ids(
    db: Session,
    *,
    allowed_customer_ids: set[int] | None,
    station: Literal["printing", "die_cut"],
    page: int,
    page_size: int,
) -> tuple[list[int], int, int]:
    """Return one station's pending task identities before payload pagination.

    The projection deliberately reads only task-frozen printing content, the
    immutable component snapshot, and the ordinary product's explicit box
    facts.  It never infers die cutting from names, drawings, notes or print
    content, and it keeps customer/status/order eligibility in the shared task
    query.
    """

    if station not in PRODUCTION_STATIONS:
        raise ValueError("unsupported production station")
    component_snapshot = aliased(SalesOrderItemBomComponent)
    component_task = aliased(ProductionTask)
    query = (
        _filtered_task_query(
            db,
            allowed_customer_ids=allowed_customer_ids,
            status=PENDING,
        )
        .outerjoin(
            component_snapshot,
            and_(
                component_snapshot.sales_order_item_id == OrderItem.id,
                component_snapshot.is_required.is_(True),
                or_(
                    ProductionTask.task_role == "order_main",
                    component_snapshot.id
                    == ProductionTask.sales_order_item_bom_component_id,
                ),
            ),
        )
        .outerjoin(
            component_task,
            and_(
                component_task.sales_order_item_bom_component_id
                == component_snapshot.id,
                component_task.task_role == "component_internal",
            ),
        )
        .with_only_columns(
            ProductionTask.id.label("task_id"),
            ProductionTask.print_content_snapshot.label("main_print_content_snapshot"),
            ProductionTask.production_profile_schema_version.label(
                "task_profile_schema_version"
            ),
            ProductionTask.production_box_style_snapshot.label(
                "task_box_style_snapshot"
            ),
            ProductionTask.production_needs_die_cut_snapshot.label(
                "task_needs_die_cut_snapshot"
            ),
            component_task.print_content_snapshot.label(
                "component_print_content_snapshot"
            ),
            component_snapshot.id.label(
                "bom_component_snapshot_id"
            ),
            Product.box_category.label("product_box_category"),
            Product.box_style.label("product_box_style"),
            component_snapshot.is_die_cut.label("component_is_die_cut"),
            component_snapshot.snapshot_component_box_style.label(
                "component_box_style"
            ),
        )
        .order_by(Order.delivery_date, Order.id, OrderItem.id, ProductionTask.id)
    )
    matching_ids: list[int] = []
    seen_ids: set[int] = set()
    for row in db.execute(query).mappings().all():
        is_component = row.bom_component_snapshot_id is not None
        has_frozen_profile = row.task_profile_schema_version is not None
        memberships = production_station_memberships(
            print_content_snapshot=(
                row.component_print_content_snapshot
                if is_component
                else row.main_print_content_snapshot
            ),
            box_style=(
                row.task_box_style_snapshot
                if has_frozen_profile
                else row.component_box_style
                if is_component
                else row.product_box_style
            ),
            die_cut_required=(
                bool(row.task_needs_die_cut_snapshot)
                if has_frozen_profile
                else bool(row.component_is_die_cut)
                if is_component
                else row.product_box_category == "die_cut"
            ),
        )
        task_id = int(row.task_id)
        if station in memberships and task_id not in seen_ids:
            matching_ids.append(task_id)
            seen_ids.add(task_id)

    total = len(matching_ids)
    last_page = max(1, (total + page_size - 1) // page_size)
    resolved_page = min(max(int(page), 1), last_page)
    start = (resolved_page - 1) * page_size
    return matching_ids[start : start + page_size], total, resolved_page


def find_pending_production_task_lookup_rows(
    db: Session,
    *,
    allowed_customer_ids: set[int] | None,
    keyword: str,
    limit: int = 50,
) -> tuple[list[dict], int]:
    """Find active pending tasks from a scanner or a short manual query.

    This is only an identity projection. The caller still obtains the
    workshop-safe task payload through :func:`list_production_tasks`, keeping
    existing production calculations and component snapshots authoritative.
    """

    normalized = keyword.strip()
    if not normalized:
        return [], 0
    escaped = (
        normalized.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
    pattern = f"%{escaped}%"
    lowered = normalized.casefold()
    parent_code = func.coalesce(
        func.nullif(OrderItem.snapshot_product_code, ""),
        Product.product_code,
    )
    parent_name = func.coalesce(
        func.nullif(OrderItem.snapshot_product_name, ""),
        Product.product_name,
    )
    lookup_component = aliased(SalesOrderItemBomComponent)
    component_match = exists().where(
        lookup_component.sales_order_item_id == OrderItem.id,
        or_(
            ProductionTask.task_role == "order_main",
            lookup_component.id == ProductionTask.sales_order_item_bom_component_id,
        ),
        or_(
            lookup_component.snapshot_component_product_code.ilike(
                pattern, escape="\\"
            ),
            lookup_component.snapshot_component_product_name.ilike(
                pattern, escape="\\"
            ),
        ),
    )
    component_exact = exists().where(
        lookup_component.sales_order_item_id == OrderItem.id,
        or_(
            ProductionTask.task_role == "order_main",
            lookup_component.id == ProductionTask.sales_order_item_bom_component_id,
        ),
        func.lower(lookup_component.snapshot_component_product_code) == lowered,
    )
    match_condition = or_(
        cast(ProductionTask.id, String) == normalized,
        Order.order_number.ilike(pattern, escape="\\"),
        Order.customer_po.ilike(pattern, escape="\\"),
        OrderItem.item_order_number.ilike(pattern, escape="\\"),
        parent_code.ilike(pattern, escape="\\"),
        parent_name.ilike(pattern, escape="\\"),
        Product.product_code.ilike(pattern, escape="\\"),
        Product.customer_material_code.ilike(pattern, escape="\\"),
        Customer.name.ilike(pattern, escape="\\"),
        component_match,
    )
    exact_rank = case(
        (func.lower(cast(ProductionTask.id, String)) == lowered, 0),
        (func.lower(parent_code) == lowered, 0),
        (component_exact, 0),
        (func.lower(Order.order_number) == lowered, 0),
        (func.lower(OrderItem.item_order_number) == lowered, 0),
        (func.lower(Order.customer_po) == lowered, 0),
        else_=1,
    )
    base = _filtered_task_query(
        db,
        allowed_customer_ids=allowed_customer_ids,
        status=PENDING,
    ).where(match_condition)
    count_query = base.with_only_columns(ProductionTask.id).order_by(None).subquery()
    total = int(db.scalar(select(func.count()).select_from(count_query)) or 0)
    rows = db.execute(
        base.with_only_columns(
            ProductionTask.id.label("task_id"),
            Order.customer_po.label("customer_po"),
            Order.delivery_date.label("delivery_date"),
        )
        .order_by(
            exact_rank,
            Order.delivery_date.is_(None),
            Order.delivery_date,
            Order.id,
            OrderItem.id,
            ProductionTask.id,
        )
        .limit(max(1, min(int(limit), 50)))
    ).mappings().all()
    return [
        {
            "task_id": int(row.task_id),
            "customer_po": row.customer_po,
            "delivery_date": row.delivery_date,
        }
        for row in rows
    ], total


def count_production_tasks(
    db: Session,
    *,
    allowed_customer_ids: set[int] | None,
    status: str | None = None,
) -> int:
    task_ids = (
        _filtered_task_query(
            db,
            allowed_customer_ids=allowed_customer_ids,
            status=status,
        )
        .with_only_columns(ProductionTask.id)
        .order_by(None)
        .subquery()
    )
    return int(db.scalar(select(func.count()).select_from(task_ids)) or 0)


def _completion_rows(
    db: Session,
    *,
    allowed_customer_ids: set[int] | None,
    completion_ids: Sequence[int] | None = None,
    customer_id: int | None = None,
    order_keyword: str | None = None,
    product_code: str | None = None,
    product_name: str | None = None,
    completed_date_from: date | None = None,
    completed_date_to: date | None = None,
    status: str | None = None,
    page: int | None = None,
    page_size: int | None = None,
):
    query = (
        select(
            ProductionCompletion,
            ProductionTask,
            OrderItem,
            Order,
            Customer,
            Product,
            User,
            ProductionStockTransfer,
        )
        .join(ProductionTask, ProductionTask.id == ProductionCompletion.task_id)
        .join(OrderItem, OrderItem.id == ProductionCompletion.order_item_id)
        .join(Order, Order.id == OrderItem.order_id)
        .join(Customer, Customer.id == Order.customer_id)
        .join(Product, Product.id == OrderItem.product_id)
        .outerjoin(User, User.id == ProductionCompletion.completed_by)
        .outerjoin(
            ProductionStockTransfer,
            ProductionStockTransfer.completion_id == ProductionCompletion.id,
        )
    )
    if allowed_customer_ids is not None:
        query = query.where(Order.customer_id.in_(allowed_customer_ids))
    if completion_ids is not None:
        query = query.where(ProductionCompletion.id.in_(completion_ids))
    if customer_id is not None:
        query = query.where(Order.customer_id == customer_id)
    if normalized_keyword := (order_keyword or "").strip():
        pattern = f"%{normalized_keyword}%"
        query = query.where(
            or_(
                Order.order_number.like(pattern),
                Order.customer_po.like(pattern),
                OrderItem.item_order_number.like(pattern),
            )
        )
    if normalized_product_code := (product_code or "").strip():
        pattern = f"%{normalized_product_code}%"
        query = query.where(
            or_(
                Product.product_code.like(pattern),
                Product.customer_material_code.like(pattern),
                OrderItem.snapshot_product_code.like(pattern),
            )
        )
    if normalized_product_name := (product_name or "").strip():
        pattern = f"%{normalized_product_name}%"
        query = query.where(
            or_(
                Product.product_name.like(pattern),
                OrderItem.snapshot_product_name.like(pattern),
            )
        )
    if completed_date_from is not None:
        start_at, _ = beijing_date_bounds_utc_naive(completed_date_from)
        query = query.where(ProductionCompletion.completed_at >= start_at)
    if completed_date_to is not None:
        _, end_at = beijing_date_bounds_utc_naive(completed_date_to)
        query = query.where(ProductionCompletion.completed_at < end_at)
    if status is not None:
        query = query.where(ProductionCompletion.status == status)

    query = query.order_by(
        ProductionCompletion.completed_at.desc(),
        ProductionCompletion.id.desc(),
    )
    if page is not None and page_size is not None:
        query = query.offset((page - 1) * page_size).limit(page_size)
    return db.execute(query).all()


def _production_completion_total(
    db: Session,
    *,
    allowed_customer_ids: set[int] | None,
    customer_id: int | None,
    order_keyword: str | None,
    product_code: str | None,
    product_name: str | None,
    completed_date_from: date | None,
    completed_date_to: date | None,
    status: str | None,
) -> int:
    """Count the same customer-scoped completion set as the history page."""

    query = (
        select(ProductionCompletion.id)
        .join(ProductionTask, ProductionTask.id == ProductionCompletion.task_id)
        .join(OrderItem, OrderItem.id == ProductionCompletion.order_item_id)
        .join(Order, Order.id == OrderItem.order_id)
        .join(Product, Product.id == OrderItem.product_id)
    )
    if allowed_customer_ids is not None:
        query = query.where(Order.customer_id.in_(allowed_customer_ids))
    if customer_id is not None:
        query = query.where(Order.customer_id == customer_id)
    if normalized_keyword := (order_keyword or "").strip():
        pattern = f"%{normalized_keyword}%"
        query = query.where(
            or_(
                Order.order_number.like(pattern),
                Order.customer_po.like(pattern),
                OrderItem.item_order_number.like(pattern),
            )
        )
    if normalized_product_code := (product_code or "").strip():
        pattern = f"%{normalized_product_code}%"
        query = query.where(
            or_(
                Product.product_code.like(pattern),
                Product.customer_material_code.like(pattern),
                OrderItem.snapshot_product_code.like(pattern),
            )
        )
    if normalized_product_name := (product_name or "").strip():
        pattern = f"%{normalized_product_name}%"
        query = query.where(
            or_(
                Product.product_name.like(pattern),
                OrderItem.snapshot_product_name.like(pattern),
            )
        )
    if completed_date_from is not None:
        start_at, _ = beijing_date_bounds_utc_naive(completed_date_from)
        query = query.where(ProductionCompletion.completed_at >= start_at)
    if completed_date_to is not None:
        _, end_at = beijing_date_bounds_utc_naive(completed_date_to)
        query = query.where(ProductionCompletion.completed_at < end_at)
    if status is not None:
        query = query.where(ProductionCompletion.status == status)
    return int(db.scalar(select(func.count()).select_from(query.subquery())) or 0)


def _production_completion_dicts(db: Session, rows: Sequence[tuple]) -> list[dict]:
    dispatched_item_ids = _dispatched_delivery_order_item_ids(
        db,
        [item.id for _completion, _task, item, *_rest in rows],
    )
    result: list[dict] = []
    for completion, task, item, order, customer, product, user, transfer in rows:
        received_now, allowed_input_now = _material_quantity_facts(db, item)
        posted_input = int(
            db.scalar(
                select(
                    func.coalesce(
                        func.sum(ProductionCompletion.material_input_quantity),
                        0,
                    )
                ).where(
                    ProductionCompletion.task_id == task.id,
                    ProductionCompletion.status == "posted",
                )
            )
            or 0
        )
        available_input = max(
            max(allowed_input_now, int(task.material_input_quantity or 0))
            - posted_input,
            0,
        )
        effective_location_id = (
            transfer.warehouse_location_id
            if transfer is not None
            else completion.warehouse_location_id
        )
        effective_lot_id = (
            transfer.inventory_lot_id if transfer is not None else completion.inventory_lot_id
        )
        effective_lot = (
            db.get(InventoryLot, effective_lot_id)
            if effective_lot_id is not None
            else None
        )
        effective_pallet = (
            effective_lot.pallet_item.pallet
            if effective_lot is not None and effective_lot.pallet_item is not None
            else None
        )
        location = (
            db.get(WarehouseLocation, effective_location_id)
            if effective_location_id is not None
            else None
        )
        result.append(
            {
                "id": completion.id,
                "batch_id": completion.batch_id,
                "task_id": task.id,
                "task_version": int(task.version),
                "order_item_id": item.id,
                "order_id": order.id,
                "order_number": order.order_number,
                "item_order_number": item.item_order_number,
                "customer_id": order.customer_id,
                "customer_name": customer.name,
                **_task_product_snapshot(
                    db,
                    task=task,
                    item=item,
                    parent_product=product,
                ),
                "quantity": int(completion.quantity),
                "completion_type": completion.completion_type,
                "material_input_quantity": int(completion.material_input_quantity),
                "planned_output_quantity": int(completion.planned_output_quantity),
                "actual_output_quantity": int(completion.actual_output_quantity),
                "defective_quantity": int(completion.defective_quantity),
                "order_reserved_quantity": int(completion.order_reserved_quantity),
                "direct_delivery_quantity": int(completion.direct_delivery_quantity),
                "stock_quantity": int(completion.stock_quantity),
                "surplus_finished_quantity": int(completion.surplus_finished_quantity),
                "available_material_input_quantity": available_input,
                "current_material_received_quantity": max(
                    received_now,
                    int(task.material_received_quantity or 0),
                ),
                "output_factor": max(int(task.output_factor or 1), 1),
                "pieces_per_box": (
                    1
                    if task.sales_order_item_bom_component_id is not None
                    else production_pieces_per_box(item)
                ),
                "can_supplement": (
                    completion.status == "posted"
                    and task.status == COMPLETED
                    and available_input > 0
                    and order.status in MUTABLE_ORDER_STATUSES
                    and not item.is_force_closed
                ),
                "status": completion.status,
                "initial_disposition": completion.initial_disposition,
                "warehouse_location_id": effective_location_id,
                "warehouse_location_code": location.location_code if location else None,
                "warehouse_location_name": employee_location_name(location),
                "inventory_lot_id": effective_lot_id,
                "system_pallet_id": (
                    effective_pallet.id if effective_pallet is not None else None
                ),
                "system_pallet_code": (
                    effective_pallet.pallet_code
                    if effective_pallet is not None
                    else None
                ),
                "remarks": completion.remarks,
                "completed_by": completion.completed_by,
                "completed_by_name": user.real_name if user is not None else None,
                "completed_at": (
                    utc_naive_to_api(completion.completed_at)
                    if completion.completed_at
                    else None
                ),
                "reversed_by": completion.reversed_by,
                "reversed_at": (
                    utc_naive_to_api(completion.reversed_at)
                    if completion.reversed_at
                    else None
                ),
                "reversal_reason": completion.reversal_reason,
                "stock_transfer_id": transfer.id if transfer is not None else None,
                "can_transfer_to_stock": (
                    completion.initial_disposition == "direct"
                    and transfer is None
                    and int(item.delivered_quantity or 0) == 0
                    and item.id not in dispatched_item_ids
                    and order.status in MUTABLE_ORDER_STATUSES
                    and not item.is_force_closed
                ),
                "can_revert": (
                    completion.status == "posted"
                    and task.status == COMPLETED
                    and int(item.delivered_quantity or 0) == 0
                    and item.id not in dispatched_item_ids
                    and order.status in MUTABLE_ORDER_STATUSES
                    and not item.is_force_closed
                ),
            }
        )
    _annotate_printing_plate_current_locations(db, result)
    return result


def list_production_completions(
    db: Session,
    *,
    allowed_customer_ids: set[int] | None,
    completion_ids: Sequence[int] | None = None,
) -> list[dict]:
    """Return the full legacy completion history for existing workflow callers."""

    rows = _completion_rows(
        db,
        allowed_customer_ids=allowed_customer_ids,
        completion_ids=completion_ids,
    )
    return _production_completion_dicts(db, rows)


def list_production_completions_page(
    db: Session,
    *,
    allowed_customer_ids: set[int] | None,
    customer_id: int | None = None,
    order_keyword: str | None = None,
    product_code: str | None = None,
    product_name: str | None = None,
    completed_date_from: date | None = None,
    completed_date_to: date | None = None,
    status: Literal["posted", "reversed"] | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[dict], int]:
    """Return a stable, customer-scoped page for the production history list."""

    total = _production_completion_total(
        db,
        allowed_customer_ids=allowed_customer_ids,
        customer_id=customer_id,
        order_keyword=order_keyword,
        product_code=product_code,
        product_name=product_name,
        completed_date_from=completed_date_from,
        completed_date_to=completed_date_to,
        status=status,
    )
    rows = _completion_rows(
        db,
        allowed_customer_ids=allowed_customer_ids,
        customer_id=customer_id,
        order_keyword=order_keyword,
        product_code=product_code,
        product_name=product_name,
        completed_date_from=completed_date_from,
        completed_date_to=completed_date_to,
        status=status,
        page=page,
        page_size=page_size,
    )
    return _production_completion_dicts(db, rows), total


def completion_customer_id(db: Session, completion_id: int) -> int | None:
    return db.scalar(
        select(Order.customer_id)
        .join(OrderItem, OrderItem.order_id == Order.id)
        .join(
            ProductionCompletion,
            ProductionCompletion.order_item_id == OrderItem.id,
        )
        .where(ProductionCompletion.id == completion_id)
    )


def batch_customer_ids(
    db: Session,
    completions: Sequence[ProductionCompletion],
) -> set[int]:
    item_ids = [row.order_item_id for row in completions]
    if not item_ids:
        return set()
    return set(
        db.scalars(
            select(Order.customer_id)
            .join(OrderItem, OrderItem.order_id == Order.id)
            .where(OrderItem.id.in_(item_ids))
        ).all()
    )
