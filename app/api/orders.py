from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload, selectinload

from app.api.deps import RoleChecker, get_db
from app.models.audit import OperationLog
from app.models.customer import Customer
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.user import User
from app.services.history_orders import (
    build_display_registry,
    filter_order_ids_for_display_search,
    sanitize_user_text,
    serialize_order_number_fields,
)
from app.services.order_numbering import (
    format_item_order_number,
    preview_next_order_number,
    reserve_next_item_sequence,
    reserve_next_order_number,
)
from app.services.order_pdf_import import (
    extract_text_from_pdf_bytes,
    match_import_draft,
    parse_purchase_order_text,
)


router = APIRouter()
can_create = RoleChecker(["admin", "sales"])
can_read = RoleChecker(["admin", "finance", "sales", "workshop"])
MONEY_QUANTUM = Decimal("0.00")


class OrderItemCreate(BaseModel):
    product_id: int
    quantity: int
    unit_price: Decimal
    product_code: str | None = None
    product_name: str | None = None
    material: str | None = None
    specification: str | None = None


class OrderItemUpdate(BaseModel):
    quantity: int
    unit_price: Decimal
    product_code: str
    product_name: str
    material: str | None = None
    specification: str | None = None


class OrderCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    customer_id: int | None = None
    customer_name: str | None = None
    customer_po: str | None = None
    order_date: date | None = None
    delivery_date: date | None = None
    status: str = "pending_production"
    payment_status: str = "unpaid"
    remark: str | None = None
    items: list[OrderItemCreate] | None = None

    # legacy single-line compatibility payload
    product_archive_id: int | None = None
    style_no: str | None = None
    product_name: str | None = None
    material: str | None = None
    flute_type: str | None = None
    process_note: str | None = None
    delivery_due_date: str | None = None
    unit: str = "只"
    length_mm: float | None = None
    width_mm: float | None = None
    height_mm: float | None = None
    order_quantity: int | None = None
    sale_unit_price: float | None = None
    sale_unit_price_no_tax: float | None = None
    cost_unit_price: float | None = None
    warning_confirmed: bool = False
    created_by: int | None = None


class OrderUpdate(BaseModel):
    customer_po: str | None = None
    delivery_date: date | None = None
    remark: str | None = None


def _plain_decimal(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value, "f").rstrip("0").rstrip(".") or "0"


def _snapshot_spec(product: Product) -> str | None:
    dimensions = (product.length_mm, product.width_mm, product.height_mm)
    if any(value is None for value in dimensions):
        return None
    return "脳".join(_plain_decimal(value) or "0" for value in dimensions) + "mm"


def _display_material(value: str | None) -> str | None:
    text = sanitize_user_text(value)
    if not text:
        return text
    cleaned = re.sub(r"^\s*\d+\s+", "", text).strip()
    return cleaned or text


def _legacy_create(payload: OrderCreate, user: User) -> JSONResponse:
    import main as legacy

    legacy_payload = legacy.OrderCreateRequest(
        **{
            **payload.model_dump(
                exclude={"items", "order_date", "delivery_date", "status", "payment_status"}
            ),
            "created_by": user.id,
        }
    )
    result = legacy.create_order_record(legacy_payload)
    if result["conflict"]:
        return JSONResponse(
            status_code=409,
            content={
                "ok": False,
                "code": "ORDER_HISTORY_CONFLICT",
                "message": result["message"],
                "differences": result["differences"],
            },
        )
    return JSONResponse({"ok": True, "order": result["order"]})


def _order_group_key(order: Order) -> str:
    if order.customer_po and order.customer_po.strip():
        return f"{order.customer_id}::{order.customer_po.strip()}"
    return f"single::{order.order_number}"


def _order_response(
    order: Order,
    user: User,
    *,
    customer_name: str | None = None,
    display_registry=None,
) -> dict:
    data = {
        "id": order.id,
        **serialize_order_number_fields(order, display_registry),
        "group_key": _order_group_key(order),
        "customer_id": order.customer_id,
        "customer_name": customer_name,
        "customer_po": order.customer_po,
        "order_date": order.order_date,
        "delivery_date": order.delivery_date,
        "status": order.status,
        "payment_status": order.payment_status,
        "total_amount": order.total_amount,
        "remark": sanitize_user_text(order.remark),
        "items": [
            {
                "id": item.id,
                "product_id": item.product_id,
                "item_order_number": item.item_order_number,
                "item_sequence": item.item_sequence,
                "quantity": item.quantity,
                "delivered_quantity": item.delivered_quantity,
                "is_force_closed": item.is_force_closed,
                "unit_price": item.unit_price,
                "subtotal": item.subtotal,
                "material_status": item.material_status,
                "material_received_at": item.material_received_at,
                "snapshot_product_code": item.snapshot_product_code,
                "snapshot_product_name": item.snapshot_product_name,
                "snapshot_spec": item.snapshot_spec,
                "snapshot_material": item.snapshot_material,
                "display_material": _display_material(item.snapshot_material),
                "inventory_deducted_qty": item.inventory_deducted_qty,
                "requisition_qty": item.requisition_qty,
                "requisition_status": item.requisition_status,
                "special_process": item.special_process,
                "requisition_spec": item.requisition_spec,
                "cardboard_len": item.cardboard_len,
                "cardboard_width": item.cardboard_width,
                "supplier_delivery_time": item.supplier_delivery_time,
            }
            for item in order.items
        ],
    }
    if user.role == "workshop":
        data.pop("total_amount", None)
        data.pop("payment_status", None)
        for item in data["items"]:
            item.pop("unit_price", None)
            item.pop("subtotal", None)
    return data


def _refresh_total(db: Session, order: Order) -> None:
    total = db.scalar(
        select(func.coalesce(func.sum(OrderItem.subtotal), 0)).where(
            OrderItem.order_id == order.id
        )
    )
    order.total_amount = Decimal(str(total or 0)).quantize(
        MONEY_QUANTUM,
        rounding=ROUND_HALF_UP,
    )


@router.get("")
def list_orders(
    customer_id: int | None = None,
    keyword: str | None = None,
    order_number: str | None = None,
    customer_name: str | None = None,
    order_date: date | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    display_registry = build_display_registry(db)
    ids_query = select(Order.id).distinct()

    if customer_id is not None:
        ids_query = ids_query.where(Order.customer_id == customer_id)

    if customer_name and customer_name.strip():
        ids_query = ids_query.join(Customer, Customer.id == Order.customer_id).where(
            Customer.name.ilike(f"%{customer_name.strip()}%")
        )

    if order_date is not None:
        ids_query = ids_query.where(Order.order_date == order_date)
    if date_from is not None:
        ids_query = ids_query.where(Order.order_date >= date_from)
    if date_to is not None:
        ids_query = ids_query.where(Order.order_date <= date_to)

    if status_filter:
        ids_query = ids_query.where(Order.status == status_filter)

    if keyword and keyword.strip():
        trimmed = keyword.strip()
        display_ids = filter_order_ids_for_display_search(db, trimmed, display_registry)
        ids_query = ids_query.outerjoin(OrderItem, OrderItem.order_id == Order.id).where(
            or_(
                Order.order_number.ilike(f"%{trimmed}%"),
                Order.customer_po.ilike(f"%{trimmed}%"),
                OrderItem.item_order_number.ilike(f"%{trimmed}%"),
                OrderItem.snapshot_product_code.ilike(f"%{trimmed}%"),
                OrderItem.snapshot_product_name.ilike(f"%{trimmed}%"),
                Order.id.in_(display_ids) if display_ids else False,
            )
        )

    if order_number and order_number.strip():
        trimmed = order_number.strip()
        display_ids = filter_order_ids_for_display_search(db, trimmed, display_registry)
        ids_query = ids_query.outerjoin(OrderItem, OrderItem.order_id == Order.id).where(
            or_(
                Order.order_number == trimmed,
                OrderItem.item_order_number == trimmed,
                Order.id.in_(display_ids) if display_ids else False,
            )
        )

    ids_query = ids_query.order_by(Order.order_date.desc(), Order.id.desc())
    total = db.scalar(select(func.count()).select_from(ids_query.subquery())) or 0
    page_ids = list(
        db.scalars(ids_query.offset((page - 1) * page_size).limit(page_size)).all()
    )

    orders: list[Order] = []
    if page_ids:
        loaded = db.scalars(
            select(Order)
            .options(selectinload(Order.items))
            .where(Order.id.in_(page_ids))
        ).all()
        order_map = {order.id: order for order in loaded}
        orders = [order_map[item_id] for item_id in page_ids if item_id in order_map]

    customer_ids = {order.customer_id for order in orders}
    customer_names = (
        {
            customer.id: customer.name
            for customer in db.scalars(
                select(Customer).where(Customer.id.in_(customer_ids))
            ).all()
        }
        if customer_ids
        else {}
    )
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [
            _order_response(
                order,
                user,
                customer_name=customer_names.get(order.customer_id),
                display_registry=display_registry,
            )
            for order in orders
        ],
    }


@router.get("/number-preview")
def get_order_number_preview(
    order_date: date | None = None,
    item_count: int = Query(default=1, ge=1, le=20),
    db: Session = Depends(get_db),
    user: User = Depends(can_create),
) -> dict:
    preview_date = order_date or date.today()
    main_number = preview_next_order_number(db, preview_date)
    return {
        "order_number": main_number,
        "item_order_numbers": [
            format_item_order_number(main_number, index)
            for index in range(1, item_count + 1)
        ],
    }


@router.post("/pdf-preview")
async def preview_order_pdf(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    _user: User = Depends(can_create),
) -> dict:
    filename = (file.filename or "").strip()
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="只支持上传 PDF 文件")
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="上传的 PDF 为空")
    try:
        text = extract_text_from_pdf_bytes(content)
        draft = parse_purchase_order_text(text, source_name=filename)
        return match_import_draft(db, draft)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=400, detail=f"PDF 识别失败：{error}") from error


@router.get("/{order_id}")
def get_order_detail(
    order_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_read),
) -> dict:
    display_registry = build_display_registry(db)
    order = db.scalar(
        select(Order)
        .options(selectinload(Order.items))
        .where(Order.id == order_id)
    )
    if order is None:
        raise HTTPException(status_code=404, detail="订单不存在")
    customer = db.get(Customer, order.customer_id)
    return _order_response(
        order,
        user,
        customer_name=customer.name if customer is not None else None,
        display_registry=display_registry,
    )


@router.put("/{order_id}")
def update_order(
    order_id: int,
    payload: OrderUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(can_create),
) -> dict:
    display_registry = build_display_registry(db)
    order = db.scalar(
        select(Order)
        .options(selectinload(Order.items))
        .where(Order.id == order_id)
    )
    if order is None:
        raise HTTPException(status_code=404, detail="订单不存在")

    order.customer_po = (payload.customer_po or "").strip() or None
    order.delivery_date = payload.delivery_date
    order.remark = (payload.remark or "").strip() or None
    db.commit()
    db.refresh(order)
    customer = db.get(Customer, order.customer_id)
    return _order_response(
        order,
        user,
        customer_name=customer.name if customer is not None else None,
        display_registry=display_registry,
    )


@router.post("", status_code=status.HTTP_201_CREATED)
def create_order(
    payload: OrderCreate,
    db: Session = Depends(get_db),
    user: User = Depends(can_create),
):
    if payload.items is None:
        return _legacy_create(payload, user)
    if not payload.items:
        raise HTTPException(status_code=400, detail="订单至少需要一条明细")
    if payload.customer_id is None:
        raise HTTPException(status_code=400, detail="客户不能为空")

    try:
        customer = db.get(Customer, payload.customer_id)
        if customer is None:
            raise HTTPException(status_code=400, detail="客户不存在")

        order_date = payload.order_date or date.today()
        order = Order(
            order_number=reserve_next_order_number(db, order_date),
            customer_id=customer.id,
            customer_po=(payload.customer_po or "").strip() or None,
            order_date=order_date,
            delivery_date=payload.delivery_date,
            status=payload.status,
            payment_status=payload.payment_status,
            total_amount=Decimal("0"),
            remark=(payload.remark or "").strip() or None,
            created_by=user.id,
        )
        db.add(order)
        db.flush()

        total = Decimal("0")
        created_items: list[OrderItem] = []
        for index, item_payload in enumerate(payload.items, start=1):
            if item_payload.quantity <= 0:
                raise HTTPException(
                    status_code=400,
                    detail=f"第{index}条明细数量必须大于0",
                )
            try:
                unit_price = Decimal(str(item_payload.unit_price))
            except (InvalidOperation, ValueError) as error:
                raise HTTPException(
                    status_code=400,
                    detail=f"第{index}条明细单价无效",
                ) from error
            if unit_price < 0:
                raise HTTPException(
                    status_code=400,
                    detail=f"第{index}条明细单价不能为负数",
                )

            product = db.scalar(
                select(Product)
                .options(joinedload(Product.material))
                .where(Product.id == item_payload.product_id)
            )
            if product is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"第{index}条明细产品不存在",
                )
            if not product.is_active:
                raise HTTPException(
                    status_code=400,
                    detail="该纸箱已停用，不能用于新建订单",
                )
            if product.customer_id != customer.id:
                raise HTTPException(
                    status_code=400,
                    detail=f"第{index}条明细产品不属于当前客户",
                )

            subtotal = (
                Decimal(item_payload.quantity) * unit_price
            ).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
            total += subtotal
            item_sequence = reserve_next_item_sequence(db, order.id)
            item = OrderItem(
                order_id=order.id,
                product_id=product.id,
                item_sequence=item_sequence,
                item_order_number=format_item_order_number(
                    order.order_number,
                    item_sequence,
                ),
                quantity=item_payload.quantity,
                unit_price=unit_price,
                subtotal=subtotal,
                material_status="pending",
                snapshot_product_code=(
                    (item_payload.product_code or "").strip() or product.product_code
                ),
                snapshot_product_name=(
                    (item_payload.product_name or "").strip() or product.product_name
                ),
                snapshot_spec=(
                    (item_payload.specification or "").strip() or _snapshot_spec(product)
                ),
                snapshot_material=(
                    (item_payload.material or "").strip()
                    or (
                        product.material.code
                        if product.material is not None
                        else product.legacy_material_text
                    )
                ),
                requisition_status="未报料",
            )
            db.add(item)
            created_items.append(item)

        order.total_amount = total.quantize(
            MONEY_QUANTUM,
            rounding=ROUND_HALF_UP,
        )
        db.add(
            OperationLog(
                user_id=user.id,
                action="CREATE",
                resource="Order",
                details=json.dumps(
                    {
                        "order_number": order.order_number,
                        "customer_id": order.customer_id,
                        "item_count": len(payload.items),
                        "total_amount": str(order.total_amount),
                    },
                    ensure_ascii=False,
                ),
                username=user.username,
                role=user.role,
                entity_type="order",
                entity_id=order.id,
                description="创建多明细订单",
            )
        )
        db.commit()
        db.refresh(order)
        return _order_response(
            order,
            user,
            customer_name=customer.name,
        )
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="订单号或订单数据冲突") from error
    except Exception:
        db.rollback()
        raise


@router.put("/items/{item_id}")
def update_order_item(
    item_id: int,
    payload: OrderItemUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(can_create),
) -> dict:
    item = db.get(OrderItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="订单明细不存在")
    if item.delivered_quantity > 0:
        raise HTTPException(status_code=409, detail="已发货明细禁止修改")
    if item.material_status == "received":
        raise HTTPException(status_code=409, detail="已入库明细禁止修改")
    if item.requisition_status != "未报料":
        raise HTTPException(status_code=409, detail="请先取消报料再修改订单明细")
    if payload.quantity <= 0:
        raise HTTPException(status_code=400, detail="数量必须大于0")
    unit_price = Decimal(str(payload.unit_price))
    if unit_price < 0:
        raise HTTPException(status_code=400, detail="单价不能为负数")
    order = db.get(Order, item.order_id)
    before = {
        "quantity": item.quantity,
        "unit_price": str(item.unit_price),
        "product_code": item.snapshot_product_code,
        "product_name": item.snapshot_product_name,
        "material": item.snapshot_material,
        "specification": item.snapshot_spec,
    }
    item.quantity = payload.quantity
    item.unit_price = unit_price
    item.subtotal = (Decimal(payload.quantity) * unit_price).quantize(
        MONEY_QUANTUM,
        rounding=ROUND_HALF_UP,
    )
    item.snapshot_product_code = payload.product_code.strip()
    item.snapshot_product_name = payload.product_name.strip()
    item.snapshot_material = (payload.material or "").strip() or None
    item.snapshot_spec = (payload.specification or "").strip() or None
    _refresh_total(db, order)
    db.add(
        OperationLog(
            user_id=user.id,
            action="UPDATE",
            resource="OrderItem",
            details=json.dumps(
                {"before": before, "after": payload.model_dump()},
                ensure_ascii=False,
                default=str,
            ),
            username=user.username,
            role=user.role,
            entity_type="order_item",
            entity_id=item.id,
            description="修改订单单条明细",
        )
    )
    db.commit()
    db.refresh(item)
    return {
        "id": item.id,
        "product_id": item.product_id,
        "item_order_number": item.item_order_number,
        "item_sequence": item.item_sequence,
        "quantity": item.quantity,
        "unit_price": item.unit_price,
        "subtotal": item.subtotal,
        "snapshot_product_code": item.snapshot_product_code,
        "snapshot_product_name": item.snapshot_product_name,
        "snapshot_material": item.snapshot_material,
        "snapshot_spec": item.snapshot_spec,
    }


@router.delete("/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_order_item(
    item_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(can_create),
) -> Response:
    item = db.get(OrderItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="订单明细不存在")
    if item.delivered_quantity > 0 or item.material_status == "received":
        raise HTTPException(status_code=409, detail="已流转明细禁止删除")
    if item.requisition_status != "未报料":
        raise HTTPException(status_code=409, detail="请先取消报料再删除订单明细")
    order = db.get(Order, item.order_id)
    item_count = db.scalar(
        select(func.count()).select_from(OrderItem).where(
            OrderItem.order_id == order.id
        )
    )
    if item_count <= 1:
        raise HTTPException(status_code=409, detail="订单至少保留一条明细")
    deleted = {
        "item_id": item.id,
        "item_order_number": item.item_order_number,
        "product_name": item.snapshot_product_name,
        "quantity": item.quantity,
    }
    db.delete(item)
    db.flush()
    _refresh_total(db, order)
    db.add(
        OperationLog(
            user_id=user.id,
            action="DELETE",
            resource="OrderItem",
            details=json.dumps(deleted, ensure_ascii=False),
            username=user.username,
            role=user.role,
            entity_type="order_item",
            entity_id=item_id,
            description="删除订单单条明细",
        )
    )
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
