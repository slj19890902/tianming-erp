from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import String, and_, case, cast, delete, exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import (
    PermissionChecker,
    customer_scope_ids,
    get_db,
    get_current_user,
    has_permission,
    has_unrestricted_customer_access,
    require_customer_access,
)
from app.core.time_contract import beijing_today, utc_naive_to_api, utc_now_naive
from app.models.audit import OperationLog
from app.models.company_config import CompanyConfig
from app.models.customer import Customer
from app.models.delivery import (
    Delivery,
    DeliveryItem,
    DeliveryPickTask,
    DeliveryPickTaskItem,
)
from app.models.finance import ReturnReceipt
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.product_bom import (
    BomComponentDirectDeliveryAllocation,
    SalesOrderItemBomComponent,
    SalesOrderItemBomDemandAdjustment,
)
from app.models.production import (
    ProductionCompletion,
    ProductionStockTransfer,
    ProductionTask,
)
from app.models.requisition import RequisitionItem
from app.models.tianhua_pre_delivery import (
    TianhuaPreDeliveryDraft,
    TianhuaPreDeliveryDraftItem,
)
from app.models.user import User
from app.models.warehouse_inventory import (
    DeliveryInventoryAllocation,
    Floor3LocationLayout,
    FinishedGoodsInventoryDetail,
    InventoryLot,
    InventoryMovement,
    InventoryReservation,
    OrderItemSemiRequirement,
    UnorderedFinishedDeliveryAllocation,
    WarehouseArea,
    WarehouseFloor,
    WarehouseLocation,
)
from app.services.history_orders import build_display_registry, display_order_number
from app.services.location_candidates import is_operational_location
from app.services.warehouse_floor1_candidate_planner import (
    overlay_formal_area_bindings,
)
from app.services.warehouse_twin_layout import (
    WarehouseTwinLayoutNotFoundError,
    load_warehouse_twin_floor,
)
from app.services.delivery_numbering import (
    DeliveryNumberingError,
    next_delivery_number,
)
from app.services.delivery_snapshots import (
    build_order_delivery_snapshot,
    ensure_order_delivery_snapshot,
)
from app.services.production_workflow import (
    ProductionWorkflowError,
    lock_order_rows_for_production_transition,
    normalized_completion_output,
    production_ready_quantity,
)
from app.services.composite_bom_workflow import (
    ACTIVE_RESERVATION_STATUSES,
    DIRECT_DISPOSITION,
    ComponentDemand,
    CompositeBomWorkflowError,
    component_availability,
    delivery_component_required_quantities,
    delivered_component_quantities,
    delivery_item_component_quantities,
    effective_component_demands,
    execute_delivery_component_consumption,
    is_composite_order_item,
    kit_availability,
    kit_available_sets_by_order_item_ids,
    reverse_delivery_component_allocations,
)
from app.services.semi_finished_inventory import (
    active_semi_requirement_credited_quantity,
    consume_delivery_item_inventory,
    inventory_fully_covers_order_item,
    reverse_delivery_item_inventory,
)
from app.services.warehouse_inventory import (
    WarehouseInventoryError,
    active_finished_reserved_qty,
    active_finished_reservations_by_item_ids,
    inventory_fifo_order_columns,
    inventory_fifo_sort_key,
    release_empty_pallets_after_delivery,
    restore_auto_released_pallets_after_delivery_cancel,
)
from app.services.unordered_finished_delivery import (
    cancel_unordered_finished_dispatch,
    dispatch_unordered_finished_inventory,
)


router = APIRouter()
order_actions_router = APIRouter()
pick_router = APIRouter()
can_read = PermissionChecker("deliveries.view")
can_operate = PermissionChecker("deliveries.execute")
can_pick = PermissionChecker("deliveries.pick")
PICK_TASK_STATUSES = {"pushed", "driver_confirmed", "exception", "applied", "dispatched"}


def _utc_now() -> datetime:
    return utc_now_naive()


def _print_product_code(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return re.split(r"\s*/\s*|\s+", text, maxsplit=1)[0]


def _tianhua_internal_remarks_by_delivery_item(
    db: Session,
    delivery_item_ids: set[int] | list[int],
) -> dict[int, set[str]]:
    ids = {int(value) for value in delivery_item_ids if value}
    if not ids:
        return {}
    rows = db.execute(
        select(
            TianhuaPreDeliveryDraftItem.delivery_item_id,
            TianhuaPreDeliveryDraft.draft_number,
        )
        .join(
            TianhuaPreDeliveryDraft,
            TianhuaPreDeliveryDraft.id
            == TianhuaPreDeliveryDraftItem.draft_id,
        )
        .where(TianhuaPreDeliveryDraftItem.delivery_item_id.in_(ids))
    ).all()
    internal_remarks: dict[int, set[str]] = {}
    for delivery_item_id, draft_number in rows:
        if delivery_item_id is None:
            continue
        internal_remarks.setdefault(int(delivery_item_id), set()).add(
            f"来源：天华预送货草稿 {draft_number}"
        )
    return internal_remarks


def _customer_visible_delivery_remark(
    delivery_item_id: int,
    remark: str | None,
    internal_remarks: dict[int, set[str]],
) -> str | None:
    value = str(remark or "").strip()
    if not value:
        return None
    if value in internal_remarks.get(int(delivery_item_id), set()):
        return None
    return value


def _component_kind(name: str | None) -> str:
    value = str(name or "")
    if value.endswith("-底"):
        return "base"
    if value.endswith("-盖"):
        return "cover"
    return ""


def _received_telescoping_capacity(db: Session, order_item_id: int) -> int | None:
    rows = db.execute(
        select(
            RequisitionItem.product_name_snapshot,
            RequisitionItem.requisition_qty,
            RequisitionItem.status,
        ).where(RequisitionItem.order_item_id == order_item_id)
    ).all()
    base_qty = 0
    cover_qty = 0
    has_telescoping_component = False
    for product_name, quantity, item_status in rows:
        component = _component_kind(product_name)
        if component:
            has_telescoping_component = True
        if item_status != "已入库":
            continue
        if component == "base":
            base_qty += int(quantity or 0)
        elif component == "cover":
            cover_qty += int(quantity or 0)
    if has_telescoping_component:
        return min(base_qty, cover_qty)
    return None


def _delivery_remaining_quantity(db: Session, order_item: OrderItem) -> int:
    if is_composite_order_item(db, order_item.id):
        return int(kit_availability(db, order_item.id)["available_sets"])
    task = db.scalar(
        select(ProductionTask).where(
            ProductionTask.order_item_id == order_item.id,
        )
    )
    if task is not None:
        if task.status not in {"completed", "not_required"}:
            return 0
        max_deliverable = max(production_ready_quantity(db, order_item), 0)
        return max(max_deliverable - int(order_item.delivered_quantity or 0), 0)

    max_deliverable = int(order_item.quantity or 0)
    inventory_covered = inventory_fully_covers_order_item(db, order_item.id)
    component_capacity = _received_telescoping_capacity(db, order_item.id)
    if component_capacity is not None:
        max_deliverable = min(max_deliverable, component_capacity)
    elif order_item.material_status != "received" and not inventory_covered:
        return 0
    return max(max_deliverable - int(order_item.delivered_quantity or 0), 0)


def _delivery_quantity_facts(db: Session, order_item: OrderItem) -> dict[str, int]:
    ordered = max(int(order_item.quantity or 0), 0)
    delivered = max(int(order_item.delivered_quantity or 0), 0)
    order_remaining = max(ordered - delivered, 0)
    deliverable = _delivery_remaining_quantity(db, order_item)
    return {
        "ordered_quantity": ordered,
        "delivered_quantity": delivered,
        "order_remaining_quantity": order_remaining,
        "deliverable_quantity": deliverable,
        "over_delivery_quantity": max(deliverable - order_remaining, 0),
        "surplus_finished_quantity": max(deliverable - order_remaining, 0),
    }


def _has_production_task(db: Session, order_item_id: int) -> bool:
    return db.scalar(
        select(ProductionTask.id)
        .where(ProductionTask.order_item_id == order_item_id)
        .limit(1)
    ) is not None


def _has_active_production_stock_reservation(
    db: Session,
    order_item_id: int,
) -> bool:
    return db.scalar(
        select(InventoryReservation.id)
        .join(InventoryLot, InventoryLot.id == InventoryReservation.inventory_lot_id)
        .where(
            InventoryReservation.order_item_id == order_item_id,
            InventoryReservation.reservation_type == "finished_order",
            InventoryReservation.status.in_(("active", "partial")),
            InventoryReservation.reserved_stock_quantity
            > InventoryReservation.consumed_stock_quantity
            + InventoryReservation.released_stock_quantity,
            InventoryLot.source_ref_type == "production_completion",
        )
        .limit(1)
    ) is not None


_DELIVERY_ROUTE_AREAS = (
    ("苏州工业园区", ("苏州工业园区", "工业园区")),
    ("相城区", ("相城区",)),
    ("吴中区", ("吴中区",)),
    ("吴江区", ("吴江区",)),
    ("虎丘区/高新区", ("虎丘区", "高新区", "苏州新区")),
    ("姑苏区", ("姑苏区",)),
    ("昆山市", ("昆山市", "昆山")),
    ("常熟市", ("常熟市", "常熟")),
    ("太仓市", ("太仓市", "太仓")),
    ("张家港市", ("张家港市", "张家港")),
)


def _delivery_route_area(address: str | None) -> tuple[str, str | None]:
    text = re.sub(r"\s+", "", str(address or "").strip())
    if not text:
        return "地址未登记", None
    area = "其他区域"
    for label, aliases in _DELIVERY_ROUTE_AREAS:
        if any(alias in text for alias in aliases):
            area = label
            break
    if area == "其他区域":
        match = re.search(r"([\u4e00-\u9fff]{2,8}(?:区|县|市))", text)
        if match:
            area = match.group(1)
    subarea_match = re.search(
        r"([\u4e00-\u9fff]{2,12}(?:镇|街道|开发区|产业园|工业园))",
        text,
    )
    return area, subarea_match.group(1) if subarea_match else None


def _baidu_delivery_direction_url(
    origin: str | None,
    destination: str | None,
) -> str | None:
    if not origin or not destination:
        return None
    return "https://api.map.baidu.com/direction?" + urlencode(
        {
            "origin": origin,
            "destination": destination,
            "mode": "driving",
            "region": "苏州",
            "output": "html",
            "coord_type": "bd09ll",
            "src": "webapp.tianming.erp",
        }
    )


def _normalized_delivery_address(address: str | None) -> str:
    return re.sub(r"[\s,，/]+", "", str(address or "").strip()).lower()


class UnorderedFinishedAllocationCreate(BaseModel):
    inventory_lot_id: int
    quantity: int


class DeliveryLineCreate(BaseModel):
    source_type: str = "order"
    order_item_id: int | None = None
    product_id: int | None = None
    delivered_quantity: int
    unit_price: Decimal | None = None
    allocations: list[UnorderedFinishedAllocationCreate] = Field(default_factory=list)
    over_delivery_confirmed: bool = False
    over_delivery_reason: str | None = None
    remarks: str | None = None


class DeliveryCreate(BaseModel):
    customer_id: int
    delivery_date: date | None = None
    vehicle_number: str | None = None
    source_mode: str = "order"
    items: list[DeliveryLineCreate] = Field(default_factory=list)
    lines: list[DeliveryLineCreate] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_delivery_lines(self):
        if self.items and self.lines:
            raise ValueError("送货明细只能使用 items 或 lines 其中一种格式")
        selected = self.items or self.lines
        if not selected:
            raise ValueError("送货单至少需要一条明细")
        self.items = selected
        self.lines = []
        if self.source_mode == "unordered_finished":
            for line in selected:
                if line.source_type == "finished_stock":
                    line.source_type = "unordered_finished"
        return self


class DeliveryUpdate(BaseModel):
    delivery_date: date | None = None
    vehicle_number: str | None = None
    source_mode: str = "order"
    items: list[DeliveryLineCreate] = Field(default_factory=list)
    lines: list[DeliveryLineCreate] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_delivery_lines(self):
        if self.items and self.lines:
            raise ValueError("送货明细只能使用 items 或 lines 其中一种格式")
        selected = self.items or self.lines
        if not selected:
            raise ValueError("送货单至少需要一条明细")
        self.items = selected
        self.lines = []
        if self.source_mode == "unordered_finished":
            for line in selected:
                if line.source_type == "finished_stock":
                    line.source_type = "unordered_finished"
        return self


def _validate_delivery_source_contract(
    source_mode: str,
    lines: list[DeliveryLineCreate],
) -> None:
    if source_mode not in {"order", "unordered_finished", "mixed"}:
        raise ValueError("送货来源必须是订单待送、无订单成品库存或两者混合")
    seen_order_items: set[int] = set()
    seen_products: set[int] = set()
    seen_lots: set[int] = set()
    source_types: set[str] = set()
    for index, line in enumerate(lines, start=1):
        normalized = (line.source_type or "order").strip()
        if normalized == "finished_stock":
            normalized = "unordered_finished"
            line.source_type = normalized
        if normalized not in {"order", "unordered_finished"}:
            raise ValueError(f"第 {index} 条送货明细来源无效")
        source_types.add(normalized)
        if source_mode != "mixed" and normalized != source_mode:
            raise ValueError(f"第 {index} 条送货明细来源不一致，禁止混合送货")
        if normalized == "order":
            if line.order_item_id is None or line.product_id is not None:
                raise ValueError(f"第 {index} 条订单待送明细缺少订单关联")
            if line.order_item_id in seen_order_items:
                raise ValueError("同一订单明细不能重复选择")
            seen_order_items.add(line.order_item_id)
            if line.allocations:
                raise ValueError("订单待送不能提交无订单库存批次")
            continue
        if line.order_item_id is not None or line.product_id is None:
            raise ValueError(f"第 {index} 条无订单库存明细缺少产品")
        if line.product_id in seen_products:
            raise ValueError("同一产品在一张无订单送货单中只能出现一行")
        seen_products.add(line.product_id)
        if line.unit_price is not None and line.unit_price < 0:
            raise ValueError(f"第 {index} 条无订单库存明细单价不能小于零")
        if not line.allocations:
            raise ValueError(f"第 {index} 条无订单库存明细必须选择库存批次")
        allocated = 0
        for allocation in line.allocations:
            if allocation.quantity <= 0:
                raise ValueError(f"第 {index} 条库存批次数量必须大于零")
            if allocation.inventory_lot_id in seen_lots:
                raise ValueError("同一库存批次不能在一张送货单中重复使用")
            seen_lots.add(allocation.inventory_lot_id)
            allocated += allocation.quantity
        if allocated != line.delivered_quantity:
            raise ValueError(f"第 {index} 条送货数量必须等于批次分配数量")
    if source_mode == "mixed" and source_types != {"order", "unordered_finished"}:
        raise ValueError("混合送货必须同时包含订单待送和无订单成品库存")


class DeliveryPickItemUpdate(BaseModel):
    pick_status: str
    picked_quantity: int | None = None

    @field_validator("pick_status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"picked", "partial", "no_stock"}:
            raise ValueError("拿货状态必须是 picked、partial 或 no_stock")
        return normalized


class DeliveryPickAssignmentUpdate(BaseModel):
    picker_user_id: int | None = Field(default=None, gt=0)


def _pick_task_read_context(
    db: Session,
    items: list[DeliveryPickTaskItem],
) -> dict:
    """Preload stable task references and empty-source facts in batches."""
    delivery_item_ids = {
        item.delivery_item_id for item in items if item.delivery_item_id is not None
    }
    order_item_ids = {
        item.order_item_id for item in items if item.order_item_id is not None
    }
    delivery_items = {
        row.id: row
        for row in db.scalars(
            select(DeliveryItem).where(DeliveryItem.id.in_(delivery_item_ids))
        ).all()
    } if delivery_item_ids else {}
    order_items = {
        row.id: row
        for row in db.scalars(
            select(OrderItem).where(OrderItem.id.in_(order_item_ids))
        ).all()
    } if order_item_ids else {}
    composite_order_item_ids = set(
        db.scalars(
            select(SalesOrderItemBomComponent.sales_order_item_id)
            .where(
                SalesOrderItemBomComponent.sales_order_item_id.in_(order_item_ids)
            )
            .distinct()
        ).all()
    ) if order_item_ids else set()
    inventory_source_order_item_ids = set(
        db.scalars(
            select(InventoryReservation.order_item_id)
            .where(
                InventoryReservation.order_item_id.in_(order_item_ids),
                InventoryReservation.status != "cancelled",
                func.coalesce(
                    InventoryReservation.credited_requirement_quantity,
                    0,
                )
                > InventoryReservation.released_requirement_quantity,
            )
            .distinct()
        ).all()
    ) if order_item_ids else set()
    return {
        "delivery_items": delivery_items,
        "order_items": order_items,
        "composite_order_item_ids": composite_order_item_ids,
        "inventory_source_order_item_ids": inventory_source_order_item_ids,
    }


def _pick_item_component_lines(
    db: Session,
    item: DeliveryPickTaskItem,
    *,
    read_context: dict | None = None,
) -> list[dict]:
    """Return read-only parent-priced component goods for one pick row.

    Pick tasks persist exactly one operable row per delivery item.  Components
    deliberately remain derived display data: the driver still confirms only
    the parent delivery quantity, while the later dispatch transaction applies
    the existing component inventory gate and allocation facts.
    """
    delivery_item = (
        read_context["delivery_items"].get(item.delivery_item_id)
        if read_context is not None
        else (
            db.get(DeliveryItem, item.delivery_item_id)
            if item.delivery_item_id is not None
            else None
        )
    )
    if (
        delivery_item is None
        or item.order_item_id is None
        or delivery_item.order_item_id != item.order_item_id
    ):
        return []
    order_item = (
        read_context["order_items"].get(item.order_item_id)
        if read_context is not None
        else db.get(OrderItem, item.order_item_id)
    )
    is_composite = (
        order_item is not None
        and (
            order_item.id in read_context["composite_order_item_ids"]
            if read_context is not None
            else is_composite_order_item(db, order_item.id)
        )
    )
    if order_item is None or not is_composite:
        return []
    return [
        component
        for component in _delivery_component_lines(
            db,
            order_item=order_item,
            planned_delivery_quantity=int(item.original_quantity or 0),
            delivery_item_id=delivery_item.id,
            dispatched=False,
        )
        if int(component.get("planned_delivery_quantity") or 0) > 0
    ]


def _pick_unordered_location_plan(
    db: Session,
    *,
    item: DeliveryPickTaskItem,
    delivery_item: DeliveryItem,
) -> tuple[list[dict], bool]:
    allocations = db.scalars(
        select(UnorderedFinishedDeliveryAllocation)
        .where(
            UnorderedFinishedDeliveryAllocation.delivery_item_id
            == delivery_item.id,
            UnorderedFinishedDeliveryAllocation.status == "planned",
        )
        .order_by(UnorderedFinishedDeliveryAllocation.id)
    ).all()
    lines: list[dict] = []
    for allocation in allocations:
        quantity = int(allocation.planned_quantity or 0)
        if quantity <= 0:
            continue
        location = _pick_source_location(
            db,
            source={"lot_id": allocation.inventory_lot_id},
        )
        lines.append(
            {
                "pick_item_id": item.id,
                "order_item_id": None,
                "source_type": "finished_inventory",
                "reservation_id": None,
                "lot_id": allocation.inventory_lot_id,
                "lot_number": allocation.lot_number_snapshot,
                "component_snapshot_id": None,
                "product_code": item.product_code_snapshot,
                "product_name": item.product_name_snapshot,
                "specification": item.specification_snapshot,
                "pick_quantity": quantity,
                "requirement_quantity": quantity,
                "unit": "个",
                "location_id": location["location_id"],
                "location_code": allocation.warehouse_location_code_snapshot
                or location["location_code"],
                "location_name": location["location_name"],
                "warehouse_floor": location["warehouse_floor"],
                "area_code": location["area_code"],
                "location_sort_order": location["location_sort_order"],
                "placement_status": location["placement_status"],
                "pallet_id": location["pallet_id"],
                "pallet_code": allocation.pallet_code_snapshot
                or location["pallet_code"],
                "location_operational": location["location_operational"],
                "needs_relocation": location["needs_relocation"],
                "requires_attention": bool(
                    location["location_id"] is not None
                    and not location["location_operational"]
                ),
            }
        )
    complete_quantity = sum(int(line["pick_quantity"]) for line in lines)
    complete = complete_quantity == int(item.original_quantity or 0) and not any(
        line["requires_attention"] for line in lines
    )
    return lines, complete


def _pick_source_location(
    db: Session,
    *,
    source: dict,
) -> dict:
    lot = (
        db.get(InventoryLot, int(source["lot_id"]))
        if source.get("lot_id") is not None
        else None
    )
    location = (
        db.get(WarehouseLocation, lot.warehouse_location_id)
        if lot is not None
        else None
    )
    pallet_item = lot.pallet_item if lot is not None else None
    pallet = pallet_item.pallet if pallet_item is not None else None
    location_operational = bool(
        location is not None
        and is_operational_location(
            db,
            location,
            warehouse_types={"finished", "shared"},
        )
    )
    needs_relocation = bool(
        (pallet is not None and pallet.needs_relocation)
        or (location is not None and location.placement_status == "unplaced")
        or (location is not None and not location_operational)
    )
    return {
        "location_id": location.id if location else None,
        "location_code": location.location_code if location else None,
        "location_name": location.location_name if location else None,
        "warehouse_floor": location.warehouse_floor if location else None,
        "area_code": location.area_code if location else None,
        "location_sort_order": int(location.sort_order or 0) if location else None,
        "placement_status": location.placement_status if location else None,
        "pallet_id": pallet.id if pallet else None,
        "pallet_code": pallet.pallet_code if pallet else None,
        "location_operational": location_operational,
        "needs_relocation": needs_relocation,
    }


def _pick_parent_finished_sources(
    db: Session,
    *,
    order_item: OrderItem,
    planned_quantity: int,
) -> list[dict]:
    """Select unconsumed parent-product reservations for a composite delivery."""

    remaining = max(int(planned_quantity or 0), 0)
    if remaining <= 0:
        return []
    reservations = db.scalars(
        select(InventoryReservation)
        .join(InventoryLot, InventoryLot.id == InventoryReservation.inventory_lot_id)
        .where(
            InventoryReservation.order_item_id == order_item.id,
            InventoryReservation.reservation_type == "finished_order",
            InventoryReservation.sales_order_item_bom_component_id.is_(None),
            InventoryReservation.status != "cancelled",
            InventoryReservation.reserved_stock_quantity
            > InventoryReservation.consumed_stock_quantity
            + InventoryReservation.released_stock_quantity,
        )
        .order_by(*inventory_fifo_order_columns(), InventoryReservation.id)
    ).all()
    sources: list[dict] = []
    for reservation in reservations:
        available = max(
            int(reservation.reserved_stock_quantity or 0)
            - int(reservation.consumed_stock_quantity or 0)
            - int(reservation.released_stock_quantity or 0),
            0,
        )
        picked = min(available, remaining)
        if picked <= 0:
            continue
        lot = db.get(InventoryLot, reservation.inventory_lot_id)
        sources.append(
            {
                "source_type": "finished",
                "reservation_id": reservation.id,
                "lot_id": lot.id if lot else None,
                "lot_number": lot.lot_number if lot else None,
                "component_snapshot_id": None,
                "quantity_to_pick_stock": picked,
                "quantity_to_pick_requirement": picked,
            }
        )
        remaining -= picked
        if remaining <= 0:
            break
    return sources


def _pick_item_location_plan(
    db: Session,
    *,
    item: DeliveryPickTaskItem,
    component_lines: list[dict],
    read_context: dict | None = None,
) -> tuple[list[dict], bool]:
    """Build a read-only loading plan from current inventory and production facts."""

    delivery_item = (
        read_context["delivery_items"].get(item.delivery_item_id)
        if read_context is not None
        else (
            db.get(DeliveryItem, item.delivery_item_id)
            if item.delivery_item_id is not None
            else None
        )
    )
    if delivery_item is not None and delivery_item.source_type == "unordered_finished":
        return _pick_unordered_location_plan(
            db,
            item=item,
            delivery_item=delivery_item,
        )
    order_item = (
        read_context["order_items"].get(item.order_item_id)
        if read_context is not None
        else db.get(OrderItem, item.order_item_id)
    )
    if order_item is None:
        return [], False
    composite_hint = (
        order_item.id in read_context["composite_order_item_ids"]
        if read_context is not None
        else None
    )
    if (
        read_context is not None
        and not composite_hint
        and order_item.id not in read_context["inventory_source_order_item_ids"]
    ):
        return [], False
    planned_quantity = max(int(item.original_quantity or 0), 0)
    raw_sources = _inventory_sources_for_order_item(
        db,
        order_item=order_item,
        planned_delivery_quantity=planned_quantity,
        delivery_item_id=item.delivery_item_id,
        dispatched=False,
        composite_hint=composite_hint,
    )
    if component_lines:
        raw_sources = [
            *_pick_parent_finished_sources(
                db,
                order_item=order_item,
                planned_quantity=planned_quantity,
            ),
            *raw_sources,
        ]
    component_by_snapshot = {
        int(row["component_snapshot_id"]): row
        for row in component_lines
        if row.get("component_snapshot_id") is not None
    }
    lines: list[dict] = []
    covered_by_component: dict[int, int] = {}
    finished_covered = 0

    for source in raw_sources:
        source_type = str(source.get("source_type") or "")
        # Semi-finished reservations are production inputs, not finished goods
        # that a driver should load.  Their eventual finished output is listed
        # below as a production-area direct pick.
        if source_type == "semi_finished":
            continue
        stock_quantity = max(int(source.get("quantity_to_pick_stock") or 0), 0)
        requirement_quantity = max(
            int(source.get("quantity_to_pick_requirement") or 0),
            0,
        )
        display_quantity = stock_quantity or requirement_quantity
        if display_quantity <= 0:
            continue
        component_snapshot_id = source.get("component_snapshot_id")
        component = (
            component_by_snapshot.get(int(component_snapshot_id))
            if component_snapshot_id is not None
            else None
        )
        if component_snapshot_id is not None:
            snapshot_id = int(component_snapshot_id)
            covered_by_component[snapshot_id] = (
                covered_by_component.get(snapshot_id, 0) + requirement_quantity
            )
        elif source_type == "finished":
            finished_covered += requirement_quantity
        location = _pick_source_location(db, source=source)
        is_direct = source_type == "component_direct"
        lines.append(
            {
                "pick_item_id": item.id,
                "order_item_id": item.order_item_id,
                "source_type": (
                    "production_direct" if is_direct else "finished_inventory"
                ),
                "reservation_id": source.get("reservation_id"),
                "lot_id": source.get("lot_id"),
                "lot_number": source.get("lot_number"),
                "component_snapshot_id": component_snapshot_id,
                "product_code": (
                    (component or {}).get("product_code")
                    or source.get("component_code")
                    or item.product_code_snapshot
                ),
                "product_name": (
                    (component or {}).get("product_name")
                    or source.get("component_name")
                    or item.product_name_snapshot
                ),
                "specification": (
                    (component or {}).get("specification")
                    or item.specification_snapshot
                ),
                "pick_quantity": display_quantity,
                "requirement_quantity": requirement_quantity,
                "unit": "个",
                "location_id": None if is_direct else location["location_id"],
                "location_code": None if is_direct else location["location_code"],
                "location_name": None if is_direct else location["location_name"],
                "warehouse_floor": (
                    None if is_direct else location["warehouse_floor"]
                ),
                "area_code": None if is_direct else location["area_code"],
                "location_sort_order": (
                    None if is_direct else location["location_sort_order"]
                ),
                "placement_status": (
                    None if is_direct else location["placement_status"]
                ),
                "pallet_id": None if is_direct else location["pallet_id"],
                "pallet_code": None if is_direct else location["pallet_code"],
                "location_operational": (
                    True if is_direct else location["location_operational"]
                ),
                "needs_relocation": (
                    False if is_direct else location["needs_relocation"]
                ),
                "requires_attention": bool(
                    not is_direct
                    and location["location_id"] is not None
                    and not location["location_operational"]
                ),
            }
        )

    if component_lines:
        parent_direct_quantity = max(planned_quantity - finished_covered, 0)
        if parent_direct_quantity > 0:
            lines.append(
                {
                    "pick_item_id": item.id,
                    "order_item_id": item.order_item_id,
                    "source_type": "production_direct",
                    "reservation_id": None,
                    "lot_id": None,
                    "lot_number": None,
                    "component_snapshot_id": None,
                    "product_code": item.product_code_snapshot,
                    "product_name": item.product_name_snapshot,
                    "specification": item.specification_snapshot,
                    "pick_quantity": parent_direct_quantity,
                    "requirement_quantity": parent_direct_quantity,
                    "unit": "个",
                    "location_id": None,
                    "location_code": None,
                    "location_name": None,
                    "warehouse_floor": None,
                    "area_code": None,
                    "location_sort_order": None,
                    "placement_status": None,
                    "pallet_id": None,
                    "pallet_code": None,
                    "location_operational": True,
                    "needs_relocation": False,
                    "requires_attention": False,
                }
            )
        for component in component_lines:
            snapshot_id = int(component["component_snapshot_id"])
            expected = max(
                int(component.get("planned_delivery_quantity") or 0),
                0,
            )
            missing = max(expected - covered_by_component.get(snapshot_id, 0), 0)
            if missing <= 0:
                continue
            lines.append(
                {
                    "pick_item_id": item.id,
                    "order_item_id": item.order_item_id,
                    "source_type": "unassigned",
                    "reservation_id": None,
                    "lot_id": None,
                    "lot_number": None,
                    "component_snapshot_id": snapshot_id,
                    "product_code": component.get("product_code"),
                    "product_name": component.get("product_name"),
                    "specification": component.get("specification"),
                    "pick_quantity": missing,
                    "requirement_quantity": missing,
                    "unit": "个",
                    "location_id": None,
                    "location_code": None,
                    "location_name": None,
                    "warehouse_floor": None,
                    "area_code": None,
                    "location_sort_order": None,
                    "placement_status": None,
                    "pallet_id": None,
                    "pallet_code": None,
                    "location_operational": False,
                    "needs_relocation": False,
                    "requires_attention": True,
                }
            )
    else:
        direct_quantity = max(planned_quantity - finished_covered, 0)
        if direct_quantity > 0:
            lines.append(
                {
                    "pick_item_id": item.id,
                    "order_item_id": item.order_item_id,
                    "source_type": "production_direct",
                    "reservation_id": None,
                    "lot_id": None,
                    "lot_number": None,
                    "component_snapshot_id": None,
                    "product_code": item.product_code_snapshot,
                    "product_name": item.product_name_snapshot,
                    "specification": item.specification_snapshot,
                    "pick_quantity": direct_quantity,
                    "requirement_quantity": direct_quantity,
                    "unit": "个",
                    "location_id": None,
                    "location_code": None,
                    "location_name": None,
                    "warehouse_floor": None,
                    "area_code": None,
                    "location_sort_order": None,
                    "placement_status": None,
                    "pallet_id": None,
                    "pallet_code": None,
                    "location_operational": True,
                    "needs_relocation": False,
                    "requires_attention": False,
                }
            )
    return lines, not any(line["requires_attention"] for line in lines)


def _pick_location_groups(db: Session, item_responses: list[dict]) -> list[dict]:
    groups: dict[tuple, dict] = {}
    for item in item_responses:
        for line in item.get("location_lines") or []:
            source_type = str(line.get("source_type") or "")
            if source_type == "production_direct":
                priority = 1
                group_key = ("production_direct",)
                label = "生产区直接拿货"
            elif source_type == "unassigned":
                priority = 3
                group_key = ("unassigned",)
                label = "未分配拿货位置（请核对）"
            else:
                priority = 2 if line.get("needs_relocation") else 0
                group_key = (
                    "finished_inventory",
                    line.get("location_id"),
                    line.get("pallet_id"),
                    bool(line.get("needs_relocation")),
                )
                floor = line.get("warehouse_floor")
                prefix = f"{floor}楼" if floor is not None else "仓库"
                area = line.get("area_code") or "未分区"
                location = line.get("location_code") or "未标库位"
                label = f"{prefix} · {area} · {location}"
                if line.get("needs_relocation"):
                    label += "（待归位）"
            group = groups.setdefault(
                group_key,
                {
                    "key": "|".join(str(part) for part in group_key),
                    "priority": priority,
                    "label": label,
                    "source_type": source_type,
                    "warehouse_floor": line.get("warehouse_floor"),
                    "area_code": line.get("area_code"),
                    "location_id": line.get("location_id"),
                    "location_code": line.get("location_code"),
                    "location_name": line.get("location_name"),
                    "location_sort_order": line.get("location_sort_order"),
                    "pallet_id": line.get("pallet_id"),
                    "pallet_code": line.get("pallet_code"),
                    "needs_relocation": bool(line.get("needs_relocation")),
                    "requires_attention": bool(line.get("requires_attention")),
                    "total_pick_quantity": 0,
                    "lines": [],
                },
            )
            group["total_pick_quantity"] += int(line.get("pick_quantity") or 0)
            group["requires_attention"] = bool(
                group["requires_attention"] or line.get("requires_attention")
            )
            group["lines"].append(line)

    def sort_key(group: dict) -> tuple:
        return (
            int(group["priority"]),
            int(group["warehouse_floor"] or 999),
            str(group["area_code"] or ""),
            int(group["location_sort_order"] or 0),
            str(group["location_code"] or ""),
            str(group["pallet_code"] or ""),
        )

    ordered = sorted(groups.values(), key=sort_key)
    location_ids = {
        int(group["location_id"])
        for group in ordered
        if group.get("location_id") is not None
        and not group.get("needs_relocation")
        and not group.get("requires_attention")
    }
    layouts = {
        row.location_id: row
        for row in db.scalars(
            select(Floor3LocationLayout).where(
                Floor3LocationLayout.location_id.in_(location_ids)
            )
        ).all()
    } if location_ids else {}
    for sequence, group in enumerate(ordered, start=1):
        group["recommended_sequence"] = sequence
        layout = layouts.get(group.get("location_id"))
        if layout is not None:
            group["map_status"] = "mapped"
            group["map_point"] = {
                "left_pct": float(layout.left_pct),
                "top_pct": float(layout.top_pct),
                "width_pct": float(layout.width_pct),
                "height_pct": float(layout.height_pct),
                "z_index": int(layout.z_index or 0),
            }
        else:
            group["map_status"] = (
                "text_only"
                if group.get("source_type") == "production_direct"
                or group.get("needs_relocation")
                or group.get("requires_attention")
                or group.get("location_id") is None
                else "unmapped"
            )
            group["map_point"] = None
    return ordered


def _pick_item_response(
    db: Session,
    item: DeliveryPickTaskItem,
    *,
    include_location_plan: bool = True,
    read_context: dict | None = None,
) -> dict:
    component_lines = _pick_item_component_lines(
        db,
        item,
        read_context=read_context,
    )
    location_lines, location_plan_complete = (
        _pick_item_location_plan(
            db,
            item=item,
            component_lines=component_lines,
            read_context=read_context,
        )
        if include_location_plan
        else ([], True)
    )
    return {
        "id": item.id,
        "delivery_item_id": item.delivery_item_id,
        "order_item_id": item.order_item_id,
        "original_quantity": item.original_quantity,
        "planned_quantity": item.original_quantity,
        "picked_quantity": item.picked_quantity,
        "status": item.status,
        "pick_status": item.status,
        "product_code": item.product_code_snapshot,
        "product_name": item.product_name_snapshot,
        "specification": item.specification_snapshot,
        "is_composite_bom": bool(component_lines),
        "component_lines": component_lines,
        "location_lines": location_lines,
        "location_plan_complete": location_plan_complete,
        "updated_at": utc_naive_to_api(item.updated_at) if item.updated_at else None,
    }


def _pick_task_response(
    db: Session,
    task: DeliveryPickTask,
    *,
    include_location_plan: bool = True,
) -> dict:
    task_items = list(task.items)
    read_context = _pick_task_read_context(db, task_items)
    item_responses = [
        _pick_item_response(
            db,
            item,
            include_location_plan=include_location_plan,
            read_context=read_context,
        )
        for item in task_items
    ]
    location_groups = (
        _pick_location_groups(db, item_responses) if include_location_plan else []
    )
    print_version_payload = {
        "task_id": task.id,
        "snapshot_version": task.snapshot_version,
        "items": [
            {
                "id": item.get("id"),
                "planned_quantity": item.get("planned_quantity"),
                "picked_quantity": item.get("picked_quantity"),
                "pick_status": item.get("pick_status"),
                "product_code": item.get("product_code"),
            }
            for item in item_responses
        ],
        "location_groups": [
            {
                "key": group.get("key"),
                "sequence": group.get("recommended_sequence"),
                "location_id": group.get("location_id"),
                "location_code": group.get("location_code"),
                "lines": [
                    {
                        "pick_item_id": line.get("pick_item_id"),
                        "source_type": line.get("source_type"),
                        "location_id": line.get("location_id"),
                        "pallet_id": line.get("pallet_id"),
                        "pick_quantity": line.get("pick_quantity"),
                    }
                    for line in group.get("lines") or []
                ],
            }
            for group in location_groups
        ],
    }
    print_version_digest = hashlib.sha256(
        json.dumps(
            print_version_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:10]
    exception_items = [
        {
            **item_response,
            "customer_name": task.customer.name if task.customer else None,
        }
        for item, item_response in zip(task_items, item_responses, strict=True)
        if item.status in {"partial", "no_stock"}
        or int(item.picked_quantity) > int(item.original_quantity)
    ]
    assigned_user = db.get(User, task.assigned_to) if task.assigned_to else None
    return {
        "id": task.id,
        "delivery_id": task.delivery_id,
        "customer_id": task.customer_id,
        "customer_name": task.customer.name if task.customer else None,
        "delivery_number": task.delivery.delivery_number if task.delivery else None,
        "planned_delivery_date": (
            task.delivery.delivery_date.isoformat()
            if task.delivery and task.delivery.delivery_date
            else None
        ),
        "status": task.status,
        "has_exception": bool(exception_items) or task.status == "exception",
        "exceptions": exception_items,
        "snapshot_version": task.snapshot_version,
        "print_version": f"{task.snapshot_version}-{print_version_digest}",
        "assigned_to": task.assigned_to,
        "assigned_to_name": (
            assigned_user.display_name or assigned_user.real_name or assigned_user.username
            if assigned_user is not None
            else None
        ),
        "assignment_required": task.assigned_to is None,
        "created_at": utc_naive_to_api(task.created_at) if task.created_at else None,
        "submitted_at": utc_naive_to_api(task.submitted_at) if task.submitted_at else None,
        "applied_at": utc_naive_to_api(task.applied_at) if task.applied_at else None,
        "dispatched_at": utc_naive_to_api(task.dispatched_at) if task.dispatched_at else None,
        "items": item_responses,
        "location_groups": location_groups,
        "location_plan_complete": (
            all(bool(item.get("location_plan_complete")) for item in item_responses)
            if include_location_plan
            else None
        ),
        "as_of": utc_naive_to_api(_utc_now()),
    }


def _delivery_pick_task(db: Session, delivery_id: int) -> DeliveryPickTask | None:
    return db.scalar(
        select(DeliveryPickTask)
        .where(DeliveryPickTask.delivery_id == delivery_id)
        .order_by(DeliveryPickTask.id.desc())
    )


def _discard_delivery_pick_task(
    db: Session,
    *,
    delivery_id: int,
    user: User,
    reason: str,
) -> None:
    task = _delivery_pick_task(db, delivery_id)
    if task is None:
        return
    _write_audit(
        db,
        user=user,
        action="PICK_TASK_INVALIDATED",
        resource="DeliveryPickTask",
        entity_id=task.id,
        details={"delivery_id": delivery_id, "reason": reason},
        description="送货草稿变更，已作废旧拿货快照",
    )
    db.delete(task)
    db.flush()


class ForceCloseRequest(BaseModel):
    reason: str | None = None

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


def _pending_query(
    *,
    customer_id: int | None = None,
    customer_ids: set[int] | None = None,
    inventory_keyword: str | None = None,
    customer_po_keyword: str | None = None,
    product_name_keyword: str | None = None,
    general_keyword: str | None = None,
):
    production_task_exists = exists(
        select(ProductionTask.id).where(
            ProductionTask.order_item_id == OrderItem.id,
        )
    )
    production_task_ready = exists(
        select(ProductionTask.id).where(
            ProductionTask.order_item_id == OrderItem.id,
            ProductionTask.status.in_(["completed", "not_required"]),
        )
    )
    active_finished_reserved = (
        select(
            func.coalesce(
                func.sum(
                    func.coalesce(
                        InventoryReservation.credited_requirement_quantity, 0
                    )
                    - InventoryReservation.released_requirement_quantity
                ),
                0,
            )
        )
        .where(
            InventoryReservation.order_item_id == OrderItem.id,
            InventoryReservation.reservation_type == "finished_order",
            InventoryReservation.status != "cancelled",
        )
        .correlate(OrderItem)
        .scalar_subquery()
    )
    active_semi_for_requirement = (
        select(
            func.coalesce(
                func.sum(
                    func.coalesce(
                        InventoryReservation.credited_requirement_quantity, 0
                    )
                    - InventoryReservation.released_requirement_quantity
                ),
                0,
            )
        )
        .where(
            InventoryReservation.semi_requirement_id
            == OrderItemSemiRequirement.id,
            InventoryReservation.reservation_type == "semi_order",
            InventoryReservation.status != "cancelled",
        )
        .correlate(OrderItemSemiRequirement)
        .scalar_subquery()
    )
    semi_requirement_count = (
        select(func.count(OrderItemSemiRequirement.id))
        .where(OrderItemSemiRequirement.order_item_id == OrderItem.id)
        .correlate(OrderItem)
        .scalar_subquery()
    )
    uncovered_semi_requirement_count = (
        select(func.count(OrderItemSemiRequirement.id))
        .where(
            OrderItemSemiRequirement.order_item_id == OrderItem.id,
            active_semi_for_requirement
            < OrderItemSemiRequirement.required_piece_quantity,
        )
        .correlate(OrderItem)
        .scalar_subquery()
    )
    semi_fully_covered = and_(
        semi_requirement_count > 0,
        uncovered_semi_requirement_count == 0,
    )
    received_telescoping_components = (
        select(func.count(RequisitionItem.id))
        .where(
            RequisitionItem.order_item_id == OrderItem.id,
            RequisitionItem.status == "已入库",
            or_(
                RequisitionItem.product_name_snapshot.like("%-盖"),
                RequisitionItem.product_name_snapshot.like("%-底"),
            ),
        )
        .correlate(OrderItem)
        .scalar_subquery()
    )
    query = (
        select(
            OrderItem.id.label("item_id"),
            OrderItem.id.label("order_item_id"),
            Order.id.label("order_id"),
            Order.order_number,
            Order.customer_po,
            Order.customer_id,
            Customer.name.label("customer_name"),
            Product.product_code,
            OrderItem.snapshot_product_name.label("product_name"),
            OrderItem.snapshot_spec.label("specification"),
            OrderItem.snapshot_material.label("material"),
            OrderItem.flute_type,
            OrderItem.snapshot_production_notes.label("production_notes"),
            OrderItem.quantity,
            OrderItem.delivered_quantity,
            (
                OrderItem.quantity - OrderItem.delivered_quantity
            ).label("remaining_quantity"),
            Order.delivery_date,
        )
        .join(Order, Order.id == OrderItem.order_id)
        .join(Customer, Customer.id == Order.customer_id)
        .join(Product, Product.id == OrderItem.product_id)
        .where(
            or_(
                production_task_ready,
                and_(
                    ~production_task_exists,
                    or_(
                        OrderItem.material_status == "received",
                        active_finished_reserved >= OrderItem.quantity,
                        semi_fully_covered,
                        received_telescoping_components > 0,
                    ),
                ),
            ),
            OrderItem.is_force_closed.is_(False),
        )
    )
    if customer_id is not None:
        query = query.where(Order.customer_id == customer_id)
    if customer_ids is not None:
        query = query.where(Order.customer_id.in_(customer_ids))
    inventory_keyword = (inventory_keyword or "").strip()
    customer_po_keyword = (customer_po_keyword or "").strip()
    product_name_keyword = (product_name_keyword or "").strip()
    general_keyword = (general_keyword or "").strip()

    if inventory_keyword:
        lowered = inventory_keyword.lower()
        fuzzy = f"%{inventory_keyword}%"
        query = query.where(
            or_(
                func.lower(Product.product_code) == lowered,
                Product.product_code.like(fuzzy),
            )
        )
    if customer_po_keyword:
        lowered = customer_po_keyword.lower()
        fuzzy = f"%{customer_po_keyword}%"
        query = query.where(
            or_(
                func.lower(Order.customer_po) == lowered,
                Order.customer_po.like(fuzzy),
            )
        )
    if product_name_keyword:
        query = query.where(
            OrderItem.snapshot_product_name.like(f"%{product_name_keyword}%")
        )

    rank_ordering = []
    if general_keyword:
        lowered = general_keyword.lower()
        prefix = f"{general_keyword}%"
        fuzzy = f"%{general_keyword}%"
        query = query.where(
            or_(
                func.lower(Product.product_code) == lowered,
                Product.product_code.like(prefix),
                Product.product_code.like(fuzzy),
                func.lower(Order.customer_po) == lowered,
                Order.customer_po.like(prefix),
                Order.customer_po.like(fuzzy),
                OrderItem.snapshot_product_name.like(fuzzy),
            )
        )
        rank_ordering.append(
            case(
                (func.lower(Product.product_code) == lowered, 0),
                (func.lower(Order.customer_po) == lowered, 1),
                (Product.product_code.like(prefix), 2),
                (Order.customer_po.like(prefix), 3),
                (Product.product_code.like(fuzzy), 4),
                (Order.customer_po.like(fuzzy), 5),
                else_=6,
            )
        )
    elif inventory_keyword:
        lowered = inventory_keyword.lower()
        prefix = f"{inventory_keyword}%"
        fuzzy = f"%{inventory_keyword}%"
        rank_ordering.append(
            case(
                (func.lower(Product.product_code) == lowered, 0),
                (Product.product_code.like(prefix), 1),
                (Product.product_code.like(fuzzy), 2),
                else_=3,
            )
        )
    elif customer_po_keyword:
        lowered = customer_po_keyword.lower()
        prefix = f"{customer_po_keyword}%"
        fuzzy = f"%{customer_po_keyword}%"
        rank_ordering.append(
            case(
                (func.lower(Order.customer_po) == lowered, 0),
                (Order.customer_po.like(prefix), 1),
                (Order.customer_po.like(fuzzy), 2),
                else_=3,
            )
        )

    query = query.order_by(
        *rank_ordering,
        OrderItem.material_received_at.desc(),
        OrderItem.created_at.desc(),
        OrderItem.id.desc(),
    )
    return query


def _delivery_or_404(db: Session, delivery_id: int) -> Delivery:
    delivery = db.get(Delivery, delivery_id)
    if delivery is None:
        raise HTTPException(status_code=404, detail="送货单不存在")
    return delivery


def _visible_customer_ids(user: User, db: Session) -> set[int] | None:
    if has_unrestricted_customer_access(user, db):
        return None
    return customer_scope_ids(user, db)


def _delivery_for_user(
    db: Session,
    delivery_id: int,
    user: User,
) -> Delivery:
    delivery = _delivery_or_404(db, delivery_id)
    require_customer_access(delivery.customer_id, user, db)
    return delivery


def _require_order_item_customer_access(
    db: Session,
    order_item_id: int,
    user: User,
) -> None:
    customer_id = db.scalar(
        select(Order.customer_id)
        .join(OrderItem, OrderItem.order_id == Order.id)
        .where(OrderItem.id == order_item_id)
    )
    if customer_id is not None:
        require_customer_access(customer_id, user, db)


def _delivery_location_metadata(
    db: Session,
    location: WarehouseLocation | None,
    *,
    finished: bool,
) -> dict:
    warehouse_types = (
        {"finished", "shared"}
        if finished
        else {"semi_finished", "shared"}
    )
    return {
        "warehouse_floor": location.warehouse_floor if location else None,
        "area_code": location.area_code if location else None,
        "location_operational": is_operational_location(
            db,
            location,
            warehouse_types=warehouse_types,
        ),
    }


def _composite_inventory_sources_for_order_item(
    db: Session,
    *,
    order_item: OrderItem,
    planned_delivery_quantity: int,
    delivery_item_id: int | None,
    dispatched: bool,
) -> list[dict]:
    """Expose N039 component pick sources without treating pieces as parent sets."""
    demands = effective_component_demands(db, order_item.id)
    demand_by_snapshot = {row.snapshot_id: row for row in demands}
    if not demand_by_snapshot:
        return []

    def source_payload(
        *,
        demand,
        source_type: str,
        quantity: int,
        reservation: InventoryReservation | None = None,
    ) -> dict:
        lot = db.get(InventoryLot, reservation.inventory_lot_id) if reservation else None
        location = (
            db.get(WarehouseLocation, lot.warehouse_location_id)
            if lot is not None
            else None
        )
        return {
            "source_type": source_type,
            "reservation_id": reservation.id if reservation else None,
            "lot_id": lot.id if lot else None,
            "lot_number": lot.lot_number if lot else None,
            "location_id": location.id if location else None,
            "location_code": location.location_code if location else None,
            "location_name": location.location_name if location else None,
            **_delivery_location_metadata(
                db,
                location,
                finished=source_type == "component_stock",
            ),
            "component_type": "bom_component",
            "component_snapshot_id": demand.snapshot_id,
            "component_code": demand.component_code,
            "component_name": demand.component_name,
            "quantity_per_set": demand.quantity_per_set,
            "quantity_to_pick_stock": quantity if reservation else 0,
            "quantity_to_pick_requirement": quantity,
        }

    items: list[dict] = []
    if dispatched and delivery_item_id is not None:
        stock_allocations = db.scalars(
            select(DeliveryInventoryAllocation)
            .join(
                InventoryReservation,
                InventoryReservation.id == DeliveryInventoryAllocation.reservation_id,
            )
            .where(
                DeliveryInventoryAllocation.delivery_item_id == delivery_item_id,
                InventoryReservation.sales_order_item_bom_component_id.is_not(None),
            )
            .order_by(DeliveryInventoryAllocation.id)
        ).all()
        for allocation in stock_allocations:
            reservation = db.get(InventoryReservation, allocation.reservation_id)
            if reservation is None:
                continue
            demand = demand_by_snapshot.get(
                reservation.sales_order_item_bom_component_id
            )
            quantity = max(
                int(allocation.consumed_stock_quantity or 0)
                - int(allocation.reversed_stock_quantity or 0),
                0,
            )
            if demand is not None and quantity > 0:
                items.append(
                    source_payload(
                        demand=demand,
                        source_type="component_stock",
                        quantity=quantity,
                        reservation=reservation,
                    )
                )
        direct_allocations = db.scalars(
            select(BomComponentDirectDeliveryAllocation)
            .where(
                BomComponentDirectDeliveryAllocation.delivery_item_id
                == delivery_item_id
            )
            .order_by(BomComponentDirectDeliveryAllocation.id)
        ).all()
        for allocation in direct_allocations:
            demand = demand_by_snapshot.get(
                allocation.sales_order_item_bom_component_id
            )
            quantity = max(
                int(allocation.consumed_quantity or 0)
                - int(allocation.reversed_quantity or 0),
                0,
            )
            if demand is not None and quantity > 0:
                items.append(
                    source_payload(
                        demand=demand,
                        source_type="component_direct",
                        quantity=quantity,
                    )
                )
        return items

    delivery_sets = max(int(planned_delivery_quantity or 0), 0)
    required_quantities = delivery_component_required_quantities(
        db,
        order_item_id=order_item.id,
        delivery_sets=delivery_sets,
    )
    for demand in demands:
        required_pieces = required_quantities.get(demand.snapshot_id, 0)
        if required_pieces <= 0:
            continue
        remaining = required_pieces
        reservations = db.scalars(
            select(InventoryReservation)
            .join(InventoryLot, InventoryLot.id == InventoryReservation.inventory_lot_id)
            .where(
                InventoryReservation.order_item_id == order_item.id,
                InventoryReservation.sales_order_item_bom_component_id
                == demand.snapshot_id,
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
        for reservation in reservations:
            available = max(
                int(reservation.reserved_stock_quantity or 0)
                - int(reservation.consumed_stock_quantity or 0)
                - int(reservation.released_stock_quantity or 0),
                0,
            )
            picked = min(available, remaining)
            if picked > 0:
                items.append(
                    source_payload(
                        demand=demand,
                        source_type="component_stock",
                        quantity=picked,
                        reservation=reservation,
                    )
                )
                remaining -= picked
            if remaining <= 0:
                break
        direct_quantity = min(
            max(component_availability(db, demand.snapshot_id).direct_quantity, 0),
            remaining,
        )
        if direct_quantity > 0:
            items.append(
                source_payload(
                    demand=demand,
                    source_type="component_direct",
                    quantity=direct_quantity,
                )
            )
    return items


def _delivery_component_lines(
    db: Session,
    *,
    order_item: OrderItem,
    planned_delivery_quantity: int,
    delivery_item_id: int | None = None,
    dispatched: bool = False,
) -> list[dict]:
    demands = effective_component_demands(db, order_item.id)
    if not demands:
        return []
    cumulative = delivered_component_quantities(db, order_item.id)
    document_quantities = (
        delivery_item_component_quantities(db, delivery_item_id)
        if dispatched and delivery_item_id is not None
        else delivery_component_required_quantities(
            db,
            order_item_id=order_item.id,
            delivery_sets=max(int(planned_delivery_quantity or 0), 0),
        )
    )
    return [
        {
            "line_type": "component",
            "component_snapshot_id": demand.snapshot_id,
            "component_product_id": demand.component_product_id,
            "product_code": demand.component_code,
            "product_name": demand.component_name,
            "specification": demand.specification,
            "unit": "PCS",
            "quantity_per_set": demand.quantity_per_set,
            "target_quantity": demand.required_piece_quantity,
            "delivered_quantity": cumulative.get(demand.snapshot_id, 0),
            "remaining_quantity": max(
                demand.required_piece_quantity
                - cumulative.get(demand.snapshot_id, 0),
                0,
            ),
            "planned_delivery_quantity": document_quantities.get(
                demand.snapshot_id,
                0,
            ),
            "pricing_included": False,
            "show_on_delivery": bool(demand.show_on_delivery),
            "pricing_note": "套内组件，不单独计价",
            "independent_return_receipt": False,
            "independent_statement": False,
        }
        for demand in demands
    ]


def _actual_goods_lines(
    *,
    order_item_id: int,
    product_code: str | None,
    product_name: str | None,
    specification: str | None,
    parent_quantity: int,
    component_lines: list[dict],
) -> list[dict]:
    lines = [
        {
            "line_type": "parent",
            "order_item_id": order_item_id,
            "component_snapshot_id": None,
            "product_code": product_code,
            "product_name": product_name,
            "specification": specification,
            "unit": "PCS",
            "quantity": max(int(parent_quantity or 0), 0),
            "pricing_included": True,
            "independent_return_receipt": True,
            "independent_statement": True,
        }
    ]
    lines.extend(
        {
            "line_type": "component",
            "order_item_id": order_item_id,
            "component_snapshot_id": component["component_snapshot_id"],
            "product_code": component["product_code"],
            "product_name": component["product_name"],
            "specification": component["specification"],
            "unit": component["unit"],
            "quantity": component["planned_delivery_quantity"],
            "pricing_included": False,
            "independent_return_receipt": False,
            "independent_statement": False,
        }
        for component in component_lines
        if int(component["planned_delivery_quantity"] or 0) > 0
        and bool(component.get("show_on_delivery", True))
    )
    return lines


def _delivery_kit_metadata(
    db: Session,
    order_item: OrderItem | None,
    *,
    planned_delivery_quantity: int | None = None,
    delivery_item_id: int | None = None,
    dispatched: bool = False,
) -> dict:
    if order_item is None or not is_composite_order_item(db, order_item.id):
        return {
            "is_composite_bom": False,
            "kit_availability": None,
            "available_sets": None,
            "missing_components": [],
            "component_lines": [],
        }
    availability = kit_availability(db, order_item.id)
    planned_quantity = (
        int(availability.get("available_sets") or 0)
        if planned_delivery_quantity is None
        else max(int(planned_delivery_quantity or 0), 0)
    )
    component_lines = _delivery_component_lines(
        db,
        order_item=order_item,
        planned_delivery_quantity=planned_quantity,
        delivery_item_id=delivery_item_id,
        dispatched=dispatched,
    )
    return {
        "is_composite_bom": True,
        "kit_availability": availability,
        "available_sets": int(availability.get("available_sets") or 0),
        "missing_components": availability.get("missing_components") or [],
        "component_lines": component_lines,
    }


def _inventory_sources_for_order_item(
    db: Session,
    *,
    order_item: OrderItem,
    planned_delivery_quantity: int,
    delivery_item_id: int | None = None,
    dispatched: bool = False,
    composite_hint: bool | None = None,
) -> list[dict]:
    if (
        composite_hint
        if composite_hint is not None
        else is_composite_order_item(db, order_item.id)
    ):
        return _composite_inventory_sources_for_order_item(
            db,
            order_item=order_item,
            planned_delivery_quantity=planned_delivery_quantity,
            delivery_item_id=delivery_item_id,
            dispatched=dispatched,
        )
    reservations = db.scalars(
        select(InventoryReservation)
        .join(InventoryLot, InventoryLot.id == InventoryReservation.inventory_lot_id)
        .where(
            InventoryReservation.order_item_id == order_item.id,
            InventoryReservation.reservation_type.in_(("finished_order", "semi_order")),
            InventoryReservation.status != "cancelled",
            func.coalesce(InventoryReservation.credited_requirement_quantity, 0)
            > InventoryReservation.released_requirement_quantity,
        )
        .order_by(
            case(
                (InventoryReservation.reservation_type == "finished_order", 0),
                else_=1,
            ),
            *inventory_fifo_order_columns(),
            InventoryReservation.id,
        )
    ).all()
    requirements = {
        row.id: row
        for row in db.scalars(
            select(OrderItemSemiRequirement).where(
                OrderItemSemiRequirement.order_item_id == order_item.id
            )
        ).all()
    }
    allocated_by_reservation: dict[int, tuple[int, int]] = {}
    if delivery_item_id is not None:
        allocations = db.scalars(
            select(DeliveryInventoryAllocation).where(
                DeliveryInventoryAllocation.delivery_item_id == delivery_item_id
            )
        ).all()
        for allocation in allocations:
            stock = (
                int(allocation.consumed_stock_quantity)
                - int(allocation.reversed_stock_quantity or 0)
            )
            credit = (
                int(allocation.credited_requirement_quantity)
                - int(allocation.reversed_requirement_quantity or 0)
            )
            previous_stock, previous_credit = allocated_by_reservation.get(
                allocation.reservation_id, (0, 0)
            )
            allocated_by_reservation[allocation.reservation_id] = (
                previous_stock + stock,
                previous_credit + credit,
            )

    planned_stock: dict[int, tuple[int, int]] = {}
    if not dispatched:
        target_delivered = min(
            int(order_item.delivered_quantity or 0)
            + max(int(planned_delivery_quantity), 0),
            int(order_item.quantity or 0),
        )
        finished_coverage = active_finished_reserved_qty(db, order_item.id)
        finished_target = min(target_delivered, finished_coverage)
        finished_current = sum(
            int(row.consumed_stock_quantity or 0)
            for row in reservations
            if row.reservation_type == "finished_order"
        )
        finished_need = max(finished_target - finished_current, 0)
        for reservation in reservations:
            available_stock = (
                int(reservation.reserved_stock_quantity)
                - int(reservation.consumed_stock_quantity or 0)
                - int(reservation.released_stock_quantity or 0)
            )
            if reservation.reservation_type == "finished_order":
                stock = min(available_stock, finished_need)
                planned_stock[reservation.id] = (stock, stock)
                finished_need -= stock

        semi_boxes = max(target_delivered - finished_coverage, 0)
        for requirement in requirements.values():
            coverage = active_semi_requirement_credited_quantity(db, requirement.id)
            target_pieces = min(
                semi_boxes * max(int(requirement.pieces_per_box or 1), 1),
                coverage,
            )
            current_pieces = sum(
                int(row.consumed_requirement_quantity or 0)
                for row in reservations
                if row.semi_requirement_id == requirement.id
            )
            for reservation in reservations:
                if reservation.semi_requirement_id != requirement.id:
                    continue
                available_stock = (
                    int(reservation.reserved_stock_quantity)
                    - int(reservation.consumed_stock_quantity or 0)
                    - int(reservation.released_stock_quantity or 0)
                )
                yield_factor = max(int(reservation.yield_factor or 1), 1)
                needed_pieces = max(target_pieces - current_pieces, 0)
                stock = min(
                    available_stock,
                    (needed_pieces + yield_factor - 1) // yield_factor,
                )
                credit = min(
                    max(
                        int(reservation.credited_requirement_quantity or 0)
                        - int(reservation.consumed_requirement_quantity or 0)
                        - int(reservation.released_requirement_quantity or 0),
                        0,
                    ),
                    stock * yield_factor,
                )
                planned_stock[reservation.id] = (stock, credit)
                current_pieces += credit

    items: list[dict] = []
    for reservation in reservations:
        lot = db.get(InventoryLot, reservation.inventory_lot_id)
        if lot is None:
            continue
        location = db.get(WarehouseLocation, lot.warehouse_location_id)
        requirement = requirements.get(reservation.semi_requirement_id)
        pick_stock, pick_credit = (
            allocated_by_reservation.get(reservation.id, (0, 0))
            if dispatched
            else planned_stock.get(reservation.id, (0, 0))
        )
        if pick_stock <= 0 and pick_credit <= 0:
            continue
        items.append(
            {
                "source_type": (
                    "finished"
                    if reservation.reservation_type == "finished_order"
                    else "semi_finished"
                ),
                "reservation_id": reservation.id,
                "lot_id": lot.id,
                "lot_number": lot.lot_number,
                "location_id": location.id if location else None,
                "location_code": location.location_code if location else None,
                "location_name": location.location_name if location else None,
                **_delivery_location_metadata(
                    db,
                    location,
                    finished=reservation.reservation_type == "finished_order",
                ),
                "component_type": (
                    requirement.component_type if requirement else "whole"
                ),
                "yield_factor": max(int(reservation.yield_factor or 1), 1),
                "reserved_stock_quantity": int(
                    reservation.reserved_stock_quantity or 0
                ),
                "remaining_reserved_stock_quantity": max(
                    int(reservation.reserved_stock_quantity or 0)
                    - int(reservation.consumed_stock_quantity or 0)
                    - int(reservation.released_stock_quantity or 0),
                    0,
                ),
                "covered_requirement_quantity": max(
                    int(reservation.credited_requirement_quantity or 0)
                    - int(reservation.released_requirement_quantity or 0),
                    0,
                ),
                "quantity_to_pick_stock": pick_stock,
                "quantity_to_pick_requirement": pick_credit,
            }
        )
    return items


def _delivery_item_rows(db: Session, delivery_ids: list[int]) -> list[dict]:
    if not delivery_ids:
        return []
    return [
        dict(row._mapping)
        for row in db.execute(
            select(
                DeliveryItem.id,
                DeliveryItem.delivery_id,
                DeliveryItem.source_type,
                DeliveryItem.order_item_id,
                DeliveryItem.product_id,
                DeliveryItem.delivered_quantity,
                DeliveryItem.unit_price_snapshot.label("unit_price"),
                DeliveryItem.price_source,
                DeliveryItem.ordered_quantity_snapshot,
                DeliveryItem.order_remaining_snapshot,
                DeliveryItem.over_delivery_quantity,
                DeliveryItem.over_delivery_confirmed_by,
                DeliveryItem.over_delivery_reason,
                DeliveryItem.remarks,
                Order.id.label("order_id"),
                Order.order_number,
                Order.customer_po,
                func.coalesce(
                    DeliveryItem.product_code_snapshot,
                    OrderItem.snapshot_product_code,
                ).label("product_code"),
                func.coalesce(
                    DeliveryItem.product_name_snapshot,
                    OrderItem.snapshot_product_name,
                ).label("product_name"),
                func.coalesce(
                    DeliveryItem.specification_snapshot,
                    OrderItem.snapshot_spec,
                ).label("specification"),
                DeliveryItem.unit_snapshot,
                OrderItem.snapshot_production_notes.label("production_notes"),
            )
            .outerjoin(OrderItem, OrderItem.id == DeliveryItem.order_item_id)
            .outerjoin(Order, Order.id == OrderItem.order_id)
            .outerjoin(
                Product,
                Product.id
                == func.coalesce(DeliveryItem.product_id, OrderItem.product_id),
            )
            .where(DeliveryItem.delivery_id.in_(delivery_ids))
            .order_by(DeliveryItem.delivery_id, DeliveryItem.id)
        ).all()
    ]


def _delivery_pick_task_summary(
    db: Session,
    task: DeliveryPickTask,
    *,
    customer_name: str | None,
    delivery_number: str | None,
    items: list[DeliveryPickTaskItem],
    assigned_user: User | None = None,
    assignee_preloaded: bool = False,
) -> dict:
    """List-view summary; full location planning remains on the detail routes."""

    exception_items = [
        {
            "id": item.id,
            "delivery_item_id": item.delivery_item_id,
            "order_item_id": item.order_item_id,
            "planned_quantity": item.original_quantity,
            "picked_quantity": item.picked_quantity,
            "status": item.status,
            "pick_status": item.status,
            "product_code": item.product_code_snapshot,
            "product_name": item.product_name_snapshot,
            "specification": item.specification_snapshot,
            "customer_name": customer_name,
        }
        for item in items
        if item.status in {"partial", "no_stock"}
        or int(item.picked_quantity) > int(item.original_quantity)
    ]
    if not assignee_preloaded and task.assigned_to:
        assigned_user = db.get(User, task.assigned_to)
    return {
        "id": task.id,
        "delivery_id": task.delivery_id,
        "customer_id": task.customer_id,
        "customer_name": customer_name,
        "delivery_number": delivery_number,
        "status": task.status,
        "has_exception": bool(exception_items) or task.status == "exception",
        "exceptions": exception_items,
        "snapshot_version": task.snapshot_version,
        "assigned_to": task.assigned_to,
        "assigned_to_name": (
            assigned_user.display_name or assigned_user.real_name or assigned_user.username
            if assigned_user is not None
            else None
        ),
        "assignment_required": task.assigned_to is None,
        "created_at": utc_naive_to_api(task.created_at) if task.created_at else None,
        "submitted_at": utc_naive_to_api(task.submitted_at) if task.submitted_at else None,
        "applied_at": utc_naive_to_api(task.applied_at) if task.applied_at else None,
        "dispatched_at": utc_naive_to_api(task.dispatched_at) if task.dispatched_at else None,
        "items": [],
        "location_groups": [],
        "location_plan_complete": None,
    }


def _delivery_pick_task_list_summaries(
    db: Session,
    tasks: list[DeliveryPickTask],
) -> list[dict]:
    """Build mobile list summaries with a fixed number of batch queries."""
    if not tasks:
        return []
    task_ids = [task.id for task in tasks]
    customer_ids = {task.customer_id for task in tasks}
    delivery_ids = {task.delivery_id for task in tasks}
    assigned_ids = {task.assigned_to for task in tasks if task.assigned_to is not None}
    customers = {
        row.id: row
        for row in db.scalars(select(Customer).where(Customer.id.in_(customer_ids))).all()
    }
    deliveries = {
        row.id: row
        for row in db.scalars(select(Delivery).where(Delivery.id.in_(delivery_ids))).all()
    }
    assignees = {
        row.id: row
        for row in db.scalars(select(User).where(User.id.in_(assigned_ids))).all()
    } if assigned_ids else {}
    items_by_task: dict[int, list[DeliveryPickTaskItem]] = {}
    for item in db.scalars(
        select(DeliveryPickTaskItem)
        .where(DeliveryPickTaskItem.task_id.in_(task_ids))
        .order_by(DeliveryPickTaskItem.task_id, DeliveryPickTaskItem.id)
    ).all():
        items_by_task.setdefault(int(item.task_id), []).append(item)
    return [
        _delivery_pick_task_summary(
            db,
            task,
            customer_name=(
                customers[task.customer_id].name
                if task.customer_id in customers
                else None
            ),
            delivery_number=(
                deliveries[task.delivery_id].delivery_number
                if task.delivery_id in deliveries
                else None
            ),
            items=items_by_task.get(task.id, []),
            assigned_user=assignees.get(task.assigned_to),
            assignee_preloaded=True,
        )
        for task in tasks
    ]


def _empty_delivery_kit_metadata() -> dict:
    return {
        "is_composite_bom": False,
        "kit_availability": None,
        "available_sets": None,
        "missing_components": [],
        "component_lines": [],
    }


def _delivery_list_positive_integer(value: object, *, field: str) -> int:
    try:
        decimal_value = Decimal(str(value))
    except (ArithmeticError, TypeError, ValueError) as error:
        raise CompositeBomWorkflowError(f"{field}必须为正整数") from error
    if decimal_value <= 0 or decimal_value != decimal_value.to_integral_value():
        raise CompositeBomWorkflowError(f"{field}必须为正整数")
    return int(decimal_value)


def _delivery_list_component_demands(
    snapshots: list[SalesOrderItemBomComponent],
    adjustments: dict[int, tuple[int, int]],
) -> dict[int, list[ComponentDemand]]:
    result: dict[int, list[ComponentDemand]] = {}
    for snapshot in snapshots:
        delta_sets, delta_pieces = adjustments.get(int(snapshot.id), (0, 0))
        quantity_per_set = _delivery_list_positive_integer(
            snapshot.quantity_per_set,
            field="每套组件数量",
        )
        effective_sets = int(snapshot.order_set_quantity) + delta_sets
        if effective_sets < 0:
            raise CompositeBomWorkflowError("组件调整后的有效套数不能小于0")
        required_piece_quantity = (
            _delivery_list_positive_integer(
                snapshot.required_piece_quantity,
                field="组件需求件数",
            )
            + delta_pieces
        )
        if required_piece_quantity <= 0:
            raise CompositeBomWorkflowError("组件调整后的需求件数必须大于0")
        demand = ComponentDemand(
            snapshot_id=int(snapshot.id),
            order_item_id=int(snapshot.sales_order_item_id),
            component_product_id=int(snapshot.component_product_id),
            component_code=snapshot.snapshot_component_product_code,
            component_name=snapshot.snapshot_component_product_name,
            specification=snapshot.snapshot_component_spec,
            quantity_per_set=quantity_per_set,
            is_required=bool(snapshot.is_required),
            effective_sets=effective_sets,
            required_piece_quantity=required_piece_quantity,
            show_on_delivery=bool(
                getattr(snapshot, "show_on_delivery", True)
            ),
        )
        result.setdefault(demand.order_item_id, []).append(demand)
    return result


def _delivery_list_location_operational(
    context: dict,
    location: WarehouseLocation | None,
    *,
    finished: bool,
) -> bool:
    if location is None or not location.is_active:
        return False
    if (location.placement_status or "placed") != "placed":
        return False
    accepted_types = {"finished", "shared"} if finished else {"semi_finished", "shared"}
    if location.warehouse_type not in accepted_types:
        return False
    if not context["has_space_ledger"]:
        return True
    if location.warehouse_floor is None or not str(location.area_code or "").strip():
        return False
    floor = context["floors_by_number"].get(int(location.warehouse_floor))
    if floor is None or floor.construction_status != "enabled":
        return False
    area = context["areas_by_floor_code"].get(
        (int(floor.id), str(location.area_code).strip().upper())
    )
    return bool(area is not None and area.construction_status == "enabled")


def _delivery_list_location_payload(
    context: dict,
    reservation: InventoryReservation | None,
    *,
    finished: bool,
) -> tuple[InventoryLot | None, WarehouseLocation | None, dict]:
    lot = (
        context["lots"].get(int(reservation.inventory_lot_id))
        if reservation is not None
        else None
    )
    location = (
        context["locations"].get(int(lot.warehouse_location_id))
        if lot is not None
        else None
    )
    return lot, location, {
        "warehouse_floor": location.warehouse_floor if location else None,
        "area_code": location.area_code if location else None,
        "location_operational": _delivery_list_location_operational(
            context,
            location,
            finished=finished,
        ),
    }


def _delivery_list_component_required_quantities(
    context: dict,
    *,
    order_item: OrderItem,
    delivery_sets: int,
) -> dict[int, int]:
    sets = int(delivery_sets)
    if sets < 0:
        raise CompositeBomWorkflowError("送货套数不能小于0")
    delivered_after = max(int(order_item.delivered_quantity or 0), 0) + sets
    result: dict[int, int] = {}
    for demand in context["component_demands_by_order_item"].get(order_item.id, []):
        consumed = context["component_delivered_by_snapshot"].get(demand.snapshot_id, 0)
        target_after_dispatch = min(
            delivered_after * demand.quantity_per_set,
            demand.required_piece_quantity,
        )
        result[demand.snapshot_id] = max(target_after_dispatch - consumed, 0)
    return result


def _delivery_list_kit_metadata(
    context: dict,
    *,
    order_item: OrderItem,
    planned_delivery_quantity: int,
    delivery_item_id: int,
    dispatched: bool,
) -> dict:
    demands = context["component_demands_by_order_item"].get(order_item.id, [])
    if not demands:
        return _empty_delivery_kit_metadata()

    delivered_sets = int(order_item.delivered_quantity or 0)
    effective_sets = max(int(order_item.quantity or 0), 0)
    available_sets = max(effective_sets - delivered_sets, 0)
    missing_components: list[dict] = []
    components: list[dict] = []
    for demand in demands:
        stock_quantity = sum(
            max(
                int(row.reserved_stock_quantity or 0)
                - int(row.consumed_stock_quantity or 0)
                - int(row.released_stock_quantity or 0),
                0,
            )
            for row in context["reservations_by_snapshot"].get(demand.snapshot_id, [])
            if row.reservation_type == "finished_order"
            and row.status in ACTIVE_RESERVATION_STATUSES
        )
        direct_quantity = context["component_direct_available_by_snapshot"].get(
            demand.snapshot_id,
            0,
        )
        component_available = stock_quantity + direct_quantity
        consumed = context["component_delivered_by_snapshot"].get(demand.snapshot_id, 0)
        remaining_target = max(demand.required_piece_quantity - consumed, 0)
        shortage = max(remaining_target - component_available, 0)
        components.append(
            {
                "snapshot_id": demand.snapshot_id,
                "component_code": demand.component_code,
                "component_name": demand.component_name,
                "quantity_per_set": demand.quantity_per_set,
                "is_required": demand.is_required,
                "stock_quantity": stock_quantity,
                "direct_quantity": direct_quantity,
                "available_quantity": component_available,
                "required_piece_quantity": demand.required_piece_quantity,
                "target_quantity": demand.required_piece_quantity,
                "delivered_quantity": consumed,
                "remaining_quantity": remaining_target,
                "delivered_piece_quantity": consumed,
                "remaining_required_piece_quantity": remaining_target,
            }
        )
        if not demand.is_required:
            continue
        total_coverable = consumed + component_available
        if total_coverable < demand.required_piece_quantity:
            component_sets = max(
                total_coverable // demand.quantity_per_set - delivered_sets,
                0,
            )
            available_sets = min(available_sets, component_sets)
            missing_components.append(
                {
                    "snapshot_id": demand.snapshot_id,
                    "component_code": demand.component_code,
                    "component_name": demand.component_name,
                    "required_quantity": remaining_target,
                    "available_quantity": component_available,
                    "shortage_quantity": shortage,
                }
            )
    availability = {
        "applicable": True,
        "effective_sets": effective_sets,
        "delivered_sets": delivered_sets,
        "available_sets": available_sets,
        "components": components,
        "missing_components": missing_components,
    }

    document_quantities = (
        context["component_quantities_by_delivery_item"].get(delivery_item_id, {})
        if dispatched
        else _delivery_list_component_required_quantities(
            context,
            order_item=order_item,
            delivery_sets=max(int(planned_delivery_quantity or 0), 0),
        )
    )
    component_lines = [
        {
            "line_type": "component",
            "component_snapshot_id": demand.snapshot_id,
            "component_product_id": demand.component_product_id,
            "product_code": demand.component_code,
            "product_name": demand.component_name,
            "specification": demand.specification,
            "unit": "PCS",
            "quantity_per_set": demand.quantity_per_set,
            "target_quantity": demand.required_piece_quantity,
            "delivered_quantity": context["component_delivered_by_snapshot"].get(
                demand.snapshot_id,
                0,
            ),
            "remaining_quantity": max(
                demand.required_piece_quantity
                - context["component_delivered_by_snapshot"].get(demand.snapshot_id, 0),
                0,
            ),
            "planned_delivery_quantity": document_quantities.get(demand.snapshot_id, 0),
            "pricing_included": False,
            "show_on_delivery": bool(demand.show_on_delivery),
            "pricing_note": "套内组件，不单独计价",
            "independent_return_receipt": False,
            "independent_statement": False,
        }
        for demand in demands
    ]
    return {
        "is_composite_bom": True,
        "kit_availability": availability,
        "available_sets": available_sets,
        "missing_components": missing_components,
        "component_lines": component_lines,
    }


def _delivery_list_composite_inventory_sources(
    context: dict,
    *,
    order_item: OrderItem,
    planned_delivery_quantity: int,
    delivery_item_id: int,
    dispatched: bool,
) -> list[dict]:
    demands = context["component_demands_by_order_item"].get(order_item.id, [])
    demand_by_snapshot = {row.snapshot_id: row for row in demands}
    if not demands:
        return []

    def source_payload(
        *,
        demand: ComponentDemand,
        source_type: str,
        quantity: int,
        reservation: InventoryReservation | None = None,
    ) -> dict:
        lot, location, location_metadata = _delivery_list_location_payload(
            context,
            reservation,
            finished=source_type == "component_stock",
        )
        return {
            "source_type": source_type,
            "reservation_id": reservation.id if reservation else None,
            "lot_id": lot.id if lot else None,
            "lot_number": lot.lot_number if lot else None,
            "location_id": location.id if location else None,
            "location_code": location.location_code if location else None,
            "location_name": location.location_name if location else None,
            **location_metadata,
            "component_type": "bom_component",
            "component_snapshot_id": demand.snapshot_id,
            "component_code": demand.component_code,
            "component_name": demand.component_name,
            "quantity_per_set": demand.quantity_per_set,
            "quantity_to_pick_stock": quantity if reservation else 0,
            "quantity_to_pick_requirement": quantity,
        }

    items: list[dict] = []
    if dispatched:
        for allocation in context["delivery_allocations_by_delivery_item"].get(
            delivery_item_id,
            [],
        ):
            reservation = context["reservations"].get(int(allocation.reservation_id))
            if reservation is None:
                continue
            demand = demand_by_snapshot.get(reservation.sales_order_item_bom_component_id)
            quantity = max(
                int(allocation.consumed_stock_quantity or 0)
                - int(allocation.reversed_stock_quantity or 0),
                0,
            )
            if demand is not None and quantity > 0:
                items.append(
                    source_payload(
                        demand=demand,
                        source_type="component_stock",
                        quantity=quantity,
                        reservation=reservation,
                    )
                )
        for allocation in context["direct_allocations_by_delivery_item"].get(
            delivery_item_id,
            [],
        ):
            demand = demand_by_snapshot.get(
                int(allocation.sales_order_item_bom_component_id)
            )
            quantity = max(
                int(allocation.consumed_quantity or 0)
                - int(allocation.reversed_quantity or 0),
                0,
            )
            if demand is not None and quantity > 0:
                items.append(
                    source_payload(
                        demand=demand,
                        source_type="component_direct",
                        quantity=quantity,
                    )
                )
        return items

    required_quantities = _delivery_list_component_required_quantities(
        context,
        order_item=order_item,
        delivery_sets=max(int(planned_delivery_quantity or 0), 0),
    )
    for demand in demands:
        remaining = required_quantities.get(demand.snapshot_id, 0)
        if remaining <= 0:
            continue
        reservations = [
            row
            for row in context["reservations_by_snapshot"].get(demand.snapshot_id, [])
            if row.order_item_id == order_item.id
            and row.status != "cancelled"
            and int(row.reserved_stock_quantity or 0)
            > int(row.consumed_stock_quantity or 0)
            + int(row.released_stock_quantity or 0)
            and int(row.inventory_lot_id) in context["lots"]
        ]
        reservations.sort(
            key=lambda row: (
                *inventory_fifo_sort_key(context["lots"][int(row.inventory_lot_id)]),
                int(row.id),
            )
        )
        for reservation in reservations:
            available = max(
                int(reservation.reserved_stock_quantity or 0)
                - int(reservation.consumed_stock_quantity or 0)
                - int(reservation.released_stock_quantity or 0),
                0,
            )
            picked = min(available, remaining)
            if picked > 0:
                items.append(
                    source_payload(
                        demand=demand,
                        source_type="component_stock",
                        quantity=picked,
                        reservation=reservation,
                    )
                )
                remaining -= picked
            if remaining <= 0:
                break
        direct_quantity = min(
            max(
                context["component_direct_available_by_snapshot"].get(
                    demand.snapshot_id,
                    0,
                ),
                0,
            ),
            remaining,
        )
        if direct_quantity > 0:
            items.append(
                source_payload(
                    demand=demand,
                    source_type="component_direct",
                    quantity=direct_quantity,
                )
            )
    return items


def _delivery_list_standard_inventory_sources(
    context: dict,
    *,
    order_item: OrderItem,
    planned_delivery_quantity: int,
    delivery_item_id: int,
    dispatched: bool,
) -> list[dict]:
    reservations = [
        row
        for row in context["reservations_by_order_item"].get(order_item.id, [])
        if row.reservation_type in {"finished_order", "semi_order"}
        and row.status != "cancelled"
        and int(row.credited_requirement_quantity or 0)
        > int(row.released_requirement_quantity or 0)
        and int(row.inventory_lot_id) in context["lots"]
    ]
    reservations.sort(
        key=lambda row: (
            0 if row.reservation_type == "finished_order" else 1,
            *inventory_fifo_sort_key(context["lots"][int(row.inventory_lot_id)]),
            int(row.id),
        )
    )
    requirements = {
        int(row.id): row
        for row in context["requirements_by_order_item"].get(order_item.id, [])
    }
    allocated_by_reservation: dict[int, tuple[int, int]] = {}
    for allocation in context["delivery_allocations_by_delivery_item"].get(
        delivery_item_id,
        [],
    ):
        stock = int(allocation.consumed_stock_quantity or 0) - int(
            allocation.reversed_stock_quantity or 0
        )
        credit = int(allocation.credited_requirement_quantity or 0) - int(
            allocation.reversed_requirement_quantity or 0
        )
        previous_stock, previous_credit = allocated_by_reservation.get(
            int(allocation.reservation_id),
            (0, 0),
        )
        allocated_by_reservation[int(allocation.reservation_id)] = (
            previous_stock + stock,
            previous_credit + credit,
        )

    planned_stock: dict[int, tuple[int, int]] = {}
    if not dispatched:
        target_delivered = min(
            int(order_item.delivered_quantity or 0)
            + max(int(planned_delivery_quantity or 0), 0),
            int(order_item.quantity or 0),
        )
        finished_coverage = context["active_finished_by_order_item"].get(
            order_item.id,
            0,
        )
        finished_target = min(target_delivered, finished_coverage)
        finished_current = sum(
            int(row.consumed_stock_quantity or 0)
            for row in reservations
            if row.reservation_type == "finished_order"
        )
        finished_need = max(finished_target - finished_current, 0)
        for reservation in reservations:
            available_stock = (
                int(reservation.reserved_stock_quantity or 0)
                - int(reservation.consumed_stock_quantity or 0)
                - int(reservation.released_stock_quantity or 0)
            )
            if reservation.reservation_type == "finished_order":
                stock = min(available_stock, finished_need)
                planned_stock[int(reservation.id)] = (stock, stock)
                finished_need -= stock

        semi_boxes = max(target_delivered - finished_coverage, 0)
        for requirement in requirements.values():
            coverage = context["active_semi_by_requirement"].get(requirement.id, 0)
            target_pieces = min(
                semi_boxes * max(int(requirement.pieces_per_box or 1), 1),
                coverage,
            )
            current_pieces = sum(
                int(row.consumed_requirement_quantity or 0)
                for row in reservations
                if row.semi_requirement_id == requirement.id
            )
            for reservation in reservations:
                if reservation.semi_requirement_id != requirement.id:
                    continue
                available_stock = (
                    int(reservation.reserved_stock_quantity or 0)
                    - int(reservation.consumed_stock_quantity or 0)
                    - int(reservation.released_stock_quantity or 0)
                )
                yield_factor = max(int(reservation.yield_factor or 1), 1)
                needed_pieces = max(target_pieces - current_pieces, 0)
                stock = min(
                    available_stock,
                    (needed_pieces + yield_factor - 1) // yield_factor,
                )
                credit = min(
                    max(
                        int(reservation.credited_requirement_quantity or 0)
                        - int(reservation.consumed_requirement_quantity or 0)
                        - int(reservation.released_requirement_quantity or 0),
                        0,
                    ),
                    stock * yield_factor,
                )
                planned_stock[int(reservation.id)] = (stock, credit)
                current_pieces += credit

    items: list[dict] = []
    for reservation in reservations:
        lot, location, location_metadata = _delivery_list_location_payload(
            context,
            reservation,
            finished=reservation.reservation_type == "finished_order",
        )
        if lot is None:
            continue
        requirement = requirements.get(reservation.semi_requirement_id)
        pick_stock, pick_credit = (
            allocated_by_reservation.get(int(reservation.id), (0, 0))
            if dispatched
            else planned_stock.get(int(reservation.id), (0, 0))
        )
        if pick_stock <= 0 and pick_credit <= 0:
            continue
        items.append(
            {
                "source_type": (
                    "finished"
                    if reservation.reservation_type == "finished_order"
                    else "semi_finished"
                ),
                "reservation_id": reservation.id,
                "lot_id": lot.id,
                "lot_number": lot.lot_number,
                "location_id": location.id if location else None,
                "location_code": location.location_code if location else None,
                "location_name": location.location_name if location else None,
                **location_metadata,
                "component_type": requirement.component_type if requirement else "whole",
                "yield_factor": max(int(reservation.yield_factor or 1), 1),
                "reserved_stock_quantity": int(
                    reservation.reserved_stock_quantity or 0
                ),
                "remaining_reserved_stock_quantity": max(
                    int(reservation.reserved_stock_quantity or 0)
                    - int(reservation.consumed_stock_quantity or 0)
                    - int(reservation.released_stock_quantity or 0),
                    0,
                ),
                "covered_requirement_quantity": max(
                    int(reservation.credited_requirement_quantity or 0)
                    - int(reservation.released_requirement_quantity or 0),
                    0,
                ),
                "quantity_to_pick_stock": pick_stock,
                "quantity_to_pick_requirement": pick_credit,
            }
        )
    return items


def _unordered_finished_allocation_payload(row: UnorderedFinishedDeliveryAllocation) -> dict:
    return {
        "id": row.id,
        "inventory_lot_id": row.inventory_lot_id,
        "lot_number": row.lot_number_snapshot,
        "location_id": row.warehouse_location_id_snapshot,
        "location_code": row.warehouse_location_code_snapshot,
        "pallet_code": row.pallet_code_snapshot,
        "quantity": int(row.planned_quantity or 0),
        "planned_quantity": int(row.planned_quantity or 0),
        "consumed_quantity": int(row.consumed_quantity or 0),
        "restored_quantity": int(row.restored_quantity or 0),
        "status": row.status,
    }


def _delivery_list_page_context(db: Session, delivery_ids: list[int]) -> dict:
    """Batch data used only by the desktop delivery list page."""

    deliveries = {
        delivery.id: delivery
        for delivery in db.scalars(
            select(Delivery).where(Delivery.id.in_(delivery_ids))
        ).all()
    } if delivery_ids else {}
    customer_ids = {delivery.customer_id for delivery in deliveries.values()}
    customers = {
        customer.id: customer
        for customer in db.scalars(select(Customer).where(Customer.id.in_(customer_ids))).all()
    } if customer_ids else {}
    receipts = {
        receipt.delivery_id: receipt
        for receipt in db.scalars(
            select(ReturnReceipt).where(ReturnReceipt.delivery_id.in_(delivery_ids))
        ).all()
    } if delivery_ids else {}
    item_rows = _delivery_item_rows(db, delivery_ids)
    rows_by_delivery: dict[int, list[dict]] = {}
    for row in item_rows:
        rows_by_delivery.setdefault(int(row["delivery_id"]), []).append(row)
    order_ids = {
        int(row["order_id"])
        for row in item_rows
        if row.get("order_id") is not None
    }
    orders = {
        order.id: order
        for order in db.scalars(select(Order).where(Order.id.in_(order_ids))).all()
    } if order_ids else {}
    order_item_ids = {
        int(row["order_item_id"])
        for row in item_rows
        if row.get("order_item_id") is not None
    }
    order_items = {
        item.id: item
        for item in db.scalars(select(OrderItem).where(OrderItem.id.in_(order_item_ids))).all()
    } if order_item_ids else {}
    snapshots = list(
        db.scalars(
            select(SalesOrderItemBomComponent)
            .where(SalesOrderItemBomComponent.sales_order_item_id.in_(order_item_ids))
            .order_by(
                SalesOrderItemBomComponent.sales_order_item_id,
                SalesOrderItemBomComponent.display_order,
                SalesOrderItemBomComponent.id,
            )
        ).all()
    ) if order_item_ids else []
    composite_item_ids = {
        int(row.sales_order_item_id) for row in snapshots
    }
    snapshot_ids = [int(row.id) for row in snapshots]
    adjustments = {
        int(snapshot_id): (int(delta_sets or 0), int(delta_pieces or 0))
        for snapshot_id, delta_sets, delta_pieces in db.execute(
            select(
                SalesOrderItemBomDemandAdjustment.sales_order_item_bom_component_id,
                func.coalesce(
                    func.sum(
                        SalesOrderItemBomDemandAdjustment.delta_order_set_quantity
                    ),
                    0,
                ),
                func.coalesce(
                    func.sum(
                        SalesOrderItemBomDemandAdjustment.delta_required_piece_quantity
                    ),
                    0,
                ),
            )
            .where(
                SalesOrderItemBomDemandAdjustment.sales_order_item_bom_component_id.in_(
                    snapshot_ids
                )
            )
            .group_by(
                SalesOrderItemBomDemandAdjustment.sales_order_item_bom_component_id
            )
        ).all()
    } if snapshot_ids else {}
    component_demands_by_order_item = _delivery_list_component_demands(
        snapshots,
        adjustments,
    )
    requirements = list(
        db.scalars(
            select(OrderItemSemiRequirement)
            .where(OrderItemSemiRequirement.order_item_id.in_(order_item_ids))
            .order_by(OrderItemSemiRequirement.order_item_id, OrderItemSemiRequirement.id)
        ).all()
    ) if order_item_ids else []
    requirements_by_order_item: dict[int, list[OrderItemSemiRequirement]] = {}
    for requirement in requirements:
        requirements_by_order_item.setdefault(int(requirement.order_item_id), []).append(
            requirement
        )
    requirement_ids = [int(row.id) for row in requirements]

    delivery_item_ids = [int(row["id"]) for row in item_rows]
    delivery_allocations = list(
        db.scalars(
            select(DeliveryInventoryAllocation)
            .where(DeliveryInventoryAllocation.delivery_item_id.in_(delivery_item_ids))
            .order_by(DeliveryInventoryAllocation.id)
        ).all()
    ) if delivery_item_ids else []
    delivery_allocations_by_delivery_item: dict[int, list[DeliveryInventoryAllocation]] = {}
    for allocation in delivery_allocations:
        delivery_allocations_by_delivery_item.setdefault(
            int(allocation.delivery_item_id),
            [],
        ).append(allocation)
    allocation_reservation_ids = {
        int(row.reservation_id) for row in delivery_allocations
    }
    direct_allocations = list(
        db.scalars(
            select(BomComponentDirectDeliveryAllocation)
            .where(
                BomComponentDirectDeliveryAllocation.delivery_item_id.in_(
                    delivery_item_ids
                )
            )
            .order_by(BomComponentDirectDeliveryAllocation.id)
        ).all()
    ) if delivery_item_ids and snapshot_ids else []
    direct_allocations_by_delivery_item: dict[
        int, list[BomComponentDirectDeliveryAllocation]
    ] = {}
    for allocation in direct_allocations:
        direct_allocations_by_delivery_item.setdefault(
            int(allocation.delivery_item_id),
            [],
        ).append(allocation)
    unordered_allocations = list(
        db.scalars(
            select(UnorderedFinishedDeliveryAllocation)
            .where(
                UnorderedFinishedDeliveryAllocation.delivery_item_id.in_(
                    delivery_item_ids
                )
            )
            .order_by(
                UnorderedFinishedDeliveryAllocation.delivery_item_id,
                UnorderedFinishedDeliveryAllocation.id,
            )
        ).all()
    ) if delivery_item_ids else []
    unordered_allocations_by_delivery_item: dict[
        int, list[UnorderedFinishedDeliveryAllocation]
    ] = {}
    for allocation in unordered_allocations:
        unordered_allocations_by_delivery_item.setdefault(
            int(allocation.delivery_item_id),
            [],
        ).append(allocation)

    reservation_conditions = []
    if order_item_ids:
        reservation_conditions.append(
            InventoryReservation.order_item_id.in_(order_item_ids)
        )
    if requirement_ids:
        reservation_conditions.append(
            InventoryReservation.semi_requirement_id.in_(requirement_ids)
        )
    if snapshot_ids:
        reservation_conditions.append(
            InventoryReservation.sales_order_item_bom_component_id.in_(snapshot_ids)
        )
    if allocation_reservation_ids:
        reservation_conditions.append(
            InventoryReservation.id.in_(allocation_reservation_ids)
        )
    reservation_rows = list(
        db.scalars(
            select(InventoryReservation)
            .where(or_(*reservation_conditions))
            .order_by(InventoryReservation.id)
        ).all()
    ) if reservation_conditions else []
    reservations = {int(row.id): row for row in reservation_rows}
    reservations_by_order_item: dict[int, list[InventoryReservation]] = {}
    reservations_by_requirement: dict[int, list[InventoryReservation]] = {}
    reservations_by_snapshot: dict[int, list[InventoryReservation]] = {}
    for reservation in reservation_rows:
        if reservation.order_item_id is not None:
            reservations_by_order_item.setdefault(
                int(reservation.order_item_id),
                [],
            ).append(reservation)
        if reservation.semi_requirement_id is not None:
            reservations_by_requirement.setdefault(
                int(reservation.semi_requirement_id),
                [],
            ).append(reservation)
        if reservation.sales_order_item_bom_component_id is not None:
            reservations_by_snapshot.setdefault(
                int(reservation.sales_order_item_bom_component_id),
                [],
            ).append(reservation)
    reserved_item_ids = {
        int(row.order_item_id)
        for row in reservation_rows
        if row.order_item_id is not None and row.status != "cancelled"
    }
    semi_requirement_item_ids = set(requirements_by_order_item)
    safe_empty_inventory_order_item_ids = (
        order_item_ids - composite_item_ids - reserved_item_ids - semi_requirement_item_ids
    )

    lot_ids = {int(row.inventory_lot_id) for row in reservation_rows}
    lots = {
        int(row.id): row
        for row in db.scalars(
            select(InventoryLot).where(InventoryLot.id.in_(lot_ids))
        ).all()
    } if lot_ids else {}
    location_ids = {int(row.warehouse_location_id) for row in lots.values()}
    locations = {
        int(row.id): row
        for row in db.scalars(
            select(WarehouseLocation).where(WarehouseLocation.id.in_(location_ids))
        ).all()
    } if location_ids else {}
    floors = list(db.scalars(select(WarehouseFloor)).all()) if location_ids else []
    floors_by_number = {int(row.floor_number): row for row in floors}
    floor_ids = [int(row.id) for row in floors]
    areas = list(
        db.scalars(
            select(WarehouseArea).where(WarehouseArea.floor_id.in_(floor_ids))
        ).all()
    ) if floor_ids else []
    areas_by_floor_code = {
        (int(row.floor_id), str(row.area_code).strip().upper()): row
        for row in areas
    }

    active_finished_by_order_item: dict[int, int] = {}
    active_semi_by_requirement: dict[int, int] = {}
    for reservation in reservation_rows:
        if (
            reservation.order_item_id is not None
            and reservation.reservation_type == "finished_order"
            and reservation.sales_order_item_bom_component_id is None
            and reservation.status != "cancelled"
        ):
            item_id = int(reservation.order_item_id)
            active_finished_by_order_item[item_id] = (
                active_finished_by_order_item.get(item_id, 0)
                + max(
                    int(reservation.credited_requirement_quantity or 0)
                    - int(reservation.released_requirement_quantity or 0),
                    0,
                )
            )
        if (
            reservation.semi_requirement_id is not None
            and reservation.reservation_type == "semi_order"
            and reservation.status != "cancelled"
        ):
            requirement_id = int(reservation.semi_requirement_id)
            active_semi_by_requirement[requirement_id] = (
                active_semi_by_requirement.get(requirement_id, 0)
                + max(
                    int(reservation.credited_requirement_quantity or 0)
                    - int(reservation.released_requirement_quantity or 0),
                    0,
                )
            )

    component_direct_available_by_snapshot: dict[int, int] = {}
    component_delivered_direct = {}
    component_delivered_stock = {}
    if snapshot_ids:
        allocated_direct = func.coalesce(
            func.sum(
                BomComponentDirectDeliveryAllocation.consumed_quantity
                - BomComponentDirectDeliveryAllocation.reversed_quantity
            ),
            0,
        )
        for snapshot_id, _completion_id, quantity, allocated in db.execute(
            select(
                ProductionTask.sales_order_item_bom_component_id,
                ProductionCompletion.id,
                ProductionCompletion.quantity,
                allocated_direct.label("allocated_quantity"),
            )
            .join(ProductionTask, ProductionTask.id == ProductionCompletion.task_id)
            .outerjoin(
                ProductionStockTransfer,
                ProductionStockTransfer.completion_id == ProductionCompletion.id,
            )
            .outerjoin(
                BomComponentDirectDeliveryAllocation,
                (
                    BomComponentDirectDeliveryAllocation.production_completion_id
                    == ProductionCompletion.id
                )
                & (
                    BomComponentDirectDeliveryAllocation.status.in_(
                        ACTIVE_RESERVATION_STATUSES
                    )
                ),
            )
            .where(
                ProductionTask.sales_order_item_bom_component_id.in_(snapshot_ids),
                ProductionCompletion.status == "posted",
                ProductionCompletion.initial_disposition == DIRECT_DISPOSITION,
                ProductionCompletion.inventory_lot_id.is_(None),
                ProductionStockTransfer.id.is_(None),
            )
            .group_by(
                ProductionTask.sales_order_item_bom_component_id,
                ProductionCompletion.id,
                ProductionCompletion.quantity,
            )
        ).all():
            key = int(snapshot_id)
            component_direct_available_by_snapshot[key] = (
                component_direct_available_by_snapshot.get(key, 0)
                + max(int(quantity or 0) - int(allocated or 0), 0)
            )
        component_delivered_direct = {
            int(snapshot_id): int(quantity or 0)
            for snapshot_id, quantity in db.execute(
                select(
                    BomComponentDirectDeliveryAllocation.sales_order_item_bom_component_id,
                    func.coalesce(
                        func.sum(
                            BomComponentDirectDeliveryAllocation.consumed_quantity
                            - BomComponentDirectDeliveryAllocation.reversed_quantity
                        ),
                        0,
                    ),
                )
                .where(
                    BomComponentDirectDeliveryAllocation.sales_order_item_bom_component_id.in_(
                        snapshot_ids
                    ),
                    BomComponentDirectDeliveryAllocation.status.in_(
                        ACTIVE_RESERVATION_STATUSES
                    ),
                )
                .group_by(
                    BomComponentDirectDeliveryAllocation.sales_order_item_bom_component_id
                )
            ).all()
        }
        component_delivered_stock = {
            int(snapshot_id): int(quantity or 0)
            for snapshot_id, quantity in db.execute(
                select(
                    InventoryReservation.sales_order_item_bom_component_id,
                    func.coalesce(
                        func.sum(
                            DeliveryInventoryAllocation.credited_requirement_quantity
                            - DeliveryInventoryAllocation.reversed_requirement_quantity
                        ),
                        0,
                    ),
                )
                .join(
                    InventoryReservation,
                    InventoryReservation.id
                    == DeliveryInventoryAllocation.reservation_id,
                )
                .where(
                    InventoryReservation.sales_order_item_bom_component_id.in_(
                        snapshot_ids
                    ),
                    DeliveryInventoryAllocation.status.in_(
                        ACTIVE_RESERVATION_STATUSES
                    ),
                )
                .group_by(InventoryReservation.sales_order_item_bom_component_id)
            ).all()
        }
    component_delivered_by_snapshot = {
        snapshot_id: component_delivered_direct.get(snapshot_id, 0)
        + component_delivered_stock.get(snapshot_id, 0)
        for snapshot_id in snapshot_ids
    }
    component_quantities_by_delivery_item: dict[int, dict[int, int]] = {}
    for allocation in direct_allocations:
        if allocation.status not in ACTIVE_RESERVATION_STATUSES:
            continue
        quantities = component_quantities_by_delivery_item.setdefault(
            int(allocation.delivery_item_id),
            {},
        )
        snapshot_id = int(allocation.sales_order_item_bom_component_id)
        quantities[snapshot_id] = quantities.get(snapshot_id, 0) + int(
            allocation.consumed_quantity or 0
        ) - int(allocation.reversed_quantity or 0)
    for allocation in delivery_allocations:
        if allocation.status not in ACTIVE_RESERVATION_STATUSES:
            continue
        reservation = reservations.get(int(allocation.reservation_id))
        if reservation is None or reservation.sales_order_item_bom_component_id is None:
            continue
        quantities = component_quantities_by_delivery_item.setdefault(
            int(allocation.delivery_item_id),
            {},
        )
        snapshot_id = int(reservation.sales_order_item_bom_component_id)
        quantities[snapshot_id] = quantities.get(snapshot_id, 0) + int(
            allocation.credited_requirement_quantity or 0
        ) - int(allocation.reversed_requirement_quantity or 0)
    pick_task_rows = db.scalars(
        select(DeliveryPickTask)
        .where(DeliveryPickTask.delivery_id.in_(delivery_ids))
        .order_by(DeliveryPickTask.delivery_id, DeliveryPickTask.id.desc())
    ).all() if delivery_ids else []
    pick_tasks: dict[int, DeliveryPickTask] = {}
    for task in pick_task_rows:
        pick_tasks.setdefault(int(task.delivery_id), task)
    pick_task_ids = [task.id for task in pick_tasks.values()]
    pick_items_by_task: dict[int, list[DeliveryPickTaskItem]] = {}
    if pick_task_ids:
        for item in db.scalars(
            select(DeliveryPickTaskItem)
            .where(DeliveryPickTaskItem.task_id.in_(pick_task_ids))
            .order_by(DeliveryPickTaskItem.id)
        ).all():
            pick_items_by_task.setdefault(int(item.task_id), []).append(item)
    assigned_user_ids = {
        int(task.assigned_to)
        for task in pick_tasks.values()
        if task.assigned_to is not None
    }
    assigned_users = {
        int(row.id): row
        for row in db.scalars(
            select(User).where(User.id.in_(assigned_user_ids))
        ).all()
    } if assigned_user_ids else {}
    internal_remarks = _tianhua_internal_remarks_by_delivery_item(
        db, [int(row["id"]) for row in item_rows]
    )
    inventory_backed_delivery_item_ids = {
        int(allocation.delivery_item_id)
        for allocation in delivery_allocations
        if (
            (reservation := reservations.get(int(allocation.reservation_id)))
            is not None
            and reservation.sales_order_item_bom_component_id is None
        )
    }
    context = {
        "deliveries": deliveries,
        "customers": customers,
        "receipts": receipts,
        "rows_by_delivery": rows_by_delivery,
        "orders": orders,
        "order_items": order_items,
        "safe_empty_inventory_order_item_ids": safe_empty_inventory_order_item_ids,
        "component_demands_by_order_item": component_demands_by_order_item,
        "component_direct_available_by_snapshot": component_direct_available_by_snapshot,
        "component_delivered_by_snapshot": component_delivered_by_snapshot,
        "component_quantities_by_delivery_item": component_quantities_by_delivery_item,
        "requirements_by_order_item": requirements_by_order_item,
        "reservations": reservations,
        "reservations_by_order_item": reservations_by_order_item,
        "reservations_by_requirement": reservations_by_requirement,
        "reservations_by_snapshot": reservations_by_snapshot,
        "delivery_allocations_by_delivery_item": delivery_allocations_by_delivery_item,
        "direct_allocations_by_delivery_item": direct_allocations_by_delivery_item,
        "unordered_allocations_by_delivery_item": unordered_allocations_by_delivery_item,
        "lots": lots,
        "locations": locations,
        "has_space_ledger": bool(floors),
        "floors_by_number": floors_by_number,
        "areas_by_floor_code": areas_by_floor_code,
        "active_finished_by_order_item": active_finished_by_order_item,
        "active_semi_by_requirement": active_semi_by_requirement,
        "pick_tasks": pick_tasks,
        "pick_items_by_task": pick_items_by_task,
        "assigned_users": assigned_users,
        "internal_remarks": internal_remarks,
        "inventory_backed_delivery_item_ids": inventory_backed_delivery_item_ids,
    }
    item_contexts: dict[int, dict] = {}
    for row in item_rows:
        delivery_item_id = int(row["id"])
        if row.get("source_type") == "unordered_finished":
            unordered_payloads = [
                _unordered_finished_allocation_payload(allocation)
                for allocation in unordered_allocations_by_delivery_item.get(
                    delivery_item_id,
                    [],
                )
            ]
            item_contexts[delivery_item_id] = {
                "kit_metadata": _empty_delivery_kit_metadata(),
                "inventory_sources": unordered_payloads,
                "unordered_allocations": unordered_payloads,
            }
            continue
        order_item_id = row.get("order_item_id")
        order_item = order_items.get(order_item_id) if order_item_id is not None else None
        if order_item is None:
            item_contexts[delivery_item_id] = {
                "kit_metadata": _empty_delivery_kit_metadata(),
                "inventory_sources": [],
                "unordered_allocations": [],
            }
            continue
        delivery = deliveries.get(int(row["delivery_id"]))
        dispatched = bool(delivery and delivery.status in {"dispatched", "voided"})
        if order_item.id in composite_item_ids:
            kit_metadata = _delivery_list_kit_metadata(
                context,
                order_item=order_item,
                planned_delivery_quantity=int(row["delivered_quantity"] or 0),
                delivery_item_id=delivery_item_id,
                dispatched=dispatched,
            )
            inventory_sources = _delivery_list_composite_inventory_sources(
                context,
                order_item=order_item,
                planned_delivery_quantity=int(row["delivered_quantity"] or 0),
                delivery_item_id=delivery_item_id,
                dispatched=dispatched,
            )
        elif order_item.id in safe_empty_inventory_order_item_ids:
            kit_metadata = _empty_delivery_kit_metadata()
            inventory_sources = []
        else:
            kit_metadata = _empty_delivery_kit_metadata()
            inventory_sources = _delivery_list_standard_inventory_sources(
                context,
                order_item=order_item,
                planned_delivery_quantity=int(row["delivered_quantity"] or 0),
                delivery_item_id=delivery_item_id,
                dispatched=dispatched,
            )
        item_contexts[delivery_item_id] = {
            "kit_metadata": kit_metadata,
            "inventory_sources": inventory_sources,
            "unordered_allocations": [],
        }
    context["item_contexts"] = item_contexts
    context["registry"] = build_display_registry(db)
    return context


def _delivery_list_summary_context(db: Session, delivery_ids: list[int]) -> dict:
    """Build the collapsed desktop list without expanding delivery details.

    The regular response intentionally remains available to existing callers.
    This context is only used by ``view=summary`` and keeps its query families
    fixed for ordinary deliveries, regardless of the number of documents or
    detail rows on the current page.
    """

    if not delivery_ids:
        return {
            "deliveries": {},
            "customers": {},
            "receipts": {},
            "item_counts": {},
            "actual_goods_quantities": {},
            "pick_tasks": {},
        }

    deliveries = {
        delivery.id: delivery
        for delivery in db.scalars(
            select(Delivery).where(Delivery.id.in_(delivery_ids))
        ).all()
    }
    customer_ids = {delivery.customer_id for delivery in deliveries.values()}
    customers = {
        customer.id: customer
        for customer in db.scalars(
            select(Customer).where(Customer.id.in_(customer_ids))
        ).all()
    } if customer_ids else {}

    receipts: dict[int, ReturnReceipt] = {}
    for receipt in db.scalars(
        select(ReturnReceipt)
        .where(ReturnReceipt.delivery_id.in_(delivery_ids))
        .order_by(ReturnReceipt.delivery_id, ReturnReceipt.id)
    ).all():
        receipts[int(receipt.delivery_id)] = receipt

    item_counts: dict[int, int] = {}
    actual_goods_quantities: dict[int, int] = {}
    for delivery_id, item_count, delivered_quantity in db.execute(
        select(
            DeliveryItem.delivery_id,
            func.count(DeliveryItem.id),
            func.coalesce(func.sum(DeliveryItem.delivered_quantity), 0),
        )
        .where(DeliveryItem.delivery_id.in_(delivery_ids))
        .group_by(DeliveryItem.delivery_id)
    ).all():
        normalized_id = int(delivery_id)
        item_counts[normalized_id] = int(item_count or 0)
        actual_goods_quantities[normalized_id] = int(delivered_quantity or 0)

    # Ordinary deliveries need no further work.  Composite orders are rare but
    # their collapsed quantity must still include the physical component lines,
    # so calculate only those exceptional rows using the established workflow.
    composite_rows = db.execute(
        select(
            DeliveryItem.id,
            DeliveryItem.delivery_id,
            DeliveryItem.order_item_id,
            DeliveryItem.delivered_quantity,
            Delivery.status,
            OrderItem.delivered_quantity.label("order_delivered_quantity"),
        )
        .join(Delivery, Delivery.id == DeliveryItem.delivery_id)
        .join(OrderItem, OrderItem.id == DeliveryItem.order_item_id)
        .where(
            DeliveryItem.delivery_id.in_(delivery_ids),
            DeliveryItem.order_item_id.in_(
                select(SalesOrderItemBomComponent.sales_order_item_id).distinct()
            ),
        )
        .order_by(DeliveryItem.delivery_id, DeliveryItem.id)
    ).all()
    component_quantities = _delivery_summary_component_quantities(
        db, composite_rows
    )
    for delivery_id, component_quantity in component_quantities.items():
        actual_goods_quantities[delivery_id] = (
            actual_goods_quantities.get(delivery_id, 0) + component_quantity
        )

    pick_task_rows = db.scalars(
        select(DeliveryPickTask)
        .where(DeliveryPickTask.delivery_id.in_(delivery_ids))
        .order_by(DeliveryPickTask.delivery_id, DeliveryPickTask.id.desc())
    ).all()
    latest_pick_tasks: dict[int, DeliveryPickTask] = {}
    for task in pick_task_rows:
        latest_pick_tasks.setdefault(int(task.delivery_id), task)
    pick_tasks = {
        int(summary["delivery_id"]): summary
        for summary in _delivery_pick_task_list_summaries(
            db, list(latest_pick_tasks.values())
        )
    }
    return {
        "deliveries": deliveries,
        "customers": customers,
        "receipts": receipts,
        "item_counts": item_counts,
        "actual_goods_quantities": actual_goods_quantities,
        "pick_tasks": pick_tasks,
    }


def _delivery_summary_component_quantities(
    db: Session,
    composite_rows: list,
) -> dict[int, int]:
    """Return exact component pieces with a fixed set of aggregate queries."""

    if not composite_rows:
        return {}
    pending_rows = [
        row for row in composite_rows if row.status not in {"dispatched", "voided"}
    ]
    historical_rows = [
        row for row in composite_rows if row.status in {"dispatched", "voided"}
    ]
    result: dict[int, int] = {}

    if historical_rows:
        historical_item_ids = [int(row.id) for row in historical_rows]
        historical_by_item: dict[int, int] = {}
        for delivery_item_id, quantity in db.execute(
            select(
                BomComponentDirectDeliveryAllocation.delivery_item_id,
                func.coalesce(
                    func.sum(
                        BomComponentDirectDeliveryAllocation.consumed_quantity
                        - BomComponentDirectDeliveryAllocation.reversed_quantity
                    ),
                    0,
                ),
            )
            .where(
                BomComponentDirectDeliveryAllocation.delivery_item_id.in_(
                    historical_item_ids
                ),
                BomComponentDirectDeliveryAllocation.status.in_(
                    ACTIVE_RESERVATION_STATUSES
                ),
            )
            .group_by(BomComponentDirectDeliveryAllocation.delivery_item_id)
        ).all():
            historical_by_item[int(delivery_item_id)] = int(quantity or 0)
        for delivery_item_id, quantity in db.execute(
            select(
                DeliveryInventoryAllocation.delivery_item_id,
                func.coalesce(
                    func.sum(
                        DeliveryInventoryAllocation.credited_requirement_quantity
                        - DeliveryInventoryAllocation.reversed_requirement_quantity
                    ),
                    0,
                ),
            )
            .join(
                InventoryReservation,
                InventoryReservation.id == DeliveryInventoryAllocation.reservation_id,
            )
            .where(
                DeliveryInventoryAllocation.delivery_item_id.in_(
                    historical_item_ids
                ),
                InventoryReservation.sales_order_item_bom_component_id.is_not(None),
                DeliveryInventoryAllocation.status.in_(
                    ACTIVE_RESERVATION_STATUSES
                ),
            )
            .group_by(DeliveryInventoryAllocation.delivery_item_id)
        ).all():
            normalized_id = int(delivery_item_id)
            historical_by_item[normalized_id] = (
                historical_by_item.get(normalized_id, 0) + int(quantity or 0)
            )
        for row in historical_rows:
            delivery_id = int(row.delivery_id)
            result[delivery_id] = (
                result.get(delivery_id, 0)
                + historical_by_item.get(int(row.id), 0)
            )

    if pending_rows:
        order_item_ids = {int(row.order_item_id) for row in pending_rows}
        snapshots = db.scalars(
            select(SalesOrderItemBomComponent)
            .where(
                SalesOrderItemBomComponent.sales_order_item_id.in_(
                    order_item_ids
                )
            )
            .order_by(
                SalesOrderItemBomComponent.sales_order_item_id,
                SalesOrderItemBomComponent.display_order,
                SalesOrderItemBomComponent.id,
            )
        ).all()
        snapshot_ids = [int(snapshot.id) for snapshot in snapshots]
        snapshots_by_order_item: dict[int, list[SalesOrderItemBomComponent]] = {}
        for snapshot in snapshots:
            snapshots_by_order_item.setdefault(
                int(snapshot.sales_order_item_id), []
            ).append(snapshot)

        adjustment_totals = {
            int(snapshot_id): (int(delta_sets or 0), int(delta_pieces or 0))
            for snapshot_id, delta_sets, delta_pieces in db.execute(
                select(
                    SalesOrderItemBomDemandAdjustment.sales_order_item_bom_component_id,
                    func.coalesce(
                        func.sum(
                            SalesOrderItemBomDemandAdjustment.delta_order_set_quantity
                        ),
                        0,
                    ),
                    func.coalesce(
                        func.sum(
                            SalesOrderItemBomDemandAdjustment.delta_required_piece_quantity
                        ),
                        0,
                    ),
                )
                .where(
                    SalesOrderItemBomDemandAdjustment.sales_order_item_bom_component_id.in_(
                        snapshot_ids
                    )
                )
                .group_by(
                    SalesOrderItemBomDemandAdjustment.sales_order_item_bom_component_id
                )
            ).all()
        } if snapshot_ids else {}
        consumed_quantities: dict[int, int] = {}
        if snapshot_ids:
            for snapshot_id, quantity in db.execute(
                select(
                    BomComponentDirectDeliveryAllocation.sales_order_item_bom_component_id,
                    func.coalesce(
                        func.sum(
                            BomComponentDirectDeliveryAllocation.consumed_quantity
                            - BomComponentDirectDeliveryAllocation.reversed_quantity
                        ),
                        0,
                    ),
                )
                .where(
                    BomComponentDirectDeliveryAllocation.sales_order_item_bom_component_id.in_(
                        snapshot_ids
                    ),
                    BomComponentDirectDeliveryAllocation.status.in_(
                        ACTIVE_RESERVATION_STATUSES
                    ),
                )
                .group_by(
                    BomComponentDirectDeliveryAllocation.sales_order_item_bom_component_id
                )
            ).all():
                consumed_quantities[int(snapshot_id)] = int(quantity or 0)
            for snapshot_id, quantity in db.execute(
                select(
                    InventoryReservation.sales_order_item_bom_component_id,
                    func.coalesce(
                        func.sum(
                            DeliveryInventoryAllocation.credited_requirement_quantity
                            - DeliveryInventoryAllocation.reversed_requirement_quantity
                        ),
                        0,
                    ),
                )
                .join(
                    InventoryReservation,
                    InventoryReservation.id
                    == DeliveryInventoryAllocation.reservation_id,
                )
                .where(
                    InventoryReservation.sales_order_item_bom_component_id.in_(
                        snapshot_ids
                    ),
                    DeliveryInventoryAllocation.status.in_(
                        ACTIVE_RESERVATION_STATUSES
                    ),
                )
                .group_by(
                    InventoryReservation.sales_order_item_bom_component_id
                )
            ).all():
                normalized_id = int(snapshot_id)
                consumed_quantities[normalized_id] = (
                    consumed_quantities.get(normalized_id, 0) + int(quantity or 0)
                )

        for row in pending_rows:
            component_quantity = 0
            delivered_after = max(int(row.order_delivered_quantity or 0), 0) + max(
                int(row.delivered_quantity or 0), 0
            )
            for snapshot in snapshots_by_order_item.get(
                int(row.order_item_id), []
            ):
                snapshot_id = int(snapshot.id)
                delta_sets, delta_pieces = adjustment_totals.get(
                    snapshot_id, (0, 0)
                )
                if int(snapshot.order_set_quantity or 0) + delta_sets < 0:
                    raise CompositeBomWorkflowError(
                        "组件调整后的有效套数不能小于0"
                    )
                target = int(snapshot.required_piece_quantity or 0) + (
                    delta_pieces
                )
                if target <= 0:
                    raise CompositeBomWorkflowError(
                        "组件调整后的需求件数必须大于0"
                    )
                target_after = min(
                    delivered_after * int(snapshot.quantity_per_set or 0),
                    target,
                )
                component_quantity += max(
                    target_after - consumed_quantities.get(snapshot_id, 0), 0
                )
            delivery_id = int(row.delivery_id)
            result[delivery_id] = result.get(delivery_id, 0) + component_quantity
    return result


def _delivery_summary_response(delivery_id: int, *, context: dict) -> dict:
    """Serialize fields needed before a delivery row is expanded."""

    delivery = context["deliveries"].get(delivery_id)
    if delivery is None:
        raise HTTPException(status_code=404, detail="送货单不存在")
    customer = context["customers"].get(delivery.customer_id)
    return_receipt = context["receipts"].get(delivery_id)
    return {
        "id": delivery.id,
        "delivery_number": delivery.delivery_number,
        "customer_id": delivery.customer_id,
        "customer_name": customer.name if customer else None,
        "delivery_date": delivery.delivery_date,
        "vehicle_number": delivery.vehicle_number,
        "source_mode": delivery.source_mode,
        "status": delivery.status,
        "total_quantity": delivery.total_quantity,
        "total_actual_goods_quantity": context["actual_goods_quantities"].get(
            delivery_id, 0
        ),
        "item_count": context["item_counts"].get(delivery_id, 0),
        "dispatched_at": (
            utc_naive_to_api(delivery.dispatched_at)
            if delivery.dispatched_at
            else None
        ),
        "ever_dispatched_at": (
            utc_naive_to_api(delivery.ever_dispatched_at)
            if delivery.ever_dispatched_at
            else None
        ),
        "voided_at": (
            utc_naive_to_api(delivery.voided_at)
            if delivery.voided_at
            else None
        ),
        "voided_by": delivery.voided_by,
        "is_printed": delivery.printed_at is not None,
        "printed_at": (
            utc_naive_to_api(delivery.printed_at) if delivery.printed_at else None
        ),
        "printed_by": delivery.printed_by,
        "return_receipt_id": return_receipt.id if return_receipt else None,
        "return_receipt_status": (
            return_receipt.status if return_receipt else None
        ),
        "pick_task": context["pick_tasks"].get(delivery_id),
    }


def _unordered_finished_allocation_response(
    db: Session,
    delivery_item_id: int,
) -> list[dict]:
    rows = db.scalars(
        select(UnorderedFinishedDeliveryAllocation)
        .where(
            UnorderedFinishedDeliveryAllocation.delivery_item_id
            == delivery_item_id
        )
        .order_by(UnorderedFinishedDeliveryAllocation.id)
    ).all()
    return [
        {
            "id": row.id,
            "inventory_lot_id": row.inventory_lot_id,
            "lot_number": row.lot_number_snapshot,
            "location_id": row.warehouse_location_id_snapshot,
            "location_code": row.warehouse_location_code_snapshot,
            "pallet_code": row.pallet_code_snapshot,
            "quantity": int(row.planned_quantity or 0),
            "planned_quantity": int(row.planned_quantity or 0),
            "consumed_quantity": int(row.consumed_quantity or 0),
            "restored_quantity": int(row.restored_quantity or 0),
            "status": row.status,
        }
        for row in rows
    ]


def _delivery_response(
    db: Session,
    delivery_id: int,
    *,
    list_context: dict | None = None,
) -> dict:
    from app.models.finance import ReturnReceipt

    delivery = (
        list_context["deliveries"].get(delivery_id)
        if list_context is not None
        else _delivery_or_404(db, delivery_id)
    )
    if delivery is None:
        raise HTTPException(status_code=404, detail="送货单不存在")
    customer = (
        list_context["customers"].get(delivery.customer_id)
        if list_context is not None
        else db.get(Customer, delivery.customer_id)
    )
    return_receipt = (
        list_context["receipts"].get(delivery_id)
        if list_context is not None
        else db.scalar(select(ReturnReceipt).where(ReturnReceipt.delivery_id == delivery_id))
    )
    items = (
        list_context["rows_by_delivery"].get(delivery_id, [])
        if list_context is not None
        else _delivery_item_rows(db, [delivery_id])
    )
    registry = list_context["registry"] if list_context is not None else build_display_registry(db)
    order_ids = {
        row["order_id"]
        for row in items
        if row.get("order_id") is not None
    }
    orders = (
        {order_id: list_context["orders"][order_id] for order_id in order_ids if order_id in list_context["orders"]}
        if list_context is not None
        else {
            order.id: order for order in db.scalars(select(Order).where(Order.id.in_(order_ids))).all()
        } if order_ids else {}
    )
    pick_task = (
        list_context["pick_tasks"].get(delivery_id)
        if list_context is not None
        else _delivery_pick_task(db, delivery_id)
    )
    has_dispatch_history = delivery.status in {"dispatched", "voided"}
    pick_by_delivery_item = {
        item.delivery_item_id: _pick_item_response(db, item)
        for item in (pick_task.items if pick_task and list_context is None else [])
    }
    internal_remarks = (
        list_context["internal_remarks"]
        if list_context is not None
        else _tianhua_internal_remarks_by_delivery_item(
            db,
            [
                row["id"]
                for row in items
                if str(row["remarks"] or "").strip().startswith(
                "来源：天华预送货草稿 "
                )
            ],
        )
    )
    delivery_item_ids = [int(row["id"]) for row in items]
    inventory_backed_delivery_item_ids = (
        set(list_context["inventory_backed_delivery_item_ids"])
        if list_context is not None
        else set(
            db.scalars(
                select(DeliveryInventoryAllocation.delivery_item_id)
                .join(
                    InventoryReservation,
                    InventoryReservation.id
                    == DeliveryInventoryAllocation.reservation_id,
                )
                .where(
                    DeliveryInventoryAllocation.delivery_item_id.in_(
                        delivery_item_ids
                    ),
                    InventoryReservation.sales_order_item_bom_component_id.is_(None),
                )
                .distinct()
            ).all()
        )
        if delivery_item_ids
        else set()
    )
    response_items: list[dict] = []
    total_actual_goods_quantity = 0
    for row in items:
        mapping = dict(row)
        mapping["product_code"] = (
            str(mapping.get("product_code") or "").strip()
            or "存货编码未登记"
        )
        mapping["product_name"] = (
            str(mapping.get("product_name") or "").strip()
            or "产品名称未登记"
        )
        mapping["specification"] = (
            str(mapping.get("specification") or "").strip()
            or "规格未登记"
        )
        is_unordered = mapping.get("source_type") == "unordered_finished"
        order_item = (
            list_context["order_items"].get(mapping["order_item_id"])
            if list_context is not None
            else (
                db.get(OrderItem, mapping["order_item_id"])
                if mapping.get("order_item_id") is not None
                else None
            )
        )
        skip_inventory_lookup = bool(
            list_context is not None
            and order_item is not None
            and int(order_item.id)
            in list_context["safe_empty_inventory_order_item_ids"]
        )
        item_context = (
            list_context["item_contexts"].get(int(mapping["id"]))
            if list_context is not None
            else None
        )
        kit_metadata = (
            item_context["kit_metadata"]
            if item_context is not None
            else (
                _empty_delivery_kit_metadata()
                if skip_inventory_lookup
                else _delivery_kit_metadata(
                    db,
                    order_item,
                    planned_delivery_quantity=mapping["delivered_quantity"],
                    delivery_item_id=mapping["id"],
                    dispatched=has_dispatch_history,
                )
            )
        )
        actual_goods_lines = (
            [
                {
                    "line_type": "parent",
                    "order_item_id": None,
                    "component_snapshot_id": None,
                    "product_code": mapping["product_code"],
                    "product_name": mapping["product_name"],
                    "specification": mapping["specification"],
                    "unit": mapping.get("unit_snapshot") or "PCS",
                    "quantity": int(mapping["delivered_quantity"] or 0),
                    "pricing_included": True,
                    "independent_return_receipt": True,
                    "independent_statement": True,
                }
            ]
            if is_unordered
            else _actual_goods_lines(
                order_item_id=mapping["order_item_id"],
                product_code=mapping["product_code"],
                product_name=mapping["product_name"],
                specification=mapping["specification"],
                parent_quantity=mapping["delivered_quantity"],
                component_lines=kit_metadata["component_lines"],
            )
        )
        actual_goods_quantity = sum(
            int(line["quantity"] or 0) for line in actual_goods_lines
        )
        total_actual_goods_quantity += actual_goods_quantity
        response_items.append(
            {
                **dict(mapping),
                "remarks": _customer_visible_delivery_remark(
                    mapping["id"],
                    mapping["remarks"],
                    internal_remarks,
                ),
                "actual_delivery_quantity": mapping["delivered_quantity"],
                "order_number": (
                    "无订单库存"
                    if is_unordered
                    else display_order_number(
                        orders.get(mapping["order_id"]),
                        registry,
                    )
                ),
                "display_order_number": (
                    "无订单库存"
                    if is_unordered
                    else display_order_number(
                        orders.get(mapping["order_id"]),
                        registry,
                    )
                ),
                "customer_po": (
                    "无订单库存" if is_unordered else mapping.get("customer_po")
                ),
                **kit_metadata,
                "actual_goods_lines": actual_goods_lines,
                "actual_goods_quantity": actual_goods_quantity,
                "requires_return_location": bool(
                    not is_unordered
                    and has_dispatch_history
                    and int(mapping["id"])
                    in inventory_backed_delivery_item_ids
                ),
                "allocations": (
                    item_context["unordered_allocations"]
                    if item_context is not None
                    else _unordered_finished_allocation_response(db, mapping["id"])
                    if is_unordered
                    else []
                ),
                "inventory_sources": (
                    item_context["inventory_sources"]
                    if item_context is not None
                    else _unordered_finished_allocation_response(db, mapping["id"])
                    if is_unordered
                    else []
                    if skip_inventory_lookup
                    else _inventory_sources_for_order_item(
                        db,
                        order_item=order_item,
                        planned_delivery_quantity=mapping["delivered_quantity"],
                        delivery_item_id=mapping["id"],
                        dispatched=has_dispatch_history,
                    )
                ),
                "pick_result": pick_by_delivery_item.get(mapping["id"]),
            }
        )
    return {
        "id": delivery.id,
        "delivery_number": delivery.delivery_number,
        "customer_id": delivery.customer_id,
        "customer_name": customer.name if customer else None,
        "delivery_date": delivery.delivery_date,
        "vehicle_number": delivery.vehicle_number,
        "source_mode": delivery.source_mode,
        "status": delivery.status,
        "total_quantity": delivery.total_quantity,
        "total_actual_goods_quantity": total_actual_goods_quantity,
        "dispatched_at": (
            utc_naive_to_api(delivery.dispatched_at)
            if delivery.dispatched_at
            else None
        ),
        "ever_dispatched_at": (
            utc_naive_to_api(delivery.ever_dispatched_at)
            if delivery.ever_dispatched_at
            else None
        ),
        "voided_at": (
            utc_naive_to_api(delivery.voided_at)
            if delivery.voided_at
            else None
        ),
        "voided_by": delivery.voided_by,
        "is_printed": delivery.printed_at is not None,
        "printed_at": (
            utc_naive_to_api(delivery.printed_at) if delivery.printed_at else None
        ),
        "printed_by": delivery.printed_by,
        "return_receipt_id": return_receipt.id if return_receipt else None,
        "return_receipt_status": (
            return_receipt.status if return_receipt else None
        ),
        "pick_task": (
            _delivery_pick_task_summary(
                db,
                pick_task,
                customer_name=customer.name if customer else None,
                delivery_number=delivery.delivery_number,
                items=list_context["pick_items_by_task"].get(pick_task.id, []),
                assigned_user=list_context["assigned_users"].get(
                    pick_task.assigned_to
                ),
                assignee_preloaded=True,
            )
            if pick_task and list_context is not None
            else (_pick_task_response(db, pick_task) if pick_task else None)
        ),
        "items": response_items,
    }


def _write_audit(
    db: Session,
    *,
    user: User,
    action: str,
    resource: str,
    entity_id: int,
    details: dict,
    description: str,
) -> None:
    db.add(
        OperationLog(
            user_id=user.id,
            action=action,
            resource=resource,
            details=json.dumps(details, ensure_ascii=False, default=str),
            username=user.username,
            role=user.role,
            entity_type=resource.lower(),
            entity_id=entity_id,
            description=description,
        )
    )


def _current_delivery_lifecycle_dispatch_at(
    db: Session,
    delivery: Delivery,
) -> datetime | None:
    """Return a dispatch timestamp without confusing a reused SQLite row id."""

    create_logs = db.scalars(
        select(OperationLog)
        .where(
            OperationLog.resource == "Delivery",
            OperationLog.entity_id == delivery.id,
            OperationLog.action == "CREATE",
        )
        .order_by(OperationLog.id.desc())
    ).all()
    current_create_log: OperationLog | None = None
    for log in create_logs:
        try:
            details = json.loads(log.details or "{}")
        except (TypeError, ValueError):
            continue
        if details.get("delivery_number") == delivery.delivery_number:
            current_create_log = log
            break
    if current_create_log is None:
        # Legacy/imported rows may not have a CREATE audit.  A timestamp-bound
        # lookup is conservative: a same-second id reuse may cause a harmless
        # soft void, but it cannot cause historical facts to be hard-deleted.
        return db.scalar(
            select(func.min(OperationLog.created_at)).where(
                OperationLog.resource == "Delivery",
                OperationLog.entity_id == delivery.id,
                OperationLog.created_at >= delivery.created_at,
                OperationLog.action.in_(("DISPATCH", "CANCEL_DISPATCH")),
            )
        )
    return db.scalar(
        select(func.min(OperationLog.created_at)).where(
            OperationLog.resource == "Delivery",
            OperationLog.entity_id == delivery.id,
            OperationLog.id > current_create_log.id,
            OperationLog.action.in_(("DISPATCH", "CANCEL_DISPATCH")),
        )
    )


def _delivery_deletion_facts(
    db: Session,
    delivery: Delivery,
) -> dict:
    from app.models.finance import (
        ReturnReceipt,
        ReturnReceiptItem,
        StatementItem,
    )

    item_ids = list(
        db.scalars(
            select(DeliveryItem.id).where(
                DeliveryItem.delivery_id == delivery.id
            )
        ).all()
    )
    inventory_allocations = (
        db.scalars(
            select(DeliveryInventoryAllocation).where(
                DeliveryInventoryAllocation.delivery_item_id.in_(item_ids)
            )
        ).all()
        if item_ids
        else []
    )
    component_allocations = (
        db.scalars(
            select(BomComponentDirectDeliveryAllocation).where(
                BomComponentDirectDeliveryAllocation.delivery_item_id.in_(
                    item_ids
                )
            )
        ).all()
        if item_ids
        else []
    )
    unordered_allocations = (
        db.scalars(
            select(UnorderedFinishedDeliveryAllocation).where(
                UnorderedFinishedDeliveryAllocation.delivery_item_id.in_(
                    item_ids
                )
            )
        ).all()
        if item_ids
        else []
    )
    receipt = db.scalar(
        select(ReturnReceipt).where(ReturnReceipt.delivery_id == delivery.id)
    )
    statement_item_id = db.scalar(
        select(StatementItem.id)
        .join(
            ReturnReceiptItem,
            ReturnReceiptItem.id == StatementItem.return_receipt_item_id,
        )
        .join(
            ReturnReceipt,
            ReturnReceipt.id == ReturnReceiptItem.return_receipt_id,
        )
        .where(ReturnReceipt.delivery_id == delivery.id)
        .limit(1)
    )
    movement_at = db.scalar(
        select(func.min(InventoryMovement.created_at)).where(
            InventoryMovement.related_delivery_id == delivery.id
        )
    )
    lifecycle_dispatch_at = _current_delivery_lifecycle_dispatch_at(
        db,
        delivery,
    )
    history_times = [
        delivery.ever_dispatched_at,
        movement_at,
        lifecycle_dispatch_at,
        receipt.created_at if receipt is not None else None,
        *(row.created_at for row in inventory_allocations),
        *(row.created_at for row in component_allocations),
        *(
            row.dispatched_at or row.created_at
            for row in unordered_allocations
            if int(row.consumed_quantity or 0) > 0
        ),
    ]
    history_times = [value for value in history_times if value is not None]
    return {
        "inventory_allocations": inventory_allocations,
        "component_allocations": component_allocations,
        "unordered_allocations": unordered_allocations,
        "receipt": receipt,
        "statement_item_id": statement_item_id,
        "history_at": min(history_times) if history_times else None,
        "has_history": bool(history_times),
    }


def _build_pick_task(
    db: Session,
    *,
    delivery: Delivery,
    user: User,
) -> DeliveryPickTask:
    if delivery.status != "pending":
        raise HTTPException(status_code=409, detail="已发货送货单不能创建拿货任务")
    previous = _delivery_pick_task(db, delivery.id)
    if previous is not None:
        # The normal button is idempotent.  Editing the delivery explicitly
        # invalidates the task; a repeated click must never erase driver input.
        if previous.assigned_to is None and previous.status == "pushed":
            candidates = _eligible_delivery_pickers(
                db,
                customer_id=previous.customer_id,
            )
            if len(candidates) == 1:
                previous.assigned_to = candidates[0].id
        return previous
    lines = db.scalars(
        select(DeliveryItem)
        .where(
            DeliveryItem.delivery_id == delivery.id,
        )
        .order_by(DeliveryItem.id)
    ).all()
    if not lines:
        raise HTTPException(status_code=409, detail="送货单没有可拿货明细")
    task = DeliveryPickTask(
        delivery_id=delivery.id,
        customer_id=delivery.customer_id,
        status="pushed",
        snapshot_version=1,
        created_by=user.id,
    )
    candidates = _eligible_delivery_pickers(db, customer_id=delivery.customer_id)
    if len(candidates) == 1:
        task.assigned_to = candidates[0].id
    db.add(task)
    db.flush()
    for line in lines:
        order_item = (
            db.get(OrderItem, line.order_item_id)
            if line.order_item_id is not None
            else None
        )
        product = db.get(Product, order_item.product_id) if order_item else None
        db.add(
            DeliveryPickTaskItem(
                task_id=task.id,
                delivery_item_id=line.id,
                order_item_id=line.order_item_id,
                original_quantity=int(line.delivered_quantity),
                picked_quantity=0,
                status="pending",
                product_code_snapshot=(
                    product.product_code if product else line.product_code_snapshot
                ),
                product_name_snapshot=(
                    order_item.snapshot_product_name
                    if order_item
                    else line.product_name_snapshot
                ),
                specification_snapshot=(
                    order_item.snapshot_spec
                    if order_item
                    else line.specification_snapshot
                ),
            )
        )
    db.flush()
    _write_audit(
        db,
        user=user,
        action="CREATE_PICK_TASK",
        resource="DeliveryPickTask",
        entity_id=task.id,
        details={
            "delivery_id": delivery.id,
            "delivery_number": delivery.delivery_number,
            "snapshot_version": task.snapshot_version,
            "item_count": len(lines),
            "assigned_to": task.assigned_to,
        },
        description="创建送货拿货任务快照",
    )
    return task


def _picker_can_access_customer(
    db: Session,
    picker: User,
    customer_id: int,
) -> bool:
    return has_unrestricted_customer_access(picker, db) or customer_id in customer_scope_ids(
        picker,
        db,
    )


def _eligible_delivery_pickers(
    db: Session,
    *,
    customer_id: int | None = None,
) -> list[User]:
    candidates = db.scalars(
        select(User)
        .where(User.is_active.is_(True), User.role == "delivery_picker")
        .order_by(User.real_name, User.username)
    ).all()
    return [
        picker
        for picker in candidates
        if has_permission(picker, "deliveries.pick")
        and (
            customer_id is None
            or _picker_can_access_customer(db, picker, customer_id)
        )
    ]


def _can_view_pick_tasks(
    user: User = Depends(get_current_user),
) -> User:
    if not (
        has_permission(user, "deliveries.pick")
        or has_permission(user, "deliveries.execute")
    ):
        raise HTTPException(status_code=403, detail="权限不足")
    return user


def _pick_task_for_user(
    db: Session,
    task_id: int,
    user: User,
) -> DeliveryPickTask:
    task = db.get(DeliveryPickTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="拿货任务不存在")
    require_customer_access(task.customer_id, user, db)
    if not has_permission(user, "deliveries.execute") and task.assigned_to != user.id:
        # Do not reveal whether another employee has this task.
        raise HTTPException(status_code=404, detail="拿货任务不存在或未分配给当前账号")
    return task


def _delivery_pick_measured_map_context(
    db: Session,
    *,
    task: DeliveryPickTask,
) -> tuple[dict, list[dict], dict[int, object], dict[int, object]]:
    """Return the current pick plan plus its formal warehouse-space metadata.

    The delivery pick plan remains the authority for which locations belong to
    this task.  The map only supplies measured floor geometry for those exact
    locations; it must never expand the task into a general inventory view.
    """

    task_payload = _pick_task_response(db, task)
    location_groups = [
        group
        for group in task_payload.get("location_groups") or []
        if group.get("location_id") is not None
        and group.get("warehouse_floor") is not None
        and str(group.get("area_code") or "").strip()
    ]
    floor_numbers = {
        int(group["warehouse_floor"])
        for group in location_groups
        if group.get("warehouse_floor") is not None
    }
    floor_rows = list(
        db.scalars(
            select(WarehouseFloor).where(
                WarehouseFloor.floor_number.in_(floor_numbers or {-1})
            )
        ).all()
    )
    floors_by_number = {int(row.floor_number): row for row in floor_rows}
    floor_ids = [int(row.id) for row in floor_rows]
    area_rows = list(
        db.scalars(
            select(WarehouseArea).where(
                WarehouseArea.floor_id.in_(floor_ids or [-1])
            )
        ).all()
    )
    areas_by_key = {
        (int(row.floor_id), str(row.area_code or "").strip().upper()): row
        for row in area_rows
    }
    return task_payload, location_groups, floors_by_number, areas_by_key


def _delivery_pick_floor_code(
    floor_number: int,
    floors_by_number: dict[int, object],
) -> str:
    floor = floors_by_number.get(int(floor_number))
    return (
        str(floor.floor_code).strip().upper()
        if floor is not None and str(floor.floor_code or "").strip()
        else f"{int(floor_number)}F"
    )


def _delivery_pick_map_floor(
    db: Session,
    *,
    floor_code: str,
) -> dict | None:
    try:
        return overlay_formal_area_bindings(
            db,
            floor_code=floor_code,
            floor_layout=load_warehouse_twin_floor(floor_code),
        )
    except (WarehouseTwinLayoutNotFoundError, ValueError):
        return None


def _delivery_pick_map_features(
    map_floor: dict | None,
    *,
    area_code: str,
) -> list[dict]:
    if map_floor is None:
        return []
    normalized_area = area_code.strip().upper()
    features: list[dict] = []
    for raw in map_floor.get("features") or []:
        kind = str(raw.get("feature_kind") or "")
        bound_area = str(raw.get("erp_area_code") or "").strip().upper()
        if kind == "aisle" or (kind == "zone" and bound_area == normalized_area):
            features.append(
                {
                    "id": raw.get("id"),
                    "feature_kind": kind,
                    "name": raw.get("name"),
                    "points": raw.get("points") or [],
                }
            )
    return features


def _delivery_pick_group_updated_at(task_payload: dict, group: dict) -> str | None:
    updated_by_item = {
        int(item["id"]): item.get("updated_at")
        for item in task_payload.get("items") or []
        if item.get("id") is not None and item.get("updated_at")
    }
    timestamps = [
        updated_by_item.get(int(line["pick_item_id"]))
        for line in group.get("lines") or []
        if line.get("pick_item_id") is not None
        and updated_by_item.get(int(line["pick_item_id"]))
    ]
    return max(timestamps) if timestamps else task_payload.get("as_of")


@router.post("/{delivery_id}/pick-task", status_code=status.HTTP_201_CREATED)
def create_or_rebuild_delivery_pick_task(
    delivery_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    delivery = _delivery_for_user(db, delivery_id, user)
    try:
        task = _build_pick_task(db, delivery=delivery, user=user)
        db.commit()
        return _pick_task_response(db, task)
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise


@pick_router.get("")
def list_delivery_pick_tasks(
    status_filter: str | None = Query(default=None, alias="status"),
    response_mode: Literal["full", "summary"] = Query(default="full"),
    include_dispatched: bool = Query(default=True),
    page: int | None = Query(default=None, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(_can_view_pick_tasks),
) -> dict:
    query = select(DeliveryPickTask).order_by(DeliveryPickTask.id.desc())
    if not has_permission(user, "deliveries.execute"):
        query = query.where(DeliveryPickTask.assigned_to == user.id)
    visible = _visible_customer_ids(user, db)
    if visible is not None:
        query = query.where(DeliveryPickTask.customer_id.in_(visible))
    if status_filter:
        normalized_status = status_filter.strip().lower()
        if normalized_status not in PICK_TASK_STATUSES:
            raise HTTPException(status_code=400, detail="拿货任务状态筛选值无效")
        query = query.where(DeliveryPickTask.status == normalized_status)
    if not include_dispatched:
        query = query.where(DeliveryPickTask.status != "dispatched")
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    if page is not None:
        query = query.offset((page - 1) * page_size).limit(page_size)
    tasks = db.scalars(query).all()
    items = (
        _delivery_pick_task_list_summaries(db, tasks)
        if response_mode == "summary"
        else [
            _pick_task_response(db, task, include_location_plan=False)
            for task in tasks
        ]
    )
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size if page is not None else total,
        "total_pages": (
            (total + page_size - 1) // page_size
            if page is not None and total
            else (1 if total else 0)
        ),
        "as_of": utc_naive_to_api(_utc_now()),
    }


@pick_router.get("/assignees")
def list_delivery_pick_assignees(
    customer_id: int | None = Query(default=None, gt=0),
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    if customer_id is not None:
        require_customer_access(customer_id, user, db)
    return {
        "items": [
            {
                "id": picker.id,
                "username": picker.username,
                "name": picker.display_name or picker.real_name or picker.username,
            }
            for picker in _eligible_delivery_pickers(db, customer_id=customer_id)
        ]
    }


@pick_router.put("/{task_id}/assignment")
def assign_delivery_pick_task(
    task_id: int,
    payload: DeliveryPickAssignmentUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    task = _pick_task_for_user(db, task_id, user)
    if task.status != "pushed" or any(
        item.status != "pending" or int(item.picked_quantity or 0) != 0
        for item in task.items
    ):
        raise HTTPException(status_code=409, detail="拿货已开始，不能改派送货员")
    picker = None
    if payload.picker_user_id is not None:
        picker = db.get(User, payload.picker_user_id)
        if (
            picker is None
            or picker not in _eligible_delivery_pickers(
                db,
                customer_id=task.customer_id,
            )
        ):
            raise HTTPException(status_code=400, detail="请选择可执行本客户任务的送货拿货员")
    previous = task.assigned_to
    task.assigned_to = picker.id if picker is not None else None
    _write_audit(
        db,
        user=user,
        action="ASSIGN_PICK_TASK",
        resource="DeliveryPickTask",
        entity_id=task.id,
        details={"previous_assigned_to": previous, "assigned_to": task.assigned_to},
        description="分配送货拿货任务",
    )
    db.commit()
    return _pick_task_response(db, task)


@pick_router.get("/{task_id}")
def get_delivery_pick_task(
    task_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(_can_view_pick_tasks),
) -> dict:
    return _pick_task_response(db, _pick_task_for_user(db, task_id, user))


@pick_router.get("/{task_id}/measured-map/floors")
def get_delivery_pick_measured_map_floors(
    task_id: int,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(_can_view_pick_tasks),
) -> dict:
    response.headers["Cache-Control"] = "private, no-store"
    task = _pick_task_for_user(db, task_id, user)
    task_payload, groups, floors_by_number, areas_by_key = (
        _delivery_pick_measured_map_context(db, task=task)
    )
    grouped: dict[str, dict] = {}
    for group in groups:
        floor_number = int(group["warehouse_floor"])
        floor_code = _delivery_pick_floor_code(floor_number, floors_by_number)
        floor_row = floors_by_number.get(floor_number)
        area_code = str(group.get("area_code") or "").strip().upper()
        area_row = (
            areas_by_key.get((int(floor_row.id), area_code))
            if floor_row is not None
            else None
        )
        floor = grouped.setdefault(
            floor_code,
            {
                "floor_code": floor_code,
                "floor_name": (
                    floor_row.floor_name if floor_row is not None else f"{floor_number}楼"
                ),
                "floor_number": floor_number,
                "areas": {},
            },
        )
        area = floor["areas"].setdefault(
            area_code,
            {
                "area_code": area_code,
                "area_name": area_row.area_name if area_row is not None else area_code,
                "task_location_count": 0,
                "mapped_location_count": 0,
            },
        )
        area["task_location_count"] += 1
        if group.get("map_status") == "mapped" and group.get("map_point"):
            area["mapped_location_count"] += 1

    floors: list[dict] = []
    for floor in sorted(
        grouped.values(),
        key=lambda item: (item["floor_number"], item["floor_code"]),
    ):
        map_floor = _delivery_pick_map_floor(db, floor_code=floor["floor_code"])
        areas = []
        for area in sorted(
            floor.pop("areas").values(), key=lambda item: item["area_code"]
        ):
            measured = bool(map_floor is not None and area["mapped_location_count"])
            area["map_status"] = "ready" if measured else "unmeasured"
            area["map_status_text"] = (
                "实测地图已建立" if measured else "未建立实测地图"
            )
            areas.append(area)
        floor["areas"] = areas
        floors.append(floor)
    return {
        "task_id": int(task.id),
        "snapshot_version": int(task.snapshot_version),
        "floors": floors,
        "text_only_groups": [
            {
                "key": group.get("key"),
                "label": group.get("label"),
                "map_status": group.get("map_status"),
                "reason": "未建立实测地图，只能按文字位置核对。",
            }
            for group in task_payload.get("location_groups") or []
            if group.get("map_status") != "mapped" or not group.get("map_point")
        ],
        "as_of": task_payload.get("as_of"),
        "read_only": True,
    }


@pick_router.get("/{task_id}/measured-map/floors/{floor_code}")
def get_delivery_pick_measured_map_area(
    task_id: int,
    floor_code: str,
    response: Response,
    area_code: str = Query(min_length=1, max_length=30),
    db: Session = Depends(get_db),
    user: User = Depends(_can_view_pick_tasks),
) -> dict:
    response.headers["Cache-Control"] = "private, no-store"
    task = _pick_task_for_user(db, task_id, user)
    task_payload, groups, floors_by_number, areas_by_key = (
        _delivery_pick_measured_map_context(db, task=task)
    )
    normalized_floor = floor_code.strip().upper()
    normalized_area = area_code.strip().upper()
    selected_groups = [
        group
        for group in groups
        if _delivery_pick_floor_code(
            int(group["warehouse_floor"]), floors_by_number
        )
        == normalized_floor
        and str(group.get("area_code") or "").strip().upper() == normalized_area
    ]
    if not selected_groups:
        raise HTTPException(
            status_code=404,
            detail="当前拿货任务不包含这个楼层和区域",
        )
    floor_number = int(selected_groups[0]["warehouse_floor"])
    floor_row = floors_by_number.get(floor_number)
    area_row = (
        areas_by_key.get((int(floor_row.id), normalized_area))
        if floor_row is not None
        else None
    )
    map_floor = _delivery_pick_map_floor(db, floor_code=normalized_floor)
    measured = bool(
        map_floor is not None
        and any(
            group.get("map_status") == "mapped" and group.get("map_point")
            for group in selected_groups
        )
    )
    group_payloads = []
    for group in selected_groups:
        group_payloads.append(
            {
                "key": group.get("key"),
                "recommended_sequence": group.get("recommended_sequence"),
                "label": group.get("label"),
                "source_type": group.get("source_type"),
                "location_id": group.get("location_id"),
                "location_code": group.get("location_code"),
                "location_name": group.get("location_name"),
                "pallet_code": group.get("pallet_code"),
                "total_pick_quantity": group.get("total_pick_quantity"),
                "geometry": group.get("map_point") if measured else None,
                "map_status": "ready" if measured and group.get("map_point") else "unmeasured",
                "map_status_text": (
                    "实测地图已建立"
                    if measured and group.get("map_point")
                    else "未建立实测地图"
                ),
                "updated_at": _delivery_pick_group_updated_at(task_payload, group),
                "lines": [
                    {
                        "pick_item_id": line.get("pick_item_id"),
                        "product_code": line.get("product_code"),
                        "product_name": line.get("product_name"),
                        "specification": line.get("specification"),
                        "lot_number": line.get("lot_number"),
                        "pick_quantity": line.get("pick_quantity"),
                        "unit": line.get("unit"),
                    }
                    for line in group.get("lines") or []
                ],
            }
        )
    return {
        "task_id": int(task.id),
        "snapshot_version": int(task.snapshot_version),
        "floor_code": normalized_floor,
        "floor_name": (
            floor_row.floor_name if floor_row is not None else f"{floor_number}楼"
        ),
        "area_code": normalized_area,
        "area_name": area_row.area_name if area_row is not None else normalized_area,
        "map_status": "ready" if measured else "unmeasured",
        "map_status_text": "实测地图已建立" if measured else "未建立实测地图",
        "guidance": (
            "橙色高亮为当前任务位置；到现场后核对库位、产品、批次和更新时间。"
            if measured
            else "未建立实测地图，只能按文字位置核对；系统不会生成假坐标或编号格子。"
        ),
        "bounds_mm": map_floor.get("bounds_mm") if map_floor else None,
        "features": _delivery_pick_map_features(
            map_floor,
            area_code=normalized_area,
        ),
        "groups": group_payloads,
        "as_of": task_payload.get("as_of"),
        "read_only": True,
    }


@pick_router.post("/{task_id}/complete-planned")
def complete_delivery_pick_task_as_planned(
    task_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_pick),
) -> dict:
    """Confirm the normal path in one action without dispatching the delivery."""

    task = _pick_task_for_user(db, task_id, user)
    if task.status == "driver_confirmed" and all(
        item.status == "picked"
        and int(item.picked_quantity or 0) == int(item.original_quantity or 0)
        for item in task.items
    ):
        return _pick_task_response(db, task)
    if task.status != "pushed":
        raise HTTPException(status_code=409, detail="当前拿货任务不能一键按计划拿齐")
    response = _pick_task_response(db, task)
    if not response["location_plan_complete"]:
        raise HTTPException(
            status_code=409,
            detail="存在未分配拿货来源，请按实际情况登记部分拿货或没货",
        )
    if not task.items:
        raise HTTPException(status_code=409, detail="拿货任务没有可确认明细")
    for item in task.items:
        item.status = "picked"
        item.picked_quantity = int(item.original_quantity or 0)
    task.status = "driver_confirmed"
    task.submitted_by = user.id
    task.submitted_at = _utc_now()
    _write_audit(
        db,
        user=user,
        action="COMPLETE_PICK_TASK_PLANNED",
        resource="DeliveryPickTask",
        entity_id=task.id,
        details={
            "delivery_id": task.delivery_id,
            "item_count": len(task.items),
            "location_group_count": len(response["location_groups"]),
            "status": task.status,
        },
        description="本单全部按库位计划拿齐，进入可发货打印",
    )
    db.commit()
    return _pick_task_response(db, task)


@pick_router.put("/{task_id}/items/{item_id}")
def update_delivery_pick_task_item(
    task_id: int,
    item_id: int,
    payload: DeliveryPickItemUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(can_pick),
) -> dict:
    task = _pick_task_for_user(db, task_id, user)
    if task.status not in {"pushed", "driver_confirmed", "exception"}:
        raise HTTPException(status_code=409, detail="当前拿货任务不能再更新")
    item = db.scalar(
        select(DeliveryPickTaskItem).where(
            DeliveryPickTaskItem.id == item_id,
            DeliveryPickTaskItem.task_id == task.id,
        )
    )
    if item is None:
        raise HTTPException(status_code=404, detail="拿货任务明细不存在")
    original = int(item.original_quantity)
    requested = payload.picked_quantity
    if payload.pick_status == "picked":
        quantity = original if requested is None else int(requested)
        if quantity < original:
            raise HTTPException(status_code=400, detail="完整拿货数量不能小于原送货数量")
    elif payload.pick_status == "partial":
        if requested is None or not 0 < int(requested) < original:
            raise HTTPException(status_code=400, detail="部分拿货数量必须大于0且小于原送货数量")
        quantity = int(requested)
    else:
        if requested not in {None, 0}:
            raise HTTPException(status_code=400, detail="无货状态的拿货数量必须为0")
        quantity = 0
    item.status = payload.pick_status
    item.picked_quantity = quantity
    if task.status in {"driver_confirmed", "exception"}:
        task.status = "pushed"
        task.submitted_at = None
        task.submitted_by = None
    _write_audit(
        db,
        user=user,
        action="UPDATE_PICK_ITEM",
        resource="DeliveryPickTaskItem",
        entity_id=item.id,
        details={"task_id": task.id, "status": item.status, "picked_quantity": quantity},
        description="更新送货拿货结果",
    )
    db.commit()
    return {
        "task": _pick_task_response(db, task),
        "item": _pick_item_response(db, item),
    }


@pick_router.post("/{task_id}/submit")
def submit_delivery_pick_task(
    task_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_pick),
) -> dict:
    task = _pick_task_for_user(db, task_id, user)
    if task.status not in {"pushed", "driver_confirmed", "exception"}:
        raise HTTPException(status_code=409, detail="当前拿货任务不能提交")
    if any(item.status == "pending" for item in task.items):
        raise HTTPException(status_code=400, detail="仍有未处理的拿货明细")
    has_exception = any(
        item.status in {"partial", "no_stock"}
        or int(item.picked_quantity) > int(item.original_quantity)
        for item in task.items
    )
    task.status = "exception" if has_exception else "driver_confirmed"
    task.submitted_by = user.id
    task.submitted_at = _utc_now()
    _write_audit(
        db,
        user=user,
        action="SUBMIT_PICK_TASK",
        resource="DeliveryPickTask",
        entity_id=task.id,
        details={"delivery_id": task.delivery_id, "status": task.status},
        description="司机提交送货拿货结果",
    )
    db.commit()
    return _pick_task_response(db, task)


@pick_router.post("/{task_id}/apply")
def apply_delivery_pick_task(
    task_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    task = _pick_task_for_user(db, task_id, user)
    if task.status not in {"driver_confirmed", "exception"}:
        raise HTTPException(status_code=409, detail="请先由司机提交拿货结果")
    delivery = _delivery_for_user(db, task.delivery_id, user)
    if delivery.status != "pending":
        raise HTTPException(status_code=409, detail="已发货送货单不能应用拿货结果")
    try:
        # Acquire the same delivery-row write claim used by dispatch without
        # changing business state.  This serializes apply vs dispatch: if
        # dispatch wins first, rowcount is zero; if apply wins, dispatch waits
        # and then reads the adjusted draft quantities.
        claimed = db.execute(
            update(Delivery)
            .where(Delivery.id == delivery.id, Delivery.status == "pending")
            .values(total_quantity=Delivery.total_quantity)
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:
            raise HTTPException(status_code=409, detail="送货单状态已变化，请刷新后重试")
        applied_changes: list[dict] = []
        for item in list(task.items):
            delivery_item = db.get(DeliveryItem, item.delivery_item_id)
            if (
                delivery_item is None
                or delivery_item.delivery_id != delivery.id
                or delivery_item.order_item_id != item.order_item_id
                or int(delivery_item.delivered_quantity) != int(item.original_quantity)
            ):
                raise HTTPException(status_code=409, detail="送货草稿已变更，请重新创建拿货任务")
            applied_changes.append(
                {
                    "task_item_id": item.id,
                    "delivery_item_id": item.delivery_item_id,
                    "order_item_id": item.order_item_id,
                    "product_code": item.product_code_snapshot,
                    "product_name": item.product_name_snapshot,
                    "original_quantity": int(item.original_quantity),
                    "picked_quantity": int(item.picked_quantity),
                    "pick_status": item.status,
                }
            )
            if int(item.picked_quantity) <= 0:
                db.delete(delivery_item)
            else:
                if delivery_item.source_type == "unordered_finished":
                    allocations = db.scalars(
                        select(UnorderedFinishedDeliveryAllocation)
                        .where(
                            UnorderedFinishedDeliveryAllocation.delivery_item_id
                            == delivery_item.id,
                            UnorderedFinishedDeliveryAllocation.status == "planned",
                        )
                        .order_by(UnorderedFinishedDeliveryAllocation.id)
                    ).all()
                    planned_total = sum(
                        int(allocation.planned_quantity or 0)
                        for allocation in allocations
                    )
                    picked_quantity = int(item.picked_quantity)
                    if picked_quantity > planned_total:
                        raise HTTPException(
                            status_code=409,
                            detail="无订单成品库存拿货数量不能超过草稿已选批次数量",
                        )
                    remaining = picked_quantity
                    for allocation in allocations:
                        planned = int(allocation.planned_quantity or 0)
                        if remaining <= 0:
                            db.delete(allocation)
                            continue
                        kept = min(planned, remaining)
                        allocation.planned_quantity = kept
                        remaining -= kept
                delivery_item.delivered_quantity = int(item.picked_quantity)
        db.flush()
        delivery.total_quantity = int(
            db.scalar(
                select(func.coalesce(func.sum(DeliveryItem.delivered_quantity), 0)).where(
                    DeliveryItem.delivery_id == delivery.id
                )
            )
            or 0
        )
        task.status = "applied"
        task.applied_by = user.id
        task.applied_at = _utc_now()
        _write_audit(
            db,
            user=user,
            action="APPLY_PICK_TASK",
            resource="DeliveryPickTask",
            entity_id=task.id,
            details={
                "delivery_id": delivery.id,
                "total_quantity": delivery.total_quantity,
                "item_count": len(task.items),
                "items": applied_changes,
            },
            description="应用送货拿货结果到本次送货明细",
        )
        db.commit()
        return _delivery_response(db, delivery.id)
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise


def _refresh_order_status(db: Session, order_id: int) -> None:
    items = db.scalars(
        select(OrderItem).where(OrderItem.order_id == order_id)
    ).all()
    if items and all(
        item.is_force_closed or item.delivered_quantity >= item.quantity
        for item in items
    ):
        new_status = "delivered"
    elif any(item.delivered_quantity > 0 for item in items):
        new_status = "partially_delivered"
    else:
        new_status = "pending_delivery"
    db.execute(
        update(Order).where(Order.id == order_id).values(status=new_status)
    )


def _collect_delivery_lines(
    db: Session,
    *,
    customer_id: int,
    lines: list[DeliveryLineCreate],
    user: User,
) -> tuple[list[tuple[OrderItem, DeliveryLineCreate]], int, list[dict]]:
    """校验送货明细并返回 (订单明细, 行) 列表、总数量、超送警告。

    不写入任何数据，供创建与编辑共用。已送数量在此阶段不变动，
    因此剩余可送量按订单明细当前 delivered_quantity 计算。
    """
    built: list[tuple[OrderItem, DeliveryLineCreate]] = []
    seen: set[int] = set()
    total_quantity = 0
    warnings: list[dict] = []
    for index, line in enumerate(lines, start=1):
        if line.order_item_id in seen:
            raise HTTPException(
                status_code=400,
                detail=f"第{index}条订单明细重复选择",
            )
        seen.add(line.order_item_id)
        if line.delivered_quantity <= 0:
            raise HTTPException(
                status_code=400,
                detail=f"第{index}条送货数量必须大于0",
            )
        row = db.execute(
            select(OrderItem, Order)
            .join(Order, Order.id == OrderItem.order_id)
            .where(OrderItem.id == line.order_item_id)
        ).one_or_none()
        if row is None:
            raise HTTPException(
                status_code=400,
                detail=f"第{index}条订单明细不存在",
            )
        order_item, order = row
        production_managed = _has_production_task(db, order_item.id)
        quantity_facts = _delivery_quantity_facts(db, order_item)
        remaining = quantity_facts["deliverable_quantity"]
        order_remaining = quantity_facts["order_remaining_quantity"]
        component_capacity = (
            None
            if production_managed
            else _received_telescoping_capacity(db, order_item.id)
        )
        component_remaining = (
            max(component_capacity - int(order_item.delivered_quantity or 0), 0)
            if component_capacity is not None
            else None
        )
        if order.customer_id != customer_id:
            raise HTTPException(
                status_code=400,
                detail=f"第{index}条订单明细不属于当前客户",
            )
        full_inventory_coverage = (
            False
            if production_managed
            else inventory_fully_covers_order_item(db, order_item.id)
        )
        if (
            component_remaining is not None
            and line.delivered_quantity > component_remaining
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"第{index}条天地盖盖/底成套可送数量不足，"
                    f"当前物理可送数量为 {component_remaining}"
                ),
            )
        if production_managed and (
            order_item.is_force_closed
            or remaining <= 0
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"第{index}条订单明细生产可送数量不足，"
                    f"当前可送数量为 {remaining}"
                ),
            )
        if (
            not production_managed
            and (
            (
                order_item.material_status != "received"
                and not full_inventory_coverage
                and _received_telescoping_capacity(db, order_item.id) is None
            )
            or order_item.is_force_closed
            )
        ):
            raise HTTPException(
                status_code=400,
                detail=f"第{index}条订单明细当前不可发货",
            )
        if line.delivered_quantity > remaining:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"第{index}条实际可送成品不足，当前最多可送 {remaining}"
                ),
            )
        over_delivery = max(line.delivered_quantity - order_remaining, 0)
        if over_delivery > 0:
            if not has_permission(user, "deliveries.over_delivery"):
                raise HTTPException(
                    status_code=403,
                    detail=f"第{index}条超订单送货需要超量送货权限",
                )
            warnings.append(
                {
                    "code": "OVER_DELIVERY",
                    "order_item_id": order_item.id,
                    "remaining_quantity": order_remaining,
                    "deliverable_quantity": remaining,
                    "delivered_quantity": line.delivered_quantity,
                    "excess_quantity": over_delivery,
                    "message": f"本次送货超过订单数量 {over_delivery}，已用黄色提醒",
                }
            )
        built.append((order_item, line))
        total_quantity += line.delivered_quantity
    return built, total_quantity, warnings


def _collect_unordered_finished_lines(
    db: Session,
    *,
    customer_id: int,
    lines: list[DeliveryLineCreate],
) -> tuple[list[dict], int]:
    built: list[dict] = []
    total_quantity = 0
    for index, line in enumerate(lines, start=1):
        product = db.get(Product, line.product_id)
        if (
            product is None
            or product.deleted_at is not None
            or not product.is_active
        ):
            raise HTTPException(
                status_code=409,
                detail=f"第 {index} 条产品不存在或已停用",
            )
        if product.customer_id != customer_id:
            raise HTTPException(
                status_code=409,
                detail=f"第 {index} 条产品不属于当前客户",
            )
        submitted_unit_price = (
            Decimal(str(line.unit_price)).quantize(Decimal("0.0001"))
            if line.unit_price is not None
            else None
        )
        if submitted_unit_price is not None and submitted_unit_price < 0:
            raise HTTPException(
                status_code=400,
                detail=f"第 {index} 条无订单库存明细单价不能小于零",
            )
        # 无订单库存可能在客户临时要货时尚未定价。显式 0 与空值都只代表
        # “待补价”，不得把 0 冻结成正式成交价或进入财务金额。
        unit_price = (
            submitted_unit_price
            if submitted_unit_price is not None and submitted_unit_price > 0
            else None
        )
        allocation_rows: list[dict] = []
        for planned in line.allocations:
            row = db.execute(
                select(
                    InventoryLot,
                    FinishedGoodsInventoryDetail,
                    WarehouseLocation,
                )
                .join(
                    FinishedGoodsInventoryDetail,
                    FinishedGoodsInventoryDetail.inventory_lot_id
                    == InventoryLot.id,
                )
                .join(
                    WarehouseLocation,
                    WarehouseLocation.id
                    == InventoryLot.warehouse_location_id,
                )
                .where(InventoryLot.id == planned.inventory_lot_id)
            ).one_or_none()
            if row is None:
                raise HTTPException(
                    status_code=409,
                    detail=f"第 {index} 条所选成品库存批次不存在",
                )
            lot, detail, location = row
            if (
                lot.inventory_type != "finished"
                or lot.status != "active"
                or int(lot.quantity_reserved or 0) != 0
                or int(lot.quantity_available or 0) < planned.quantity
                or detail.is_general
                or detail.owner_customer_id != customer_id
                or detail.product_id != product.id
            ):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"批次 {lot.lot_number} 已不可用于当前客户和产品，"
                        "请刷新库存后重试"
                    ),
                )
            pallet = lot.pallet_item.pallet if lot.pallet_item else None
            allocation_rows.append(
                {
                    "lot": lot,
                    "quantity": int(planned.quantity),
                    "location": location,
                    "pallet_code": pallet.pallet_code if pallet else None,
                }
            )
        built.append(
            {
                "product": product,
                "line": line,
                "unit_price": unit_price,
                "price_source": (
                    "pending"
                    if unit_price is None
                    else (
                        "product_default"
                        if product.sale_unit_price is not None
                        and Decimal(str(product.sale_unit_price)).quantize(
                            Decimal("0.0001")
                        )
                        == unit_price
                        else "manual"
                    )
                ),
                "allocations": allocation_rows,
            }
        )
        total_quantity += int(line.delivered_quantity)
    return built, total_quantity


def _pending_order_quantities_by_product_code(
    db: Session,
    *,
    customer_id: int,
) -> dict[str, int]:
    """Return authoritative currently deliverable order quantity by stock code."""

    rows = list(db.execute(_pending_query(customer_id=customer_id)))
    if not rows:
        return {}
    context = _PendingDeliveryReadContext(db, rows)
    totals: dict[str, int] = {}
    for row in rows:
        mapping = row._mapping
        order_item = context.order_item(mapping["order_item_id"])
        if order_item is None:
            continue
        quantity = context.remaining_quantity(db, order_item)
        code = str(mapping["product_code"] or "").strip().casefold()
        if quantity > 0 and code:
            totals[code] = totals.get(code, 0) + int(quantity)
    return totals


def _enforce_unordered_finished_order_priority(
    db: Session,
    *,
    customer_id: int,
    order_lines: list[tuple[OrderItem, DeliveryLineCreate]],
    unordered_lines: list[dict],
) -> None:
    """An unordered lot may supplement a stock code only after all order work."""

    if not unordered_lines:
        return
    product_ids = {int(item.product_id) for item, _line in order_lines}
    products = {
        int(product.id): product
        for product in db.scalars(select(Product).where(Product.id.in_(product_ids))).all()
    } if product_ids else {}
    selected_by_code: dict[str, int] = {}
    for order_item, line in order_lines:
        product = products.get(int(order_item.product_id))
        code = str(product.product_code if product is not None else "").strip().casefold()
        if code:
            selected_by_code[code] = selected_by_code.get(code, 0) + int(
                line.delivered_quantity or 0
            )
    pending_by_code = _pending_order_quantities_by_product_code(
        db,
        customer_id=customer_id,
    )
    for entry in unordered_lines:
        product: Product = entry["product"]
        code = str(product.product_code or "").strip().casefold()
        pending = int(pending_by_code.get(code, 0))
        selected = int(selected_by_code.get(code, 0))
        if pending > selected:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"存货编码 {product.product_code} 仍有订单待送 {pending}，"
                    f"本单只选择 {selected}；请先把订单待送数量全部加入，"
                    "不足部分才能使用无订单成品库存补量"
                ),
            )


def _store_unordered_finished_items(
    db: Session,
    *,
    delivery: Delivery,
    built: list[dict],
    user: User,
) -> None:
    for entry in built:
        product: Product = entry["product"]
        line: DeliveryLineCreate = entry["line"]
        delivery_item = DeliveryItem(
            delivery_id=delivery.id,
            source_type="unordered_finished",
            order_item_id=None,
            product_id=product.id,
            product_code_snapshot=product.product_code,
            product_name_snapshot=product.product_name,
            specification_snapshot=_product_specification(product),
            unit_snapshot=product.unit or "只",
            unit_price_snapshot=entry["unit_price"],
            price_source=entry["price_source"],
            delivered_quantity=int(line.delivered_quantity),
            ordered_quantity_snapshot=0,
            order_remaining_snapshot=0,
            over_delivery_quantity=0,
            remarks=(line.remarks or "").strip() or None,
        )
        db.add(delivery_item)
        db.flush()
        for planned in entry["allocations"]:
            lot: InventoryLot = planned["lot"]
            location: WarehouseLocation = planned["location"]
            db.add(
                UnorderedFinishedDeliveryAllocation(
                    delivery_item_id=delivery_item.id,
                    inventory_lot_id=lot.id,
                    planned_quantity=planned["quantity"],
                    consumed_quantity=0,
                    restored_quantity=0,
                    status="planned",
                    lot_number_snapshot=lot.lot_number,
                    warehouse_location_id_snapshot=location.id,
                    warehouse_location_code_snapshot=location.location_code,
                    pallet_code_snapshot=planned["pallet_code"],
                    created_by=user.id,
                )
            )


def _product_specification(product: Product) -> str | None:
    dimensions = [
        value
        for value in (product.length_mm, product.width_mm, product.height_mm)
        if value is not None
    ]
    if not dimensions:
        return None
    return "×".join(
        str(int(value)) if value == int(value) else str(value)
        for value in dimensions
    )


def _unordered_finished_customer_summaries(
    db: Session,
    *,
    user: User,
) -> list[dict]:
    """Return customers that own at least one currently selectable free lot.

    Keep this qualification aligned with ``unordered_finished_candidates`` so
    the delivery customer selector never advertises a customer whose inventory
    picker would immediately be empty.
    """

    active_reservation = exists(
        select(1).where(
            InventoryReservation.inventory_lot_id == InventoryLot.id,
            InventoryReservation.status != "cancelled",
            InventoryReservation.reserved_stock_quantity
            > (
                InventoryReservation.consumed_stock_quantity
                + InventoryReservation.released_stock_quantity
            ),
        )
    )
    query = (
        select(
            Customer.id.label("customer_id"),
            Customer.name.label("customer_name"),
            func.count(InventoryLot.id).label("lot_count"),
            func.coalesce(func.sum(InventoryLot.quantity_available), 0).label(
                "available_quantity"
            ),
        )
        .select_from(InventoryLot)
        .join(
            FinishedGoodsInventoryDetail,
            FinishedGoodsInventoryDetail.inventory_lot_id == InventoryLot.id,
        )
        .join(Product, Product.id == FinishedGoodsInventoryDetail.product_id)
        .join(
            WarehouseLocation,
            WarehouseLocation.id == InventoryLot.warehouse_location_id,
        )
        .join(Customer, Customer.id == FinishedGoodsInventoryDetail.owner_customer_id)
        .where(
            Customer.is_active.is_(True),
            InventoryLot.inventory_type == "finished",
            InventoryLot.status == "active",
            InventoryLot.quantity_available > 0,
            InventoryLot.quantity_reserved == 0,
            FinishedGoodsInventoryDetail.is_general.is_(False),
            Product.customer_id == Customer.id,
            Product.is_active.is_(True),
            Product.deleted_at.is_(None),
            ~active_reservation,
        )
        .group_by(Customer.id, Customer.name)
        .order_by(Customer.name, Customer.id)
    )
    visible_customer_ids = _visible_customer_ids(user, db)
    if visible_customer_ids is not None:
        query = query.where(Customer.id.in_(visible_customer_ids))
    return [
        {
            "customer_id": int(row.customer_id),
            "customer_name": row.customer_name,
            "lot_count": int(row.lot_count or 0),
            "available_quantity": int(row.available_quantity or 0),
        }
        for row in db.execute(query).all()
    ]


def _delivery_customer_candidates_from_pending_items(
    db: Session,
    *,
    user: User,
    pending_items: list[dict],
) -> list[dict]:
    """Merge order and free-stock sources without repeating the pending query."""

    grouped: dict[int, dict] = {}
    for item in pending_items:
        customer_id = int(item["customer_id"])
        candidate = grouped.setdefault(
            customer_id,
            {
                "customer_id": customer_id,
                "customer_name": item["customer_name"],
                "has_pending_orders": True,
                "pending_item_count": 0,
                "pending_quantity": 0,
                "has_unordered_finished": False,
                "unordered_lot_count": 0,
                "unordered_available_quantity": 0,
            },
        )
        candidate["pending_item_count"] += 1
        candidate["pending_quantity"] += int(
            item.get("deliverable_quantity")
            or item.get("remaining_quantity")
            or 0
        )

    for summary in _unordered_finished_customer_summaries(db, user=user):
        customer_id = int(summary["customer_id"])
        candidate = grouped.setdefault(
            customer_id,
            {
                "customer_id": customer_id,
                "customer_name": summary["customer_name"],
                "has_pending_orders": False,
                "pending_item_count": 0,
                "pending_quantity": 0,
                "has_unordered_finished": False,
                "unordered_lot_count": 0,
                "unordered_available_quantity": 0,
            },
        )
        candidate["has_unordered_finished"] = True
        candidate["unordered_lot_count"] = int(summary["lot_count"])
        candidate["unordered_available_quantity"] = int(
            summary["available_quantity"]
        )

    if grouped:
        active_customer_ids = set(
            db.scalars(
                select(Customer.id).where(
                    Customer.id.in_(grouped),
                    Customer.is_active.is_(True),
                )
            ).all()
        )
        grouped = {
            customer_id: candidate
            for customer_id, candidate in grouped.items()
            if customer_id in active_customer_ids
        }

    return sorted(
        grouped.values(),
        key=lambda row: (row["customer_name"], row["customer_id"]),
    )


def _delivery_customer_candidates_from_summaries(
    db: Session,
    *,
    user: User,
    pending_summaries: list[dict],
) -> list[dict]:
    """Build the delivery customer selector without expanding every order item."""

    grouped: dict[int, dict] = {}
    for summary in pending_summaries:
        customer_id = int(summary["customer_id"])
        grouped[customer_id] = {
            "customer_id": customer_id,
            "customer_name": summary["customer_name"],
            "has_pending_orders": True,
            "pending_item_count": int(summary.get("item_count") or 0),
            "pending_quantity": int(summary.get("pending_quantity") or 0),
            "has_unordered_finished": False,
            "unordered_lot_count": 0,
            "unordered_available_quantity": 0,
        }

    for summary in _unordered_finished_customer_summaries(db, user=user):
        customer_id = int(summary["customer_id"])
        candidate = grouped.setdefault(
            customer_id,
            {
                "customer_id": customer_id,
                "customer_name": summary["customer_name"],
                "has_pending_orders": False,
                "pending_item_count": 0,
                "pending_quantity": 0,
                "has_unordered_finished": False,
                "unordered_lot_count": 0,
                "unordered_available_quantity": 0,
            },
        )
        candidate["has_unordered_finished"] = True
        candidate["unordered_lot_count"] = int(summary["lot_count"])
        candidate["unordered_available_quantity"] = int(
            summary["available_quantity"]
        )

    return sorted(
        grouped.values(),
        key=lambda row: (row["customer_name"], row["customer_id"]),
    )


@router.get("/unordered-finished-candidates")
def unordered_finished_candidates(
    customer_id: int = Query(gt=0),
    q: str = Query(default="", max_length=150),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=12, ge=1, le=50),
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    require_customer_access(customer_id, user, db)
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="客户不存在")
    active_reservation = exists(
        select(1).where(
            InventoryReservation.inventory_lot_id == InventoryLot.id,
            InventoryReservation.status != "cancelled",
            InventoryReservation.reserved_stock_quantity
            > (
                InventoryReservation.consumed_stock_quantity
                + InventoryReservation.released_stock_quantity
            ),
        )
    )
    query = (
        select(
            InventoryLot,
            FinishedGoodsInventoryDetail,
            Product,
            WarehouseLocation,
        )
        .join(
            FinishedGoodsInventoryDetail,
            FinishedGoodsInventoryDetail.inventory_lot_id == InventoryLot.id,
        )
        .join(Product, Product.id == FinishedGoodsInventoryDetail.product_id)
        .join(
            WarehouseLocation,
            WarehouseLocation.id == InventoryLot.warehouse_location_id,
        )
        .where(
            InventoryLot.inventory_type == "finished",
            InventoryLot.status == "active",
            InventoryLot.quantity_available > 0,
            InventoryLot.quantity_reserved == 0,
            FinishedGoodsInventoryDetail.owner_customer_id == customer_id,
            FinishedGoodsInventoryDetail.is_general.is_(False),
            Product.customer_id == customer_id,
            Product.is_active.is_(True),
            Product.deleted_at.is_(None),
            ~active_reservation,
        )
    )
    keyword = q.strip()
    if keyword:
        fuzzy = f"%{keyword}%"
        query = query.where(
            or_(
                Product.product_code.like(fuzzy),
                Product.product_name.like(fuzzy),
                cast(Product.length_mm, String).like(fuzzy),
                cast(Product.width_mm, String).like(fuzzy),
                cast(Product.height_mm, String).like(fuzzy),
            )
        )
    query = query.order_by(Product.product_code, *inventory_fifo_order_columns())
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = db.execute(
        query.offset((page - 1) * page_size).limit(page_size)
    ).all()
    pending_by_code = _pending_order_quantities_by_product_code(
        db,
        customer_id=customer_id,
    )
    items: list[dict] = []
    for lot, detail, product, location in rows:
        pallet = lot.pallet_item.pallet if lot.pallet_item else None
        items.append(
            {
                "inventory_lot_id": lot.id,
                "inventory_lot_number": lot.lot_number,
                "lot_number": lot.lot_number,
                "inventory_version": lot.version,
                "product_id": product.id,
                "product_code": detail.inventory_code_snapshot
                or product.product_code,
                "product_name": detail.product_name_snapshot
                or product.product_name,
                "specification": _product_specification(product),
                "unit": product.unit,
                "owner_customer_id": customer_id,
                "customer_id": customer_id,
                "is_general": False,
                "available_quantity": int(lot.quantity_available or 0),
                "unit_price": (
                    str(product.sale_unit_price)
                    if product.sale_unit_price is not None
                    and product.sale_unit_price > 0
                    else None
                ),
                "price_required": not bool(
                    product.sale_unit_price is not None
                    and product.sale_unit_price > 0
                ),
                "location_id": location.id,
                "location_code": location.location_code,
                "location_name": location.location_name,
                "pallet_code": pallet.pallet_code if pallet else None,
                "order_pending_quantity": int(
                    pending_by_code.get(str(product.product_code or "").strip().casefold(), 0)
                ),
            }
        )
    return {
        "customer_id": customer_id,
        "items": items,
        "total": int(total),
        "page": page,
        "page_size": page_size,
        "total_pages": max((int(total) + page_size - 1) // page_size, 1),
    }


@router.get("/route-suggestions")
def delivery_route_suggestions(
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    raw_rows = list(
        db.execute(
            _pending_query(customer_ids=_visible_customer_ids(user, db))
        )
    )
    customer_ids = {
        int(row._mapping["customer_id"])
        for row in raw_rows
        if row._mapping["customer_id"] is not None
    }
    customers = {
        row.id: row
        for row in db.scalars(
            select(Customer).where(Customer.id.in_(customer_ids))
        ).all()
    }
    aggregate: dict[int, dict] = {}
    for row in raw_rows:
        mapping = row._mapping
        order_item = db.get(OrderItem, mapping["order_item_id"])
        remaining = _delivery_remaining_quantity(db, order_item) if order_item else 0
        if remaining <= 0:
            continue
        customer_id = int(mapping["customer_id"])
        customer = customers.get(customer_id)
        if customer is None:
            continue
        entry = aggregate.setdefault(
            customer_id,
            {
                "customer_id": customer_id,
                "customer_name": customer.name,
                "address": (customer.address or "").strip() or None,
                "contact_person": customer.contact_person,
                "phone": customer.phone,
                "pending_item_count": 0,
                "pending_quantity": 0,
                "earliest_delivery_date": None,
                "product_codes": set(),
            },
        )
        entry["pending_item_count"] += 1
        entry["pending_quantity"] += int(remaining)
        if mapping["product_code"]:
            entry["product_codes"].add(str(mapping["product_code"]))
        delivery_date = mapping["delivery_date"]
        if delivery_date and (
            entry["earliest_delivery_date"] is None
            or delivery_date < entry["earliest_delivery_date"]
        ):
            entry["earliest_delivery_date"] = delivery_date

    company = db.scalar(select(CompanyConfig).where(CompanyConfig.id == 1))
    origin_address = (company.address or "").strip() if company else ""
    origin_area, _origin_subarea = _delivery_route_area(origin_address)
    grouped: dict[str, list[dict]] = {}
    for entry in aggregate.values():
        area, subarea = _delivery_route_area(entry["address"])
        entry["area"] = area
        entry["subarea"] = subarea
        entry["product_codes"] = sorted(entry["product_codes"])
        grouped.setdefault(area, []).append(entry)

    groups = []
    for area, entries in grouped.items():
        entries.sort(
            key=lambda row: (
                row["subarea"] or "",
                row["earliest_delivery_date"] or date.max,
                row["customer_name"],
            )
        )
        previous_address = origin_address or None
        for sequence, entry in enumerate(entries, start=1):
            entry["sequence"] = sequence
            entry["navigation_from"] = previous_address
            is_same_address = bool(
                _normalized_delivery_address(previous_address)
                and _normalized_delivery_address(previous_address)
                == _normalized_delivery_address(entry["address"])
            )
            entry["same_as_previous_address"] = is_same_address
            if is_same_address:
                entry["navigation_url"] = None
                entry["navigation_note"] = "与上一站同地址，可同站处理"
            else:
                entry["navigation_url"] = _baidu_delivery_direction_url(
                    previous_address, entry["address"]
                )
                entry["navigation_note"] = (
                    None if entry["navigation_url"] else "缺少起点或地址"
                )
            if entry["address"]:
                previous_address = entry["address"]
        groups.append(
            {
                "area": area,
                "same_as_origin_area": bool(origin_address and area == origin_area),
                "customer_count": len(entries),
                "pending_item_count": sum(
                    row["pending_item_count"] for row in entries
                ),
                "pending_quantity": sum(row["pending_quantity"] for row in entries),
                "customers": entries,
            }
        )
    groups.sort(
        key=lambda group: (
            not group["same_as_origin_area"],
            min(
                (
                    row["earliest_delivery_date"] or date.max
                    for row in group["customers"]
                ),
                default=date.max,
            ),
            group["area"],
        )
    )
    return {
        "origin_address": origin_address or None,
        "origin_area": origin_area if origin_address else None,
        "customer_count": len(aggregate),
        "groups": groups,
        "planning_mode": "regional_grouping_with_segment_navigation",
        "disclaimer": (
            "这是按客户地址区域和交期生成的同车辅助建议，不是实时路况最优解；"
            "请在百度地图打开每一段后由司机确认最终顺序。"
        ),
    }


class _PendingDeliveryReadContext:
    """Request-scoped lookup for uncomplicated pending rows.

    Complex production, inventory and BOM rows continue through the existing
    workflow helpers.  A received-material row with none of those related
    facts has an exact, stable delivery formula: ordered minus delivered.
    Prefetching only the negative facts prevents a query-per-row cascade
    without using a stale process-wide cache for inventory availability.
    """

    def __init__(self, db: Session, rows: list[object]):
        item_ids = {
            int(row._mapping["order_item_id"])
            for row in rows
            if row._mapping["order_item_id"] is not None
        }
        order_ids = {
            int(row._mapping["order_id"])
            for row in rows
            if row._mapping["order_id"] is not None
        }
        self.order_items = {
            item.id: item
            for item in db.scalars(
                select(OrderItem).where(OrderItem.id.in_(item_ids))
            ).all()
        } if item_ids else {}
        self.orders = {
            order.id: order
            for order in db.scalars(select(Order).where(Order.id.in_(order_ids))).all()
        } if order_ids else {}
        if not item_ids:
            self.fast_item_ids: set[int] = set()
            return

        self.composite_ids = set(
            db.scalars(
                select(SalesOrderItemBomComponent.sales_order_item_id)
                .where(SalesOrderItemBomComponent.sales_order_item_id.in_(item_ids))
                .distinct()
            ).all()
        )
        self.composite_available_sets = kit_available_sets_by_order_item_ids(
            db,
            self.composite_ids,
        )
        tasks = list(
            db.scalars(
                select(ProductionTask).where(ProductionTask.order_item_id.in_(item_ids))
            ).all()
        )
        task_item_ids = {int(task.order_item_id) for task in tasks}
        regular_tasks = {
            int(task.order_item_id): task
            for task in tasks
            if task.sales_order_item_bom_component_id is None
        }
        reservation_item_ids = set(
            db.scalars(
                select(InventoryReservation.order_item_id)
                .where(InventoryReservation.order_item_id.in_(item_ids))
                .distinct()
            ).all()
        )
        semi_requirements = list(
            db.scalars(
                select(OrderItemSemiRequirement).where(
                    OrderItemSemiRequirement.order_item_id.in_(item_ids)
                )
            ).all()
        )
        semi_requirement_item_ids = {
            int(requirement.order_item_id) for requirement in semi_requirements
        }
        telescoping_rows = db.execute(
            select(
                RequisitionItem.order_item_id,
                RequisitionItem.product_name_snapshot,
                RequisitionItem.requisition_qty,
                RequisitionItem.status,
            ).where(RequisitionItem.order_item_id.in_(item_ids))
        ).all()
        telescoping: dict[int, dict[str, int | bool]] = {}
        for item_id, product_name, quantity, item_status in telescoping_rows:
            component = _component_kind(product_name)
            if not component:
                continue
            state = telescoping.setdefault(
                int(item_id), {"has": True, "base": 0, "cover": 0}
            )
            if item_status == "已入库":
                state[component] = int(state[component]) + int(quantity or 0)
        telescoping_item_ids = set(telescoping)
        excluded_ids = (
            self.composite_ids
            | task_item_ids
            | reservation_item_ids
            | semi_requirement_item_ids
            | telescoping_item_ids
        )
        self.fast_item_ids = {
            item_id
            for item_id, item in self.order_items.items()
            if item.material_status == "received" and item_id not in excluded_ids
        }
        completions_by_item: dict[int, list[ProductionCompletion]] = {}
        for completion in db.scalars(
            select(ProductionCompletion).where(
                ProductionCompletion.order_item_id.in_(item_ids),
                ProductionCompletion.status == "posted",
            )
        ).all():
            completions_by_item.setdefault(
                int(completion.order_item_id), []
            ).append(completion)
        reserved_by_item = active_finished_reservations_by_item_ids(
            db, sorted(item_ids)
        )
        semi_credited_by_requirement: dict[int, int] = {}
        semi_requirement_ids = [requirement.id for requirement in semi_requirements]
        if semi_requirement_ids:
            for reservation in db.scalars(
                select(InventoryReservation).where(
                    InventoryReservation.semi_requirement_id.in_(
                        semi_requirement_ids
                    ),
                    InventoryReservation.reservation_type == "semi_order",
                    InventoryReservation.status != "cancelled",
                )
            ).all():
                requirement_id = int(reservation.semi_requirement_id)
                credited = max(
                    int(reservation.credited_requirement_quantity or 0)
                    - int(reservation.released_requirement_quantity or 0),
                    0,
                )
                semi_credited_by_requirement[requirement_id] = (
                    semi_credited_by_requirement.get(requirement_id, 0)
                    + credited
                )
        product_styles = {
            int(product_id): str(box_style or "")
            for product_id, box_style in db.execute(
                select(Product.id, Product.box_style).where(
                    Product.id.in_(
                        {
                            int(item.product_id)
                            for item in self.order_items.values()
                        }
                    )
                )
            ).all()
        }
        requirements_by_item: dict[int, dict[str, OrderItemSemiRequirement]] = {}
        for requirement in semi_requirements:
            requirements_by_item.setdefault(
                int(requirement.order_item_id), {}
            )[requirement.component_type] = requirement
        semi_fully_covered_ids: set[int] = set()
        for item_id, requirements in requirements_by_item.items():
            item = self.order_items.get(item_id)
            if item is None:
                continue
            box_style = product_styles.get(int(item.product_id), "")
            expected_components = (
                {"cover", "base"}
                if "天地盖" in box_style or "A3" in box_style.upper()
                else {"whole"}
            )
            if not expected_components.issubset(requirements):
                continue
            production_boxes = max(
                int(item.quantity or 0)
                - int(reserved_by_item.get(item_id, 0)),
                0,
            )
            if all(
                semi_credited_by_requirement.get(requirements[component].id, 0)
                >= production_boxes
                * max(int(requirements[component].pieces_per_box or 1), 1)
                for component in expected_components
            ):
                semi_fully_covered_ids.add(item_id)
        self.remaining_by_item: dict[int, int] = {}
        for item_id, item in self.order_items.items():
            if item_id in self.composite_ids:
                continue
            delivered = max(int(item.delivered_quantity or 0), 0)
            task = regular_tasks.get(item_id)
            if task is not None:
                if task.status not in {"completed", "not_required"}:
                    self.remaining_by_item[item_id] = 0
                    continue
                completions = completions_by_item.get(item_id, [])
                if completions:
                    ready_quantity = max(
                        int(task.finished_coverage_snapshot or 0)
                        + sum(
                            normalized_completion_output(item, completion)
                            for completion in completions
                        ),
                        0,
                    )
                else:
                    ready_quantity = max(int(reserved_by_item.get(item_id, 0)), 0)
                self.remaining_by_item[item_id] = max(
                    ready_quantity - delivered, 0
                )
                continue

            max_deliverable = max(int(item.quantity or 0), 0)
            component_state = telescoping.get(item_id)
            if component_state is not None:
                max_deliverable = min(
                    max_deliverable,
                    int(component_state["base"]),
                    int(component_state["cover"]),
                )
            elif (
                item.material_status != "received"
                and int(reserved_by_item.get(item_id, 0)) < max_deliverable
                and item_id not in semi_fully_covered_ids
            ):
                self.remaining_by_item[item_id] = 0
                continue
            self.remaining_by_item[item_id] = max(
                max_deliverable - delivered, 0
            )

    def order(self, order_id: int) -> Order | None:
        return self.orders.get(int(order_id))

    def order_item(self, order_item_id: int) -> OrderItem | None:
        return self.order_items.get(int(order_item_id))

    def is_fast(self, order_item: OrderItem | None) -> bool:
        return bool(order_item and order_item.id in self.fast_item_ids)

    def remaining_quantity(self, db: Session, order_item: OrderItem) -> int:
        if order_item.id in self.composite_ids:
            return max(
                int(self.composite_available_sets.get(order_item.id, 0)),
                0,
            )
        return max(int(self.remaining_by_item.get(order_item.id, 0)), 0)


def _pending_delivery_item_payload(
    db: Session,
    *,
    row,
    registry,
    context: _PendingDeliveryReadContext,
    include_material_display: bool = False,
) -> dict | None:
    """Build the legacy response, avoiding duplicate SQL for the safe path."""

    mapping = row._mapping
    order = context.order(mapping["order_id"])
    order_item = context.order_item(mapping["order_item_id"])
    if order_item is None:
        return None
    display = display_order_number(order, registry) if order is not None else mapping["order_number"]
    if context.is_fast(order_item):
        ordered = max(int(order_item.quantity or 0), 0)
        delivered = max(int(order_item.delivered_quantity or 0), 0)
        remaining_quantity = max(ordered - delivered, 0)
        if remaining_quantity <= 0:
            return None
        quantity_facts = {
            "ordered_quantity": ordered,
            "delivered_quantity": delivered,
            "order_remaining_quantity": remaining_quantity,
            "deliverable_quantity": remaining_quantity,
            "over_delivery_quantity": 0,
            "surplus_finished_quantity": 0,
        }
        kit_metadata = {
            "is_composite_bom": False,
            "kit_availability": None,
            "available_sets": None,
            "missing_components": [],
            "component_lines": [],
        }
        inventory_sources: list[dict] = []
    else:
        remaining_quantity = context.remaining_quantity(db, order_item)
        if remaining_quantity <= 0:
            return None
        ordered = max(int(order_item.quantity or 0), 0)
        delivered = max(int(order_item.delivered_quantity or 0), 0)
        order_remaining = max(ordered - delivered, 0)
        quantity_facts = {
            "ordered_quantity": ordered,
            "delivered_quantity": delivered,
            "order_remaining_quantity": order_remaining,
            "deliverable_quantity": remaining_quantity,
            "over_delivery_quantity": max(
                remaining_quantity - order_remaining, 0
            ),
            "surplus_finished_quantity": max(
                remaining_quantity - order_remaining, 0
            ),
        }
        kit_metadata = (
            _delivery_kit_metadata(db, order_item)
            if order_item.id in context.composite_ids
            else {
                "is_composite_bom": False,
                "kit_availability": None,
                "available_sets": None,
                "missing_components": [],
                "component_lines": [],
            }
        )
        inventory_sources = _inventory_sources_for_order_item(
            db,
            order_item=order_item,
            planned_delivery_quantity=remaining_quantity,
        )

    payload = {
        **dict(mapping),
        "remaining_quantity": remaining_quantity,
        **quantity_facts,
        "order_number": display,
        "display_order_number": display,
        **kit_metadata,
        "inventory_sources": inventory_sources,
    }
    if include_material_display:
        material = (mapping["material"] or "").strip()
        flute_type = (mapping["flute_type"] or "").strip()
        payload["material_display"] = (
            f"{material} / {flute_type}" if material and flute_type else material
        )
    return payload


@router.get("/pending_items")
def pending_delivery_items(
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    registry = build_display_registry(db)
    rows = list(
        db.execute(_pending_query(customer_ids=_visible_customer_ids(user, db)))
    )
    context = _PendingDeliveryReadContext(db, rows)
    items = []
    for row in rows:
        payload = _pending_delivery_item_payload(
            db, row=row, registry=registry, context=context
        )
        if payload is not None:
            items.append(payload)
    return {
        "items": items,
        "customer_candidates": _delivery_customer_candidates_from_pending_items(
            db,
            user=user,
            pending_items=items,
        ),
    }


def pending_delivery_customer_summaries(
    db: Session,
    *,
    user: User,
) -> list[dict]:
    """Return the exact pending-delivery customer set without item payload N+1.

    Qualification reuses ``_pending_query`` and the same request-scoped
    remaining-quantity context as ``pending_delivery_items``.  The dashboard
    needs customer identities and counts only, so inventory source and BOM
    display payloads are deliberately not expanded here.
    """

    rows = list(db.execute(_pending_query(customer_ids=_visible_customer_ids(user, db))))
    context = _PendingDeliveryReadContext(db, rows)
    grouped: dict[int, dict] = {}
    for row in rows:
        mapping = row._mapping
        order_item = context.order_item(mapping["order_item_id"])
        if order_item is None:
            continue
        remaining_quantity = context.remaining_quantity(db, order_item)
        if remaining_quantity <= 0:
            continue
        customer_id = int(mapping["customer_id"])
        group = grouped.setdefault(
            customer_id,
            {
                "customer_id": customer_id,
                "customer_name": mapping["customer_name"],
                "item_count": 0,
                "pending_quantity": 0,
                "order_item_ids": [],
                "delivery_date": mapping["delivery_date"],
            },
        )
        group["item_count"] += 1
        group["pending_quantity"] += int(remaining_quantity)
        group["order_item_ids"].append(int(mapping["order_item_id"]))
        candidate_date = mapping["delivery_date"]
        if candidate_date is not None and (
            group["delivery_date"] is None
            or candidate_date < group["delivery_date"]
        ):
            group["delivery_date"] = candidate_date
    return sorted(
        grouped.values(),
        key=lambda row: (
            row["delivery_date"] is None,
            row["delivery_date"] or date.max,
            row["customer_name"],
            row["customer_id"],
        ),
    )


@router.get("/pending-customer-options")
def pending_delivery_customer_options(
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    """Return only customers that currently have a real selectable source."""

    summaries = pending_delivery_customer_summaries(db, user=user)
    return {
        "items": _delivery_customer_candidates_from_summaries(
            db,
            user=user,
            pending_summaries=summaries,
        )
    }


@router.get("/pending-items/search")
def search_pending_delivery_items(
    customer_id: int = Query(gt=0),
    inventory_code: str = Query(default="", max_length=150),
    q: str = Query(default="", max_length=150),
    customer_po: str = Query(default="", max_length=150),
    product_name: str = Query(default="", max_length=150),
    search_type: str = Query(default="", max_length=50),
    list_all: bool = False,
    limit: int | None = Query(default=None, ge=1, le=200),
    page: int = Query(default=1, ge=1),
    page_size: int | None = Query(default=None, ge=1, le=50),
    db: Session = Depends(get_db),
    _user: User = Depends(can_operate),
) -> dict:
    require_customer_access(customer_id, _user, db)
    inventory_keyword = inventory_code.strip()
    general_keyword = q.strip()
    customer_po_keyword = customer_po.strip()
    product_name_keyword = product_name.strip()
    search_type = search_type.strip().lower()

    if search_type:
        allowed_search_types = {
            "inventory_code",
            "customer_po",
            "product_name",
        }
        if search_type not in allowed_search_types:
            raise HTTPException(status_code=400, detail="search_type 参数无效")
        if general_keyword:
            if search_type == "inventory_code":
                inventory_keyword = inventory_keyword or general_keyword
            elif search_type == "customer_po":
                customer_po_keyword = customer_po_keyword or general_keyword
            else:
                product_name_keyword = product_name_keyword or general_keyword
            general_keyword = ""

    if not list_all and not any(
        [
            inventory_keyword,
            general_keyword,
            customer_po_keyword,
            product_name_keyword,
        ]
    ):
        return {"items": [], "total": 0, "page": page, "page_size": page_size or 20, "total_pages": 1}
    if db.get(Customer, customer_id) is None:
        raise HTTPException(status_code=400, detail="客户不存在")
    load_all = list_all and page_size is None and limit is None
    effective_limit = page_size or limit or (100 if list_all else 20)
    registry = build_display_registry(db)
    base_query = _pending_query(
        customer_id=customer_id,
        inventory_keyword=inventory_keyword,
        customer_po_keyword=customer_po_keyword,
        product_name_keyword=product_name_keyword,
        general_keyword=general_keyword,
    )
    total = db.scalar(select(func.count()).select_from(base_query.subquery())) or 0
    if load_all:
        rows = list(db.execute(base_query))
        effective_limit = max(int(total), 1)
        page = 1
    else:
        query_limit = min(effective_limit * 3, 200)
        offset = 0 if list_all and page_size is None else (page - 1) * effective_limit
        rows = list(db.execute(base_query.offset(offset).limit(query_limit)))
    context = _PendingDeliveryReadContext(db, rows)
    items = []
    for row in rows:
        payload = _pending_delivery_item_payload(
            db,
            row=row,
            registry=registry,
            context=context,
            include_material_display=True,
        )
        if payload is not None:
            items.append(payload)
        if len(items) >= effective_limit:
            break
    return {
        "items": items,
        "total": int(total),
        "page": page,
        "page_size": effective_limit,
        "total_pages": (
            1
            if load_all
            else max((int(total) + effective_limit - 1) // effective_limit, 1)
        ),
    }


@router.get("")
def list_deliveries(
    customer_id: int | None = None,
    customer_ids: list[int] | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    statuses: list[str] | None = Query(default=None),
    keyword: str | None = Query(default=None, max_length=200),
    delivery_no: str | None = None,
    order_no: str | None = None,
    customer_po: str | None = None,
    product_code: str | None = None,
    product_name: str | None = None,
    spec: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    return_status: list[str] | None = Query(default=None),
    view: Literal["full", "summary"] = Query(default="full"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    # New delivery drafts must stay at the top. Printing or dispatching an old
    # delivery must not move it ahead of a delivery that was just created.
    query = select(Delivery.id)

    # Apply the customer's visibility boundary before any document or product
    # filter.  A direct filter request for an out-of-scope customer keeps the
    # existing endpoint's explicit 403 behaviour instead of silently revealing
    # whether that customer has deliveries.
    requested_customer_ids = {
        int(value) for value in (customer_ids or [])
    }
    if customer_id is not None:
        requested_customer_ids.add(customer_id)
    if requested_customer_ids:
        for requested_customer_id in requested_customer_ids:
            require_customer_access(requested_customer_id, user, db)
        query = query.where(Delivery.customer_id.in_(requested_customer_ids))
    else:
        visible_customer_ids = _visible_customer_ids(user, db)
        if visible_customer_ids is not None:
            query = query.where(Delivery.customer_id.in_(visible_customer_ids))

    normalized_statuses = [
        value.strip() for value in (statuses or []) if value and value.strip()
    ]
    if normalized_statuses:
        query = query.where(Delivery.status.in_(normalized_statuses))
    elif status_filter:
        query = query.where(Delivery.status == status_filter)
    else:
        query = query.where(Delivery.status != "voided")

    def _contains(column, value: str | None):
        normalized = (value or "").strip().lower()
        return func.lower(column).like(f"%{normalized}%") if normalized else None

    delivery_number_filter = _contains(Delivery.delivery_number, delivery_no)
    if delivery_number_filter is not None:
        query = query.where(delivery_number_filter)
    normalized_keyword = (keyword or "").strip().lower()
    if normalized_keyword:
        pattern = f"%{normalized_keyword}%"
        customer_keyword_match = exists(
            select(1).where(
                Customer.id == Delivery.customer_id,
                or_(
                    func.lower(Customer.name).like(pattern),
                    func.lower(Customer.customer_code).like(pattern),
                ),
            )
        )
        item_keyword_match = exists(
            select(1)
            .select_from(DeliveryItem)
            .outerjoin(OrderItem, OrderItem.id == DeliveryItem.order_item_id)
            .outerjoin(Order, Order.id == OrderItem.order_id)
            .outerjoin(
                Product,
                Product.id
                == func.coalesce(DeliveryItem.product_id, OrderItem.product_id),
            )
            .where(
                DeliveryItem.delivery_id == Delivery.id,
                or_(
                    func.lower(Order.order_number).like(pattern),
                    func.lower(Order.customer_po).like(pattern),
                    func.lower(
                        func.coalesce(
                            func.nullif(DeliveryItem.product_code_snapshot, ""),
                            func.nullif(OrderItem.snapshot_product_code, ""),
                            Product.product_code,
                        )
                    ).like(pattern),
                    func.lower(
                        func.coalesce(
                            func.nullif(DeliveryItem.product_name_snapshot, ""),
                            func.nullif(OrderItem.snapshot_product_name, ""),
                            Product.product_name,
                        )
                    ).like(pattern),
                    func.lower(
                        func.coalesce(
                            func.nullif(DeliveryItem.specification_snapshot, ""),
                            OrderItem.snapshot_spec,
                        )
                    ).like(pattern),
                ),
            )
        )
        query = query.where(
            or_(
                func.lower(Delivery.delivery_number).like(pattern),
                customer_keyword_match,
                item_keyword_match,
            )
        )
    if date_from is not None:
        query = query.where(Delivery.delivery_date >= date_from)
    if date_to is not None:
        query = query.where(Delivery.delivery_date <= date_to)

    item_filters = [
        condition
        for condition in (
            _contains(Order.order_number, order_no),
            _contains(Order.customer_po, customer_po),
            _contains(Product.product_code, product_code),
            _contains(OrderItem.snapshot_product_name, product_name),
            _contains(OrderItem.snapshot_spec, spec),
        )
        if condition is not None
    ]
    if item_filters:
        query = query.where(
            exists(
                select(1)
                .select_from(DeliveryItem)
                .join(OrderItem, OrderItem.id == DeliveryItem.order_item_id)
                .join(Order, Order.id == OrderItem.order_id)
                .join(Product, Product.id == OrderItem.product_id)
                .where(
                    DeliveryItem.delivery_id == Delivery.id,
                    *item_filters,
                )
            )
        )

    normalized_return_statuses = {
        value.strip() for value in (return_status or []) if value and value.strip()
    }
    if normalized_return_statuses:
        return_conditions = []
        confirmed_receipt_exists = exists(
            select(1).where(
                ReturnReceipt.delivery_id == Delivery.id,
                ReturnReceipt.status == "confirmed",
            )
        )
        for receipt_status in normalized_return_statuses:
            if receipt_status in {"waiting", "waiting_receipt", "pending"}:
                # A cancelled receipt is not an effective receipt.  The delivery
                # therefore remains actionable in the same waiting set used by
                # the dashboard until a confirmed receipt exists.
                return_conditions.append(~confirmed_receipt_exists)
            else:
                return_conditions.append(
                    exists(
                        select(1).where(
                            ReturnReceipt.delivery_id == Delivery.id,
                            ReturnReceipt.status == receipt_status,
                        )
                    )
                )
        query = query.where(or_(*return_conditions))

    query = query.order_by(
        Delivery.created_at.desc(),
        Delivery.id.desc(),
    )
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    delivery_ids = db.scalars(
        query.offset((page - 1) * page_size).limit(page_size)
    ).all()
    if view == "summary":
        summary_context = _delivery_list_summary_context(db, list(delivery_ids))
        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "view": "summary",
            "items": [
                _delivery_summary_response(delivery_id, context=summary_context)
                for delivery_id in delivery_ids
            ],
        }
    list_context = _delivery_list_page_context(db, list(delivery_ids))
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [
            _delivery_response(db, delivery_id, list_context=list_context)
            for delivery_id in delivery_ids
        ],
    }


@router.post("", status_code=status.HTTP_201_CREATED)
def create_delivery(
    payload: DeliveryCreate,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    require_customer_access(payload.customer_id, user, db)
    try:
        _validate_delivery_source_contract(payload.source_mode, payload.items)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    customer = db.get(Customer, payload.customer_id)
    if customer is None:
        raise HTTPException(status_code=400, detail="客户不存在")
    delivery_date = payload.delivery_date or beijing_today()
    try:
        delivery = Delivery(
            delivery_number=next_delivery_number(
                db,
                customer=customer,
                delivery_date=delivery_date,
            ),
            customer_id=payload.customer_id,
            delivery_date=delivery_date,
            vehicle_number=(payload.vehicle_number or "").strip() or None,
            source_mode=payload.source_mode,
            status="pending",
            total_quantity=0,
            created_by=user.id,
        )
        db.add(delivery)
        db.flush()
        warnings: list[dict] = []
        order_payload_lines = [
            line for line in payload.items if line.source_type == "order"
        ]
        unordered_payload_lines = [
            line for line in payload.items if line.source_type == "unordered_finished"
        ]
        built: list[tuple[OrderItem, DeliveryLineCreate]] = []
        built_unordered: list[dict] = []
        order_quantity = 0
        unordered_quantity = 0
        if order_payload_lines:
            built, order_quantity, warnings = _collect_delivery_lines(
                db,
                customer_id=payload.customer_id,
                lines=order_payload_lines,
                user=user,
            )
        if unordered_payload_lines:
            built_unordered, total_quantity = _collect_unordered_finished_lines(
                db,
                customer_id=payload.customer_id,
                lines=unordered_payload_lines,
            )
            unordered_quantity = total_quantity
            _enforce_unordered_finished_order_priority(
                db,
                customer_id=payload.customer_id,
                order_lines=built,
                unordered_lines=built_unordered,
            )
            _store_unordered_finished_items(
                db,
                delivery=delivery,
                built=built_unordered,
                user=user,
            )
        for order_item, line in built:
            order_remaining = max(
                int(order_item.quantity or 0)
                - int(order_item.delivered_quantity or 0),
                0,
            )
            over_delivery = max(line.delivered_quantity - order_remaining, 0)
            db.add(
                DeliveryItem(
                    delivery_id=delivery.id,
                    source_type="order",
                    order_item_id=order_item.id,
                    **build_order_delivery_snapshot(db, order_item),
                    delivered_quantity=line.delivered_quantity,
                    ordered_quantity_snapshot=int(order_item.quantity or 0),
                    order_remaining_snapshot=order_remaining,
                    over_delivery_quantity=over_delivery,
                    over_delivery_confirmed_by=(
                        user.id if over_delivery > 0 else None
                    ),
                    over_delivery_reason=(
                        (line.over_delivery_reason or "").strip() or None
                    ),
                    remarks=(line.remarks or "").strip() or None,
                )
            )
        total_quantity = order_quantity + unordered_quantity
        delivery.total_quantity = total_quantity
        _write_audit(
            db,
            user=user,
            action="CREATE",
            resource="Delivery",
            entity_id=delivery.id,
            details={
                "delivery_number": delivery.delivery_number,
                "source_mode": delivery.source_mode,
                "item_count": len(payload.items),
                "total_quantity": total_quantity,
            },
            description="创建待发货送货单",
        )
        db.commit()
        response = _delivery_response(db, delivery.id)
        response["warnings"] = warnings
        return response
    except DeliveryNumberingError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(error)) from error
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="送货单数据冲突") from error
    except Exception:
        db.rollback()
        raise


@router.put("/{delivery_id}/dispatch")
def dispatch_delivery(
    delivery_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    delivery = _delivery_for_user(db, delivery_id, user)
    dispatched_at = _utc_now()
    try:
        pick_task = _delivery_pick_task(db, delivery_id)
        if pick_task and pick_task.status == "exception":
            raise HTTPException(
                status_code=409,
                detail="拿货任务存在异常，请先在电脑端应用拿货结果后再发货",
            )
        order_ids = (
            []
            if delivery.source_mode == "unordered_finished"
            else list(
                db.scalars(
                    select(OrderItem.order_id)
                    .join(DeliveryItem, DeliveryItem.order_item_id == OrderItem.id)
                    .where(DeliveryItem.delivery_id == delivery_id)
                    .distinct()
                    .order_by(OrderItem.order_id)
                ).all()
            )
        )
        if order_ids:
            try:
                # Global transition order: Order -> Delivery -> OrderItem.  Workflow
                # rollback also starts from Order before touching Delivery rows.
                lock_order_rows_for_production_transition(db, order_ids)
            except ProductionWorkflowError as error:
                raise HTTPException(
                    status_code=error.status_code,
                    detail=str(error),
                ) from error
        claimed = db.execute(
            update(Delivery)
            .where(
                Delivery.id == delivery_id,
                Delivery.status == "pending",
            )
            .values(
                status="dispatched",
                dispatched_by=user.id,
                dispatched_at=dispatched_at,
                ever_dispatched_at=func.coalesce(
                    Delivery.ever_dispatched_at,
                    dispatched_at,
                ),
            )
        )
        if claimed.rowcount != 1:
            exists = db.scalar(
                select(Delivery.id).where(Delivery.id == delivery_id)
            )
            if exists is None:
                raise HTTPException(status_code=404, detail="送货单不存在")
            existing_status = db.scalar(
                select(Delivery.status).where(Delivery.id == delivery_id)
            )
            if existing_status == "voided":
                raise HTTPException(
                    status_code=409,
                    detail="送货单已作废，不能再次确认发货",
                )
            raise HTTPException(status_code=409, detail="送货单已确认发货")

        lines = db.scalars(
            select(DeliveryItem)
            .where(DeliveryItem.delivery_id == delivery_id)
            .order_by(DeliveryItem.id)
        ).all()
        if not lines:
            raise HTTPException(
                status_code=409,
                detail="本次送货单已无可发货明细，请删除送货草稿或重新编辑",
            )
        order_lines = [line for line in lines if line.source_type == "order"]
        unordered_lines = [
            line for line in lines if line.source_type == "unordered_finished"
        ]
        if delivery.source_mode == "mixed" and (
            not order_lines or not unordered_lines
        ):
            raise HTTPException(
                status_code=409,
                detail="混合送货单来源不完整，请重新编辑后再发货",
            )
        if unordered_lines:
            priority_order_lines: list[tuple[OrderItem, DeliveryItem]] = []
            for line in order_lines:
                order_item = db.get(OrderItem, line.order_item_id)
                if order_item is None:
                    raise HTTPException(
                        status_code=409,
                        detail=f"订单明细{line.order_item_id}不存在",
                    )
                priority_order_lines.append((order_item, line))
            priority_unordered_lines: list[dict] = []
            for line in unordered_lines:
                product = db.get(Product, line.product_id)
                if product is None:
                    raise HTTPException(
                        status_code=409,
                        detail="无订单库存送货产品不存在，请刷新后重试",
                    )
                priority_unordered_lines.append({"product": product, "line": line})
            _enforce_unordered_finished_order_priority(
                db,
                customer_id=delivery.customer_id,
                order_lines=priority_order_lines,
                unordered_lines=priority_unordered_lines,
            )
        if delivery.source_mode == "unordered_finished":
            if any(
                line.source_type != "unordered_finished"
                or line.order_item_id is not None
                for line in lines
            ):
                raise HTTPException(
                    status_code=409,
                    detail="无订单成品库存送货单来源异常，禁止发货",
                )
            dispatch_unordered_finished_inventory(
                db,
                delivery=delivery,
                delivery_items=lines,
                operator_id=user.id,
                dispatched_at=dispatched_at,
            )
            released_pallet_ids = release_empty_pallets_after_delivery(
                db,
                delivery_id=delivery_id,
                operator_id=user.id,
            )
            if pick_task is not None:
                pick_task.status = "dispatched"
                pick_task.dispatched_at = dispatched_at
            _write_audit(
                db,
                user=user,
                action="DISPATCH",
                resource="Delivery",
                entity_id=delivery_id,
                details={
                    "source_mode": "unordered_finished",
                    "dispatched_at": dispatched_at,
                    "item_count": len(lines),
                    "total_quantity": sum(
                        int(line.delivered_quantity or 0) for line in lines
                    ),
                    "released_pallet_ids": released_pallet_ids,
                },
                description="确认无订单客户专用成品正式发货",
            )
            db.commit()
            return _delivery_response(db, delivery_id)
        current_order_ids = set(
            db.scalars(
                select(OrderItem.order_id)
                .where(OrderItem.id.in_([line.order_item_id for line in lines]))
                .distinct()
            ).all()
        )
        if not current_order_ids.issubset(set(order_ids)):
            raise HTTPException(
                status_code=409,
                detail="送货单明细已变化，请刷新后重新确认发货",
            )
        affected_order_ids: set[int] = set()
        for line in order_lines:
            order_item = db.get(OrderItem, line.order_item_id)
            if order_item is None:
                raise HTTPException(
                    status_code=409,
                    detail=f"订单明细{line.order_item_id}不存在",
                )
            production_managed = _has_production_task(db, order_item.id)
            if production_managed:
                delivered_before = int(order_item.delivered_quantity or 0)
                locked = db.execute(
                    update(OrderItem)
                    .where(
                        OrderItem.id == order_item.id,
                        OrderItem.delivered_quantity == delivered_before,
                    )
                    .values(delivered_quantity=OrderItem.delivered_quantity)
                    .execution_options(synchronize_session=False)
                )
                if locked.rowcount != 1:
                    raise HTTPException(
                        status_code=409,
                        detail=f"订单明细{line.order_item_id}状态已变化，请刷新后重试",
                    )
                db.flush()
                db.expire(order_item)
                order_item = db.get(OrderItem, line.order_item_id)
                if order_item is None:
                    raise HTTPException(
                        status_code=409,
                        detail=f"订单明细{line.order_item_id}不存在",
                    )
                production_managed = _has_production_task(db, order_item.id)
            remaining = _delivery_remaining_quantity(db, order_item)
            if production_managed and (
                order_item.is_force_closed
                or remaining <= 0
            ):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"订单明细{line.order_item_id}生产可送数量不足，"
                        f"当前可送数量为 {remaining}"
                    ),
                )
            if line.delivered_quantity > remaining:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"订单明细{line.order_item_id}实际可送成品不足，"
                        f"当前最多可送 {remaining}"
                    ),
                )
            order_remaining_before = max(
                int(order_item.quantity or 0)
                - int(order_item.delivered_quantity or 0),
                0,
            )
            over_delivery = max(
                int(line.delivered_quantity) - order_remaining_before,
                0,
            )
            if over_delivery > 0:
                if not has_permission(user, "deliveries.over_delivery"):
                    raise HTTPException(
                        status_code=403,
                        detail="当前账号没有超量送货权限",
                    )
                if line.over_delivery_confirmed_by is None:
                    line.over_delivery_confirmed_by = user.id
            line.ordered_quantity_snapshot = int(order_item.quantity or 0)
            line.order_remaining_snapshot = order_remaining_before
            line.over_delivery_quantity = over_delivery
            ensure_order_delivery_snapshot(db, line, order_item)
            component_capacity = (
                None
                if production_managed
                else _received_telescoping_capacity(db, order_item.id)
            )
            component_remaining = (
                max(component_capacity - int(order_item.delivered_quantity or 0), 0)
                if component_capacity is not None
                else None
            )
            if (
                component_remaining is not None
                and line.delivered_quantity > component_remaining
            ):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"订单明细{line.order_item_id}天地盖盖/底成套可送数量不足，"
                        f"当前物理可送数量为 {component_remaining}"
                    ),
                )
            order_id = order_item.order_id
            delivered_before = int(order_item.delivered_quantity or 0)
            operation_key = (
                f"d{delivery_id}-{dispatched_at:%Y%m%d%H%M%S%f}-i{line.id}"
            )
            if is_composite_order_item(db, order_item.id):
                execute_delivery_component_consumption(
                    db,
                    delivery_item_id=line.id,
                    delivery_sets=line.delivered_quantity,
                    operator_id=user.id,
                    operation_key=operation_key,
                )
            else:
                consume_delivery_item_inventory(
                    db,
                    delivery_item_id=line.id,
                    delivered_quantity_after_dispatch=(
                        delivered_before + line.delivered_quantity
                    ),
                    operator_id=user.id,
                    operation_key=operation_key,
                )
            result = db.execute(
                update(OrderItem)
                .where(
                    OrderItem.id == line.order_item_id,
                    OrderItem.is_force_closed.is_(False),
                    OrderItem.delivered_quantity == delivered_before,
                )
                .values(
                    delivered_quantity=(
                        OrderItem.delivered_quantity + line.delivered_quantity
                    )
                )
            )
            if result.rowcount != 1:
                raise HTTPException(
                    status_code=409,
                    detail=f"订单明细{line.order_item_id}状态或可发数量已变化",
                )
            if order_id is not None:
                affected_order_ids.add(order_id)
        if unordered_lines:
            dispatch_unordered_finished_inventory(
                db,
                delivery=delivery,
                delivery_items=unordered_lines,
                operator_id=user.id,
                dispatched_at=dispatched_at,
            )
        released_pallet_ids = release_empty_pallets_after_delivery(
            db,
            delivery_id=delivery_id,
            operator_id=user.id,
        )
        for order_id in affected_order_ids:
            _refresh_order_status(db, order_id)
        if pick_task is not None:
            pick_task.status = "dispatched"
            pick_task.dispatched_at = dispatched_at
        _write_audit(
            db,
            user=user,
            action="DISPATCH",
            resource="Delivery",
            entity_id=delivery_id,
            details={
                "dispatched_at": dispatched_at,
                "item_count": len(lines),
                "source_mode": delivery.source_mode,
                "unordered_item_count": len(unordered_lines),
                "released_pallet_ids": released_pallet_ids,
                "over_delivery_quantity": sum(
                    int(line.over_delivery_quantity or 0) for line in lines
                ),
                "over_delivery_items": [
                    {
                        "delivery_item_id": line.id,
                        "order_item_id": line.order_item_id,
                        "ordered_quantity_snapshot": line.ordered_quantity_snapshot,
                        "actual_delivery_quantity": line.delivered_quantity,
                        "over_delivery_quantity": line.over_delivery_quantity,
                        "confirmed_by": line.over_delivery_confirmed_by,
                        "reason": line.over_delivery_reason,
                    }
                    for line in lines
                    if int(line.over_delivery_quantity or 0) > 0
                ],
            },
            description="确认送货单发货",
        )
        db.commit()
        return _delivery_response(db, delivery_id)
    except HTTPException:
        db.rollback()
        raise
    except WarehouseInventoryError as error:
        db.rollback()
        raise HTTPException(
            status_code=error.status_code,
            detail=str(error),
        ) from error
    except CompositeBomWorkflowError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(error)) from error
    except Exception:
        db.rollback()
        raise


@router.put("/{delivery_id}")
def update_delivery(
    delivery_id: int,
    payload: DeliveryUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    _delivery_for_user(db, delivery_id, user)
    try:
        _validate_delivery_source_contract(payload.source_mode, payload.items)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    try:
        # 先以条件写取得 SQLite 写锁。这样编辑与发货并发时，
        # 只有先取得 pending 状态的一方继续，避免按过期状态替换明细。
        claimed = db.execute(
            update(Delivery)
            .where(
                Delivery.id == delivery_id,
                Delivery.status == "pending",
            )
            .values(status="pending")
        )
        if claimed.rowcount != 1:
            existing_status = db.scalar(
                select(Delivery.status).where(Delivery.id == delivery_id)
            )
            if existing_status is None:
                raise HTTPException(status_code=404, detail="送货单不存在")
            if existing_status == "voided":
                raise HTTPException(
                    status_code=409,
                    detail="送货单已作废，不能编辑",
                )
            raise HTTPException(
                status_code=409,
                detail="送货单已确认发货，不能编辑，请先取消发货",
            )
        delivery = _delivery_or_404(db, delivery_id)
        if payload.source_mode != delivery.source_mode:
            raise HTTPException(
                status_code=409,
                detail="送货单保存后不能切换订单待送与无订单库存来源",
            )
        _discard_delivery_pick_task(
            db,
            delivery_id=delivery_id,
            user=user,
            reason="delivery_draft_updated",
        )
        warnings: list[dict] = []
        order_payload_lines = [
            line for line in payload.items if line.source_type == "order"
        ]
        unordered_payload_lines = [
            line for line in payload.items if line.source_type == "unordered_finished"
        ]
        built: list[tuple[OrderItem, DeliveryLineCreate]] = []
        built_unordered: list[dict] = []
        order_quantity = 0
        unordered_quantity = 0
        if order_payload_lines:
            built, order_quantity, warnings = _collect_delivery_lines(
                db,
                customer_id=delivery.customer_id,
                lines=order_payload_lines,
                user=user,
            )
        if unordered_payload_lines:
            built_unordered, total_quantity = _collect_unordered_finished_lines(
                db,
                customer_id=delivery.customer_id,
                lines=unordered_payload_lines,
            )
            unordered_quantity = total_quantity
            _enforce_unordered_finished_order_priority(
                db,
                customer_id=delivery.customer_id,
                order_lines=built,
                unordered_lines=built_unordered,
            )
        if delivery.source_mode in {"unordered_finished", "mixed"}:
            delivery_item_ids = select(DeliveryItem.id).where(
                DeliveryItem.delivery_id == delivery_id
            )
            db.execute(
                delete(UnorderedFinishedDeliveryAllocation).where(
                    UnorderedFinishedDeliveryAllocation.delivery_item_id.in_(
                        delivery_item_ids
                    )
                )
            )
        db.execute(
            delete(DeliveryItem).where(DeliveryItem.delivery_id == delivery_id)
        )
        db.flush()
        if built_unordered:
            _store_unordered_finished_items(
                db,
                delivery=delivery,
                built=built_unordered,
                user=user,
            )
        for order_item, line in built:
            order_remaining = max(
                int(order_item.quantity or 0)
                - int(order_item.delivered_quantity or 0),
                0,
            )
            over_delivery = max(line.delivered_quantity - order_remaining, 0)
            db.add(
                DeliveryItem(
                    delivery_id=delivery.id,
                    source_type="order",
                    order_item_id=order_item.id,
                    **build_order_delivery_snapshot(db, order_item),
                    delivered_quantity=line.delivered_quantity,
                    ordered_quantity_snapshot=int(order_item.quantity or 0),
                    order_remaining_snapshot=order_remaining,
                    over_delivery_quantity=over_delivery,
                    over_delivery_confirmed_by=(
                        user.id if over_delivery > 0 else None
                    ),
                    over_delivery_reason=(
                        (line.over_delivery_reason or "").strip() or None
                    ),
                    remarks=(line.remarks or "").strip() or None,
                )
            )
        total_quantity = order_quantity + unordered_quantity
        if payload.delivery_date is not None:
            delivery.delivery_date = payload.delivery_date
        if payload.vehicle_number is not None:
            delivery.vehicle_number = payload.vehicle_number.strip() or None
        delivery.total_quantity = total_quantity
        _write_audit(
            db,
            user=user,
            action="UPDATE",
            resource="Delivery",
            entity_id=delivery.id,
            details={
                "delivery_number": delivery.delivery_number,
                "source_mode": delivery.source_mode,
                "item_count": len(payload.items),
                "total_quantity": total_quantity,
            },
            description="编辑待发货送货单",
        )
        db.commit()
        response = _delivery_response(db, delivery.id)
        response["warnings"] = warnings
        return response
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="送货单数据冲突") from error
    except Exception:
        db.rollback()
        raise


@router.delete("/{delivery_id}")
def delete_delivery(
    delivery_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    delivery = db.get(Delivery, delivery_id)
    if delivery is None:
        return {
            "deleted": True,
            "voided": False,
            "disposition": "deleted",
            "id": delivery_id,
        }
    require_customer_access(delivery.customer_id, user, db)
    try:
        if delivery.status == "voided":
            return {
                "deleted": False,
                "voided": True,
                "disposition": "voided",
                "id": delivery_id,
            }
        # 与确认发货竞争时先锁定 pending 状态，防止发货累计数量后
        # 送货单又被按旧状态删除。
        claimed = db.execute(
            update(Delivery)
            .where(
                Delivery.id == delivery_id,
                Delivery.status == "pending",
            )
            .values(status="pending")
        )
        if claimed.rowcount != 1:
            existing_status = db.scalar(
                select(Delivery.status).where(Delivery.id == delivery_id)
            )
            if existing_status is None:
                db.rollback()
                return {
                    "deleted": True,
                    "voided": False,
                    "disposition": "deleted",
                    "id": delivery_id,
                }
            if existing_status == "voided":
                return {
                    "deleted": False,
                    "voided": True,
                    "disposition": "voided",
                    "id": delivery_id,
                }
            raise HTTPException(
                status_code=409,
                detail="已确认发货的送货单不能删除，请改用取消发货",
            )
        delivery = _delivery_or_404(db, delivery_id)
        facts = _delivery_deletion_facts(db, delivery)
        if facts["statement_item_id"] is not None:
            raise HTTPException(
                status_code=409,
                detail="送货单已进入对账，不能删除或作废；请先反审核对账",
            )
        receipt = facts["receipt"]
        if receipt is not None and receipt.status == "confirmed":
            raise HTTPException(
                status_code=409,
                detail="送货单已有有效回单，不能删除或作废；请先撤销回单",
            )
        if any(
            row.status != "reversed"
            or int(row.reversed_stock_quantity or 0)
            != int(row.consumed_stock_quantity or 0)
            or int(row.reversed_requirement_quantity or 0)
            != int(row.credited_requirement_quantity or 0)
            for row in facts["inventory_allocations"]
        ):
            raise HTTPException(
                status_code=409,
                detail="送货单库存抵扣尚未全部冲回，不能作废；请先取消发货并刷新后重试",
            )
        if any(
            row.status != "reversed"
            or int(row.reversed_quantity or 0)
            != int(row.consumed_quantity or 0)
            for row in facts["component_allocations"]
        ):
            raise HTTPException(
                status_code=409,
                detail="送货单组件成品抵扣尚未全部冲回，不能作废；请先取消发货并刷新后重试",
            )
        if any(
            int(row.restored_quantity or 0)
            != int(row.consumed_quantity or 0)
            for row in facts["unordered_allocations"]
        ):
            raise HTTPException(
                status_code=409,
                detail="无订单成品送货库存尚未全部退回原批次，不能作废",
            )
        _discard_delivery_pick_task(
            db,
            delivery_id=delivery_id,
            user=user,
            reason=(
                "delivery_voided_after_cancel"
                if facts["has_history"]
                else "送货草稿被删除"
            ),
        )
        if facts["has_history"]:
            voided_at = _utc_now()
            voided = db.execute(
                update(Delivery)
                .where(
                    Delivery.id == delivery_id,
                    Delivery.status == "pending",
                )
                .values(
                    status="voided",
                    voided_by=user.id,
                    voided_at=voided_at,
                    ever_dispatched_at=func.coalesce(
                        Delivery.ever_dispatched_at,
                        facts["history_at"],
                        voided_at,
                    ),
                    printed_by=None,
                    printed_at=None,
                )
            )
            if voided.rowcount != 1:
                current_status = db.scalar(
                    select(Delivery.status).where(Delivery.id == delivery_id)
                )
                if current_status == "voided":
                    db.rollback()
                    return {
                        "deleted": False,
                        "voided": True,
                        "disposition": "voided",
                        "id": delivery_id,
                    }
                raise HTTPException(
                    status_code=409,
                    detail="送货单状态已变化，请刷新后重试",
                )
            _write_audit(
                db,
                user=user,
                action="VOID_AFTER_CANCEL",
                resource="Delivery",
                entity_id=delivery.id,
                details={
                    "delivery_number": delivery.delivery_number,
                    "total_quantity": delivery.total_quantity,
                    "inventory_allocation_count": len(
                        facts["inventory_allocations"]
                    ),
                    "component_allocation_count": len(
                        facts["component_allocations"]
                    ),
                    "unordered_allocation_count": len(
                        facts["unordered_allocations"]
                    ),
                    "return_receipt_status": (
                        receipt.status if receipt is not None else None
                    ),
                },
                description="作废已取消发货的送货单并保留库存及审计记录",
            )
            db.commit()
            return {
                "deleted": False,
                "voided": True,
                "disposition": "voided",
                "id": delivery_id,
            }
        _write_audit(
            db,
            user=user,
            action="DELETE",
            resource="Delivery",
            entity_id=delivery.id,
            details={
                "delivery_number": delivery.delivery_number,
                "total_quantity": delivery.total_quantity,
            },
            description="删除待发货送货单",
        )
        if delivery.source_mode in {"unordered_finished", "mixed"}:
            delivery_item_ids = select(DeliveryItem.id).where(
                DeliveryItem.delivery_id == delivery.id
            )
            db.execute(
                delete(UnorderedFinishedDeliveryAllocation).where(
                    UnorderedFinishedDeliveryAllocation.delivery_item_id.in_(
                        delivery_item_ids
                    )
                )
            )
        db.delete(delivery)
        db.commit()
        return {
            "deleted": True,
            "voided": False,
            "disposition": "deleted",
            "id": delivery_id,
        }
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=(
                "送货单仍有关联业务记录，不能物理删除；"
                "请刷新后确认回单、对账、库存抵扣或组件抵扣状态"
            ),
        ) from error
    except Exception:
        db.rollback()
        raise


@router.put("/{delivery_id}/cancel")
def cancel_delivery(
    delivery_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    from app.models.finance import ReturnReceipt, ReturnReceiptItem, StatementItem

    _delivery_for_user(db, delivery_id, user)
    cancelled_at = _utc_now()
    try:
        # 先原子抢占 dispatched -> pending 并取得写锁，再检查回单/对账。
        # 若门禁不通过，整个事务 rollback，状态仍保持 dispatched。
        claimed = db.execute(
            update(Delivery)
            .where(
                Delivery.id == delivery_id,
                Delivery.status == "dispatched",
            )
            .values(
                status="pending",
                dispatched_by=None,
                dispatched_at=None,
                printed_by=None,
                printed_at=None,
            )
        )
        if claimed.rowcount != 1:
            existing_status = db.scalar(
                select(Delivery.status).where(Delivery.id == delivery_id)
            )
            if existing_status is None:
                raise HTTPException(status_code=404, detail="送货单不存在")
            if existing_status == "voided":
                raise HTTPException(
                    status_code=409,
                    detail="送货单已作废，不能取消发货",
                )
            raise HTTPException(
                status_code=409,
                detail="送货单未确认发货，无需取消",
            )
        delivery = _delivery_or_404(db, delivery_id)
        # 门禁 1（优先）：已进入对账 -> 必须先反审核对账，本阶段不开放
        statement_linked = db.scalar(
            select(StatementItem.id)
            .join(
                ReturnReceiptItem,
                ReturnReceiptItem.id == StatementItem.return_receipt_item_id,
            )
            .join(
                ReturnReceipt,
                ReturnReceipt.id == ReturnReceiptItem.return_receipt_id,
            )
            .where(ReturnReceipt.delivery_id == delivery_id)
            .limit(1)
        )
        if statement_linked is not None:
            raise HTTPException(
                status_code=409,
                detail="送货单已进入对账，必须先反审核对账，本阶段不支持取消发货",
            )
        # 门禁 2：已有有效回单 -> 不能取消发货
        receipt = db.scalar(
            select(ReturnReceipt).where(ReturnReceipt.delivery_id == delivery_id)
        )
        if receipt is not None and receipt.status == "confirmed":
            raise HTTPException(
                status_code=409,
                detail="送货单已有回单，请先撤销回单后再取消发货",
            )
        lines = db.scalars(
            select(DeliveryItem)
            .where(DeliveryItem.delivery_id == delivery_id)
            .order_by(DeliveryItem.id)
        ).all()
        order_lines = [line for line in lines if line.source_type == "order"]
        unordered_lines = [
            line for line in lines if line.source_type == "unordered_finished"
        ]
        if unordered_lines:
            cancel_unordered_finished_dispatch(
                db,
                delivery=delivery,
                delivery_items=unordered_lines,
                operator_id=user.id,
            )
        if delivery.source_mode == "unordered_finished":
            restored_pallet_ids = restore_auto_released_pallets_after_delivery_cancel(
                db,
                delivery_id=delivery_id,
                operator_id=user.id,
            )
            # Once an unordered-stock dispatch has been reversed, its immutable
            # allocation/reversal audit must remain attached to the original
            # document.  Archive it immediately instead of presenting a
            # misleading editable pending draft that cannot safely reuse those
            # allocations.
            delivery.status = "voided"
            delivery.voided_by = user.id
            delivery.voided_at = cancelled_at
            _discard_delivery_pick_task(
                db,
                delivery_id=delivery_id,
                user=user,
                reason="unordered_finished_delivery_dispatch_cancelled",
            )
            _write_audit(
                db,
                user=user,
                action="CANCEL_DISPATCH",
                resource="Delivery",
                entity_id=delivery_id,
                details={
                    "source_mode": "unordered_finished",
                    "delivery_number": delivery.delivery_number,
                    "item_count": len(lines),
                    "restored_quantity": delivery.total_quantity,
                    "disposition": "voided_after_dispatch_cancel",
                    "restored_pallet_ids": restored_pallet_ids,
                },
                description="取消无订单成品送货、退回原库存批次并归档",
            )
            db.commit()
            return _delivery_response(db, delivery_id)
        affected_order_ids: set[int] = set()
        for line in order_lines:
            order_item = db.get(OrderItem, line.order_item_id)
            if order_item is None:
                raise HTTPException(
                    status_code=409,
                    detail=f"订单明细{line.order_item_id}不存在，无法回滚",
                )
            order_id = order_item.order_id
            delivered_before = int(order_item.delivered_quantity or 0)
            if delivered_before < line.delivered_quantity:
                raise HTTPException(
                    status_code=409,
                    detail=f"订单明细{line.order_item_id}已送数量异常，无法回滚",
                )
            delivered_after = delivered_before - line.delivered_quantity
            operation_key = (
                f"c{delivery_id}-{cancelled_at:%Y%m%d%H%M%S%f}-i{line.id}"
            )
            if is_composite_order_item(db, order_item.id):
                reverse_delivery_component_allocations(
                    db,
                    delivery_item_id=line.id,
                    operator_id=user.id,
                    operation_key=operation_key,
                )
            else:
                reverse_delivery_item_inventory(
                    db,
                    delivery_item_id=line.id,
                    delivered_quantity_after_cancel=delivered_after,
                    operator_id=user.id,
                    operation_key=operation_key,
                )
            result = db.execute(
                update(OrderItem)
                .where(
                    OrderItem.id == line.order_item_id,
                    OrderItem.delivered_quantity == delivered_before,
                )
                .values(delivered_quantity=delivered_after)
            )
            if result.rowcount != 1:
                raise HTTPException(
                    status_code=409,
                    detail=f"订单明细{line.order_item_id}已送数量异常，无法回滚",
                )
            if order_id is not None:
                affected_order_ids.add(order_id)
        for order_id in affected_order_ids:
            _refresh_order_status(db, order_id)
        restored_pallet_ids = restore_auto_released_pallets_after_delivery_cancel(
            db,
            delivery_id=delivery_id,
            operator_id=user.id,
        )
        _discard_delivery_pick_task(
            db,
            delivery_id=delivery_id,
            user=user,
            reason="delivery_dispatch_cancelled",
        )
        if unordered_lines:
            # Mixed documents contain immutable unordered-lot reversal history.
            # Archive the whole document after both source branches are reversed.
            delivery.status = "voided"
            delivery.voided_by = user.id
            delivery.voided_at = cancelled_at
        _write_audit(
            db,
            user=user,
            action="CANCEL_DISPATCH",
            resource="Delivery",
            entity_id=delivery_id,
            details={
                "delivery_number": delivery.delivery_number,
                "item_count": len(lines),
                "restored_quantity": delivery.total_quantity,
                "source_mode": delivery.source_mode,
                "restored_pallet_ids": restored_pallet_ids,
                "disposition": (
                    "voided_after_dispatch_cancel" if unordered_lines else "pending"
                ),
            },
            description=(
                "取消混合送货、回滚订单已送数量、退回原库存批次并归档"
                if unordered_lines
                else "取消送货单发货并回滚已送数量"
            ),
        )
        db.commit()
        return _delivery_response(db, delivery_id)
    except HTTPException:
        db.rollback()
        raise
    except WarehouseInventoryError as error:
        db.rollback()
        raise HTTPException(
            status_code=error.status_code,
            detail=str(error),
        ) from error
    except CompositeBomWorkflowError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(error)) from error
    except Exception:
        db.rollback()
        raise


@router.put("/{delivery_id}/printed")
def mark_delivery_printed(
    delivery_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    _delivery_for_user(db, delivery_id, user)
    printed_at = _utc_now()
    try:
        updated = db.execute(
            update(Delivery)
            .where(
                Delivery.id == delivery_id,
                Delivery.status == "dispatched",
            )
            .values(
                printed_at=printed_at,
                printed_by=user.id,
            )
        )
        if updated.rowcount != 1:
            existing_status = db.scalar(
                select(Delivery.status).where(Delivery.id == delivery_id)
            )
            if existing_status is None:
                raise HTTPException(status_code=404, detail="送货单不存在")
            if existing_status == "voided":
                raise HTTPException(
                    status_code=409,
                    detail="送货单已作废，不能打印",
                )
            raise HTTPException(status_code=409, detail="送货单尚未确认发货")
        _write_audit(
            db,
            user=user,
            action="PRINT_DELIVERY",
            resource="Delivery",
            entity_id=delivery_id,
            details={"printed_at": printed_at},
            description="打印送货单",
        )
        db.commit()
        return _delivery_response(db, delivery_id)
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise


@order_actions_router.put("/items/{item_id}/force_close")
def force_close_order_item(
    item_id: int,
    payload: ForceCloseRequest,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    reason = payload.reason or "订单未送尾数强制结案（系统记录）"
    item = db.get(OrderItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="订单明细不存在")
    _require_order_item_customer_access(db, item.id, user)
    try:
        lock_order_rows_for_production_transition(db, [item.order_id])
    except ProductionWorkflowError as error:
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    item = db.scalar(
        select(OrderItem)
        .where(OrderItem.id == item_id)
        .execution_options(populate_existing=True)
    )
    if item is None:
        raise HTTPException(status_code=409, detail="订单明细已被删除，请刷新后重试")
    if item.delivered_quantity >= item.quantity:
        raise HTTPException(status_code=409, detail="订单明细已全部发货")
    if _has_active_production_stock_reservation(db, item.id):
        raise HTTPException(
            status_code=409,
            detail="该明细仍有生产完工成品库存预占，请先完成送货或库存处理后再结案",
        )
    try:
        result = db.execute(
            update(OrderItem)
            .where(
                OrderItem.id == item_id,
                OrderItem.is_force_closed.is_(False),
                OrderItem.delivered_quantity < OrderItem.quantity,
            )
            .values(is_force_closed=True)
        )
        if result.rowcount != 1:
            raise HTTPException(status_code=409, detail="订单明细已结案")
        _refresh_order_status(db, item.order_id)
        _write_audit(
            db,
            user=user,
            action="FORCE_CLOSE_ORDER_ITEM",
            resource="OrderItem",
            entity_id=item_id,
            details={
                "reason": reason,
                "ordered_quantity": item.quantity,
                "delivered_quantity": item.delivered_quantity,
            },
            description="缺货尾数强制结案",
        )
        db.commit()
        return {
            "item_id": item_id,
            "is_force_closed": True,
            "reason": reason,
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise


@router.get("/{delivery_id}")
def get_delivery(
    delivery_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    _delivery_for_user(db, delivery_id, user)
    return _delivery_response(db, delivery_id)


@router.get("/{delivery_id}/print")
def get_delivery_print_data(
    delivery_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    delivery = _delivery_for_user(db, delivery_id, user)
    if delivery.status == "voided":
        raise HTTPException(
            status_code=409,
            detail="送货单已作废，不能打印",
        )
    customer = db.get(Customer, delivery.customer_id)
    company = db.scalar(select(CompanyConfig).where(CompanyConfig.id == 1))
    rows = db.execute(
        select(
            DeliveryItem.id.label("delivery_item_id"),
            DeliveryItem.source_type,
            DeliveryItem.order_item_id,
            case(
                (DeliveryItem.source_type == "unordered_finished", "无订单库存"),
                else_=Order.customer_po,
            ).label("customer_po"),
            func.coalesce(
                DeliveryItem.product_code_snapshot,
                OrderItem.snapshot_product_code,
            ).label("product_code"),
            func.coalesce(
                DeliveryItem.product_name_snapshot,
                OrderItem.snapshot_product_name,
            ).label("product_name"),
            func.coalesce(
                DeliveryItem.specification_snapshot,
                OrderItem.snapshot_spec,
            ).label("specification"),
            DeliveryItem.unit_snapshot,
            DeliveryItem.delivered_quantity.label("quantity"),
            DeliveryItem.ordered_quantity_snapshot,
            DeliveryItem.over_delivery_quantity,
            DeliveryItem.remarks,
        )
        .outerjoin(OrderItem, OrderItem.id == DeliveryItem.order_item_id)
        .outerjoin(Order, Order.id == OrderItem.order_id)
        .outerjoin(
            Product,
            Product.id
            == func.coalesce(DeliveryItem.product_id, OrderItem.product_id),
        )
        .where(DeliveryItem.delivery_id == delivery_id)
        .order_by(DeliveryItem.id)
    ).all()
    internal_remarks = _tianhua_internal_remarks_by_delivery_item(
        db,
        [
            row.delivery_item_id
            for row in rows
            if str(row.remarks or "").strip().startswith(
                "来源：天华预送货草稿 "
            )
        ],
    )
    print_items: list[dict] = []
    actual_goods_items: list[dict] = []
    for row in rows:
        product_code = (
            str(row.product_code or "").strip() or "存货编码未登记"
        )
        product_name = (
            str(row.product_name or "").strip() or "产品名称未登记"
        )
        specification = (
            str(row.specification or "").strip() or "规格未登记"
        )
        is_unordered = row.source_type == "unordered_finished"
        order_item = (
            db.get(OrderItem, row.order_item_id)
            if row.order_item_id is not None
            else None
        )
        kit_metadata = _delivery_kit_metadata(
            db,
            order_item,
            planned_delivery_quantity=row.quantity,
            delivery_item_id=row.delivery_item_id,
            dispatched=delivery.status == "dispatched",
        )
        actual_goods_lines = (
            [
                {
                    "line_type": "parent",
                    "order_item_id": None,
                    "component_snapshot_id": None,
                    "product_code": product_code,
                    "product_name": product_name,
                    "specification": specification,
                    "unit": row.unit_snapshot or "PCS",
                    "quantity": int(row.quantity or 0),
                    "pricing_included": True,
                    "independent_return_receipt": True,
                    "independent_statement": True,
                }
            ]
            if is_unordered
            else _actual_goods_lines(
                order_item_id=row.order_item_id,
                product_code=_print_product_code(product_code),
                product_name=product_name,
                specification=specification,
                parent_quantity=row.quantity,
                component_lines=kit_metadata["component_lines"],
            )
        )
        document_goods_lines = [
            {
                **line,
                "delivery_item_id": row.delivery_item_id,
                "customer_po": row.customer_po,
                "product_code": _print_product_code(line["product_code"]),
            }
            for line in actual_goods_lines
        ]
        actual_goods_items.extend(document_goods_lines)
        print_items.append(
            {
                "delivery_item_id": row.delivery_item_id,
                "order_item_id": row.order_item_id,
                "customer_po": row.customer_po,
                "product_code": _print_product_code(product_code),
                "product_name": product_name,
                "specification": specification,
                "unit": row.unit_snapshot or "PCS",
                "quantity": row.quantity,
                "ordered_quantity": row.ordered_quantity_snapshot,
                "over_delivery_quantity": row.over_delivery_quantity,
                "remarks": _customer_visible_delivery_remark(
                    row.delivery_item_id,
                    row.remarks,
                    internal_remarks,
                ),
                "component_lines": kit_metadata["component_lines"],
                "actual_goods_lines": document_goods_lines,
                "actual_goods_quantity": sum(
                    int(line["quantity"] or 0) for line in document_goods_lines
                ),
            }
        )
    return {
        "id": delivery.id,
        "delivery_number": delivery.delivery_number,
        "delivery_date": delivery.delivery_date,
        "vehicle_number": delivery.vehicle_number,
        "status": delivery.status,
        "total_quantity": delivery.total_quantity,
        "total_actual_goods_quantity": sum(
            int(line["quantity"] or 0) for line in actual_goods_items
        ),
        "actual_goods_items": actual_goods_items,
        "created_at": utc_naive_to_api(delivery.created_at),
        "customer": {
            "name": customer.name if customer else "",
            "contact_person": customer.contact_person if customer else None,
            "phone": customer.phone if customer else None,
            "address": customer.address if customer else None,
        },
        "sender": {
            "company_name": company.company_name if company else "",
            "address": company.address if company else None,
            "phone": company.phone if company else None,
            "fax": company.fax if company else None,
            "tax_number": company.tax_number if company else None,
            "bank_name": company.bank_name if company else None,
            "bank_account": company.bank_account if company else None,
            "contact_person": company.contact_person if company else None,
            "contact_phone": company.contact_phone if company else None,
        },
        "items": print_items,
    }
