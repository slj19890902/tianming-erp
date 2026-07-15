from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, field_validator
from sqlalchemy import and_, case, delete, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import (
    PermissionChecker,
    customer_scope_ids,
    get_db,
    has_unrestricted_customer_access,
    require_customer_access,
)
from app.models.audit import OperationLog
from app.models.company_config import CompanyConfig
from app.models.customer import Customer
from app.models.delivery import Delivery, DeliveryItem
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.requisition import RequisitionItem
from app.models.user import User
from app.models.warehouse_inventory import (
    DeliveryInventoryAllocation,
    InventoryLot,
    InventoryReservation,
    OrderItemSemiRequirement,
    WarehouseLocation,
)
from app.services.history_orders import build_display_registry, display_order_number
from app.services.semi_finished_inventory import (
    active_semi_requirement_credited_quantity,
    consume_delivery_item_inventory,
    inventory_fully_covers_order_item,
    reverse_delivery_item_inventory,
)
from app.services.warehouse_inventory import (
    WarehouseInventoryError,
    active_finished_reserved_qty,
)


router = APIRouter()
order_actions_router = APIRouter()
can_read = PermissionChecker("deliveries.view")
can_operate = PermissionChecker("deliveries.execute")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _print_product_code(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return re.split(r"\s*/\s*|\s+", text, maxsplit=1)[0]


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
    max_deliverable = int(order_item.quantity or 0)
    inventory_covered = inventory_fully_covers_order_item(db, order_item.id)
    component_capacity = _received_telescoping_capacity(db, order_item.id)
    if component_capacity is not None:
        max_deliverable = min(max_deliverable, component_capacity)
    elif order_item.material_status != "received" and not inventory_covered:
        return 0
    return max(max_deliverable - int(order_item.delivered_quantity or 0), 0)


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


class DeliveryLineCreate(BaseModel):
    order_item_id: int
    delivered_quantity: int
    remarks: str | None = None


class DeliveryCreate(BaseModel):
    customer_id: int
    delivery_date: date | None = None
    vehicle_number: str | None = None
    items: list[DeliveryLineCreate]

    @field_validator("items")
    @classmethod
    def validate_items(cls, value: list[DeliveryLineCreate]):
        if not value:
            raise ValueError("送货单至少需要一条明细")
        ids = [item.order_item_id for item in value]
        if len(ids) != len(set(ids)):
            raise ValueError("同一订单明细不能重复选择")
        return value


class DeliveryUpdate(BaseModel):
    delivery_date: date | None = None
    vehicle_number: str | None = None
    items: list[DeliveryLineCreate]

    @field_validator("items")
    @classmethod
    def validate_items(cls, value: list[DeliveryLineCreate]):
        if not value:
            raise ValueError("送货单至少需要一条明细")
        ids = [item.order_item_id for item in value]
        if len(ids) != len(set(ids)):
            raise ValueError("同一订单明细不能重复选择")
        return value


class ForceCloseRequest(BaseModel):
    reason: str

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        reason = value.strip()
        if not reason:
            raise ValueError("强制结案原因不能为空")
        return reason


def _next_delivery_number(db: Session, delivery_date: date) -> str:
    sequence = db.execute(
        text(
            """
            INSERT INTO delivery_daily_sequences (sequence_date, last_value)
            VALUES (:sequence_date, 1)
            ON CONFLICT(sequence_date)
            DO UPDATE SET last_value = last_value + 1
            RETURNING last_value
            """
        ),
        {"sequence_date": delivery_date.isoformat()},
    ).scalar_one()
    if sequence > 999:
        raise HTTPException(status_code=409, detail="当日送货单流水号已超过999")
    return f"DH-{delivery_date:%Y%m%d}-{sequence:03d}"


def _pending_query(
    *,
    customer_id: int | None = None,
    customer_ids: set[int] | None = None,
    inventory_keyword: str | None = None,
    customer_po_keyword: str | None = None,
    product_name_keyword: str | None = None,
    general_keyword: str | None = None,
):
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
                OrderItem.material_status == "received",
                active_finished_reserved >= OrderItem.quantity,
                semi_fully_covered,
                received_telescoping_components > 0,
            ),
            OrderItem.delivered_quantity < OrderItem.quantity,
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


def _inventory_sources_for_order_item(
    db: Session,
    *,
    order_item: OrderItem,
    planned_delivery_quantity: int,
    delivery_item_id: int | None = None,
    dispatched: bool = False,
) -> list[dict]:
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
            InventoryLot.stock_date,
            InventoryLot.id,
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


def _delivery_response(db: Session, delivery_id: int) -> dict:
    from app.models.finance import ReturnReceipt

    delivery = _delivery_or_404(db, delivery_id)
    customer = db.get(Customer, delivery.customer_id)
    return_receipt = db.scalar(
        select(ReturnReceipt).where(ReturnReceipt.delivery_id == delivery_id)
    )
    items = db.execute(
        select(
            DeliveryItem.id,
            DeliveryItem.order_item_id,
            DeliveryItem.delivered_quantity,
            DeliveryItem.remarks,
            Order.id.label("order_id"),
            Order.order_number,
            Order.customer_po,
            Product.product_code,
            OrderItem.snapshot_product_name.label("product_name"),
            OrderItem.snapshot_spec.label("specification"),
            OrderItem.snapshot_production_notes.label("production_notes"),
        )
        .join(OrderItem, OrderItem.id == DeliveryItem.order_item_id)
        .join(Order, Order.id == OrderItem.order_id)
        .join(Product, Product.id == OrderItem.product_id)
        .where(DeliveryItem.delivery_id == delivery_id)
        .order_by(DeliveryItem.id)
    ).all()
    registry = build_display_registry(db)
    order_ids = {
        row._mapping["order_id"]
        for row in items
    }
    orders = {
        order.id: order for order in db.scalars(select(Order).where(Order.id.in_(order_ids))).all()
    } if order_ids else {}
    return {
        "id": delivery.id,
        "delivery_number": delivery.delivery_number,
        "customer_id": delivery.customer_id,
        "customer_name": customer.name if customer else None,
        "delivery_date": delivery.delivery_date,
        "vehicle_number": delivery.vehicle_number,
        "status": delivery.status,
        "total_quantity": delivery.total_quantity,
        "dispatched_at": delivery.dispatched_at,
        "is_printed": delivery.printed_at is not None,
        "printed_at": delivery.printed_at,
        "printed_by": delivery.printed_by,
        "return_receipt_id": return_receipt.id if return_receipt else None,
        "return_receipt_status": (
            return_receipt.status if return_receipt else None
        ),
        "items": [
            {
                **dict(row._mapping),
                "order_number": display_order_number(
                    orders.get(row._mapping["order_id"]),
                    registry,
                )
                if "order_id" in row._mapping
                else row._mapping["order_number"],
                "display_order_number": display_order_number(
                    orders.get(row._mapping["order_id"]),
                    registry,
                )
                if "order_id" in row._mapping
                else row._mapping["order_number"],
                "inventory_sources": _inventory_sources_for_order_item(
                    db,
                    order_item=db.get(OrderItem, row._mapping["order_item_id"]),
                    planned_delivery_quantity=row._mapping["delivered_quantity"],
                    delivery_item_id=row._mapping["id"],
                    dispatched=delivery.status == "dispatched",
                ),
            }
            for row in items
        ],
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
        remaining = _delivery_remaining_quantity(db, order_item)
        component_capacity = _received_telescoping_capacity(db, order_item.id)
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
        full_inventory_coverage = inventory_fully_covers_order_item(
            db, order_item.id
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
        if (
            (
                order_item.material_status != "received"
                and not full_inventory_coverage
                and _received_telescoping_capacity(db, order_item.id) is None
            )
            or order_item.is_force_closed
        ):
            raise HTTPException(
                status_code=400,
                detail=f"第{index}条订单明细当前不可发货",
            )
        if line.delivered_quantity > remaining:
            warnings.append(
                {
                    "code": "OVER_DELIVERY",
                    "order_item_id": order_item.id,
                    "remaining_quantity": remaining,
                    "delivered_quantity": line.delivered_quantity,
                    "excess_quantity": line.delivered_quantity - remaining,
                    "message": "实际发货数已超过订单可送数量",
                }
            )
        built.append((order_item, line))
        total_quantity += line.delivered_quantity
    return built, total_quantity, warnings


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


@router.get("/pending_items")
def pending_delivery_items(
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    registry = build_display_registry(db)
    items = []
    for row in db.execute(
        _pending_query(customer_ids=_visible_customer_ids(user, db))
    ):
        order = db.get(Order, row._mapping["order_id"])
        order_item = db.get(OrderItem, row._mapping["order_item_id"])
        remaining_quantity = (
            _delivery_remaining_quantity(db, order_item) if order_item else 0
        )
        if remaining_quantity <= 0:
            continue
        display = display_order_number(order, registry)
        items.append(
            {
                **dict(row._mapping),
                "remaining_quantity": remaining_quantity,
                "order_number": display,
                "display_order_number": display,
                "inventory_sources": _inventory_sources_for_order_item(
                    db,
                    order_item=order_item,
                    planned_delivery_quantity=remaining_quantity,
                ),
            }
        )
    return {"items": items}


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
        return {"items": []}
    if db.get(Customer, customer_id) is None:
        raise HTTPException(status_code=400, detail="客户不存在")
    effective_limit = limit or (100 if list_all else 20)
    registry = build_display_registry(db)
    items = []
    query_limit = min(effective_limit * 3, 200)
    for row in db.execute(
        _pending_query(
            customer_id=customer_id,
            inventory_keyword=inventory_keyword,
            customer_po_keyword=customer_po_keyword,
            product_name_keyword=product_name_keyword,
            general_keyword=general_keyword,
        ).limit(query_limit)
    ):
        order = db.get(Order, row._mapping["order_id"])
        order_item = db.get(OrderItem, row._mapping["order_item_id"])
        remaining_quantity = (
            _delivery_remaining_quantity(db, order_item) if order_item else 0
        )
        if remaining_quantity <= 0:
            continue
        display = display_order_number(order, registry)
        material = (row._mapping["material"] or "").strip()
        flute_type = (row._mapping["flute_type"] or "").strip()
        items.append(
            {
                **dict(row._mapping),
                "remaining_quantity": remaining_quantity,
                "order_number": display,
                "display_order_number": display,
                "material_display": (
                    f"{material} / {flute_type}"
                    if material and flute_type
                    else material
                ),
                "inventory_sources": _inventory_sources_for_order_item(
                    db,
                    order_item=order_item,
                    planned_delivery_quantity=remaining_quantity,
                ),
            }
        )
        if len(items) >= effective_limit:
            break
    return {"items": items}


@router.get("")
def list_deliveries(
    customer_id: int | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    # New delivery drafts must stay at the top. Printing or dispatching an old
    # delivery must not move it ahead of a delivery that was just created.
    query = select(Delivery.id).order_by(
        Delivery.created_at.desc(),
        Delivery.id.desc(),
    )
    if customer_id is not None:
        require_customer_access(customer_id, user, db)
        query = query.where(Delivery.customer_id == customer_id)
    else:
        visible_customer_ids = _visible_customer_ids(user, db)
        if visible_customer_ids is not None:
            query = query.where(Delivery.customer_id.in_(visible_customer_ids))
    if status_filter:
        query = query.where(Delivery.status == status_filter)
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    delivery_ids = db.scalars(
        query.offset((page - 1) * page_size).limit(page_size)
    ).all()
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [
            _delivery_response(db, delivery_id)
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
    if db.get(Customer, payload.customer_id) is None:
        raise HTTPException(status_code=400, detail="客户不存在")
    delivery_date = payload.delivery_date or date.today()
    try:
        delivery = Delivery(
            delivery_number=_next_delivery_number(db, delivery_date),
            customer_id=payload.customer_id,
            delivery_date=delivery_date,
            vehicle_number=(payload.vehicle_number or "").strip() or None,
            status="pending",
            total_quantity=0,
            created_by=user.id,
        )
        db.add(delivery)
        db.flush()
        built, total_quantity, warnings = _collect_delivery_lines(
            db,
            customer_id=payload.customer_id,
            lines=payload.items,
        )
        for order_item, line in built:
            db.add(
                DeliveryItem(
                    delivery_id=delivery.id,
                    order_item_id=order_item.id,
                    delivered_quantity=line.delivered_quantity,
                    remarks=(line.remarks or "").strip() or None,
                )
            )
        delivery.total_quantity = total_quantity
        _write_audit(
            db,
            user=user,
            action="CREATE",
            resource="Delivery",
            entity_id=delivery.id,
            details={
                "delivery_number": delivery.delivery_number,
                "item_count": len(payload.items),
                "total_quantity": total_quantity,
            },
            description="创建待发货送货单",
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


@router.put("/{delivery_id}/dispatch")
def dispatch_delivery(
    delivery_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
    _delivery_for_user(db, delivery_id, user)
    dispatched_at = _utc_now()
    try:
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
            )
        )
        if claimed.rowcount != 1:
            exists = db.scalar(
                select(Delivery.id).where(Delivery.id == delivery_id)
            )
            if exists is None:
                raise HTTPException(status_code=404, detail="送货单不存在")
            raise HTTPException(status_code=409, detail="送货单已确认发货")

        lines = db.scalars(
            select(DeliveryItem)
            .where(DeliveryItem.delivery_id == delivery_id)
            .order_by(DeliveryItem.id)
        ).all()
        affected_order_ids: set[int] = set()
        for line in lines:
            order_item = db.get(OrderItem, line.order_item_id)
            if order_item is None:
                raise HTTPException(
                    status_code=409,
                    detail=f"订单明细{line.order_item_id}不存在",
                )
            remaining = _delivery_remaining_quantity(db, order_item)
            component_capacity = _received_telescoping_capacity(db, order_item.id)
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
            consume_delivery_item_inventory(
                db,
                delivery_item_id=line.id,
                delivered_quantity_after_dispatch=(
                    delivered_before + line.delivered_quantity
                ),
                operator_id=user.id,
                operation_key=(
                    f"d{delivery_id}-{dispatched_at:%Y%m%d%H%M%S%f}-i{line.id}"
                ),
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
        for order_id in affected_order_ids:
            _refresh_order_status(db, order_id)
        _write_audit(
            db,
            user=user,
            action="DISPATCH",
            resource="Delivery",
            entity_id=delivery_id,
            details={
                "dispatched_at": dispatched_at,
                "item_count": len(lines),
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
            exists = db.scalar(
                select(Delivery.id).where(Delivery.id == delivery_id)
            )
            if exists is None:
                raise HTTPException(status_code=404, detail="送货单不存在")
            raise HTTPException(
                status_code=409,
                detail="送货单已确认发货，不能编辑，请先取消发货",
            )
        delivery = _delivery_or_404(db, delivery_id)
        built, total_quantity, warnings = _collect_delivery_lines(
            db,
            customer_id=delivery.customer_id,
            lines=payload.items,
        )
        db.execute(
            delete(DeliveryItem).where(DeliveryItem.delivery_id == delivery_id)
        )
        db.flush()
        for order_item, line in built:
            db.add(
                DeliveryItem(
                    delivery_id=delivery.id,
                    order_item_id=order_item.id,
                    delivered_quantity=line.delivered_quantity,
                    remarks=(line.remarks or "").strip() or None,
                )
            )
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
                "item_count": len(built),
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
    _delivery_for_user(db, delivery_id, user)
    try:
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
            exists = db.scalar(
                select(Delivery.id).where(Delivery.id == delivery_id)
            )
            if exists is None:
                raise HTTPException(status_code=404, detail="送货单不存在")
            raise HTTPException(
                status_code=409,
                detail="已确认发货的送货单不能删除，请改用取消发货",
            )
        delivery = _delivery_or_404(db, delivery_id)
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
        db.delete(delivery)
        db.commit()
        return {"deleted": True, "id": delivery_id}
    except HTTPException:
        db.rollback()
        raise
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
            exists = db.scalar(
                select(Delivery.id).where(Delivery.id == delivery_id)
            )
            if exists is None:
                raise HTTPException(status_code=404, detail="送货单不存在")
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
        affected_order_ids: set[int] = set()
        for line in lines:
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
            reverse_delivery_item_inventory(
                db,
                delivery_item_id=line.id,
                delivered_quantity_after_cancel=delivered_after,
                operator_id=user.id,
                operation_key=(
                    f"c{delivery_id}-{cancelled_at:%Y%m%d%H%M%S%f}-i{line.id}"
                ),
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
            },
            description="取消送货单发货并回滚已送数量",
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
            exists = db.scalar(
                select(Delivery.id).where(Delivery.id == delivery_id)
            )
            if exists is None:
                raise HTTPException(status_code=404, detail="送货单不存在")
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
    item = db.get(OrderItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="订单明细不存在")
    _require_order_item_customer_access(db, item.id, user)
    if item.delivered_quantity >= item.quantity:
        raise HTTPException(status_code=409, detail="订单明细已全部发货")
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
                "reason": payload.reason,
                "ordered_quantity": item.quantity,
                "delivered_quantity": item.delivered_quantity,
            },
            description="缺货尾数强制结案",
        )
        db.commit()
        return {
            "item_id": item_id,
            "is_force_closed": True,
            "reason": payload.reason,
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
    customer = db.get(Customer, delivery.customer_id)
    company = db.scalar(select(CompanyConfig).where(CompanyConfig.id == 1))
    rows = db.execute(
        select(
            Order.customer_po,
            Product.product_code,
            OrderItem.snapshot_product_name.label("product_name"),
            OrderItem.snapshot_spec.label("specification"),
            DeliveryItem.delivered_quantity.label("quantity"),
            DeliveryItem.remarks,
            OrderItem.snapshot_production_notes.label("production_notes"),
        )
        .join(OrderItem, OrderItem.id == DeliveryItem.order_item_id)
        .join(Order, Order.id == OrderItem.order_id)
        .join(Product, Product.id == OrderItem.product_id)
        .where(DeliveryItem.delivery_id == delivery_id)
        .order_by(DeliveryItem.id)
    ).all()
    return {
        "id": delivery.id,
        "delivery_number": delivery.delivery_number,
        "delivery_date": delivery.delivery_date,
        "vehicle_number": delivery.vehicle_number,
        "status": delivery.status,
        "total_quantity": delivery.total_quantity,
        "created_at": delivery.created_at,
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
        "items": [
            {
                "customer_po": row.customer_po,
                "product_code": _print_product_code(row.product_code),
                "product_name": row.product_name,
                "specification": row.specification,
                "unit": "PCS",
                "quantity": row.quantity,
                "remarks": row.remarks,
                "production_notes": row.production_notes,
            }
            for row in rows
        ],
    }
