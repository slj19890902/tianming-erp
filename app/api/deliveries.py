from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, field_validator
from sqlalchemy import case, delete, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import RoleChecker, get_db
from app.models.audit import OperationLog
from app.models.company_config import CompanyConfig
from app.models.customer import Customer
from app.models.delivery import Delivery, DeliveryItem
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.user import User
from app.models.warehouse_inventory import InventoryReservation
from app.services.history_orders import build_display_registry, display_order_number
from app.services.warehouse_inventory import active_finished_reserved_qty


router = APIRouter()
order_actions_router = APIRouter()
can_read = RoleChecker(["admin", "finance", "sales", "workshop"])
can_operate = RoleChecker(["admin", "sales"])


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _print_product_code(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return re.split(r"\s*/\s*|\s+", text, maxsplit=1)[0]


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
    inventory_keyword: str | None = None,
    customer_po_keyword: str | None = None,
    product_name_keyword: str | None = None,
    general_keyword: str | None = None,
):
    active_finished_reserved = (
        select(
            func.coalesce(
                func.sum(InventoryReservation.credited_requirement_quantity), 0
            )
        )
        .where(
            InventoryReservation.order_item_id == OrderItem.id,
            InventoryReservation.reservation_type == "finished_order",
            InventoryReservation.status == "active",
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
            ),
            OrderItem.delivered_quantity < OrderItem.quantity,
            OrderItem.is_force_closed.is_(False),
        )
    )
    if customer_id is not None:
        query = query.where(Order.customer_id == customer_id)
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
        remaining = order_item.quantity - order_item.delivered_quantity
        if order.customer_id != customer_id:
            raise HTTPException(
                status_code=400,
                detail=f"第{index}条订单明细不属于当前客户",
            )
        full_finished_reservation = (
            active_finished_reserved_qty(db, order_item.id)
            >= order_item.quantity
        )
        if line.delivered_quantity > remaining:
            raise HTTPException(
                status_code=400,
                detail=f"第{index}条本次送货数量不能超过未送数量，当前未送数量为 {remaining}",
            )
        if (
            (
                order_item.material_status != "received"
                and not full_finished_reservation
            )
            or order_item.is_force_closed
            or remaining <= 0
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


@router.get("/pending_items")
def pending_delivery_items(
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> dict:
    registry = build_display_registry(db)
    items = []
    for row in db.execute(_pending_query()):
        order = db.get(Order, row._mapping["order_id"])
        display = display_order_number(order, registry)
        items.append(
            {
                **dict(row._mapping),
                "order_number": display,
                "display_order_number": display,
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
    for row in db.execute(
        _pending_query(
            customer_id=customer_id,
            inventory_keyword=inventory_keyword,
            customer_po_keyword=customer_po_keyword,
            product_name_keyword=product_name_keyword,
            general_keyword=general_keyword,
        ).limit(effective_limit)
    ):
        order = db.get(Order, row._mapping["order_id"])
        display = display_order_number(order, registry)
        material = (row._mapping["material"] or "").strip()
        flute_type = (row._mapping["flute_type"] or "").strip()
        items.append(
            {
                **dict(row._mapping),
                "order_number": display,
                "display_order_number": display,
                "material_display": (
                    f"{material} / {flute_type}"
                    if material and flute_type
                    else material
                ),
            }
        )
    return {"items": items}


@router.get("")
def list_deliveries(
    customer_id: int | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> dict:
    query = select(Delivery.id).order_by(
        func.coalesce(
            Delivery.printed_at,
            Delivery.dispatched_at,
            Delivery.created_at,
        ).desc(),
        Delivery.id.desc(),
    )
    if customer_id is not None:
        query = query.where(Delivery.customer_id == customer_id)
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
            order_id = db.scalar(
                select(OrderItem.order_id).where(
                    OrderItem.id == line.order_item_id
                )
            )
            result = db.execute(
                update(OrderItem)
                .where(
                    OrderItem.id == line.order_item_id,
                    OrderItem.material_status == "received",
                    OrderItem.is_force_closed.is_(False),
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
            order_id = db.scalar(
                select(OrderItem.order_id).where(
                    OrderItem.id == line.order_item_id
                )
            )
            result = db.execute(
                update(OrderItem)
                .where(
                    OrderItem.id == line.order_item_id,
                    OrderItem.delivered_quantity >= line.delivered_quantity,
                )
                .values(
                    delivered_quantity=(
                        OrderItem.delivered_quantity - line.delivered_quantity
                    )
                )
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
    except Exception:
        db.rollback()
        raise


@router.put("/{delivery_id}/printed")
def mark_delivery_printed(
    delivery_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_operate),
) -> dict:
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
    _user: User = Depends(can_read),
) -> dict:
    return _delivery_response(db, delivery_id)


@router.get("/{delivery_id}/print")
def get_delivery_print_data(
    delivery_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(can_read),
) -> dict:
    delivery = _delivery_or_404(db, delivery_id)
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
