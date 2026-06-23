from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from phase1_postgres.database import get_session
from phase1_postgres.models import Customer, OrderItem, Product, SalesOrder


router = APIRouter(prefix="/api/orders", tags=["orders"])


class OrderItemPayload(BaseModel):
    product_id: int | None = None
    product_code: str | None = None
    product_name: str | None = None
    length_mm: Decimal | None = None
    width_mm: Decimal | None = None
    height_mm: Decimal | None = None
    quantity: int = Field(gt=0)
    unit_price: Decimal | None = Field(default=None, ge=0)
    snapshot_material: str | None = None
    snapshot_flute_type: str | None = None
    snapshot_score_line: str | None = None
    snapshot_cardboard_length_mm: Decimal | None = None
    snapshot_cardboard_width_mm: Decimal | None = None
    snapshot_pieces_per_sheet: int = Field(default=1, ge=1)
    note: str | None = None


class OrderPayload(BaseModel):
    customer_id: int
    customer_po: str | None = None
    order_date: date
    delivery_date: date | None = None
    note: str | None = None
    items: list[OrderItemPayload] = Field(min_length=1)


def money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.00"), rounding=ROUND_HALF_UP)


def q4(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.0000"), rounding=ROUND_HALF_UP)


def _spec(length: Decimal | None, width: Decimal | None, height: Decimal | None) -> str | None:
    if length is None or width is None or height is None:
        return None
    return f"{length.normalize()}×{width.normalize()}×{height.normalize()}"


def _material_snapshot(product: Product) -> str | None:
    if product.material is not None:
        return product.material.code
    return product.default_material_text


def _normalize_code(value: str | None, fallback: str) -> str:
    text = " ".join((value or "").strip().upper().split())
    if text:
        return text[:120]
    return fallback[:120]


def _history_key(customer: Customer, payload: OrderItemPayload) -> str:
    parts = [
        customer.short_name or customer.name,
        payload.product_code,
        payload.product_name,
        _spec(payload.length_mm, payload.width_mm, payload.height_mm),
    ]
    return " ".join(str(part).strip() for part in parts if part).strip()


def _unique_product_code(session: Session, customer_id: int, base_code: str, current_id: int | None = None) -> str:
    candidate = base_code[:120]
    suffix = 1
    while True:
        existing = session.scalar(
            select(Product.id).where(Product.customer_id == customer_id, Product.product_code == candidate)
        )
        if existing is None or existing == current_id:
            return candidate
        suffix += 1
        candidate = f"{base_code[:110]}-{suffix}"[:120]


def _persist_history_formula(session: Session, customer: Customer, payload: OrderItemPayload) -> Product | None:
    if payload.product_id:
        product = session.get(Product, payload.product_id)
        if product is None:
            return None
        if not any(
            [
                payload.product_code,
                payload.product_name,
                payload.snapshot_material,
                payload.snapshot_score_line,
                payload.snapshot_cardboard_length_mm,
                payload.snapshot_cardboard_width_mm,
            ]
        ):
            return product
    else:
        if not payload.product_name:
            return None
        history_key = _history_key(customer, payload)
        material = payload.snapshot_material
        product = session.scalar(
            select(Product).where(
                Product.customer_id == customer.id,
                Product.historical_search_key == history_key,
                Product.historical_material_code == material,
            )
        )
        if product is None and payload.product_code:
            product = session.scalar(
                select(Product).where(
                    Product.customer_id == customer.id,
                    or_(
                        Product.product_code == _normalize_code(payload.product_code, "TEMP"),
                        Product.customer_material_code == _normalize_code(payload.product_code, "TEMP"),
                    ),
                )
            )
        if product is None:
            base_code = _normalize_code(payload.product_code, f"TEMP-{customer.id}")
            product = Product(
                customer_id=customer.id,
                product_code=_unique_product_code(session, customer.id, base_code),
                customer_material_code=_unique_product_code(session, customer.id, base_code),
                product_name=payload.product_name,
                box_category="normal",
                default_pieces_per_sheet=payload.snapshot_pieces_per_sheet,
                is_active=True,
            )
            session.add(product)
            session.flush()

    product.product_name = payload.product_name or product.product_name
    if payload.product_code:
        normalized = _normalize_code(payload.product_code, product.product_code)
        product.product_code = _unique_product_code(session, customer.id, normalized, product.id)
        product.customer_material_code = product.product_code
    product.length_mm = payload.length_mm or product.length_mm
    product.width_mm = payload.width_mm or product.width_mm
    product.height_mm = payload.height_mm or product.height_mm
    product.default_material_text = payload.snapshot_material or product.default_material_text
    product.default_score_line = payload.snapshot_score_line or product.default_score_line
    product.default_cardboard_length_mm = payload.snapshot_cardboard_length_mm or product.default_cardboard_length_mm
    product.default_cardboard_width_mm = payload.snapshot_cardboard_width_mm or product.default_cardboard_width_mm
    product.default_pieces_per_sheet = payload.snapshot_pieces_per_sheet or product.default_pieces_per_sheet
    product.default_unit_price = q4(payload.unit_price) if payload.unit_price is not None else product.default_unit_price
    product.historical_search_key = _history_key(customer, payload) or product.historical_search_key
    product.historical_style_no = payload.product_code or product.historical_style_no
    product.historical_material_code = payload.snapshot_material or product.historical_material_code
    session.flush()
    return product


def _next_order_number(session: Session, order_date: date) -> str:
    prefix = f"PO-{order_date:%Y%m%d}-"
    max_number = session.scalar(
        select(func.max(SalesOrder.order_number)).where(SalesOrder.order_number.like(f"{prefix}%"))
    )
    if not max_number:
        return f"{prefix}001"
    try:
        serial = int(str(max_number).rsplit("-", 1)[1]) + 1
    except (IndexError, ValueError):
        serial = 1
    return f"{prefix}{serial:03d}"


def _item_from_product(session: Session, payload: OrderItemPayload) -> tuple[OrderItem, Decimal]:
    product = session.get(Product, payload.product_id)
    if product is None or not product.is_active:
        raise HTTPException(status_code=400, detail="常用箱不存在或已停用")
    unit_price = q4(payload.unit_price if payload.unit_price is not None else product.default_unit_price or Decimal("0"))
    subtotal = money(unit_price * Decimal(payload.quantity))
    item = OrderItem(
        product_id=product.id,
        quantity=payload.quantity,
        unit_price=unit_price,
        subtotal=subtotal,
        snapshot_product_code=product.product_code,
        snapshot_product_name=product.product_name,
        snapshot_spec=_spec(product.length_mm, product.width_mm, product.height_mm),
        snapshot_material=_material_snapshot(product),
        snapshot_flute_type=product.flute_type.code if product.flute_type else None,
        snapshot_score_line=product.default_score_line,
        snapshot_cardboard_length_mm=product.default_cardboard_length_mm,
        snapshot_cardboard_width_mm=product.default_cardboard_width_mm,
        snapshot_pieces_per_sheet=product.default_pieces_per_sheet,
        note=payload.note,
    )
    return item, subtotal


def _item_from_snapshot(payload: OrderItemPayload, product: Product | None = None) -> tuple[OrderItem, Decimal]:
    if not payload.product_name or payload.unit_price is None:
        raise HTTPException(status_code=400, detail="临时纸箱必须填写品名和单价")
    subtotal = money(q4(payload.unit_price) * Decimal(payload.quantity))
    item = OrderItem(
        product_id=product.id if product is not None else None,
        quantity=payload.quantity,
        unit_price=q4(payload.unit_price),
        subtotal=subtotal,
        snapshot_product_code=payload.product_code,
        snapshot_product_name=payload.product_name,
        snapshot_spec=_spec(payload.length_mm, payload.width_mm, payload.height_mm),
        snapshot_material=payload.snapshot_material,
        snapshot_flute_type=payload.snapshot_flute_type,
        snapshot_score_line=payload.snapshot_score_line,
        snapshot_cardboard_length_mm=payload.snapshot_cardboard_length_mm,
        snapshot_cardboard_width_mm=payload.snapshot_cardboard_width_mm,
        snapshot_pieces_per_sheet=payload.snapshot_pieces_per_sheet,
        note=payload.note,
    )
    return item, subtotal


def order_item_dict(item: OrderItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "product_id": item.product_id,
        "quantity": item.quantity,
        "unit_price": str(item.unit_price),
        "subtotal": str(item.subtotal),
        "snapshot_product_code": item.snapshot_product_code,
        "snapshot_product_name": item.snapshot_product_name,
        "snapshot_spec": item.snapshot_spec,
        "snapshot_material": item.snapshot_material,
        "snapshot_flute_type": item.snapshot_flute_type,
        "snapshot_score_line": item.snapshot_score_line,
        "snapshot_cardboard_length_mm": None if item.snapshot_cardboard_length_mm is None else str(item.snapshot_cardboard_length_mm),
        "snapshot_cardboard_width_mm": None if item.snapshot_cardboard_width_mm is None else str(item.snapshot_cardboard_width_mm),
        "snapshot_pieces_per_sheet": item.snapshot_pieces_per_sheet,
    }


def order_dict(order: SalesOrder) -> dict[str, Any]:
    return {
        "id": order.id,
        "order_number": order.order_number,
        "customer_id": order.customer_id,
        "customer_name": order.customer.name if order.customer else None,
        "customer_po": order.customer_po,
        "order_date": order.order_date.isoformat(),
        "delivery_date": order.delivery_date.isoformat() if order.delivery_date else None,
        "status": order.status,
        "payment_status": order.payment_status,
        "total_amount": str(order.total_amount),
        "items": [order_item_dict(item) for item in order.items],
    }


@router.post("", status_code=status.HTTP_201_CREATED)
def create_order(payload: OrderPayload, session: Session = Depends(get_session)) -> dict[str, Any]:
    customer = session.get(Customer, payload.customer_id)
    if customer is None or not customer.is_active:
        raise HTTPException(status_code=400, detail="客户不存在或已停用")

    order = SalesOrder(
        order_number=_next_order_number(session, payload.order_date),
        customer_id=payload.customer_id,
        customer_po=payload.customer_po,
        order_date=payload.order_date,
        delivery_date=payload.delivery_date,
        status="ordered",
        payment_status="unsettled",
        total_amount=Decimal("0.00"),
        note=payload.note,
    )
    total = Decimal("0.00")
    for line in payload.items:
        if line.product_id:
            item, subtotal = _item_from_product(session, line)
            _persist_history_formula(session, customer, line)
        else:
            persisted_product = _persist_history_formula(session, customer, line)
            item, subtotal = _item_from_snapshot(line, persisted_product)
        order.items.append(item)
        total += subtotal
    order.total_amount = money(total)

    try:
        session.add(order)
        session.commit()
    except IntegrityError as error:
        session.rollback()
        raise HTTPException(status_code=409, detail="订单号冲突，请重试") from error
    session.refresh(order)
    return order_dict(order)


@router.get("")
def list_orders(
    customer_id: int | None = None,
    page: int = 1,
    page_size: int = 50,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    query = select(SalesOrder).order_by(SalesOrder.order_date.desc(), SalesOrder.id.desc())
    if customer_id is not None:
        query = query.where(SalesOrder.customer_id == customer_id)
    total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
    orders = session.scalars(query.offset((page - 1) * page_size).limit(page_size)).unique().all()
    return {"total": total, "page": page, "page_size": page_size, "items": [order_dict(order) for order in orders]}
